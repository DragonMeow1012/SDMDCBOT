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
import random
import sys
import threading
import time
from collections import deque
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import aiohttp
import requests as std_requests
from PIL import Image
from pixivpy3 import AppPixivAPI

import pixiv_config as config
import pixiv_database as db
import pixiv_feature as fe

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional dependency fallback
    curl_requests = None

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
_page_log_lock = threading.Lock()
_PAGE_LOG_MAX_LINES = 5000
_tag_request_lock = threading.Lock()
_tag_progress_lock = threading.Lock()
_tag_progress: dict[str, dict] = {}   # key: "tag::sort", value: {page, done, chosen_query, chosen_sort, ts}

# Token 自動 refresh：access token 約 3600s 過期，每 50 分鐘主動更新
_auth_lock = threading.Lock()
_api_last_auth: "dict[int, float]" = {}
_API_TOKEN_REFRESH_SECS: float = 3000.0  # 50 分鐘


def _maybe_reauth(api: "AppPixivAPI") -> None:
    """若距上次驗證超過 50 分鐘，重新取得 access token。線程安全。"""
    key = id(api)
    if time.monotonic() - _api_last_auth.get(key, 0) < _API_TOKEN_REFRESH_SECS:
        return
    with _auth_lock:
        if time.monotonic() - _api_last_auth.get(key, 0) < _API_TOKEN_REFRESH_SECS:
            return  # 其他 thread 已更新
        try:
            api.auth(refresh_token=api.refresh_token or config.PIXIV_REFRESH_TOKEN)
            _api_last_auth[key] = time.monotonic()
            logger.info("[Token] access token 已更新")
        except Exception as e:
            logger.warning(f"[Token] 更新失敗（下次 API 呼叫前重試）: {e}")


def _append_page_log(filename: str, payload: dict) -> None:
    page_log_dir = Path(config.PAGE_LOG_DIR)
    page_log_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with _page_log_lock:
        log_path = page_log_dir / filename
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        try:
            with log_path.open("r+", encoding="utf-8") as f:
                lines = f.readlines()
                if len(lines) > _PAGE_LOG_MAX_LINES:
                    f.seek(0)
                    f.truncate()
                    f.writelines(lines[-_PAGE_LOG_MAX_LINES:])
        except Exception:
            pass


def _log_page_fetch(
    source: str,
    page: int,
    *,
    offset: int | None = None,
    items: int | None = None,
    next_url: bool | None = None,
    status: str = "ok",
    extra: dict | None = None,
) -> None:
    payload = {
        "source": source,
        "page": page,
        "status": status,
    }
    if offset is not None:
        payload["offset"] = offset
    if items is not None:
        payload["items"] = items
    if next_url is not None:
        payload["has_next_url"] = next_url
    if extra:
        payload.update(extra)
    _append_page_log("page_log.jsonl", payload)


def _log_timeout(event: str, target: str, timeout: float, *, page: int | None = None, extra: dict | None = None) -> None:
    payload = {
        "event": event,
        "target": target,
        "timeout": timeout,
    }
    if page is not None:
        payload["page"] = page
    if extra:
        payload.update(extra)
    _append_page_log("timeout_log.jsonl", payload)


# ──────────────────────────────────────────────
# Tag 爬取進度追蹤
# ──────────────────────────────────────────────

def _tag_key(tag: str, sort: str) -> str:
    return f"{tag}::{sort}"


def _load_tag_progress() -> None:
    """從磁碟載入 tag 進度；爬蟲啟動時呼叫一次。"""
    global _tag_progress
    path = getattr(config, "TAG_CRAWL_PROGRESS_FILE", "")
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _tag_progress_lock:
            _tag_progress = data
        logger.info(f"[進度] 已載入 {len(data)} 條 tag 進度記錄")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"[進度] 載入 tag 進度失敗: {e}")


def _save_tag_progress() -> None:
    """將 tag 進度寫入磁碟；每次抓完一個 tag 後呼叫。"""
    path = getattr(config, "TAG_CRAWL_PROGRESS_FILE", "")
    if not path:
        return
    with _tag_progress_lock:
        data = dict(_tag_progress)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[進度] 儲存 tag 進度失敗: {e}")


def _get_tag_progress(tag: str, sort: str) -> dict:
    key = _tag_key(tag, sort)
    with _tag_progress_lock:
        return dict(_tag_progress.get(key, {}))


