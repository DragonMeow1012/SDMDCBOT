"""
以圖搜圖模組：本地 Pixiv（優先）→ SauceNAO → soutubot（fallback）
- 本地 Pixiv FAISS pHash ≥95% → 只輸出本地結果
- SauceNAO 有 ≥80% 且有連結 → 只輸出 SauceNAO 結果
- 否則輸出 soutubot ≥50% 結果
- 原始回傳全數寫入 log，不做任何篩選
"""
import asyncio
import io
import os
import tempfile
import requests
from config import SAUCENAO_API_KEY


# ── 常數 ────────────────────────────────────────────────────────────────────

_PIXIV_LOCAL_THRESHOLD = 95.0    # pHash 相似度百分比（100 - Hamming/64*100）
_PIXIV_LOCAL_TOP_K     = 1      # 最多回傳幾筆本地結果

_SAUCENAO_URL  = 'https://saucenao.com/search.php'
_SOUTUBOT_BASE = 'https://soutubot.moe'
_SIM_THRESHOLD       = 80
_SOUTU_SIM_THRESHOLD = 50

_HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
}

_INDEX_NAMES: dict[int, str] = {
    5:  'pixiv',
    6:  'pixiv',
    8:  'nico nico seiga',
    9:  'danbooru',
    12: 'yande.re',
    16: 'FAKKU',
    18: 'nhentai',
    21: 'anime',
    22: 'H-Misc',
    25: 'gelbooru',
    26: 'konachan',
    38: 'e-hentai',
}

_SOUTUBOT_BASE_URLS: dict[str, str] = {
    'nhentai':  'https://nhentai.net',
    'pixiv':    'https://www.pixiv.net',
    'e-hentai': 'https://e-hentai.org',
}


# ── SauceNAO ─────────────────────────────────────────────────────────────────

def _parse_saucenao_entry(r: dict) -> dict | None:
    hdr = r.get('header', {})
    dat = r.get('data', {})
    sim = float(hdr.get('similarity', 0))
    if sim < 50:
        return None

    idx    = int(hdr.get('index_id', -1))
    source = _INDEX_NAMES.get(idx, hdr.get('index_name', ''))
    title  = dat.get('title') or dat.get('material') or '未知'
    author = dat.get('member_name') or dat.get('creator') or dat.get('author') or ''
    page   = dat.get('part') or dat.get('page') or ''

    ext_urls   = hdr.get('ext_urls', [])
    dat_source = dat.get('source', '')
    url = ext_urls[0] if ext_urls else (dat_source if dat_source.startswith('http') else '')

    if idx == 18 and dat.get('nh_id'):
        url = url or f'https://nhentai.net/g/{dat["nh_id"]}/'

    # nhentai URL 格式：nhentai.net/g/{id}/{page}
    # 從 URL 提取頁數，並將 URL 還原為不含頁數的作品連結
    if 'nhentai.net/g/' in url:
        parts = url.rstrip('/').split('/')
        if len(parts) >= 2 and parts[-1].isdigit():
            if not page:
                page = parts[-1]
            url = '/'.join(parts[:-1])

    return {
        'engine':     'SauceNAO',
        'source':     source,
        'title':      title,
        'author':     author,
        'page':       str(page) if page else '',
        'url':        url,
        'similarity': sim,
    }


