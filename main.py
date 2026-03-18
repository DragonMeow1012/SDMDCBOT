"""
Discord Bot 主程式入口。
負責：Discord 事件處理、對話 session 管理、URL 偵測與分派。

啟動方式：
    PYTHONIOENCODING=utf-8 python -u main.py
"""
import os
import sys
import re
import asyncio
import datetime
import discord
from discord import app_commands

# 修正 Windows cp932 編碼問題：使用 line_buffering=True 避免破壞 -u 無緩衝模式
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

from config import DISCORD_TOKEN, MASTER_ID
from history import load_history, save_history
from web import fetch_url
from gemini_worker import create_chat, msg_queue, gemini_worker, analyze_for_kb
from nicknames import (
    load_nicknames, save_nicknames,
    build_all_nicknames_summary,
)
from knowledge import (
    load_knowledge, add_entry, remove_entry, search_entries,
    build_knowledge_context, consolidate_knowledge,
    list_sections, remove_section,
)
from reverse_search import reverse_image_search
from summary import load_summary

# --- Discord Client ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# chat_sessions 結構：
#   { channel_id (int) -> {
#       'chat_obj': Chat | None,
#       'personality': str | None,
#       'raw_history': list,
#       'current_web_context': str | None,
#   }}
chat_sessions: dict = {}
nicknames: dict[str, str] = {}        # { user_id_str -> nickname }
knowledge_entries: list[dict] = []    # 知識庫條目
_worker_started: bool = False


# ---------------------------------------------------------------------------
# 全域斜線指令
# ---------------------------------------------------------------------------
@tree.command(name="nick", description="設定你的暱稱，模型會優先用暱稱稱呼你。主人可指定對象。")
@app_commands.describe(暱稱="要設定的暱稱", 對象="目標成員（主人限定，預設為自己）")
async def slash_nick(interaction: discord.Interaction, 暱稱: str, 對象: discord.Member = None):
    is_master = (interaction.user.id == MASTER_ID)
    target = 對象 or interaction.user

    if target.id != interaction.user.id and not is_master:
        await interaction.response.send_message('你只能設定自己的暱稱喵！', ephemeral=True)
        return

    nicknames[str(target.id)] = 暱稱
    save_nicknames(nicknames)

    if target.id == interaction.user.id:
        await interaction.response.send_message(f'好的，我會記住你叫「{暱稱}」！', ephemeral=True)
    else:
        await interaction.response.send_message(f'已將 {target.mention} 的暱稱設為「{暱稱}」。', ephemeral=True)


@tree.command(name="清除記憶", description="清除所有頻道的聊天歷史，下次對話將重新開始。（主人限定）")
async def slash_clear_memory(interaction: discord.Interaction):
    if interaction.user.id != MASTER_ID:
        await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
        return

    chat_sessions.clear()
    import json as _json
    from config import HISTORY_FILE
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        _json.dump({}, f)
    await interaction.response.send_message('✅ 所有頻道的聊天記憶已清除！', ephemeral=True)
    print('[RESET] 主人清除了所有聊天記憶。')


@tree.command(name="清空知識庫", description="清空所有永久知識庫條目，無法復原。（主人限定）")
async def slash_clear_kb(interaction: discord.Interaction):
    global knowledge_entries
    if interaction.user.id != MASTER_ID:
        await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
        return

    knowledge_entries.clear()
    import json as _json
    from knowledge import KNOWLEDGE_FILE
    with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
        _json.dump([], f)
    await interaction.response.send_message('✅ 知識庫已清空！', ephemeral=True)
    print('[RESET] 主人清空了知識庫。')


class RouletteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.participants: list[discord.Member] = []
        self.closed = False

    @discord.ui.button(label='參加輪盤 🎰', style=discord.ButtonStyle.danger)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.closed:
            await interaction.response.send_message('報名已結束！', ephemeral=True)
            return
        if any(m.id == interaction.user.id for m in self.participants):
            await interaction.response.send_message('你已經報名了喵！', ephemeral=True)
            return
        self.participants.append(interaction.user)
        await interaction.response.send_message(
            f'✅ {interaction.user.mention} 已報名！目前 {len(self.participants)} 人參加。',
            ephemeral=False)

    async def on_timeout(self):
        self.closed = True
        self.stop()


