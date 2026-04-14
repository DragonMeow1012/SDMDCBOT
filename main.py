"""
Discord Bot 主程式入口。
負責：Discord Client 設定、事件處理（on_ready / on_message）、
      URL 偵測、附件處理、啟動 Gemini Worker 與 LINE Bot。

啟動方式：
    PYTHONIOENCODING=utf-8 python -u main.py
"""
import os
import sys
import re
import asyncio
import discord
from discord import app_commands

# 修正 Windows cp932 編碼問題
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

from logger import setup_logger
setup_logger()

from config import DISCORD_TOKEN, MASTER_ID
from history import load_history, save_history
from web import fetch_url
from gemini_worker import create_chat, msg_queue, gemini_worker
from nicknames import load_nicknames, build_all_nicknames_summary
from knowledge import (
    load_knowledge, build_knowledge_context, consolidate_knowledge,
)
from reverse_search import reverse_image_search
from summary import load_summary
import state
from commands import setup_all
from commands.kb import handle_kb_command

# ---------------------------------------------------------------------------
# Discord Client
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# 注冊所有斜線指令
setup_all(tree)

# ---------------------------------------------------------------------------
# 附件處理常數
# ---------------------------------------------------------------------------
_SOURCE_KEYWORDS: frozenset[str] = frozenset({
    '來源', '圖源', '出處', '哪裡', '從哪', '誰畫', '作者', '作品', '找圖', 'source', 'where', 'origin',
    '找本子', '找本本', '番號', '號碼', '查本子',
})

_INLINE_MIME_TYPES: frozenset[str] = frozenset({
    'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf',
})

_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    '.txt', '.py', '.js', '.ts', '.json', '.md', '.csv', '.html', '.htm',
    '.css', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.log',
    '.sh', '.bat', '.c', '.cpp', '.h', '.java', '.go', '.rs', '.rb',
})


def _is_source_query(text: str) -> bool:
    return any(kw in text.lower() for kw in _SOURCE_KEYWORDS)


def _guess_mime(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
        '.pdf': 'application/pdf',
    }.get(ext, 'application/octet-stream')


def _is_text_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in _TEXT_EXTENSIONS


def _strip_bot_mention(text: str, bot_id: int) -> str:
    # Discord mention 可能是 <@id> 或 <@!id>
    return re.sub(rf'<@!?\s*{bot_id}\s*>', '', text).strip()


# ---------------------------------------------------------------------------
# Session 初始化
# ---------------------------------------------------------------------------
def _init_session(cid: int, personality: str, sess: dict | None) -> None:
    raw_history = sess.get('raw_history', []) if sess else []
    web_context = sess.get('current_web_context') if sess else None
    summary     = load_summary(cid)

    state.chat_sessions[cid] = {
        'chat_obj':           create_chat(personality, raw_history, summary),
        'personality':        personality,
        'raw_history':        raw_history,
        'current_web_context': web_context,
    }


# ---------------------------------------------------------------------------
# Discord 事件
# ---------------------------------------------------------------------------
@client.event
async def on_ready() -> None:
    print(f'[OK] Logged in as: {client.user}')

    loaded = load_history()
    state.chat_sessions.update(loaded)

    state.nicknames.update(load_nicknames())
    state.knowledge_entries[:] = load_knowledge()
    consolidate_knowledge(state.knowledge_entries)

    if not state._worker_started:
        state._worker_started = True
        asyncio.create_task(gemini_worker(state.chat_sessions, state.knowledge_entries))

    await tree.sync()
    print(f'[OK] Bot ready! {len(state.chat_sessions)} channels, '
          f'{len(state.nicknames)} nicknames, {len(state.knowledge_entries)} KB entries.')


