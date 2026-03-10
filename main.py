"""
Discord Bot 主程式入口。
負責：Discord 事件處理、對話 session 管理、URL 偵測與分派。

啟動方式：
    PYTHONIOENCODING=utf-8 python -u main.py
"""
import sys
import re
import asyncio
import discord

# 修正 Windows cp932 編碼問題：使用 line_buffering=True 避免破壞 -u 無緩衝模式
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

from config import DISCORD_TOKEN, MASTER_ID
from history import load_history
from web import fetch_url
from gemini_worker import create_chat, msg_queue, gemini_worker
from nicknames import (
    load_nicknames, save_nicknames,
    get_nickname, build_user_context, build_all_nicknames_summary,
)

# --- Discord Client ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# chat_sessions 結構：
#   { channel_id (int) -> {
#       'chat_obj': Chat | None,
#       'personality': str | None,
#       'raw_history': list,
#       'current_web_context': str | None,
#   }}
chat_sessions: dict = {}
nicknames: dict[str, str] = {}   # { user_id_str -> nickname }
_worker_started: bool = False


def _init_session(cid: int, personality: str, sess: dict | None) -> None:
    """初始化或切換頻道的對話 session。"""
    raw_history = sess.get('raw_history', []) if sess else []
    web_context = sess.get('current_web_context') if sess else None

    chat_sessions[cid] = {
        'chat_obj': create_chat(personality, raw_history),
        'personality': personality,
        'raw_history': raw_history,
        'current_web_context': web_context,
    }


# ---------------------------------------------------------------------------
# !nick 指令處理（不經過 Gemini，直接操作暱稱檔案）
# 語法：
#   @Bot !nick 設定 <@user 或 user_id> <暱稱>   → 設定暱稱（主人：任意；訪客：僅限自己）
#   @Bot !nick 刪除 <@user 或 user_id>          → 刪除暱稱（主人限定）
#   @Bot !nick 列表                             → 列出所有暱稱（主人限定）
#   @Bot !nick 我的暱稱 <暱稱>                   → 設定自己的暱稱（任何人）
# ---------------------------------------------------------------------------
async def handle_nick_command(msg: discord.Message, args: str) -> bool:
    """
    處理 !nick 指令。
    回傳 True 表示已處理（不需再送 Gemini）；False 表示不是 nick 指令。
    """
    global nicknames

    args = args.strip()
    is_master = (msg.author.id == MASTER_ID)

    # !nick 列表（主人限定）
    if args in ('列表', 'list') and is_master:
        if not nicknames:
            await msg.reply('目前沒有任何已登記的暱稱。')
        else:
            lines = '\n'.join(f'`{uid}` → {nick}' for uid, nick in nicknames.items())
            await msg.reply(f'**已登記暱稱清單：**\n{lines}')
        return True

    # !nick 我的暱稱 <暱稱>（任何人，設定自己）
    if args.startswith('我的暱稱 ') or args.startswith('我的暱稱　'):
        new_nick = args[5:].strip()
        if not new_nick:
            await msg.reply('請提供要設定的暱稱。')
            return True
        nicknames[str(msg.author.id)] = new_nick
        save_nicknames(nicknames)
        await msg.reply(f'好的，我會記住你叫「{new_nick}」！')
        return True

    # !nick 設定 <target> <暱稱>
    if args.startswith('設定 ') or args.startswith('設定　'):
        parts = args[3:].strip().split(None, 1)
        if len(parts) < 2:
            await msg.reply('語法：`!nick 設定 <@user 或 user_id> <暱稱>`')
            return True

        target_raw, new_nick = parts[0], parts[1].strip()
        target_id = _parse_user_id(target_raw, msg)

        if target_id is None:
            await msg.reply('無法辨識目標用戶，請使用 @提及 或直接輸入 user_id。')
            return True

        # 權限檢查：訪客只能設定自己
        if not is_master and target_id != msg.author.id:
            await msg.reply('你只能設定自己的暱稱喔。')
            return True

        nicknames[str(target_id)] = new_nick
        save_nicknames(nicknames)
        await msg.reply(f'已將 `{target_id}` 的暱稱設為「{new_nick}」。')
        return True

    # !nick 刪除 <target>（主人限定）
    if (args.startswith('刪除 ') or args.startswith('刪除　')) and is_master:
        target_raw = args[3:].strip()
        target_id = _parse_user_id(target_raw, msg)

        if target_id is None:
            await msg.reply('無法辨識目標用戶。')
            return True

        uid_str = str(target_id)
        if uid_str in nicknames:
            removed = nicknames.pop(uid_str)
            save_nicknames(nicknames)
            await msg.reply(f'已刪除 `{target_id}` 的暱稱（原為「{removed}」）。')
        else:
            await msg.reply(f'`{target_id}` 沒有登記暱稱。')
        return True

    return False  # 不是 nick 指令


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
    global _worker_started, nicknames

    print(f'[OK] Logged in as: {client.user}')

    loaded = load_history()
    chat_sessions.update(loaded)

    nicknames = load_nicknames()

    if not _worker_started:
        _worker_started = True
        asyncio.create_task(gemini_worker(chat_sessions))

    print(f'[OK] Bot ready! {len(chat_sessions)} channels, {len(nicknames)} nicknames.')


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
    if not raw_text:
        await msg.reply('主...主人...請問...需...需要什麼協助嗎？喵嗚...')
        return

    # --- !nick 指令攔截（不送 Gemini）---
    if raw_text.startswith('!nick ') or raw_text.startswith('!nick　'):
        await handle_nick_command(msg, raw_text[6:])
        return

    print(f'[MSG] ch={cid} [{personality}]: {raw_text[:80]}')

    # --- 建立用戶身分前綴注入給模型 ---
    user_ctx = build_user_context(msg.author.id, nicknames)
    # 主人模式額外附上全部暱稱清單，方便模型稱呼其他用戶
    if is_master:
        nick_summary = build_all_nicknames_summary(nicknames)
        identity_prefix = f'{nick_summary}\n{user_ctx}\n'
    else:
        identity_prefix = f'{user_ctx}\n'

    prompt: str = raw_text

    # --- URL 偵測 ---
    if url_match := re.search(r'https?://\S+', prompt):
        url: str = url_match.group(0)
        query: str = prompt.replace(url, '').strip()

        await msg.channel.send('喵嗚~ 偵測到網址，正在抓取內容中...')
        print(f'[WEB] Fetching: {url}')

        content = await fetch_url(url)

        if content.startswith('錯誤:') or not content:
            await msg.reply(f'喵嗚... 抓取網頁失敗: {content}')
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

    # 將身分前綴合併進 prompt
    final_prompt = identity_prefix + prompt

    await msg_queue.put({
        'channel_id': cid,
        'prompt_text': final_prompt,
        'message_object': msg,
    })


if __name__ == '__main__':
    try:
        print('Connecting to Discord...')
        client.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print('[ERROR] Invalid Discord Bot Token. Check your .env file.')
    except KeyboardInterrupt:
        print('[STOP] Bot stopped by user.')
    except Exception as e:
        print(f'[ERROR] Failed to start bot: {e}')
