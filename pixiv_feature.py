"""
Pixiv 特徵提取模組
- pHash（感知哈希，64 bits）
- 批次建立 FAISS 二值索引（Hamming 距離）
- 索引 ID 編碼：illust_id * _ID_MULTIPLIER + page_index
"""
import logging
import numpy as np
import faiss
from PIL import Image
from pathlib import Path

import pixiv_config as config
import pixiv_database as db

logger = logging.getLogger(__name__)

# illust_id 最大約 2×10^8，page_index ≤ MAX_GALLERY_PAGES=20
# 用 10000 作乘數保留足夠空間，整體 < int64 最大值
_ID_MULTIPLIER = 10000


def encode_id(illust_id: int, page_index: int) -> int:
    return illust_id * _ID_MULTIPLIER + page_index


def decode_id(encoded: int) -> tuple[int, int]:
    """回傳 (illust_id, page_index)"""
    return encoded // _ID_MULTIPLIER, encoded % _ID_MULTIPLIER


# ──────────────────────────────────────────────
# 特徵提取
# ──────────────────────────────────────────────

def extract_phash(img: Image.Image) -> np.ndarray:
    """
    計算圖片的 pHash（感知哈希），回傳 uint8 陣列（8 bytes = 64 bits）。
    使用 imagehash.phash，基於 8x8 DCT。
    """
    import imagehash
    img_gray = img.convert("L")
    h = imagehash.phash(img_gray, hash_size=8)
    bits = h.hash.flatten()          # 64 bool values
    return np.packbits(bits)         # 8 uint8 values


def process_image(local_path: str) -> tuple[np.ndarray, list] | None:
    """
    對單張圖片提取 pHash，回傳 (phash_vec, []) 或 None。
    """
    try:
        img = Image.open(local_path).convert("RGB")
        phash_vec = extract_phash(img)
        return phash_vec, []
    except Exception as e:
        logger.warning(f"特徵提取失敗 {local_path}: {e}")
        return None


# ──────────────────────────────────────────────
# FAISS 二值索引管理
# ──────────────────────────────────────────────

import threading as _threading

_index_lock = _threading.Lock()
_live_index: faiss.IndexBinary | None = None
_live_ids: list[int] = []        # 編碼後的 ID：encode_id(illust_id, page_index)
_live_ids_set: set[int] = set()  # 用於 O(1) 去重
_save_counter = 0
_SAVE_EVERY = 10


def _is_old_format(ids: list[int]) -> bool:
    """判斷是否為舊格式（plain illust_id，未含 page_index 編碼）"""
    if not ids:
        return False
    # 舊格式 illust_id 約在 10^7~2×10^8，編碼後最小值 = 10^7 * 10000 = 10^11
    return max(ids) < 10 ** 9


def _save_index():
    """將記憶體索引寫入磁碟（呼叫前需持有 _index_lock）"""
    if _live_index is None or _live_index.ntotal == 0:
        return
    faiss.write_index_binary(_live_index, config.FAISS_INDEX_PATH)
    np.save(config.FAISS_INDEX_PATH + ".ids.npy",
            np.array(_live_ids, dtype=np.int64))


def init_live_index():
    """
    啟動時呼叫：從磁碟載入現有索引到記憶體。
    若格式為舊版（plain illust_id）則清空並等待 build_faiss_index 重建。
    """
    global _live_index, _live_ids, _live_ids_set
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    with _index_lock:
        if Path(idx_path).exists() and Path(ids_path).exists():
            try:
                loaded_index = faiss.read_index_binary(idx_path)
                loaded_ids = np.load(ids_path).tolist()
                if _is_old_format(loaded_ids):
                    logger.warning("FAISS 索引為舊格式（未含頁索引編碼），清空等待重建")
                    _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
                    _live_ids = []
                    _live_ids_set = set()
                else:
                    _live_index = loaded_index
                    _live_ids = loaded_ids
                    _live_ids_set = set(loaded_ids)
                    logger.info(f"載入現有 FAISS 二值索引: {_live_index.ntotal} 筆")
            except Exception:
                logger.warning("FAISS 索引格式不符，建立新二值索引")
                _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
                _live_ids = []
                _live_ids_set = set()
        else:
            _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
            _live_ids = []
            _live_ids_set = set()
            logger.info("建立新 FAISS 二值索引（空）")


