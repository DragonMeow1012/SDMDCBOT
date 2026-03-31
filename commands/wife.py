"""
今日老婆指令：/抽今日老婆、/和今日老婆離婚
老婆關係維持 24 小時後自動失效。
"""
import asyncio
import io
import json
import os
from datetime import datetime, timezone, timedelta
import random
import discord
from discord import app_commands


_WIFE_FILE  = os.path.join('data', 'wife_records.json')
_LOVE_EMOJI = '<:klllove:1486300373068152832>'
_DAY_KEY_FMT = "%Y-%m-%d"


def _load_wife() -> dict:
    if os.path.exists(_WIFE_FILE):
        with open(_WIFE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_wife(data: dict) -> None:
    with open(_WIFE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _today_key() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime(_DAY_KEY_FMT)


def _record_day_key(rec: dict) -> str | None:
    if "date" in rec:
        return rec.get("date")
    ts = rec.get("timestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime(_DAY_KEY_FMT)
    except Exception:
        return None


def _purge_expired(data: dict) -> dict:
    """移除所有已過期（跨日）的紀錄。"""
    today = _today_key()
    for gid in list(data):
        for uid in list(data[gid]):
            if _record_day_key(data[gid][uid]) != today:
                del data[gid][uid]
        if not data[gid]:
            del data[gid]
    return data


def get_active_wife_rels(guild_id: int) -> dict[str, str]:
    """
    回傳目前有效的老婆關係 {husband_id: wife_id}，供關係圖使用。
    """
    data   = _purge_expired(_load_wife())
    gid    = str(guild_id)
    result = {}
    for uid, rec in data.get(gid, {}).items():
        if _record_day_key(rec) == _today_key():
            result[uid] = rec['wife_id']
    return result


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="抽今日老婆", description="從本群隨機抽一位成員作為你的老婆（當日只會抽一次）💕")
    async def slash_draw_wife(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用！', color=discord.Color.red()),
                ephemeral=True
            )
            return

        await interaction.response.defer()

        if not guild.chunked:
            try:
                await asyncio.wait_for(guild.chunk(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        candidates = [
            m for m in guild.members
            if not m.bot and m.id != interaction.user.id
        ]
        if not candidates:
            await interaction.followup.send(
                embed=discord.Embed(description='找不到可以抽的對象喵QQ', color=discord.Color.red()),
                ephemeral=True
            )
            return

        # 儲存與讀取（當日有抽過就直接沿用）
        gid  = str(guild.id)
        uid  = str(interaction.user.id)
        data = _purge_expired(_load_wife())
        rec  = data.get(gid, {}).get(uid)

        wife = None
        wife_id = None
        if rec is not None and _record_day_key(rec) == _today_key():
            wife_id = rec.get('wife_id')
            if wife_id:
                wife = guild.get_member(int(wife_id))
                if wife is None:
                    try:
                        wife = await guild.fetch_member(int(wife_id))
                    except discord.NotFound:
                        wife = None
        else:
            wife    = random.choice(candidates)
            wife_id = str(wife.id)
            data.setdefault(gid, {})[uid] = {
                'date':    _today_key(),
                'wife_id': wife_id,
            }
            _save_wife(data)

        # 取得頭像
        if wife is not None:
            name       = wife.display_name
            avatar_url = str(wife.display_avatar.replace(size=512).url)
        else:
            name       = f'<@{wife_id}>' if wife_id else '對方'
            avatar_url = None

        import requests as _req
        if avatar_url:
            try:
                resp         = await asyncio.to_thread(_req.get, avatar_url, timeout=8)
                avatar_bytes = resp.content if resp.status_code == 200 else None
            except Exception:
                avatar_bytes = None
        else:
            avatar_bytes = None

        text = f'你今天的老婆是：**{name}**\n要好好對待她哦{_LOVE_EMOJI}'
        embed = discord.Embed(description=text, color=discord.Color.pink())
        if avatar_bytes:
            await interaction.followup.send(
                embed=embed.set_image(url='attachment://wife.png'),
                file=discord.File(io.BytesIO(avatar_bytes), filename='wife.png'),
            )
        else:
            await interaction.followup.send(embed=embed)

    @tree.command(name="強娶老婆", description="強制指定一位成員作為你的老婆，取代原有老婆")
    @app_commands.describe(用戶="要強娶的對象")
    async def slash_force_wife(interaction: discord.Interaction, 用戶: discord.Member):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用！', color=discord.Color.red()),
                ephemeral=True
            )
            return
        if 用戶.bot:
            await interaction.response.send_message(
                embed=discord.Embed(description='不能強娶 Bot 喵！', color=discord.Color.red()),
                ephemeral=True
            )
            return
        if 用戶.id == interaction.user.id:
            await interaction.response.send_message(
                embed=discord.Embed(description='不能娶自己喵！', color=discord.Color.red()),
                ephemeral=True
            )
            return

        gid  = str(guild.id)
        uid  = str(interaction.user.id)
        data = _purge_expired(_load_wife())
        data.setdefault(gid, {})[uid] = {
            'date':    _today_key(),
            'wife_id': str(用戶.id),
        }
        _save_wife(data)

        text = f' {interaction.user.mention} 強娶了 {用戶.mention} 作為老婆！{_LOVE_EMOJI}'
        await interaction.response.send_message(
            embed=discord.Embed(description=text, color=discord.Color.pink())
        )

    @tree.command(name="拋棄婚約", description="解除指定用戶對你的婚姻關係💔")
    @app_commands.describe(用戶="要拋棄的對象")
    async def slash_abandon_wife(interaction: discord.Interaction, 用戶: discord.Member):
        gid    = str(interaction.guild_id)
        uid    = str(用戶.id)
        my_id  = str(interaction.user.id)
        data   = _purge_expired(_load_wife())

        rec = data.get(gid, {}).get(uid)
        if rec is None or rec.get('wife_id') != my_id:
            await interaction.response.send_message(
                embed=discord.Embed(description='對方跟你沒有婚姻關係喵！', color=discord.Color.red()),
                ephemeral=True
            )
            return

        data[gid].pop(uid)
        _save_wife(data)
        text = f'{interaction.user.mention} 跟 {用戶.mention} 離婚了<:crycat:1486308949173997730>'
        await interaction.response.send_message(
            embed=discord.Embed(description=text, color=discord.Color.red())
        )

    @tree.command(name="和今日老婆離婚", description="與目前的老婆離婚💔")
    async def slash_divorce(interaction: discord.Interaction):
        gid  = str(interaction.guild_id)
        uid  = str(interaction.user.id)
        data = _purge_expired(_load_wife())

        if uid not in data.get(gid, {}):
            await interaction.response.send_message(
                embed=discord.Embed(description='你目前沒有老婆喵！', color=discord.Color.red()),
                ephemeral=True
            )
            return

        wife_id = data[gid][uid].get('wife_id')
        data[gid].pop(uid)
        _save_wife(data)
        wife_name = '對方'
        if wife_id and interaction.guild:
            member = interaction.guild.get_member(int(wife_id))
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(int(wife_id))
                except discord.NotFound:
                    member = None
            if member:
                wife_name = member.display_name
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f'你已和{wife_name}離婚了<:crycat:1486308949173997730>',
                color=discord.Color.red()
            )
        )
