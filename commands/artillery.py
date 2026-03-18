"""
炮決指令：/炮決蘿莉控、/炮決排行、/清除炮決名單
"""
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
    with open(_ARTILLERY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="炮決蘿莉控", description="從頻道成員隨機或指定一人炮決💀，並記錄累計被炮決次數")
    @app_commands.describe(用戶="指定炮決對象（不填則隨機）")
    async def slash_artillery(interaction: discord.Interaction, 用戶: discord.Member = None):
        channel = interaction.channel
        guild   = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        if 用戶 is not None:
            victim = 用戶
        else:
            members = [m for m in guild.members if not m.bot and channel.permissions_for(m).read_messages]
            if not members:
                await interaction.response.send_message('找不到可炮決的對象喵...', ephemeral=True)
                return
            victim = random.choice(members)

        uid = str(victim.id)
        gid = str(guild.id)

        records = _load_artillery()
        if gid not in records:
            records[gid] = {}
        records[gid][uid] = records[gid].get(uid, 0) + 1
        count = records[gid][uid]
        _save_artillery(records)

        nick = victim.display_name
        await interaction.response.send_message(
            f'今天炮決的是 {victim.mention}（{nick}）💀\n'
            f'（累計被炮決 **{count}** 次）',
            file=discord.File(_ARTILLERY_IMG))

    @tree.command(name="炮決排行", description="查看本伺服器被炮決次數 TOP10 排行榜💀")
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

        sorted_records = sorted(records[gid].items(), key=lambda x: x[1], reverse=True)
        lines = ['💀 **炮決排行榜** 💀']
        for i, (uid, cnt) in enumerate(sorted_records[:10], 1):
            member = guild.get_member(int(uid))
            name = member.display_name if member else f'（已離開 {uid}）'
            lines.append(f'`{i}.` {name} — **{cnt}** 次')
        await interaction.response.send_message('\n'.join(lines))

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
