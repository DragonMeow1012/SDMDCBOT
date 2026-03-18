# 小龍喵 Discord Bot

基於 Gemini AI 的 Discord / LINE 聊天機器人，支援多角色人格、暱稱系統、知識庫、網頁抓取、圖片反搜、對話記憶跨重啟保留。

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
- **API Key 輪替**：支援最多 4 組 Gemini API Key，超出限制自動切換

---

## 專案結構

```
DCbot_1.0/
├── main.py               # 入口：Discord 事件、session 管理、URL/附件處理
├── state.py              # 共享可變狀態（chat_sessions / nicknames / knowledge_entries）
├── config.py             # 環境變數、PERSONALITY 人格設定、常數
├── gemini_worker.py      # Gemini API Worker、模型初始化、Key 輪替
├── history.py            # 本地聊天歷史讀寫（data/chat_history.json）
├── nicknames.py          # 暱稱系統（load/save/build context）
├── knowledge.py          # 知識庫（data/knowledge.json）
├── summary.py            # 對話摘要序列化（data/summaries/）
├── web.py                # 網頁抓取（requests + BeautifulSoup）
├── line_bot.py           # LINE Bot Webhook 伺服器（port 8080）
├── reverse_search.py     # 圖片反向搜尋（SauceNAO + soutubot）
├── quote_image.py        # 名言佳句圖片生成（Pillow，1920×1080）
├── commands/             # 斜線指令套件
│   ├── __init__.py       # setup_all(tree) 統一注冊
│   ├── admin.py          # /清除記憶、/清空知識庫
│   ├── nick.py           # /nick
│   ├── gag.py            # /電子口球、/口球輪盤
│   ├── fun.py            # /電子氣泡紙、/電子木魚、/電子木魚功德排行榜
│   │                     # /賽博體重計、/擲硬幣、/擲硬幣幹話版
│   ├── social.py         # /認養寵物、/認主人、/本群關係圖、/賽博釣群友
│   ├── artillery.py      # /炮決蘿莉控、/炮決排行、/清除炮決名單
│   ├── quote.py          # 右鍵選單：名言佳句、Make it Quote
│   ├── search.py         # /以圖搜圖
│   └── kb.py             # /kb 群組（add/remove/list/load）、!kb 文字指令
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
├── .env                  # 金鑰設定（不提交 git）
├── .env.example          # 金鑰範本
├── requirements.txt      # Python 依賴套件
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

# LINE Bot（可選）
LINE_CHANNEL_ACCESS_TOKEN=你的_LINE_Channel_Access_Token
LINE_CHANNEL_SECRET=你的_LINE_Channel_Secret
```

- Discord Token：[Discord Developer Portal](https://discord.com/developers/applications)
- Gemini API Key：[Google AI Studio](https://aistudio.google.com/apikey)
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

### 娛樂

| 指令 | 說明 | 權限 |
|------|------|------|
| `/擲硬幣` | 擲一枚硬幣，正面或反面 | 所有人 |
| `/擲硬幣幹話版` | 硬幣先歷經奇妙旅程，1~10 句後揭曉結果 | 所有人 |
| `/電子氣泡紙 尺寸` | 發送可點擊的電子氣泡紙（5×2 / 10×5） | 所有人 |
| `/電子木魚` | 敲木魚積功德按鈕 | 所有人 |
| `/電子木魚功德排行榜` | 功德 TOP10 排行榜 | 所有人 |
| `/賽博體重計` | 量測賽博體重 | 所有人 |
| `/炮決蘿莉控 [用戶]` | 隨機或指定炮決，並記錄次數💀 | 所有人 |
| `/炮決排行` | 被炮決次數 TOP10 | 所有人 |
| `/清除炮決名單` | 清除本伺服器炮決記錄 | 主人限定 |

### 社交

| 指令 | 說明 | 權限 |
|------|------|------|
| `/認養寵物 用戶` | 邀請對方成為你的寵物🐾 | 所有人 |
| `/認主人 用戶` | 邀請對方成為你的主人🐾 | 所有人 |
| `/本群關係圖` | 樹狀顯示本伺服器主寵關係 | 所有人 |
| `/賽博釣群友` | 放出釣魚按鈕，咬鉤者會被偽裝發言🪝 | 所有人 |

### 管理

| 指令 | 說明 | 權限 |
|------|------|------|
| `/電子口球 time [who]` | 對成員套用 Timeout 禁言🔇 | 主人直接執行，對他人需確認 |
| `/口球輪盤` | 1分鐘報名，隨機抽一人禁言 30 秒💀 | 所有人 |

---

## 設定說明

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `MASTER_ID` | `404111257008865280` | 主人的 Discord 用戶 ID |
| `GEMINI_MODEL_NAME` | `gemini-2.5-flash` | 使用的 Gemini 模型 |
| `API_DELAY` | `5.0` 秒 | 每次 API 請求最短間隔 |

---

## 注意事項

- `.env` 包含敏感金鑰，請勿提交至 git（已加入 `.gitignore`）
- `data/chat_history.json` 儲存對話記錄，請定期備份
- Bot 需在 Discord Developer Portal 開啟 **Message Content Intent**
- LINE Bot 需設定 Webhook URL 為 `https://<your-domain>:8080/webhook`
- 圖片字型依賴 Windows 字型（`NotoSansTC-VF.ttf` / `msjhbd.ttc`），Linux 需另行安裝
