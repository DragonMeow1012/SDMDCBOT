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


_FISHING_PHRASES = [
    '其實...我平時在家都偷偷穿女裝的，有人想看嗎？',
    '汪汪！我是主人的小修勾！誰要來領養我？',
    '各位哥哥好，我是新來的笨蛋小男娘...',
    '有沒有人可以教教我怎麼化妝呀？我買了裙子不敢穿。',
    '嗚嗚嗚，今天也被自己可愛到了，想被摸摸頭。',
    '其實我的聲音可以夾得比女生還細喔，要聽聽看嗎？',
    '大家都叫我哥，其實我內心是個小公主啦。',
    '喵喵喵～今天也是求抱抱的一天！',
    '我已經準備好白絲了，有沒有哥哥來誇誇我？',
    '討厭啦，不要一直盯著人家看，人家會害羞。',
    '其實人家私底下超愛撒嬌的，只是平時裝得很man。',
    '今晚有人要陪小男娘打遊戲嗎？我很菜但很會叫喔。',
    '偷偷說...我的衣櫃裡其實都是 JK 制服。',
    '哥哥們，我這樣可愛嗎？（附上奇怪的網圖連結）',
    '每天出門前都要花一小時練習夾娃娃音，好累喔。',
    '誰能拒絕一個會嚶嚶嚶的男孩子呢？',
    '我不管，我就是全群最嬌弱的寶寶，快寵我！',
    '其實我的夢想是被當成小寵物養起來。',
    '偷偷告訴你們，我連內褲都是粉紅色的。',
    '唉，身為一個猛男，卻有顆想當魔法少女的心，好煩惱。',
    '兄弟們不瞞你說，我一天不鹿管就渾身難受。',
    '請問群裡有好看的腳腳可以看嗎？我好急，在線等。',
    '我承認了，我是個無可救藥的腿控加變態。',
    '為什麼我都交不到女朋友？難道是因為我硬碟裡有2T的片？',
    '有沒有那種...就是那種...很刺激的群組可以拉我？',
    '我單身 20 年了，現在看電線桿都覺得眉清目秀。',
    '完了，我又進入發情期了，誰來救救我。',
    '我對二次元老婆的愛已經超越了碳基生物的極限！',
    '誠招一個網戀對象，性別別卡太死，是活的就行。',
    '請問哪裡可以買到原味襪子？幫朋友問的，急。',
    '我這輩子最大的願望就是被漂亮大姐姐踩在腳下。',
    '只要群主一句話，我馬上脫衣服給你看！',
    '兄弟們，我又色色了，對不起這個社會。',
    '我的硬碟裡有 500G 的「學習資料」，點連結自取。',
    '我每天都在幻想自己是個後宮番男主角。',
    '看著群友的頭像，我不爭氣地流下了口水。',
    '誰能給我發張色圖？我現在什麼都願意做。',
    '我宣布，從今天起我就是群裡第一大變態，誰贊成誰反對？',
    '請問鹿管鹿到破皮了該擦什麼藥？我很認真。',
    '好想被包養喔，阿姨我不想努力了。',
    '吾乃漆黑烈焰使，凡人們，還不快跪下顫抖！',
    '我的右手又在隱隱作痛了，封印快解除了嗎？',
    '其實我是外星人派來地球的臥底，今天準備收網了。',
    '大家都以為我是普通人，其實我昨晚才拯救了世界。',
    '不要靠近我，我體內的洪荒之力會傷到你的。',
    '我在被窩裡拉屎了，怎麼辦，在線等。',
    '其實我上廁所從來不擦屁股，因為我相信自然風乾。',
    '我昨天偷偷嘗了一口自己的鼻屎，竟然是甜的。',
    '我洗澡的時候喜歡假裝自己是水棲生物在吐泡泡。',
    '錯的不是我，是這個世界！',
    '我已經看透了紅塵，準備明天就剃度出家。',
    '其實我有特異功能，我可以用意念控制自己睡覺。',
    '我每天都在和鏡子裡的自己猜拳，而且經常輸。',
    '誰能借我一百塊？我要去買拯救世界的裝備。',
    '我覺得我是天選之子，只是還沒覺醒而已。',
    '我把內褲套在頭上，感覺魔力湧上來了！',
    '其實我一直在暗戀群裡的某個人，但我不敢說是誰。',
    '我昨天對著路邊的野狗叫了半小時，結果牠贏了。',
    '我是一個沒有感情的殺手，但今晚我想吃麥當勞。',
    '媽媽說我是世界上最聰明的寶寶，對吧？',
    '點擊我的個人檔案，有驚喜福利喔～（羞）',
    '誰能幫我點一下這個連結？點了我就給你看我的秘密相簿。',
    '哥哥，加我這個新的小號嘛，大號被封了。',
    '想看人家換衣服嗎？進這個頻道等你喔。',
    '我剛剛拍了一段很害羞的影片，只發給你一個人看喔...',
    '其實我今天沒穿內衣，想看的點這裡...',
    '求求好心人施捨一點 Nitro，小女子願意做牛做馬。',
    '我找到了一個可以免費看那種東西的網站，偷偷分享給你們。',
    '我現在一個人在家，好怕怕，誰來陪我點連結聊天？',
    '只要你給我發紅包，我就是你的人了。',
    '掃這個 QR 碼，就可以解鎖我的私密生活照喔。',
    '我被壞人威脅了，點這個連結幫我檢舉好不好？',
    '誰幫我點這個邀請碼，我就讓他體驗天堂的感覺。',
    '我其實是個富婆，只要你點擊這裡，我就包養你。',
    '想要我的原味內衣嗎？參加這個抽獎就送喔。',
    '我把我的日記都寫在這個檔案裡了，密碼在連結裡。',
    '只要你幫我註冊這個帳號，我就滿足你一個願望。',
    '哥哥，人家好無聊，陪我玩這個色色的小遊戲好嗎？',
    '我偷偷辦了 OnlyFans，點連結免費訂閱我喔。',
]


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

        import random as _random
        phrase = _random.choice(_FISHING_PHRASES)
        await wh.send(phrase, username=display_name, avatar_url=avatar_url)
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

    @tree.command(name="本群關係圖", description="生成本伺服器主寵關係視覺圖🐾")
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

        await interaction.response.defer()

        try:
            from graph_render import render_relation_graph
            from commands.wife import get_active_wife_rels
            wife_rels = get_active_wife_rels(guild.id)
            buf = await render_relation_graph(guild, rels, wife_rels)
            await interaction.followup.send(file=discord.File(buf, filename='relations.png'))
        except Exception as e:
            print(f'[GRAPH] 圖形渲染失敗: {e}')
            await interaction.followup.send(f'圖形渲染失敗喵：{e}', ephemeral=True)

    @tree.command(name="賽博釣群友", description="放出釣魚按鈕，點下「咬鉤」的人會被 Webhook 偽裝發出一則訊息🪝")
    async def slash_fishing(interaction: discord.Interaction):
        view = FishingView()
        await interaction.response.send_message(
            '🎣 **賽博釣魚中...**\n', view=view)
