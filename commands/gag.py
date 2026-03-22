"""
電子口球指令：/電子口球、/口球輪盤
"""
import asyncio
import datetime
import random
import discord
from discord import app_commands

from config import MASTER_ID

_MAX_SECONDS = 2419200   # Discord timeout 上限 28 天


async def _apply_gag(target: discord.Member, seconds: int) -> str | None:
    """套用 Timeout。成功回傳 None，失敗回傳錯誤訊息。"""
    try:
        await target.timeout(datetime.timedelta(seconds=seconds), reason='電子口球')
        return None
    except discord.Forbidden:
        return 'Bot 缺少「管理成員」權限，請在伺服器設定中授予此權限喵！'
    except discord.HTTPException as e:
        return f'套用口球失敗：{e}'


class GagConfirmView(discord.ui.View):
    """請求對方同意後才套用 Timeout 的確認按鈕。"""

    def __init__(self, requester: discord.Member, target: discord.Member, seconds: int):
        super().__init__(timeout=30)
        self.requester = requester
        self.target    = target
        self.seconds   = seconds

    @discord.ui.button(label='同意', style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        err = await _apply_gag(self.target, self.seconds)
        msg = (err or f'{self.target.mention} 已戴上電子口球 {self.seconds} 秒！')
        await interaction.response.edit_message(content=msg, view=None)
        self.stop()

    @discord.ui.button(label='拒絕', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f'{self.target.mention} 拒絕了電子口球！', view=None)
        self.stop()


class RouletteView(discord.ui.View):
    """口球輪盤報名按鈕。"""

    def __init__(self):
        super().__init__(timeout=60)
        self.participants: list[discord.Member] = []
        self.closed = False

    @discord.ui.button(label='參加輪盤', style=discord.ButtonStyle.danger)
    async def join(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if self.closed:
            await interaction.response.send_message('報名已結束喵！', ephemeral=True)
            return
        if any(m.id == interaction.user.id for m in self.participants):
            await interaction.response.send_message('你已經報名了喵！', ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message('Bot 不能參加輪盤喵！', ephemeral=True)
            return
        self.participants.append(interaction.user)
        await interaction.response.edit_message(
            content=f'**口球輪盤開始！**\n'
                    f'60 秒內點下方按鈕報名，時間到將從參加者中隨機抽出一人戴上電子口球 30 秒！\n\n'
                    f'目前報名：{len(self.participants)} 人',
        )
        await interaction.followup.send('已報名！', ephemeral=True)

    async def on_timeout(self):
        self.closed = True
        self.stop()


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="電子口球", description="對成員套用禁言（Timeout）。主人可直接執行，對他人需對方確認")
    @app_commands.describe(秒數="禁言持續秒數（1 ~ 2419200）", 對象="目標成員（不填則對自己）")
    async def slash_gag(
        interaction: discord.Interaction,
        秒數: app_commands.Range[int, 1, _MAX_SECONDS],
        對象: discord.Member = None,
    ):
        target    = 對象 or interaction.user
        is_master = (interaction.user.id == MASTER_ID)
        is_self   = (target.id == interaction.user.id)

        if target.bot:
            await interaction.response.send_message('不能對 Bot 套口球喵！', ephemeral=True)
            return

        # 主人或自願 → 直接套用
        if is_master or is_self:
            err = await _apply_gag(target, 秒數)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
            else:
                await interaction.response.send_message(
                    f'{target.mention} 已戴上電子口球 {秒數} 秒！',
                    ephemeral=is_self and not is_master,
                )
            return

        # 需要對方同意
        view = GagConfirmView(interaction.user, target, 秒數)
        await interaction.response.send_message(
            f'{target.mention}，{interaction.user.mention} 想幫你戴上電子口球 {秒數} 秒，你同意嗎？',
            view=view,
        )

    @tree.command(name="口球輪盤", description="開啟口球輪盤！60 秒報名，時間到從參加者隨機抽一人禁言 30 秒")
    async def slash_roulette(interaction: discord.Interaction):
        view = RouletteView()
        await interaction.response.send_message(
            '**口球輪盤開始！**\n'
            '60 秒內點下方按鈕報名，時間到將從參加者中隨機抽出一人戴上電子口球 30 秒！',
            view=view,
        )

        await asyncio.sleep(60)
        view.closed = True

        if not view.participants:
            await interaction.edit_original_response(
                content='**口球輪盤結束**\n...沒有人報名，輪盤空轉了喵。', view=None)
            return

        victim   = random.choice(view.participants)
        mentions = '、'.join(m.mention for m in view.participants)
        err      = await _apply_gag(victim, 30)

        if err:
            result = f'抽中了 {victim.mention}，但是... {err}'
        else:
            result = f'恭喜 {victim.mention} 獲得電子口球 30 秒！'

        await interaction.edit_original_response(
            content=f'**輪盤結束！** 參加者：{mentions}\n{result}',
            view=None,
        )