async def _saucenao_search(image_data: bytes, mime_type: str) -> list[dict]:
    params: dict = {'output_type': 2, 'numres': 8, 'db': 999}
    if SAUCENAO_API_KEY:
        params['api_key'] = SAUCENAO_API_KEY

    try:
        await asyncio.sleep(0.5)
        resp = await asyncio.to_thread(
            requests.post,
            _SAUCENAO_URL,
            headers=_HEADERS,
            files={'file': ('image', image_data, mime_type)},
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json().get('results', [])

        # log 原始回傳（不篩選）
        print(f'[SAUCE] 原始回傳 {len(raw)} 筆：')
        for r in raw:
            hdr    = r.get('header', {})
            dat    = r.get('data', {})
            sim    = hdr.get('similarity', '?')
            idx    = hdr.get('index_id', '?')
            name   = hdr.get('index_name', '')
            urls   = hdr.get('ext_urls', [])
            title  = dat.get('title') or dat.get('source') or dat.get('material') or ''
            author = dat.get('member_name') or dat.get('creator') or dat.get('author') or ''
            print(f'  [{sim:>6}%] idx={idx} {name} | {title} | {author} | {urls}')

        results = []
        for r in raw:
            parsed = _parse_saucenao_entry(r)
            if parsed:
                results.append(parsed)
            if len(results) >= 5:
                break
        return results

    except Exception as e:
        print(f'[SAUCE] 搜尋失敗: {e}')
        return []


# ── soutubot ─────────────────────────────────────────────────────────────────

async def _soutubot_search(image_data: bytes, mime_type: str) -> list[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('[SOUTU] playwright 未安裝，跳過')
        return []

    ext = '.jpg' if 'jpeg' in mime_type else ('.gif' if 'gif' in mime_type else '.png')
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        tmp.write(image_data)
        tmp.close()

        captured: list[dict] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            ctx = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                locale='zh-TW',
                timezone_id='Asia/Taipei',
                viewport={'width': 1280, 'height': 800},
                extra_http_headers={
                    'Accept-Language':    'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                    'sec-ch-ua':          '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
                    'sec-ch-ua-mobile':   '?0',
                    'sec-ch-ua-platform': '"Windows"',
                },
            )
            page = await ctx.new_page()
            await page.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )

            async def on_response(resp):
                if '/api/search' in resp.url:
                    try:
                        body = await resp.json()
                        items = body.get('data') or []
                        if items:
                            captured.extend(items)
                    except Exception:
                        pass

            page.on('response', on_response)
            await page.goto(_SOUTUBOT_BASE + '/', wait_until='load', timeout=30000)
            await page.wait_for_timeout(800)

            file_input = page.locator('input[type="file"]').first
            await file_input.wait_for(state='attached', timeout=15000)
            await file_input.set_input_files(
                {'name': f'image{ext}', 'mimeType': mime_type, 'buffer': image_data}
            )

            try:
                await page.wait_for_function(
                    'document.querySelector("[class*=result],[class*=Result]") !== null',
                    timeout=25000,
                )
            except Exception:
                await page.wait_for_timeout(5000)

            await browser.close()

        # log 原始回傳（不篩選）
        print(f'[SOUTU] 原始回傳 {len(captured)} 筆：')
        for item in captured:
            print(f'  {item}')

        results = []
        for item in captured[:3]:
            source    = item.get('source', '')
            title     = item.get('title') or '未知'
            page      = str(item.get('page', '')) if item.get('page') is not None else ''
            page_path = item.get('pagePath', '')
            subj      = item.get('subjectPath', '')
            base      = _SOUTUBOT_BASE_URLS.get(source, f'https://{source}' if source else '')
            # 連結固定用作品 URL（不含頁數）
            path      = subj
            url       = (base + path) if path else ''
            sim = float(item.get('similarity', 0))
            results.append({
                'engine':     'soutubot',
                'source':     source,
                'title':      title,
                'author':     '',
                'page':       page,
                'url':        url,
                'similarity': sim,
            })
        return results

    except Exception as e:
        print(f'[SOUTU] 搜尋失敗: {e}')
        return []
    finally:
        os.unlink(tmp.name)


# ── 本地 Pixiv FAISS 搜尋 ─────────────────────────────────────────────────────

async def _pixiv_local_search(image_data: bytes) -> list[dict]:
    """
    用 pHash 在本地 FAISS 二值索引（Hamming 距離）搜尋相似作品。
    similarity = (64 - hamming) / 64 * 100，閾值 _PIXIV_LOCAL_THRESHOLD（百分比）。
    """
    try:
        import numpy as np
        from PIL import Image
        import pixiv_feature as fe
        import pixiv_database as db

        # 提取查詢圖片的 pHash
        img = Image.open(io.BytesIO(image_data)).convert("RGB")
        query_vec = fe.extract_phash(img).reshape(1, -1).astype(np.uint8)

        # 載入 FAISS 二值索引
        index, id_list = fe.load_faiss_index()
        if index is None or not id_list:
            print("[PIXIV_LOCAL] 索引尚未建立，跳過本地搜尋")
            return []

        k = min(_PIXIV_LOCAL_TOP_K, index.ntotal)
        distances, indices = index.search(query_vec, k)

        _PHASH_BITS = 64
        results = []
        high_sim_results = []
        for hamming, idx in zip(distances[0], indices[0]):
            if idx < 0:
                continue
            sim_pct = round((1.0 - hamming / _PHASH_BITS) * 100, 1)
            if sim_pct >= 90.0:
                illust_id = id_list[idx]
                row = db.get_artwork(illust_id)
                if row:
                    high_sim_results.append({
                        'illust_id': illust_id,
                        'title':     row['title'],
                        'author':    row['user_name'],
                        'similarity': sim_pct,
                    })
            if sim_pct < _PIXIV_LOCAL_THRESHOLD:
                continue
            illust_id = id_list[idx]
            row = db.get_artwork(illust_id)
            if row is None:
                continue
            results.append({
                'engine':     'Pixiv本地',
                'source':     'pixiv',
                'title':      row['title'],
                'author':     row['user_name'],
                'page':       '',
                'url':        f'https://www.pixiv.net/artworks/{illust_id}',
                'similarity': sim_pct,
            })

        if high_sim_results:
            print(f"[PIXIV_LOCAL] ≥90% 結果：")
            for res in high_sim_results:
                print(f"  ID:{res['illust_id']} | {res['title']} | {res['author']} | {res['similarity']}%")

        print(f"[PIXIV_LOCAL] 命中 {len(results)} 筆 ≥{_PIXIV_LOCAL_THRESHOLD:.0f}%")
        return results

    except Exception as e:
        print(f"[PIXIV_LOCAL] 搜尋失敗: {e}")
        return []


