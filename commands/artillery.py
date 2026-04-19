"""
炮決指令：/炮決蘿莉控、/炮決排行、/清除炮決名單
"""
import asyncio
import os
import random
import discord
from discord import app_commands

from config import MASTER_ID
from utils.json_store import load_json, save_json
from utils.discord_helpers import format_leaderboard


_ARTILLERY_FILE = os.path.join('data', 'artillery_records.json')
_ARTILLERY_IMG  = os.path.join('picture', 'artillerylolicon.jpg')


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="炮決蘿莉控", description="砲擊指定或隨機一位蘿莉控💀，並累計記錄被炮決次數")
    @app_commands.describe(用戶="砲擊對象（不填則從頻道隨機抽一人）")
    async def slash_artillery(interaction: discord.Interaction, 用戶: discord.Member = None):
        guild   = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用！', color=discord.Color.red()),
                ephemeral=True
            )
            return

        # ── 決定受害者 ──────────────────────────────────────────
        if 用戶 is not None:
            victim = 用戶
        else:
            await interaction.response.defer()
            if not guild.chunked:
                try:
                    await asyncio.wait_for(guild.chunk(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            # 只從 guild.members 過濾，確保成員仍在伺服器（避免 stale cache 導致 mention 顯示成原始 ID）
            if isinstance(channel, discord.TextChannel):
                members = [
                    m for m in guild.members
                    if not m.bot and channel.permissions_for(m).view_channel
                ]
            else:
                members = [m for m in guild.members if not m.bot]
            if not members:
                await interaction.followup.send(
                    embed=discord.Embed(description='找不到可砲擊的對象喵...', color=discord.Color.red()),
                    ephemeral=True
                )
                return
            victim = random.choice(members)
            # 用 guild.get_member 重取最新 Member，避免抽到已離開伺服器的 stale 實例
            fresh = guild.get_member(victim.id)
            if fresh is None:
                try:
                    fresh = await guild.fetch_member(victim.id)
                except discord.HTTPException:
                    fresh = None
            if fresh is not None:
                victim = fresh

        # ── 更新紀錄 ────────────────────────────────────────────
        uid = str(victim.id)
        gid = str(guild.id)
        records = load_json(_ARTILLERY_FILE)
        records.setdefault(gid, {})[uid] = records.get(gid, {}).get(uid, 0) + 1
        count = records[gid][uid]
        save_json(_ARTILLERY_FILE, records)

        # ── 回覆訊息 ────────────────────────────────────────────
        text = (
            f'💀 今天的蘿莉控是 {victim.mention}！\n'
            f'（累計被炮決 **{count}** 次）'
        )
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message

        if os.path.exists(_ARTILLERY_IMG):
            embed = discord.Embed(description=text, color=discord.Color.dark_red())
            embed.set_image(url='attachment://artillerylolicon.jpg')
            await send(embed=embed, file=discord.File(_ARTILLERY_IMG, filename='artillerylolicon.jpg'))
        else:
            await send(embed=discord.Embed(description=text, color=discord.Color.dark_red()))

    # ── 排行榜 ──────────────────────────────────────────────────
    @tree.command(name="炮決排行", description="查看本伺服器被炮決次數 TOP 10 排行榜💀")
    async def slash_artillery_rank(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        records = load_json(_ARTILLERY_FILE)
        gid = str(guild.id)
        if gid not in records or not records[gid]:
            await interaction.response.send_message('還沒有人被炮決過喵！', ephemeral=True)
            return

        await interaction.response.defer()
        text = await format_leaderboard(records[gid], guild, '💀 **炮決排行榜** 💀')
        await interaction.followup.send(text)

    # ── 清除紀錄（主人限定） ────────────────────────────────────
    @tree.command(name="清除炮決名單", description="清除本伺服器的所有炮決記錄，無法復原。（主人限定）")
    async def slash_artillery_clear(interaction: discord.Interaction):
        if interaction.user.id != MASTER_ID:
            await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return
        records = load_json(_ARTILLERY_FILE)
        records.pop(str(guild.id), None)
        save_json(_ARTILLERY_FILE, records)
        await interaction.response.send_message('✅ 本伺服器的炮決名單已清除！', ephemeral=True)
