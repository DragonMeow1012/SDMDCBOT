"""
電子口球指令：/電子口球、/口球輪盤
"""
import asyncio
import datetime
import random
import discord
from discord import app_commands

from config import MASTER_ID


async def apply_gag(target: discord.Member, duration: int) -> str | None:
    """套用全伺服器禁言。成功回傳 None，失敗回傳錯誤訊息。"""
    try:
        await target.timeout(datetime.timedelta(seconds=duration), reason='電子口球')
        return None
    except discord.Forbidden:
        return '喵嗚... Bot 缺少「管理成員」權限，請在伺服器設定中授予 Bot 此權限！'


class GagConfirmView(discord.ui.View):
    def __init__(self, target: discord.Member, duration: int):
        super().__init__(timeout=30)
        self.target = target
        self.duration = duration

    @discord.ui.button(label='同意 🔇', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        err = await apply_gag(self.target, self.duration)
        if err:
            await interaction.response.edit_message(content=err, view=None)
        else:
            await interaction.response.edit_message(
                content=f'🔇 {self.target.mention} 已戴上電子口球 {self.duration} 秒！', view=None)
        self.stop()

    @discord.ui.button(label='拒絕 ❌', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f'❌ {self.target.mention} 拒絕了電子口球！', view=None)
        self.stop()


class RouletteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.participants: list[discord.Member] = []
        self.closed = False

    @discord.ui.button(label='參加輪盤 ', style=discord.ButtonStyle.danger)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.closed:
            await interaction.response.send_message('報名已結束！', ephemeral=True)
            return
        if any(m.id == interaction.user.id for m in self.participants):
            await interaction.response.send_message('你已經報名了喵！', ephemeral=True)
            return
        self.participants.append(interaction.user)
        await interaction.response.send_message(
            f'✅ 已報名！目前 {len(self.participants)} 人參加。', ephemeral=True)

    async def on_timeout(self):
        self.closed = True
        self.stop()


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="電子口球", description="對成員套用全伺服器禁言（Timeout）。主人可直接執行，對他人需對方確認🔇")
    @app_commands.describe(time="持續秒數", who="目標（預設為自己）")
    async def slash_gag(interaction: discord.Interaction, time: int, who: discord.Member = None):
        target = who or interaction.user
        is_master = (interaction.user.id == MASTER_ID)
        is_self = (target.id == interaction.user.id)

        if time <= 0:
            await interaction.response.send_message('秒數必須大於 0 喵！', ephemeral=True)
            return

        if is_master or is_self:
            err = await apply_gag(target, time)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
            else:
                await interaction.response.send_message(
                    f'🔇 {target.mention} 已戴上電子口球 {time} 秒！', ephemeral=is_self)
            return

        view = GagConfirmView(target, time)
        await interaction.response.send_message(
            f'{target.mention}，{interaction.user.mention} 想幫你戴上電子口球 {time} 秒，你同意嗎？',
            view=view)

    @tree.command(name="口球輪盤", description="開啟口球輪盤！1分鐘報名，時間到從參加者隨機抽一人禁言 30 秒💀")
    async def slash_roulette(interaction: discord.Interaction):
        view = RouletteView()
        await interaction.response.send_message(
            ' **口球輪盤開始！**\n1分鐘內點下方按鈕報名，時間到將從參加者中隨機抽出一人戴上電子口球 30 秒！💀',
            view=view)

        await asyncio.sleep(60)
        view.closed = True

        if not view.participants:
            await interaction.edit_original_response(
                content=' **口球輪盤結束**\n...沒有人報名，輪盤空轉了喵。', view=None)
            return

        victim = random.choice(view.participants)
        mentions = '、'.join(m.mention for m in view.participants)

        err = await apply_gag(victim, 30)
        if err:
            await interaction.edit_original_response(
                content=f' **輪盤結束！** 參加者：{mentions}\n抽中了 {victim.mention}，但是... {err}', view=None)
        else:
            await interaction.edit_original_response(
                content=f' **輪盤結束！** 參加者：{mentions}\n💀 恭喜 {victim.mention} 獲得電子口球 30 秒！',
                view=None)
