"""
以圖搜圖模組：soutubot → SauceNAO。
優先顯示 pixiv/twitter/x 連結，其次 nhentai，直接格式化輸出不經 Gemini。
"""
import asyncio
import requests
from config import SAUCENAO_API_KEY

_SAUCENAO_URL = 'https://saucenao.com/search.php'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
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

_PRIORITY_SOURCES = frozenset({'pixiv', 'twitter', 'x', 'x.com'})
_NH_SOURCES       = frozenset({'nhentai'})


def _parse_saucenao(r: dict) -> dict | None:
    hdr = r.get('header', {})
    dat = r.get('data', {})
    sim = float(hdr.get('similarity', 0))
    if sim < 50:
        return None

    idx    = int(hdr.get('index_id', -1))
    source = _INDEX_NAMES.get(idx, hdr.get('index_name', ''))
    title  = dat.get('title') or dat.get('source') or dat.get('material') or '未知'
    author = (dat.get('member_name') or dat.get('creator') or dat.get('author') or '')
    urls   = hdr.get('ext_urls', [])
    url    = urls[0] if urls else ''

    if idx == 18 and dat.get('nh_id'):
        url = url or f'https://nhentai.net/g/{dat["nh_id"]}/'

    return {'source': source, 'title': title, 'author': author, 'url': url, 'similarity': sim, 'from': 'SauceNAO'}


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
        results = []
        for r in resp.json().get('results', []):
            parsed = _parse_saucenao(r)
            if parsed:
                results.append(parsed)
            if len(results) >= 5:
                break
        return results
    except Exception as e:
        print(f'[SAUCE] 搜尋失敗: {e}')
        return []


_SOUTUBOT_BASE = 'https://soutubot.moe'

async def _soutubot_search(image_data: bytes, mime_type: str) -> list[dict]:
    import os, tempfile
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
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                locale='zh-TW',
                timezone_id='Asia/Taipei',
                viewport={'width': 1280, 'height': 800},
                extra_http_headers={
                    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                    'sec-ch-ua': '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
                    'sec-ch-ua-mobile': '?0',
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

        _SOURCE_URL: dict[str, str] = {
            'nhentai': 'https://nhentai.net',
            'pixiv':   'https://www.pixiv.net',
            'e-hentai':'https://e-hentai.org',
        }
        results = []
        for item in captured[:3]:
            source = item.get('source', '')
            title  = item.get('title') or '未知'
            subj   = item.get('subjectPath', '')
            base   = _SOURCE_URL.get(source, f'https://{source}' if source else '')
            url    = (base + subj) if subj else ''
            results.append({'source': source, 'title': title, 'author': '', 'url': url, 'similarity': 99.0, 'from': 'soutubot'})
        return results

    except Exception as e:
        print(f'[SOUTU] 搜尋失敗: {e}')
        return []
    finally:
        os.unlink(tmp.name)


def _pick_best(results: list[dict]) -> dict | None:
    """優先選 pixiv/twitter/x，其次 nhentai，最後任何有 URL 的結果。"""
    with_url = [r for r in results if r['url']]
    if not with_url:
        return None

    priority = [r for r in with_url if r['source'].lower() in _PRIORITY_SOURCES]
    if priority:
        return max(priority, key=lambda r: r['similarity'])

    nh = [r for r in with_url if r['source'].lower() in _NH_SOURCES]
    if nh:
        return max(nh, key=lambda r: r['similarity'])

    return max(with_url, key=lambda r: r['similarity'])


def _format_result(i: int, r: dict) -> str:
    source = r['source'] or '未知來源'
    title  = r['title']
    author = r['author']
    sim    = r['similarity']
    url    = r['url']
    header = f'{source} | {title} | {author} |' if author else f'{source} | {title} |'
    return f'`{i}.` {header} {sim:.1f}%\n**{url}**'


async def reverse_image_search(image_data: bytes, mime_type: str) -> str:
    """搜尋並回傳所有相似度 ≥ 80% 的結果清單。"""
    soutu = await _soutubot_search(image_data, mime_type)
    await asyncio.sleep(0.5)
    sauce = await _saucenao_search(image_data, mime_type)

    # 合併去重（同 URL 只保留一筆）
    seen: set[str] = set()
    combined: list[dict] = []
    for r in soutu + sauce:
        if not r['url']:
            continue
        if r['url'] in seen:
            continue
        seen.add(r['url'])
        combined.append(r)

    # 輸出所有原始結果到 log
    print(f'[RSEARCH] 合併結果共 {len(combined)} 筆：')
    for r in combined:
        print(f'  [{r["similarity"]:5.1f}%] ({r.get("from", "?")}) {r["source"]} | {r["title"]} | {r["author"]} | {r["url"]}')

    # 篩選相似度 ≥ 80%，並依相似度排序
    hits = sorted(
        [r for r in combined if r['similarity'] >= 80],
        key=lambda r: r['similarity'],
        reverse=True,
    )

    if not hits:
        return '找不到相似度 80% 以上的圖片來源。'

    lines = [f'找到 {len(hits)} 筆相似度 ≥ 80% 的結果：']
    for i, r in enumerate(hits, 1):
        lines.append(_format_result(i, r))
    return '\n\n'.join(lines)
