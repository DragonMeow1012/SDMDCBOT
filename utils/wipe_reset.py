"""
清空 Pixiv DB + FAISS 索引，準備重新爬取。
- 刪除 pixiv.db / pixiv.db-wal / pixiv.db-shm
- 刪除 feature.index + *.ids.npy + *.ids.tail.npy
- 刪除 nn.index + *.ids.npy + *.ids.tail.npy
- 若還有舊版 tile.index 相關檔案也順手清掉
- 保留 ~/.cache/sscd/ 的模型快取（不需要重下）

執行前會列出所有要刪的檔案與大小，要 y/N 確認才動手。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pixiv_config as config


def _size_mb(p: Path) -> float:
    try:
        return p.stat().st_size / (1024 * 1024)
    except FileNotFoundError:
        return 0.0


def _collect_targets() -> list[Path]:
    data_dir = Path(config.DATA_DIR)

    candidates: list[Path] = []

    db_path = Path(config.DB_PATH)
    candidates.append(db_path)
    candidates.append(db_path.with_name(db_path.name + "-wal"))
    candidates.append(db_path.with_name(db_path.name + "-shm"))

    for base in (config.FAISS_INDEX_PATH, config.NN_INDEX_PATH):
        b = Path(base)
        candidates.append(b)
        candidates.append(b.with_name(b.name + ".ids.npy"))
        candidates.append(b.with_name(b.name + ".ids.tail.npy"))
        candidates.append(b.with_name(b.name + ".ids.npy.tmp.npy"))

    tile_base = data_dir / "tile.index"
    candidates.append(tile_base)
    candidates.append(tile_base.with_name(tile_base.name + ".ids.npy"))
    candidates.append(tile_base.with_name(tile_base.name + ".ids.tail.npy"))

    return [p for p in candidates if p.exists()]


def main() -> int:
    targets = _collect_targets()
    if not targets:
        print("沒有可刪除的檔案，已是乾淨狀態。")
        return 0

    print("即將刪除以下檔案：")
    total_mb = 0.0
    for p in targets:
        mb = _size_mb(p)
        total_mb += mb
        print(f"  - {p}  ({mb:.2f} MB)")
    print(f"合計 {total_mb:.2f} MB")
    print(f"保留：{config.NN_MODEL_CACHE}（SSCD 模型快取）")

    ans = input("確認刪除? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("已取消。")
        return 1

    for p in targets:
        try:
            os.remove(p)
            print(f"deleted: {p}")
        except Exception as e:
            print(f"failed: {p} ({e})")

    print("完成。下次啟動爬蟲會建立空索引。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