def _update_tag_progress(
    tag: str,
    sort: str,
    last_page: int,
    done: bool,
    chosen_query: "str | None" = None,
    chosen_sort: "str | None" = None,
) -> None:
    key = _tag_key(tag, sort)
    with _tag_progress_lock:
        _tag_progress[key] = {
            "page": last_page,
            "done": done,
            "chosen_query": chosen_query,
            "chosen_sort": chosen_sort,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


async def _to_thread_with_timeout(func, *args, timeout: float | None = None, **kwargs):
    timeout = getattr(config, "PIXIV_API_TIMEOUT", 60.0) if timeout is None else timeout
    try:
        return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)
    except asyncio.TimeoutError as e:
        func_name = getattr(func, "__name__", repr(func))
        _log_timeout(
            "api_call",
            func_name,
            timeout,
            extra={
                "args": [str(arg) for arg in args[:4]],
                "kwargs": {k: str(v) for k, v in list(kwargs.items())[:4]},
            },
        )
        raise TimeoutError(f"{func_name} timed out after {timeout:.0f}s") from e


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


def _build_tag_http_session(api: AppPixivAPI):
    timeout = float(getattr(config, "PIXIV_API_TIMEOUT", 60.0))
    session_kind = "requests"
    session = std_requests.Session()

    try:
        existing_headers = dict(getattr(api.requests, "headers", {}))
        if existing_headers:
            session.headers.update(existing_headers)
    except Exception:
        pass
    session.headers.update(_PIXIV_HEADERS)
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.pixiv.net",
        "X-Requested-With": "XMLHttpRequest",
    })

    if config.PROXY:
        session.proxies = {"http": config.PROXY, "https": config.PROXY}

    raw_cookie = (getattr(config, "PIXIV_WEB_COOKIE", "") or "").strip()
    if raw_cookie:
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception as e:
            logger.warning(f"invalid PIXIV_WEB_COOKIE format: {e}")
        else:
            for key, morsel in jar.items():
                try:
                    session.cookies.set(key, morsel.value, domain=".pixiv.net")
                except Exception:
                    session.cookies.set(key, morsel.value)
    return session, session_kind


def _normalize_ajax_artwork(item: dict) -> dict | None:
    if not item or item.get("isAdContainer"):
        return None

    illust_id_raw = item.get("illustId") or item.get("id")
    if not illust_id_raw:
        return None
    try:
        illust_id = int(illust_id_raw)
    except Exception:
        return None

    illust_type = item.get("illustType", item.get("type"))
    if illust_type is None:
        illust_type = "0"
    illust_type = str(illust_type)
    # 0=illust, 1=manga, 2=ugoira (pixiv web/app commonly use these)
    if illust_type not in ("0", "1", "2"):
        illust_type = "0"

    image_url = item.get("url") or ""
    tags = item.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    return {
        "illust_id": illust_id,
        "title": item.get("illustTitle") or item.get("title") or "",
        "user_id": int(item.get("userId") or item.get("user_id") or 0),
        "user_name": item.get("userName") or item.get("user_name") or "",
        "tags": json.dumps(tags, ensure_ascii=False),
        "bookmarks": int(item.get("bookmarkCount") or 0),
        "views": int(item.get("viewCount") or 0),
        "width": int(item.get("width") or 0),
        "height": int(item.get("height") or 0),
        "page_count": int(item.get("pageCount") or 1),
        "image_url": image_url,
        "gallery_urls": [image_url] if image_url else [],
        "local_path": None,
        "created_at": item.get("createDate") or item.get("uploadDate") or "",
    }


def _extract_ajax_illusts(payload: dict, sort: str, current_page: int) -> tuple[list[dict], bool]:
    body = payload.get("body") or {}
    illust_manga = body.get("illustManga") or []
    if isinstance(illust_manga, dict):
        raw_items = illust_manga.get("data") or illust_manga.get("items") or []
        # Pixiv web payload variants seen in the wild:
        # - nextUrl (string)
        # - isLastPage (bool)
        # - page/pageCount (ints)
        # - total (int) with fixed page size (often 60)
        if illust_manga.get("isLastPage") is True:
            has_next = False
        elif illust_manga.get("isLastPage") is False:
            has_next = True
        elif illust_manga.get("nextUrl"):
            has_next = True
        elif illust_manga.get("next"):
            has_next = True
        elif illust_manga.get("page") and illust_manga.get("pageCount"):
            try:
                has_next = int(illust_manga["page"]) < int(illust_manga["pageCount"])
            except Exception:
                has_next = False
        elif illust_manga.get("total"):
            try:
                total = int(illust_manga["total"])
                per_page = (
                    illust_manga.get("perPage")
                    or illust_manga.get("per_page")
                    or len(raw_items)
                    or int(getattr(config, "PIXIV_TAG_PAGE_SIZE", 60))
                )
                has_next = (current_page * int(per_page)) < total
            except Exception:
                has_next = False
        else:
            has_next = False
    else:
        raw_items = illust_manga
        has_next = False

    items: list[dict] = []
    seen_ids: set[int] = set()

    if sort == "popular_desc":
        popular = body.get("popular") or {}
        for bucket in ("permanent", "recent"):
            for raw in popular.get(bucket) or []:
                parsed = _normalize_ajax_artwork(raw)
                if not parsed:
                    continue
                iid = parsed["illust_id"]
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                items.append(parsed)

    for raw in raw_items or []:
        parsed = _normalize_ajax_artwork(raw)
        if not parsed:
            continue
        iid = parsed["illust_id"]
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        items.append(parsed)

    return items, has_next


