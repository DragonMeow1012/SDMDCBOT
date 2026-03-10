"""
Gemini API 工作器模組（使用新版 google-genai SDK）。
負責：Client 初始化、API Key 輪替、請求佇列限速處理。
"""
import asyncio
from google import genai
from google.genai import types

from config import GEMINI_API_KEYS, GEMINI_MODEL_NAME, PERSONALITY, API_DELAY
from history import save_history

# --- API Key 輪替 ---
_key_index: int = 0
_client: genai.Client = None


def _create_client() -> genai.Client:
    key = GEMINI_API_KEYS[_key_index % len(GEMINI_API_KEYS)]
    return genai.Client(api_key=key)


def rotate_api_key() -> None:
    """切換至下一組 API Key 並重建 Client。"""
    global _key_index, _client
    _key_index += 1
    _client = _create_client()
    print(f"[ROTATE] API Key index={_key_index % len(GEMINI_API_KEYS)}")


# 初始化 Client
_client = _create_client()

# --- 各人格的對話設定 ---
_CHAT_CONFIGS: dict[str, types.GenerateContentConfig] = {
    'general': types.GenerateContentConfig(
        system_instruction=PERSONALITY['general']
    ),
    'master': types.GenerateContentConfig(
        system_instruction=PERSONALITY['master']
    ),
}


def create_chat(personality: str, history: list) -> genai.chats.Chat:
    """
    建立新的 Gemini Chat session。
    history 格式：[{"role": "user", "parts": [{"text": "..."}]}, ...]
    """
    return _client.chats.create(
        model=GEMINI_MODEL_NAME,
        config=_CHAT_CONFIGS[personality],
        history=history or [],
    )


# --- 請求佇列 ---
msg_queue: asyncio.Queue = asyncio.Queue()
_last_api_time: float = 0.0


async def gemini_worker(chat_sessions: dict) -> None:
    """
    持續從 msg_queue 取出請求並呼叫 Gemini API。
    確保 task_done() 在所有路徑皆被呼叫。
    """
    global _last_api_time

    while True:
        req = await msg_queue.get()
        cid: int = req['channel_id']
        prompt: str = req['prompt_text']
        msg = req['message_object']

        try:
            sess = chat_sessions.get(cid)
            if not sess or sess.get('chat_obj') is None:
                print(f"[WARN] ch={cid} 無對話物件，略過此請求")
                continue

            chat = sess['chat_obj']

            # 限速：距上次呼叫需間隔 API_DELAY 秒
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - _last_api_time
            if elapsed < API_DELAY:
                await asyncio.sleep(API_DELAY - elapsed)

            async with msg.channel.typing():
                try:
                    resp = await asyncio.to_thread(chat.send_message, prompt)
                    _last_api_time = asyncio.get_running_loop().time()
                    text: str = resp.text

                    if len(text) > 2000:
                        await msg.reply("我的回應太長了，我會分段傳送：")
                        for i in range(0, len(text), 1990):
                            await msg.channel.send(text[i:i + 1990])
                    else:
                        await msg.reply(text)

                    save_history(chat_sessions)

                except Exception as e:
                    err = str(e).lower()
                    if any(kw in err for kw in ["quota", "rate limit", "429", "resource_exhausted", "toomanyrequests"]):
                        print(f"[WARN] 限速觸發 ch={cid}: {e}")
                        rotate_api_key()
                        await msg.reply("你們傳送的太快了喵! 等我換一下API再試一次！嗚喵!")
                    elif "timeout" in err:
                        print(f"[WARN] API逾時 ch={cid}: {e}")
                        await msg.reply("喵嗚...Gemini API 回應時間太長了，請稍後再試試看喔！")
                    else:
                        print(f"[ERROR] {type(e).__name__}: {e}")
                        await msg.reply("抱歉，我在處理您的請求時遇到了未知的錯誤喵。")

        finally:
            msg_queue.task_done()
