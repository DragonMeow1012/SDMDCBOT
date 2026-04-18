"""
Pixiv 資料庫模組 - SQLite 封裝，路徑由 pixiv_config 決定。

Schema v3（2026-04-18 重整）：
  - artworks        基本元資料
  - features        page 0 pHash 備份
  - GalleryPixiv    每頁 (image_url, color_hist 8B, nn_hash 64B)
  - tags            正規化 tag 字典
  - artwork_tags    作品 ↔ tag 關聯

nn_hash = SSCD binary embedding（抗裁剪/修圖/翻譯）。
init_db 會自動 ALTER 舊 DB 補 nn_hash 欄。
"""
import sqlite3
import threading
import numpy as np
from pathlib import Path
from typing import Iterable, Optional
import pixiv_config as config

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """取得 thread-local 的 SQLite 連線（避免每次呼叫都新建連線）。"""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    """建立/升級 schema。對舊 DB 會 ADD COLUMN 補齊，不會 drop 舊欄位。"""
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS artworks (
                illust_id     INTEGER PRIMARY KEY,
                title         TEXT NOT NULL,
                user_id       INTEGER,
                user_name     TEXT,
                bookmarks     INTEGER DEFAULT 0,
                views         INTEGER DEFAULT 0,
                width         INTEGER,
                height        INTEGER,
                page_count    INTEGER DEFAULT 1,
                image_url     TEXT,
                created_at    TEXT,
                fetched_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS features (
                illust_id     INTEGER PRIMARY KEY REFERENCES artworks(illust_id),
                color_hist    BLOB,
                dominant_colors BLOB,
                updated_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS GalleryPixiv (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                illust_id     INTEGER NOT NULL REFERENCES artworks(illust_id),
                page_index    INTEGER NOT NULL,
                image_url     TEXT,
                color_hist    BLOB,   -- pHash 8 bytes
                nn_hash       BLOB,   -- SSCD binary hash 64 bytes (512-bit)
                updated_at    TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(illust_id, page_index)
            );

            CREATE TABLE IF NOT EXISTS tags (
                tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name   TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS artwork_tags (
                illust_id INTEGER NOT NULL REFERENCES artworks(illust_id),
                tag_id    INTEGER NOT NULL REFERENCES tags(tag_id),
                PRIMARY KEY (illust_id, tag_id)
            );

            CREATE INDEX IF NOT EXISTS idx_artworks_bookmarks ON artworks(bookmarks DESC);
            CREATE INDEX IF NOT EXISTS idx_artworks_user      ON artworks(user_id);
            CREATE INDEX IF NOT EXISTS idx_artwork_tags_tag   ON artwork_tags(tag_id);
        """)

        # 舊 DB 補欄位（救生圈）
        gallery_cols = _columns(conn, "GalleryPixiv")
        if "nn_hash" not in gallery_cols:
            conn.execute("ALTER TABLE GalleryPixiv ADD COLUMN nn_hash BLOB")


def upsert_artwork(data: dict) -> None:
    """寫入/更新作品元資料。tags 另外用 replace_artwork_tags() 寫。"""
    sql = """
        INSERT INTO artworks
            (illust_id, title, user_id, user_name, bookmarks, views,
             width, height, page_count, image_url, created_at)
        VALUES
            (:illust_id, :title, :user_id, :user_name, :bookmarks, :views,
             :width, :height, :page_count, :image_url, :created_at)
        ON CONFLICT(illust_id) DO UPDATE SET
            bookmarks  = excluded.bookmarks,
            views      = excluded.views,
            fetched_at = datetime('now','localtime')
    """
    with get_connection() as conn:
        conn.execute(sql, data)


def replace_artwork_tags(illust_id: int, tag_names: Iterable[str]) -> None:
    """將作品的 tag 關聯換成新的一組（先清再寫，冪等）。"""
    names = [n for n in (tag_names or []) if isinstance(n, str) and n]
    with get_connection() as conn:
        conn.execute("DELETE FROM artwork_tags WHERE illust_id = ?", (illust_id,))
        if not names:
            return
        # 先確保所有 tag 在 tags 表
        conn.executemany("INSERT OR IGNORE INTO tags(name) VALUES (?)", [(n,) for n in names])
        placeholders = ",".join("?" * len(names))
        rows = conn.execute(
            f"SELECT tag_id FROM tags WHERE name IN ({placeholders})", names
        ).fetchall()
        tag_ids = [r["tag_id"] for r in rows]
        conn.executemany(
            "INSERT OR IGNORE INTO artwork_tags(illust_id, tag_id) VALUES (?, ?)",
            [(illust_id, tid) for tid in tag_ids],
        )


def upsert_features(illust_id: int, phash_vec: np.ndarray) -> None:
    """寫入 page 0 pHash（8 bytes）備份。Wave 3 會連同 features 表一起廢除。"""
    sql = """
        INSERT INTO features (illust_id, color_hist, dominant_colors)
        VALUES (?, ?, NULL)
        ON CONFLICT(illust_id) DO UPDATE SET
            color_hist = excluded.color_hist,
            updated_at = datetime('now','localtime')
    """
    with get_connection() as conn:
        conn.execute(sql, (illust_id, phash_vec.astype(np.uint8).tobytes()))


def upsert_gallery_page(
    illust_id: int,
    page_index: int,
    image_url: str | None,
    phash_vec: np.ndarray | None,
    nn_hash: bytes | None = None,
) -> None:
    """寫入一頁 (image_url, pHash 8B, nn_hash 64B)。任一為 None 皆不覆蓋舊值。"""
    sql = """
        INSERT INTO GalleryPixiv (illust_id, page_index, image_url, color_hist, nn_hash)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(illust_id, page_index) DO UPDATE SET
            image_url   = excluded.image_url,
            color_hist  = COALESCE(excluded.color_hist, color_hist),
            nn_hash     = COALESCE(excluded.nn_hash,    nn_hash),
            updated_at  = datetime('now','localtime')
    """
    phash_blob = None if phash_vec is None else phash_vec.astype(np.uint8).tobytes()
    with get_connection() as conn:
        conn.execute(sql, (illust_id, page_index, image_url, phash_blob, nn_hash))


def get_artwork(illust_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM artworks WHERE illust_id = ?", (illust_id,)
        ).fetchone()


def search_by_ids(illust_ids: list[int]) -> list[sqlite3.Row]:
    """依 ID 列表查詢作品。"""
    if not illust_ids:
        return []
    placeholders = ",".join("?" * len(illust_ids))
    with get_connection() as conn:
        return conn.execute(
            f"SELECT * FROM artworks WHERE illust_id IN ({placeholders})",
            illust_ids,
        ).fetchall()


def stats() -> dict:
    """統計作品數、下載數、gallery 頁數、nn_hash 頁數。"""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM artworks) AS total,
                (SELECT COUNT(*) FROM features) AS downloaded,
                (SELECT COUNT(*) FROM GalleryPixiv WHERE color_hist IS NOT NULL) AS gallery_pages,
                (SELECT COUNT(*) FROM GalleryPixiv WHERE nn_hash    IS NOT NULL) AS nn_pages
        """).fetchone()
    return {
        "total": row[0],
        "downloaded": row[1],
        "indexed": row[1],
        "gallery_pages": row[2],
        "nn_pages": row[3],
    }


def is_artwork_fully_indexed(illust_id: int, page_count: int) -> bool:
    with get_connection() as conn:
        has_feature = conn.execute(
            "SELECT 1 FROM features WHERE illust_id = ?", (illust_id,),
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


def iter_user_id_chunks(chunk_size: int = 100_000):
    """串流 DISTINCT user_id，用於 Bloom filter 批次初始化。"""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT DISTINCT user_id FROM artworks WHERE user_id IS NOT NULL"
        )
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            yield np.fromiter((r["user_id"] for r in rows), dtype=np.int64, count=len(rows))


def user_exists(user_id: int) -> bool:
    """用 idx_artworks_user 快速判斷 user_id 是否已在 artworks 表。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM artworks WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()
    return row is not None


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
