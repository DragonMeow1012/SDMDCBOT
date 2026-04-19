"""
Pixiv 爬蟲模組
- asyncio + aiohttp 並行下載圖片（DOWNLOAD_WORKERS 並發）
- producer/consumer：API 抓取（asyncio.to_thread）與下載同步進行
- run_full_crawl / crawl_user_by_id 對外仍為同步介面（在背景執行緒呼叫 asyncio.run）
"""
import asyncio
import gc
import io
import json
import logging
import random
import ssl
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Callable

import aiohttp
from PIL import Image
from pixivpy3 import AppPixivAPI

import pixiv_config as config
import pixiv_database as db
import pixiv_feature as fe
from utils.bloom import BloomFilter

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
# i.pximg.net 用 SSL context
# OpenSSL 3.x 預設禁用 unsafe legacy renegotiation (CVE-2009-3555 保護)，
# 但 Pixiv CDN 在資料傳輸途中會發起 TLS renegotiation (curl 看得到
# "remote party requests renegotiation")，導致 aiohttp 連線被中斷，
# 表現為 WinError 10053「本機系統已中止網路連線」。
# 啟用 SSL_OP_LEGACY_SERVER_CONNECT 可恢復舊行為，curl 預設就是如此。
# 僅用於 i.pximg.net 圖片下載，pixivpy 的 API 走 requests 不受影響。
# ──────────────────────────────────────────────
_PXIMG_SSL_CONTEXT: "ssl.SSLContext | None" = None


def _get_pximg_ssl_context() -> ssl.SSLContext:
    global _PXIMG_SSL_CONTEXT
    if _PXIMG_SSL_CONTEXT is None:
        ctx = ssl.create_default_context()
        op_legacy = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        ctx.options |= op_legacy
        _PXIMG_SSL_CONTEXT = ctx
    return _PXIMG_SSL_CONTEXT


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
    limit = float(getattr(config, "DOWNLOAD_RATE_LIMIT_Mbps", 0.0) or 0.0)
    max_limit = float(getattr(config, "MAX_DOWNLOAD_RATE_LIMIT_Mbps", 0.0) or 0.0)
    if max_limit > 0 and limit > max_limit:
        logger.info(f"下載限速 {limit} Mbps > 上限 {max_limit} Mbps，已自動限制")
        limit = max_limit
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
_priority_user_queue: "deque[int]" = deque()
_priority_user_ids: set[int] = set()
_priority_lock = threading.Lock()
_priority_user_done_hook: "Callable[[dict], None] | None" = None
_page_log_lock = threading.Lock()
_PAGE_LOG_MAX_LINES = 500
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
        # 超過閾值才讀整個檔案裁切，避免每次寫入都讀整檔。
        # 閾值設成 _PAGE_LOG_MAX_LINES × 150 bytes（寬估單行長度），
        # 超出時砍掉最舊的部分只留最新 _PAGE_LOG_MAX_LINES 行。
        try:
            if log_path.stat().st_size > _PAGE_LOG_MAX_LINES * 150:
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
    log_file: str = "page_log.jsonl",
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
    _append_page_log(log_file, payload)


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

def _tag_key(tag: str, sort: str, window: "str | None" = None) -> str:
    base = f"{tag}::{sort}"
    return f"{base}::w:{window}" if window else base


def _date_windows() -> list[tuple["str | None", "str | None"]]:
    """依 config 產生 (start_date, end_date) 日期窗口清單。
    TAG_DATE_SLICE_DAYS=0 → 回傳單一 (None, None) 代表停用日期切片、維持舊行為。
    窗口為 inclusive，格式 YYYY-MM-DD。逆序排列（最新的窗口先跑，更有機會撞到熱門作品）。
    """
    days = int(getattr(config, "TAG_DATE_SLICE_DAYS", 0) or 0)
    if days <= 0:
        return [(None, None)]
    from datetime import date, timedelta
    try:
        start_str = str(getattr(config, "TAG_DATE_SLICE_START", "2007-09-10"))
        anchor = date.fromisoformat(start_str)
    except Exception:
        anchor = date(2007, 9, 10)
    today = date.today()
    windows: list[tuple[str, str]] = []
    cur = anchor
    while cur <= today:
        end = min(cur + timedelta(days=days - 1), today)
        windows.append((cur.isoformat(), end.isoformat()))
        cur = end + timedelta(days=1)
    windows.reverse()
    return [(s, e) for s, e in windows]


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


def _get_tag_progress(tag: str, sort: str, window: "str | None" = None) -> dict:
    key = _tag_key(tag, sort, window)
    with _tag_progress_lock:
        return dict(_tag_progress.get(key, {}))


