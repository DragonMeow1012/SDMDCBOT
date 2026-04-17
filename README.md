# 小龍喵 Discord Bot

基於 Gemini AI 的 Discord / LINE 聊天機器人，支援多角色人格、暱稱系統、知識庫、網頁抓取、圖片反搜、對話記憶跨重啟保留，以及大規模 Pixiv 圖片爬蟲。

---

## 功能

- **AI 對話**：使用 Gemini 2.5 Flash，@提及即可對話
- **雙人格模式**：主人（龍龍喵）與一般訪客使用不同人格設定
- **對話記憶**：歷史儲存於 `data/chat_history.json`，重啟自動載入
- **對話摘要**：自動序列化為 TXT（`data/summaries/`），供模型跨 session 參考
- **暱稱系統**：每位用戶可設定暱稱，模型優先用暱稱稱呼
- **知識庫**：永久儲存跨頻道知識條目，支援文字或檔案 AI 分析
- **網頁抓取**：訊息附上 URL 自動抓取並摘要或回答問題
- **圖片反搜**：`/以圖搜圖` 或關鍵字觸發反向圖片搜尋
- **名言佳句**：右鍵訊息生成 1920×1080 精美引言圖
- **LINE 整合**：支援 LINE Bot Webhook，LINE 聊天也能與 Gemini 對話
- **電子口球**：對成員套用全伺服器禁言（Timeout）
- **API Key 輪替**：支援最多 7 組 Gemini API Key（`GEMINI_API_KEY` 至 `GEMINI_API_KEY6`），超出限制自動切換
- **Pixiv 爬蟲**：全站 tag／ranking／作者擴散爬取，pHash 去重，FAISS 二值索引，支援指定作者優先爬取

---

## 專案結構