@tree.command(name="口球輪盤", description="開啟口球輪盤！1分鐘報名，時間到從參加者隨機抽一人禁言 30 秒💀")
async def slash_roulette(interaction: discord.Interaction):
    view = RouletteView()
    await interaction.response.send_message(
        '🎰 **口球輪盤開始！**\n1分鐘內點下方按鈕報名，時間到將從參加者中隨機抽出一人戴上電子口球 30 秒！💀',
        view=view)

    await asyncio.sleep(60)
    view.closed = True

    if not view.participants:
        await interaction.edit_original_response(
            content='🎰 **口球輪盤結束**\n...沒有人報名，輪盤空轉了喵。', view=None)
        return

    import random
    victim = random.choice(view.participants)
    mentions = '、'.join(m.mention for m in view.participants)

    err = await _apply_gag(victim, 30)
    if err:
        await interaction.edit_original_response(
            content=f'🎰 **輪盤結束！** 參加者：{mentions}\n抽中了 {victim.mention}，但是... {err}', view=None)
    else:
        await interaction.edit_original_response(
            content=f'🎰 **輪盤結束！** 參加者：{mentions}\n💀 恭喜 {victim.mention} 獲得電子口球 30 秒！', view=None)


class QuoteToggleView(discord.ui.View):
    def __init__(self, avatar_url: str, quote: str, author_name: str, author_id: int, grayscale: bool = True):
        super().__init__(timeout=120)
        self.avatar_url = avatar_url
        self.quote = quote
        self.author_name = author_name
        self.author_id = author_id
        self.grayscale = grayscale
        self._update_label()

    def _update_label(self):
        self.toggle_btn.label = '切換彩色 🎨' if self.grayscale else '切換黑白 ⬛'

    @discord.ui.button(label='切換彩色 🎨', style=discord.ButtonStyle.secondary)
    async def toggle_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        from quote_image import make_quote_image
        import io
        self.grayscale = not self.grayscale
        self._update_label()
        await interaction.response.defer()
        img_bytes = await asyncio.get_running_loop().run_in_executor(
            None, lambda: make_quote_image(
                self.avatar_url, self.quote, self.author_name, self.author_id, grayscale=self.grayscale))
        await interaction.edit_original_response(
            attachments=[discord.File(io.BytesIO(img_bytes), filename='quote.png')],
            view=self)


@tree.command(name="名言佳句", description="輸入一段話，生成帶有用戶頭像的名言佳句圖片，支援黑白/彩色切換。")
@app_commands.describe(文字="名言內容", 用戶="頭像主角（預設為自己）")
async def slash_quote(interaction: discord.Interaction, 文字: str, 用戶: discord.Member = None):
    from quote_image import make_quote_image
    import io
    await interaction.response.defer()

    target = 用戶 or interaction.user
    avatar_url = target.display_avatar.replace(size=512).url
    nick = nicknames.get(str(target.id)) or target.display_name

    img_bytes = await asyncio.get_running_loop().run_in_executor(
        None, lambda: make_quote_image(avatar_url, 文字, nick, target.id))

    view = QuoteToggleView(avatar_url, 文字, nick, target.id, grayscale=True)
    await interaction.followup.send(
        file=discord.File(io.BytesIO(img_bytes), filename='quote.png'),
        view=view)


@tree.context_menu(name="名言佳句")
async def ctx_quote(interaction: discord.Interaction, message: discord.Message):
    """右鍵訊息 → Apps → 名言佳句，直接以該訊息內容生圖。"""
    from quote_image import make_quote_image
    import io

    text = message.content.strip()
    if not text:
        await interaction.response.send_message('這則訊息沒有文字內容喵！', ephemeral=True)
        return

    await interaction.response.defer()

    target = message.author
    avatar_url = target.display_avatar.replace(size=512).url
    nick = nicknames.get(str(target.id)) or (target.display_name if isinstance(target, discord.Member) else target.name)

    img_bytes = await asyncio.get_running_loop().run_in_executor(
        None, lambda: make_quote_image(avatar_url, text, nick, target.id))

    view = QuoteToggleView(avatar_url, text, nick, target.id, grayscale=True)
    await interaction.followup.send(
        file=discord.File(io.BytesIO(img_bytes), filename='quote.png'),
        view=view)


@tree.command(name="電子氣泡紙", description="發送一片可點擊的電子氣泡紙，每格「啵」都是獨立的防雷標籤，點一下啵一下！")
@app_commands.describe(尺寸="氣泡紙大小")
@app_commands.choices(尺寸=[
    app_commands.Choice(name="2×5（10顆）",  value="2x5"),
    app_commands.Choice(name="5×10（50顆）", value="5x10"),
])
async def slash_bubblewrap(interaction: discord.Interaction, 尺寸: str = "2x5"):
    cols, rows = (2, 5) if 尺寸 == "2x5" else (5, 10)
    grid = '\n'.join(' '.join('||啵||' for _ in range(cols)) for _ in range(rows))
    await interaction.response.send_message(f'🫧 **電子氣泡紙 {cols}×{rows}**\n{grid}')


