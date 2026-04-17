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
from typing import Iterator, Sequence, overload

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
_save_counter = 0
_SAVE_EVERY = int(getattr(config, "FAISS_SAVE_EVERY", 50))
if _SAVE_EVERY < 10:
    _SAVE_EVERY = 10


class IdListView(Sequence[int]):
    """Memory-light view of FAISS ids without materializing Python int lists."""

    def __init__(self, base_ids: "np.ndarray | None", tail_ids: list[int]):
        self._base = base_ids
        self._tail = tail_ids

    def __len__(self) -> int:
        base_len = int(self._base.shape[0]) if self._base is not None else 0
        return base_len + len(self._tail)

    @overload
    def __getitem__(self, idx: int) -> int: ...

    @overload
    def __getitem__(self, idx: slice) -> list[int]: ...

    def __getitem__(self, idx):
        base_len = int(self._base.shape[0]) if self._base is not None else 0
        if isinstance(idx, slice):
            start, stop, step = idx.indices(len(self))
            return [self[i] for i in range(start, stop, step)]
        if idx < 0:
            idx += len(self)
        if idx < 0 or idx >= len(self):
            raise IndexError(idx)
        if idx < base_len:
            return int(self._base[idx])
        return int(self._tail[idx - base_len])

    def __iter__(self) -> Iterator[int]:
        base_len = int(self._base.shape[0]) if self._base is not None else 0
        for i in range(base_len):
            yield int(self._base[i])
        yield from (int(x) for x in self._tail)


# New id storage (avoid Python int list/set for millions of ids)
_base_ids: "np.ndarray | None" = None          # np.int64, aligns with _live_index order
_base_ids_sorted: "np.ndarray | None" = None   # np.int64 sorted copy for membership checks
_tail_ids: list[int] = []                      # new ids appended after loading base ids
_tail_ids_set: set[int] = set()                # membership for tail ids only (kept small)


def _is_old_format(ids: "np.ndarray | None") -> bool:
    """判斷是否為舊格式（plain illust_id，未含 page_index 編碼）"""
    if ids is None or int(ids.shape[0]) == 0:
        return False
    # 舊格式 illust_id 約在 10^7~2×10^8，編碼後最小值 = 10^7 * 10000 = 10^11
    try:
        return int(ids.max()) < 10**9
    except Exception:
        return False


def _save_index():
    """將記憶體索引寫入磁碟（呼叫前需持有 _index_lock）"""
    if _live_index is None or _live_index.ntotal == 0:
        return
    faiss.write_index_binary(_live_index, config.FAISS_INDEX_PATH)
    ids_path = config.FAISS_INDEX_PATH + ".ids.npy"
    tail_path = config.FAISS_INDEX_PATH + ".ids.tail.npy"

    # Avoid rewriting a potentially huge ids.npy frequently; persist only the tail most of the time.
    if _tail_ids:
        np.save(tail_path, np.array(_tail_ids, dtype=np.int64))

    # Ensure ids.npy exists for freshly-built index cases.
    if not Path(ids_path).exists():
        base = _base_ids if _base_ids is not None else np.array([], dtype=np.int64)
        np.save(ids_path, np.asarray(base, dtype=np.int64))


def init_live_index():
    """
    啟動時呼叫：從磁碟載入現有索引到記憶體。
    若格式為舊版（plain illust_id）則清空並等待 build_faiss_index 重建。
    """
    global _live_index, _base_ids, _base_ids_sorted, _tail_ids, _tail_ids_set
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    tail_path = idx_path + ".ids.tail.npy"
    with _index_lock:
        if Path(idx_path).exists() and Path(ids_path).exists():
            try:
                loaded_index = faiss.read_index_binary(idx_path)
                base_ids = np.load(ids_path, mmap_mode="r")
                if base_ids.dtype != np.int64:
                    base_ids = base_ids.astype(np.int64, copy=False)

                tail_ids: list[int] = []
                if Path(tail_path).exists():
                    try:
                        tail_arr = np.load(tail_path, mmap_mode="r")
                        if tail_arr.dtype != np.int64:
                            tail_arr = tail_arr.astype(np.int64, copy=False)
                        if int(tail_arr.shape[0]) > 0:
                            tail_ids = [int(x) for x in tail_arr.tolist()]
                    except Exception:
                        tail_ids = []

                if _is_old_format(base_ids):
                    logger.warning("FAISS 索引為舊格式（未含頁索引編碼），清空等待重建")
                    _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
                    _base_ids = None
                    _base_ids_sorted = None
                    _tail_ids = []
                    _tail_ids_set = set()
                else:
                    _live_index = loaded_index
                    _base_ids = base_ids
                    _base_ids_sorted = np.sort(np.asarray(base_ids, dtype=np.int64))
                    _tail_ids = tail_ids
                    _tail_ids_set = set(_tail_ids)
                    logger.info(f"載入現有 FAISS 二值索引: {_live_index.ntotal} 筆")
            except Exception:
                logger.warning("FAISS 索引格式不符，建立新二值索引")
                _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
                _base_ids = None
                _base_ids_sorted = None
                _tail_ids = []
                _tail_ids_set = set()
        else:
            _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
            _base_ids = None
            _base_ids_sorted = None
            _tail_ids = []
            _tail_ids_set = set()
            logger.info("建立新 FAISS 二值索引（空）")


