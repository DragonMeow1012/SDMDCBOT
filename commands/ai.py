"""
AI 來源切換（本地 LM Studio / 線上 Gemini）。

指令：
- /ai模型                顯示目前頻道使用的 AI（本地/線上）
- /ai模型 模型:本地/線上   切換來源（所有人可用）

簡介：本地模型無安全審查，線上功能較完整
"""
from __future__ import annotations

from typing import Literal

import discord
from discord import app_commands

from config import MASTER_ID, GEMINI_API_KEYS
from history import save_history_async
from ai_session import ensure_session
from utils.ai_helpers import normalize_provider
import state


def _provider_label(provider: str) -> str:
    return "本地" if normalize_provider(provider) == "lmstudio" else "線上"



def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="ai模型", description="本地模型無安全審查，線上功能較完整")
    @app_commands.describe(model="要切換的模型來源：本地 / 線上（不填則只顯示狀態）")
    async def slash_ai_model(interaction: discord.Interaction, model: Literal["本地", "線上"] | None = None):
        cid = interaction.channel_id
        sess = state.chat_sessions.get(cid)
        current = normalize_provider(sess.get("ai_provider") if sess else None)

        if model is None:
            await interaction.response.send_message(
                f"目前此頻道 AI：`{_provider_label(current)}`",
                ephemeral=True,
            )
            return

        target = "lmstudio" if model == "本地" else "gemini"
        if target == "gemini" and not GEMINI_API_KEYS:
            await interaction.response.send_message(
                "尚未設定 `GEMINI_API_KEY`，無法切換到 Gemini。",
                ephemeral=True,
            )
            return

        # 以現有 personality 為準；若還沒有 session，就用互動者身分推定一個。
        personality = (sess.get("personality") if sess else None) or (
            "master" if interaction.user.id == MASTER_ID else "general"
        )
        ensure_session(state.chat_sessions, cid, personality, sess, target)
        await save_history_async(state.chat_sessions)

        new_sess = state.chat_sessions.get(cid)
        chat_ok = new_sess and new_sess.get("chat_obj") is not None if target == "gemini" else True
        status_note = "" if chat_ok else "（⚠️ Gemini chat 建立失敗，下次對話時會重試）"

        await interaction.response.send_message(
            f"已切換此頻道 AI：`{_provider_label(current)}` → `{_provider_label(target)}`{status_note}",
            ephemeral=True,
        )

    pass
