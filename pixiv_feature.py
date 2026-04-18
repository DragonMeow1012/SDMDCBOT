"""
Pixiv 特徵提取模組
- pHash（感知哈希，64 bits）
- 批次建立 FAISS 二值索引（Hamming 距離）
- 索引 ID 編碼：illust_id * _ID_MULTIPLIER + page_index
"""
import logging
import os
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
                if Path(ids_path).exists():
                    # 用 mmap 讀後立即 copy 成 in-memory array，避免持續佔用檔案
                    base_view = np.load(ids_path, mmap_mode="r")
                    base = np.array(base_view, dtype=np.int64)
                    del base_view
                else:
                    base = np.array([], dtype=np.int64)
                merged = np.concatenate([base, np.array(_tail_ids, dtype=np.int64)], axis=0)

                # Windows 不允許覆寫正在被 mmap 的檔案；先釋放舊的 mmap，再走 tmp+replace
                _base_ids = None
                _base_ids_sorted = None

                tmp_path = ids_path + ".tmp.npy"
                np.save(tmp_path, merged)
                os.replace(tmp_path, ids_path)

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


def build_faiss_index() -> tuple[faiss.IndexBinary, np.ndarray]:
    """
    從資料庫完整重建 FAISS 二值索引（覆蓋記憶體與磁碟）。
    優先使用 GalleryPixiv（含所有頁），不在其中的作品則取 features 作為第 0 頁。

    用 fetchmany 串流 + 邊讀邊 add，避免把數千萬筆 row 全載入記憶體。
    features 補完透過 LEFT JOIN 由 SQLite 過濾掉已在 gallery 的 illust，
    省下 Python 端的 gallery_illust_ids set（破億筆時 set 會吃 GB 級 RAM）。
    """
    global _live_index, _base_ids, _base_ids_sorted, _tail_ids, _tail_ids_set

    CHUNK = 10_000
    id_chunks: list[np.ndarray] = []
    index = faiss.IndexBinaryFlat(config.PHASH_BITS)

    def _consume(cursor, encode_fn) -> int:
        added = 0
        while True:
            rows = cursor.fetchmany(CHUNK)
            if not rows:
                break
            batch_ids: list[int] = []
            batch_vecs: list[np.ndarray] = []
            for row in rows:
                blob = row["color_hist"]
                if not blob or len(blob) != 8:
                    continue
                batch_vecs.append(np.frombuffer(blob, dtype=np.uint8))
                batch_ids.append(encode_fn(row))
            if not batch_vecs:
                continue
            matrix = np.stack(batch_vecs, axis=0).astype(np.uint8, copy=False)
            index.add(matrix)
            id_chunks.append(np.asarray(batch_ids, dtype=np.int64))
            added += len(batch_ids)
        return added

    with db.get_connection() as conn:
        # 1. GalleryPixiv：所有頁的 pHash
        gallery_cur = conn.execute(
            "SELECT illust_id, page_index, color_hist FROM GalleryPixiv "
            "WHERE color_hist IS NOT NULL"
        )
        n_gallery = _consume(gallery_cur, lambda r: encode_id(r["illust_id"], r["page_index"]))

        # 2. features：用 LEFT JOIN 讓 SQLite 本身過濾掉已在 gallery 的 illust（利用 idx_gallery_illust）
        feat_cur = conn.execute(
            "SELECT f.illust_id AS illust_id, f.color_hist AS color_hist "
            "FROM features f LEFT JOIN GalleryPixiv g ON g.illust_id = f.illust_id "
            "WHERE f.color_hist IS NOT NULL AND g.illust_id IS NULL"
        )
        n_features = _consume(feat_cur, lambda r: encode_id(r["illust_id"], 0))

    if not id_chunks:
        raise RuntimeError("資料庫中沒有有效的 pHash 特徵，請先執行爬取")

    id_arr = np.concatenate(id_chunks, axis=0)
    # 釋放 chunk list 的記憶體，concat 後不再需要
    id_chunks.clear()

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

    logger.info(
        f"FAISS 二值索引完整重建完成: {int(id_arr.shape[0])} 筆 "
        f"(gallery={n_gallery}, features-only={n_features})"
    )
    return index, id_arr


