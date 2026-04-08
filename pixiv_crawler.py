"""
Pixiv 爬蟲模組（整合 crawler + full_crawl）
- 使用 PixivPy 爬取圖片元數據並下載
- run_full_crawl(stop_event) 在背景執行緒持續爬取，直到 stop_event 被設置
（改自 pixiv_x_Spider/crawler.py + full_crawl.py，使用 pixiv_config / pixiv_database / pixiv_feature）
"""
import json
import time
import logging
import threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import io
from typing import Callable

from pixivpy3 import AppPixivAPI

import pixiv_config as config
import pixiv_database as db
import pixiv_feature as fe

logger = logging.getLogger(__name__)

_PIXIV_HEADERS = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
}


# ──────────────────────────────────────────────
# API 初始化
# ──────────────────────────────────────────────

def _setup_api() -> AppPixivAPI:
    """初始化並驗證 PixivPy API"""
    api = AppPixivAPI()
    if config.PROXY:
        api.set_additional_headers({"Proxy": config.PROXY})
        logger.info(f"使用代理: {config.PROXY}")
    api.auth(refresh_token=config.PIXIV_REFRESH_TOKEN)
    logger.info("Pixiv 驗證成功")
    return api


def _build_session(api: AppPixivAPI) -> requests.Session:
    """建立帶有 Pixiv 認證 headers 的 session"""
    session = requests.Session()
    session.headers.update(api.requests.headers)
    session.headers.update(_PIXIV_HEADERS)
    return session


# ──────────────────────────────────────────────
# 元數據解析
# ──────────────────────────────────────────────

def _parse_illust(illust: dict) -> dict:
    """將 API 回傳的作品物件轉換為資料庫格式"""
    tags = [t["name"] for t in illust.get("tags", [])]

    meta_pages = illust.get("meta_pages", [])
    if meta_pages:
        image_url = meta_pages[0]["image_urls"].get("original") or \
                    meta_pages[0]["image_urls"].get("large")
    else:
        urls = illust.get("meta_single_page", {})
        image_url = urls.get("original_image_url") or \
                    illust.get("image_urls", {}).get("large")

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
# 圖片下載
# ──────────────────────────────────────────────

def _download_image(session: requests.Session, artwork: dict) -> bool:
    """下載單張圖片到記憶體，提取特徵存入 DB，回傳是否成功"""
    illust_id = artwork["illust_id"]
    url = artwork["image_url"]
    if not url:
        return False

    try:
        resp = session.get(url, headers=_PIXIV_HEADERS, timeout=30)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.thumbnail(config.MAX_IMAGE_SIZE, Image.LANCZOS)

        color_hist = fe.extract_color_histogram(img)
        dominant = fe.extract_dominant_colors(img)
        db.upsert_features(illust_id, color_hist, dominant)
        fe.add_to_index(illust_id, color_hist)  # 立即加入記憶體索引

        logger.info(f"已處理 {illust_id} | {artwork['title'][:40]}")
        return True
    except Exception as e:
        logger.warning(f"處理失敗 {illust_id}: {e}")
        return False


# ──────────────────────────────────────────────
# 全站爬取輔助函式
# ──────────────────────────────────────────────

def _already_fetched(illust_id: int) -> bool:
    """檢查是否已有特徵（代表已處理過）"""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM features WHERE illust_id = ?", (illust_id,)
        ).fetchone()
    return row is not None


def _process_batch(session: requests.Session, artworks: list[dict],
                   counters: dict, stop_event: threading.Event,
                   status_callback: Callable[[dict, int, bool], None] | None = None,
                   total_artworks: int | None = None):
    """處理並提取特徵，更新計數器；stop_event 被設置時提前返回"""
    processed = 0
    for aw in artworks:
        if stop_event.is_set():
            break
        processed += 1
        if _already_fetched(aw["illust_id"]):
            counters["skipped"] += 1
        else:
            db.upsert_artwork(aw)
            success = _download_image(session, aw)
            if success:
                counters["downloaded"] += 1
            else:
                counters["failed"] += 1

        if status_callback and processed % 5 == 0:
            status_callback(counters, total_artworks or len(artworks), False)

        time.sleep(0.1)

    if status_callback:
        status_callback(counters, total_artworks or len(artworks), False)


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
            offset=offset
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


# ──────────────────────────────────────────────
# 主要入口：背景持續爬取
# ──────────────────────────────────────────────

