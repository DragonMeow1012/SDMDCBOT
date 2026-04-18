"""
Bloom filter — numpy 位元陣列實作。
用途：在破億筆規模下取代 `set[int]` 做成員去重（例：爬蟲已見 user_id）。

特性：
- 1% 誤判率下每筆約 9.6 bits → 1 億筆 ≈ 120 MB
- 允許 false positive（呼叫端把漏掉視為可接受代價）；絕不 false negative
- bulk add 走 numpy 向量化，1 億筆建立約 30–60 秒
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np

# Kirsch-Mitzenmacher 雙哈希用的 64-bit 常數（splitmix64 變體）
_H1_MUL = np.uint64(0x9E3779B97F4A7C15)
_H2_MUL = np.uint64(0xBF58476D1CE4E5B9)
_SHIFT_31 = np.uint64(31)


class BloomFilter:
    __slots__ = ("_m", "_k", "_n", "_bits")

    def __init__(self, expected_n: int, fp_rate: float = 0.01):
        if expected_n <= 0:
            raise ValueError("expected_n must be positive")
        if not 0.0 < fp_rate < 1.0:
            raise ValueError("fp_rate must be in (0, 1)")

        # m = -n * ln(p) / (ln 2)^2     k = (m/n) * ln 2
        m = int(math.ceil(-expected_n * math.log(fp_rate) / (math.log(2) ** 2)))
        m = max(m, 64)
        k = max(int(round((m / expected_n) * math.log(2))), 1)

        self._m: int = m
        self._k: int = k
        self._n: int = 0
        self._bits: np.ndarray = np.zeros((m + 7) // 8, dtype=np.uint8)

    # ── 單筆 API ──────────────────────────────────────────────────────
    def _positions(self, x: int) -> list[int]:
        """回傳 x 對應的 k 個 bit 位置（0..m-1）。走 Python int 避免 numpy 純量
        uint64 乘法會觸發 RuntimeWarning；單筆 k 次 Python 運算夠快。"""
        mask = 0xFFFFFFFFFFFFFFFF
        x64 = int(x) & mask
        h1 = (x64 * 0x9E3779B97F4A7C15) & mask
        h2 = ((x64 ^ (x64 >> 31)) * 0xBF58476D1CE4E5B9) & mask
        m = self._m
        return [((h1 + i * h2) & mask) % m for i in range(self._k)]

    def add(self, x: int) -> None:
        m_bits = self._bits
        for pos in self._positions(x):
            m_bits[pos >> 3] |= np.uint8(1 << (pos & 7))
        self._n += 1

    def __contains__(self, x: int) -> bool:
        m_bits = self._bits
        for pos in self._positions(x):
            if not (m_bits[pos >> 3] & (1 << (pos & 7))):
                return False
        return True

    def __len__(self) -> int:
        return self._n

    # ── 批次 API（populate 時用，極速）─────────────────────────────
    def add_many(self, xs: "Iterable[int] | np.ndarray") -> int:
        """批次加入，回傳實際加入筆數。xs 可為任何 int iterable 或 int64 陣列。"""
        if isinstance(xs, np.ndarray):
            arr = xs if xs.dtype == np.int64 else xs.astype(np.int64, copy=False)
        else:
            arr = np.fromiter((int(x) for x in xs), dtype=np.int64)
        if arr.size == 0:
            return 0

        # 每 1M 一批，避免臨時陣列 (k*n 形狀) 爆記憶體
        CHUNK = 1_000_000
        for i in range(0, arr.size, CHUNK):
            self._add_chunk(arr[i : i + CHUNK])
        return int(arr.size)

    def _add_chunk(self, xs: np.ndarray) -> None:
        xs64 = xs.astype(np.uint64, copy=False)
        h1 = xs64 * _H1_MUL
        h2 = (xs64 ^ (xs64 >> _SHIFT_31)) * _H2_MUL
        k_arr = np.arange(self._k, dtype=np.uint64).reshape(-1, 1)        # (k, 1)
        pos = ((h1[np.newaxis, :] + k_arr * h2[np.newaxis, :])
               % np.uint64(self._m)).ravel()                                # (k*n,)
        byte_idx = (pos >> np.uint64(3)).astype(np.int64, copy=False)
        bit_mask = (np.uint8(1) << (pos & np.uint64(7)).astype(np.uint8))
        np.bitwise_or.at(self._bits, byte_idx, bit_mask)
        self._n += int(xs.size)

    # ── debug/statistics ────────────────────────────────────────────
    def bytes_used(self) -> int:
        return int(self._bits.nbytes)

    def capacity_info(self) -> dict:
        return {
            "m_bits": self._m,
            "k_hashes": self._k,
            "n_inserted": self._n,
            "bytes": self.bytes_used(),
        }
