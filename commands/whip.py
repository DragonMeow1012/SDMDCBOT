"""
電子皮鞭指令：/電子皮鞭、/調教排行、/清除調教
"""
import os
import discord
from discord import app_commands

from config import MASTER_ID
from utils.json_store import load_json, save_json
from utils.discord_helpers import owner_only_button_check, format_leaderboard


_WHIP_FILE = os.path.join('data', 'whip_records.json')
_WHIP_REL_FILE = os.path.join('data', 'whip_relations.json')
_WHIP_IMG  = os.path.join('picture', 'whip.png')


def load_relations() -> dict:
    return load_json(_WHIP_REL_FILE)


def is_trainer_of(guild_id: int, trainer_id: int, trainee_id: int) -> bool:
    """檢查 trainer 是否為 trainee 的調教者。"""
    rels = load_relations()
    return rels.get(str(guild_id), {}).get(str(trainee_id)) == str(trainer_id)


# ── 輔助：執行調教並輸出結果 ──────────────────────────────────────────────────

async def _do_whip(send_fn, trainer: discord.Member, trainee: discord.Member, guild_id: int) -> None:
    gid = str(guild_id)
    uid = str(trainee.id)

    # 次數 +1
    records = load_json(_WHIP_FILE)
    records.setdefault(gid, {})[uid] = records.get(gid, {}).get(uid, 0) + 1
    save_json(_WHIP_FILE, records)

    # 建立關係
    rels = load_relations()
    rels.setdefault(gid, {})[uid] = str(trainer.id)
    save_json(_WHIP_REL_FILE, rels)

    text = (f'{trainee.mention} 被 {trainer.mention} 用皮鞭狠狠調教了，'
            f'現在是隻乖狗狗了❤️')
    if os.path.exists(_WHIP_IMG):
        await send_fn(text, file=discord.File(_WHIP_IMG))
    else:
        await send_fn(text)


# ── 確認 View ─────────────────────────────────────────────────────────────────

class WhipConfirmView(discord.ui.View):
    def __init__(self, trainer: discord.Member, trainee: discord.Member, guild_id: int):
        super().__init__(timeout=30)
        self.trainer  = trainer
        self.trainee  = trainee
        self.guild_id = guild_id

    @discord.ui.button(label='願意', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.trainee.id):
            return
        await interaction.response.edit_message(content='調教中...', view=None)
        await _do_whip(interaction.followup.send, self.trainer, self.trainee, self.guild_id)
        self.stop()

    @discord.ui.button(label='拒絕', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.trainee.id):
            return
        await interaction.response.edit_message(
            content=f'{self.trainee.mention} 拒絕了調教！', view=None)
        self.stop()


# ── 指令 ─────────────────────────────────────────────────────────────────────

def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="電子皮鞭", description="調教指定成員。已建立調教關係者無需確認直接執行")
    @app_commands.describe(
        用戶a="調教對象（只填此項）或調教者（同時填用戶b時）",
        用戶b="被調教對象（填此項時用戶a為調教者）",
    )
    async def slash_whip(
        interaction: discord.Interaction,
        用戶a: discord.Member,
        用戶b: discord.Member = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        trainer = 用戶a if 用戶b else interaction.user
        trainee = 用戶b if 用戶b else 用戶a

        if trainee.bot:
            await interaction.response.send_message('不能調教 Bot 喵！', ephemeral=True)
            return
        if trainee.id == interaction.user.id and 用戶b is None:
            await interaction.response.send_message('不能調教自己喵！', ephemeral=True)
            return

        # 已建立調教關係 → 直接執行
        if is_trainer_of(guild.id, trainer.id, trainee.id):
            await interaction.response.defer()
            await _do_whip(interaction.followup.send, trainer, trainee, guild.id)
            return

        # 需要確認
        view = WhipConfirmView(trainer, trainee, guild.id)
        await interaction.response.send_message(
            f'{trainee.mention}，你願意被 {trainer.mention} 調教嗎？',
            view=view,
        )

    @tree.command(name="調教排行", description="查看本伺服器被調教次數 TOP 10 排行榜")
    async def slash_whip_rank(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        records = load_json(_WHIP_FILE)
        gid = str(guild.id)
        if gid not in records or not records[gid]:
            await interaction.response.send_message('還沒有人被調教過喵！', ephemeral=True)
            return

        await interaction.response.defer()
        text = await format_leaderboard(records[gid], guild, '**調教排行榜**')
        await interaction.followup.send(text)

    @tree.command(name="清除調教", description="解除指定成員的調教關係（管理員/主人限定）")
    @app_commands.describe(被調教者="要解除調教關係的成員")
    async def slash_whip_clear(interaction: discord.Interaction, 被調教者: discord.Member):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        is_master = (interaction.user.id == MASTER_ID)
        is_admin  = interaction.user.guild_permissions.manage_guild
        if not is_master and not is_admin:
            await interaction.response.send_message('此指令限管理員或主人使用喵！', ephemeral=True)
            return

        rels = load_relations()
        gid  = str(guild.id)
        uid  = str(被調教者.id)
        if rels.get(gid, {}).pop(uid, None) is None:
            await interaction.response.send_message(
                f'{被調教者.mention} 目前沒有調教關係喵！', ephemeral=True)
            return

        save_json(_WHIP_REL_FILE, rels)
        await interaction.response.send_message(
            f'✅ 已解除 {被調教者.mention} 的調教關係。', ephemeral=True)