def _fetch_tag_ajax(tag_session, query: str, page: int, sort: str) -> dict:
    encoded_query = quote(query, safe="")
    url = f"https://www.pixiv.net/ajax/search/artworks/{encoded_query}"
    params = {
        "word": query,
        "p": page,
        "order": {
            "date_desc": "date_d",
            "date_asc": "date",
        }.get(sort, "date_d"),
        "mode": "all",
        "s_mode": "s_tag",
        "type": "all",
        "lang": "zh",
    }
    timeout = float(getattr(config, "PIXIV_API_TIMEOUT", 60.0))
    response = tag_session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload.get("message") or "pixiv ajax search failed")
    return payload


# ──────────────────────────────────────────────
# API 初始化
# ──────────────────────────────────────────────

def _setup_api() -> AppPixivAPI:
    api = AppPixivAPI()
    if config.PROXY:
        api.set_additional_headers({"Proxy": config.PROXY})
        logger.info(f"使用代理: {config.PROXY}")
    api.auth(refresh_token=config.PIXIV_REFRESH_TOKEN)
    _api_last_auth[id(api)] = time.monotonic()  # 記錄 auth 時間，供 _maybe_reauth 使用
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
    """從 API 結果提取有效作品，統一處理類型過濾。"""
    return [
        _parse_illust(i)
        for i in result.get("illusts", [])
        if i.get("type") in ("illust", "manga", "ugoira")
    ]