def get_index_size() -> int:
    with _index_lock:
        return _live_index.ntotal if _live_index else 0


def add_to_index(illust_id: int, page_index: int, phash_vec: np.ndarray):
    """
    將單頁 pHash 加入記憶體索引，每 _SAVE_EVERY 筆存檔一次。
    線程安全。
    """
    global _live_index, _base_ids_sorted, _tail_ids, _tail_ids_set, _save_counter
    encoded = encode_id(illust_id, page_index)
    vec = phash_vec.astype(np.uint8).reshape(1, -1)
    with _index_lock:
        if _live_index is None:
            _live_index = faiss.IndexBinaryFlat(config.PHASH_BITS)
            _base_ids_sorted = None
            _tail_ids = []
            _tail_ids_set = set()
        if encoded in _tail_ids_set:
            return
        if _base_ids_sorted is not None and int(_base_ids_sorted.shape[0]) > 0:
            pos = int(np.searchsorted(_base_ids_sorted, int(encoded)))
            if pos < int(_base_ids_sorted.shape[0]) and int(_base_ids_sorted[pos]) == int(encoded):
                return
        _live_index.add(vec)
        _tail_ids.append(int(encoded))
        _tail_ids_set.add(int(encoded))
        _save_counter += 1
        if _save_counter % _SAVE_EVERY == 0:
            _save_index()
            logger.debug(f"FAISS 索引已存檔: {_live_index.ntotal} 筆")


def flush_index():
    """強制將記憶體索引存檔（爬取結束時呼叫）"""
    global _base_ids, _base_ids_sorted, _tail_ids, _tail_ids_set
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    tail_path = idx_path + ".ids.tail.npy"

    with _index_lock:
        _save_index()
        if _tail_ids:
            try:
                base = np.load(ids_path) if Path(ids_path).exists() else np.array([], dtype=np.int64)
                if base.dtype != np.int64:
                    base = base.astype(np.int64, copy=False)
                merged = np.concatenate([base, np.array(_tail_ids, dtype=np.int64)], axis=0)
                np.save(ids_path, merged)
                if Path(tail_path).exists():
                    try:
                        Path(tail_path).unlink()
                    except Exception:
                        pass

                _base_ids = merged
                _base_ids_sorted = np.sort(merged)
                _tail_ids = []
                _tail_ids_set = set()
            except Exception as e:
                logger.warning(f"FAISS ids merge failed: {e}")

    if _live_index:
        logger.info(f"FAISS 索引最終存檔完成: {_live_index.ntotal} 筆")


def build_faiss_index() -> tuple[faiss.IndexBinary, list[int]]:
    """
    從資料庫完整重建 FAISS 二值索引（覆蓋記憶體與磁碟）。
    優先使用 GalleryPixiv（含所有頁），不在其中的作品則取 features 作為第 0 頁。
    """
    global _live_index, _base_ids, _base_ids_sorted, _tail_ids, _tail_ids_set

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

    id_arr = np.array([e[0] for e in entries], dtype=np.int64)
    matrix = np.stack([e[1] for e in entries], axis=0).astype(np.uint8)

    index = faiss.IndexBinaryFlat(config.PHASH_BITS)
    index.add(matrix)

    with _index_lock:
        _live_index = index
        _base_ids = id_arr
        _base_ids_sorted = np.sort(id_arr)
        _tail_ids = []
        _tail_ids_set = set()
        faiss.write_index_binary(_live_index, config.FAISS_INDEX_PATH)
        np.save(config.FAISS_INDEX_PATH + ".ids.npy", id_arr)
        tail_path = Path(config.FAISS_INDEX_PATH + ".ids.tail.npy")
        if tail_path.exists():
            try:
                tail_path.unlink()
            except Exception:
                pass

    logger.info(f"FAISS 二值索引完整重建完成: {int(id_arr.shape[0])} 筆（含多頁）")
    return index, [int(x) for x in id_arr.tolist()]


def load_faiss_index() -> tuple[faiss.IndexBinary, Sequence[int]] | tuple[None, None]:
    """回傳記憶體中的即時索引（若未初始化則從磁碟載入）"""
    global _live_index, _base_ids, _tail_ids
    with _index_lock:
        if _live_index is not None and _live_index.ntotal > 0:
            return _live_index, IdListView(_base_ids, list(_tail_ids))
    idx_path = config.FAISS_INDEX_PATH
    ids_path = idx_path + ".ids.npy"
    tail_path = idx_path + ".ids.tail.npy"
    if not Path(idx_path).exists() or not Path(ids_path).exists():
        return None, None
    try:
        index = faiss.read_index_binary(idx_path)
        base_ids = np.load(ids_path, mmap_mode="r")
        if base_ids.dtype != np.int64:
            base_ids = base_ids.astype(np.int64, copy=False)
        tail_ids: list[int] = []
        if Path(tail_path).exists():
            try:
                tail_arr = np.load(tail_path, mmap_mode="r")
                if tail_arr.dtype != np.int64:
                    tail_arr = tail_arr.astype(np.int64, copy=False)
                if int(tail_arr.shape[0]) > 0:
                    tail_ids = [int(x) for x in tail_arr.tolist()]
            except Exception:
                tail_ids = []
        return index, IdListView(base_ids, tail_ids)
    except Exception:
        return None, None
