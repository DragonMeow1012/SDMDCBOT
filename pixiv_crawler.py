"""
Pixiv 爬蟲模組
- asyncio + aiohttp 並行下載圖片（DOWNLOAD_WORKERS 並發）
- producer/consumer：API 抓取（asyncio.to_thread）與下載同步進行
- run_full_crawl / crawl_user_by_id 對外仍為同步介面（在背景執行緒呼叫 asyncio.run）
"""
import asyncio
import io
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import aiohttp
from PIL import Image
from pixivpy3 import AppPixivAPI

import pixiv_config as config
import pixiv_database as db
import pixiv_feature as fe

logger = logging.getLogger(__name__)

# 進度 hook：每 _HOOK_INTERVAL 筆（下載成功+失敗）呼叫一次
_progress_hook: "Callable[[dict], None] | None" = None
_HOOK_INTERVAL: int = 5


def set_progress_hook(hook: "Callable[[dict], None] | None", interval: int = 5) -> None:
    """由外部（commands/pixiv.py）注入進度回呼，interval = 每幾筆觸發一次"""
    global _progress_hook, _HOOK_INTERVAL
    _progress_hook = hook
    _HOOK_INTERVAL = interval


_PIXIV_HEADERS = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
}


# ──────────────────────────────────────────────
# API 初始化
# ──────────────────────────────────────────────

def _setup_api() -> AppPixivAPI:
    api = AppPixivAPI()
    if config.PROXY:
        api.set_additional_headers({"Proxy": config.PROXY})
        logger.info(f"使用代理: {config.PROXY}")
    api.auth(refresh_token=config.PIXIV_REFRESH_TOKEN)
    logger.info("Pixiv 驗證成功")
    return api


def _get_dl_headers(api: AppPixivAPI) -> dict:
    """取得用於 aiohttp 圖片下載的 headers"""
    headers = dict(api.requests.headers)
    headers.update(_PIXIV_HEADERS)
    return headers


# ──────────────────────────────────────────────
# 元數據解析
# ──────────────────────────────────────────────

def _parse_illust(illust: dict) -> dict:
    tags = [t["name"] for t in illust.get("tags", [])]
    meta_pages = illust.get("meta_pages", [])
    if meta_pages:
        image_url = (meta_pages[0]["image_urls"].get("original")
                     or meta_pages[0]["image_urls"].get("large"))
    else:
        urls = illust.get("meta_single_page", {})
        image_url = (urls.get("original_image_url")
                     or illust.get("image_urls", {}).get("large"))
    return {
        "illust_id":  illust["id"],
        "title":      illust["title"],
        "user_id":    illust["user"]["id"],
        "user_name":  illust["user"]["name"],
        "tags":       json.dumps(tags, ensure_ascii=False),
        "bookmarks":  illust["total_bookmarks"],
        "views":      illust["total_view"],
        "width":      illust["width"],
        "height":     illust["height"],
        "page_count": illust["page_count"],
        "image_url":  image_url,
        "local_path": None,
        "created_at": illust["create_date"],
    }


# ──────────────────────────────────────────────
# 同步 API 抓取（交由 asyncio.to_thread 執行）
# ──────────────────────────────────────────────

def _fetch_ranking(api: AppPixivAPI, mode: str, limit: int) -> list[dict]:
    artworks, offset = [], 0
    while len(artworks) < limit:
        result = api.illust_ranking(mode=mode, offset=offset)
        if not result or "illusts" not in result:
            break
        for illust in result["illusts"]:
            if illust["type"] in ("illust", "manga"):
                artworks.append(_parse_illust(illust))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks[:limit]


def _fetch_tag(api: AppPixivAPI, tag: str, limit: int) -> list[dict]:
    artworks, offset = [], 0
    while len(artworks) < limit:
        result = api.search_illust(
            word=tag,
            search_target="partial_match_for_tags",
            sort="popular_desc",
            offset=offset,
        )
        if not result or "illusts" not in result:
            break
        for illust in result["illusts"]:
            if illust["type"] in ("illust", "manga"):
                artworks.append(_parse_illust(illust))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks[:limit]