def _fetch_ranking(api: AppPixivAPI, mode: str) -> list[dict]:
    _maybe_reauth(api)
    artworks, offset = [], 0
    page = 1
    while True:
        result = api.illust_ranking(mode=mode, offset=offset)
        if not result or "illusts" not in result or not result["illusts"]:
            _log_page_fetch(f"ranking:{mode}", page, offset=offset, items=0, next_url=False, status="empty")
            break
        page_items = _iter_illusts(result)
        artworks.extend(page_items)
        has_next = bool(result.get("next_url"))
        _log_page_fetch(f"ranking:{mode}", page, offset=offset, items=len(page_items), next_url=has_next)
        if not has_next:
            break
        offset += 30
        page += 1
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_tag(
    api: AppPixivAPI,
    tag: str,
    sort: str = "popular_desc",
    start_page: int = 1,
    max_pages: "int | None" = None,
    resume_query: "str | None" = None,
    resume_sort: "str | None" = None,
) -> "tuple[list[dict], bool, int, str | None, str | None]":
    """
    抓取 tag 搜尋結果，支援斷點續抓與頁數限制。

    start_page:   起始頁碼（1-based），恢復上次中斷的進度
    max_pages:    本次最多抓幾頁（None=不限）
    resume_query/sort: 上次成功使用的 query/sort，跳過重新探測

    回傳 (artworks, is_done, last_success_page, effective_query, effective_sort)
        is_done:           True=已無更多頁面，False=因 max_pages 或錯誤中止
        last_success_page: 最後成功抓到的頁碼（0=未抓任何頁）
    """
    def _tag_candidates(raw_tag: str) -> list[str]:
        cleaned = raw_tag.strip()
        candidates = [cleaned]
        no_hash = cleaned.lstrip("#＃").strip()
        if no_hash and no_hash != cleaned:
            candidates.append(no_hash)
        return list(dict.fromkeys(candidates))

    candidates = _tag_candidates(tag)
    sort_candidates = [sort]

    with _tag_request_lock:
        try:
            tag_session, session_kind = _build_tag_http_session(api)
            if tag_session is None:
                raise RuntimeError("failed to create http session")
        except Exception as e:
            logger.warning(f"tag AJAX session init failed: {e}")
            _log_page_fetch(
                f"tag:{tag}:{sort}", start_page, offset=0, items=0,
                next_url=False, status="ajax_init_error", extra={"error": str(e)},
            )
            return [], False, 0, None, None

        try:
            chosen_query: "str | None" = None
            chosen_sort: "str | None" = None
            artworks: list[dict] = []
            pages_fetched: int = 0
            has_next: bool = True
            last_success_page: int = 0
            page: int = 0

            # ── 決定 chosen_query / chosen_sort + 初始頁碼 ────────────────
            if start_page > 1 and resume_query and resume_sort:
                # 快速路徑：有上次記錄，直接跳到 start_page
                chosen_query = resume_query
                chosen_sort = resume_sort
                page = start_page - 1   # 迴圈第一次 += 1 後變成 start_page
                has_next = True
            else:
                # 探測路徑：從第 1 頁確認有效的 (query, sort)
                first_items: list[dict] = []
                first_has_next = False
                for try_sort in sort_candidates:
                    for query in candidates:
                        try:
                            payload = _fetch_tag_ajax(tag_session, query, 1, try_sort)
                            items, hn = _extract_ajax_illusts(payload, try_sort, 1)
                        except Exception as e:
                            logger.warning(f"tag AJAX request failed query={query} sort={try_sort}: {e}")
                            continue
                        if items:
                            chosen_query = query
                            chosen_sort = try_sort
                            first_items = items
                            first_has_next = hn
                            break
                    if chosen_query is not None:
                        break

                if chosen_query is None or chosen_sort is None:
                    _log_page_fetch(
                        f"tag:{tag}:{sort}", 1, offset=0, items=0, next_url=False,
                        status="empty",
                        extra={"query_candidates": candidates, "sort_candidates": sort_candidates,
                               "via": "ajax", "client": session_kind},
                    )
                    return [], True, 0, None, None

                if start_page == 1:
                    # 第 1 頁結果納入
                    artworks = list(first_items)
                    pages_fetched = 1
                    last_success_page = 1
                    has_next = first_has_next
                    _log_page_fetch(
                        f"tag:{tag}:{sort}", 1, offset=0, items=len(first_items),
                        next_url=has_next,
                        extra={"query": chosen_query, "effective_sort": chosen_sort,
                               "via": "ajax", "client": session_kind},
                    )
                    if not has_next:
                        return artworks, True, 1, chosen_query, chosen_sort
                    page = 1
                else:
                    # start_page > 1 但無 resume 資訊：探測成功後跳至 start_page
                    page = start_page - 1
                    has_next = True

            # ── 翻頁迴圈 ─────────────────────────────────────────────────
            is_done = True
            while has_next:
                if max_pages is not None and pages_fetched >= max_pages:
                    is_done = False
                    break
                page += 1
                time.sleep(config.FULL_CRAWL_API_DELAY)
                try:
                    payload = _fetch_tag_ajax(tag_session, chosen_query, page, chosen_sort)
                    page_items, has_next = _extract_ajax_illusts(payload, chosen_sort, page)
                except Exception as e:
                    _log_page_fetch(
                        f"tag:{tag}:{sort}", page, offset=(page - 1) * 60,
                        items=0, next_url=False, status="ajax_error",
                        extra={"query": chosen_query, "effective_sort": chosen_sort,
                               "error": str(e), "via": "ajax", "client": session_kind},
                    )
                    is_done = False  # 因錯誤中止，尚有更多
                    break
                if not page_items:
                    _log_page_fetch(
                        f"tag:{tag}:{sort}", page, offset=(page - 1) * 60,
                        items=0, next_url=False, status="empty",
                        extra={"query": chosen_query, "effective_sort": chosen_sort,
                               "via": "ajax", "client": session_kind},
                    )
                    is_done = True  # 正常結束
                    break
                artworks.extend(page_items)
                pages_fetched += 1
                last_success_page = page
                _log_page_fetch(
                    f"tag:{tag}:{sort}", page, offset=(page - 1) * 60,
                    items=len(page_items), next_url=has_next,
                    extra={"query": chosen_query, "effective_sort": chosen_sort,
                           "via": "ajax", "client": session_kind},
                )

            return artworks, is_done, last_success_page, chosen_query, chosen_sort

        finally:
            try:
                tag_session.close()
            except Exception:
                pass


def _fetch_related(api: AppPixivAPI, illust_id: int) -> list[dict]:
    """抓取相關作品，透過 next_qs 翻頁直到沒有下一頁。"""
    artworks: list[dict] = []
    result = api.illust_related(illust_id=illust_id)
    page = 1
    while result and "illusts" in result:
        if not result["illusts"]:
            _log_page_fetch(f"related:{illust_id}", page, items=0, next_url=False, status="empty")
            break
        page_items = _iter_illusts(result)
        artworks.extend(page_items)
        next_url = result.get("next_url")
        has_next = bool(next_url)
        _log_page_fetch(f"related:{illust_id}", page, items=len(page_items), next_url=has_next)
        if not has_next:
            break
        time.sleep(config.FULL_CRAWL_API_DELAY)
        try:
            qs = api.parse_qs(next_url)
            result = api.illust_related(**qs)
            page += 1
        except Exception:
            break
    return artworks


