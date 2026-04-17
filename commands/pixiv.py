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
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

from config import MASTER_ID, NGROK_AUTH_TOKEN
import pixiv_database as db
import pixiv_crawler as crawler
from pixiv_config import STATUS_WEB_PORT

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
_heartbeat_stop_event: threading.Event | None = None
_heartbeat_thread: threading.Thread | None = None

_last_status_counters: dict = {}
_priority_notice_requests: "dict[int, list[tuple[discord.Interaction, asyncio.AbstractEventLoop, str]]]" = {}
_status_proc: "subprocess.Popen | None" = None
_status_public_url: str = ""

STATUS_JSON = Path("pixivdata/data/status.json")
_STREAMLIT_PORT = STATUS_WEB_PORT

HEARTBEAT_INTERVAL_SEC = 15
HEARTBEAT_LOG_EVERY_SEC = 60


def _write_status_json(running_override: bool | None = None) -> None:
    try:
        s = db.stats()
    except Exception:
        s = {"total": 0, "downloaded": 0, "indexed": 0}

    data: dict = {
        "running": _is_running() if running_override is None else running_override,
        "total": s["total"],
        "downloaded": s["downloaded"],
        "indexed": s["indexed"],
        "priority_queue": crawler.get_priority_queue_size() or None,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if _last_status_counters:
        data["round_downloaded"] = _last_status_counters.get("downloaded", 0)
        data["round_skipped"]    = _last_status_counters.get("skipped", 0)
        data["round_failed"]     = _last_status_counters.get("failed", 0)
        if "round" in _last_status_counters:
            data["round"] = _last_status_counters["round"]

    STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _ensure_status_web_server() -> None:
    global _status_proc, _status_public_url
    if _status_proc is not None and _status_proc.poll() is None:
        return

    _write_status_json()

    app_path = Path(__file__).parent.parent / "pixiv_status_app.py"
    _status_proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(app_path),
         "--server.port", str(_STREAMLIT_PORT),
         "--server.headless", "true",
         "--server.address", "0.0.0.0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(3)  # 等 Streamlit 啟動

    if NGROK_AUTH_TOKEN:
        try:
            from pyngrok import ngrok, conf
            conf.get_default().auth_token = NGROK_AUTH_TOKEN
            tunnel = await asyncio.to_thread(ngrok.connect, _STREAMLIT_PORT, "http")
            _status_public_url = tunnel.public_url
            logger.info(f"ngrok 公開網址：{_status_public_url}")
        except Exception as e:
            logger.warning(f"ngrok 啟動失敗：{e}")

    if not _status_public_url:
        _status_public_url = f"http://localhost:{_STREAMLIT_PORT}/"


def _build_status_text() -> str:
    try:
        s = db.stats()
    except Exception:
        s = {"total": 0, "downloaded": 0, "indexed": 0, "gallery_pages": 0}

    status_str = "執行中" if _is_running() else "已停止"
    lines = [
        f"**Pixiv 爬取狀態：{status_str}**",
        f"作品總數：{s['total']}",
        f"已下載：{s['downloaded']}",
        f"已建立索引：{s['indexed']}",
    ]
    priority_queue_size = crawler.get_priority_queue_size()
    if priority_queue_size:
        lines.append(f"優先作者佇列：{priority_queue_size}")

    if _last_status_counters:
        lines.append(
            "本輪進度："
            f"新增 {_last_status_counters.get('downloaded', 0)} / "
            f"跳過 {_last_status_counters.get('skipped', 0)} / "
            f"失敗 {_last_status_counters.get('failed', 0)}"
        )
        if "round" in _last_status_counters:
            lines.append(f"輪次：{_last_status_counters['round']}")

    return "\n".join(lines)


def _dispatch_status_update(counters: dict) -> None:
    global _last_status_counters
    if counters:
        _last_status_counters = dict(counters)
        _write_status_json()


def _heartbeat_loop(stop_event: threading.Event) -> None:
    last_log_at = 0.0
    while not stop_event.is_set():
        running = _is_running()
        _write_status_json(running_override=running)
        now = time.monotonic()
        if running and now - last_log_at >= HEARTBEAT_LOG_EVERY_SEC:
            logger.info('[heartbeat] Pixiv crawler thread alive')
            last_log_at = now
        for _ in range(HEARTBEAT_INTERVAL_SEC):
            if stop_event.is_set():
                break
            time.sleep(1)


def _start_heartbeat() -> None:
    global _heartbeat_stop_event, _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop_event = threading.Event()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(_heartbeat_stop_event,),
        daemon=True,
        name='pixiv-heartbeat',
    )
    _heartbeat_thread.start()