def _fetch_recommended(api: AppPixivAPI, limit: int) -> list[dict]:
    artworks, offset = [], 0
    while len(artworks) < limit:
        result = api.illust_recommended(offset=offset)
        if not result or "illusts" not in result:
            break
        for illust in result["illusts"]:
            if illust["type"] in ("illust", "manga"):
                artworks.append(_parse_illust(illust))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks[:limit]


def _already_fetched(illust_id: int) -> bool:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM features WHERE illust_id = ?", (illust_id,)
        ).fetchone()
    return row is not None


# ──────────────────────────────────────────────
# 非同步圖片下載 + 特徵提取
# ──────────────────────────────────────────────

async def _download_artwork_async(
    session: aiohttp.ClientSession,
    artwork: dict,
    sem: asyncio.Semaphore,
    stop_event: threading.Event,
    counters: dict,
) -> None:
    """下載單張圖片、提取 pHash 並存入 DB/索引（並發受 sem 限制）"""
    if stop_event.is_set():
        return
    illust_id = artwork["illust_id"]
    if _already_fetched(illust_id):
        counters["skipped"] += 1
        return

    url = artwork.get("image_url")
    if not url:
        return

    async with sem:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.read()

            # pHash 提取（CPU bound）→ 執行緒池
            def _process() -> None:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img.thumbnail(config.MAX_IMAGE_SIZE, Image.LANCZOS)
                phash_vec = fe.extract_phash(img)
                db.upsert_artwork(artwork)
                db.upsert_features(illust_id, phash_vec)
                fe.add_to_index(illust_id, phash_vec)

            await asyncio.to_thread(_process)
            counters["downloaded"] += 1
            logger.info(f"已處理 {illust_id} | {artwork['title'][:40]}")

        except Exception as e:
            counters["failed"] += 1
            logger.warning(f"處理失敗 {illust_id}: {e}")

        finally:
            n = counters["downloaded"] + counters["failed"]
            if _progress_hook and n > 0 and n % _HOOK_INTERVAL == 0:
                _progress_hook(dict(counters))


async def _process_batch_async(
    session: aiohttp.ClientSession,
    artworks: list[dict],
    counters: dict,
    stop_event: threading.Event,
    sem: asyncio.Semaphore,
    status_callback: Callable | None = None,
    total_artworks: int | None = None,
) -> None:
    """並行處理一批作品，全部完成後才回傳"""
    tasks = [
        _download_artwork_async(session, aw, sem, stop_event, counters)
        for aw in artworks
    ]
    if tasks:
        await asyncio.gather(*tasks)
    if status_callback:
        status_callback(counters, total_artworks or len(artworks), False)


# ──────────────────────────────────────────────
# 全站爬取（async 核心）
# ──────────────────────────────────────────────

async def _run_full_crawl_async(
    stop_event: threading.Event,
    api: AppPixivAPI,
    dl_headers: dict,
) -> None:
    limit = config.FULL_CRAWL_PER_SOURCE
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "round": 0}
    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)

    # 來源清單：(fetch_fn, *args)
    def _sources():
        for mode in config.ALL_RANKING_MODES:
            yield _fetch_ranking, api, mode, limit
        for tag in config.ALL_TAGS:
            yield _fetch_tag, api, tag, limit
        yield _fetch_recommended, api, limit

    while not stop_event.is_set():
        counters["round"] += 1
        current_round = counters["round"]
        logger.info(f"===== 第 {current_round} 輪開始 =====")

        # maxsize=3：pipeline 緩衝，避免 producer 跑太快佔記憶體
        batch_queue: asyncio.Queue[list[dict] | None] = asyncio.Queue(maxsize=3)

        connector = aiohttp.TCPConnector(limit=config.DOWNLOAD_WORKERS * 2)
        dl_session = aiohttp.ClientSession(headers=dl_headers, connector=connector)

        async def producer() -> None:
            for fetch_fn, *args in _sources():
                if stop_event.is_set():
                    break
                try:
                    artworks = await asyncio.to_thread(fetch_fn, *args)
                    logger.info(f"[抓取] {fetch_fn.__name__}({args[1] if len(args) > 1 else ''}) → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[抓取] 來源失敗: {e}")
                    artworks = []
                await batch_queue.put(artworks)
            await batch_queue.put(None)  # sentinel

        async def consumer() -> None:
            while True:
                batch = await batch_queue.get()
                if batch is None:
                    break
                if batch:
                    await _process_batch_async(
                        dl_session, batch, counters, stop_event, download_sem
                    )

        try:
            await asyncio.gather(producer(), consumer())
        finally:
            await dl_session.close()

        s = db.stats()
        logger.info(
            f"第 {current_round} 輪結束 | 新增 {counters['downloaded']} | "
            f"跳過 {counters['skipped']} | 失敗 {counters['failed']} | "
            f"DB {s['total']} 件 / 已索引 {s['indexed']} 件"
        )

        if not stop_event.is_set():
            logger.info("等待 60 秒後開始下一輪...")
            for _ in range(60):
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)

    logger.info("爬取結束，存檔 FAISS 索引...")
    fe.flush_index()
    s = db.stats()
    logger.info(f"爬取已停止，DB 共 {s['total']} 件，已索引 {s['indexed']} 件")


