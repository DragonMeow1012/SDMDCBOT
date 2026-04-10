"""
Pixiv 鞈?摨急芋蝯?- SQLite ?脣???????孵噩??
嚗??pixiv_x_Spider/database.py嚗蝙??pixiv_config嚗?
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
    """?????澈銵冽"""
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

            CREATE TABLE IF NOT EXISTS GalleryPixiv (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                illust_id     INTEGER NOT NULL REFERENCES artworks(illust_id),
                page_index    INTEGER NOT NULL,
                image_url     TEXT,
                local_path    TEXT,
                color_hist    BLOB,   -- pHash bytes (8 bytes)
                updated_at    TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(illust_id, page_index)
            );

            CREATE INDEX IF NOT EXISTS idx_artworks_bookmarks ON artworks(bookmarks DESC);
            CREATE INDEX IF NOT EXISTS idx_artworks_user ON artworks(user_id);
            CREATE INDEX IF NOT EXISTS idx_gallery_illust ON GalleryPixiv(illust_id);
        """)


def upsert_artwork(data: dict):
    """?啣???唬????豢?"""
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
    """寫入 pHash 特徵（8 bytes uint8）。"""
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


def upsert_gallery_page(
    illust_id: int,
    page_index: int,
    image_url: str | None,
    phash_vec: np.ndarray | None,
    local_path: str | None = None,
) -> None:
    sql = """
        INSERT INTO GalleryPixiv (illust_id, page_index, image_url, local_path, color_hist)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(illust_id, page_index) DO UPDATE SET
            image_url   = excluded.image_url,
            local_path  = excluded.local_path,
            color_hist  = COALESCE(excluded.color_hist, color_hist),
            updated_at  = datetime('now','localtime')
    """
    blob = None if phash_vec is None else phash_vec.astype(np.uint8).tobytes()
    with get_connection() as conn:
        conn.execute(sql, (illust_id, page_index, image_url, local_path, blob))


def get_artwork(illust_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM artworks WHERE illust_id = ?", (illust_id,)
        ).fetchone()


def get_all_features() -> list[tuple[int, np.ndarray]]:
    """????歇?? pHash ?孵噩????? [(illust_id, phash_uint8_array), ...]"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT illust_id, color_hist FROM features WHERE color_hist IS NOT NULL"
        ).fetchall()
    result = []
    for row in rows:
        blob = row["color_hist"]
        if len(blob) != 8:      # 頝喲??? float32 鞈?嚗?84 bytes嚗?
            continue
        arr = np.frombuffer(blob, dtype=np.uint8).copy()
        result.append((row["illust_id"], arr))
    return result


def get_artworks_without_features() -> list[sqlite3.Row]:
    """??撠???孵噩?歇銝?雿?"""
    with get_connection() as conn:
        return conn.execute("""
            SELECT a.* FROM artworks a
            LEFT JOIN features f ON a.illust_id = f.illust_id
            WHERE a.local_path IS NOT NULL
              AND f.illust_id IS NULL
        """).fetchall()


def search_by_ids(illust_ids: list[int]) -> list[sqlite3.Row]:
    """靘?ID ?”?寞活?亥岷雿?"""
    if not illust_ids:
        return []
    placeholders = ",".join("?" * len(illust_ids))
    with get_connection() as conn:
        return conn.execute(
            f"SELECT * FROM artworks WHERE illust_id IN ({placeholders})",
            illust_ids
        ).fetchall()


def stats() -> dict:
    """統計作品與索引進度"""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
        downloaded = conn.execute(
            "SELECT COUNT(*) FROM features"
        ).fetchone()[0]
        gallery_pages = conn.execute(
            "SELECT COUNT(*) FROM GalleryPixiv WHERE color_hist IS NOT NULL"
        ).fetchone()[0]
        indexed = downloaded
    return {
        "total": total,
        "downloaded": downloaded,
        "indexed": indexed,
        "gallery_pages": gallery_pages,
    }

def is_artwork_fully_indexed(illust_id: int, page_count: int) -> bool:
    with get_connection() as conn:
        has_feature = conn.execute(
            "SELECT 1 FROM features WHERE illust_id = ?",
            (illust_id,),
        ).fetchone() is not None
        if not has_feature:
            return False

        effective_pages = min(page_count, config.MAX_GALLERY_PAGES)
        if effective_pages <= 1:
            return True

        gallery_count = conn.execute(
            "SELECT COUNT(*) FROM GalleryPixiv WHERE illust_id = ?",
            (illust_id,),
        ).fetchone()[0]
    return gallery_count >= effective_pages


def get_fully_indexed_artwork_ids(page_requirements: dict[int, int]) -> set[int]:
    """批次查詢哪些作品已完整建立特徵與多頁索引。"""
    if not page_requirements:
        return set()

    illust_ids = list(page_requirements.keys())
    placeholders = ",".join("?" for _ in illust_ids)

    with get_connection() as conn:
        feature_rows = conn.execute(
            f"SELECT illust_id FROM features WHERE illust_id IN ({placeholders})",
            illust_ids,
        ).fetchall()
        feature_ids = {row["illust_id"] for row in feature_rows}

        multi_page_ids = [
            illust_id
            for illust_id, page_count in page_requirements.items()
            if min(page_count, config.MAX_GALLERY_PAGES) > 1 and illust_id in feature_ids
        ]

        gallery_counts: dict[int, int] = {}
        if multi_page_ids:
            gallery_placeholders = ",".join("?" for _ in multi_page_ids)
            gallery_rows = conn.execute(
                f"""
                SELECT illust_id, COUNT(*) AS page_count
                FROM GalleryPixiv
                WHERE illust_id IN ({gallery_placeholders})
                GROUP BY illust_id
                """,
                multi_page_ids,
            ).fetchall()
            gallery_counts = {
                row["illust_id"]: row["page_count"]
                for row in gallery_rows
            }

    fully_indexed: set[int] = set()
    for illust_id, page_count in page_requirements.items():
        if illust_id not in feature_ids:
            continue
        effective_pages = min(page_count, config.MAX_GALLERY_PAGES)
        if effective_pages <= 1:
            fully_indexed.add(illust_id)
            continue
        if gallery_counts.get(illust_id, 0) >= effective_pages:
            fully_indexed.add(illust_id)
    return fully_indexed


def get_all_user_ids() -> set[int]:
    """載入資料庫中所有已知的 user_id。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT user_id FROM artworks WHERE user_id IS NOT NULL").fetchall()
    return {row["user_id"] for row in rows}


def get_all_fully_indexed_artwork_ids() -> set[int]:
    """載入目前資料庫中已完整建立特徵與多頁索引的作品 ID。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                a.illust_id,
                a.page_count,
                COUNT(g.id) AS gallery_count
            FROM artworks a
            JOIN features f ON f.illust_id = a.illust_id
            LEFT JOIN GalleryPixiv g ON g.illust_id = a.illust_id
            GROUP BY a.illust_id, a.page_count
            """
        ).fetchall()

    fully_indexed: set[int] = set()
    for row in rows:
        effective_pages = min(row["page_count"] or 1, config.MAX_GALLERY_PAGES)
        if effective_pages <= 1 or row["gallery_count"] >= effective_pages:
            fully_indexed.add(row["illust_id"])
    return fully_indexed