```
DCbot_1.0/
├── main.py                    # 入口：Discord 事件、session 管理、URL/附件處理
├── state.py                   # 共享可變狀態（chat_sessions / nicknames / knowledge_entries）
├── config.py                  # 環境變數、PERSONALITY 人格設定、常數
├── gemini_worker.py           # Gemini API Worker、模型初始化、Key 輪替
├── ai_session.py              # AI 供應商抽象層（Gemini / LM Studio）
├── history.py                 # 本地聊天歷史讀寫；atomic write + save_history_async
├── nicknames.py               # 暱稱系統（load/save/build context）
├── knowledge.py               # 知識庫（data/knowledge.json）
├── summary.py                 # 對話摘要序列化（data/summaries/）
├── web.py                     # 非同步網頁抓取（aiohttp + BeautifulSoup）
├── line_bot.py                # LINE Bot Webhook 伺服器（port 8080）
├── reverse_search.py          # 圖片反向搜尋（SauceNAO + soutubot）
├── quote_image.py             # 名言佳句圖片生成（Pillow，1920×1080）
├── graph_render.py            # 本群關係圖網絡渲染（matplotlib + networkx）
├── logger.py                  # 統一 logging 設定
├── pixiv_crawler/             # Pixiv 全站非同步爬蟲套件（asyncio + aiohttp + AppPixivAPI）
├── pixiv_feature.py           # pHash 特徵提取 + FAISS 二值索引管理
├── pixiv_database.py          # Pixiv SQLite 資料庫操作（pixiv.db）
├── pixiv_config.py            # Pixiv 模組設定（路徑、tag 列表、爬取參數）
├── pixiv_status_app.py        # Streamlit 爬取狀態監控頁面（port 8766）
├── utils/                     # 共用工具
│   ├── json_store.py          # load_json / save_json（原子寫入） / save_json_async
│   ├── discord_helpers.py     # Discord 成員查詢、權限判斷
│   ├── ai_helpers.py          # AI 回呼、錯誤訊息格式化
│   └── text_processing.py     # 預編譯正則、文字清理
├── commands/                  # 斜線指令套件
│   ├── __init__.py            # setup_all(tree) 統一注冊
│   ├── admin.py               # /清除記憶、/清空知識庫
│   ├── ai.py                  # AI 相關指令（provider 切換等）
│   ├── nick.py                # /nick
│   ├── gag.py                 # /電子口球、/口球輪盤
│   ├── fun.py                 # /電子氣泡紙、/電子木魚、/電子木魚功德排行榜
│   │                          # /賽博體重計、/擲硬幣、/擲硬幣幹話版
│   ├── social.py              # /認養寵物、/認主人、/本群關係圖、/賽博釣群友
│   ├── artillery.py           # /炮決蘿莉控、/炮決排行、/清除炮決名單
│   ├── quote.py               # 右鍵選單：名言佳句、Make it Quote
│   ├── search.py              # /以圖搜圖
│   ├── kb.py                  # /kb 群組（add/remove/list/load）、!kb 文字指令
│   ├── whip.py                # 電子鞭子：鞭打群友
│   ├── wife.py                # /抽今日媽媽、/認媽媽、/拋棄兒子、/和今日媽媽斷絕關係
│   └── pixiv.py               # /pixiv爬蟲、/pixiv停止、/pixiv狀態
├── data/
│   ├── chat_history.json      # 各頻道對話歷史（自動生成）
│   ├── nicknames.json         # 用戶暱稱（自動生成）
│   ├── knowledge.json         # 知識庫條目（自動生成）
│   ├── merit.json             # 電子木魚功德記錄（自動生成）
│   ├── relationships.json     # 主寵關係（自動生成）
│   ├── artillery_records.json # 炮決記錄（自動生成）
│   ├── summaries/             # 各頻道對話摘要 TXT
│   ├── picture/
│   │   └── artillerylolicon.jpg
│   └── bot.log                # 執行 log（背景啟動時產生）
├── pixivdata/                 # Pixiv 爬蟲資料根目錄（自動生成）
│   ├── images/                # 下載的圖片（按 illust_id 子目錄）
│   ├── data/
│   │   ├── pixiv.db           # SQLite 作品資料庫
│   │   ├── feature.index      # FAISS 二值索引（pHash）
│   │   ├── feature.index.ids.npy          # 索引 ID 映射（encoded illust_id + page）
│   │   ├── tag_crawl_progress.json        # tag 爬取斷點記錄
│   │   ├── user_id_scan_cursor.json       # 作者 ID 掃描游標
│   │   └── status.json                    # 爬取狀態（供 Streamlit 讀取）
│   ├── logs/
│   │   ├── spider.log         # 爬蟲主 log
│   │   └── pixiv_query.log    # 指令操作 log
│   └── pagedata/
│       ├── page_log.jsonl     # 每輪相變診斷日誌
│       └── timeout_log.jsonl  # 超時事件日誌
├── .env                       # 金鑰設定（不提交 git）
├── .env.example               # 金鑰範本
├── requirements.txt           # Python 依賴套件
├── Dockerfile
└── docker-compose.yml
```

---

## 安裝與設定

### 1. 安裝 Python 3.12+

```bash
winget install Python.Python.3.12
```

### 2. 安裝依賴套件

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. 設定金鑰

```bash
copy .env.example .env
```

編輯 `.env`：

```env
DISCORD_BOT_TOKEN=你的_Discord_Bot_Token
GEMINI_API_KEY=你的_Gemini_API_Key
GEMINI_API_KEY1=備用Key1（可選）
GEMINI_API_KEY2=備用Key2（可選）
GEMINI_API_KEY3=備用Key3（可選）
GEMINI_API_KEY4=備用Key4（可選）
GEMINI_API_KEY5=備用Key5（可選）
GEMINI_API_KEY6=備用Key6（可選）

# Pixiv 爬蟲（可選，設定後可使用 /pixiv爬蟲）
PIXIV_REFRESH_TOKEN=你的_Pixiv_Refresh_Token
PIXIV_WEB_COOKIE=你的_Pixiv_Web_Cookie（可選，提升搜尋配額）
NGROK_AUTH_TOKEN=你的_ngrok_Token（可選，狀態頁面公開存取）

# LINE Bot（可選）
LINE_CHANNEL_ACCESS_TOKEN=你的_LINE_Channel_Access_Token
LINE_CHANNEL_SECRET=你的_LINE_Channel_Secret
```