def _stop_heartbeat() -> None:
    global _heartbeat_stop_event, _heartbeat_thread
    if _heartbeat_stop_event is not None:
        _heartbeat_stop_event.set()
    _heartbeat_thread = None



def _register_priority_notice(
    user_id: int,
    interaction: discord.Interaction,
    loop: asyncio.AbstractEventLoop,
    author_label: str,
) -> None:
    requests = _priority_notice_requests.setdefault(user_id, [])
    requests.append((interaction, loop, author_label))


def _clear_priority_notices() -> None:
    _priority_notice_requests.clear()


async def _send_priority_done_notice(
    interaction: discord.Interaction,
    author_label: str,
    event: dict,
) -> None:
    status = event.get("status", "completed")
    if status == "error":
        text = f"作者 {author_label}（{event['user_id']}）的優先爬取失敗，請稍後再試。"
    elif status == "stopped":
        text = f"作者 {author_label}（{event['user_id']}）的優先爬取因停止指令而中斷。"
    else:
        text = (
            f"作者 {author_label}（{event['user_id']}）已爬取完成。\n"
            f"總作品：{event.get('total', 0)}\n"
            f"新增：{event.get('downloaded', 0)}\n"
            f"跳過：{event.get('skipped', 0)}\n"
            f"失敗：{event.get('failed', 0)}"
        )

    try:
        await interaction.followup.send(text, ephemeral=True)
        return
    except Exception as e:
        logger.warning(f"優先作者 ephemeral 通知失敗 {event.get('user_id')}: {e}")


def _dispatch_priority_done(event: dict) -> None:
    user_id = int(event["user_id"])
    requests = _priority_notice_requests.pop(user_id, [])
    if not requests:
        logger.warning(f"優先作者完成但無待通知請求 user_id={user_id}")
        return
    for interaction, loop, author_label in requests:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _send_priority_done_notice(interaction, author_label, event),
                loop,
            )
            def _log_future_result(f):
                try:
                    f.result()
                except Exception as ex:
                    logger.warning(f"優先作者通知執行失敗 {user_id}: {ex}")
            fut.add_done_callback(_log_future_result)
        except Exception as e:
            logger.warning(f"派送優先作者完成通知失敗 {user_id}: {e}")



# ──────────────────────────────────────────────
# 執行緒目標函式
# ──────────────────────────────────────────────

def _is_running() -> bool:
    return _crawl_thread is not None and _crawl_thread.is_alive()


def _run_full(stop_event: threading.Event):
    """爬蟲執行緒主體，含自動重啟邏輯。"""
    global _crawl_thread
    try:
        while not stop_event.is_set():
            _write_status_json(running_override=True)
            logger.info('啟動 Pixiv 全站爬取')
            try:
                crawler.run_full_crawl(stop_event)
            except Exception as e:
                logger.error(f"全站爬取異常: {e}")
            if stop_event.is_set():
                break
            logger.info("自動重啟：等待 90 分鐘後重試")
            for _ in range(90 * 60):
                if stop_event.is_set():
                    break
                time.sleep(1)
            if stop_event.is_set():
                break
            logger.info("爬蟲已自動重啟")
    finally:
        _crawl_thread = None
        _write_status_json(running_override=False)
        _stop_heartbeat()

# ──────────────────────────────────────────────
# 指令註冊
# ──────────────────────────────────────────────