# ──────────────────────────────────────────────
# NN binary hash (SSCD) 抽取與 FAISS 索引管理
# 每頁 1 個 512-bit = 64 bytes 二值哈希，ID 用 encode_id(illust_id, page_index)
# ──────────────────────────────────────────────

_nn_model = None
_nn_device = None
_nn_transform = None


def _load_nn_model():
    """Lazy 載入 SSCD torchscript 模型；首次使用時自動下載到 ~/.cache/sscd/。"""
    global _nn_model, _nn_device, _nn_transform
    if _nn_model is not None:
        return
    import torch
    from torchvision import transforms
    cache_dir = Path(config.NN_MODEL_CACHE)
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = cache_dir / "sscd_disc_mixup.torchscript.pt"
    if not model_path.exists():
        import urllib.request
        logger.info(f"Downloading SSCD model: {config.NN_MODEL_URL}")
        urllib.request.urlretrieve(config.NN_MODEL_URL, model_path)
    _nn_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = torch.jit.load(str(model_path), map_location=_nn_device)
    model.eval()
    _nn_model = model
    _nn_transform = transforms.Compose([
        transforms.Resize(config.NN_INPUT_SIZE),
        transforms.CenterCrop(config.NN_INPUT_SIZE),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    logger.info(f"SSCD NN model loaded on {_nn_device}")


def extract_nn_hash(img: Image.Image) -> np.ndarray:
    """單張圖 → 64 bytes (512-bit) binary hash。L2 正規化後用 sign quantize。"""
    import torch
    _load_nn_model()
    t = _nn_transform(img.convert("RGB")).unsqueeze(0).to(_nn_device)
    with torch.no_grad():
        emb = _nn_model(t)
    emb = emb.squeeze(0).cpu().numpy().astype(np.float32)
    norm = float(np.linalg.norm(emb)) + 1e-12
    emb = emb / norm
    bits = (emb > 0).astype(np.uint8)
    if bits.shape[0] != config.NN_HASH_BITS:
        # 模型若輸出維度與設定不符（512 vs 其他），以實際為主
        raise RuntimeError(f"NN embedding dim {bits.shape[0]} != NN_HASH_BITS {config.NN_HASH_BITS}")
    return np.packbits(bits)


def extract_nn_hashes_batch(imgs: Sequence[Image.Image]) -> np.ndarray:
    """批次版：(N,) PIL → (N, NN_HASH_BYTES) uint8。GPU 用得上的主力介面。"""
    import torch
    _load_nn_model()
    if not imgs:
        return np.empty((0, config.NN_HASH_BYTES), dtype=np.uint8)
    batch = torch.stack([_nn_transform(im.convert("RGB")) for im in imgs]).to(_nn_device)
    with torch.no_grad():
        emb = _nn_model(batch)
    emb = emb.cpu().numpy().astype(np.float32)
    norms = np.linalg.norm(emb, axis=1, keepdims=True) + 1e-12
    emb = emb / norms
    bits = (emb > 0).astype(np.uint8)
    return np.packbits(bits, axis=1)


_nn_index_lock = _threading.Lock()
_nn_index: faiss.IndexBinary | None = None
_nn_save_counter = 0
_NN_SAVE_EVERY = int(getattr(config, "FAISS_SAVE_EVERY", 50))
if _NN_SAVE_EVERY < 10:
    _NN_SAVE_EVERY = 10

_nn_base_ids: "np.ndarray | None" = None
_nn_base_ids_sorted: "np.ndarray | None" = None
_nn_tail_ids: list[int] = []
_nn_tail_ids_set: set[int] = set()


def _nn_paths() -> tuple[str, str, str]:
    base = getattr(config, "NN_INDEX_PATH", None) or (
        os.path.join(os.path.dirname(config.FAISS_INDEX_PATH), "nn.index")
    )
    return base, base + ".ids.npy", base + ".ids.tail.npy"


def _save_nn_index() -> None:
    """呼叫前須持有 _nn_index_lock。"""
    if _nn_index is None or _nn_index.ntotal == 0:
        return
    idx_path, ids_path, tail_path = _nn_paths()
    faiss.write_index_binary(_nn_index, idx_path)
    if _nn_tail_ids:
        np.save(tail_path, np.array(_nn_tail_ids, dtype=np.int64))
    if not Path(ids_path).exists():
        base = _nn_base_ids if _nn_base_ids is not None else np.array([], dtype=np.int64)
        np.save(ids_path, np.asarray(base, dtype=np.int64))


def init_nn_index() -> None:
    """啟動時呼叫：從磁碟載入 NN 索引到記憶體；不存在則建空的。"""
    global _nn_index, _nn_base_ids, _nn_base_ids_sorted, _nn_tail_ids, _nn_tail_ids_set
    idx_path, ids_path, tail_path = _nn_paths()
    with _nn_index_lock:
        if Path(idx_path).exists() and Path(ids_path).exists():
            try:
                loaded = faiss.read_index_binary(idx_path)
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
                _nn_index = loaded
                _nn_base_ids = base_ids
                _nn_base_ids_sorted = np.sort(np.asarray(base_ids, dtype=np.int64))
                _nn_tail_ids = tail_ids
                _nn_tail_ids_set = set(_nn_tail_ids)
                logger.info(f"載入 NN FAISS 索引: {_nn_index.ntotal} 筆")
            except Exception as e:
                logger.warning(f"NN 索引載入失敗，建立新索引: {e}")
                _nn_index = faiss.IndexBinaryFlat(config.NN_HASH_BITS)
                _nn_base_ids = None
                _nn_base_ids_sorted = None
                _nn_tail_ids = []
                _nn_tail_ids_set = set()
        else:
            _nn_index = faiss.IndexBinaryFlat(config.NN_HASH_BITS)
            _nn_base_ids = None
            _nn_base_ids_sorted = None
            _nn_tail_ids = []
            _nn_tail_ids_set = set()
            logger.info("建立新 NN FAISS 索引（空）")


def get_nn_index_size() -> int:
    with _nn_index_lock:
        return _nn_index.ntotal if _nn_index else 0


def add_nn_to_index(illust_id: int, page_index: int, nn_vec: np.ndarray) -> None:
    """加入一頁的 NN hash（單個 512-bit/64-byte 向量）。"""
    global _nn_index, _nn_base_ids_sorted, _nn_tail_ids, _nn_tail_ids_set, _nn_save_counter
    if nn_vec.shape != (config.NN_HASH_BYTES,):
        raise ValueError(
            f"nn_vec shape={nn_vec.shape} 與 NN_HASH_BYTES={config.NN_HASH_BYTES} 不符"
        )
    vec = nn_vec.astype(np.uint8, copy=False).reshape(1, -1)
    encoded = encode_id(illust_id, page_index)
    with _nn_index_lock:
        if _nn_index is None:
            _nn_index = faiss.IndexBinaryFlat(config.NN_HASH_BITS)
            _nn_base_ids_sorted = None
            _nn_tail_ids = []
            _nn_tail_ids_set = set()
        if encoded in _nn_tail_ids_set:
            return
        if _nn_base_ids_sorted is not None and int(_nn_base_ids_sorted.shape[0]) > 0:
            pos = int(np.searchsorted(_nn_base_ids_sorted, int(encoded)))
            if pos < int(_nn_base_ids_sorted.shape[0]) and int(_nn_base_ids_sorted[pos]) == int(encoded):
                return
        _nn_index.add(vec)
        _nn_tail_ids.append(int(encoded))
        _nn_tail_ids_set.add(int(encoded))
        _nn_save_counter += 1
        if _nn_save_counter % _NN_SAVE_EVERY == 0:
            _save_nn_index()


def flush_nn_index() -> None:
    """強制存檔並把 tail 合併進 base。"""
    global _nn_base_ids, _nn_base_ids_sorted, _nn_tail_ids, _nn_tail_ids_set
    idx_path, ids_path, tail_path = _nn_paths()
    with _nn_index_lock:
        _save_nn_index()
        if _nn_tail_ids:
            try:
                if Path(ids_path).exists():
                    base_view = np.load(ids_path, mmap_mode="r")
                    base = np.array(base_view, dtype=np.int64)
                    del base_view
                else:
                    base = np.array([], dtype=np.int64)
                merged = np.concatenate([base, np.array(_nn_tail_ids, dtype=np.int64)], axis=0)
                _nn_base_ids = None
                _nn_base_ids_sorted = None
                tmp_path = ids_path + ".tmp.npy"
                np.save(tmp_path, merged)
                os.replace(tmp_path, ids_path)
                if Path(tail_path).exists():
                    try:
                        Path(tail_path).unlink()
                    except Exception:
                        pass
                _nn_base_ids = merged
                _nn_base_ids_sorted = np.sort(merged)
                _nn_tail_ids = []
                _nn_tail_ids_set = set()
            except Exception as e:
                logger.warning(f"NN FAISS ids merge failed: {e}")
    if _nn_index:
        logger.info(f"NN FAISS 索引最終存檔: {_nn_index.ntotal} 筆")


def build_nn_faiss_index() -> tuple[faiss.IndexBinary, np.ndarray]:
    """從 DB 的 GalleryPixiv.nn_hash 完整重建 NN 索引。"""
    global _nn_index, _nn_base_ids, _nn_base_ids_sorted, _nn_tail_ids, _nn_tail_ids_set

    CHUNK = 10_000
    id_chunks: list[np.ndarray] = []
    index = faiss.IndexBinaryFlat(config.NN_HASH_BITS)
    expected_bytes = config.NN_HASH_BYTES

    with db.get_connection() as conn:
        cur = conn.execute(
            "SELECT illust_id, page_index, nn_hash FROM GalleryPixiv "
            "WHERE nn_hash IS NOT NULL"
        )
        added = 0
        while True:
            rows = cur.fetchmany(CHUNK)
            if not rows:
                break
            batch_ids: list[int] = []
            batch_vecs: list[np.ndarray] = []
            for row in rows:
                blob = row["nn_hash"]
                if not blob or len(blob) != expected_bytes:
                    continue
                batch_vecs.append(np.frombuffer(blob, dtype=np.uint8))
                batch_ids.append(encode_id(row["illust_id"], row["page_index"]))
            if not batch_vecs:
                continue
            matrix = np.stack(batch_vecs, axis=0).astype(np.uint8, copy=False)
            index.add(matrix)
            id_chunks.append(np.asarray(batch_ids, dtype=np.int64))
            added += len(batch_ids)

    if not id_chunks:
        raise RuntimeError("DB 沒有 nn_hash 資料，請先跑爬蟲產生")

    id_arr = np.concatenate(id_chunks, axis=0)
    id_chunks.clear()

    idx_path, ids_path, tail_path = _nn_paths()
    with _nn_index_lock:
        _nn_index = index
        _nn_base_ids = id_arr
        _nn_base_ids_sorted = np.sort(id_arr)
        _nn_tail_ids = []
        _nn_tail_ids_set = set()
        faiss.write_index_binary(_nn_index, idx_path)
        np.save(ids_path, id_arr)
        tp = Path(tail_path)
        if tp.exists():
            try:
                tp.unlink()
            except Exception:
                pass

    logger.info(f"NN FAISS 索引重建完成: {int(id_arr.shape[0])} 筆")
    return index, id_arr


def load_nn_faiss_index() -> tuple[faiss.IndexBinary, Sequence[int]] | tuple[None, None]:
    """回傳記憶體中的 NN 索引（若未初始化則從磁碟載入）。結構對稱 load_faiss_index。"""
    global _nn_index, _nn_base_ids, _nn_tail_ids
    with _nn_index_lock:
        if _nn_index is not None and _nn_index.ntotal > 0:
            return _nn_index, IdListView(_nn_base_ids, list(_nn_tail_ids))
    idx_path, ids_path, tail_path = _nn_paths()
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
