"""
共用 Discord 輔助函式。
提取指令模組中重複的按鈕權限檢查、排行榜格式化、成員查找等邏輯。
"""
import discord


async def owner_only_button_check(interaction: discord.Interaction, owner_id: int) -> bool:
    """
    檢查按下按鈕的使用者是否為指定的 owner。
    若不是，自動回覆拒絕訊息並回傳 False。
    """
    if interaction.user.id == owner_id:
        return True
    await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
    return False


async def get_member_safe(guild: discord.Guild, uid: int) -> discord.Member | None:
    """先從快取取成員，失敗再用 API fetch，都找不到回傳 None。"""
    member = guild.get_member(uid)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(uid)
    except discord.NotFound:
        return None


async def format_leaderboard(
    records: dict[str, int],
    guild: discord.Guild,
    title: str,
    limit: int = 10,
) -> str:
    """
    將 {uid_str: count} 格式化為排行榜文字。
    自動解析成員顯示名稱，找不到的標記為「已離開」。
    """
    top = sorted(records.items(), key=lambda x: x[1], reverse=True)[:limit]
    lines = [title]
    for rank, (uid, cnt) in enumerate(top, 1):
        member = await get_member_safe(guild, int(uid))
        name = member.display_name if member else f'（已離開：{uid}）'
        lines.append(f'`{rank}.` {name} — **{cnt}** 次')
    return '\n'.join(lines)