- Discord Token：[Discord Developer Portal](https://discord.com/developers/applications)
- Gemini API Key：[Google AI Studio](https://aistudio.google.com/apikey)
- Pixiv Refresh Token：使用 [pixivpy3](https://github.com/upbit/pixivpy) 的 `refresh_token` 取得方式取得
- LINE Token：[LINE Developers Console](https://developers.line.biz/)

---

## 啟動

### 前景執行（開發用）

```bash
python main.py
```

### 背景執行（持續運行）

```bash
python main.py > data/bot.log 2>&1 &
```

啟動後會顯示 PID，記下備用：

```
[1] 1234
```

查看 log：

```bash
tail -20 data/bot.log
```

查詢 Bot PID：

```bash
ps aux | grep "main.py" | grep -v grep
```

關閉 Bot：

```bash
kill <PID>
# 或全部關閉
pkill -f "main.py"
```

重啟 Bot：

```bash
kill <PID>; sleep 1 && python main.py > data/bot.log 2>&1 &
```

### Docker

```bash
docker compose up -d
```

---

## 使用方式

在 Discord 頻道中 **@小龍喵** 即可開始對話。

| 操作 | 說明 |
|------|------|
| `@小龍喵 你好` | 一般對話 |
| `@小龍喵 https://example.com` | 抓取網頁並摘要 |
| `@小龍喵 https://example.com 這篇說什麼？` | 抓取後依問題回答 |
| 附圖 + `@小龍喵` | 圖片分析並自動存入知識庫 |
| 附圖 + `@小龍喵 來源？` | 自動觸發反向圖片搜尋 |

---

## 斜線指令

### AI 與記憶

| 指令 | 說明 | 權限 |
|------|------|------|
| `/nick 暱稱` | 設定自己的暱稱（模型稱呼用） | 所有人 |
| `/nick 暱稱 對象` | 設定指定成員的暱稱 | 主人限定 |
| `/清除記憶` | 清除所有頻道的聊天記憶 | 主人限定 |

### 知識庫

| 指令 | 說明 | 權限 |
|------|------|------|
| `/kb add 文字` | 新增文字到知識庫 | 所有人 |
| `/kb add 檔案` | 上傳檔案由 AI 分析後儲存 | 所有人 |
| `/kb remove 節次` | 刪除指定節次 | 主人限定 |
| `/kb list` | 列出所有節次 | 主人限定 |
| `/kb load` | 重新載入並注入當前頻道 | 所有人 |
| `/清空知識庫` | 清空所有知識庫條目 | 主人限定 |

### 搜尋與圖片

| 指令 | 說明 | 權限 |
|------|------|------|
| `/以圖搜圖 圖片` | 用截圖找來源（pixiv/twitter/x/nh） | 所有人 |
| 右鍵 → `名言佳句` | 將訊息製成名言圖（1920×1080） | 所有人 |
| 右鍵 → `Make it Quote` | 同上（英文選單） | 所有人 |

### Pixiv 爬蟲

| 指令 | 說明 | 權限 |
|------|------|------|
| `/pixiv爬蟲` | 開始全站背景爬取（tag + ranking + 作者擴散） | 主人限定 |
| `/pixiv爬蟲 作者ID` | 將指定 Pixiv 作者加入優先爬取佇列；爬蟲未啟動時自動啟動 | 主人限定 |
| `/pixiv停止` | 停止背景爬取（優雅等待當前批次完成後停止） | 主人限定 |
| `/pixiv狀態` | 查看爬取狀態、作品統計、本輪進度，以及 Streamlit 狀態頁 URL | 所有人 |

### 娛樂

| 指令 | 說明 | 權限 |
|------|------|------|
| `/擲硬幣` | 擲一枚硬幣，正面或反面 | 所有人 |
| `/擲硬幣幹話版` | 硬幣先歷經奇妙旅程，1~10 句後揭曉結果 | 所有人 |
| `/電子氣泡紙 尺寸` | 發送可點擊的電子氣泡紙（5×2 / 10×5） | 所有人 |
| `/電子木魚` | 敲木魚積功德按鈕 | 所有人 |
| `/電子木魚功德排行榜` | 功德 TOP10 排行榜 | 所有人 |
| `/賽博體重計` | 量測賽博體重 | 所有人 |
| `/炮決蘿莉控 [用戶]` | 隨機或指定炮決，並記錄次數 | 所有人 |
| `/炮決排行` | 被炮決次數 TOP10 | 所有人 |
| `/清除炮決名單` | 清除本伺服器炮決記錄 | 主人限定 |

### 社交

| 指令 | 說明 | 權限 |
|------|------|------|
| `/認養寵物 用戶` | 邀請對方成為你的寵物 | 所有人 |
| `/認主人 用戶` | 邀請對方成為你的主人 | 所有人 |
| `/本群關係圖` | 視覺化本伺服器的主寵 + 母子關係網路 | 所有人 |
| `/賽博釣群友` | 放出釣魚按鈕，咬鉤者會被偽裝發言 | 所有人 |
| `/抽今日媽媽` | 從本群隨機抽一位成員作為你的媽媽（當日限一次） | 所有人 |
| `/認媽媽 用戶` | 強制指定一位成員作為你的媽媽 | 所有人 |
| `/拋棄兒子 用戶` | 解除指定用戶認你為媽媽的關係 | 所有人 |
| `/和今日媽媽斷絕關係` | 與今日抽到的媽媽解除關係 | 所有人 |
| `/電子鞭子 [用戶]` | 隨機或指定成員鞭打，記錄次數 | 所有人 |

### 管理

| 指令 | 說明 | 權限 |
|------|------|------|
| `/電子口球 time [who]` | 對成員套用 Timeout 禁言 | 主人直接執行，對他人需確認 |
| `/口球輪盤` | 1分鐘報名，隨機抽一人禁言 30 秒 | 所有人 |

---

## 設定說明

### Discord Bot（config.py）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `MASTER_ID` | `404111257008865280` | 主人的 Discord 用戶 ID |
| `GEMINI_MODEL_NAME` | `gemini-2.5-flash` | 使用的 Gemini 模型 |
| `API_DELAY` | `5.0` 秒 | 每次 API 請求最短間隔 |
| `HISTORY_MAX_TURNS` | `150` | 每頻道保留的最大歷史訊息筆數 |

### Pixiv 爬蟲（pixiv_config.py）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DOWNLOAD_WORKERS` | `6` | 並行下載 worker 數 |
| `DOWNLOAD_RATE_LIMIT_Mbps` | `50` | 下載頻寬上限（Mbps） |
| `TAG_PAGES_PER_VISIT` | `200` | 每個 tag/sort 每輪最多抓的頁數 |
| `USER_SCAN_BATCH_SIZE` | `100` | 每次 user_scan 掃描的有效用戶數 |
| `MIN_BOOKMARKS` | `0` | 最低收藏數過濾（0 = 不過濾） |
| `MAX_GALLERY_PAGES` | `100` | 每件漫畫作品最多索引的頁數 |
| `STATUS_WEB_PORT` | `8766` | Streamlit 狀態頁面 port |
| `ALL_TAGS` | （見 pixiv_config.py） | 爬取的 tag 列表 |

---

## Pixiv 爬蟲說明

### 爬取流程

1. **Tag 爬取**：依序爬取 `ALL_TAGS` 中每個 tag 的 `date_desc` / `date_asc` 兩個排序方向，每個 tag 最多 `TAG_PAGES_PER_VISIT` 頁
2. **Ranking 爬取**：每日執行一次，爬取 `day / week / month / day_male / day_female / week_original / week_rookie` 七種排行榜
3. **作者擴散**：新下載作品自動將其作者推入擴散佇列，爬取該作者全部作品
4. **相關作品擴散**：新作品的相關作品也會被採樣爬取，進一步擴展覆蓋率
5. **User ID 掃描**：從 user_id=1 起順序掃描，發現新作者自動爬取

### 去重機制

- **FAISS pHash 索引**：每張圖片計算 64-bit pHash，存入 FAISS 二值索引，Hamming 距離去重
- **SQLite 資料庫**：記錄每件作品的下載狀態與索引狀態，批次查詢已索引作品避免重複下載
- **斷點續爬**：tag 爬取進度存於 `tag_crawl_progress.json`，重啟後從斷點繼續

### 取得 Pixiv Refresh Token

```bash
pip install pixivpy3
python -c "
from pixivpy3 import AppPixivAPI
api = AppPixivAPI()
# 使用瀏覽器登入 Pixiv，從開發者工具取得 code
# 詳見 https://github.com/upbit/pixivpy/issues/158
"
```

---

## 效能與可靠性設計

- **Atomic write**：所有 JSON / TXT 狀態檔（`chat_history.json`、`nicknames.json`、`knowledge.json`、`summaries/*.txt`、`merit.json`、`relationships.json`、`artillery_records.json`、`wife_records.json`、`tag_crawl_progress.json`、`status.json` …）都採用「tmp 檔 + `os.replace`」寫入，確保程式中斷或斷電時不會留下半寫入的壞檔。
- **非同步存檔**：熱路徑（AI 回覆、訊息送出、爬蟲入庫）使用 `save_json_async` / `save_history_async`，把寫檔丟給 thread pool，不會阻塞 Discord `event loop`。
- **原生 aiohttp 抓取**：`web.py`、`graph_render.py` 的頭像、`commands/wife.py` 的頭像皆改用 `aiohttp` / `discord.Asset.read()`，取代會 block event loop 的 `requests.get`。
- **預編譯正則**：`main.py` 的 URL/指令/提及偵測、`utils/text_processing.py` 等熱路徑全部採用 module-level `re.compile`。
- **Gemini Chat 會話重建**：僅在歷史達到 `HISTORY_MAX_TURNS` 時重建，避免「曾經有附件就每一輪重建」的 N² 成本。
- **O(n) 摘要裁剪**：`summary.py` 以反向累計長度找起點，不用 `pop(0)` 的 O(n²) 迴圈。
- **多 Key 輪替**：`gemini_worker.py` 在配額或 5xx 錯誤時自動切換下一組 Gemini API Key，單一 Key 被封鎖仍可持續服務。

---

## 注意事項

- `.env` 包含敏感金鑰，請勿提交至 git（已加入 `.gitignore`）
- `data/chat_history.json` 儲存對話記錄，請定期備份
- Bot 需在 Discord Developer Portal 開啟 **Message Content Intent**
- LINE Bot 需設定 Webhook URL 為 `https://<your-domain>:8080/webhook`
- 圖片字型依賴 Windows 字型（`NotoSansTC-VF.ttf` / `msjhbd.ttc`），Linux 需另行安裝
- Pixiv 爬蟲需設定 `PIXIV_REFRESH_TOKEN`，否則 `/pixiv爬蟲` 會回傳未設定錯誤
- `pixivdata/` 目錄體積會隨爬取量持續增長（數萬張圖片可達數十 GB），請確認磁碟空間充足
- Streamlit 狀態頁面在 `/pixiv狀態` 指令時自動啟動（port 8766），也可手動執行 `streamlit run pixiv_status_app.py`
- 設定 `NGROK_AUTH_TOKEN` 後，狀態頁面可透過 ngrok 公開存取
