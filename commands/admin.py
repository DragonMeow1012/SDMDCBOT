"""
管理員指令：/清除記憶
"""
import json
import discord
from discord import app_commands

from config import HISTORY_FILE
import state


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='清除記憶', description='當小龍喵對話被安全過濾卡住時使用，清除本頻道聊天記憶')
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

        embed = discord.Embed(
            title='清除記憶',
            description='本頻道的聊天記憶已清除，下次對話將重新開始',
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        print(f'[RESET] {interaction.user} 清除了頻道 {cid} 的聊天記憶。')
