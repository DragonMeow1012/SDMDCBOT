"""
Discord Bot 主程式入口。
負責：Discord Client 設定、事件處理（on_ready / on_message）、
      URL 偵測、附件處理、啟動 Gemini Worker 與 LINE Bot。

啟動方式：
    PYTHONIOENCODING=utf-8 python -u main.py
"""
import io
import os
import sys
import re
import asyncio
import signal
import time
import discord
from discord import app_commands

# 修正 Windows cp932 編碼問題
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

from logger import setup_logger
setup_logger()

# Windows 上 pyngrok / streamlit 子行程斷線時常會把 CTRL 信號灌回主 console，
# 單次 Ctrl+C 可能是假信號；要求 2 秒內連按兩次才真正關機。
_SIGINT_CONFIRM_WINDOW = 2.0
_last_sigint_ts = 0.0

def _handle_shutdown_signal(signum, frame):
    global _last_sigint_ts
    now = time.time()
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    if now - _last_sigint_ts < _SIGINT_CONFIRM_WINDOW:
        print(f'[STOP] 確認中斷（第二次 {sig_name}），關閉 bot...')
        raise KeyboardInterrupt
    _last_sigint_ts = now
    print(f'[SIGNAL] 收到 {sig_name} — 若為手動關機請於 {_SIGINT_CONFIRM_WINDOW:g}s 內再按一次 Ctrl+C；'
          '否則視為子行程洩漏的假信號，忽略。')

signal.signal(signal.SIGINT, _handle_shutdown_signal)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, _handle_shutdown_signal)

from config import DISCORD_TOKEN, MASTER_ID
from history import load_history, save_history
from web import fetch_url
from gemini_worker import msg_queue, gemini_worker
from ai_session import ensure_session
from nicknames import load_nicknames, build_all_nicknames_summary
from knowledge import (
    load_knowledge, build_knowledge_context, consolidate_knowledge,
)
from reverse_search import reverse_image_search
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

_TRANSLATE_KEYWORDS: frozenset[str] = frozenset({
    '翻譯', '中文化', '漢化', 'translate',
})

# 目標語言關鍵字（早 match 早結束；若都沒命中 → 預設繁體中文）
_LANG_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (('英文', '英語', 'english', '英譯'), 'English'),
    (('日文', '日語', '日本語', 'japanese'), '日本語'),
    (('簡體', '簡中', 'simplified'), '簡體中文'),
    (('韓文', '韓語', 'korean'), '한국어'),
)

_INLINE_MIME_TYPES: frozenset[str] = frozenset({
    'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf',
})

_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    '.txt', '.py', '.js', '.ts', '.json', '.md', '.csv', '.html', '.htm',
    '.css', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.log',
    '.sh', '.bat', '.c', '.cpp', '.h', '.java', '.go', '.rs', '.rb',
})

_MIME_BY_EXT: dict[str, str] = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
    '.pdf': 'application/pdf',
}

# 預先編譯 hot-path 正規表達式，避免每次訊息都重建
_URL_RE = re.compile(r'https?://[^\s\)\]\>\"\'`]+(?<![.,;:!?])')
_KB_RE = re.compile(r'^!kb(\s|$)')


def _is_source_query(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _SOURCE_KEYWORDS)


def _is_translate_query(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _TRANSLATE_KEYWORDS)


def _detect_target_lang(text: str) -> str:
    lowered = text.lower()
    for hints, lang in _LANG_HINTS:
        if any(h in lowered for h in hints):
            return lang
    return '繁體中文'