async def _fetch_related_async(api: AppPixivAPI, illust_id: int) -> list[dict]:
    """Fetch related artworks page-by-page with per-page timeout to avoid long blocking."""
    artworks: list[dict] = []
    page = 1
    timeout = float(getattr(config, "RELATED_API_TIMEOUT", 20.0))

    try:
        result = await _to_thread_with_timeout(
            api.illust_related,
            timeout=timeout,
            illust_id=illust_id,
        )
    except TimeoutError:
        _log_page_fetch(f"related:{illust_id}", page, items=0, next_url=False, status="timeout")
        return artworks
    except Exception:
        return artworks

    while result and "illusts" in result:
        if not result["illusts"]:
            _log_page_fetch(f"related:{illust_id}", page, items=0, next_url=False, status="empty")
            break
        page_items = _iter_illusts(result)
        artworks.extend(page_items)
        next_url = result.get("next_url")
        has_next = bool(next_url)
        _log_page_fetch(f"related:{illust_id}", page, items=len(page_items), next_url=has_next)
        if not has_next:
            break

        await asyncio.sleep(config.FULL_CRAWL_API_DELAY)
        try:
            qs = api.parse_qs(next_url)
            page += 1
            result = await _to_thread_with_timeout(
                api.illust_related,
                timeout=timeout,
                **qs,
            )
        except TimeoutError:
            _log_page_fetch(f"related:{illust_id}", page, items=0, next_url=False, status="timeout")
            break
        except Exception:
            break
    return artworks


def _fetch_user_artworks_sync(
    api: AppPixivAPI,
    user_id: int,
    api_lock: "threading.Lock | None" = None,
) -> list[dict]:
    _maybe_reauth(api)
    artworks: list[dict] = []
    seen_ids: set[int] = set()
    fetch_types = list(getattr(config, "USER_FETCH_TYPES", ["illust"]))

    for fetch_type in fetch_types:
        offset = 0
        page = 1
        while True:
            if api_lock:
                with api_lock:
                    result = api.user_illusts(user_id, type=fetch_type, offset=offset)
            else:
                result = api.user_illusts(user_id, type=fetch_type, offset=offset)
            source = f"user_sync:{user_id}:{fetch_type}"
            if not result or "illusts" not in result or not result["illusts"]:
                _log_page_fetch(source, page, offset=offset, items=0, next_url=False, status="empty")
                break
            page_items = _iter_illusts(result)
            added = 0
            for aw in page_items:
                iid = aw.get("illust_id")
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                artworks.append(aw)
                added += 1
            has_next = bool(result.get("next_url"))
            _log_page_fetch(source, page, offset=offset, items=added, next_url=has_next)
            if added == 0 or not has_next:
                break
            offset += 30
            page += 1
            time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_recommended(api: AppPixivAPI) -> list[dict]:
    _maybe_reauth(api)
    artworks, offset = [], 0
    page = 1
    while True:
        result = api.illust_recommended(offset=offset)
        if not result or "illusts" not in result or not result["illusts"]:
            _log_page_fetch("recommended", page, offset=offset, items=0, next_url=False, status="empty")
            break
        page_items = _iter_illusts(result)
        artworks.extend(page_items)
        has_next = bool(result.get("next_url"))
        _log_page_fetch("recommended", page, offset=offset, items=len(page_items), next_url=has_next)
        if not has_next:
            break
        offset += 30
        page += 1
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


def _fetch_new_illusts(api: AppPixivAPI, content_type: str = "illust") -> list[dict]:
    """抓取全站最新上傳作品（每輪前 NEW_ILLUSTS_MAX_PAGES 頁），作為擴散種子。"""
    _maybe_reauth(api)
    max_pages: int = getattr(config, "NEW_ILLUSTS_MAX_PAGES", 15)
    artworks: list[dict] = []
    try:
        result = api.illust_new(content_type=content_type)
    except Exception as e:
        logger.warning(f"[新作品] illust_new({content_type}) 不支援或失敗: {e}")
        return artworks
    pages = 0
    page = 1
    while result and "illusts" in result and pages < max_pages:
        if not result["illusts"]:
            _log_page_fetch(f"new:{content_type}", page, offset=pages * 30, items=0, next_url=False, status="empty")
            break
        page_items = _iter_illusts(result)
        artworks.extend(page_items)
        next_url = result.get("next_url")
        has_next = bool(next_url)
        _log_page_fetch(f"new:{content_type}", page, offset=pages * 30, items=len(page_items), next_url=has_next)
        if not has_next:
            break
        time.sleep(config.FULL_CRAWL_API_DELAY)
        try:
            qs = api.parse_qs(next_url)
            result = api.illust_new(**qs)
        except Exception:
            break
        pages += 1
        page += 1
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
                    detail = await _to_thread_with_timeout(api.illust_detail, artwork["illust_id"])
                    await asyncio.sleep(0.3)  # API 禮貌延遲
            else:
                detail = await _to_thread_with_timeout(api.illust_detail, artwork["illust_id"])
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
        user_detail = await _to_thread_with_timeout(api.user_detail, user_id)
        user_name = user_detail["user"]["name"] if user_detail else str(user_id)
        logger.info(f"確認作者: https://www.pixiv.net/users/{user_id} ({user_name})")
    except Exception as e:
        logger.warning(f"無法確認作者 {user_id}: {e}")
        user_name = str(user_id)

    artworks: list[dict] = []
    seen_ids: set[int] = set()
    fetch_types = list(getattr(config, "USER_FETCH_TYPES", ["illust"]))

    for fetch_type in fetch_types:
        offset = 0
        page = 1
        while not stop_event.is_set():
            try:
                result = await _to_thread_with_timeout(
                    api.user_illusts, user_id, type=fetch_type, offset=offset
                )
            except TimeoutError:
                _log_timeout(
                    "user_async_page",
                    f"user_async:{user_id}:{fetch_type}",
                    getattr(config, "PIXIV_API_TIMEOUT", 60.0),
                    page=page,
                    extra={"offset": offset},
                )
                raise
            source = f"user_async:{user_id}:{fetch_type}"
            if not result or "illusts" not in result or not result["illusts"]:
                _log_page_fetch(source, page, offset=offset, items=0, next_url=False, status="empty")
                break
            page_items = _iter_illusts(result)
            added = 0
            for aw in page_items:
                iid = aw.get("illust_id")
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                artworks.append(aw)
                added += 1
            has_next = bool(result.get("next_url"))
            _log_page_fetch(source, page, offset=offset, items=added, next_url=has_next)
            if added == 0 or not has_next:
                break
            offset += 30
            page += 1
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
# User ID 掃描批次（tag→user 交替用）
# ──────────────────────────────────────────────

