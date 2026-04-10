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
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import aiohttp
from PIL import Image
from pixivpy3 import AppPixivAPI

import pixiv_config as config
import pixiv_database as db
import pixiv_feature as fe

# ──────────────────────────────────────────────
# Windows ProactorEventLoop WinError 10054 修補
# 伺服器強制重置連線時 socket.shutdown() 會丟出 ConnectionResetError，
# 這是 Windows asyncio 已知 bug，直接在 transport 層壓制即可。
# ──────────────────────────────────────────────
if sys.platform == "win32":
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport

        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _patched_call_connection_lost(self, exc):
            try:
                _orig_call_connection_lost(self, exc)
            except ConnectionResetError:
                pass

        _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost
    except Exception:
        pass

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Token-bucket 下載限速（全域共享）
# ──────────────────────────────────────────────

class _TokenBucket:
    """非同步 token-bucket 限速器，用於限制總下載頻寬。"""

    def __init__(self, rate_mbps: float) -> None:
        self._rate_bytes = rate_mbps * 1024 * 1024 / 8  # bytes/sec
        self._tokens: float = self._rate_bytes
        self._last_refill: float = time.monotonic()
        self._lock: "asyncio.Lock | None" = None  # 懶初始化，確保在 event loop 內建立

    def _get_lock(self) -> asyncio.Lock:
        """第一次使用時在當前 event loop 內建立 Lock，避免 Python 3.10+ 跨 loop 錯誤。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def update_rate(self, rate_mbps: float) -> None:
        self._rate_bytes = rate_mbps * 1024 * 1024 / 8

    async def consume(self, n_bytes: int) -> None:
        """消耗 n_bytes 個 token；不足時 sleep 等待補充。"""
        async with self._get_lock():
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._rate_bytes,
                self._tokens + elapsed * self._rate_bytes,
            )
            self._last_refill = now

            if self._tokens >= n_bytes:
                self._tokens -= n_bytes
                return

            deficit = n_bytes - self._tokens
            wait = deficit / self._rate_bytes
            self._tokens = 0

        await asyncio.sleep(wait)


_rate_limiter: "_TokenBucket | None" = None
_rate_limiter_lock = threading.Lock()


def _get_rate_limiter() -> "_TokenBucket | None":
    return _rate_limiter


def _init_rate_limiter() -> None:
    """依 config 建立（或關閉）全域限速器；可在執行中呼叫以動態更新。"""
    global _rate_limiter
    limit = getattr(config, "DOWNLOAD_RATE_LIMIT_Mbps", 0.0)
    with _rate_limiter_lock:
        if limit and limit > 0:
            if _rate_limiter is None:
                _rate_limiter = _TokenBucket(limit)
                logger.info(f"下載限速已啟用: {limit} Mbps")
            else:
                _rate_limiter.update_rate(limit)
                logger.info(f"下載限速已更新: {limit} Mbps")
        else:
            _rate_limiter = None
            logger.info("下載限速已關閉")


# 進度 hook：每 _HOOK_INTERVAL 筆（新增+失敗+跳過）呼叫一次
_progress_hook: "Callable[[dict], None] | None" = None
_HOOK_INTERVAL: int = 5
_fully_indexed_ids_cache: set[int] | None = None
_cache_lock = threading.Lock()
_priority_user_queue: "deque[int]" = deque()
_priority_user_ids: set[int] = set()
_priority_lock = threading.Lock()
_priority_user_done_hook: "Callable[[dict], None] | None" = None


def set_progress_hook(hook: "Callable[[dict], None] | None", interval: int = 5) -> None:
    """由外部（commands/pixiv.py）注入進度回呼，interval = 每幾筆觸發一次"""
    global _progress_hook, _HOOK_INTERVAL
    _progress_hook = hook
    _HOOK_INTERVAL = interval


def set_priority_user_done_hook(hook: "Callable[[dict], None] | None") -> None:
    """由外部（commands/pixiv.py）注入優先作者完成回呼。"""
    global _priority_user_done_hook
    _priority_user_done_hook = hook


def _reset_fully_indexed_cache() -> None:
    global _fully_indexed_ids_cache
    with _cache_lock:
        _fully_indexed_ids_cache = None


def _load_fully_indexed_cache() -> set[int]:
    global _fully_indexed_ids_cache
    with _cache_lock:
        if _fully_indexed_ids_cache is None:
            _fully_indexed_ids_cache = db.get_all_fully_indexed_artwork_ids()
            logger.info(f"已載入完整索引快取 {len(_fully_indexed_ids_cache)} 筆")
        return set(_fully_indexed_ids_cache)


def _is_cached_fully_indexed(illust_id: int) -> bool:
    with _cache_lock:
        if _fully_indexed_ids_cache is not None:
            return illust_id in _fully_indexed_ids_cache
    return illust_id in db.get_all_fully_indexed_artwork_ids()


def _mark_artwork_fully_indexed(illust_id: int) -> None:
    with _cache_lock:
        if _fully_indexed_ids_cache is not None:
            _fully_indexed_ids_cache.add(illust_id)


def enqueue_priority_user(user_id: int) -> bool:
    """將作者加入最高優先爬取佇列；已在佇列中則回傳 False。"""
    with _priority_lock:
        if user_id in _priority_user_ids:
            return False
        _priority_user_queue.append(user_id)
        _priority_user_ids.add(user_id)
    logger.info(f"已加入優先作者佇列: {user_id}")
    return True


def get_priority_queue_size() -> int:
    with _priority_lock:
        return len(_priority_user_queue)


def _clear_priority_queue() -> None:
    with _priority_lock:
        _priority_user_queue.clear()
        _priority_user_ids.clear()


def clear_priority_queue() -> None:
    _clear_priority_queue()


def _pop_priority_user() -> int | None:
    with _priority_lock:
        if not _priority_user_queue:
            return None
        user_id = _priority_user_queue.popleft()
        _priority_user_ids.discard(user_id)
        return user_id


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
    gallery_urls = _extract_gallery_urls(illust)
    image_url = gallery_urls[0] if gallery_urls else ""
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
        "gallery_urls": gallery_urls,
        "local_path": None,
        "created_at": illust["create_date"],
    }


def _extract_gallery_urls(illust: dict) -> list[str]:
    urls: list[str] = []
    meta_pages = illust.get("meta_pages", []) or []
    for page in meta_pages:
        image_urls = page.get("image_urls", {}) or {}
        url = image_urls.get("original") or image_urls.get("large")
        if url:
            urls.append(url)

    if not urls:
        single = (illust.get("meta_single_page", {}) or {}).get("original_image_url")
        fallback = (illust.get("image_urls", {}) or {}).get("large")
        if single:
            urls.append(single)
        elif fallback:
            urls.append(fallback)
    return urls


# ──────────────────────────────────────────────
# 同步 API 抓取（交由 asyncio.to_thread 執行）
# ──────────────────────────────────────────────

def _iter_illusts(result: dict) -> list[dict]:
    """從 API 結果提取有效插畫/漫畫，統一處理類型過濾。"""
    return [
        _parse_illust(i)
        for i in result.get("illusts", [])
        if i.get("type") in ("illust", "manga")
    ]


def _fetch_ranking(api: AppPixivAPI, mode: str) -> list[dict]:
    artworks, offset = [], 0
    while True:
        result = api.illust_ranking(mode=mode, offset=offset)
        if not result or "illusts" not in result or not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_tag(api: AppPixivAPI, tag: str, sort: str = "popular_desc") -> list[dict]:
    artworks, offset = [], 0
    while True:
        result = api.search_illust(
            word=tag,
            search_target="partial_match_for_tags",
            sort=sort,
            offset=offset,
        )
        if not result or "illusts" not in result or not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_related(api: AppPixivAPI, illust_id: int) -> list[dict]:
    """抓取相關作品，透過 next_qs 翻頁直到沒有下一頁。"""
    artworks: list[dict] = []
    result = api.illust_related(illust_id=illust_id)
    while result and "illusts" in result:
        if not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        next_url = result.get("next_url")
        if not next_url:
            break
        time.sleep(config.FULL_CRAWL_API_DELAY)
        try:
            qs = api.parse_qs(next_url)
            result = api.illust_related(**qs)
        except Exception:
            break
    return artworks


def _fetch_user_artworks_sync(api: AppPixivAPI, user_id: int) -> list[dict]:
    artworks: list[dict] = []
    offset = 0
    while True:
        result = api.user_illusts(user_id, type="illust", offset=offset)
        if not result or "illusts" not in result or not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_recommended(api: AppPixivAPI) -> list[dict]:
    artworks, offset = [], 0
    while True:
        result = api.illust_recommended(offset=offset)
        if not result or "illusts" not in result or not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_new_illusts(api: AppPixivAPI, content_type: str = "illust") -> list[dict]:
    """抓取全站最新上傳作品（每輪前 NEW_ILLUSTS_MAX_PAGES 頁），作為擴散種子。"""
    max_pages: int = getattr(config, "NEW_ILLUSTS_MAX_PAGES", 15)
    artworks: list[dict] = []
    try:
        result = api.illust_new(content_type=content_type)
    except Exception as e:
        logger.warning(f"[新作品] illust_new({content_type}) 不支援或失敗: {e}")
        return artworks
    pages = 0
    while result and "illusts" in result and pages < max_pages:
        if not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        next_url = result.get("next_url")
        if not next_url:
            break
        time.sleep(config.FULL_CRAWL_API_DELAY)
        try:
            qs = api.parse_qs(next_url)
            result = api.illust_new(**qs)
        except Exception:
            break
        pages += 1
    return artworks


async def _ensure_gallery_urls(
    api: AppPixivAPI,
    artwork: dict,
    api_sem: "asyncio.Semaphore | None" = None,
) -> list[str]:
    """確保多頁漫畫有完整 URL；透過 api_sem 限制並發 illust_detail 呼叫數量。"""
    page_count = int(artwork.get("page_count") or 1)
    cached_urls = [u for u in (artwork.get("gallery_urls") or []) if u]
    if page_count <= 1:
        return cached_urls or ([artwork.get("image_url")] if artwork.get("image_url") else [])
    if len(cached_urls) >= page_count:
        return cached_urls

    for attempt in range(1, 4):  # 最多重試 3 次
        try:
            if api_sem:
                async with api_sem:
                    detail = await asyncio.to_thread(api.illust_detail, artwork["illust_id"])
                    await asyncio.sleep(0.3)  # API 禮貌延遲
            else:
                detail = await asyncio.to_thread(api.illust_detail, artwork["illust_id"])
            illust = detail.get("illust") if detail else None
            if illust:
                urls = _extract_gallery_urls(illust)
                if urls:
                    artwork["gallery_urls"] = urls
                    artwork["image_url"] = urls[0]
                    return urls
        except Exception as e:
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
            else:
                logger.warning(f"補抓圖集 URL 失敗 {artwork['illust_id']}: {e}")
    return cached_urls or ([artwork.get("image_url")] if artwork.get("image_url") else [])


async def _fetch_user_artworks(
    api: AppPixivAPI,
    user_id: int,
    stop_event: threading.Event,
) -> tuple[str, list[dict]]:
    try:
        user_detail = await asyncio.to_thread(api.user_detail, user_id)
        user_name = user_detail["user"]["name"] if user_detail else str(user_id)
        logger.info(f"確認作者: https://www.pixiv.net/users/{user_id} ({user_name})")
    except Exception as e:
        logger.warning(f"無法確認作者 {user_id}: {e}")
        user_name = str(user_id)

    artworks: list[dict] = []
    offset = 0
    while not stop_event.is_set():
        result = await asyncio.to_thread(
            api.user_illusts, user_id, type="illust", offset=offset
        )
        if not result or "illusts" not in result or not result["illusts"]:
            break
        artworks.extend(_iter_illusts(result))
        if not result.get("next_url"):
            break
        offset += 30
        await asyncio.sleep(config.FULL_CRAWL_API_DELAY)

    return user_name, artworks


# ──────────────────────────────────────────────
# 非同步圖片下載 + 特徵提取
# ──────────────────────────────────────────────

async def _download_artwork_async(
    api: AppPixivAPI,
    session: aiohttp.ClientSession,
    artwork: dict,
    sem: asyncio.Semaphore,
    stop_event: threading.Event,
    counters: dict,
    on_success: "Callable[[dict], None] | None" = None,
    api_sem: "asyncio.Semaphore | None" = None,
    on_skip: "Callable[[dict], None] | None" = None,
) -> None:
    """Download one artwork, persist features, and emit progress updates."""
    if stop_event.is_set():
        return

    illust_id = artwork["illust_id"]

    try:
        if _is_cached_fully_indexed(illust_id):
            counters["skipped"] += 1
            # 跳過時只做輕量的作者發現（不加入 related_diff_q 以免爆炸性增長）
            if on_skip:
                on_skip(artwork)
            return

        urls = await _ensure_gallery_urls(api, artwork, api_sem)
        if not urls:
            counters["failed"] += 1
            return

        async with sem:
            page_features: list[tuple[int, str, object]] = []
            urls_to_fetch = urls[:config.MAX_GALLERY_PAGES]

            def _compute_phash(raw: bytes) -> object:
                """CPU-bound pHash 提取，交由執行緒池處理以釋放 GIL。"""
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail(config.MAX_IMAGE_SIZE, Image.LANCZOS)
                return fe.extract_phash(img)

            for page_index, page_url in enumerate(urls_to_fetch):
                page_success = False
                last_error: Exception | None = None
                for attempt in range(1, config.DOWNLOAD_RETRIES + 1):
                    try:
                        async with session.get(
                            page_url,
                            timeout=aiohttp.ClientTimeout(total=90),
                        ) as resp:
                            resp.raise_for_status()
                            chunks: list[bytes] = []
                            rl = _get_rate_limiter()
                            chunk_size = getattr(config, "DOWNLOAD_CHUNK_SIZE", 65536)
                            async for chunk in resp.content.iter_chunked(chunk_size):
                                if rl is not None:
                                    await rl.consume(len(chunk))
                                chunks.append(chunk)
                            data = b"".join(chunks)

                        # 在執行緒池計算 pHash，讓 event loop 可同時處理其他 I/O
                        phash_vec = await asyncio.to_thread(_compute_phash, data)
                        page_features.append((page_index, page_url, phash_vec))
                        page_success = True
                        break
                    except Exception as e:
                        last_error = e
                        if attempt < config.DOWNLOAD_RETRIES:
                            logger.info(
                                f"下載重試 {illust_id} p{page_index} "
                                f"({attempt}/{config.DOWNLOAD_RETRIES})"
                            )
                            await asyncio.sleep(min(1.5 * attempt, 3))
                if not page_success:
                    logger.warning(
                        f"下載圖片失敗 {illust_id} p{page_index} "
                        f"(已重試 {config.DOWNLOAD_RETRIES} 次): {last_error}"
                    )

            if not page_features:
                counters["failed"] += 1
                return

            def _persist() -> None:
                db.upsert_artwork(artwork)
                first_phash = None
                for page_index, page_url, phash_vec in page_features:
                    db.upsert_gallery_page(
                        illust_id=illust_id,
                        page_index=page_index,
                        image_url=page_url,
                        phash_vec=phash_vec,
                    )
                    fe.add_to_index(illust_id, page_index, phash_vec)
                    if first_phash is None:
                        first_phash = phash_vec
                if first_phash is not None:
                    db.upsert_features(illust_id, first_phash)

            await asyncio.to_thread(_persist)
            _mark_artwork_fully_indexed(illust_id)
            counters["downloaded"] += 1
            logger.info(f"已處理 {illust_id} | {artwork['title'][:40]}")
            if on_success:
                on_success(artwork)

    except Exception as e:
        counters["failed"] += 1
        logger.warning(f"處理作品失敗 {illust_id}: {e}")

    finally:
        processed = counters["downloaded"] + counters["failed"] + counters["skipped"]
        if _progress_hook and processed > 0 and processed % _HOOK_INTERVAL == 0:
            _progress_hook(dict(counters))


async def _process_batch_async(
    api: AppPixivAPI,
    session: aiohttp.ClientSession,
    artworks: list[dict],
    counters: dict,
    stop_event: threading.Event,
    sem: asyncio.Semaphore,
    status_callback: Callable | None = None,
    total_artworks: int | None = None,
    on_success: "Callable[[dict], None] | None" = None,
    api_sem: "asyncio.Semaphore | None" = None,
    on_skip: "Callable[[dict], None] | None" = None,
) -> None:
    """並行處理一批作品，全部完成後才回傳"""
    tasks = [
        _download_artwork_async(
            api,
            session,
            aw,
            sem,
            stop_event,
            counters,
            on_success=on_success,
            api_sem=api_sem,
            on_skip=on_skip,
        )
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
    visited_users: "set[int] | None" = None,
) -> None:
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "round": 0}
    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)
    # illust_detail 專用 semaphore：限制並發 API 呼叫數，避免 rate limit
    api_detail_sem = asyncio.Semaphore(getattr(config, "API_DETAIL_CONCURRENCY", 3))

    # ── 擴散佇列 ──────────────────────────────────────
    # visited_users：已排程爬全作品的 user_id（session 內去重）
    if visited_users is None:
        visited_users = await asyncio.to_thread(db.get_all_user_ids)
        logger.info(f"已從 DB 載入 {len(visited_users)} 位已知作者")
    # related_visited：已取過相關作品的 illust_id
    related_visited: set[int] = set()
    # 擴散佇列（無界，producer 從這裡取）
    user_diff_q: asyncio.Queue[int] = asyncio.Queue()
    related_diff_q: asyncio.Queue[int] = asyncio.Queue()

    def _on_artwork_success(artwork: dict) -> None:
        """新下載的作品：推入作者佇列 + 相關作品佇列（完整擴散）。"""
        uid = artwork.get("user_id")
        if uid and uid not in visited_users:
            visited_users.add(uid)
            user_diff_q.put_nowait(uid)
        iid = artwork.get("illust_id")
        if iid and iid not in related_visited:
            related_visited.add(iid)
            related_diff_q.put_nowait(iid)

    def _on_artwork_skip(artwork: dict) -> None:
        """已索引的作品：只做作者發現，不加入 related_diff_q（避免爆炸性增長）。"""
        uid = artwork.get("user_id")
        if uid and uid not in visited_users:
            visited_users.add(uid)
            user_diff_q.put_nowait(uid)

    # ── 來源清單（種子）──────────────────────────────
    def _seed_sources():
        # 全站最新上傳：捕捉新作者，擴散覆蓋率最高
        yield _fetch_new_illusts, api, "illust"
        yield _fetch_new_illusts, api, "manga"
        for mode in config.ALL_RANKING_MODES:
            yield _fetch_ranking, api, mode
        for tag in config.ALL_TAGS:
            for sort in getattr(config, "CRAWL_TAG_SORTS", ["popular_desc", "date_desc"]):
                yield _fetch_tag, api, tag, sort
        yield _fetch_recommended, api

    while not stop_event.is_set():
        counters["round"] += 1
        current_round = counters["round"]
        logger.info(f"===== 第 {current_round} 輪開始 =====")

        connector = aiohttp.TCPConnector(
            limit=config.DOWNLOAD_WORKERS * 3,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )
        dl_session = aiohttp.ClientSession(headers=dl_headers, connector=connector)

        async def _process(artworks: list[dict]) -> None:
            if artworks:
                await _process_batch_async(
                    api, dl_session, artworks, counters, stop_event, download_sem,
                    on_success=_on_artwork_success,
                    on_skip=_on_artwork_skip,
                    api_sem=api_detail_sem,
                )

        async def _drain_priority() -> None:
            while not stop_event.is_set():
                priority_uid = _pop_priority_user()
                if priority_uid is None:
                    return
                user_name = str(priority_uid)
                artworks: list[dict] = []
                user_counters = {"downloaded": 0, "skipped": 0, "failed": 0}
                status = "completed"
                try:
                    user_name, artworks = await _fetch_user_artworks(api, priority_uid, stop_event)
                    logger.info(f"[priority] {user_name} ({priority_uid}) → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[priority] 失敗 {priority_uid}: {e}")
                    status = "error"
                else:
                    if artworks:
                        await _process_batch_async(
                            api, dl_session, artworks, user_counters,
                            stop_event, download_sem, on_success=_on_artwork_success,
                            on_skip=_on_artwork_skip,
                            api_sem=api_detail_sem,
                        )
                    if stop_event.is_set():
                        status = "stopped"
                if _priority_user_done_hook:
                    _priority_user_done_hook({
                        "user_id": priority_uid, "user_name": user_name,
                        "total": len(artworks),
                        "downloaded": user_counters["downloaded"],
                        "skipped": user_counters["skipped"],
                        "failed": user_counters["failed"],
                        "status": status,
                    })

        async def _drain_diffusion() -> None:
            """直接處理擴散佇列中所有項目；優先插隊由 _priority_watcher 並行處理。"""
            while not stop_event.is_set() and not user_diff_q.empty():
                uid = user_diff_q.get_nowait()
                try:
                    artworks = await asyncio.to_thread(_fetch_user_artworks_sync, api, uid)
                    logger.info(f"[擴散-作者] user={uid} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[擴散-作者] 失敗 user={uid}: {e}")
                    artworks = []
                await _process(artworks)
                await _drain_related()

            await _drain_related()

        async def _drain_related() -> None:
            while not stop_event.is_set() and not related_diff_q.empty():
                iid = related_diff_q.get_nowait()
                try:
                    artworks = await asyncio.to_thread(_fetch_related, api, iid)
                    logger.info(f"[擴散-相關] illust={iid} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[擴散-相關] 失敗 illust={iid}: {e}")
                    artworks = []
                await _process(artworks)

        async def _priority_watcher() -> None:
            """
            並行背景監視優先佇列。
            asyncio 合作式排程：只要主流程到達任何 await 點（包含
            asyncio.to_thread 等待期間），此 task 就能立即被排程執行，
            不再受限於串行檢查點。
            """
            while not stop_event.is_set():
                await _drain_priority()
                await asyncio.sleep(2)  # 每 2 秒主動輪詢

        # 啟動優先監視器（整個 round 含 60s 等待期間都有效）
        _watcher_task = asyncio.create_task(_priority_watcher())
        try:
            for fetch_fn, *args in _seed_sources():
                if stop_event.is_set():
                    break
                try:
                    artworks = await asyncio.to_thread(fetch_fn, *args)
                    label = f"{fetch_fn.__name__}({', '.join(str(a) for a in args[1:])})"
                    logger.info(f"[種子] {label} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[種子] 來源失敗: {e}")
                    artworks = []
                await _process(artworks)
                await _drain_diffusion()
            # 種子全跑完後把擴散佇列清空到底
            await _drain_diffusion()

            try:
                s = db.stats()
                logger.info(
                    f"第 {current_round} 輪結束 | 新增 {counters['downloaded']} | "
                    f"跳過 {counters['skipped']} | 失敗 {counters['failed']} | "
                    f"DB {s['total']} 件 / 已索引 {s['indexed']} 件"
                )
            except Exception as e:
                logger.warning(f"第 {current_round} 輪結束統計失敗: {e}")

            if not stop_event.is_set():
                logger.info("等待 60 秒後開始下一輪...")
                for _ in range(60):
                    if stop_event.is_set():
                        break
                    await asyncio.sleep(1)
        finally:
            _watcher_task.cancel()
            await asyncio.gather(_watcher_task, return_exceptions=True)
            await dl_session.close()

    logger.info("爬取結束，存檔 FAISS 索引...")
    fe.flush_index()
    s = db.stats()
    logger.info(f"爬取已停止，DB 共 {s['total']} 件，已索引 {s['indexed']} 件")


# ──────────────────────────────────────────────
# 作者 ID 順序掃描
# ──────────────────────────────────────────────

import json as _json

def _load_scan_cursor() -> int:
    path = config.USER_ID_SCAN_CURSOR_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(_json.load(f).get("cursor", 0))
    except Exception:
        return 0


def _save_scan_cursor(cursor: int) -> None:
    path = config.USER_ID_SCAN_CURSOR_FILE
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"cursor": cursor}, f)
    except Exception as e:
        logger.warning(f"[掃描] 無法存進度: {e}")


async def _user_id_scan_async(
    stop_event: threading.Event,
    api: AppPixivAPI,
    dl_headers: dict,
    visited_users: set[int],
    on_success: "Callable[[dict], None]",
) -> None:
    """
    從上次記錄的 cursor 開始，依序探測每個 user_id。
    N 個 worker 共享同一個原子計數器，各自獨立請求，
    總速率 ≈ WORKERS / DELAY 次/秒。
    """
    if not getattr(config, "USER_ID_SCAN_ENABLED", True):
        logger.info("[掃描] USER_ID_SCAN_ENABLED=False，跳過作者 ID 掃描")
        return

    n_workers: int = getattr(config, "USER_ID_SCAN_WORKERS", 3)
    delay: float = getattr(config, "USER_ID_SCAN_DELAY", 3.0)

    # 原子計數器（asyncio 單執行緒，不需要 Lock）
    state = {"cursor": _load_scan_cursor()}
    cursor_lock = asyncio.Lock()

    async def _next_id() -> int:
        async with cursor_lock:
            state["cursor"] += 1
            if state["cursor"] % 100 == 0:
                _save_scan_cursor(state["cursor"])
            return state["cursor"]

    # 全域 API 速率限制：限制同時執行的 API 呼叫數，避免並行 worker 爆量
    # 每個呼叫後強制 delay，實際速率 = 1 / delay req/s（單 worker 視角）
    # 多 worker 共享此 semaphore，確保同一時間最多 1 個 API 呼叫
    api_sem = asyncio.Semaphore(1)

    async def _api_call(fn, *args):
        """透過 semaphore 序列化所有掃描 API 呼叫，呼叫後強制等待 delay。"""
        async with api_sem:
            result = await asyncio.to_thread(fn, *args)
            await asyncio.sleep(delay)
            return result

    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)
    scan_api_detail_sem = asyncio.Semaphore(getattr(config, "API_DETAIL_CONCURRENCY", 3))
    counters = {"downloaded": 0, "skipped": 0, "failed": 0}

    async def worker(worker_id: int) -> None:
        # 每個 worker 建立自己的 connector+session，避免共用 connector 被提早關閉
        connector = aiohttp.TCPConnector(
            limit=config.DOWNLOAD_WORKERS * 3,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )
        async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:
            while not stop_event.is_set():
                uid = await _next_id()
                if uid in visited_users:
                    await asyncio.sleep(0)  # yield，讓其他 task 執行
                    continue
                visited_users.add(uid)

                # 探測 user_detail（受 api_sem 序列化）
                try:
                    result = await _api_call(api.user_detail, uid)
                    if not result or "user" not in result:
                        # 不存在的 user_id — delay 已在 _api_call 內執行
                        continue
                    user_name = result["user"]["name"]
                except Exception as e:
                    logger.debug(f"[掃描-W{worker_id}] user={uid} 無效: {e}")
                    continue

                # 抓取該作者全部作品（每頁在 _fetch_user_artworks_sync 內 sleep 1s）
                # 翻頁的 sleep 不受 api_sem 控制，但速率已由 delay 隔開
                try:
                    artworks = await asyncio.to_thread(
                        _fetch_user_artworks_sync, api, uid
                    )
                except Exception as e:
                    logger.warning(f"[掃描-W{worker_id}] user={uid} 作品抓取失敗: {e}")
                    artworks = []

                if artworks:
                    logger.info(
                        f"[掃描-W{worker_id}] user={uid} ({user_name}) → {len(artworks)} 件"
                    )
                    await _process_batch_async(
                        api, session, artworks, counters,
                        stop_event, download_sem, on_success=on_success,
                        api_sem=scan_api_detail_sem,
                    )

        _save_scan_cursor(state["cursor"])
        logger.info(f"[掃描-W{worker_id}] 結束，cursor={state['cursor']}")

    logger.info(
        f"[掃描] 啟動 {n_workers} 個 worker，從 user_id={state['cursor']+1} 開始，"
        f"間隔 {delay}s（總速率 ≤ {n_workers/delay:.1f} req/s）"
    )
    await asyncio.gather(*[worker(i) for i in range(n_workers)])


# ──────────────────────────────────────────────
# 主要入口：背景持續爬取
# ──────────────────────────────────────────────

def run_full_crawl(stop_event: threading.Event) -> None:
    _init_rate_limiter()
    db.init_db()
    Path(config.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    fe.init_live_index()
    _reset_fully_indexed_cache()
    if fe.get_index_size() == 0:
        logger.info("FAISS 索引為空，從 DB 重建（含多頁）...")
        try:
            fe.build_faiss_index()
        except RuntimeError:
            pass
    _load_fully_indexed_cache()

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        return

    dl_headers = _get_dl_headers(api)

    async def _main():
        # visited_users 在兩個分支間共享，避免重複爬同一作者
        visited_users: set[int] = await asyncio.to_thread(db.get_all_user_ids)

        # on_success 需要在這裡定義，讓 scan 分支也能把新作者回饋給 visited_users
        # （主爬蟲的 on_success 在 _run_full_crawl_async 內部定義，各自維護 visited_users）
        # 兩個分支共享同一個 visited_users set，asyncio 單執行緒安全

        def _scan_on_success(aw: dict) -> None:
            uid = aw.get("user_id")
            if uid:
                visited_users.add(uid)

        scan_task = asyncio.create_task(
            _user_id_scan_async(stop_event, api, dl_headers, visited_users, _scan_on_success)
        )
        try:
            await _run_full_crawl_async(stop_event, api, dl_headers, visited_users)
        finally:
            scan_task.cancel()
            try:
                await scan_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_main())


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
    user_name, artworks = await _fetch_user_artworks(api, user_id, stop_event)
    total = len(artworks)
    logger.info(f"作者 [{user_name}] 共取得 {total} 件作品，開始下載")

    counters = {"downloaded": 0, "skipped": 0, "failed": 0}
    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)
    api_detail_sem = asyncio.Semaphore(getattr(config, "API_DETAIL_CONCURRENCY", 3))

    connector = aiohttp.TCPConnector(
        limit=config.DOWNLOAD_WORKERS * 3,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
    )
    async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:
        await _process_batch_async(
            api, session, artworks, counters, stop_event, download_sem,
            status_callback=status_callback, total_artworks=total,
            api_sem=api_detail_sem,
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
    _init_rate_limiter()
    db.init_db()
    Path(config.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    fe.init_live_index()
    _reset_fully_indexed_cache()
    if fe.get_index_size() == 0:
        logger.info("FAISS 索引為空，從 DB 重建（含多頁）...")
        try:
            fe.build_faiss_index()
        except RuntimeError:
            pass
    _load_fully_indexed_cache()

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        raise

    dl_headers = _get_dl_headers(api)
    return asyncio.run(
        _crawl_user_async(user_id, stop_event, api, dl_headers, status_callback)
    )
