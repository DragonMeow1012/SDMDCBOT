"""
/rank：所有排行榜整合於單一指令。

用法：/rank 選項:<功能> [清除類型]

選項：
    功德 / 炮決 / 調教 / 清除（清除需另填「清除類型」，主人限定）
"""
from __future__ import annotations

import os
from typing import Literal, Optional

import discord
from discord import app_commands

from config import MASTER_ID
from utils.json_store import load_json, save_json
from utils.discord_helpers import send_leaderboard


_MERIT_FILE     = os.path.join('data', 'merit.json')
_ARTILLERY_FILE = os.path.join('data', 'artillery_records.json')
_WHIP_FILE      = os.path.join('data', 'whip_records.json')


async def _send_error(interaction: discord.Interaction, msg: str) -> None:
    embed = discord.Embed(description=msg, color=discord.Color.red())
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _handle_clear(interaction: discord.Interaction, target: str | None):
    if interaction.user.id != MASTER_ID:
        await _send_error(interaction, '此選項限主人使用')
        return
    guild = interaction.guild
    if guild is None:
        await _send_error(interaction, '此指令只能在伺服器中使用')
        return
    if target not in ('功德', '炮決', '調教'):
        await _send_error(interaction, '清除需要另填「清除類型」（功德 / 炮決 / 調教）')
        return

    gid = str(guild.id)
    file_by_target = {
        '功德': _MERIT_FILE,
        '炮決': _ARTILLERY_FILE,
        '調教': _WHIP_FILE,
    }
    path = file_by_target[target]
    data = load_json(path)
    removed = data.pop(gid, None) is not None
    save_json(path, data)

    msg = (f'本伺服器 {target} 排行榜已清除' if removed
           else f'本伺服器 {target} 排行榜本來就是空的')
    embed = discord.Embed(
        description=msg,
        color=discord.Color.green() if removed else discord.Color.dark_grey(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


_RankOption = Literal['功德', '炮決', '調教', '清除']
_ClearTarget = Literal['功德', '炮決', '調教']


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='rank', description='查詢本群各種排行榜')
    @app_commands.describe(
        選項='要查詢或清除的排行榜',
        清除類型='選項=清除 時必填：要清除的排行榜種類',
    )
    async def slash_rank(
        interaction: discord.Interaction,
        選項: _RankOption,
        清除類型: Optional[_ClearTarget] = None,
    ):
        guild = interaction.guild
        if guild is None:
            await _send_error(interaction, '此指令只能在伺服器中使用')
            return

        gid = str(guild.id)
        if 選項 == '功德':
            records = load_json(_MERIT_FILE).get(gid, {})
            await send_leaderboard(interaction, records, '功德排行',
                                   color=discord.Color.dark_gold())
        elif 選項 == '炮決':
            records = load_json(_ARTILLERY_FILE).get(gid, {})
            await send_leaderboard(interaction, records, '炮決排行',
                                   color=discord.Color.dark_red())
        elif 選項 == '調教':
            records = load_json(_WHIP_FILE).get(gid, {})
            await send_leaderboard(interaction, records, '調教排行',
                                   color=discord.Color.red())
        elif 選項 == '清除':
            await _handle_clear(interaction, 清除類型)