@client.event
async def on_message(msg: discord.Message) -> None:
    if msg.author == client.user:
        return

    mentioned: bool = client.user.mentioned_in(msg)

    # 移除 @提及，取得純文字
    raw_text: str = _strip_bot_mention(msg.content, client.user.id)

    # !kb 指令攔截（不送 Gemini；不需要 @ 也能用）
    if re.match(r'^!kb(\s|$)', raw_text):
        args = raw_text[3:].strip()
        await handle_kb_command(msg, args)
        return

    # 只有被 @ 時才走 AI 對話流程
    if not mentioned:
        return

    cid: int        = msg.channel.id
    is_master: bool = (msg.author.id == MASTER_ID)
    personality: str = 'master' if is_master else 'general'

    sess = state.chat_sessions.get(cid)
    if not sess or sess.get('personality') != personality:
        print(f'[INIT] ch={cid} personality={personality}')
        _init_session(cid, personality, sess)

    if not raw_text and not msg.attachments:
        await msg.reply('主...主人...請問...需...需要什麼協助嗎？喵嗚...')
        return

    print(f'[MSG] ch={cid} [{personality}]: {raw_text[:80]}')

    # 用戶身分前綴
    uid_str      = str(msg.author.id)
    nick         = state.nicknames.get(uid_str)
    display_name = msg.author.display_name

    if nick:
        user_ctx = f'[用戶: {nick}]'
    else:
        user_ctx = f'[用戶: {display_name}]'

    if is_master:
        nick_summary    = build_all_nicknames_summary(state.nicknames)
        identity_prefix = f'{nick_summary}\n{user_ctx}\n'
    else:
        identity_prefix = f'{user_ctx}\n'

    prompt: str = raw_text if raw_text else '請描述這個附件的內容。'

    # 附件處理
    file_parts: list[dict] = []
    if msg.attachments:
        await msg.channel.send('喵嗚~ 偵測到附件，讀取中...')
        for attachment in msg.attachments:
            mime = (attachment.content_type or _guess_mime(attachment.filename)).split(';')[0].strip()

            if mime in _INLINE_MIME_TYPES:
                if attachment.size > 20 * 1024 * 1024:
                    await msg.reply(
                        f'`{attachment.filename}` 檔案過大（{attachment.size / 1024 / 1024:.1f} MB），'
                        '最大支援 20 MB 喵！')
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
                    '目前支援：圖片（jpg/png/gif/webp）、PDF、文字檔。')

    # 以圖搜圖（關鍵字觸發）
    if file_parts and _is_source_query(prompt):
        await msg.channel.send('喵嗚~ 正在以圖搜圖，尋找來源中...')
        result = await reverse_image_search(
            file_parts[-1]['data'], file_parts[-1]['mime_type'])
        await msg.reply(result)
        return

    # URL 偵測
    if url_match := re.search(r'https?://[^\s\)\]\>\"\'`]+(?<![.,;:!?])', prompt):
        url: str   = url_match.group(0)
        query: str = prompt.replace(url, '').strip()

        await msg.channel.send('喵嗚~ 偵測到網址，正在抓取內容中...')
        print(f'[WEB] Fetching: {url}')
        content = await fetch_url(url)

        if content.startswith('錯誤:') or not content:
            print(f'[WEB] 抓取失敗: {url}')
        else:
            state.chat_sessions[cid]['current_web_context'] = content
            await msg.reply('喵嗚！已成功抓取網頁內容囉！')
            prompt = (
                f'請簡潔摘要以下網頁內容：\n```\n{content}\n```\n原始網址：{url}'
                if not query else
                f'以下是從網址 `{url}` 抓取到的內容：\n```\n{content}\n```\n請根據這些內容，回答我的問題：{query}'
            )

    elif web_ctx := state.chat_sessions[cid].get('current_web_context'):
        prompt = f'根據我之前讀取的內容：\n```\n{web_ctx}\n```\n請問：{prompt}'

    # 注入知識庫
    kb_ctx       = build_knowledge_context(state.knowledge_entries)
    final_prompt = (kb_ctx + identity_prefix + prompt) if kb_ctx else (identity_prefix + prompt)

    await msg_queue.put({
        'channel_id':  cid,
        'prompt_text': final_prompt,
        'file_parts':  file_parts,
        'reply_fn':    msg.reply,
        'send_fn':     msg.channel.send,
        'typing_ctx':  msg.channel.typing(),
        'kb_save':     None,
    })


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------
async def _main() -> None:
    from config import LINE_CHANNEL_ACCESS_TOKEN, LINE_WEBHOOK_PORT
    from line_bot import start_line_server

    tasks = []
    if LINE_CHANNEL_ACCESS_TOKEN:
        tasks.append(asyncio.create_task(
            start_line_server(state.chat_sessions, state.knowledge_entries,
                              LINE_WEBHOOK_PORT, _init_session)
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
        if state.chat_sessions:
            print('[SAVE] 關閉前儲存聊天歷史...')
            save_history(state.chat_sessions)
