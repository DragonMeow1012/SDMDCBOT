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


def _maybe_downscale(image_bytes: bytes, mime: str) -> tuple[bytes, str, int]:
    """
    單邊超過 _MAX_IMG_DIM 時等比縮，回傳 (新 bytes, 新 mime, 縮後最大邊)。
    第三項用來決定 inpainting_size，等於圖實際最大邊→inpaint 不會再 resize → 不糊。
    讀圖失敗時回傳 (原 bytes, 原 mime, 0)，inpainting_size 用 fallback。
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            w, h = im.size
            if max(w, h) <= _MAX_IMG_DIM:
                return image_bytes, mime, max(w, h)
            scale = _MAX_IMG_DIM / max(w, h)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            print(f'[TRANSLATE] 圖片 {w}x{h} 超過 {_MAX_IMG_DIM}，縮到 {new_size[0]}x{new_size[1]}')
            resized = im.convert('RGB').resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format='PNG')
            return buf.getvalue(), 'image/png', max(new_size)
    except Exception as e:
        print(f'[TRANSLATE] 縮圖失敗，沿用原圖: {type(e).__name__}: {e}')
        return image_bytes, mime, 0


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
            # default 保留：ctd 實測對細字／手寫敏感度比 default 還差（漏更多 region），
            # dbconvnext 又沒提供模型權重 URL（upstream bug）。default + 全開。
            'detector': 'default',
            # 高解析度多撈小字／淡字／手寫字（4096 撞 OOM；3072 是穩定上限）
            'detection_size': 3072,
            # box 外擴比例。預設 2.3 會讓相鄰氣泡的 bbox 重疊→textline_merge
            # 把兩個氣泡併成一個 region（譯文跨氣泡黏在一起）。降到 1.5 讓
            # box 收斂只覆蓋實際文字，相近氣泡才能分開。
            # 副作用：同氣泡內行距較大時可能切成多 region，由 textline_merge
            # 的 char_gap_tolerance 接住，整體仍會合回同一框。
            'unclip_ratio': 1.5,
            # 門檻再壓低（預設 0.5/0.7）→ 多撈淡墨／手寫／小字（寧濫勿漏）
            'text_threshold': 0.15,
            'box_threshold': 0.15,
            # 全開所有圖像增強選項：旋轉／反相／伽瑪 一次掃完
            'det_auto_rotate': True,
            'det_rotate': True,        # 雙方向都跑（垂直＋橫向）
            'det_invert': True,        # 反白底字／白字黑底特效（夜景 SFX、黑色對話框）
            'det_gamma_correct': True, # 低對比掃描原稿、淡墨手寫
        },
        'ocr': {
            # 單字對話「啊」「！」「？」也要保留（預設 min=2 會濾掉）
            'min_text_length': 1,
            # OCR 信心門檻：預設模型內部 ~0.5，壓到 0.05 連極模糊／極小字也認
            'prob': 0.05,
            # 不過濾非氣泡區的文字（旁白、SFX、效果文字）；0 = 全收
            'ignore_bubble': 0,
        },
        'render': {
            # 字級沿用 OCR 偵測到的原文字級（offset=0）。renderer 算出文字塞不下時
            # 會自動 scale region；負 offset 會讓字偏小、留太多白邊（排不滿）。
            'font_size_offset': 0,
            # 中文不該斷字（連字號是西文渲染遺留）
            'no_hyphenation': True,
            # line_spacing 不指定，走 renderer default（橫排 0.01／直排 0.2），
            # 多行對白靠 region 自動擴張容納，不靠擠行距
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
