"""
AI session 初始化 / 重建。

目前支援：
- gemini：google-genai Chat (gemini_worker.create_chat)
- lmstudio：LM Studio OpenAI-compatible API（不需要 chat_obj，僅用 raw_history）
"""
from __future__ import annotations

from summary import load_summary
from gemini_worker import create_chat
from utils.ai_helpers import normalize_provider


def ensure_session(
    chat_sessions: dict,
    cid: int,
    personality: str,
    sess: dict | None,
    provider: str | None = None,
) -> dict:
    """
    建立/重建一個 channel session。

    - 保留既有 raw_history / current_web_context
    - 依 provider 決定是否建立 Gemini chat_obj
    """
    provider_norm = normalize_provider(provider or (sess.get("ai_provider") if sess else None))

    raw_history = sess.get("raw_history", []) if sess else []
    web_context = sess.get("current_web_context") if sess else None
    summary = load_summary(cid)

    next_sess = {
        "chat_obj": None,
        "model": None,
        "personality": personality,
        "raw_history": raw_history,
        "current_web_context": web_context,
        "ai_provider": provider_norm,
    }

    if provider_norm == "gemini":
        try:
            next_sess["chat_obj"] = create_chat(personality, raw_history, summary)
        except Exception as e:
            # 允許在未設定 GEMINI_API_KEY 的情況下啟動（例如只用本地 LM Studio）
            print(f"[AI] create_chat failed (gemini): {e}")
            next_sess["chat_obj"] = None

    chat_sessions[cid] = next_sess
    return next_sess
