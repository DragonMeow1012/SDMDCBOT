"""
測試 nhentai v2 API 爬 tag=fullcolor 的作品。

nhentai 已停止支援舊 /api/galleries/search 與 /tag/{slug}/ HTML 頁，
現用 OpenAPI 3.1 的 /api/v2/* 端點（文件：https://nhentai.net/api/v2/docs）。

本測試依序驗證：
  [A] GET /api/v2/tags/tag/fullcolor        → 取得 tag_id
  [B] GET /api/v2/galleries/tagged?tag_id=  → 用 tag_id 列作品
  [C] GET /api/v2/search?query=tag:fullcolor → 字串查詢

執行：
    python test_nhentai_ajax.py
"""
import asyncio
import json
import sys

import aiohttp

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

TAG = 'fullcolor'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
}


async def _get_json(session: aiohttp.ClientSession, url: str, label: str, **params):
    print(f'\n{label} GET {url}' + (f'  params={params}' if params else ''))
    try:
        async with session.get(url, params=params or None,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            ct = r.headers.get('content-type', '')
            raw = await r.text()
            print(f'    status: {r.status}  content-type: {ct}  body_len: {len(raw)}')
            if 'json' not in ct:
                print(f'    head: {raw[:300]}')
                return None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f'    JSON parse failed: {e}  head: {raw[:200]}')
                return None
            if r.status >= 400:
                print(f'    body: {data}')
                return None
            return data
    except Exception as e:
        print(f'    FAILED: {type(e).__name__}: {e}')
        return None


def _describe_gallery(g: dict) -> str:
    title = g.get('english_title') or g.get('japanese_title') or g.get('title_pretty') or '?'
    gid   = g.get('id') if g.get('id') is not None else '?'
    pages = g.get('num_pages') if g.get('num_pages') is not None else '?'
    tids  = g.get('tag_ids') or []
    return f'id={gid!s:<8} pages={pages!s:<4} tags={len(tids):<3} title={title[:70]}'


def _print_result_list(data: dict) -> None:
    # tagged 與 search 的 schema 可能略有不同；兩者都支援 results 或 result 鍵
    results = data.get('results') or data.get('result') or data.get('galleries') or []
    meta_keys = {k: data[k] for k in ('total', 'num_pages', 'per_page', 'page') if k in data}
    print(f'    meta: {meta_keys}   回傳筆數: {len(results)}')
    for i, g in enumerate(results[:5], 1):
        print(f'    [{i}] {_describe_gallery(g)}')


async def main() -> None:
    print(f'測試目標：tag={TAG}')
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # [A] 用 slug 取 tag_id（試多種常見 slug 寫法）
        tag_id = None
        for slug in (TAG, TAG.replace('-', ''), TAG.replace('_', '-'), 'full-color'):
            tag_info = await _get_json(
                session,
                f'https://nhentai.net/api/v2/tags/tag/{slug}',
                f'[A] tag lookup slug={slug!r}',
            )
            if isinstance(tag_info, dict) and tag_info.get('id'):
                tag_id = tag_info['id']
                print(f'    → tag_id={tag_id}  count={tag_info.get("count")}  '
                      f'name={tag_info.get("name")}')
                break

        # [B] tag_id 查作品
        if tag_id is not None:
            data = await _get_json(
                session,
                'https://nhentai.net/api/v2/galleries/tagged',
                '[B] galleries/tagged',
                tag_id=tag_id, page=1, per_page=10,
            )
            if isinstance(data, dict):
                _print_result_list(data)

        # [C] 字串查詢
        data = await _get_json(
            session,
            'https://nhentai.net/api/v2/search',
            '[C] search',
            query=f'tag:{TAG}', page=1,
        )
        if isinstance(data, dict):
            _print_result_list(data)


if __name__ == '__main__':
    asyncio.run(main())
