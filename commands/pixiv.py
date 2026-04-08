"""
Pixiv 爬取指令模組
- /pixiv爬蟲              開始全站背景爬取 
- /pixiv爬蟲 作者ID       爬取指定作者的所有作品 
- /pixiv停止              停止爬取（限主人）
- /pixiv狀態              查看爬取狀態與統計（所有人）

作者ID 欄位填：
  - Pixiv 作者的 user ID（數字）
"""
import asyncio
import logging
import threading
from pathlib import Path

import discord
from discord import app_commands

from config import MASTER_ID
import pixiv_database as db
import pixiv_crawler as crawler

# 設定 Pixiv 查詢日誌
LOG_DIR = Path("pixivdata/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOG_DIR / "pixiv_query.log"

file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
logger = logging.getLogger(__name__)
logger.addHandler(file_handler)
logger.setLevel(logging.INFO)

_stop_event: threading.Event | None = None
_crawl_thread: threading.Thread | None = None
_log_handler: "_DiscordLogHandler | None" = None


# ──────────────────────────────────────────────
# Discord 即時 Log Handler
# ──────────────────────────────────────────────

class _DiscordLogHandler(logging.Handler):
    """
    攔截 pixiv_crawler / pixiv_feature 的 log，每 3 秒批次傳送到 Discord 頻道。
    """
    _FLUSH_INTERVAL = 3.0   # 秒
    _MAX_LINES      = 25    # 每次最多傳幾行

    def __init__(self, channel: discord.TextChannel,
                 loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._channel = channel
        self._loop    = loop
        self._buf: list[str] = []
        self._lock  = threading.Lock()
        self._timer: threading.Timer | None = None
        self.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))

    def emit(self, record: logging.LogRecord):
        line = self.format(record)
        with self._lock:
            self._buf.append(line)
            if self._timer is None or not self._timer.is_alive():
                self._timer = threading.Timer(self._FLUSH_INTERVAL, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self):
        with self._lock:
            if not self._buf:
                return
            lines, self._buf = self._buf[:self._MAX_LINES], self._buf[self._MAX_LINES:]
            has_more = bool(self._buf)

        text = "```\n" + "\n".join(lines) + "\n```"
        asyncio.run_coroutine_threadsafe(
            self._channel.send(text), self._loop
        )

        if has_more:
            with self._lock:
                self._timer = threading.Timer(0.5, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def close(self):
        if self._timer and self._timer.is_alive():
            self._timer.cancel()
        self._flush()
        super().close()


def _attach_log_handler(channel: discord.TextChannel,
                        loop: asyncio.AbstractEventLoop) -> "_DiscordLogHandler":
    handler = _DiscordLogHandler(channel, loop)
    logging.getLogger("pixiv_crawler").addHandler(handler)
    logging.getLogger("pixiv_feature").addHandler(handler)
    return handler


def _detach_log_handler(handler: "_DiscordLogHandler"):
    logging.getLogger("pixiv_crawler").removeHandler(handler)
    logging.getLogger("pixiv_feature").removeHandler(handler)
    handler.close()


# ──────────────────────────────────────────────
# 執行緒目標函式
# ──────────────────────────────────────────────

def _is_running() -> bool:
    return _crawl_thread is not None and _crawl_thread.is_alive()


def _run_full(stop_event: threading.Event):
    try:
        crawler.run_full_crawl(stop_event)
    except Exception as e:
        logger.error(f"全站爬取異常: {e}")


def _run_user(user_id: int, stop_event: threading.Event,
              status_callback=None):
    try:
        crawler.crawl_user_by_id(user_id, stop_event,
                                 status_callback=status_callback)
    except Exception as e:
        logger.error(f"作者爬取失敗 (ID:{user_id}): {e}")


# ──────────────────────────────────────────────
# 指令註冊
# ──────────────────────────────────────────────

def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="pixiv爬蟲", description="Pixiv 爬蟲")
    @app_commands.describe(author_id="作者 user ID（數字）")
    async def pixiv_start(interaction: discord.Interaction, author_id: str = ""):
        global _stop_event, _crawl_thread, _log_handler

        if _is_running():
            await interaction.response.send_message("爬取已在執行中，請先使用 /pixiv停止", ephemeral=True)
            return

        loop    = asyncio.get_event_loop()
        channel = interaction.channel

        _stop_event = threading.Event()
        _log_handler = _attach_log_handler(channel, loop)

        if author_id.strip():
            raw = author_id.strip()
            if not raw.isdigit():
                await interaction.response.send_message("ID 必須是數字", ephemeral=True)
                _detach_log_handler(_log_handler)
                _log_handler = None
                return

            given_id = int(raw)
            await interaction.response.send_message(
                f"正在查詢作者 ID `{given_id}`，請稍候...", ephemeral=True
            )

            # 直接視為作者 ID，查詢作者名稱
            uid = given_id
            try:
                uname = await loop.run_in_executor(
                    None, crawler.get_user_name, uid
                )
                logger.info(f"作者 {uid} 的名稱：{uname}")
            except Exception as e:
                logger.warning(f"查詢作者 {uid} 名稱失敗: {e}")
                uname = str(uid)

            author_label = uname if uname != str(uid) else str(uid)
            notice = f"作者：**{author_label}**，開始爬取作品"

            def _on_edit_done(future: "asyncio.Future[None]"):
                try:
                    future.result()
                except Exception:
                    pass

            def _format_progress(total: int, downloaded: int, indexed: int, done: bool) -> str:
                progress_title = "進度(已完成):" if done else "進度:"
                return (
                    f"作者：{author_label}，開始爬取作品\n"
                    f"{progress_title}\n"
                    f"總作品數：{total}\n"
                    f"已下載：{downloaded}\n"
                    f"已建立索引：{indexed}"
                )

            def _status_callback(counters: dict, total_artworks: int, done: bool) -> None:
                try:
                    s = db.stats()
                except Exception:
                    s = {"total": 0, "downloaded": 0, "indexed": 0}
                content = _format_progress(
                    s["total"], s["downloaded"], s["indexed"], done
                )
                future = asyncio.run_coroutine_threadsafe(
                    interaction.edit_original_response(content=content),
                    loop,
                )
                future.add_done_callback(_on_edit_done)

            _crawl_thread = threading.Thread(
                target=_run_user,
                args=(uid, _stop_event, _status_callback),
                daemon=True,
                name="pixiv-crawler",
            )
            _crawl_thread.start()

            try:
                s = await loop.run_in_executor(None, db.stats)
            except Exception:
                s = {"total": 0, "downloaded": 0, "indexed": 0}

            await interaction.edit_original_response(
                content=_format_progress(s["total"], s["downloaded"], s["indexed"])
            )

        else:
            _crawl_thread = threading.Thread(
                target=_run_full,
                args=(_stop_event,),
                daemon=True,
                name="pixiv-crawler",
            )
            _crawl_thread.start()
            await channel.send("Pixiv 全站爬取已啟動，log 將即時輸出在此頻道")

    @tree.command(name="pixiv停止", description="停止 Pixiv 背景爬取")
    async def pixiv_stop(interaction: discord.Interaction):
        global _log_handler

        if not _is_running():
            await interaction.response.send_message("目前沒有執行中的爬取", ephemeral=True)
            return

        _stop_event.set()
        await interaction.response.send_message(
            "已傳送停止訊號，爬取將在當前批次完成後結束", ephemeral=True
        )

        # 等執行緒結束後再移除 handler（最多等 10 秒）
        def _cleanup():
            global _log_handler
            if _crawl_thread:
                _crawl_thread.join(timeout=10)
            if _log_handler:
                _detach_log_handler(_log_handler)
                _log_handler = None

        threading.Thread(target=_cleanup, daemon=True).start()

    @tree.command(name="pixiv狀態", description="查看 Pixiv 爬取狀態與統計")
    async def pixiv_status(interaction: discord.Interaction):
        running = _is_running()
        try:
            loop = asyncio.get_event_loop()
            s = await loop.run_in_executor(None, db.stats)
            status_str = "執行中" if running else "已停止"
            msg = (
                f"**Pixiv 爬取狀態：{status_str}**\n"
                f"總作品數：{s['total']}\n"
                f"已下載：{s['downloaded']}\n"
                f"已建立索引：{s['indexed']}"
            )
        except Exception as e:
            msg = f"**Pixiv 爬取狀態：{'執行中' if running else '已停止'}**\n無法讀取統計：{e}"
        await interaction.response.send_message(msg, ephemeral=True)
