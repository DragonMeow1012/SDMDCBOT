"""
AI provider 共用輔助函式。
"""
from config import AI_PROVIDER_DEFAULT

_LMSTUDIO_ALIASES = {"local", "lm", "lm-studio", "lm_studio", "lmstudio"}


def normalize_provider(provider: str | None) -> str:
    """將各種寫法的 provider 名稱正規化為 'gemini' 或 'lmstudio'。"""
    p = (provider or AI_PROVIDER_DEFAULT or "gemini").strip().lower()
    return "lmstudio" if p in _LMSTUDIO_ALIASES else "gemini"
