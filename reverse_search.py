"""
以圖搜圖模組：SauceNAO（優先）→ soutubot（fallback）
- SauceNAO 有 ≥80% 且有連結 → 只輸出 SauceNAO 結果
- 否則輸出 soutubot ≥80% 結果
- 原始回傳全數寫入 log，不做任何篩選
"""
import asyncio
import os
import tempfile
import requests
from config import SAUCENAO_API_KEY


# ── 常數 ────────────────────────────────────────────────────────────────────

_SAUCENAO_URL  = 'https://saucenao.com/search.php'
_SOUTUBOT_BASE = 'https://soutubot.moe'
_SIM_THRESHOLD = 80

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
            results.append({
                'engine':     'soutubot',
                'source':     source,
                'title':      title,
                'author':     '',
                'page':       page,
                'url':        url,
                'similarity': 99.0,
            })
        return results

    except Exception as e:
        print(f'[SOUTU] 搜尋失敗: {e}')
        return []
    finally:
        os.unlink(tmp.name)


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
    1. SauceNAO 有 ≥80% 且有連結 → 只輸出 SauceNAO 結果
    2. 否則 fallback 到 soutubot ≥80% 結果
    """
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

    other_hits = [r for r in _hits(soutu) if r.get('page')]
    if other_hits:
        print(f'[RSEARCH] SauceNAO 無符合，soutubot 命中（含page）{len(other_hits)} 筆')
        lines = [f'找到 {len(other_hits)} 筆相似度 ≥{_SIM_THRESHOLD}% 的結果：']
        for i, r in enumerate(other_hits, 1):
            lines.append(_format_result(i, r))
        return '\n\n'.join(lines)

    return '找不到相似的資訊喵QQ'
