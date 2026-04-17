"""
共用 JSON 讀寫工具。

- load_json / save_json：同步版
- save_json_async     ：把寫檔丟到 thread pool，避免阻塞 event loop
- save_json 採用「tmp + os.replace」的原子寫入，程式中斷不會壞檔
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any, Callable


def load_json(path: str, default_factory: Callable[[], Any] = dict) -> Any:
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default_factory()


def save_json(path: str, data: Any) -> None:
    dir_path = os.path.dirname(path) or '.'
    os.makedirs(dir_path, exist_ok=True)

    # 寫入同資料夾的暫存檔後 rename：確保原子性
    fd, tmp_path = tempfile.mkstemp(
        prefix='.' + os.path.basename(path) + '.',
        suffix='.tmp',
        dir=dir_path,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def save_json_async(path: str, data: Any) -> None:
    """非同步版：在背景執行緒寫檔，避免阻塞 event loop。"""
    await asyncio.to_thread(save_json, path, data)
