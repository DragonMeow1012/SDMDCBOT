# 小龍喵 Discord Bot

基於 Gemini AI 的 Discord / LINE 聊天機器人，支援多角色人格、暱稱系統、知識庫、網頁抓取、對話記憶跨重啟保留。

---

## 功能

- **AI 對話**：使用 Gemini 2.5 Flash 模型，@提及即可對話
- **雙人格模式**：主人（龍龍喵）與一般訪客使用不同人格設定
- **對話記憶**：聊天歷史儲存在本地 `data/chat_history.json`，重啟後自動載入
- **對話摘要**：自動將歷史序列化為 TXT（`data/summaries/`），供模型跨 session 參考
- **暱稱系統**：每位用戶可設定自訂暱稱，模型優先使用暱稱稱呼對方
- **知識庫**：可儲存跨頻道永久知識條目，支援文字或檔案上傳後 AI 分析
- **網頁抓取**：訊息中附上 URL 會自動抓取內容並摘要或回答問題
- **圖片反搜**：附圖訊息自動觸發反向圖片搜尋
- **LINE 整合**：支援 LINE Bot Webhook，LINE 聊天也能與 Gemini 對話
- **電子口球**：主人可對成員套用全伺服器禁言（Timeout）
- **API Key 輪替**：支援最多 4 組 Gemini API Key，超出限制自動切換

---

## 專案結構

```
DCbot_1.0/
├── main.py            # Discord 事件入口（on_ready、on_message）、斜線指令
├── config.py          # 環境變數、PERSONALITY 人格設定、常數
├── gemini_worker.py   # Gemini API Worker、模型初始化、Key 輪替
├── history.py         # 本地聊天歷史讀寫
├── nicknames.py       # 暱稱系統（load/save/build context）
├── knowledge.py       # 知識庫（data/knowledge.json）
├── summary.py         # 對話摘要序列化（data/summaries/）
├── web.py             # 網頁抓取（requests + BeautifulSoup）
├── line_bot.py        # LINE Bot Webhook 伺服器（port 8080）
├── reverse_search.py  # 圖片反向搜尋
├── data/
│   ├── chat_history.json    # 自動生成，儲存各頻道對話歷史
│   ├── nicknames.json       # 自動生成，儲存用戶暱稱
│   ├── knowledge.json       # 自動生成，儲存知識庫條目
│   ├── summaries/           # 各頻道對話摘要 TXT
│   └── bot.log              # 執行時 log（背景啟動時產生）
├── .env               # 金鑰設定（不提交 git）
├── .env.example       # 金鑰範本
├── requirements.txt   # Python 依賴套件
├── Dockerfile         # Docker 映像
├── docker-compose.yml # Docker Compose 設定
└── dc1.py             # 原始 Colab 版本（已棄用）
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
```

### 3. 設定金鑰

複製範本並填入真實金鑰：

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

啟動並將 log 寫入 `data/bot.log`：

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

查詢目前 Bot 的 PID：

```bash
ps aux | grep "main.py" | grep -v grep
```

關閉 Bot（指定 PID）：

```bash
kill <PID>
```

關閉 Bot（全部）：

```bash
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
| 附圖 + `@小龍喵` | 自動觸發圖片反向搜尋 |

---

## 斜線指令

| 指令 | 說明 | 權限 |
|------|------|------|
| `/nick 暱稱` | 設定自己的暱稱 | 所有人 |
| `/nick 暱稱 對象` | 設定指定成員的暱稱 | 主人限定 |
| `/kb add 文字` | 新增文字到知識庫 | 所有人 |
| `/kb add 檔案` | 上傳檔案並由 AI 分析後儲存 | 所有人 |
| `/kb remove 節次` | 刪除知識庫指定節次 | 主人限定 |
| `/kb list` | 列出知識庫所有節次 | 主人限定 |
| `/kb load` | 重新載入知識庫並注入當前頻道 | 所有人 |
| `/電子口球 成員 時長` | 對成員套用 Timeout 禁言 | 主人限定 |
| `/清除記憶` | 清除所有頻道的聊天記憶 | 主人限定 |
| `/清空知識庫` | 清空所有知識庫內容 | 主人限定 |

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
- LINE Bot 需在 LINE Developers Console 設定 Webhook URL 為 `https://<your-domain>:8080/webhook`
