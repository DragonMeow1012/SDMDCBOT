"""
漫畫翻譯（manga-image-translator API server 接後端）。

對外：translate_image(image_bytes, mime, target_lang) -> bytes  (PNG)

server 端啟動：
    cd <manga-image-translator>/server && python main.py --use-gpu
URL 由 config.MANGA_TRANSLATOR_URL 控制（預設 http://127.0.0.1:8001）。

Gemini 翻譯後端的 API Key 由 server 進程的環境變數 GOOGLE_API_KEY 提供，
bot 端不參與 key 管理。

注意：用 streaming endpoint `/translate/with-form/image/stream` 而不是同步版本。
upstream 的 `/translate/with-form/image` 走 server/sent_data_internal.py:fetch_data，
裡面寫死 json.loads 解析 worker 回傳的 pickle bytes，永遠 fail → 包成 500 給 client。
streaming 端點走 fetch_data_stream → process_stream，正確處理 binary chunks。

stream chunk 格式（server/streaming.py）：
    1 byte status | 4 byte big-endian size | N bytes data
    status: 0=最終結果, 1=進度文字, 2=錯誤, 3=排隊位置, 4=等待 worker
"""
import asyncio
import io
import json
import time

import aiohttp
from PIL import Image

from config import (
    MANGA_TRANSLATOR_BACKEND,
    MANGA_TRANSLATOR_CONCURRENCY,
    MANGA_TRANSLATOR_URL,
)

# client-side 並行閘門。server 端單 worker 還是會在 pipeline 序列化，
# 但允許 N 個請求同時進 server queue（status 3/4 會回報排隊位置），
# 本地 LM Studio 沒 rate limit，這條只避免無限塞滿 queue。
_WORKER_SEM = asyncio.Semaphore(MANGA_TRANSLATOR_CONCURRENCY)

# 中文人類名 → manga-image-translator 用的 ISO 代碼
_LANG_CODE: dict[str, str] = {
    '繁體中文': 'CHT',
    '簡體中文': 'CHS',
    'English':  'ENG',
    '日本語':   'JPN',
    '한국어':   'KOR',
}

# 第一次請求 server 要載入 detection / OCR / inpaint 模型，可能 30-60s。
# 之後常駐記憶體，後續請求 GPU 模式 ~3-8s/頁、CPU 模式 ~15-40s/頁。
# 上限 180s = 給冷啟動 60s + 慢圖 100s + 緩衝 20s；超過視為這張卡死，
# 由上層（commands/translate.py 的 _one）catch 後跳下一張，不拖累整批。
_PER_IMAGE_TIMEOUT = 300
_TIMEOUT = aiohttp.ClientTimeout(total=_PER_IMAGE_TIMEOUT)

# Worker 冷啟動時 server 會回 "Translation service is starting up" status=2 錯誤，
# 不是真的失敗、就是還在載模型。第一張圖必中招——直接等待重試讓 user 層感覺不到。
# 12 次 × 5s = 最多等 60s 給 worker 暖機，超過就放棄當真錯。
_WARMUP_RETRY_MAX = 12
_WARMUP_RETRY_DELAY = 5.0
_WARMUP_ERROR_KEYWORDS = ('starting up', 'starting up,')

# OpenCV cv::remap 用 16-bit signed indices，SHRT_MAX=32767。manga-translator pipeline
# 內部 resize 過程容易讓任一維度撞到上限（特別是長條 webtoon），會 raise:
#   OpenCV(...) error: (-215:Assertion failed) dst.cols < SHRT_MAX && ... in function 'cv::remap'
# 客戶端先把超大圖等比縮到 _MAX_IMG_DIM 內再送 server，避免這條 assertion。
#
# 12000 是相對 SHRT_MAX（32767）有 2.7x 緩衝的高解析度上限：
# - 普通漫畫頁 < 4000px → 完全不觸發
# - 長條 webtoon（高 20000-30000px）→ 縮到 12000，文字仍清晰可讀
# 太低（如 4000-8000）會犧牲 webtoon 的文字解析度。
_MAX_IMG_DIM = 12000

