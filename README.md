# 小龍喵 Discord Bot

基於 Gemini AI 的 Discord 聊天機器人，支援多角色人格、網頁抓取、對話記憶跨重啟保留。

---

## 功能

- **AI 對話**：使用 Gemini 2.5 Flash 模型，@提及即可對話
- **雙人格模式**：主人（龍龍喵）與一般訪客使用不同人格設定
- **對話記憶**：聊天歷史儲存在本地 `data/chat_history.json`，重啟後自動載入
- **網頁抓取**：訊息中附上 URL 會自動抓取內容並摘要或回答問題
- **API Key 輪替**：支援最多 4 組 Gemini API Key，超出限制自動切換

---

## 專案結構

```
DCbot_1.0/
├── main.py            # Discord 事件入口（on_ready、on_message）
├── config.py          # 環境變數、PERSONALITY 人格設定、常數
├── gemini_worker.py   # Gemini API Worker、模型初始化、Key 輪替
├── history.py         # 本地聊天歷史讀寫
├── web.py             # 網頁抓取（requests + BeautifulSoup）
├── data/
│   ├── chat_history.json   # 自動生成，儲存各頻道對話歷史
│   └── bot.log             # 執行時 log（背景啟動時產生）
├── .env               # 金鑰設定（不提交 git）
├── .env.example       # 金鑰範本
├── requirements.txt   # Python 依賴套件
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
```

- Discord Token：[Discord Developer Portal](https://discord.com/developers/applications)
- Gemini API Key：[Google AI Studio](https://aistudio.google.com/apikey)

---

## 啟動

### 前景執行（開發用）

```bash
PYTHONIOENCODING=utf-8 python -u main.py
```

### 背景執行（持續運行）

```bash
PYTHONIOENCODING=utf-8 python -u main.py > data/bot.log 2>&1 &
```

查看 log：

```bash
cat data/bot.log
```

停止 Bot：

```bash
pkill -f "main.py"
```

---

## 使用方式

在 Discord 頻道中 **@小龍喵** 即可開始對話。

| 操作 | 說明 |
|------|------|
| `@小龍喵 你好` | 一般對話 |
| `@小龍喵 https://example.com` | 抓取網頁並摘要 |
| `@小龍喵 https://example.com 這篇說什麼？` | 抓取後依問題回答 |
| `@小龍喵 上面那篇有提到...嗎？` | 使用已抓取的上下文繼續提問 |

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