def get_index_size() -> int:
    with _index_lock:
        return _live_index.ntotal if _live_index else 0


def add_to_index(illust_id: int, page_index: int, phash_vec: np.ndarray):
    """
    將單頁 pHash 加入記憶體索引，每 _SAVE_EVERY 筆存檔一次。
    線程安全。
    """
    global _live_index, _live_ids, _live_ids_set, _save_counter
    encoded = encode_id(illust_id, page_index)
    vec = phash_vec.astype(np.uint8).reshape(1, -1)
    with _index_lock:
        if _live_index is None:
            _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
            _live_ids = []
            _live_ids_set = set()
        if encoded in _live_ids_set:
            return
        _live_index.add(vec)
        _live_ids.append(encoded)
        _live_ids_set.add(encoded)
        _save_counter += 1
        if _save_counter % _SAVE_EVERY == 0:
            _save_index()
            logger.debug(f"FAISS 索引已存檔: {_live_index.ntotal} 筆")


def flush_index():
    """強制將記憶體索引存檔（爬取結束時呼叫）"""
    with _index_lock:
        _save_index()
    if _live_index:
        logger.info(f"FAISS 索引最終存檔完成: {_live_index.ntotal} 筆")


def build_faiss_index() -> tuple[faiss.IndexBinary, list[int]]:
    """
    從資料庫完整重建 FAISS 二值索引（覆蓋記憶體與磁碟）。
    優先使用 GalleryPixiv（含所有頁），不在其中的作品則取 features 作為第 0 頁。
    """
    global _live_index, _live_ids, _live_ids_set

    entries: list[tuple[int, np.ndarray]] = []  # (encoded_id, phash)

    # 1. GalleryPixiv：所有頁的 pHash
    with db.get_connection() as conn:
        gallery_rows = conn.execute(
            "SELECT illust_id, page_index, color_hist FROM GalleryPixiv "
            "WHERE color_hist IS NOT NULL"
        ).fetchall()

    gallery_illust_ids: set[int] = set()
    for row in gallery_rows:
        blob = row["color_hist"]
        if len(blob) != 8:
            continue
        arr = np.frombuffer(blob, dtype=np.uint8).copy()
        entries.append((encode_id(row["illust_id"], row["page_index"]), arr))
        gallery_illust_ids.add(row["illust_id"])

    # 2. features：補上沒有 GalleryPixiv 的作品（第 0 頁）
    all_features = db.get_all_features()
    for illust_id, feat in all_features:
        if illust_id in gallery_illust_ids:
            continue
        if len(feat) != 8:
            continue
        entries.append((encode_id(illust_id, 0), feat))

    if not entries:
        raise RuntimeError("資料庫中沒有有效的 pHash 特徵，請先執行爬取")

    id_list = [e[0] for e in entries]
    matrix = np.stack([e[1] for e in entries], axis=0).astype(np.uint8)

    index = faiss.IndexBinaryFlat(config.PHASH_BITS)
    index.add(matrix)

    with _index_lock:
        _live_index = index
        _live_ids = id_list
        _live_ids_set = set(id_list)
        _save_index()

    logger.info(f"FAISS 二值索引完整重建完成: {len(id_list)} 筆（含多頁）")
    return index, id_list


def load_faiss_index() -> tuple[faiss.IndexBinary, list[int]] | tuple[None, None]:
    """回傳記憶體中的即時索引（若未初始化則從磁碟載入）"""
    global _live_index, _live_ids, _live_ids_set
    with _index_lock:
        if _live_index is not None and _live_index.ntotal > 0:
            return _live_index, list(_live_ids)
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    if not Path(idx_path).exists() or not Path(ids_path).exists():
        return None, None
    try:
        index = faiss.read_index_binary(idx_path)
        id_list = np.load(ids_path).tolist()
        return index, id_list
    except Exception:
        return None, None
