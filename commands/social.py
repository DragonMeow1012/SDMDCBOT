"""
社交指令：/認養寵物、/認主人、/本群關係圖、/賽博釣群友
"""
import json
import os
import discord
from discord import app_commands


_REL_FILE = os.path.join('data', 'relationships.json')


def _load_rel() -> dict:
    if os.path.exists(_REL_FILE):
        with open(_REL_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_rel(data: dict) -> None:
    with open(_REL_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_name(guild: discord.Guild, uid: str) -> str:
    member = guild.get_member(int(uid))
    return member.display_name if member else f'用戶{uid}'


class RelationView(discord.ui.View):
    """通用認養/認主人確認按鈕。mode: 'pet'=認養寵物, 'master'=認主人"""
    def __init__(self, requester: discord.Member, target: discord.Member,
                 guild_id: int, mode: str):
        super().__init__(timeout=60)
        self.requester = requester
        self.target    = target
        self.guild_id  = guild_id
        self.mode      = mode

    @discord.ui.button(label='接受 ✅', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return

        data   = _load_rel()
        gid    = str(self.guild_id)
        req_id = str(self.requester.id)
        tgt_id = str(self.target.id)
        if gid not in data:
            data[gid] = {}

        if self.mode == 'pet':
            data[gid][tgt_id] = req_id
            msg = f'🐾 {self.target.mention} 成為了 {self.requester.mention} 的寵物！'
        else:
            data[gid][req_id] = tgt_id
            msg = f'🐾 {self.requester.mention} 成為了 {self.target.mention} 的寵物！'

        _save_rel(data)
        await interaction.response.edit_message(content=msg, view=None)
        self.stop()

    @discord.ui.button(label='拒絕 ❌', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        await interaction.response.edit_message(content='❌ 對方拒絕了喵。', view=None)
        self.stop()


class FishingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label='咬鉤 🪝', style=discord.ButtonStyle.danger)
    async def bite(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        user = interaction.user

        try:
            hooks = await channel.webhooks()
            wh = next((h for h in hooks if h.name == '賽博釣魚'), None)
            if wh is None:
                wh = await channel.create_webhook(name='賽博釣魚')
        except discord.Forbidden:
            await interaction.followup.send('Bot 缺少管理 Webhook 的權限喵！', ephemeral=True)
            return

        avatar_url = user.display_avatar.replace(size=256).url
        display_name = user.display_name

        await wh.send('我是小男娘', username=display_name, avatar_url=avatar_url)
        await interaction.followup.send('🎣 上鉤了！', ephemeral=True)


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="認養寵物", description="邀請指定用戶成為你的寵物，對方同意後建立主寵關係🐾")
    @app_commands.describe(用戶="要認養的對象")
    async def slash_adopt(interaction: discord.Interaction, 用戶: discord.Member):
        if 用戶.id == interaction.user.id:
            await interaction.response.send_message('不能認養自己喵！', ephemeral=True)
            return
        if 用戶.bot:
            await interaction.response.send_message('不能認養 Bot 喵！', ephemeral=True)
            return
        req_name = interaction.user.display_name
        view = RelationView(interaction.user, 用戶, interaction.guild_id, mode='pet')
        await interaction.response.send_message(
            f'{用戶.mention}，{interaction.user.mention}（{req_name}）想認養你為寵物，你願意嗎？🐾',
            view=view)

    @tree.command(name="認主人", description="邀請指定用戶成為你的主人，對方同意後建立主寵關係🐾")
    @app_commands.describe(用戶="要認作主人的對象")
    async def slash_find_master(interaction: discord.Interaction, 用戶: discord.Member):
        if 用戶.id == interaction.user.id:
            await interaction.response.send_message('不能認自己為主人喵！', ephemeral=True)
            return
        if 用戶.bot:
            await interaction.response.send_message('不能認 Bot 為主人喵！', ephemeral=True)
            return
        req_name = interaction.user.display_name
        view = RelationView(interaction.user, 用戶, interaction.guild_id, mode='master')
        await interaction.response.send_message(
            f'{用戶.mention}，{interaction.user.mention}（{req_name}）想認你為主人，你願意嗎？🐾',
            view=view)

    @tree.command(name="本群關係圖", description="以樹狀圖顯示本伺服器所有用戶的主人與寵物關係🐾👑")
    async def slash_rel_map(interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
            return

        data = _load_rel()
        gid  = str(guild.id)
        rels = data.get(gid, {})
        if not rels:
            await interaction.response.send_message('本群還沒有任何主寵關係喵！', ephemeral=True)
            return

        master_map: dict[str, list[str]] = {}
        for pet_id, master_id in rels.items():
            master_map.setdefault(master_id, []).append(pet_id)

        lines = ['🐾 **本群主寵關係圖**']
        visited: set[str] = set()

        def build_tree(uid: str, depth: int):
            indent = '　' * depth
            name = _get_name(guild, uid)
            tag = '👑' if uid in master_map else '🐾'
            lines.append(f'{indent}{tag} {name}')
            visited.add(uid)
            for pet in master_map.get(uid, []):
                if pet not in visited:
                    build_tree(pet, depth + 1)

        roots = [m for m in master_map if m not in rels]
        for root in roots:
            build_tree(root, 0)

        orphans = [p for p in rels if p not in visited]
        if orphans:
            lines.append('\n**— 其他關係 —**')
            for pet_id in orphans:
                master_id = rels[pet_id]
                lines.append(f'🐾 {_get_name(guild, pet_id)} → 主人：{_get_name(guild, master_id)}')

        await interaction.response.send_message('\n'.join(lines))

    @tree.command(name="賽博釣群友", description="放出釣魚按鈕，點下「咬鉤」的人會被 Webhook 偽裝發出一則訊息🪝")
    async def slash_fishing(interaction: discord.Interaction):
        view = FishingView()
        await interaction.response.send_message(
            '🎣 **賽博釣魚中...**\n有人敢點嗎？', view=view)
