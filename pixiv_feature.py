"""
Pixiv 特徵提取模組
- pHash（感知哈希，64 bits）
- 批次建立 FAISS 二值索引（Hamming 距離）
"""
import logging
import numpy as np
import faiss
from PIL import Image
from pathlib import Path

import pixiv_config as config
import pixiv_database as db

logger = logging.getLogger(__name__)


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
_live_ids: list[int] = []
_save_counter = 0
_SAVE_EVERY = 10


def _save_index():
    """將記憶體索引寫入磁碟（呼叫前需持有 _index_lock）"""
    if _live_index is None or _live_index.ntotal == 0:
        return
    faiss.write_index_binary(_live_index, config.FAISS_INDEX_PATH)
    np.save(config.FAISS_INDEX_PATH + ".ids.npy",
            np.array(_live_ids, dtype=np.int64))


def init_live_index():
    """
    啟動時呼叫：從磁碟載入現有索引到記憶體；若無或格式不符則建立空索引。
    """
    global _live_index, _live_ids
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    with _index_lock:
        if Path(idx_path).exists() and Path(ids_path).exists():
            try:
                _live_index = faiss.read_index_binary(idx_path)
                _live_ids = np.load(ids_path).tolist()
                logger.info(f"載入現有 FAISS 二值索引: {_live_index.ntotal} 筆")
            except Exception:
                logger.warning("FAISS 索引格式不符（舊版），建立新二值索引")
                _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
                _live_ids = []
        else:
            _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
            _live_ids = []
            logger.info("建立新 FAISS 二值索引（空）")


def add_to_index(illust_id: int, phash_vec: np.ndarray):
    """
    將單筆 pHash 向量加入記憶體索引，每 _SAVE_EVERY 筆存檔一次。
    線程安全。
    """
    global _live_index, _live_ids, _save_counter
    vec = phash_vec.astype(np.uint8).reshape(1, -1)
    with _index_lock:
        if _live_index is None:
            _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
            _live_ids = []
        if illust_id in _live_ids:
            return
        _live_index.add(vec)
        _live_ids.append(illust_id)
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
    用於修復或初始化；正常爬取請用 add_to_index()。
    """
    global _live_index, _live_ids
    all_features = db.get_all_features()
    valid = [(fid, feat) for fid, feat in all_features if len(feat) == 8]
    if not valid:
        raise RuntimeError("資料庫中沒有有效的 pHash 特徵，請先執行爬取")

    id_list = [fid for fid, _ in valid]
    matrix = np.stack([feat for _, feat in valid], axis=0).astype(np.uint8)

    index = faiss.IndexBinaryFlat(config.PHASH_BITS)
    index.add(matrix)

    with _index_lock:
        _live_index = index
        _live_ids = id_list
        _save_index()

    logger.info(f"FAISS 二值索引完整重建完成: {len(id_list)} 筆")
    return index, id_list


def load_faiss_index() -> tuple[faiss.IndexBinary, list[int]] | tuple[None, None]:
    """回傳記憶體中的即時索引（若未初始化則從磁碟載入）"""
    global _live_index, _live_ids
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