# ---------------------------------------------------------------------------
# /電子木魚
# ---------------------------------------------------------------------------
_MERIT_FILE = os.path.join('data', 'merit.json')


def _load_merit() -> dict:
    if os.path.exists(_MERIT_FILE):
        import json as _j
        with open(_MERIT_FILE, encoding='utf-8') as f:
            return _j.load(f)
    return {}


def _save_merit(data: dict) -> None:
    import json as _j
    with open(_MERIT_FILE, 'w', encoding='utf-8') as f:
        _j.dump(data, f, ensure_ascii=False, indent=2)


class MeritView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.session_count = 0

    @discord.ui.button(label='🪘 功德+1', style=discord.ButtonStyle.success, custom_id='merit_btn')
    async def merit_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        uid = str(interaction.user.id)
        data = _load_merit()
        data[uid] = data.get(uid, 0) + 1
        _save_merit(data)
        self.session_count += 1
        nick = nicknames.get(uid) or interaction.user.display_name
        await interaction.response.edit_message(
            content=f'🪘 **電子木魚**\n'
                    f'本次功德：**{self.session_count}** 下\n'
                    f'（{nick} 累計功德：**{data[uid]}** 下）')


@tree.command(name="電子木魚", description="發送一個電子木魚，按下按鈕敲木魚，每次積累一點功德🪘")
async def slash_merit(interaction: discord.Interaction):
    view = MeritView()
    await interaction.response.send_message('🪘 **電子木魚**\n本次功德：**0** 下', view=view)


@tree.command(name="電子木魚功德排行榜", description="查看本伺服器敲木魚功德累積次數 TOP10 排行榜")
async def slash_merit_rank(interaction: discord.Interaction):
    data = _load_merit()
    if not data:
        await interaction.response.send_message('還沒有人積過功德喵！', ephemeral=True)
        return
    sorted_data = sorted(data.items(), key=lambda x: x[1], reverse=True)
    lines = ['🪘 **功德排行榜 TOP10**']
    guild = interaction.guild
    for i, (uid, cnt) in enumerate(sorted_data[:10], 1):
        member = guild.get_member(int(uid)) if guild else None
        name = nicknames.get(uid) or (member.display_name if member else f'用戶{uid}')
        lines.append(f'`{i}.` {name} — **{cnt}** 下')
    await interaction.response.send_message('\n'.join(lines))


# ---------------------------------------------------------------------------
# /認養寵物 / /認主人 / /本群關係圖
# ---------------------------------------------------------------------------
_REL_FILE = os.path.join('data', 'relationships.json')


def _load_rel() -> dict:
    if os.path.exists(_REL_FILE):
        import json as _j
        with open(_REL_FILE, encoding='utf-8') as f:
            return _j.load(f)
    return {}


def _save_rel(data: dict) -> None:
    import json as _j
    with open(_REL_FILE, 'w', encoding='utf-8') as f:
        _j.dump(data, f, ensure_ascii=False, indent=2)


def _get_name(guild: discord.Guild, uid: str) -> str:
    member = guild.get_member(int(uid))
    return nicknames.get(uid) or (member.display_name if member else f'用戶{uid}')


class RelationView(discord.ui.View):
    """通用認養/認主人確認按鈕。mode: 'pet'=認養寵物, 'master'=認主人"""
    def __init__(self, requester: discord.Member, target: discord.Member,
                 guild_id: int, mode: str):
        super().__init__(timeout=60)
        self.requester = requester
        self.target    = target
        self.guild_id  = guild_id
        self.mode      = mode  # 'pet' or 'master'

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
            # requester 認養 target 為寵物 → target 的 master = requester
            data[gid][tgt_id] = req_id
            msg = f'🐾 {self.target.mention} 成為了 {self.requester.mention} 的寵物！'
        else:
            # requester 認 target 為主人 → requester 的 master = target
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


@tree.command(name="認養寵物", description="邀請指定用戶成為你的寵物，對方同意後建立主寵關係🐾")
@app_commands.describe(用戶="要認養的對象")
async def slash_adopt(interaction: discord.Interaction, 用戶: discord.Member):
    if 用戶.id == interaction.user.id:
        await interaction.response.send_message('不能認養自己喵！', ephemeral=True)
        return
    if 用戶.bot:
        await interaction.response.send_message('不能認養 Bot 喵！', ephemeral=True)
        return
    req_name = nicknames.get(str(interaction.user.id)) or interaction.user.display_name
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
    req_name = nicknames.get(str(interaction.user.id)) or interaction.user.display_name
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

    # 建立 master -> [pets] 對應
    master_map: dict[str, list[str]] = {}
    for pet_id, master_id in rels.items():
        master_map.setdefault(master_id, []).append(pet_id)

    # 找出所有沒有主人的主人（根節點）
    lines = ['🐾 **本群主寵關係圖**']
    visited = set()

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

    # 孤立寵物（主人不在伺服器或無樹根）
    orphans = [p for p in rels if p not in visited]
    if orphans:
        lines.append('\n**— 其他關係 —**')
        for pet_id in orphans:
            master_id = rels[pet_id]
            lines.append(f'🐾 {_get_name(guild, pet_id)} → 主人：{_get_name(guild, master_id)}')

    await interaction.response.send_message('\n'.join(lines))


class FishingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label='咬鉤 🪝', style=discord.ButtonStyle.danger)
    async def bite(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        user = interaction.user

        # 取得或建立 Webhook
        try:
            hooks = await channel.webhooks()
            wh = next((h for h in hooks if h.name == '賽博釣魚'), None)
            if wh is None:
                wh = await channel.create_webhook(name='賽博釣魚')
        except discord.Forbidden:
            await interaction.followup.send('Bot 缺少管理 Webhook 的權限喵！', ephemeral=True)
            return

        avatar_url = user.display_avatar.replace(size=256).url
        display_name = nicknames.get(str(user.id)) or user.display_name

        await wh.send('我是小男娘', username=display_name, avatar_url=avatar_url)
        await interaction.followup.send('🎣 上鉤了！', ephemeral=True)


@tree.command(name="賽博釣群友", description="放出釣魚按鈕，點下「咬鉤」的人會被 Webhook 偽裝發出一則訊息🪝")
async def slash_fishing(interaction: discord.Interaction):
    view = FishingView()
    await interaction.response.send_message(
        '🎣 **賽博釣魚中...**\n有人敢點嗎？', view=view)


_COIN_DRAMA = [
    '硬幣拋向了空中...',
    '一陣風吹過，硬幣飛得更高了...',
    '硬幣突破了對流層...',
    '硬幣衝出了大氣層...',
    '硬幣撞到了馬斯克的衛星，彈了回來...',
    '硬幣路過月球，嚇到了一隻兔子...',
    '硬幣被外星人短暫研究後歸還...',
    '硬幣開始自轉，產生了引力場...',
    '硬幣被小龍喵一把抓住，然後又吐了出來...',
    '硬幣懸浮在空中，陷入了哲學思考...',
    '薛丁格的貓路過，硬幣暫時同時是正面和反面...',
    '硬幣被一隻鴿子叼走，又被另一隻鴿子搶走...',
    '硬幣飛過了某個平行宇宙，裡面的你沒有丟硬幣...',
    '硬幣不小心進入了量子疊加態，工程師正在除錯...',
    '硬幣路過 7-11，買了一瓶茶飲料...',
    '硬幣被誤認為是隕石，NASA 發了一篇論文...',
    '硬幣決定先去旅遊，訂了張機票...',
    '硬幣在空中停了一下，拍了張自拍...',
    '硬幣終於開始下落了...（好像）',
    '一隻手從天而降，接住了硬幣，然後鬆開了...',
]


@tree.command(name="擲硬幣", description="擲一枚硬幣，隨機出現正面或反面🪙")
async def slash_coin(interaction: discord.Interaction):
    import random
    result = random.choice(['🌕 正面', '🌑 反面'])
    await interaction.response.send_message(f'🪙 擲出結果：**{result}**！')


@tree.command(name="擲硬幣幹話版", description="擲硬幣幹話版，硬幣先歷經奇妙旅程，隨機 1~5 句後才揭曉正反面🪙")
async def slash_coin_drama(interaction: discord.Interaction):
    import random
    result = random.choice(['🌕 **正面**', '🌑 **反面**'])
    lines = random.sample(_COIN_DRAMA, random.randint(1, 5))

    await interaction.response.send_message(f'🪙 {lines[0]}')
    for line in lines[1:]:
        await asyncio.sleep(random.uniform(1.2, 2.2))
        await interaction.channel.send(line)

    await asyncio.sleep(random.uniform(1.2, 2.0))
    await interaction.channel.send(f'硬幣落地！結果是⋯⋯ {result}！')


@tree.command(name="賽博體重計", description="隨機量測你的賽博體重，體重過重有機率觸發特殊反應⚖️")
async def slash_weight(interaction: discord.Interaction):
    import random
    weight = random.randint(10, 150)
    msg = f'⚖️ 賽博體重計顯示：**{weight} kg**'
    if weight > 100 and random.random() < 0.05:
        msg += '\n天啊你是柚子廚'
    await interaction.response.send_message(msg)


# ---------------------------------------------------------------------------
# /炮決蘿莉控
# ---------------------------------------------------------------------------
_ARTILLERY_FILE = os.path.join('data', 'artillery_records.json')
_ARTILLERY_IMG  = os.path.join('data', 'picture', 'artillerylolicon.jpg')


def _load_artillery() -> dict:
    if os.path.exists(_ARTILLERY_FILE):
        with open(_ARTILLERY_FILE, encoding='utf-8') as f:
            import json as _j
            return _j.load(f)
    return {}


def _save_artillery(data: dict) -> None:
    import json as _j
    with open(_ARTILLERY_FILE, 'w', encoding='utf-8') as f:
        _j.dump(data, f, ensure_ascii=False, indent=2)


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
        import random
        victim = random.choice(members)

    uid = str(victim.id)
    gid = str(guild.id)

    # 更新記錄
    records = _load_artillery()
    if gid not in records:
        records[gid] = {}
    records[gid][uid] = records[gid].get(uid, 0) + 1
    count = records[gid][uid]
    _save_artillery(records)

    nick = nicknames.get(uid) or victim.display_name

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
        name = (nicknames.get(uid) or member.display_name) if member else f'（已離開 {uid}）'
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


# ---------------------------------------------------------------------------
# /kb 指令群組（新增 / 載入知識庫）
# ---------------------------------------------------------------------------
kb_group = app_commands.Group(name="kb", description="知識庫管理")
tree.add_command(kb_group)


@kb_group.command(name="add", description="新增內容到知識庫（文字或上傳檔案，檔案會由 AI 分析統整）")
@app_commands.describe(文字="要儲存的文字內容", 檔案="要儲存並分析的檔案（.txt/.csv/.json/.sql 等）")
async def slash_kb_add(interaction: discord.Interaction,
                       文字: str = None,
                       檔案: discord.Attachment = None):
    global knowledge_entries
    if not 文字 and not 檔案:
        await interaction.response.send_message('請提供文字內容或上傳檔案喵！', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    parts = []
    if 文字:
        parts.append(文字.strip())

    if 檔案:
        try:
            raw = await 檔案.read()
            file_text = raw.decode('utf-8', errors='replace')
            await interaction.followup.send(f'正在分析 `{檔案.filename}`，請稍候...', ephemeral=True)
            summary = await analyze_for_kb(f'[{檔案.filename}]\n{file_text}')
            parts.append(f'[{檔案.filename} 分析結果]\n{summary}')
        except Exception as e:
            await interaction.followup.send(f'讀取或分析檔案失敗: {e}', ephemeral=True)
            return

    entry = add_entry(knowledge_entries, '\n'.join(parts), interaction.user.id)
    await interaction.followup.send(f'✅ 已分析並儲存至知識庫 `#{entry["id"]}`！', ephemeral=True)


@kb_group.command(name="remove", description="刪除知識庫中指定節次的資料（主人限定）")
@app_commands.describe(節次="要刪除的節次編號（先用 /kb list 查看）")
async def slash_kb_remove(interaction: discord.Interaction, 節次: int):
    if interaction.user.id != MASTER_ID:
        await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
        return
    sections = list_sections(knowledge_entries)
    if not sections:
        await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
        return
    if remove_section(knowledge_entries, 節次):
        remaining = len(list_sections(knowledge_entries))
        await interaction.response.send_message(
            f'✅ 已刪除第 `{節次}` 節，剩餘 {remaining} 節。', ephemeral=True
        )
    else:
        lines = '\n'.join(f'`[{i+1}]` {s[:80]}…' for i, s in enumerate(sections))
        await interaction.response.send_message(
            f'找不到第 `{節次}` 節喵！目前有 {len(sections)} 節：\n{lines}', ephemeral=True
        )


@kb_group.command(name="list", description="列出知識庫各節內容（主人限定）")
async def slash_kb_list(interaction: discord.Interaction):
    if interaction.user.id != MASTER_ID:
        await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
        return
    sections = list_sections(knowledge_entries)
    if not sections:
        await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
        return
    lines = '\n\n'.join(
        f'**[{i+1}]** {s[:150]}{"…" if len(s) > 150 else ""}' for i, s in enumerate(sections)
    )
    await interaction.response.send_message(
        f'**知識庫各節（共 {len(sections)} 節）：**\n{lines}', ephemeral=True
    )


@kb_group.command(name="load", description="從磁碟重新載入知識庫並注入此頻道對話供模型參考")
async def slash_kb_load(interaction: discord.Interaction):
    global knowledge_entries
    # 從磁碟重載，確保包含最新圖片分析與手動新增的條目
    knowledge_entries = load_knowledge()

    cid = interaction.channel_id
    sess = chat_sessions.get(cid)
    if not sess or not sess.get('chat_obj'):
        await interaction.response.send_message('此頻道尚未開始對話喵！請先 @我 說話。', ephemeral=True)
        return
    kb_ctx = build_knowledge_context(knowledge_entries)
    if not kb_ctx.strip():
        await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    chat = sess['chat_obj']
    await asyncio.to_thread(chat.send_message, kb_ctx)
    save_history(chat_sessions)
    await interaction.followup.send(
        f'✅ 知識庫已重新載入並注入此頻道對話！（共 {len(knowledge_entries)} 筆）', ephemeral=True
    )


async def _apply_gag(target: discord.Member, duration: int) -> str | None:
    """套用全伺服器禁言。回傳錯誤訊息或 None（成功）。"""
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
        err = await _apply_gag(self.target, self.duration)
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


@tree.command(name="電子口球", description="對成員套用全伺服器禁言（Timeout）。主人可直接執行，對他人需對方確認🔇")
@app_commands.describe(time="持續秒數", who="目標（預設為自己）")
async def slash_gag(interaction: discord.Interaction,
                    time: int,
                    who: discord.Member = None):
    target = who or interaction.user
    is_master = (interaction.user.id == MASTER_ID)
    is_self = (target.id == interaction.user.id)

    if time <= 0:
        await interaction.response.send_message('秒數必須大於 0 喵！', ephemeral=True)
        return

    if is_master or is_self:
        err = await _apply_gag(target, time)
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


def _init_session(cid: int, personality: str, sess: dict | None) -> None:
    """初始化或切換頻道的對話 session。"""
    raw_history = sess.get('raw_history', []) if sess else []
    web_context = sess.get('current_web_context') if sess else None
    summary = load_summary(cid)  # 若有摘要 TXT，在 history 為空時注入

    chat_sessions[cid] = {
        'chat_obj': create_chat(personality, raw_history, summary),
        'personality': personality,
        'raw_history': raw_history,
        'current_web_context': web_context,
    }


# ---------------------------------------------------------------------------
# !kb 文字指令（主人管理用；新增/載入請用 /kb add 和 /kb load）
#   !kb 列表         → 列出統整條目各節（主人限定）
#   !kb 清除 <n>     → 刪除第 n 節（主人限定）
# ---------------------------------------------------------------------------
async def handle_kb_command(msg: discord.Message, args: str) -> None:
    global knowledge_entries

    args = args.strip()
    is_master = (msg.author.id == MASTER_ID)

    if not is_master:
        await msg.reply('知識庫管理指令限主人使用，新增請用 `/kb add` 喵！')
        return

    if args in ('列表', 'list'):
        sections = list_sections(knowledge_entries)
        if not sections:
            await msg.reply('知識庫目前是空的。')
        else:
            lines = '\n\n'.join(
                f'**[{i + 1}]** {s[:120]}{"…" if len(s) > 120 else ""}'
                for i, s in enumerate(sections)
            )
            await msg.reply(f'**知識庫各節（共 {len(sections)} 節）：**\n{lines}\n\n用 `!kb 清除 <節次>` 刪除指定節。')
        return

    if args.startswith('清除 ') or args.startswith('清除　'):
        n_str = args.split(None, 1)[1].strip()
        if not n_str.isdigit():
            await msg.reply('請提供有效的節次編號（數字）。')
            return
        if remove_section(knowledge_entries, int(n_str)):
            remaining = len(list_sections(knowledge_entries))
            await msg.reply(f'已刪除第 `{n_str}` 節，剩餘 {remaining} 節。')
        else:
            await msg.reply(f'找不到第 `{n_str}` 節，請先用 `!kb 列表` 確認節次。')
        return

    await msg.reply('語法：`!kb 列表` / `!kb 清除 <節次>`')


# ---------------------------------------------------------------------------
# 附件處理輔助
# ---------------------------------------------------------------------------
_SOURCE_KEYWORDS: frozenset[str] = frozenset({
    '來源', '圖源', '出處', '哪裡', '從哪', '誰畫', '作者', '作品', '找圖', 'source', 'where', 'origin',
    '找本子', '找本本', '番號', '號碼', '查本子',
})


def _is_source_query(text: str) -> bool:
    return any(kw in text.lower() for kw in _SOURCE_KEYWORDS)


_INLINE_MIME_TYPES: frozenset[str] = frozenset({
    'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf',
})
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    '.txt', '.py', '.js', '.ts', '.json', '.md', '.csv', '.html', '.htm',
    '.css', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.log',
    '.sh', '.bat', '.c', '.cpp', '.h', '.java', '.go', '.rs', '.rb',
})