# 下限：低於這個就放大。manga-translator 的 inpainter 邏輯是「只縮不放」，
# 小圖（如 600x864）inpainter 直接在原解析度跑 → 文字渲染像素化／糊。
# 客戶端預先放大到 _MIN_IMG_DIM 強制 inpainter 在合理解析度上跑。
# 1280 = 比原始小圖大 ~1.3-1.5x，不會劇烈失真；inpainter 在 1280 跑字夠清楚。
# 之前用 2048 對 867x1024 等小圖直接 2x 放大，過度。
_MIN_IMG_DIM = 1280


def _maybe_resize(image_bytes: bytes, mime: str) -> tuple[bytes, str, int]:
    """
    把圖縮到 [_MIN_IMG_DIM, _MAX_IMG_DIM] 範圍：
    - max_dim > _MAX_IMG_DIM → 等比縮（避免 OpenCV cv::remap 撞 SHRT_MAX）。
    - max_dim < _MIN_IMG_DIM → 等比放大（避免 inpainter 在低解析度跑導致字糊）。
    - 區間內 → 不動。
    回傳 (bytes, mime, 處理後 max_dim)。讀圖失敗回傳原圖。
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            w, h = im.size
            cur_max = max(w, h)
            if _MIN_IMG_DIM <= cur_max <= _MAX_IMG_DIM:
                return image_bytes, mime, cur_max
            if cur_max > _MAX_IMG_DIM:
                scale = _MAX_IMG_DIM / cur_max
                action = '縮小'
            else:
                scale = _MIN_IMG_DIM / cur_max
                action = '放大（避免字糊）'
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            print(f'[TRANSLATE] 圖片 {w}x{h} {action}到 {new_size[0]}x{new_size[1]}')
            resized = im.convert('RGB').resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format='PNG')
            return buf.getvalue(), 'image/png', max(new_size)
    except Exception as e:
        print(f'[TRANSLATE] resize 失敗，沿用原圖: {type(e).__name__}: {e}')
        return image_bytes, mime, 0


# 向下相容舊呼叫（如果有）
_maybe_downscale = _maybe_resize


async def _read_stream(resp: aiohttp.ClientResponse) -> bytes:
    """
    從 streaming endpoint 累積 chunk 直到拿到最終結果（status 0）或錯誤（status 2）。
    其他 status（progress / queue pos / waiting）只用來印 log。
    """
    buffer = b''
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # 一個 chunk 可能塞多個 message 也可能不滿一個 message，loop 解析
        while len(buffer) >= 5:
            status = buffer[0]
            size = int.from_bytes(buffer[1:5], 'big')
            if len(buffer) < 5 + size:
                break  # 等下一個 chunk
            payload = buffer[5:5 + size]
            buffer = buffer[5 + size:]

            if status == 0:
                return payload  # 最終 PNG bytes
            if status == 2:
                raise RuntimeError(f'manga-translator worker error: {payload.decode("utf-8", errors="replace")[:300]}')
            if status == 1:
                try:
                    print(f'[TRANSLATE]   進度: {payload.decode("utf-8", errors="replace")[:120]}')
                except Exception:
                    pass
            elif status == 3:
                print(f'[TRANSLATE]   排隊位置: {payload.decode("utf-8", errors="replace")}')
            elif status == 4:
                print('[TRANSLATE]   等待 worker 取件...')
    raise RuntimeError('manga-translator stream 結束但未收到結果')


async def translate_image(image_bytes: bytes, mime: str, target_lang: str) -> bytes:
    """
    把漫畫圖丟到 manga-image-translator server 翻譯，回傳翻譯後 PNG bytes。

    target_lang：接受 _LANG_CODE 內的中文鍵名（'繁體中文' 等），未知值預設 CHT。
    """
    image_bytes, mime, _ = await asyncio.to_thread(_maybe_downscale, image_bytes, mime)
    code = _LANG_CODE.get(target_lang, 'CHT')
    # 重點是「不漏字」。detector/OCR 都拉低門檻多撈一點，
    # 寧可多撈幾個雜訊 box（gemini 看圖會把雜訊修正成空字串自然濾掉），也不要漏對話。
    config_payload = json.dumps({
        'translator': {
            'target_lang': code,
            'translator': MANGA_TRANSLATOR_BACKEND,
            # 上游 page-/batch-level target language check 失敗時整批重打 N 次。
            # 本地模型「重打」不會突然會翻譯，只是把 5K-token prompt 重打浪費時間。
            # 設 0 = 第一次失敗就放棄，render 已得結果（多半是部份翻譯+原文），
            # 比卡 5 分鐘有意義。雲端 Gemini 才需要 retry（網路抖動）。
            'post_check_max_retry_attempts': 0,
        },
        'detector': {
            # default (dbnet) 保留：ctd 實測對細字／手寫敏感度比 default 還差（漏更多 region），
            # dbconvnext 又沒提供模型權重 URL（upstream bug）。
            'detector': 'default',
            # 速度優先：3072 → 1280（720p 等級）。detector dbnet 對 1280 仍有合理偵測力，
            # 漫畫頁主對話氣泡都是大字會被抓到；極小腳註／淡墨手寫會漏（再個別調回）。
            'detection_size': 1280,
            # box 外擴比例。預設 2.3 太鬆 → bbox 跨氣泡／跨格子；
            # 1.0 太緊 → polygon 漏邊緣字 + dst_points 比 inpaint mask 小 → 對話框被塗白但無字。
            # 1.2：跨格較不會發生（DBNet polygon 不會延伸到鄰格），同時 polygon 涵蓋完整字符。
            'unclip_ratio': 1.2,
            # 門檻 0.15（預設 0.5/0.7）→ 多撈淡墨／手寫／小字。
            # 試過 0.05 但會撈大量重疊小框，textline_merge 把它們合成大區域→ 譯文位置錯亂。
            # 0.15 是「敏感但不破壞 region 結構」的平衡點。
            'text_threshold': 0.15,
            'box_threshold': 0.15,
            # 圖像增強全關：每開一個 detector 多跑 1 趟。原本 4 個全開 = 6 趟。
            # 漏字再個別開回，例如黑色對話框多就開 det_invert。
            'det_auto_rotate': False,
            'det_rotate': False,
            'det_invert': False,
            'det_gamma_correct': False,
        },
        'ocr': {
            # mocr (manga-ocr) = 漫畫專用 OCR 模型，對日文手寫／網點／壓縮字遠強於 48px。
            # **這是「對話框漏翻」最有效的解法**——OCR 認不出整個 region 會被丟。
            # 缺點：稍慢（每框約 +0.5s）、需下載模型（首次幾分鐘）。
            'ocr': 'mocr',
            # use_mocr_merge 不開：實測會把跨對話框的 region 亂合併導致位置錯亂。
            # 下游 LLM 「多框連讀」prompt 已經能處理被切碎的長句。
            'use_mocr_merge': False,
            # 單字對話「啊」「！」「？」也要保留（預設 min=2 會濾掉）
            'min_text_length': 1,
            # OCR 信心門檻：預設模型內部 ~0.5，壓到 0.05 連極模糊／極小字也認
            'prob': 0.05,
            # 不過濾非氣泡區的文字（旁白、SFX、效果文字）；0 = 全收
            'ignore_bubble': 0,
        },
        'inpainter': {
            # lama_mpe = 漫畫專用 LaMa（網點／線條優化），比預設 lama_large 對漫畫
            # 場景的紋理重建更精細（衣服皺褶、頭髮陰影較不易塌成色塊）。
            # LaMa 系列是純 vision 模型不吃 prompt；要 prompt 控制需換 SD inpainter，
            # 但 SD 對 R18 內容會被 safety filter 擋且速度從幾秒→幾十秒/張，不採用。
            'inpainter': 'lama_mpe',
            # 1792：對話框內補白（主要 inpaint 區）對解析度不敏感、白底紋理單純；
            # 2560 → 1792 推理時間 -40%（GPU 工作量正比於 dim²）。
            # 場景紋理重建（衣服皺褶、頭髮陰影）會稍粗，但漫畫翻譯主要在意對話框乾淨。
            # 想恢復細節：拉回 2048-2560；極端品質：3072（慎防 OOM）。
            'inpainting_size': 1792,
            # bf16 是 LaMa 預設精度，速度＋精度最佳平衡。
            'inpainting_precision': 'bf16',
        },
        'render': {
            # === 自適應氣泡框 ===
            # offset 0：不主動縮也不主動放，由 __init__.py 內的長短譯文邏輯決定縮放
            #   - 譯文比原文長 → 縮字（防超框）
            #   - 譯文比原文短 → 放字（填滿氣泡，避免空白）
            'font_size_offset': 0,
            # minimum 14px：6 太小（中文 6px 等於看不見、視覺像空白對話框）。
            # 14 是中文最小可讀字級；fit_check 算出 < 14 時硬抬到 14，寧可微出框也別空白。
            'font_size_minimum': 14,
            # 中文不斷字
            'no_hyphenation': True,
            # 多行各行頭對齊（不居中，避免短行內縮歪掉）
            'alignment': 'left',
        },
    })

    url = f'{MANGA_TRANSLATOR_URL.rstrip("/")}/translate/with-form/image/stream'
    form = aiohttp.FormData()
    form.add_field(
        'image', image_bytes,
        filename='page.png',
        content_type=mime or 'image/png',
    )
    form.add_field('config', config_payload)

    size_kb = len(image_bytes) / 1024
    # 排隊等 semaphore（上限 MANGA_TRANSLATOR_CONCURRENCY 個並發）
    queued_at = time.monotonic()
    if _WORKER_SEM.locked():
        print(f'[TRANSLATE] 並發閘門滿，排隊中... ({size_kb:.0f}KB, {code})')
    async with _WORKER_SEM:
        waited = time.monotonic() - queued_at
        print(f'[TRANSLATE] 送出 → {url} ({size_kb:.0f}KB, {code}'
              f'{f", 等待 {waited:.1f}s" if waited > 1 else ""})')
        started = time.monotonic()

        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            for attempt in range(_WARMUP_RETRY_MAX):
                try:
                    async with session.post(url, data=form) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            elapsed = time.monotonic() - started
                            print(f'[TRANSLATE] 失敗 ({elapsed:.1f}s) HTTP {resp.status}: {body[:200]}')
                            raise RuntimeError(
                                f'manga-translator HTTP {resp.status}: {body[:300]}')
                        data = await _read_stream(resp)
                        elapsed = time.monotonic() - started
                        print(f'[TRANSLATE] 完成 ({elapsed:.1f}s, 回傳 {len(data)/1024:.0f}KB)')
                        return data
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - started
                    print(f'[TRANSLATE] 超時 ({elapsed:.1f}s > {_PER_IMAGE_TIMEOUT}s)，跳過這張')
                    raise RuntimeError(f'這張圖翻譯超過 {_PER_IMAGE_TIMEOUT} 秒，已跳過')
                except RuntimeError as e:
                    msg = str(e).lower()
                    if any(k in msg for k in _WARMUP_ERROR_KEYWORDS) and attempt < _WARMUP_RETRY_MAX - 1:
                        print(f'[TRANSLATE] worker 暖機中，{_WARMUP_RETRY_DELAY:.0f}s 後重試 '
                              f'({attempt + 1}/{_WARMUP_RETRY_MAX})')
                        await asyncio.sleep(_WARMUP_RETRY_DELAY)
                        # form 是 single-use，要重建
                        form = aiohttp.FormData()
                        form.add_field('image', image_bytes, filename='page.png',
                                       content_type=mime or 'image/png')
                        form.add_field('config', config_payload)
                        started = time.monotonic()
                        continue
                    raise
        # 不會到這裡，loop 內非 return 就 raise
        raise RuntimeError('translate_image: unreachable')