def _update_tag_progress(
    tag: str,
    sort: str,
    last_page: int,
    done: bool,
    chosen_query: "str | None" = None,
    chosen_sort: "str | None" = None,
    window: "str | None" = None,
) -> None:
    key = _tag_key(tag, sort, window)
    with _tag_progress_lock:
        _tag_progress[key] = {
            "page": last_page,
            "done": done,
            "chosen_query": chosen_query,
            "chosen_sort": chosen_sort,
            "window": window,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


def _get_last_processed_tag() -> "str | None":
    """返回 _tag_progress 中 ts 最新的 tag key，用於重啟後對齊斷點。"""
    with _tag_progress_lock:
        entries = {k: v for k, v in _tag_progress.items() if k != "__global__" and "ts" in v}
    if not entries:
        return None
    return max(entries, key=lambda k: entries[k]["ts"])


# ──────────────────────────────────────────────
# Ranking 每日執行狀態（避免同一天重複插入任務）
# ──────────────────────────────────────────────

def _today_ymd() -> str:
    return time.strftime("%Y-%m-%d")


def _load_ranking_state() -> dict:
    path = getattr(config, "RANKING_LAST_RUN_FILE", "")
    if not path:
        return {"date": None, "done_modes": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        date = data.get("date")
        done_modes = data.get("done_modes") or []
        if not isinstance(done_modes, list):
            done_modes = []
        return {"date": date, "done_modes": [str(m) for m in done_modes]}
    except FileNotFoundError:
        return {"date": None, "done_modes": []}
    except Exception as e:
        logger.warning(f"[ranking] 載入每日狀態失敗: {e}")
        return {"date": None, "done_modes": []}


def _save_ranking_state(state: dict) -> None:
    path = getattr(config, "RANKING_LAST_RUN_FILE", "")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[ranking] 儲存每日狀態失敗: {e}")


def _load_frontier_state() -> dict:
    path = getattr(config, "FRONTIER_STATE_FILE", "")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[frontier] 載入狀態失敗: {e}")
        return {}


def _save_frontier_state(max_id: int) -> None:
    path = getattr(config, "FRONTIER_STATE_FILE", "")
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "max_id": int(max_id),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[frontier] 儲存狀態失敗: {e}")


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


def _maybe_emit_progress(counters: dict) -> None:
    processed = counters.get("downloaded", 0) + counters.get("failed", 0) + counters.get("skipped", 0)
    if _progress_hook and processed > 0 and processed % _HOOK_INTERVAL == 0:
        _progress_hook(dict(counters))


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

def _install_requests_default_timeout(api: AppPixivAPI) -> None:
    """強制 api.requests 每次 HTTP 呼叫都帶 timeout，避免 TCP 半卡時永遠阻塞。
    pixivpy3 底層是 cloudscraper/requests.Session，預設不帶 timeout；一旦連線半卡，
    就會把 asyncio.to_thread 的 executor 執行緒無限期佔住（asyncio.wait_for 無法殺執行緒），
    累積到執行緒池耗盡後整條爬取管線 deadlock。
    """
    if getattr(api.requests, "_dc_timeout_patched", False):
        return
    connect_t = float(getattr(config, "PIXIV_API_CONNECT_TIMEOUT", 10.0))
    read_t = float(getattr(config, "PIXIV_API_READ_TIMEOUT", 30.0))
    default_timeout = (connect_t, read_t)
    orig_request = api.requests.request

    def request_with_timeout(method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = default_timeout
        return orig_request(method, url, **kwargs)

    api.requests.request = request_with_timeout
    api.requests._dc_timeout_patched = True


def _setup_api(token: "str | None" = None) -> AppPixivAPI:
    api = AppPixivAPI()
    if config.PROXY:
        api.set_additional_headers({"Proxy": config.PROXY})
        logger.info(f"使用代理: {config.PROXY}")
    _install_requests_default_timeout(api)
    refresh_token = token or config.PIXIV_REFRESH_TOKEN
    api.auth(refresh_token=refresh_token)
    _api_last_auth[id(api)] = time.monotonic()  # 記錄 auth 時間，供 _maybe_reauth 使用
    logger.info(f"Pixiv 驗證成功 (token ...{refresh_token[-6:]})")
    return api


def _setup_api_pool() -> list[AppPixivAPI]:
    """為 PIXIV_REFRESH_TOKENS 每一組 token 建立一個 AppPixivAPI。
    缺 token 時只回傳主 api 一組，行為與原本單 token 路徑一致。"""
    tokens = list(getattr(config, "PIXIV_REFRESH_TOKENS", None) or [])
    if not tokens:
        return [_setup_api()]
    pool: list[AppPixivAPI] = []
    for t in tokens:
        try:
            pool.append(_setup_api(t))
        except Exception as e:
            logger.warning(f"token ...{t[-6:]} 驗證失敗，跳過: {e}")
    if not pool:
        raise RuntimeError("所有 PIXIV_REFRESH_TOKENS 皆驗證失敗")
    logger.info(f"[pool] 共 {len(pool)} 組 token 啟用並行爬取")
    return pool


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
        "tag_names":  tags,   # 以 list 形式傳給 replace_artwork_tags；不再寫 JSON 進 artworks 表
        "bookmarks":  illust["total_bookmarks"],
        "views":      illust["total_view"],
        "width":      illust["width"],
        "height":     illust["height"],
        "page_count": illust["page_count"],
        "image_url":  image_url,
        "gallery_urls": gallery_urls,
        "created_at": illust["create_date"],
    }


def _extract_gallery_urls(illust: dict) -> list[str]:
    preferred = (getattr(config, "PREFERRED_IMAGE_SIZE", "large") or "large").lower()
    if preferred not in ("original", "large"):
        preferred = "large"

    def _pick(image_urls: dict) -> "str | None":
        if preferred == "large":
            return image_urls.get("large") or image_urls.get("original")
        return image_urls.get("original") or image_urls.get("large")

    urls: list[str] = []
    meta_pages = illust.get("meta_pages", []) or []
    for page in meta_pages:
        image_urls = page.get("image_urls", {}) or {}
        url = _pick(image_urls)
        if url:
            urls.append(url)

    if not urls:
        single = (illust.get("meta_single_page", {}) or {}).get("original_image_url")
        fallback_large = (illust.get("image_urls", {}) or {}).get("large")
        if preferred == "large":
            if fallback_large:
                urls.append(fallback_large)
            elif single:
                urls.append(single)
        else:
            if single:
                urls.append(single)
            elif fallback_large:
                urls.append(fallback_large)
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


def _fetch_ranking(
    api: AppPixivAPI,
    mode: str,
    stop_event: "threading.Event | None" = None,
) -> list[dict]:
    """抓取單一排行榜所有頁；stop_event 設置時在頁間立即中止。"""
    _maybe_reauth(api)
    artworks, offset = [], 0
    page = 1
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        result = api.illust_ranking(mode=mode, offset=offset)
        if not result or "illusts" not in result or not result["illusts"]:
            _log_page_fetch(f"ranking:{mode}", page, offset=offset, items=0, next_url=False, status="empty")
            break
        page_items = _iter_illusts(result)
        artworks.extend(page_items)
        has_next = bool(result.get("next_url"))
        _log_page_fetch(f"ranking:{mode}", page, offset=offset, items=len(page_items), next_url=has_next)
        logger.info(
            f"[ranking] {mode} 第 {page} 頁｜本頁 {len(page_items)} 件，累計 {len(artworks)} 件"
            + ("" if has_next else "  ← 最後一頁")
        )
        if not has_next:
            break
        offset += 30
        page += 1
        time.sleep(config.FULL_CRAWL_API_DELAY)
    return artworks


async def _fetch_tag_stream(
    api: AppPixivAPI,
    tag: str,
    sort: str,
    start_page: int,
    max_pages: "int | None",
    resume_query: "str | None",
    resume_sort: "str | None",
    stop_event: "threading.Event | None",
    state: dict,
    flush_pages: int = 20,
    start_date: "str | None" = None,
    end_date: "str | None" = None,
) -> AsyncIterator[list[dict]]:
    """Stream tag 搜尋結果：每 `flush_pages` 頁 yield 一批作品，讓呼叫端能邊抓邊下載。
    使用 App API `search_illust`（30 件/頁），繞過 web AJAX 的 Cloudflare 403。
    迭代結束後 `state` 會填入：is_done / last_page / effective_query / effective_sort / total_artworks。
    備註：`start_page` 為 App API 頁碼（offset = (page-1)*30）；舊 AJAX 檔是 60 件/頁，
    切換後從同 page 值重入會多覆蓋，但 DB 去重，只多耗 API 不丟資料。
    """
    state["is_done"] = False
    state["last_page"] = max(0, start_page - 1)
    state["effective_query"] = resume_query or tag
    state["effective_sort"] = resume_sort or sort
    state["total_artworks"] = 0

    query = (resume_query or tag).strip()
    effective_sort = resume_sort or sort

    buffer: list[dict] = []
    pages_in_buffer: int = 0
    pages_fetched: int = 0
    page: int = max(1, start_page)
    is_done: bool = True

    while True:
        if stop_event is not None and stop_event.is_set():
            is_done = False
            break
        if max_pages is not None and pages_fetched >= max_pages:
            is_done = False
            break

        offset = (page - 1) * 30
        # 日期切片：若 caller 傳入 start_date/end_date，帶給 pixiv API 做範圍搜尋
        search_kwargs: dict = {"word": query, "sort": effective_sort, "offset": offset}
        if start_date:
            search_kwargs["start_date"] = start_date
        if end_date:
            search_kwargs["end_date"] = end_date
        window_label = f"{start_date}_{end_date}" if (start_date or end_date) else None
        src_label = f"tag:{tag}:{sort}" + (f":{window_label}" if window_label else "")
        window_extra = {"start_date": start_date, "end_date": end_date} if window_label else {}
        try:
            result = await _to_thread_with_timeout(
                api.search_illust,
                **search_kwargs,
            )
        except TimeoutError:
            _log_page_fetch(
                src_label, page, offset=offset,
                items=0, next_url=False, status="timeout",
                extra={"query": query, "effective_sort": effective_sort, "via": "app_api", **window_extra},
            )
            is_done = False
            break
        except Exception as e:
            _log_page_fetch(
                src_label, page, offset=offset,
                items=0, next_url=False, status="api_error",
                extra={"query": query, "effective_sort": effective_sort,
                       "error": str(e), "via": "app_api", **window_extra},
            )
            is_done = False
            break

        if not result or "illusts" not in result:
            _log_page_fetch(
                src_label, page, offset=offset,
                items=0, next_url=False, status="empty",
                extra={"query": query, "effective_sort": effective_sort, "via": "app_api", **window_extra},
            )
            is_done = True
            break

        page_items = _iter_illusts(result)
        # 套用 TAG_BOOKMARK_MAX 長尾過濾（避開 ranking 重疊）
        cap = int(getattr(config, "TAG_BOOKMARK_MAX", 0) or 0)
        if cap > 0:
            page_items = [it for it in page_items if int(it.get("bookmarks") or 0) <= cap]

        has_next = bool(result.get("next_url"))

        if not page_items and not has_next:
            _log_page_fetch(
                src_label, page, offset=offset,
                items=0, next_url=False, status="empty",
                extra={"query": query, "effective_sort": effective_sort, "via": "app_api", **window_extra},
            )
            is_done = True
            break

        buffer.extend(page_items)
        pages_in_buffer += 1
        pages_fetched += 1
        state["last_page"] = page
        state["total_artworks"] += len(page_items)
        _log_page_fetch(
            src_label, page, offset=offset,
            items=len(page_items), next_url=has_next,
            extra={"query": query, "effective_sort": effective_sort, "via": "app_api", **window_extra},
        )
        limit_str = f"/{max_pages}" if max_pages is not None else ""
        logger.info(
            f"[tag] 「{tag}」{sort} "
            f"第 {page} 頁{limit_str}｜本頁 {len(page_items)} 件，累計 {state['total_artworks']} 件"
            + ("" if has_next else "  ← 最後一頁")
        )

        if pages_in_buffer >= flush_pages and buffer:
            batch = buffer
            buffer = []
            pages_in_buffer = 0
            yield batch

        if not has_next:
            is_done = True
            break

        page += 1
        await asyncio.sleep(config.FULL_CRAWL_API_DELAY)

    state["is_done"] = is_done
    if buffer:
        yield buffer


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

    max_related_pages = int(getattr(config, "RELATED_MAX_PAGES", 100))

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
        if page >= max_related_pages:
            _log_page_fetch(f"related:{illust_id}", page, items=0, next_url=True, status="limit")
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


def _fetch_recommended(
    api: AppPixivAPI,
    stop_event: "threading.Event | None" = None,
) -> list[dict]:
    _maybe_reauth(api)
    artworks, offset = [], 0
    page = 1
    while True:
        if stop_event is not None and stop_event.is_set():
            break
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


def _fetch_new_illusts(
    api: AppPixivAPI,
    content_type: str = "illust",
    stop_event: "threading.Event | None" = None,
) -> list[dict]:
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
        if stop_event is not None and stop_event.is_set():
            break
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
                if isinstance(result, dict) and "error" in result:
                    logger.warning(
                        f"user_illusts {user_id}:{fetch_type} p{page} 異常回應："
                        f"{result.get('error')}"
                    )
                elif not result:
                    logger.warning(f"user_illusts {user_id}:{fetch_type} p{page} 空回應")
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
        urls = await _ensure_gallery_urls(api, artwork, api_sem)
        if not urls:
            counters["failed"] += 1
            return

        async with sem:
            page_features: list[tuple[int, str, object, bytes]] = []
            urls_to_fetch = urls[:config.MAX_GALLERY_PAGES]

            def _compute_hashes(raw: bytes) -> tuple[object, bytes]:
                """CPU-bound：同時算 pHash + NN binary hash，共享一次 decode/resize。"""
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail(config.MAX_IMAGE_SIZE, Image.LANCZOS)
                phash = fe.extract_phash(img)
                nn_vec = fe.extract_nn_hash(img)
                return phash, nn_vec.tobytes()

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

                        # 在執行緒池同時算 pHash + NN hash，避免兩次 decode
                        phash_vec, nn_blob = await asyncio.to_thread(_compute_hashes, data)
                        page_features.append((page_index, page_url, phash_vec, nn_blob))
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
                db.replace_artwork_tags(illust_id, artwork.get("tag_names") or [])
                first_phash = None
                import numpy as _np
                for page_index, page_url, phash_vec, nn_blob in page_features:
                    db.upsert_gallery_page(
                        illust_id=illust_id,
                        page_index=page_index,
                        image_url=page_url,
                        phash_vec=phash_vec,
                        nn_hash=nn_blob,
                    )
                    fe.add_to_index(illust_id, page_index, phash_vec)
                    nn_vec = _np.frombuffer(nn_blob, dtype=_np.uint8)
                    fe.add_nn_to_index(illust_id, page_index, nn_vec)
                    if first_phash is None:
                        first_phash = phash_vec
                if first_phash is not None:
                    db.upsert_features(illust_id, first_phash)

            await asyncio.to_thread(_persist)
            counters["downloaded"] += 1
            logger.info(f"已處理 {illust_id} | {artwork['title'][:40]}")
            if on_success:
                on_success(artwork)

    except Exception as e:
        counters["failed"] += 1
        logger.warning(f"處理作品失敗 {illust_id}: {e}")

    finally:
        _maybe_emit_progress(counters)


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
    """有界並發處理：用 queue + N worker 限制同時在飛的 task 數 = DOWNLOAD_WORKERS。
    舊的 asyncio.gather(*全部tasks) 會讓數千個 coroutine 同時競搶 api_detail_sem，
    造成長達數十分鐘的卡頓，改用此模式後同時活躍的 task 數 ≤ DOWNLOAD_WORKERS。
    """
    if not artworks:
        if status_callback:
            status_callback(counters, total_artworks or 0, False)
        return

    batch_total = total_artworks or len(artworks)

    # 跨來源/跨頁去重：避免同一作品被排入多次造成重複下載
    uniq: list[dict] = []
    seen_ids: set[int] = set()
    for aw in artworks:
        iid = aw.get("illust_id")
        if not iid:
            continue
        iid = int(iid)
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        uniq.append(aw)
    artworks = uniq

    # 不再載入「全部 fully-indexed IDs」到記憶體；改為每批次用 DB 批次查詢
    # 避免長時間運行時快取無限制膨脹，也能保持準確（依 page_count / MAX_GALLERY_PAGES 判斷）
    chunk_size = int(getattr(config, "FULLY_INDEXED_QUERY_CHUNK_SIZE", 800))
    if chunk_size < 50:
        chunk_size = 50
    if chunk_size > 900:
        chunk_size = 900

    filtered: list[dict] = []
    for i in range(0, len(artworks), chunk_size):
        if stop_event.is_set():
            break
        chunk = artworks[i:i + chunk_size]
        requirements = {
            int(aw["illust_id"]): int(aw.get("page_count") or 1)
            for aw in chunk
            if aw.get("illust_id")
        }
        fully_indexed_ids = await asyncio.to_thread(db.get_fully_indexed_artwork_ids, requirements)
        for aw in chunk:
            iid = int(aw["illust_id"])
            if iid in fully_indexed_ids:
                counters["skipped"] += 1
                if on_skip:
                    on_skip(aw)
                _maybe_emit_progress(counters)
            else:
                filtered.append(aw)

    artworks = filtered
    if not artworks:
        if status_callback:
            status_callback(counters, batch_total, False)
        return

    n_workers = config.DOWNLOAD_WORKERS
    q: asyncio.Queue = asyncio.Queue()
    for aw in artworks:
        q.put_nowait(aw)

    async def _worker() -> None:
        while True:
            try:
                aw = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            if stop_event.is_set():
                return
            await _download_artwork_async(
                api, session, aw, sem, stop_event, counters,
                on_success=on_success, api_sem=api_sem, on_skip=on_skip,
            )

    await asyncio.gather(*[_worker() for _ in range(n_workers)])
    if status_callback:
        status_callback(counters, batch_total, False)


#──────────────────────────────────────────────
#User ID 掃描批次
#──────────────────────────────────────────────
async def _scan_user_batch_async(
    scan_api: "AppPixivAPI", dl_headers: dict, stop_event: "threading.Event",
    batch_size: int, on_success: "Callable[[dict], None]", main_api: "AppPixivAPI | None" = None,
) -> int:
    """跨多個 user_id 區段 round-robin 探測。
    每次迭代選下一個「未到達上限」的 segment 推進一個 ID，避免 dead-zone 壟斷掃描預算。
    """
    if not getattr(config, "USER_ID_SCAN_ENABLED", True): return 0

    delay = float(getattr(config, "USER_ID_SCAN_DELAY", 1.5))
    segments = _get_scan_segments()
    cursors = _load_scan_cursors()  # 長度對齊 segments

    scanned_count = 0
    users_done = 0
    seg_rotor = 0   # round-robin 指標

    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)
    api_detail_sem = asyncio.Semaphore(getattr(config, "API_DETAIL_CONCURRENCY", 3))
    api_lock = threading.Lock()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0}
    _proc_api = main_api or scan_api

    def _pick_next_segment() -> int:
        """回傳下一個未耗盡的 segment index；全耗盡時回傳 -1。"""
        nonlocal seg_rotor
        for _ in range(len(segments)):
            idx = seg_rotor % len(segments)
            seg_rotor += 1
            _, end = segments[idx]
            if end is None or cursors[idx] < end:
                return idx
        return -1

    connector = aiohttp.TCPConnector(
        limit=config.DOWNLOAD_WORKERS * 3,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
        ssl=_get_pximg_ssl_context(),
    )
    async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:

        while scanned_count < batch_size and not stop_event.is_set():
            seg_idx = _pick_next_segment()
            if seg_idx < 0:
                logger.info("[user_scan] 所有 segment 皆已耗盡，結束本次批次")
                break

            cursors[seg_idx] += 1
            uid = cursors[seg_idx]
            scanned_count += 1

            # 每 10 次探測落一次 cursor 檔，降低 I/O
            if scanned_count % 10 == 0:
                _save_scan_cursors(cursors)

            # 已在 DB 的 user_id 直接跳過（idx_artworks_user 索引查詢 ~μs）；
            # 避免 Bloom false positive 永久遮蔽真實用戶，scan 用 exact check。
            if await asyncio.to_thread(db.user_exists, uid): continue

            try:
                result = await _to_thread_with_timeout(scan_api.user_detail, uid)
                await asyncio.sleep(delay)
                if not result or "user" not in result: continue
                user_name = result["user"]["name"]
            except Exception as e:
                logger.debug(f"[user_scan] seg{seg_idx} user={uid} 無效: {e}")
                await asyncio.sleep(delay)
                continue

            try:
                artworks = await _to_thread_with_timeout(_fetch_user_artworks_sync, scan_api, uid, api_lock)
            except Exception as e:
                logger.warning(f"[user_scan] seg{seg_idx} user={uid} 作品抓取失敗: {e}")
                artworks = []

            if artworks:
                logger.info(f"[user_scan] seg{seg_idx} user={uid} ({user_name}) → {len(artworks)} 件")
                await _process_batch_async(_proc_api, session, artworks, counters, stop_event, download_sem, on_success=on_success, api_sem=api_detail_sem)

            users_done += 1

    _save_scan_cursors(cursors)

    cur_summary = ", ".join(f"seg{i}={c}" for i, c in enumerate(cursors))
    logger.info(f"[user_scan] 批次結束 | 探測 {scanned_count} 個 ID，有效 {users_done} 位 | {cur_summary}")
    return users_done