def _guess_mime(filename: str) -> str:
    """從副檔名推測 MIME type。"""
    ext = os.path.splitext(filename)[1].lower()
    return {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
        '.pdf': 'application/pdf',
    }.get(ext, 'application/octet-stream')


def _is_text_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in _TEXT_EXTENSIONS


def _parse_user_id(raw: str, msg: discord.Message) -> int | None:
    """從 <@id>、<@!id> 或純數字字串解析 user_id。"""
    # @提及格式
    m = re.match(r'<@!?(\d+)>', raw)
    if m:
        return int(m.group(1))
    # 純數字
    if raw.isdigit():
        return int(raw)
    return None


# ---------------------------------------------------------------------------
# Discord 事件
# ---------------------------------------------------------------------------

@client.event
async def on_ready() -> None:
    global _worker_started, nicknames, knowledge_entries

    print(f'[OK] Logged in as: {client.user}')

    loaded = load_history()
    chat_sessions.update(loaded)

    nicknames = load_nicknames()
    knowledge_entries = load_knowledge()
    consolidate_knowledge(knowledge_entries)  # 重啟時統整，只保留單一條目

    if not _worker_started:
        _worker_started = True
        asyncio.create_task(gemini_worker(chat_sessions, knowledge_entries))

    await tree.sync()
    print(f'[OK] Bot ready! {len(chat_sessions)} channels, {len(nicknames)} nicknames, {len(knowledge_entries)} KB entries.')