def _guess_mime(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_BY_EXT.get(ext, 'application/octet-stream')


def _is_text_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in _TEXT_EXTENSIONS


_MENTION_RE_CACHE: dict[int, re.Pattern[str]] = {}


def _strip_bot_mention(text: str, bot_id: int) -> str:
    """移除訊息中對 Bot 的 @ 提及（<@id> 或 <@!id>）。"""
    pat = _MENTION_RE_CACHE.get(bot_id)
    if pat is None:
        pat = re.compile(rf'<@!?\s*{bot_id}\s*>')
        _MENTION_RE_CACHE[bot_id] = pat
    return pat.sub('', text).strip()


# ---------------------------------------------------------------------------
# Session 初始化
# ---------------------------------------------------------------------------
def _init_session(cid: int, personality: str, sess: dict | None) -> None:
    ensure_session(state.chat_sessions, cid, personality, sess)


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

    # 只認內文直接出現 <@bot_id> 的情況；raw_mentions 不包含 @everyone / @here / 回覆自動附帶的 ping
    mentioned: bool = bool(client.user and client.user.id in msg.raw_mentions)

    # 移除 @提及，取得純文字
    raw_text: str = _strip_bot_mention(msg.content, client.user.id)

    # !kb 指令攔截（不送 Gemini；不需要 @ 也能用）
    if _KB_RE.match(raw_text):
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
    translate_notice: discord.Message | None = None
    if msg.attachments:
        # 先依意圖決定提示語句，避免「讀取中」+「翻譯中」兩條訊息
        if _is_translate_query(prompt):
            n_imgs = sum(
                1 for a in msg.attachments
                if (a.content_type or _guess_mime(a.filename)).split(';')[0].startswith('image/')
            )
            translate_notice = await msg.channel.send(
                f'喵嗚~ 偵測到附件，正在翻譯（共 {n_imgs} 張）...')
        elif _is_source_query(prompt):
            await msg.channel.send('喵嗚~ 偵測到附件，正在以圖搜圖...')
        else:
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

    # 翻譯漫畫（關鍵字觸發；只翻譯圖片附件，PDF 等略過）
    if file_parts and _is_translate_query(prompt):
        image_parts = [fp for fp in file_parts if fp['mime_type'].startswith('image/')]
        if image_parts:
            target_lang = _detect_target_lang(prompt)
            from manga_translate import translate_image
            files: list[discord.File] = []
            for idx, fp in enumerate(image_parts, 1):
                try:
                    out = await translate_image(fp['data'], fp['mime_type'], target_lang)
                except Exception as e:
                    print(f'[TRANSLATE] 第 {idx} 張失敗: {type(e).__name__}: {e}')
                    await msg.reply(f'第 {idx} 張翻譯失敗：{type(e).__name__}: {e}')
                    continue
                files.append(discord.File(io.BytesIO(out), filename=f'translated_{idx}.png'))
            if files:
                # 把譯圖直接編輯回原通知訊息，不再另發訊息
                done_text = '小龍喵幫你翻譯好了喵!'
                if translate_notice is not None:
                    try:
                        await translate_notice.edit(content=done_text, attachments=files)
                    except Exception as e:
                        print(f'[TRANSLATE] 編輯通知失敗、改用 reply: {type(e).__name__}: {e}')
                        await msg.reply(content=done_text, files=files)
                else:
                    await msg.reply(content=done_text, files=files)
            elif translate_notice is not None:
                try:
                    await translate_notice.edit(content='翻譯全部失敗')
                except Exception:
                    pass
            return

    # 以圖搜圖（關鍵字觸發）
    if file_parts and _is_source_query(prompt):
        result = await reverse_image_search(
            file_parts[-1]['data'], file_parts[-1]['mime_type'])
        await msg.reply(result)
        return

    # URL 偵測
    sess = state.chat_sessions[cid]
    if url_match := _URL_RE.search(prompt):
        url: str   = url_match.group(0)
        query: str = prompt.replace(url, '').strip()

        await msg.channel.send('喵嗚~ 偵測到網址，正在抓取內容中...')
        print(f'[WEB] Fetching: {url}')
        content = await fetch_url(url)

        if content.startswith('錯誤:') or not content:
            print(f'[WEB] 抓取失敗: {url}')
        else:
            sess['current_web_context'] = content
            await msg.reply('喵嗚！已成功抓取網頁內容囉！')
            prompt = (
                f'請簡潔摘要以下網頁內容：\n```\n{content}\n```\n原始網址：{url}'
                if not query else
                f'以下是從網址 `{url}` 抓取到的內容：\n```\n{content}\n```\n請根據這些內容，回答我的問題：{query}'
            )

    elif web_ctx := sess.get('current_web_context'):
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
    import manga_translator_server

    # 同生命週期 spawn manga-translator API server（autostart 開啟時）
    manga_translator_server.start()

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
        try:
            import manga_translator_server
            manga_translator_server.stop()
        except Exception as e:
            print(f'[STOP] manga-translator stop 失敗: {e}')
        if state.chat_sessions:
            print('[SAVE] 關閉前儲存聊天歷史...')
            save_history(state.chat_sessions)
