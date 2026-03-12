"""
用戶暱稱管理模組。
儲存格式：data/nicknames.json = { "user_id_str": "暱稱" }

每次對話時注入暱稱資訊，讓模型能以正確稱謂回應用戶。
主人可透過 !nick 指令管理所有暱稱；一般用戶僅能設定自己的暱稱。
"""
import json
import os
from config import DATA_DIR, NICKNAMES_FILE


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_nicknames() -> dict[str, str]:
    """
    從本地檔案載入暱稱對照表。
    回傳 dict：{ user_id_str -> nickname }
    """
    _ensure_data_dir()

    if not os.path.exists(NICKNAMES_FILE):
        with open(NICKNAMES_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        return {}

    try:
        with open(NICKNAMES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'[OK] 暱稱已載: {len(data)} 筆')
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        print(f'[ERROR] 暱稱載入失敗: {e}')
        return {}


def save_nicknames(nicknames: dict[str, str]) -> None:
    """將暱稱對照表儲存至本地檔案。"""
    _ensure_data_dir()
    try:
        with open(NICKNAMES_FILE, 'w', encoding='utf-8') as f:
            json.dump(nicknames, f, ensure_ascii=False, indent=2)
        print(f'[OK] 暱稱已存: {len(nicknames)} 筆')
    except Exception as e:
        print(f'[ERROR] 暱稱存檔失敗: {e}')


def get_nickname(user_id: int, nicknames: dict[str, str]) -> str | None:
    """取得指定用戶 ID 的暱稱，無則回傳 None。"""
    return nicknames.get(str(user_id))


def build_user_context(user_id: int, nicknames: dict[str, str]) -> str:
    """
    建立注入給模型的用戶身分前綴。
    格式：[User ID: {id}, 暱稱: {nick}]  或  [User ID: {id}, 暱稱: 未知]
    """
    nick = get_nickname(user_id, nicknames)
    nick_part = f', 暱稱: {nick}' if nick else ''
    return f'[User ID: {user_id}{nick_part}]'


def build_all_nicknames_summary(nicknames: dict[str, str]) -> str:
    """
    建立所有已知暱稱的摘要字串，供主人模式參考。
    格式：[已知用戶暱稱: id1=名稱1, id2=名稱2, ...]
    """
    if not nicknames:
        return '[已知用戶暱稱: 無]'
    entries = ', '.join(f'{uid}={nick}' for uid, nick in nicknames.items())
    return f'[已知用戶暱稱清單: {entries}]'
