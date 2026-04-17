"""
聊天歷史持久化。

- 啟動時由 main.on_ready 呼叫 load_history()
- 每輪對話結束時由 gemini_worker 呼叫 save_history_async()
- 檔案寫入使用 tmp + os.replace 的原子模式，避免崩潰時壞檔
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

from config import DATA_DIR, HISTORY_FILE, HISTORY_MAX_TURNS
from summary import save_summary


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_history() -> dict:
    """
    從檔案載入對話歷史，回傳 { channel_id -> session_dict }。
    Discord 頻道 ID 為 int；LINE 則是 'line_xxx_yyy' 字串 key。
    """
    _ensure_data_dir()

    if not os.path.exists(HISTORY_FILE):
        print("ℹ️ 無歷史檔，建立空檔")
        _atomic_write_json(HISTORY_FILE, {})
        return {}

    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        sessions: dict = {}
        for cid_str, sess_data in data.items():
            try:
                cid: Any = int(cid_str)
            except ValueError:
                cid = cid_str
            sessions[cid] = {
                'chat_obj': None,
                'model': None,
                'raw_history': sess_data.get('raw_history', []),
                'current_web_context': sess_data.get('current_web_context'),
                'ai_provider': sess_data.get('ai_provider'),
            }

        print(f"✅ 歷史已載: {HISTORY_FILE} ({len(sessions)} 個頻道)")
        return sessions

    except json.JSONDecodeError as e:
        print(f"❌ 歷史檔格式錯誤，重置: {e}")
        _atomic_write_json(HISTORY_FILE, {})
        return {}
    except Exception as e:
        print(f"❌ 載入歷史失敗: {e}")
        return {}


def _atomic_write_json(path: str, data: Any) -> None:
    dir_path = os.path.dirname(path) or '.'
    os.makedirs(dir_path, exist_ok=True)
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


def _build_snapshot(chat_sessions: dict) -> dict:
    """將 live session 轉為可序列化 dict。"""
    data: dict = {}
    for cid, sess in chat_sessions.items():
        chat_obj = sess.get('chat_obj')
        if chat_obj is not None and hasattr(chat_obj, 'get_history'):
            hist = [
                {"role": m.role,
                 "parts": [{"text": p.text if p.text else "[附件]"} for p in m.parts]}
                for m in chat_obj.get_history()
            ]
        else:
            hist = sess.get('raw_history', [])

        if len(hist) > HISTORY_MAX_TURNS:
            del hist[:-HISTORY_MAX_TURNS]

        data[str(cid)] = {
            'raw_history': hist,
            'current_web_context': sess.get('current_web_context'),
            'ai_provider': sess.get('ai_provider'),
        }
    return data


def save_history(chat_sessions: dict) -> None:
    """同步存檔（啟動/關閉時使用；熱路徑請改用 save_history_async）。"""
    _ensure_data_dir()
    try:
        data = _build_snapshot(chat_sessions)
        _atomic_write_json(HISTORY_FILE, data)

        for cid_str, sess_data in data.items():
            try:
                cid_key: Any = int(cid_str)
            except ValueError:
                cid_key = cid_str
            save_summary(cid_key, sess_data.get('raw_history', []))

        print(f"✅ 歷史已存: {HISTORY_FILE}")
    except Exception as e:
        print(f"❌ 存檔失敗: {e}")


async def save_history_async(chat_sessions: dict) -> None:
    """熱路徑用：在工作執行緒寫檔，避免阻塞 Discord event loop。"""
    try:
        snapshot = _build_snapshot(chat_sessions)
    except Exception as e:
        print(f"❌ 構造歷史快照失敗: {e}")
        return

    def _write() -> None:
        _atomic_write_json(HISTORY_FILE, snapshot)
        for cid_str, sess_data in snapshot.items():
            try:
                cid_key: Any = int(cid_str)
            except ValueError:
                cid_key = cid_str
            save_summary(cid_key, sess_data.get('raw_history', []))

    try:
        await asyncio.to_thread(_write)
    except Exception as e:
        print(f"❌ 存檔失敗: {e}")