@client.event
async def on_message(msg: discord.Message) -> None:
    if msg.author == client.user or not client.user.mentioned_in(msg):
        return

    cid: int = msg.channel.id
    is_master: bool = (msg.author.id == MASTER_ID)
    personality: str = 'master' if is_master else 'general'

    sess = chat_sessions.get(cid)
    if not sess or sess.get('personality') != personality:
        print(f'[INIT] ch={cid} personality={personality}')
        _init_session(cid, personality, sess)

    # 移除 @提及，取得純文字
    raw_text: str = msg.content.replace(f'<@{client.user.id}>', '').strip()

    # 空白且無附件才回「需要什麼協助」
    if not raw_text and not msg.attachments:
        await msg.reply('主...主人...請問...需...需要什麼協助嗎？喵嗚...')
        return

    # --- !kb 指令攔截（不送 Gemini）---
    if raw_text.startswith('!kb ') or raw_text.startswith('!kb　'):
        await handle_kb_command(msg, raw_text[4:])
        return

    print(f'[MSG] ch={cid} [{personality}]: {raw_text[:80]}')

    # --- 建立用戶身分前綴注入給模型 ---
    uid_str = str(msg.author.id)
    nick = nicknames.get(uid_str)
    display_name = msg.author.display_name

    if nick:
        user_ctx = f'[User ID: {msg.author.id}, 暱稱: {nick}]'
    else:
        user_ctx = f'[User ID: {msg.author.id}, 伺服器名稱: {display_name}]'

    if is_master:
        nick_summary = build_all_nicknames_summary(nicknames)
        identity_prefix = f'{nick_summary}\n{user_ctx}\n'
    else:
        identity_prefix = f'{user_ctx}\n'

    # 純圖片/附件無文字時使用預設提示
    prompt: str = raw_text if raw_text else '請描述這個附件的內容。'

    # --- 附件處理（圖片/PDF → file_parts；文字檔 → 附加到 prompt）---
    file_parts: list[dict] = []
    if msg.attachments:
        await msg.channel.send('喵嗚~ 偵測到附件，讀取中...')
        for attachment in msg.attachments:
            mime = attachment.content_type or _guess_mime(attachment.filename)
            # 去掉 MIME 參數部分（如 "image/png; charset=utf-8"）
            mime = mime.split(';')[0].strip()

            if mime in _INLINE_MIME_TYPES:
                if attachment.size > 20 * 1024 * 1024:
                    await msg.reply(f'`{attachment.filename}` 檔案過大（{attachment.size / 1024 / 1024:.1f} MB），最大支援 20 MB 喵！')
                    continue
                try:
                    data = await attachment.read()
                    file_parts.append({'data': data, 'mime_type': mime, 'url': attachment.url})
                    print(f'[FILE] {attachment.filename} ({mime}, {len(data)} bytes)')
                except Exception as e:
                    await msg.reply(f'讀取 `{attachment.filename}` 失敗: {e}')

            elif _is_text_file(attachment.filename):
                try:
                    data = await attachment.read()
                    text_content = data.decode('utf-8', errors='replace')
                    prompt += f'\n\n[附件 {attachment.filename}]:\n```\n{text_content[:3000]}\n```'
                    print(f'[FILE] 文字附件 {attachment.filename} ({len(text_content)} chars)')
                except Exception as e:
                    await msg.reply(f'讀取 `{attachment.filename}` 失敗: {e}')

            else:
                await msg.reply(
                    f'喵嗚... 不支援 `{attachment.filename}` 的格式（{mime}），'
                    '目前支援：圖片（jpg/png/gif/webp）、PDF、文字檔。'
                )

    # --- 以圖搜圖（詢問圖片來源時）---
    if file_parts and _is_source_query(prompt):
        await msg.channel.send('喵嗚~ 正在以圖搜圖，尋找來源中...')
        search_results = await reverse_image_search(
            file_parts[-1]['data'],
            file_parts[-1]['mime_type'],
        )
        prompt = (
            f'[以圖搜圖結果]\n{search_results}\n\n'
            f'用戶問題：{prompt}\n\n'
            f'[指示]\n'
            f'請根據上方搜尋結果挑選最相關的來源連結並輸出。\n'
            f'優先來源：pixiv、twitter、x.com、nhentai。若這些來源都沒有，再輸出其他最相關連結。\n\n'
            f'輸出格式：\n'
            f'- 每筆結果獨立一行，格式：X/twitter/pixiv/nhentai | 作品名 | 作者\n'
            f'  連結：**網址**\n'
            f'- 連結格式必須使用 **網址** 加粗顯示，不要使用 [文字](連結) 超連結格式\n'
            f'- 不需特別強調是連篇漫畫或單張插畫\n'
            f'- 不得添加任何額外說明或延伸內容'
        )

    # --- URL 偵測 ---
    if url_match := re.search(r'https?://[^\s\)\]\>\"\'`]+(?<![.,;:!?])', prompt):
        url: str = url_match.group(0)
        query: str = prompt.replace(url, '').strip()

        await msg.channel.send('喵嗚~ 偵測到網址，正在抓取內容中...')
        print(f'[WEB] Fetching: {url}')

        content = await fetch_url(url)

        if content.startswith('錯誤:') or not content:
            print(f'[WEB] 抓取失敗: {url}')
        else:
            chat_sessions[cid]['current_web_context'] = content
            await msg.reply('喵嗚！已成功抓取網頁內容囉！')
            prompt = (
                f'請簡潔摘要以下網頁內容：\n```\n{content}\n```\n原始網址：{url}'
                if not query else
                f'以下是從網址 `{url}` 抓取到的內容：\n```\n{content}\n```\n請根據這些內容，回答我的問題：{query}'
            )

    # --- 使用已儲存的網頁上下文 ---
    elif web_ctx := chat_sessions[cid].get('current_web_context'):
        prompt = f'根據我之前讀取的內容：\n```\n{web_ctx}\n```\n請問：{prompt}'

    # 自動注入知識庫（讓模型永遠能看到 KB 內容）
    kb_ctx = build_knowledge_context(knowledge_entries)
    final_prompt = (kb_ctx + identity_prefix + prompt) if kb_ctx else (identity_prefix + prompt)

    # 有圖片附件且非搜圖查詢時，回應後自動存入 KB
    has_image = any(fp['mime_type'].startswith('image/') for fp in file_parts)
    kb_save = None
    if has_image and not _is_source_query(prompt):
        img_filename = next(
            (a.filename for a in msg.attachments if (a.content_type or '').startswith('image/')),
            'image'
        )
        kb_save = {
            'entries': knowledge_entries,
            'saved_by': msg.author.id,
            'label': img_filename,
        }

    await msg_queue.put({
        'channel_id': cid,
        'prompt_text': final_prompt,
        'file_parts': file_parts,
        'reply_fn': msg.reply,
        'send_fn': msg.channel.send,
        'typing_ctx': msg.channel.typing(),
        'kb_save': kb_save,
    })


async def _main() -> None:
    """同時啟動 Discord Bot 與 LINE Webhook Server（若已設定）。"""
    from config import LINE_CHANNEL_ACCESS_TOKEN, LINE_WEBHOOK_PORT
    from line_bot import start_line_server

    tasks = []
    if LINE_CHANNEL_ACCESS_TOKEN:
        tasks.append(asyncio.create_task(
            start_line_server(chat_sessions, knowledge_entries, LINE_WEBHOOK_PORT, _init_session)
        ))

    async with client:
        await client.start(DISCORD_TOKEN)

    for t in tasks:
        t.cancel()


if __name__ == '__main__':
    try:
        print('Connecting to Discord...')
        asyncio.run(_main())
    except discord.errors.LoginFailure:
        print('[ERROR] Invalid Discord Bot Token. Check your .env file.')
    except KeyboardInterrupt:
        print('[STOP] Bot stopped by user.')
    except Exception as e:
        print(f'[ERROR] Failed to start bot: {e}')
    finally:
        if chat_sessions:
            print('[SAVE] 關閉前儲存聊天歷史...')
            save_history(chat_sessions)
