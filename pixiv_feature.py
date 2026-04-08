"""
Pixiv 特徵提取模組
- 顏色直方圖 (RGB, 96 維)
- K-Means 主色提取
- 批次建立 FAISS 索引
（改自 pixiv_x_Spider/feature_extractor.py，使用 pixiv_config / pixiv_database）
"""
import logging
import numpy as np
import faiss
from PIL import Image
from pathlib import Path
from sklearn.cluster import MiniBatchKMeans

import pixiv_config as config
import pixiv_database as db

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 特徵提取
# ──────────────────────────────────────────────

def extract_color_histogram(img: Image.Image) -> np.ndarray:
    """
    提取 RGB 顏色直方圖（各通道 COLOR_BINS bins），L2 正規化後回傳。
    輸出維度 = COLOR_BINS * 3
    """
    img_small = img.resize(config.THUMB_SIZE).convert("RGB")
    arr = np.array(img_small)

    hists = []
    for ch in range(3):
        h, _ = np.histogram(arr[:, :, ch], bins=config.COLOR_BINS, range=(0, 256))
        hists.append(h.astype(np.float32))

    hist = np.concatenate(hists)
    norm = np.linalg.norm(hist)
    if norm > 0:
        hist /= norm
    return hist


def extract_dominant_colors(img: Image.Image, n_colors: int = 5) -> list[list[int]]:
    """
    使用 K-Means 提取 n 個主要顏色，回傳 [[R,G,B], ...]（依比例排序）
    """
    img_small = img.resize((100, 100)).convert("RGB")
    pixels = np.array(img_small).reshape(-1, 3).astype(np.float32)

    kmeans = MiniBatchKMeans(n_clusters=n_colors, n_init=3, random_state=42)
    kmeans.fit(pixels)

    labels = kmeans.labels_
    centers = kmeans.cluster_centers_
    counts = np.bincount(labels)
    order = np.argsort(-counts)
    dominant = centers[order].astype(int).tolist()
    return dominant


def process_image(local_path: str) -> tuple[np.ndarray, list] | None:
    """
    對單張圖片提取全部特徵，回傳 (color_hist, dominant_colors) 或 None
    """
    try:
        img = Image.open(local_path).convert("RGB")
        color_hist = extract_color_histogram(img)
        dominant = extract_dominant_colors(img)
        return color_hist, dominant
    except Exception as e:
        logger.warning(f"特徵提取失敗 {local_path}: {e}")
        return None


# ──────────────────────────────────────────────
# FAISS 索引管理
# ──────────────────────────────────────────────

import threading as _threading

_index_lock = _threading.Lock()
_live_index: faiss.Index | None = None   # 記憶體中的即時索引
_live_ids: list[int] = []                # 與索引行數一一對應的 illust_id
_save_counter = 0
_SAVE_EVERY = 10   # 每累積幾筆就存檔一次


def _save_index():
    """將記憶體索引寫入磁碟（呼叫前需持有 _index_lock）"""
    if _live_index is None or _live_index.ntotal == 0:
        return
    faiss.write_index(_live_index, config.FAISS_INDEX_PATH)
    np.save(config.FAISS_INDEX_PATH + ".ids.npy",
            np.array(_live_ids, dtype=np.int64))


def init_live_index():
    """
    啟動時呼叫：從磁碟載入現有索引到記憶體；若無則建立空索引。
    """
    global _live_index, _live_ids
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    with _index_lock:
        if Path(idx_path).exists() and Path(ids_path).exists():
            _live_index = faiss.read_index(idx_path)
            _live_ids = np.load(ids_path).tolist()
            logger.info(f"載入現有 FAISS 索引: {_live_index.ntotal} 筆")
        else:
            _live_index = faiss.IndexFlatIP(config.COLOR_FEATURE_DIM)
            _live_ids = []
            logger.info("建立新 FAISS 索引（空）")


def add_to_index(illust_id: int, color_hist: np.ndarray):
    """
    將單筆向量加入記憶體索引，每 _SAVE_EVERY 筆存檔一次。
    線程安全。
    """
    global _live_index, _live_ids, _save_counter
    vec = color_hist.astype(np.float32).reshape(1, -1)
    with _index_lock:
        if _live_index is None:
            _live_index = faiss.IndexFlatIP(vec.shape[1])
            _live_ids = []
        # 若已存在則跳過（避免重複）
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


def build_faiss_index() -> tuple[faiss.Index, list[int]]:
    """
    從資料庫完整重建 FAISS 索引（覆蓋記憶體與磁碟）。
    用於修復或初始化；正常爬取請用 add_to_index()。
    """
    global _live_index, _live_ids
    all_features = db.get_all_features()
    if not all_features:
        raise RuntimeError("資料庫中沒有特徵資料，請先執行特徵提取")

    id_list = [fid for fid, _ in all_features]
    matrix = np.stack([feat for _, feat in all_features], axis=0).astype(np.float32)

    dim = matrix.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(matrix)

    with _index_lock:
        _live_index = index
        _live_ids = id_list
        _save_index()

    logger.info(f"FAISS 索引完整重建完成: {len(id_list)} 筆, dim={dim}")
    return index, id_list


def load_faiss_index() -> tuple[faiss.Index, list[int]] | tuple[None, None]:
    """回傳記憶體中的即時索引（若未初始化則從磁碟載入）"""
    global _live_index, _live_ids
    with _index_lock:
        if _live_index is not None and _live_index.ntotal > 0:
            return _live_index, list(_live_ids)
    # fallback：從磁碟載入
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    if not Path(idx_path).exists() or not Path(ids_path).exists():
        return None, None
    index = faiss.read_index(idx_path)
    id_list = np.load(ids_path).tolist()
    return index, id_list