# ──────────────────────────────────────────────
# 主要入口：背景持續爬取
# ──────────────────────────────────────────────

def run_full_crawl(stop_event: threading.Event) -> None:
    db.init_db()
    Path(config.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    fe.init_live_index()

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        return

    dl_headers = _get_dl_headers(api)
    asyncio.run(_run_full_crawl_async(stop_event, api, dl_headers))


# ──────────────────────────────────────────────
# 作者爬取入口
# ──────────────────────────────────────────────

def get_user_id_from_artwork(artwork_id: int) -> tuple[int, str]:
    api = _setup_api()
    result = api.illust_detail(artwork_id)
    if not result or "illust" not in result:
        raise ValueError(f"找不到作品 {artwork_id}")
    illust = result["illust"]
    return illust["user"]["id"], illust["user"]["name"]


def get_user_name(user_id: int) -> str:
    api = _setup_api()
    result = api.user_detail(user_id)
    if not result or "user" not in result:
        return str(user_id)
    return result["user"]["name"]


async def _crawl_user_async(
    user_id: int,
    stop_event: threading.Event,
    api: AppPixivAPI,
    dl_headers: dict,
    status_callback: Callable | None = None,
) -> tuple[str, int, int]:
    # 取得作者資訊
    try:
        user_detail = await asyncio.to_thread(api.user_detail, user_id)
        user_name = user_detail["user"]["name"] if user_detail else str(user_id)
        logger.info(f"確認作者: https://www.pixiv.net/users/{user_id} ({user_name})")
    except Exception as e:
        logger.warning(f"無法確認作者 {user_id}: {e}")
        user_name = str(user_id)

    logger.info(f"開始爬取作者: {user_name} (ID:{user_id})")

    # 取得所有作品元數據
    artworks: list[dict] = []
    offset = 0
    while not stop_event.is_set():
        result = await asyncio.to_thread(
            api.user_illusts, user_id, type="illust", offset=offset
        )
        if not result or "illusts" not in result:
            break
        for illust in result["illusts"]:
            if illust["type"] in ("illust", "manga"):
                artworks.append(_parse_illust(illust))
        if not result.get("next_url"):
            break
        offset += 30
        await asyncio.sleep(config.FULL_CRAWL_API_DELAY)

    total = len(artworks)
    logger.info(f"作者 [{user_name}] 共取得 {total} 件作品，開始下載")

    counters = {"downloaded": 0, "skipped": 0, "failed": 0}
    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)

    connector = aiohttp.TCPConnector(limit=config.DOWNLOAD_WORKERS * 2)
    async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:
        await _process_batch_async(
            session, artworks, counters, stop_event, download_sem,
            status_callback=status_callback, total_artworks=total,
        )

    if status_callback:
        status_callback(counters, total, True)

    logger.info(
        f"作者 [{user_name}] 爬取完成 | "
        f"新增: {counters['downloaded']} | 跳過: {counters['skipped']} | 失敗: {counters['failed']}"
    )
    fe.flush_index()
    return user_name, counters["downloaded"], total


def crawl_user_by_id(
    user_id: int,
    stop_event: threading.Event,
    status_callback: Callable | None = None,
) -> tuple[str, int, int]:
    db.init_db()
    Path(config.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    fe.init_live_index()

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        raise

    dl_headers = _get_dl_headers(api)
    return asyncio.run(
        _crawl_user_async(user_id, stop_event, api, dl_headers, status_callback)
    )
