"""
趣味指令：/電子氣泡紙、/電子木魚、/電子木魚功德排行榜、/清除功德排行榜、/賽博體重計、/擲硬幣、/擲硬幣幹話版
"""
import asyncio
import json
import os
import random
import discord
from discord import app_commands

from config import MASTER_ID


_MERIT_FILE = os.path.join('data', 'merit.json')


def _load_merit() -> dict:
    if os.path.exists(_MERIT_FILE):
        with open(_MERIT_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_merit(data: dict) -> None:
    with open(_MERIT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class MeritView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.session_count = 0

    @discord.ui.button(label='功德+1', style=discord.ButtonStyle.success, custom_id='merit_btn')
    async def merit_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        uid = str(interaction.user.id)
        data = _load_merit()
        data[uid] = data.get(uid, 0) + 1
        _save_merit(data)
        self.session_count += 1
        nick = interaction.user.display_name
        await interaction.response.edit_message(
            content=f'**電子木魚**\n'
                    f'本次功德：**{self.session_count}** 下\n'
                    f'（{nick} 累計功德：**{data[uid]}** 下）')


_COIN_DRAMA = [
    # 原版
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
    # 程式／AI 類
    '硬幣掉進了 Python 的 while True: 無窮迴圈，永遠無法落地。',
    '電腦視覺 YOLO 演算法把硬幣誤認為「飛碟」，硬幣隨即被防空系統鎖定。',
    '硬幣連續掉落太快，觸發了 HTTP 429 Too Many Requests，被強制暫停在半空中。',
    '掃地機器人啟動 SLAM 導航，繞過硬幣並順手把它標記為地圖上的永久障礙物。',
    '一個用 Gemini 寫的 Discord 機器人發送了錯誤的 POST 請求，把硬幣的重力屬性設定成了負數。',
    '硬幣掉進了 Linux 終端機，直到有人輸入 sudo reboot 才會重新出現。',
    '由於硬幣的自轉軌跡不符合線性迴歸預測，直接被系統判定為離群值並刪除。',
    '演算法判定硬幣沒有 TISAX 認證，拒絕讓它降落。',
    '硬幣被當作訓練資料送進了神經網路，現在它只是一堆權重參數。',
    'OpenCV 抓不到硬幣的邊緣特徵，導致硬幣在現實中直接隱形了。',
    '硬幣忘了更新 SSL 憑證，被地心引力拒絕連線。',
    '硬幣被存進了雲端硬碟，但因為空間不足，卡在上傳進度 99%。',
    '硬幣遇到了 404 Not Found 錯誤，掉進了異次元空間。',
    '駭客竄改了硬幣的原始碼，讓它永遠只能擲出「側立」。',
    '硬幣被當成比特幣挖走，瞬間數位化消失。',
    '一台 Jetson Nano 運算過載，散熱風扇直接把硬幣吹飛到外太空。',
    '硬幣在掉落過程中被垃圾回收機制 (Garbage Collection) 清除了。',
    '你的 Python 腳本引發了 Segmentation fault，連帶讓硬幣的物理模型崩潰碎裂。',
    '硬幣試圖連線到資料庫確認落地座標，但連線逾時，只能懸在半空中等待重試。',
    '防毒軟體將硬幣的高速自轉判定為惡意行為，已將其隔離。',
    '硬幣被打包進 Docker 容器，結果找不到對應的 Port 可以掉出來。',
    '由於網路延遲，硬幣的掉落動畫出現了嚴重的掉幀。',
    '硬幣掉進了 Git 分支，你必須解開衝突 (Merge Conflict) 它才能順利落地。',
    '你的 Discord 機器人權限不足，無法讀取硬幣的最終正反面狀態。',
    '硬幣被當成浮點數處理，產生了精度誤差，落地時變成了 0.999 塊。',
    'Nav2 導航系統規劃了錯誤的路徑，硬幣直接飛進了隔壁老王的口袋。',
    'API 達到呼叫上限 (Rate Limit)，硬幣只能明天早上八點再繼續掉落。',
    '硬幣的材質被判定為不支援的格式，無法在物理引擎中渲染。',
    '由於拋出前沒有寫 try-except，硬幣遇到空氣阻力就直接報錯閃退了。',
    '硬幣被升級成量子位元，現在處於疊加態，你一觀測它就壞掉了。',
    # 半導體／封裝類
    '硬幣不小心採用了覆晶封裝 (Flip Chip) 技術，正反面焊死在空氣分子上。',
    '硬幣表面的鈣鈦礦太陽能電池吸收了過多光子，轉換出的電能讓它自動飛向了太陽。',
    '硬幣被送進了 CoWoS 產線，現在它跟一顆 GPU 封裝在一起，算力大增。',
    '硬幣經歷了高溫迴焊 (Reflow) 製程，落地時已經融化成一灘金屬。',
    'BGA 錫球陣列在硬幣底部生成，它穩穩地黏在半空中，再也拿不下來。',
    '硬幣的晶片載板發生了翹曲 (Warpage)，導致它只能以不規則的軌跡彈跳。',
    '無塵室的氣流太強，把硬幣直接吹進了黃光區的機台裡。',
    '硬幣被當作打線接合 (Wire Bonding) 的金線材料，瞬間被抽成了一條細絲。',
    '由於沒有做好散熱設計，硬幣在劇烈摩擦下發生了熱力學崩潰。',
    '硬幣被塗上了一層光阻劑，經過曝光顯影後，上面的頭像變成了蒙娜麗莎。',
    '蝕刻氣體不小心外洩，硬幣在半空中被腐蝕成了奈米級粉末。',
    '硬幣的純度達到了 99.9999999%，被外星人當作高科技材料劫走。',
    '晶圓切割機把硬幣切成了幾萬顆 Die，散落一地。',
    '硬幣掉進了化學機械平坦化 (CMP) 設備，被磨得薄如蟬翼，隨風飄走。',
    '離子植入機對硬幣進行了轟擊，硬幣現在帶有強烈的靜電，吸附在天花板上。',
    '硬幣的良率測試未通過，直接被機械手臂丟進了報廢桶。',
    '磊晶層在硬幣表面快速生長，硬幣瞬間變成了一塊巨大的金剛石。',
    '探針卡在硬幣上戳了幾萬個洞，確認它沒有導電能力後才放它走。',
    '封裝膠體 (EMC) 將硬幣徹底包覆，它現在是一顆不起眼的黑色方塊。',
    '由於熱膨脹係數 (CTE) 不匹配，硬幣在著地瞬間應力釋放而炸成粉末。',
    '硬幣被吸進了真空腔體，再也沒有掉下來的重力。',
    '濺鍍機給硬幣鍍上了一層鈦，現在它重得連地心引力都拉不動。',
    '硬幣被當成靶材，被電漿轟擊殆盡。',
    '濕式清洗機把硬幣洗得太乾淨，折射率改變，它直接隱形了。',
    '硬幣掉進了光罩盒，被當作極紫外光 (EUV) 光罩送進了 ASML 機台。',
    '載板廠產能滿載，硬幣排隊等著落地，預計明年 Q3 才會掉下來。',
    '硬幣被當作先進封裝的中介層 (Interposer)，連接了兩個平行宇宙。',
    '鈣鈦礦晶體結構發生相變，硬幣瞬間失去了所有的物理特性，化為虛無。',
    '導電銀膠塗得太多，硬幣直接黏在了你的視網膜上。',
    '測試機台報錯，硬幣被判定為「假性失效」，強制重新拋擲一次。',
    # 物理／科學類
    '時間暫停器被誤觸，硬幣成為了世界上唯一還在動的物體。',
    '硬幣的速度超越了光速，回到了你拋出它的前一天。',
    '質量濃縮產生了微型黑洞，把周圍的空氣和你一起吸了進去。',
    '硬幣引發了蝴蝶效應，導致地球另一端發生了八級大地震，而它自己穩穩落地。',
    '空間發生摺疊，硬幣掉進了第五維度，在那裡它是個會說話的三角形。',
    '馬克士威的惡魔攔截了硬幣，把它扔進了高溫熱庫。',
    '由於測不準原理，你只能知道硬幣下落的速度，卻永遠找不到它落在哪裡。',
    '硬幣經歷了量子穿隧效應，直接穿透了地板，掉到了樓下鄰居的湯裡。',
    '平行宇宙發生碰撞，掉下來兩枚硬幣，但上面的頭像都是你。',
    '硬幣的重力勢能被轉換成了暗物質，消失在視野中。',
    '弦理論的維度捲曲解開，硬幣拉長成了一條橫跨太陽系的線。',
    '硬幣觸發了反物質湮滅，釋放出巨大的能量把你吹飛。',
    '熵值瞬間逆轉，硬幣重新飛回了你的口袋裡。',
    '蟲洞在硬幣下方開啟，它掉到了仙女座星系。',
    '萬有引力常數 G 突然改變，硬幣以每秒 100 公里的速度砸穿了地心。',
    '硬幣經歷了洛倫茲收縮，變成了一條二維的線段。',
    '薛丁格的貓不但路過，還一巴掌把硬幣拍進了疊加態的箱子裡。',
    '硬幣的波函數坍縮失敗，它變成了一團模糊的機率雲。',
    '宇宙膨脹速度大於硬幣掉落速度，地板距離硬幣越來越遠。',
    '硬幣被拉普拉斯的惡魔精準預測了軌跡，因為覺得無聊，惡魔中途把它改成了骰子。',
    '多世界詮釋發威，硬幣在每次翻轉時都分裂出了一個新的宇宙。',
    '普朗克長度突然變大，硬幣被卡在兩個空間像素之間動彈不得。',
    '硬幣掉進了莫比烏斯環，永遠在正反面之間無限循環。',
    '克萊因瓶裝住了硬幣，它既在瓶內也在瓶外。',
    '硬幣觸發了真空衰變，以光速毀滅了整個宇宙。',
    '牛頓從棺材裡跳出來，一把抓住硬幣說：「這不符合我的定律！」',
    '硬幣被彭羅斯階梯困住，永遠在往下掉，卻永遠落不到地面。',
    '宇宙的模擬器發生了 Bug，硬幣穿模掉進了虛無。',
    '時間箭頭倒轉，硬幣是一路從地板往上彈到你的手裡的。',
    '由於引力透鏡效應，你看見滿天都是硬幣的幻影，卻接不到本體。',
    # 神話／奇幻類
    '宙斯看中了這枚硬幣，把它變成了天上的一顆星座。',
    '索爾的雷神之鎚飛過，強大的磁力把硬幣直接吸走了。',
    '奇異博士開啟了傳送門，硬幣掉進了多重宇宙的瘋狂之中。',
    '霍格華茲的貓頭鷹以為那是零食，一口吞下後飛走了。',
    '土地公覺得你太窮，把硬幣變成了金條，砸碎了你的腳趾。',
    '硬幣在半空中覺醒了替身使者，發動了「世界」(The World)。',
    '煉金術師路過，順手把硬幣等價交換成了一隻烤雞。',
    '孫悟空拔了一根毫毛，把硬幣變成了漫山遍野的猴子。',
    '吸血鬼害怕硬幣上的銀質成分，慘叫一聲化為灰燼。',
    '硬幣被召喚陣選中，變成了一個只會說「阿巴阿巴」的史萊姆。',
    '死神揮舞鐮刀，斬斷了硬幣與這個世界的因果連結。',
    '精靈從硬幣裡鑽出來，說可以滿足你三個願望，但前提是你得先幫他寫期末報告。',
    '閻羅王生死簿上沒這枚硬幣的名字，拒絕讓它掉到地上。',
    '丘比特把硬幣當成箭頭射了出去，兩隻路過的流浪狗突然墜入愛河。',
    '財神爺覺得面額太小，一腳把它踢飛到太平洋。',
    '硬幣沾染了九尾狐的妖氣，變成了一個絕世美女，然後拿著你的錢包跑了。',
    '惡魔要求用硬幣交換你的靈魂，你點擊了拒絕，硬幣化成了一陣黑煙。',
    '魔法師施展了漂浮咒 (Wingardium Leviosa)，硬幣在天花板上跳起了華爾茲。',
    '硬幣被當作七龍珠之一，集齊七枚後召喚出了神龍。',
    '觀音菩薩的玉淨瓶漏水，一滴甘露把硬幣變成了一朵蓮花。',
    '月老把紅線綁在硬幣上，結果硬幣和下水道的鐵柵欄鎖死了。',
    '哆啦A夢的放大燈照到了硬幣，它現在變成了一個直徑十公尺的鐵餅。',
    '漫威的蟻人把硬幣縮小到次原子級別，它掉進了量子領域。',
    '伏地魔把硬幣做成了分靈體，現在你必須用葛萊分多寶劍砍碎它。',
    '硬幣被當作塔羅牌的「命運之輪」抽了出來，結果你的運勢變成了大凶。',
    '洛基施展了幻術，你以為硬幣落地了，其實你手裡握著的是一隻癩蛤蟆。',
    '亞瑟王拔出了石中劍，順便也把插在地上的硬幣拔了出來。',
    '硬幣掉進了聚寶盆，瞬間複製出了一千萬枚，把你淹沒了。',
    '獨角獸的眼淚滴在硬幣上，它現在可以治癒一切疾病，但被無良藥廠高價收購了。',
    '硬幣被當作祭品獻給了克蘇魯，觸手從地底伸出把它捲走。',
    # 日常荒謬類
    '一隻鴿子飛過，精準地把大便拉在硬幣上，增加的重量讓它提前落地。',
    '硬幣在空中遇到了另一個正在被拋的硬幣，它們相撞後決定私奔。',
    '你設定的惡作劇腳本突然觸發，硬幣落地瞬間大聲播報出讓人社死的一句話。',
    '鄰居的阿嬤以為那是掉下來的假牙，一把接走並塞進了嘴裡。',
    '硬幣突然開口說話：「主人...請、請不要隨便把我丟出去...我會怕...」',
    '一台疾駛而過的砂石車輾過了硬幣，把它壓成了一個平底鍋。',
    '你的老闆突然出現，以「上班時間玩硬幣」為由把它沒收並扣了你薪水。',
    '硬幣掉進了一碗剛煮好的泡麵裡，濺起的湯汁毀了你的白襯衫。',
    '突然一陣狂風吹來，硬幣精準地飛進了五十公尺外販賣機的投幣口。',
    '國稅局查緝逃漏稅，當場徵收了這枚正在半空中的硬幣。',
    '硬幣發現自己其實是巧克力金幣，在夏天的陽光下直接融化了。',
    '隔壁的小孩用彈弓把硬幣打了下來，並嘲笑你的拋硬幣技術。',
    '硬幣覺得每天被拋來拋去太累了，決定在空中躺平，直接罷工。',
    '你突然忘記了怎麼呼吸，硬幣掉落的聲音被救護車的鳴笛聲掩蓋。',
    '一名 YouTuber 衝出來把硬幣搶走，大喊：「挑戰一百天不讓硬幣落地，第一天！」',
    '硬幣在半空中參加了百萬小學堂，答錯題目被乾冰噴射器轟飛。',
    '詐騙集團打電話來，你一分心，硬幣直接砸在你的眼睛上。',
    '硬幣突然想起了自己是一枚代幣，於是自動飛向了附近的湯姆熊歡樂世界。',
    '警察臨檢，懷疑這枚硬幣是危險武器，立刻拉起封鎖線。',
    '硬幣因為沒有戴安全帽，被交通警察開了一張罰單並扣留。',
    '一群螞蟻路過，把它當作巨大的金屬飛碟扛回了蟻丘。',
    '硬幣掉落的頻率剛好跟你的耳膜產生共振，你暫時失聰了三秒鐘。',
    '你拋出的不是硬幣，而是一枚拉開保險的芭樂（手榴彈）。',
    '硬幣嫌棄地板太髒，在距離地面一公分的地方緊急煞車並懸停。',
    '突然地震，地板裂開一個大洞，硬幣剛好掉進了深淵。',
    '你發現硬幣其實是黏在手指上的，你剛剛只是在對著空氣揮手。',
    '硬幣落地，但地上早就鋪滿了強力膠，你再也拔不起來。',
    '硬幣突然意識到自己只是 AI 生成的一段文字，於是變成了一串亂碼。',
    '硬幣終於落地了，但它站得筆直，周圍的人開始對它膜拜，建立了一個新的宗教。',
]


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="電子氣泡紙", description="發送一片可點擊的電子氣泡紙，點一下啵一下！")
    @app_commands.describe(
        尺寸="選擇預設尺寸或自訂",
        長="自訂列數（1~50，尺寸選自訂時生效）",
        寬="自訂欄數（1~50，尺寸選自訂時生效）",
    )
    @app_commands.choices(尺寸=[
        app_commands.Choice(name="5×2（10顆）",  value="5x2"),
        app_commands.Choice(name="10×5（50顆）", value="10x5"),
        app_commands.Choice(name="自訂",          value="custom"),
    ])
    async def slash_bubblewrap(
        interaction: discord.Interaction,
        尺寸: str = "5x2",
        長: app_commands.Range[int, 1, 50] = None,
        寬: app_commands.Range[int, 1, 50] = None,
    ):
        if 尺寸 == "custom":
            if 長 is None or 寬 is None:
                await interaction.response.send_message('自訂尺寸需同時填入「長」與「寬」（各 1～50）喵！', ephemeral=True)
                return
            rows, cols = 長, 寬
        elif 尺寸 == "5x2":
            rows, cols = 2, 5
        else:
            rows, cols = 5, 10

        grid = '\n'.join(' '.join('||啵||' for _ in range(cols)) for _ in range(rows))
        await interaction.response.send_message(f'**電子氣泡紙 {cols}×{rows}**\n{grid}')

    @tree.command(name="電子木魚", description="發送一個電子木魚，按下按鈕敲木魚，每次積累一點功德")
    async def slash_merit(interaction: discord.Interaction):
        view = MeritView()
        await interaction.response.send_message('**電子木魚**\n本次功德：**0** 下', view=view)

    @tree.command(name="電子木魚功德排行榜", description="查看本伺服器敲木魚功德累積次數 TOP10 排行榜")
    async def slash_merit_rank(interaction: discord.Interaction):
        data = _load_merit()
        if not data:
            await interaction.response.send_message('還沒有人積過功德喵！', ephemeral=True)
            return
        sorted_data = sorted(data.items(), key=lambda x: x[1], reverse=True)
        lines = ['**功德排行榜 TOP10**']
        guild = interaction.guild
        for i, (uid, cnt) in enumerate(sorted_data[:10], 1):
            member = guild.get_member(int(uid)) if guild else None
            name = member.display_name if member else f'用戶{uid}'
            lines.append(f'`{i}.` {name} — **{cnt}** 次')
        await interaction.response.send_message('\n'.join(lines))

    @tree.command(name="清除功德排行榜", description="清除所有用戶的功德記錄，無法復原。（主人限定）")
    async def slash_merit_clear(interaction: discord.Interaction):
        if interaction.user.id != MASTER_ID:
            await interaction.response.send_message('此指令限主人使用喵！', ephemeral=True)
            return
        data = _load_merit()
        if not data:
            await interaction.response.send_message('功德排行榜本來就是空的喵！', ephemeral=True)
            return
        with open(_MERIT_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        await interaction.response.send_message('✅ 功德排行榜已清除！', ephemeral=True)

    @tree.command(name="賽博體重計", description="量測你的賽博體重，體重過重有機率觸發特殊反應")
    async def slash_weight(interaction: discord.Interaction):
        weight = random.randint(10, 150)
        msg = f' 賽博體重計顯示：**{weight} kg**'
        if weight > 100 and random.random() < 0.05:
            msg += '\n天啊你是柚子廚'
        await interaction.response.send_message(msg)

    @tree.command(name="擲硬幣", description="擲一枚硬幣，隨機出現正面或反面🪙")
    async def slash_coin(interaction: discord.Interaction):
        result = random.choice(['🌕 正面', '🌑 反面'])
        await interaction.response.send_message(f'🪙 擲出結果：**{result}**！')

    @tree.command(name="擲硬幣幹話版", description="擲硬幣幹話版，硬幣先歷經奇妙旅程，隨機 1~10 句後才揭曉正反面🪙")
    async def slash_coin_drama(interaction: discord.Interaction):
        result = random.choice(['🌕 **正面**', '🌑 **反面**'])
        lines = random.sample(_COIN_DRAMA, random.randint(1, 10))

        content = f'🪙 {lines[0]}'
        await interaction.response.send_message(content)
        for line in lines[1:]:
            await asyncio.sleep(random.uniform(1.2, 2.2))
            content += f'\n{line}'
            await interaction.edit_original_response(content=content)

        await asyncio.sleep(random.uniform(1.2, 2.0))
        content += f'\n硬幣落地！結果是⋯⋯ {result}！'
        await interaction.edit_original_response(content=content)

    @tree.command(name="roll", description="從 1~100 隨機抽一個數字")
    async def slash_roll(interaction: discord.Interaction):
        n    = random.randint(1, 100)
        name = interaction.user.display_name
        await interaction.response.send_message(f'{name} 抽到了 **{n}** 點！')

    @tree.command(name="丟骰子", description="投一顆六面骰，隨機出現 1~6")
    async def slash_dice(interaction: discord.Interaction):
        n    = random.randint(1, 6)
        name = interaction.user.display_name
        await interaction.response.send_message(f'{name} 投到了 **{n}** 點！')

    _TEAM_NUM_WORDS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']

    class TeamView(discord.ui.View):
        def __init__(self, n_teams: int):
            super().__init__(timeout=30)
            self.n_teams      = n_teams
            self.participants: list[discord.Member] = []
            self.joined_ids:   set[int]             = set()

        @discord.ui.button(label='參與 ✋', style=discord.ButtonStyle.primary)
        async def join(self, interaction: discord.Interaction, _btn: discord.ui.Button):
            user = interaction.user
            if user.id in self.joined_ids:
                await interaction.response.send_message('你已經參與了喵！', ephemeral=True)
                return
            self.joined_ids.add(user.id)
            self.participants.append(user)
            names = ' '.join(m.display_name for m in self.participants)
            await interaction.response.edit_message(
                content=(
                    f'🎮 **隊伍抽籤報名中！**\n'
                    f'將分成 **{self.n_teams}** 隊，30 秒後自動抽籤。\n'
                    f'目前參與（{len(self.participants)} 人）：{names}'
                )
            )

    @tree.command(name="抽隊伍", description="開放報名，30 秒後隨機分配隊伍")
    @app_commands.describe(隊伍數量="要分成幾隊（2～10）")
    async def slash_team_draw(interaction: discord.Interaction,
                              隊伍數量: app_commands.Range[int, 2, 10]):
        view = TeamView(隊伍數量)
        await interaction.response.send_message(
            f'🎮 **隊伍抽籤報名中！**\n'
            f'將分成 **{隊伍數量}** 隊，30 秒後自動抽籤。\n'
            f'目前參與（0 人）：',
            view=view,
        )
        await asyncio.sleep(30)
        view.stop()

        participants = view.participants
        if not participants:
            await interaction.edit_original_response(
                content='沒有人參與喵QQ', view=None)
            return

        random.shuffle(participants)
        teams: list[list[str]] = [[] for _ in range(隊伍數量)]
        for i, m in enumerate(participants):
            teams[i % 隊伍數量].append(m.display_name)

        lines = []
        for i, team in enumerate(teams):
            if team:
                num = _TEAM_NUM_WORDS[i] if i < len(_TEAM_NUM_WORDS) else str(i + 1)
                lines.append(f'第{num}隊：{" ".join(team)}')

        await interaction.edit_original_response(
            content='🎮 **抽隊伍結果！**\n' + '\n'.join(lines),
            view=None,
        )