# ──────────────────────────────────────────────
# 全站爬取（async 核心）
# ──────────────────────────────────────────────

async def _run_full_crawl_async(
    stop_event: threading.Event,
    api: AppPixivAPI,
    dl_headers: dict,
    visited_users: "BloomFilter | None" = None,
    scan_api: "AppPixivAPI | None" = None,
    scan_dl_headers: "dict | None" = None,
    diffusion_pool: "list[AppPixivAPI] | None" = None,
) -> None:
    diffusion_pool = list(diffusion_pool or [])
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "round": 0}
    download_sem = asyncio.Semaphore(config.DOWNLOAD_WORKERS)
    # illust_detail 專用 semaphore：限制並發 API 呼叫數，避免 rate limit
    api_detail_sem = asyncio.Semaphore(getattr(config, "API_DETAIL_CONCURRENCY", 3))

    # ── 擴散佇列 ──────────────────────────────────────
    # visited_users：已排程爬全作品的 user_id（session 內去重）。
    # 破億筆規模下 set[int] 會吃 GB 級 RAM，改用 Bloom filter：
    # 1% FP 下 1 億筆僅 ~120MB，false positive = 少擴散一個作者（可接受）。
    if visited_users is None:
        visited_users = BloomFilter(
            expected_n=int(getattr(config, "VISITED_USERS_BLOOM_N", 100_000_000)),
            fp_rate=float(getattr(config, "VISITED_USERS_BLOOM_FP", 0.01)),
        )

        def _populate_bloom() -> int:
            total = 0
            for chunk in db.iter_user_id_chunks(100_000):
                visited_users.add_many(chunk)
                total += int(chunk.size)
            return total

        loaded = await asyncio.to_thread(_populate_bloom)
        logger.info(
            f"已從 DB 載入 {loaded} 位作者進 Bloom filter "
            f"({visited_users.bytes_used() / 1024 / 1024:.1f} MB)"
        )
    # related_visited：已取過相關作品的 illust_id
    related_visited: set[int] = set()
    # 擴散佇列（有界，滿了就 drop producer；被 drop 的 uid 下輪可能再被發現）
    user_q_max = int(getattr(config, "DIFFUSION_USER_Q_MAXSIZE", 10_000))
    related_q_max = int(getattr(config, "DIFFUSION_RELATED_Q_MAXSIZE", 20_000))
    user_diff_q: asyncio.Queue[int] = asyncio.Queue(maxsize=user_q_max)
    related_diff_q: asyncio.Queue[int] = asyncio.Queue(maxsize=related_q_max)
    skip_related_budget = {"count": 0}

    def _on_artwork_success(artwork: dict) -> None:
        """新下載的作品：推入作者佇列 + 相關作品佇列（完整擴散）。"""
        uid = artwork.get("user_id")
        if uid and uid not in visited_users:
            visited_users.add(uid)
            try:
                user_diff_q.put_nowait(uid)
            except asyncio.QueueFull:
                pass  # 佇列滿了放棄擴散；該作者可於下輪 tag/scan 被重新發現
        iid = artwork.get("illust_id")
        if iid and iid not in related_visited:
            related_visited.add(iid)
            try:
                related_diff_q.put_nowait(iid)
            except asyncio.QueueFull:
                pass

    def _on_artwork_skip(artwork: dict) -> None:
        """已索引作品採折衷擴散：作者必擴，相關作品採機率+限額擴散。"""
        uid = artwork.get("user_id")
        if uid and uid not in visited_users:
            visited_users.add(uid)
            try:
                user_diff_q.put_nowait(uid)
            except asyncio.QueueFull:
                pass

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
        try:
            related_diff_q.put_nowait(iid)
            skip_related_budget["count"] += 1
        except asyncio.QueueFull:
            pass

    # ── 非 tag 種子（全站最新 / 推薦）────────────────────────
    # 排行榜已移入 tag 輪詢的奇數輪次（tag→user_scan→tag→ranking→user_scan）
    def _non_tag_sources():
        # 傳遞 stop_event 讓種子抓取能在頁間響應停止
        yield _fetch_new_illusts, api, "illust", stop_event
        yield _fetch_new_illusts, api, "manga", stop_event
        yield _fetch_recommended, api, stop_event

    # 載入 tag 爬取進度（爬蟲重啟後從記錄點繼續）
    _load_tag_progress()

    while not stop_event.is_set():
        # 每輪開始時清空相關作品訪問記錄，防止跨輪無限累積（數十萬條目 × 數十 bytes）。
        # 本輪內去重仍有效；跨輪重複 fetch 的 illust 會被 DB 索引快速過濾（no-op）。
        related_visited.clear()
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
            ssl=_get_pximg_ssl_context(),
        )
        dl_session = aiohttp.ClientSession(headers=dl_headers, connector=connector)

        # 背景處理 task 列表：tag/ranking 的作品下載在背景進行，主循環不等待
        _bg_tasks: list[asyncio.Task] = []
        _MAX_BG_TASKS = 3  # 最多同時 3 個背景下載批次，避免記憶體爆炸

        async def _process(artworks: list[dict]) -> None:
            """立即等待（用於 diffusion / priority 等小批次）"""
            if artworks:
                await _process_batch_async(
                    api, dl_session, artworks, counters, stop_event, download_sem,
                    on_success=_on_artwork_success,
                    on_skip=_on_artwork_skip,
                    api_sem=api_detail_sem,
                )

        async def _process_bg(artworks: list[dict], label: str = "") -> None:
            """將大批作品丟入背景 task，主循環立即繼續。
            若背景 task 已達上限，等最舊的一個完成後再繼續。"""
            nonlocal _bg_tasks
            if not artworks:
                return
            # 清掉已完成的
            _bg_tasks = [t for t in _bg_tasks if not t.done()]
            # 若達上限，等最舊的完成
            if len(_bg_tasks) >= _MAX_BG_TASKS:
                logger.info(f"[bg] 背景佇列已滿（{len(_bg_tasks)}），等待最舊批次完成...")
                _log_page_fetch("phase:bg_wait", 0, status="wait",
                                extra={"pending": len(_bg_tasks), "label": label})
                await _bg_tasks[0]
                _bg_tasks = [t for t in _bg_tasks if not t.done()]

            async def _run():
                _log_page_fetch("phase:processing", 0, status="bg_start",
                                extra={"from": label, "artworks": len(artworks)})
                await _process_batch_async(
                    api, dl_session, artworks, counters, stop_event, download_sem,
                    on_success=_on_artwork_success,
                    on_skip=_on_artwork_skip,
                    api_sem=api_detail_sem,
                )
                _log_page_fetch("phase:processing", 0, status="bg_done",
                                extra={"from": label, "artworks": len(artworks)})

            task = asyncio.create_task(_run())
            _bg_tasks.append(task)
            logger.info(f"[bg] 已排程背景處理 {len(artworks)} 件（{label}），目前背景批次={len(_bg_tasks)}）")

        async def _await_bg_tasks() -> None:
            """等待所有背景處理完成（round 結尾前呼叫）"""
            nonlocal _bg_tasks
            pending = [t for t in _bg_tasks if not t.done()]
            if pending:
                logger.info(f"[bg] 等待 {len(pending)} 個背景批次完成...")
                _log_page_fetch("phase:bg_flush", 0, status="wait", extra={"pending": len(pending)})
                await asyncio.gather(*pending, return_exceptions=True)
                _log_page_fetch("phase:bg_flush", 0, status="done")
            _bg_tasks = []

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

        async def _do_user_diffusion(uid: int, worker_api: AppPixivAPI) -> None:
            try:
                aw = await _to_thread_with_timeout(_fetch_user_artworks_sync, worker_api, uid)
                logger.info(f"[擴散-作者] user={uid} → {len(aw)} 件")
            except Exception as e:
                logger.warning(f"[擴散-作者] 失敗 user={uid}: {e}")
                aw = []
            await _process(aw)

        async def _do_related_diffusion(iid: int, worker_api: AppPixivAPI) -> None:
            try:
                aw = await _fetch_related_async(worker_api, iid)
            except Exception as e:
                logger.warning(f"[擴散-相關] 失敗 illust={iid}: {e}")
                aw = []
            # 不下載 related 作品本身，把作者丟進 user_diff_q，擴散係數 ~30→900。
            new_users = 0
            for a in aw:
                uid = a.get("user_id")
                if not uid or uid in visited_users:
                    continue
                visited_users.add(uid)
                try:
                    user_diff_q.put_nowait(uid)
                    new_users += 1
                except asyncio.QueueFull:
                    break
            logger.info(f"[擴散-相關→作者] illust={iid} → {len(aw)} 件，新增 {new_users} 位作者")

        async def _drain_diffusion(
            user_budget: int | None = None,
            related_budget: int | None = None,
        ) -> tuple[int, int]:
            """每次 tick 最多消耗 user_budget 個作者 + related_budget 個相關，
            有 diffusion_pool 時用多 token 並行處理；否則串列跑主 api。"""
            if user_budget is None:
                user_budget = int(getattr(config, "DIFFUSION_USER_QUOTA_PER_TICK", 5))
            if related_budget is None:
                related_budget = int(getattr(config, "DIFFUSION_RELATED_QUOTA_PER_TICK", 5))

            if stop_event.is_set() or get_priority_queue_size() > 0:
                return 0, 0

            # 先抽本 tick 要做的工作
            user_jobs: list[int] = []
            while len(user_jobs) < user_budget:
                try:
                    user_jobs.append(user_diff_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            related_jobs: list[int] = []
            while len(related_jobs) < related_budget:
                try:
                    related_jobs.append(related_diff_q.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if not user_jobs and not related_jobs:
                return 0, 0

            # 工作端 api pool：優先用 diffusion_pool，否則退化為主 api
            workers = diffusion_pool if diffusion_pool else [api]
            tasks: list[asyncio.Task] = []
            for i, uid in enumerate(user_jobs):
                tasks.append(asyncio.create_task(
                    _do_user_diffusion(uid, workers[i % len(workers)])
                ))
            for j, iid in enumerate(related_jobs):
                tasks.append(asyncio.create_task(
                    _do_related_diffusion(iid, workers[(len(user_jobs) + j) % len(workers)])
                ))
            await asyncio.gather(*tasks, return_exceptions=True)
            return len(user_jobs), len(related_jobs)

        async def _priority_watcher() -> None:
            """
            並行背景監視優先佇列。
            asyncio 合作式排程：只要主流程到達任何 await 點，此 task 就能立即被排程。
            """
            while not stop_event.is_set():
                await _drain_priority()
                await asyncio.sleep(2)

        # ── 持久並行 workers ────────────────────────────────────────
        # 主 loop 跑 tag/ranking/seeds (api=pool[0])；
        # 以下三個 task 各自用獨立 token 並行消化工作：
        #   - user_scan_loop: scan_api (pool[1]) 持續順序掃描 user_id
        #   - user_diff_worker: pool[2] 持續消耗 user_diff_q
        #   - related_diff_worker: pool[3] 持續消耗 related_diff_q
        async def _user_scan_loop() -> None:
            if not scan_api:
                return
            while not stop_event.is_set():
                try:
                    await _scan_user_batch_async(
                        scan_api,
                        scan_dl_headers or {},
                        stop_event,
                        user_batch_size,
                        on_success=_on_artwork_success,
                        main_api=api,
                    )
                except Exception as e:
                    logger.warning(f"[user_scan_loop] 批次失敗，2 秒後重試: {e}")
                    await asyncio.sleep(2)
                else:
                    await asyncio.sleep(0)

        async def _user_diff_worker(worker_api: AppPixivAPI) -> None:
            while not stop_event.is_set():
                try:
                    uid = await asyncio.wait_for(user_diff_q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                await _do_user_diffusion(uid, worker_api)

        async def _related_diff_worker(worker_api: AppPixivAPI) -> None:
            while not stop_event.is_set():
                try:
                    iid = await asyncio.wait_for(related_diff_q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                await _do_related_diffusion(iid, worker_api)

        # 啟動優先監視器（整個 round 含 60s 等待期間都有效）
        _watcher_task = asyncio.create_task(_priority_watcher())

        async def _frontier_probe_loop() -> None:
            """持續探測「目前 DB 最大 illust_id 之後的候選 ID」以捕捉剛上架的新作。
            每 tick（預設 5 分鐘）對 cur_max+1 往後逐一 illust_detail；只要一批命中 ≥1
            就繼續探下一批，直到整批全 404（視為已追上 Pixiv 前沿）或達到單次上限。
            命中的作品送進既有下載/索引管線；state 持久化到 FRONTIER_STATE_FILE。"""
            st = _load_frontier_state()
            cur_max = int(st.get("max_id") or 0)
            if cur_max <= 0:
                try:
                    cur_max = await asyncio.to_thread(db.max_illust_id)
                except Exception as e:
                    logger.warning(f"[frontier] 取 DB max illust_id 失敗: {e}")
                    cur_max = 0
            logger.info(f"[frontier] 啟動，起始 max_id={cur_max}")

            batch         = int(getattr(config, "FRONTIER_PROBE_BATCH", 20))
            max_per_tick  = int(getattr(config, "FRONTIER_PROBE_MAX_PER_TICK", 500))
            interval      = int(getattr(config, "FRONTIER_PROBE_INTERVAL", 300))

            async def _sleep_responsive(seconds: int) -> None:
                for _ in range(max(1, seconds)):
                    if stop_event.is_set():
                        return
                    await asyncio.sleep(1)

            while not stop_event.is_set():
                if cur_max <= 0:
                    await _sleep_responsive(interval)
                    st = _load_frontier_state()
                    cur_max = int(st.get("max_id") or 0)
                    if cur_max <= 0:
                        try:
                            cur_max = await asyncio.to_thread(db.max_illust_id)
                        except Exception:
                            cur_max = 0
                    continue

                tick_start_max = cur_max
                probed_total = 0
                found_total = 0
                # 追趕迴圈：整批全 404 才跳出（推測到達 Pixiv 前沿）
                while probed_total < max_per_tick and not stop_event.is_set():
                    found: list[dict] = []
                    highest = cur_max
                    chunk_start = cur_max
                    for offset in range(1, batch + 1):
                        if stop_event.is_set():
                            break
                        cand = chunk_start + offset
                        try:
                            async with api_detail_sem:
                                result = await _to_thread_with_timeout(api.illust_detail, cand)
                        except Exception as e:
                            logger.debug(f"[frontier] probe {cand} 失敗: {e}")
                            await asyncio.sleep(0.3)
                            continue
                        if result and "illust" in result:
                            try:
                                found.append(_parse_illust(result["illust"]))
                                if cand > highest:
                                    highest = cand
                            except Exception as e:
                                logger.debug(f"[frontier] parse {cand} 失敗: {e}")
                        await asyncio.sleep(config.FULL_CRAWL_API_DELAY)
                    probed_total += batch

                    if not found:
                        logger.info(f"[frontier] {chunk_start+1}~{chunk_start+batch} 全數未命中，本 tick 結束")
                        break

                    logger.info(
                        f"[frontier] {chunk_start+1}~{chunk_start+batch} 命中 {len(found)} 件，max_id → {highest}"
                    )
                    try:
                        await _process_bg(found, label=f"frontier:{chunk_start+1}~{chunk_start+batch}")
                    except Exception as e:
                        logger.warning(f"[frontier] 排程背景處理失敗: {e}")
                    cur_max = highest
                    found_total += len(found)
                    _save_frontier_state(cur_max)

                if probed_total >= max_per_tick:
                    logger.info(f"[frontier] 達單 tick 上限 {max_per_tick}，下一 tick 繼續追趕")

                if found_total:
                    logger.info(
                        f"[frontier] tick 完成：{tick_start_max} → {cur_max} "
                        f"（探 {probed_total} 個，命中 {found_total}）"
                    )
                await _sleep_responsive(interval)

        # 分配 diffusion_pool：偶數位給 user_diff，奇數位給 related_diff
        _persistent_tasks: list[asyncio.Task] = []
        _persistent_tasks.append(asyncio.create_task(_frontier_probe_loop(), name="frontier_probe"))
        if scan_api:
            _persistent_tasks.append(asyncio.create_task(_user_scan_loop(), name="user_scan_loop"))
        _user_diff_apis = [a for i, a in enumerate(diffusion_pool) if i % 2 == 0]
        _related_diff_apis = [a for i, a in enumerate(diffusion_pool) if i % 2 == 1]
        for i, worker_api in enumerate(_user_diff_apis):
            _persistent_tasks.append(asyncio.create_task(
                _user_diff_worker(worker_api), name=f"user_diff_{i}",
            ))
        for i, worker_api in enumerate(_related_diff_apis):
            _persistent_tasks.append(asyncio.create_task(
                _related_diff_worker(worker_api), name=f"related_diff_{i}",
            ))
        _has_persistent_diffusion = bool(_user_diff_apis or _related_diff_apis)
        logger.info(
            f"[parallel] 啟動 {len(_persistent_tasks)} 個持久 worker "
            f"(user_scan={'on' if scan_api else 'off'}, "
            f"user_diff={len(_user_diff_apis)}, related_diff={len(_related_diff_apis)})"
        )
        try:
            await _drain_priority()

# ── 建立本輪 tag 輪詢清單 (Carousel) ────────────────────────────────
            # 確保 sort 在外層，tag 在內層，達成先廣度掃描所有 tag 的「最新」
            # 日期切片：每個 (tag, sort) 展開成 N 個 (tag, sort, window) 項目，
            # 突破 pixiv search_illust offset 5000 硬性上限（每窗口各自 5000 件）。
            _date_wins = _date_windows()  # [(sd, ed), ...] 最新窗口在前
            # window_key = "YYYY-MM-DD_YYYY-MM-DD" 或 "" (代表不切片)
            tags_sorts: list[tuple[str, str, str, "str | None", "str | None"]] = [
                (tag, sort, f"{sd}_{ed}" if sd else "", sd, ed)
                for sort in getattr(config, "CRAWL_TAG_SORTS", ["date_desc", "date_asc"])
                for tag in config.ALL_TAGS
                for (sd, ed) in _date_wins
            ]

            # [關鍵修復] 根據全域紀錄旋轉清單，確保重啟後不會從頭開始
            last_tag_key = _get_last_processed_tag()
            if last_tag_key:
                resume_idx = -1
                for i, entry in enumerate(tags_sorts):
                    t, s, w = entry[0], entry[1], entry[2]
                    if _tag_key(t, s, w or None) == last_tag_key:
                        resume_idx = i
                        break

                # 如果找到了上次的斷點，將清單切開並重新拼接
                # 讓「上次跑的那個」排到最後面，它的「下一個」變成清單第 0 個
                if resume_idx != -1:
                    logger.info(f"[輪詢] 偵測到上次斷點 {last_tag_key}，正在對齊清單順序...")
                    tags_sorts = tags_sorts[resume_idx + 1:] + tags_sorts[:resume_idx + 1]

            # ── 過濾與分組 ───────────────────────────────────────────────────
            # 1. 先過濾掉 done: True 的項目 (如你所說，44頁跑完的就跳過)
            active_tags = [
                ts for ts in tags_sorts
                if not _get_tag_progress(ts[0], ts[1], ts[2] or None).get("done", False)
            ]

            # 2. 若過濾後空了，代表全站掃完一輪，觸發重置
            if not active_tags:
                logger.info("[進度] 所有排序下的 Tag 均已跑完，本輪將重置進度開啟新巡迴...")
                with _tag_progress_lock:
                    for k in list(_tag_progress.keys()):
                        if k != "__global__":
                            _tag_progress[k]["done"] = False
                            _tag_progress[k]["page"] = 0
                _save_tag_progress()
                # 重置後，重新套用一次旋轉後的原始清單
                active_tags = [ts for ts in tags_sorts]

            # 3. 根據 TAGS_PER_ROUND 提取本輪要跑的項目 (例如前 2 個)
            tags_to_process = active_tags[:config.TAGS_PER_ROUND]

            # ── tag → user_scan 交替主迴圈 ───────────────────────────────
            # 循環順序：tag(100頁) → user_scan → tag(100頁) → ranking → user_scan → 重複
            for tag_idx, (tag, sort, win_key, start_date, end_date) in enumerate(tags_to_process):
                if stop_event.is_set():
                    break
                await _drain_priority()
                win_opt: "str | None" = win_key or None

                # 讀取此 tag/sort/window 的斷點進度（已過濾 done=True，直接取斷點）
                progress = _get_tag_progress(tag, sort, win_opt)
                last_p = progress.get("page", 0)
                start_page = last_p + 1 if last_p > 0 else 1
                # 日期切片下每窗口上限 ~167 頁（offset 5000），不切片維持舊上限
                if win_opt:
                    win_cap = int(getattr(config, "TAG_DATE_SLICE_MAX_PAGES_PER_WINDOW", 167))
                    window_max_pages = min(max_tag_pages, win_cap)
                else:
                    window_max_pages = max_tag_pages
                resume_q = progress.get("chosen_query")
                resume_s = progress.get("chosen_sort")

                phase_label = "tag→user_scan" if tag_idx % 2 == 0 else "tag→ranking→user_scan"
                total_tags = len(tags_to_process)
                win_label = f" [{win_key}]" if win_key else ""
                logger.info(
                    f"[tag {tag_idx + 1}/{total_tags}] 開始: 「{tag}」{sort}{win_label} "
                    f"從第 {start_page} 頁（最多 {window_max_pages} 頁）【{phase_label}】"
                )

                tag_state: dict = {}
                try:
                    # tag 抓取是多頁長跑任務，每頁 HTTP request 已有自己的 timeout；
                    # 不套外層 asyncio timeout，改靠 stop_event 在頁間中斷。
                    # 同時啟動背景定時任務，每 5 秒觸發 progress hook 讓狀態保持更新。
                    async def _tag_status_pulse():
                        while True:
                            await asyncio.sleep(5)
                            if _progress_hook:
                                _progress_hook(dict(counters))
                    _pulse_task = asyncio.create_task(_tag_status_pulse())
                    try:
                        flush_pages = int(getattr(config, "TAG_FETCH_FLUSH_PAGES", 20))
                        # 邊抓邊下載：每 flush_pages 頁就把那一批丟進背景下載管線，
                        # tag 還在翻頁時下載器已經在處理前面的批次。
                        bg_label = f"tag:{tag}:{sort}" + (f":{win_key}" if win_key else "")
                        async for batch in _fetch_tag_stream(
                            api, tag, sort,
                            start_page, window_max_pages, resume_q, resume_s, stop_event,
                            tag_state, flush_pages=flush_pages,
                            start_date=start_date, end_date=end_date,
                        ):
                            await _process_bg(batch, label=bg_label)
                            # 每批 flush 後即時保存進度，避免中斷丟位置
                            _update_tag_progress(
                                tag, sort,
                                tag_state.get("last_page", 0),
                                False,
                                tag_state.get("effective_query"),
                                tag_state.get("effective_sort"),
                                window=win_opt,
                            )
                            _save_tag_progress()
                    finally:
                        _pulse_task.cancel()
                        await asyncio.gather(_pulse_task, return_exceptions=True)

                    is_done = bool(tag_state.get("is_done", False))
                    last_page = int(tag_state.get("last_page", 0))
                    eff_q = tag_state.get("effective_query")
                    eff_s = tag_state.get("effective_sort")
                    artworks_total = int(tag_state.get("total_artworks", 0))
                    _update_tag_progress(tag, sort, last_page, is_done, eff_q, eff_s, window=win_opt)
                    _save_tag_progress()
                    tag_status = "done" if is_done else "paused"
                    logger.info(
                        f"[tag {tag_idx + 1}/{total_tags}] 完成: 「{tag}」{sort}{win_label} "
                        f"p{start_page}→{last_page} {'✓全部完成' if is_done else '⏸暫停(達上限)'} "
                        f"→ {artworks_total} 件"
                    )
                    _log_page_fetch(
                        f"phase:tag_done", 0,
                        status=tag_status,
                        extra={"tag": tag, "sort": sort, "window": win_key or None,
                               "pages_fetched": last_page - start_page + 1,
                               "artworks": artworks_total,
                               "next": "ranking" if tag_idx % 2 == 1 else "user_scan"},
                    )
                except Exception as e:
                    logger.warning(f"[tag {tag_idx + 1}/{total_tags}] 「{tag}」{sort}{win_label} 失敗: {e}")
                    _log_page_fetch("phase:tag_done", 0, status="error",
                                    extra={"tag": tag, "sort": sort, "window": win_key or None, "error": str(e)})

                # ── 循環順序：tag → [ranking] → user_scan → process ──────────
                # user_scan 移至 process 之前，確保 page_log 立即出現切換記錄，
                # 而不是等數十分鐘的圖片下載完才出現。

                # ── 奇數輪次（1, 3, 5...）：先爬排行榜再 user_scan ──────────
                if tag_idx % 2 == 1 and not stop_event.is_set():
                    today = _today_ymd()
                    state = _load_ranking_state()
                    if state.get("date") != today:
                        state = {
                            "date": today,
                            "done_modes": [],
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        _save_ranking_state(state)

                    done_modes = set(state.get("done_modes") or [])
                    remaining_modes = [m for m in config.ALL_RANKING_MODES if m not in done_modes]
                    if not remaining_modes:
                        logger.info(f"[ranking] 今日({today})已執行過，跳過排行榜任務插入")
                        _log_page_fetch(
                            "phase:ranking_skip",
                            0,
                            status="skip",
                            extra={
                                "reason": "already_ran_today",
                                "date": today,
                                "after_tag": f"{tag}:{sort}",
                                "done_modes": list(done_modes),
                            },
                        )
                    else:
                        n_ranking = len(remaining_modes)
                        logger.info(
                            f"[ranking] ── 開始排行榜爬取（{n_ranking} 種模式 / 每日一次）"
                            f"【tag {tag_idx + 1}/{total_tags} 之後，date={today}】"
                        )
                        _log_page_fetch(
                            "phase:ranking_start",
                            0,
                            status="start",
                            extra={
                                "date": today,
                                "modes": remaining_modes,
                                "after_tag": f"{tag}:{sort}",
                                "done_modes": list(done_modes),
                            },
                        )

                    # ranking 改為 pipeline：抓到一個模式的作品立即丟背景下載，
                        # 下一個模式的 API 請求與目前模式的下載同時進行，不再等到
                        # 所有模式抓完才開始處理。
                    for r_idx, mode in enumerate(remaining_modes, 1):
                        if stop_event.is_set():
                            break
                        await _drain_priority()
                        logger.info(f"[ranking {r_idx}/{n_ranking}] 模式: {mode}")
                        _log_page_fetch("phase:ranking", 0, status="start",
                                        extra={"mode": mode, "idx": r_idx, "total": n_ranking})
                        try:
                            ranking_artworks = await _to_thread_with_timeout(
                                _fetch_ranking, api, mode, stop_event
                            )
                            logger.info(f"[ranking {r_idx}/{n_ranking}] {mode} 完成 → {len(ranking_artworks)} 件")
                        except Exception as e:
                            logger.warning(f"[ranking {r_idx}/{n_ranking}] {mode} 失敗: {e}")
                            ranking_artworks = []
                        _log_page_fetch("phase:ranking", 0, status="done",
                                        extra={"mode": mode, "artworks": len(ranking_artworks)})
                        # 單一模式結果直接送背景處理，不再累積到 artworks
                        if ranking_artworks:
                            await _process_bg(ranking_artworks, label=f"ranking:{mode}")
                        try:
                            if mode not in done_modes:
                                done_modes.add(mode)
                                state["done_modes"] = list(done_modes)
                                state["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                                _save_ranking_state(state)
                        except Exception:
                            pass

                # user_scan 與 diffusion 交給持久 bg task 並行處理；
                # 無 scan_api / 無 diffusion_pool 時退回原地串列跑，保後相容。
                await _drain_priority()
                if not _has_persistent_diffusion:
                    await _drain_diffusion(
                        user_budget=diffusion_user_quota,
                        related_budget=diffusion_related_quota,
                    )
                    await _drain_priority()

            # ── 非 tag 種子（最新上傳 / 推薦）────────────────────
            logger.info("[種子] ── 開始非 tag 種子爬取（最新上傳 / 推薦）")
            for fetch_fn, *args in _non_tag_sources():
                if stop_event.is_set():
                    break
                await _drain_priority()
                # label 顯示使用者可讀的 arg（排除 api / stop_event）
                readable_args = [
                    str(a) for a in args
                    if not isinstance(a, (AppPixivAPI, threading.Event))
                ]
                label = f"{fetch_fn.__name__}({', '.join(readable_args)})"
                logger.info(f"[種子] 開始: {label}")
                try:
                    artworks = await _to_thread_with_timeout(fetch_fn, *args)
                    logger.info(f"[種子] 完成: {label} → {len(artworks)} 件")
                except Exception as e:
                    logger.warning(f"[種子] 來源失敗: {e}")
                    artworks = []
                await _process_bg(artworks, label=label)
                await _drain_priority()
                if not _has_persistent_diffusion:
                    await _drain_diffusion(
                        user_budget=diffusion_user_quota,
                        related_budget=diffusion_related_quota,
                    )

            # ── 尾端擴散（清空積壓的佇列）──────────────────────────────
            # 有持久 worker 時，queue 會被持續消化，這裡只需 drain priority。
            if not _has_persistent_diffusion:
                await _drain_diffusion(
                    user_budget=diffusion_user_quota * diffusion_tail_multiplier,
                    related_budget=diffusion_related_quota * diffusion_tail_multiplier,
                )
            await _drain_priority()
            if not _has_persistent_diffusion:
                await _drain_diffusion(
                    user_budget=diffusion_user_quota * diffusion_tail_multiplier,
                    related_budget=diffusion_related_quota * diffusion_tail_multiplier,
                )

            # ── 等待所有背景下載完成後再統計、進入下一輪 ─────────────
            await _await_bg_tasks()

            try:
                s = db.stats()
                logger.info(
                    f"第 {current_round} 輪結束 | 新增 {counters['downloaded']} | "
                    f"跳過 {counters['skipped']} | 失敗 {counters['failed']} | "
                    f"DB {s['total']} 件 / 已索引 {s['indexed']} 件"
                )
            except Exception as e:
                logger.warning(f"第 {current_round} 輪結束統計失敗: {e}")

            # ── 輪次記憶體清理 ──────────────────────────────────────
            # 1. 合併 _tail_ids / _tail_ids_set → _base_ids，清空 tail，釋放記憶體。
            #    用 asyncio.to_thread 避免阻塞事件迴圈（flush_index 持有 threading.Lock
            #    並執行 np.concatenate / np.sort / np.save）。
            try:
                await asyncio.to_thread(fe.flush_index)
                logger.info(f"[記憶體] 第 {current_round} 輪 FAISS tail 已合併")
            except Exception as e:
                logger.warning(f"[記憶體] flush_index 失敗: {e}")
            try:
                await asyncio.to_thread(fe.flush_nn_index)
            except Exception as e:
                logger.warning(f"[記憶體] flush_nn_index 失敗: {e}")
            # 2. 建議 CPython GC 回收本輪產生的循環垃圾（Task closures / aiohttp 物件等）。
            gc.collect()

            if not stop_event.is_set():
                logger.info("等待 60 秒後開始下一輪...")
                for _ in range(60):
                    if stop_event.is_set():
                        break
                    await asyncio.sleep(1)
        finally:
            _watcher_task.cancel()
            for _t in _persistent_tasks:
                _t.cancel()
            await asyncio.gather(
                _watcher_task, *_persistent_tasks,
                return_exceptions=True,
            )
            await _await_bg_tasks()   # stop 時也等背景完成
            await dl_session.close()

    logger.info("爬取結束，存檔 FAISS 索引...")
    fe.flush_index()
    fe.flush_nn_index()
    s = db.stats()
    logger.info(f"爬取已停止，DB 共 {s['total']} 件，已索引 {s['indexed']} 件")


# ──────────────────────────────────────────────
# 作者 ID 順序掃描
# ──────────────────────────────────────────────

import json as _json

def _get_scan_segments() -> list[tuple[int, "int | None"]]:
    """取得 user_id 掃描分段；缺 config 時退回單段 (0, None) 相容舊行為。"""
    segs = getattr(config, "USER_ID_SCAN_SEGMENTS", None)
    if not segs:
        return [(0, None)]
    return [(int(s), (int(e) if e is not None else None)) for s, e in segs]


def _load_scan_cursors() -> list[int]:
    """載入每段的 cursor。
    - 新格式：{"seg0": N, "seg1": M, ...}
    - 舊格式（單 cursor）：{"cursor": N} → 自動遷移到 seg0，其餘段從 start 開始
    """
    path = config.USER_ID_SCAN_CURSOR_FILE
    segs = _get_scan_segments()
    cursors: list[int] = [s for s, _ in segs]  # 預設為每段起點
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except FileNotFoundError:
        return cursors
    except Exception as e:
        logger.warning(f"[掃描] 讀取 cursor 失敗，用預設起點: {e}")
        return cursors

    # 新格式
    for i in range(len(segs)):
        key = f"seg{i}"
        if key in data:
            try:
                cursors[i] = max(int(data[key]), segs[i][0])
            except Exception:
                pass

    # 舊格式遷移：單 "cursor" 欄位 → 放到 seg0
    if "cursor" in data and "seg0" not in data:
        try:
            cursors[0] = max(int(data["cursor"]), segs[0][0])
            logger.info(f"[掃描] 偵測到舊 cursor 格式，已遷移到 seg0={cursors[0]}")
        except Exception:
            pass

    return cursors


def _save_scan_cursors(cursors: list[int]) -> None:
    path = config.USER_ID_SCAN_CURSOR_FILE
    try:
        data = {f"seg{i}": int(c) for i, c in enumerate(cursors)}
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f)
    except Exception as e:
        logger.warning(f"[掃描] 無法存進度: {e}")


# 舊 API 相容（只回傳 seg0；仍被某些地方呼叫時不破壞行為）
def _load_scan_cursor() -> int:
    return _load_scan_cursors()[0]


def _save_scan_cursor(cursor: int) -> None:
    cursors = _load_scan_cursors()
    cursors[0] = int(cursor)
    _save_scan_cursors(cursors)


async def _user_id_scan_async(
    stop_event: threading.Event,
    api: AppPixivAPI,
    dl_headers: dict,
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
            ssl=_get_pximg_ssl_context(),
        )
        async with aiohttp.ClientSession(headers=dl_headers, connector=connector) as session:
            while not stop_event.is_set():
                uid = await _next_id()
                if await asyncio.to_thread(db.user_exists, uid):
                    await asyncio.sleep(0)  # yield，讓其他 task 執行
                    continue

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
    fe.init_nn_index()
    if fe.get_index_size() == 0:
        logger.info("FAISS 索引為空，從 DB 重建（含多頁）...")
        try:
            fe.build_faiss_index()
        except RuntimeError:
            pass
    if fe.get_nn_index_size() == 0:
        logger.info("NN 索引為空，從 DB 重建...")
        try:
            fe.build_nn_faiss_index()
        except RuntimeError:
            pass

    try:
        pool = _setup_api_pool()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        return

    api = pool[0]
    dl_headers = _get_dl_headers(api)
    # pool 分配：[0]=main tag/ranking，[1]=scan（若有），[2:]=diffusion 並行 workers
    scan_api = pool[1] if len(pool) >= 2 else None
    scan_dl_headers = _get_dl_headers(scan_api) if scan_api else None
    diffusion_pool = pool[2:] if len(pool) >= 3 else []

    async def _main():
        await _run_full_crawl_async(
            stop_event, api, dl_headers,
            scan_api=scan_api,
            scan_dl_headers=scan_dl_headers,
            diffusion_pool=diffusion_pool,
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
        ssl=_get_pximg_ssl_context(),
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
    fe.flush_nn_index()
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
    fe.init_nn_index()
    if fe.get_index_size() == 0:
        logger.info("FAISS 索引為空，從 DB 重建（含多頁）...")
        try:
            fe.build_faiss_index()
        except RuntimeError:
            pass
    if fe.get_nn_index_size() == 0:
        logger.info("NN 索引為空，從 DB 重建...")
        try:
            fe.build_nn_faiss_index()
        except RuntimeError:
            pass

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        raise

    dl_headers = _get_dl_headers(api)
    return asyncio.run(
        _crawl_user_async(user_id, stop_event, api, dl_headers, status_callback)
    )
