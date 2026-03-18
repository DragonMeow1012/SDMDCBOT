"""
管理員指令：/清除記憶、/清空知識庫
"""
import json
import discord
from discord import app_commands

from config import MASTER_ID, HISTORY_FILE
from knowledge import KNOWLEDGE_FILE
import state


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="清除記憶", description="清除所有頻道的聊天歷史，下次對話將重新開始。（主人限定）")
    async def slash_clear_memory(interaction: discord.Interaction):
        if interaction.user.id != MASTER_ID:
            await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
            return

        state.chat_sessions.clear()
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        await interaction.response.send_message('✅ 所有頻道的聊天記憶已清除！', ephemeral=True)
        print('[RESET] 主人清除了所有聊天記憶。')

    @tree.command(name="清空知識庫", description="清空所有永久知識庫條目，無法復原。（主人限定）")
    async def slash_clear_kb(interaction: discord.Interaction):
        if interaction.user.id != MASTER_ID:
            await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
            return

        state.knowledge_entries.clear()
        with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)
        await interaction.response.send_message('✅ 知識庫已清空！', ephemeral=True)
        print('[RESET] 主人清空了知識庫。')
