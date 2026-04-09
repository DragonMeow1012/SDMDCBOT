"""
Pixiv 資料庫模組 - SQLite 儲存圖片元數據與特徵向量
（改自 pixiv_x_Spider/database.py，使用 pixiv_config）
"""
import sqlite3
import json
import numpy as np
from pathlib import Path
from typing import Optional
import pixiv_config as config


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化資料庫表格"""
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS artworks (
                illust_id     INTEGER PRIMARY KEY,
                title         TEXT NOT NULL,
                user_id       INTEGER,
                user_name     TEXT,
                tags          TEXT,       -- JSON array
                bookmarks     INTEGER DEFAULT 0,
                views         INTEGER DEFAULT 0,
                width         INTEGER,
                height        INTEGER,
                page_count    INTEGER DEFAULT 1,
                image_url     TEXT,
                local_path    TEXT,
                created_at    TEXT,
                fetched_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS features (
                illust_id     INTEGER PRIMARY KEY REFERENCES artworks(illust_id),
                color_hist    BLOB,   -- numpy float32 array (96 dims)
                dominant_colors BLOB, -- JSON [[r,g,b], ...]
                updated_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_artworks_bookmarks ON artworks(bookmarks DESC);
            CREATE INDEX IF NOT EXISTS idx_artworks_user ON artworks(user_id);
        """)


def upsert_artwork(data: dict):
    """新增或更新作品元數據"""
    sql = """
        INSERT INTO artworks
            (illust_id, title, user_id, user_name, tags, bookmarks, views,
             width, height, page_count, image_url, local_path, created_at)
        VALUES
            (:illust_id, :title, :user_id, :user_name, :tags, :bookmarks, :views,
             :width, :height, :page_count, :image_url, :local_path, :created_at)
        ON CONFLICT(illust_id) DO UPDATE SET
            bookmarks  = excluded.bookmarks,
            views      = excluded.views,
            local_path = COALESCE(excluded.local_path, local_path),
            fetched_at = datetime('now','localtime')
    """
    with get_connection() as conn:
        conn.execute(sql, data)


def upsert_features(illust_id: int, phash_vec: np.ndarray, dominant_colors: list = []):
    """儲存 pHash 特徵向量（8 bytes uint8）"""
    sql = """
        INSERT INTO features (illust_id, color_hist, dominant_colors)
        VALUES (?, ?, ?)
        ON CONFLICT(illust_id) DO UPDATE SET
            color_hist      = excluded.color_hist,
            dominant_colors = excluded.dominant_colors,
            updated_at      = datetime('now','localtime')
    """
    with get_connection() as conn:
        conn.execute(sql, (
            illust_id,
            phash_vec.astype(np.uint8).tobytes(),
            json.dumps(dominant_colors)
        ))


def get_artwork(illust_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM artworks WHERE illust_id = ?", (illust_id,)
        ).fetchone()


def get_all_features() -> list[tuple[int, np.ndarray]]:
    """取得所有已提取 pHash 特徵的作品，回傳 [(illust_id, phash_uint8_array), ...]"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT illust_id, color_hist FROM features WHERE color_hist IS NOT NULL"
        ).fetchall()
    result = []
    for row in rows:
        blob = row["color_hist"]
        if len(blob) != 8:      # 跳過舊版 float32 資料（384 bytes）
            continue
        arr = np.frombuffer(blob, dtype=np.uint8).copy()
        result.append((row["illust_id"], arr))
    return result


def get_artworks_without_features() -> list[sqlite3.Row]:
    """取得尚未提取特徵的已下載作品"""
    with get_connection() as conn:
        return conn.execute("""
            SELECT a.* FROM artworks a
            LEFT JOIN features f ON a.illust_id = f.illust_id
            WHERE a.local_path IS NOT NULL
              AND f.illust_id IS NULL
        """).fetchall()


def search_by_ids(illust_ids: list[int]) -> list[sqlite3.Row]:
    """依 ID 列表批次查詢作品"""
    if not illust_ids:
        return []
    placeholders = ",".join("?" * len(illust_ids))
    with get_connection() as conn:
        return conn.execute(
            f"SELECT * FROM artworks WHERE illust_id IN ({placeholders})",
            illust_ids
        ).fetchall()


def stats() -> dict:
    """回傳資料庫統計"""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
        downloaded = conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE local_path IS NOT NULL"
        ).fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    return {"total": total, "downloaded": downloaded, "indexed": indexed}
