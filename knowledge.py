"""
知識庫模組：管理跨頻道永久儲存的知識條目。
儲存於 data/knowledge.json。

指令（在 main.py 中處理）：
  !kb 儲存 <內容>         - 儲存一條知識（任何人）
  !kb 列表               - 列出全部條目（主人限定）
  !kb 刪除 <id>          - 刪除指定條目（主人限定）
  !kb 查詢 <關鍵字>       - 搜尋相關條目（任何人）
"""
import json
import time
import os

from config import DATA_DIR

KNOWLEDGE_FILE = os.path.join(DATA_DIR, "knowledge.json")


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_knowledge() -> list[dict]:
    """從檔案載入知識庫，回傳條目列表。"""
    _ensure_data_dir()
    if not os.path.exists(KNOWLEDGE_FILE):
        return []
    try:
        with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[KB] 已載入 {len(data)} 條知識條目")
        return data
    except Exception as e:
        print(f"[KB] 載入失敗: {e}")
        return []


def save_knowledge(entries: list[dict]) -> None:
    """將知識庫寫回檔案。"""
    _ensure_data_dir()
    try:
        with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[KB] 存檔失敗: {e}")


def add_entry(entries: list[dict], content: str, saved_by: int) -> dict:
    """新增一條知識條目，回傳新條目。"""
    next_id = max((e["id"] for e in entries), default=0) + 1
    entry = {
        "id": next_id,
        "content": content,
        "saved_by": str(saved_by),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(entry)
    save_knowledge(entries)
    return entry


def remove_entry(entries: list[dict], entry_id: int) -> bool:
    """刪除指定 ID 條目，成功回傳 True。"""
    before = len(entries)
    entries[:] = [e for e in entries if e["id"] != entry_id]
    if len(entries) < before:
        save_knowledge(entries)
        return True
    return False


def search_entries(entries: list[dict], keyword: str) -> list[dict]:
    """搜尋內容包含關鍵字的條目（不區分大小寫）。"""
    kw = keyword.lower()
    return [e for e in entries if kw in e["content"].lower()]


def build_knowledge_context(entries: list[dict]) -> str:
    """格式化知識庫為模型注入字串，空庫時回傳空字串。"""
    if not entries:
        return ""
    lines = ["【知識庫·重要記憶】以下為人工儲存的重要資訊，對話時需參考："]
    for e in entries:
        lines.append(f"  #{e['id']} [{e['timestamp']}]: {e['content']}")
    return "\n".join(lines) + "\n"