async def _scan_user_batch_async(
    scan_api: "AppPixivAPI",
    dl_headers: dict,
    visited_users: "set[int]",
    stop_event: "threading.Event",
    batch_size: int,
    on_success: "Callable[[dict], None]",
    main_api: "AppPixivAPI | None" = None,
) -> int:
    """
    從上次記錄的 cursor 開始，掃描直到找到 batch_size 個有效用戶並爬取其作品。
    回傳實際處理的有效用戶數。相關作品/擴散不在本函式處理。
    """
    if not getattr(config, "USER_ID_SCAN_ENABLED", True):
        return 0

    delay = float(getattr(config, "USER_ID_SCAN_DELAY", 1.5))
    cursor = _load_scan_cursor()
    users_done = 0
    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)
    api_detail_sem = asyncio.Semaphore(getattr(config, "API_DETAIL_CONCURRENCY", 3))
    api_lock = threading.Lock()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0}
    _proc_api = main_api or scan_api

    connector = aiohttp.TCPConnector(
        limit=config.DOWNLOAD_WORKERS * 3,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
    )
    async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:
        while users_done < batch_size and not stop_event.is_set():
            cursor += 1
            _save_scan_cursor(cursor)

            if cursor in visited_users:
                continue
            visited_users.add(cursor)

            # 探測 user_detail
            try:
                result = await _to_thread_with_timeout(scan_api.user_detail, cursor)
                await asyncio.sleep(delay)
                if not result or "user" not in result:
                    continue
                user_name = result["user"]["name"]
            except Exception as e:
                logger.debug(f"[user_scan] user={cursor} 無效: {e}")
                await asyncio.sleep(delay)
                continue

            # 抓取該作者全部作品
            try:
                artworks = await _to_thread_with_timeout(
                    _fetch_user_artworks_sync, scan_api, cursor, api_lock
                )
            except Exception as e:
                logger.warning(f"[user_scan] user={cursor} 作品抓取失敗: {e}")
                artworks = []

            if artworks:
                logger.info(f"[user_scan] user={cursor} ({user_name}) → {len(artworks)} 件")
                await _process_batch_async(
                    _proc_api, session, artworks, counters,
                    stop_event, download_sem,
                    on_success=on_success,
                    api_sem=api_detail_sem,
                )

            users_done += 1

    _save_scan_cursor(cursor)
    logger.info(f"[user_scan] 批次完成，處理 {users_done} 位用戶，cursor={cursor}")
    return users_done


# ──────────────────────────────────────────────
# 全站爬取（async 核心）
# ──────────────────────────────────────────────

