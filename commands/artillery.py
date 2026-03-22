"""
炮決指令：/炮決蘿莉控、/炮決排行、/清除炮決名單
"""
import asyncio
import json
import os
import random
import discord
from discord import app_commands

from config import MASTER_ID


_ARTILLERY_FILE = os.path.join('data', 'artillery_records.json')
_ARTILLERY_IMG  = os.path.join('data', 'picture', 'artillerylolicon.jpg')


def _load_artillery() -> dict:
    if os.path.exists(_ARTILLERY_FILE):
        with open(_ARTILLERY_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_artillery(data: dict) -> None:
    os.makedirs(os.path.dirname(_ARTILLERY_FILE), exist_ok=True)
    with open(_ARTILLERY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="炮決蘿莉控", description="砲擊指定或隨機一位蘿莉控💀，並累計記錄被炮決次數")
    @app_commands.describe(用戶="砲擊對象（不填則從頻道隨機抽一人）")
    async def slash_artillery(interaction: discord.Interaction, 用戶: discord.Member = None):
        guild   = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
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
            members = (
                [m for m in channel.members if not m.bot]
                if isinstance(channel, discord.TextChannel)
                else [m for m in guild.members if not m.bot]
            )
            if not members:
                await interaction.followup.send('找不到可砲擊的對象喵...', ephemeral=True)
                return
            victim = random.choice(members)

        # ── 更新紀錄 ────────────────────────────────────────────
        uid = str(victim.id)
        gid = str(guild.id)
        records = _load_artillery()
        records.setdefault(gid, {})[uid] = records.get(gid, {}).get(uid, 0) + 1
        count = records[gid][uid]
        _save_artillery(records)

        # ── 回覆訊息 ────────────────────────────────────────────
        text = (
            f'💀 今天的蘿莉控是 {victim.mention}（{victim.display_name}）！\n'
            f'（累計被炮決 **{count}** 次）'
        )
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message

        if os.path.exists(_ARTILLERY_IMG):
            await send(text, file=discord.File(_ARTILLERY_IMG))
        else:
            await send(text)

    # ── 排行榜 ──────────────────────────────────────────────────
    @tree.command(name="炮決排行", description="查看本伺服器被炮決次數 TOP 10 排行榜💀")
    async def slash_artillery_rank(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        records = _load_artillery()
        gid = str(guild.id)
        if gid not in records or not records[gid]:
            await interaction.response.send_message('還沒有人被炮決過喵！', ephemeral=True)
            return

        top10 = sorted(records[gid].items(), key=lambda x: x[1], reverse=True)[:10]

        await interaction.response.defer()
        lines = ['💀 **炮決排行榜** 💀']
        for rank, (uid, cnt) in enumerate(top10, 1):
            member = guild.get_member(int(uid))
            if not member:
                try:
                    member = await guild.fetch_member(int(uid))
                except discord.NotFound:
                    pass
            name = member.display_name if member else f'（已離開：{uid}）'
            lines.append(f'`{rank}.` {name} — **{cnt}** 次')
        await interaction.followup.send('\n'.join(lines))

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
        records = _load_artillery()
        records.pop(str(guild.id), None)
        _save_artillery(records)
        await interaction.response.send_message('✅ 本伺服器的炮決名單已清除！', ephemeral=True)