def run_full_crawl(stop_event: threading.Event):
    """
    在背景執行緒持續爬取全站圖片。
    當 stop_event 被設置時，完成當前批次後優雅停止。
    """
    db.init_db()
    Path(config.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    fe.init_live_index()

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        return

    session = _build_session(api)
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "round": 0}
    limit = config.FULL_CRAWL_PER_SOURCE

    logger.info(f"開始全站爬取，每來源上限 {limit} 張")

    while not stop_event.is_set():
        counters["round"] += 1
        current_round = counters["round"]
        logger.info(f"===== 第 {current_round} 輪開始 =====")

        for mode in config.ALL_RANKING_MODES:
            if stop_event.is_set():
                break
            logger.info(f"[排行榜] mode={mode}")
            try:
                artworks = _fetch_ranking(api, mode, limit)
                _process_batch(session, artworks, counters, stop_event)
            except Exception as e:
                logger.warning(f"排行榜 {mode} 失敗: {e}")
            time.sleep(config.FULL_CRAWL_API_DELAY)

        for tag in config.ALL_TAGS:
            if stop_event.is_set():
                break
            logger.info(f"[標籤] {tag}")
            try:
                artworks = _fetch_tag(api, tag, limit)
                _process_batch(session, artworks, counters, stop_event)
            except Exception as e:
                logger.warning(f"標籤 [{tag}] 失敗: {e}")
            time.sleep(config.FULL_CRAWL_API_DELAY)

        if not stop_event.is_set():
            logger.info("[推薦] 探索推薦圖片")
            try:
                artworks = _fetch_recommended(api, limit)
                _process_batch(session, artworks, counters, stop_event)
            except Exception as e:
                logger.warning(f"推薦爬取失敗: {e}")

        s = db.stats()
        logger.info(
            f"第 {current_round} 輪結束 | 新增 {counters['downloaded']} 張 | "
            f"跳過 {counters['skipped']} | 失敗 {counters['failed']} | "
            f"資料庫共 {s['total']} 件 / 已索引 {s['indexed']} 件"
        )

        if not stop_event.is_set():
            logger.info("等待 60 秒後開始下一輪...")
            stop_event.wait(60)

    logger.info("爬取結束，存檔 FAISS 索引...")
    fe.flush_index()
    s = db.stats()
    logger.info(f"爬取已停止，資料庫共 {s['total']} 件，已索引 {s['indexed']} 件")


# ──────────────────────────────────────────────
# 作者爬取入口
# ──────────────────────────────────────────────

def get_user_id_from_artwork(artwork_id: int) -> tuple[int, str]:
    """
    查詢作品 ID 對應的作者 user_id 與 user_name。
    回傳 (user_id, user_name)。
    """
    api = _setup_api()
    result = api.illust_detail(artwork_id)
    if not result or "illust" not in result:
        raise ValueError(f"找不到作品 {artwork_id}")
    illust = result["illust"]
    return illust["user"]["id"], illust["user"]["name"]


def get_user_name(user_id: int) -> str:
    """
    查詢作者 user_id 對應的作者名稱。
    """
    api = _setup_api()
    result = api.user_detail(user_id)
    if not result or "user" not in result:
        return str(user_id)
    return result["user"]["name"]


def crawl_user_by_id(user_id: int, stop_event: threading.Event,
                     status_callback: Callable[[dict, int], None] | None = None) -> tuple[str, int, int]:
    """
    爬取指定作者的所有作品並下載入資料庫。
    回傳 (user_name, 下載數, 作品總數)。
    當 stop_event 被設置時提前中止。
    """
    db.init_db()
    Path(config.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    fe.init_live_index()  # 載入現有索引到記憶體

    try:
        api = _setup_api()
    except Exception as e:
        logger.error(f"Pixiv 驗證失敗，爬取中止: {e}")
        raise

    session = _build_session(api)

    # 取得作者資訊
    try:
        user_detail = api.user_detail(user_id)
        user_name = user_detail["user"]["name"] if user_detail else str(user_id)
        logger.info(f"確認作者頁面: https://www.pixiv.net/users/{user_id} ({user_name})")
    except Exception as e:
        logger.warning(f"無法確認作者 {user_id}: {e}")
        user_name = str(user_id)

    logger.info(f"開始爬取作者: {user_name} (ID:{user_id})")

    # 取得所有作品元數據
    artworks, offset = [], 0
    while not stop_event.is_set():
        result = api.user_illusts(user_id, type="illust", offset=offset)
        if not result or "illusts" not in result:
            break
        for illust in result["illusts"]:
            if illust["type"] in ("illust", "manga"):
                artworks.append(_parse_illust(illust))
        if not result.get("next_url"):
            break
        offset += 30
        time.sleep(config.FULL_CRAWL_API_DELAY)

    logger.info(f"作者 [{user_name}] 共取得 {len(artworks)} 件作品，開始下載")

    counters = {"downloaded": 0, "skipped": 0, "failed": 0}
    _process_batch(
        session,
        artworks,
        counters,
        stop_event,
        status_callback=status_callback,
        total_artworks=len(artworks),
    )

    if status_callback:
        status_callback(counters, len(artworks), True)

    logger.info(
        f"作者 [{user_name}] 爬取完成 | "
        f"新增: {counters['downloaded']} | 跳過: {counters['skipped']} | 失敗: {counters['failed']}"
    )

    fe.flush_index()
    return user_name, counters["downloaded"], len(artworks)