async def _run_full_crawl_async(
    stop_event: threading.Event,
    api: AppPixivAPI,
    dl_headers: dict,
    visited_users: "set[int] | None" = None,
    scan_api: "AppPixivAPI | None" = None,
    scan_dl_headers: "dict | None" = None,
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
    skip_related_budget = {"count": 0}

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
        """已索引作品採折衷擴散：作者必擴，相關作品採機率+限額擴散。"""
        uid = artwork.get("user_id")
        if uid and uid not in visited_users:
            visited_users.add(uid)
            user_diff_q.put_nowait(uid)

        if not getattr(config, "SKIP_RELATED_DIFFUSION_ENABLED", True):
            return

        iid = artwork.get("illust_id")
        if not iid or iid in related_visited:
            return

        if related_diff_q.qsize() >= int(getattr(config, "SKIP_RELATED_QUEUE_SOFT_LIMIT", 2000)):
            return
        if skip_related_budget["count"] >= int(getattr(config, "SKIP_RELATED_MAX_PER_ROUND", 200)):
            return
        if random.random() > float(getattr(config, "SKIP_RELATED_SAMPLE_RATE", 0.1)):
            return

        related_visited.add(iid)
        related_diff_q.put_nowait(iid)
        skip_related_budget["count"] += 1

    # ── 非 tag 種子（全站最新 / 排行 / 推薦）────────────────────────
    def _non_tag_sources():
        yield _fetch_new_illusts, api, "illust"
        yield _fetch_new_illusts, api, "manga"
        for mode in config.ALL_RANKING_MODES:
            yield _fetch_ranking, api, mode
        yield _fetch_recommended, api

    # 載入 tag 爬取進度（爬蟲重啟後從記錄點繼續）
    _load_tag_progress()

    while not stop_event.is_set():
        counters["round"] += 1
        current_round = counters["round"]
        skip_related_budget["count"] = 0
        diffusion_user_quota = int(getattr(config, "DIFFUSION_USER_QUOTA_PER_TICK", 2))
        diffusion_related_quota = int(getattr(config, "DIFFUSION_RELATED_QUOTA_PER_TICK", 2))
        diffusion_tail_multiplier = int(getattr(config, "DIFFUSION_TAIL_MULTIPLIER", 5))
        seed_sources_per_diffusion_tick = int(getattr(config, "SEED_SOURCES_PER_DIFFUSION_TICK", 1))
        max_tag_pages = int(getattr(config, "TAG_PAGES_PER_VISIT", 100))
        user_batch_size = int(getattr(config, "USER_SCAN_BATCH_SIZE", 100))
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

        async def _drain_diffusion(
            user_budget: int | None = None,
            related_budget: int | None = None,
        ) -> tuple[int, int]:
            """Drain diffusion queues with budgets to avoid starving seed/tag crawling."""
            user_done = 0
            related_done = 0
            while not stop_event.is_set() and not user_diff_q.empty():
                # Priority authors should preempt diffusion work immediately.
                if get_priority_queue_size() > 0:
                    break
                if user_budget is not None and user_done >= user_budget:
                    break
                uid = user_diff_q.get_nowait()
                try:
                    artworks = await _to_thread_with_timeout(_fetch_user_artworks_sync, api, uid)
                    logger.info(f"[擴散-作者] user={uid} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[擴散-作者] 失敗 user={uid}: {e}")
                    artworks = []
                await _process(artworks)
                user_done += 1
                if related_budget is None or related_done < related_budget:
                    related_done += await _drain_related(max_items=1)

            if related_budget is None:
                related_done += await _drain_related()
            else:
                related_done += await _drain_related(max_items=max(0, related_budget - related_done))
            return user_done, related_done

        async def _drain_related(max_items: int | None = None) -> int:
            done = 0
            while not stop_event.is_set() and not related_diff_q.empty():
                # Priority authors should preempt diffusion work immediately.
                if get_priority_queue_size() > 0:
                    break
                if max_items is not None and done >= max_items:
                    break
                iid = related_diff_q.get_nowait()
                try:
                    artworks = await _fetch_related_async(api, iid)
                    logger.info(f"[擴散-相關] illust={iid} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[擴散-相關] 失敗 illust={iid}: {e}")
                    artworks = []
                await _process(artworks)
                done += 1
            return done

        async def _priority_watcher() -> None:
            """
            並行背景監視優先佇列。
            asyncio 合作式排程：只要主流程到達任何 await 點，此 task 就能立即被排程。
            """
            while not stop_event.is_set():
                await _drain_priority()
                await asyncio.sleep(2)

        # 啟動優先監視器（整個 round 含 60s 等待期間都有效）
        _watcher_task = asyncio.create_task(_priority_watcher())
        try:
            await _drain_priority()

            # ── 建立本輪 tag 優先排序清單 ────────────────────────────────
            # 未開始 (page=0) > 進行中 (done=False) > 已完成 (done=True，重跑找新圖)
            tags_sorts = [
                (tag, sort)
                for tag in config.ALL_TAGS
                for sort in getattr(config, "CRAWL_TAG_SORTS", ["popular_desc", "date_desc", "date_asc"])
            ]

            def _tag_priority(ts: tuple) -> int:
                p = _get_tag_progress(ts[0], ts[1])
                if not p:
                    return 0   # 從未抓過 → 最優先
                if not p.get("done", False):
                    return 1   # 進行中（上次未抓完）→ 次優先
                return 2       # 已完成

            # 只處理尚未完成的 tag（priority < 2）。
            # done=True 的 tag 不清除進度，保留在檔案中以便重啟後識別狀態。
            # 若所有 tag 皆已完成（active_tags 為空），才將全部 tag 納入
            # 重跑（每個都從第 1 頁開始），以便找出新上傳的圖片。
            active_tags = [ts for ts in tags_sorts if _tag_priority(ts) < 2]
            if not active_tags:
                logger.info("[進度] 所有 tag 均已完成，本輪重跑全部 tag 找新圖")
                active_tags = list(tags_sorts)
            active_tags.sort(key=_tag_priority)

            # ── tag → user_scan 交替主迴圈 ───────────────────────────────
            # 每個 tag 抓 max_tag_pages 頁後，觸發一次 user_scan 批次
            for tag, sort in active_tags:
                if stop_event.is_set():
                    break
                await _drain_priority()

                # 讀取此 tag/sort 的斷點進度
                progress = _get_tag_progress(tag, sort)
                if progress.get("done", False):
                    # 已完成的 tag：本輪重跑找新圖，從第 1 頁開始，不沿用舊查詢
                    start_page = 1
                    resume_q = None
                    resume_s = None
                else:
                    last_p = progress.get("page", 0)
                    start_page = last_p + 1 if last_p > 0 else 1
                    resume_q = progress.get("chosen_query")
                    resume_s = progress.get("chosen_sort")

                try:
                    artworks, is_done, last_page, eff_q, eff_s = await _to_thread_with_timeout(
                        _fetch_tag, api, tag, sort,
                        start_page, max_tag_pages, resume_q, resume_s,
                    )
                    _update_tag_progress(tag, sort, last_page, is_done, eff_q, eff_s)
                    _save_tag_progress()
                    logger.info(
                        f"[tag] {tag}:{sort} p{start_page}-{last_page} "
                        f"{'完成' if is_done else '暫停'} → {len(artworks)} 件"
                    )
                except Exception as e:
                    logger.warning(f"[tag] {tag}:{sort} 失敗: {e}")
                    artworks = []

                await _process(artworks)
                await _drain_priority()
                await _drain_diffusion(
                    user_budget=diffusion_user_quota,
                    related_budget=diffusion_related_quota,
                )

                # ── 每個 tag 後執行 user_scan 批次 ──────────────────────
                if scan_api and not stop_event.is_set():
                    await _scan_user_batch_async(
                        scan_api,
                        scan_dl_headers or {},
                        visited_users,
                        stop_event,
                        user_batch_size,
                        on_success=_on_artwork_success,
                        main_api=api,
                    )
                    await _drain_priority()
                    await _drain_diffusion(
                        user_budget=diffusion_user_quota,
                        related_budget=diffusion_related_quota,
                    )

            # ── 非 tag 種子（最新上傳 / 排行榜 / 推薦）────────────────────
            for fetch_fn, *args in _non_tag_sources():
                if stop_event.is_set():
                    break
                await _drain_priority()
                try:
                    artworks = await _to_thread_with_timeout(fetch_fn, *args)
                    label = f"{fetch_fn.__name__}({', '.join(str(a) for a in args[1:])})"
                    logger.info(f"[種子] {label} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[種子] 來源失敗: {e}")
                    artworks = []
                await _process(artworks)
                await _drain_priority()
                await _drain_diffusion(
                    user_budget=diffusion_user_quota,
                    related_budget=diffusion_related_quota,
                )

            # ── 尾端擴散（清空積壓的佇列）──────────────────────────────
            await _drain_diffusion(
                user_budget=diffusion_user_quota * diffusion_tail_multiplier,
                related_budget=diffusion_related_quota * diffusion_tail_multiplier,
            )
            await _drain_priority()
            await _drain_diffusion(
                user_budget=diffusion_user_quota * diffusion_tail_multiplier,
                related_budget=diffusion_related_quota * diffusion_tail_multiplier,
            )

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
    api_lock: "threading.Lock | None" = None,
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
            result = await _to_thread_with_timeout(fn, *args)
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
                # api_lock 序列化各 worker 對 api 的實際 HTTP 呼叫，sleep 期間不持鎖
                try:
                    artworks = await _to_thread_with_timeout(
                        _fetch_user_artworks_sync, api, uid, api_lock
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
        # visited_users 在 tag 爬取與 user_scan 間共享，避免重複爬同一作者
        visited_users: set[int] = await asyncio.to_thread(db.get_all_user_ids)

        # 給 user_id_scan 建立獨立的 api 物件，避免與主爬蟲共用 requests.Session
        # （requests.Session 不是 thread-safe，共用會導致 search_illust 回傳空結果）
        try:
            scan_api = await asyncio.to_thread(_setup_api)
            scan_dl_headers = _get_dl_headers(scan_api)
        except Exception as e:
            logger.warning(f"掃描 API 驗證失敗，跳過 user_scan 批次: {e}")
            scan_api = None
            scan_dl_headers = None

        await _run_full_crawl_async(
            stop_event, api, dl_headers, visited_users,
            scan_api=scan_api,
            scan_dl_headers=scan_dl_headers,
        )

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
