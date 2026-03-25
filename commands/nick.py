"""
暱稱指令：/nick
"""
import discord
from discord import app_commands

from config import MASTER_ID
from nicknames import save_nicknames
import state


_MASTER_KEYWORDS = frozenset({
    '主人', '主子', '主宰', '老大', '大人', '主', 'master', 'Master', 'MASTER',
})


def _contains_master_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in _MASTER_KEYWORDS)


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="nick", description="設定你的暱稱，模型會優先用暱稱稱呼你。主人可指定對象。")
    @app_commands.describe(暱稱="要設定的暱稱", 對象="目標成員（主人限定，預設為自己）")
    async def slash_nick(interaction: discord.Interaction, 暱稱: str, 對象: discord.Member = None):
        is_master = (interaction.user.id == MASTER_ID)
        target = 對象 or interaction.user

        if target.id != interaction.user.id and not is_master:
            await interaction.response.send_message('你只能設定自己的暱稱喵！', ephemeral=True)
            return

        if not is_master and _contains_master_keyword(暱稱):
            await interaction.response.send_message('暱稱不能包含主人相關詞彙喵！', ephemeral=True)
            return

        state.nicknames[str(target.id)] = 暱稱
        save_nicknames(state.nicknames)

        if target.id == interaction.user.id:
            await interaction.response.send_message(f'好的，我會記住你叫「{暱稱}」！', ephemeral=True)
        else:
            await interaction.response.send_message(
                f'已將 {target.mention} 的暱稱設為「{暱稱}」。', ephemeral=True)