def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name="pixiv爬蟲", description="Pixiv 爬蟲")
    @app_commands.describe(author_id="作者 user ID（數字）")
    async def pixiv_start(interaction: discord.Interaction, author_id: str = ""):
        global _stop_event, _crawl_thread
        loop = asyncio.get_event_loop()
        crawler.set_priority_user_done_hook(_dispatch_priority_done)

        if author_id.strip():
            raw = author_id.strip()
            if not raw.isdigit():
                await interaction.response.send_message("ID 必須是數字", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            uid = int(raw)
            try:
                uname = await loop.run_in_executor(None, crawler.get_user_name, uid)
                logger.info(f"作者 {uid} 的名稱：{uname}")
            except Exception as e:
                logger.warning(f"查詢作者 {uid} 名稱失敗: {e}")
                uname = str(uid)

            author_label = uname if uname != str(uid) else str(uid)
            queued = await loop.run_in_executor(None, crawler.enqueue_priority_user, uid)
            started_now = False
            if not _is_running():
                if not queued:
                    crawler.clear_priority_queue()
                    queued = await loop.run_in_executor(None, crawler.enqueue_priority_user, uid)
                _clear_priority_notices()
                _stop_event = threading.Event()
                crawler.set_progress_hook(_dispatch_status_update, interval=5)
                crawler.set_priority_user_done_hook(_dispatch_priority_done)
                _crawl_thread = threading.Thread(
                    target=_run_full,
                    args=(_stop_event,),
                    daemon=True,
                    name="pixiv-crawler",
                )
                _crawl_thread.start()
                _start_heartbeat()
                try:
                    await _ensure_status_web_server()
                except Exception as e:
                    logger.warning(f"啟動 Pixiv 狀態網站失敗: {e}")
                started_now = True
            if queued:
                message = f"已將作者 {author_label}（{uid}）加入爬取佇列"
            else:
                message = f"作者 {author_label}（{uid}）已在優先佇列中"
            if started_now:
                message = "Pixiv全站爬取已啟動，" + message
            _register_priority_notice(uid, interaction, loop, author_label)
            await interaction.followup.send(message, ephemeral=True)

        else:
            if _is_running():
                await interaction.response.send_message("爬取已在執行中，請先使用 /pixiv停止", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            crawler.clear_priority_queue()
            _clear_priority_notices()
            _stop_event = threading.Event()
            crawler.set_progress_hook(_dispatch_status_update, interval=5)
            crawler.set_priority_user_done_hook(_dispatch_priority_done)
            _crawl_thread = threading.Thread(
                target=_run_full,
                args=(_stop_event,),
                daemon=True,
                name="pixiv-crawler",
            )
            _crawl_thread.start()
            _start_heartbeat()
            try:
                await _ensure_status_web_server()
            except Exception as e:
                logger.warning(f"啟動 Pixiv 狀態網站失敗: {e}")
            await interaction.followup.send(
                "Pixiv全站爬取已啟動，將持續運行直到手動停止", ephemeral=True
            )

    @tree.command(name="pixiv停止", description="停止 Pixiv 背景爬取")
    async def pixiv_stop(interaction: discord.Interaction):
        global _crawl_thread
        if not _is_running():
            await interaction.response.send_message("目前沒有正在執行的 Pixiv 爬蟲", ephemeral=True)
            return

        _stop_event.set()
        crawler.set_progress_hook(None)
        crawler.set_priority_user_done_hook(None)
        crawler.clear_priority_queue()
        _clear_priority_notices()
        _last_status_counters.clear()
        _write_status_json(running_override=False)
        _stop_heartbeat()
        await interaction.response.send_message(
            "已送出停止請求，Pixiv 爬蟲會在目前工作收尾後停止", ephemeral=True
        )

        def _cleanup():
            global _crawl_thread
            if _crawl_thread:
                _crawl_thread.join(timeout=10)
            if _crawl_thread and not _crawl_thread.is_alive():
                _crawl_thread = None
            _write_status_json(running_override=False)

        threading.Thread(target=_cleanup, daemon=True).start()

    @tree.command(name="pixiv狀態", description="查看 Pixiv 爬取狀態與統計")
    async def pixiv_status(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _ensure_status_web_server()
        text = _build_status_text() + f"\n\n🌐 即時狀態頁：{_status_public_url}"
        await interaction.followup.send(text, ephemeral=True)
