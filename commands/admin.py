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

    @tree.command(name="清除記憶", description="當小龍喵對話被安全過濾卡住時使用，清除本頻道的聊天記憶讓對話重新開始。")
    async def slash_clear_memory(interaction: discord.Interaction):
        cid = interaction.channel_id
        state.chat_sessions.pop(cid, None)

        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data.pop(str(cid), None)
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        await interaction.response.send_message('✅ 本頻道的聊天記憶已清除，下次對話將重新開始喵！', ephemeral=True)
        print(f'[RESET] {interaction.user} 清除了頻道 {cid} 的聊天記憶。')

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