# ── 格式化 ────────────────────────────────────────────────────────────────────

def _format_result(i: int, r: dict) -> str:
    source = r['source'] or '未知來源'
    title  = r['title']
    author = r['author']
    page   = r.get('page', '')
    sim    = r['similarity']
    url    = r['url']
    parts  = [source, title]
    if author:
        parts.append(author)
    if page:
        parts.append(f'page {page}')
    parts.append(f'{sim:.1f}%')
    return f'`{i}.` {" | ".join(parts)}\n連結:**{url}**'


# ── 主搜尋入口 ────────────────────────────────────────────────────────────────

async def reverse_image_search(image_data: bytes, mime_type: str) -> str:
    """
    1. 本地 Pixiv FAISS ≥99.8% → 優先輸出本地結果（最多1筆）
    2. 如果本地結果有新刊或R-18標籤，追加 soutubot 結果
    3. SauceNAO 有 ≥80% 且有連結 → 輸出 SauceNAO 結果
    4. 否則 fallback 到 soutubot ≥50% 結果
    """
    # ── 1. 本地 Pixiv 優先 ──
    local_hits = await _pixiv_local_search(image_data)
    if local_hits:
        lines = ['本地資料庫搜尋結果：']
        for i, r in enumerate(local_hits, 1):
            lines.append(_format_result(i, r))

        # 檢查標籤是否包含新刊、R-18 或 本/Cxxx 類標籤
        append_soutu = False
        if local_hits:
            import pixiv_database as db
            import json
            illust_id = int(local_hits[0]['url'].split('/')[-1])
            row = db.get_artwork(illust_id)
            if row and row['tags']:
                tags = json.loads(row['tags'])
                if any(tag in ['新刊', 'R-18'] or tag.startswith('本') or (tag.startswith('C') and tag[1:].isdigit()) for tag in tags):
                    append_soutu = True

        if append_soutu:
            # 執行 soutubot 搜尋並追加
            soutu = await _soutubot_search(image_data, mime_type)
            soutu_hits = sorted(
                [r for r in soutu if r['url']
                 and r['similarity'] >= _SOUTU_SIM_THRESHOLD
                 and r.get('page')],
                key=lambda r: r['similarity'],
                reverse=True,
            )
            if soutu_hits:
                for i, r in enumerate(soutu_hits[:3], len(local_hits) + 1):  # 從本地之後繼續編號
                    lines.append(_format_result(i, r))

        return '\n\n'.join(lines)

    # ── 2 & 3. 外部搜尋（並行）──
    soutu, sauce = await asyncio.gather(
        _soutubot_search(image_data, mime_type),
        _saucenao_search(image_data, mime_type),
    )

    def _hits(pool: list[dict]) -> list[dict]:
        return sorted(
            [r for r in pool if r['url']
             and r['similarity'] >= _SIM_THRESHOLD
             and 'i.pximg.net' not in r['url']],
            key=lambda r: r['similarity'],
            reverse=True,
        )

    sauce_hits = _hits(sauce)
    if sauce_hits:
        print(f'[RSEARCH] SauceNAO 命中 {len(sauce_hits)} 筆 ≥{_SIM_THRESHOLD}%')
        lines = [f'找到 {len(sauce_hits)} 筆相似度 ≥{_SIM_THRESHOLD}% 的結果：']
        for i, r in enumerate(sauce_hits, 1):
            lines.append(_format_result(i, r))
        return '\n\n'.join(lines)

    soutu_hits = sorted(
        [r for r in soutu if r['url']
         and r['similarity'] >= _SOUTU_SIM_THRESHOLD
         and r.get('page')],
        key=lambda r: r['similarity'],
        reverse=True,
    )
    if soutu_hits:
        print(f'[RSEARCH] SauceNAO 無符合，soutubot 命中（含page）{len(soutu_hits)} 筆 ≥{_SOUTU_SIM_THRESHOLD}%')
        lines = [f'找到 {len(soutu_hits)} 筆相似度 ≥{_SOUTU_SIM_THRESHOLD}% 的結果：']
        for i, r in enumerate(soutu_hits, 1):
            lines.append(_format_result(i, r))
        return '\n\n'.join(lines)

    return '找不到相關連結喵QQ'
