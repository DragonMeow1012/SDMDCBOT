"""
manga-image-translator API server 子進程管理。

bot 啟動時 spawn server、關閉時 terminate；跟 commands/pixiv.py 啟 streamlit
是同一套路（subprocess.Popen + CREATE_NEW_PROCESS_GROUP 隔離 console group）。

啟動條件：
  - config.MANGA_TRANSLATOR_AUTOSTART = True
  - MANGA_TRANSLATOR_DIR 存在且裡面有 server/main.py
  - port 沒被占用（被占就假設已有人在跑，跳過）

server 端讀 GOOGLE_API_KEY 做 Gemini 翻譯，這個環境變數從 bot 進程繼承過去。

注意：upstream 的 server/instance.py 在內部呼叫 worker 時沒帶 X-Nonce header，
但 worker 端 (manga_translator/mode/share.py) 預設會檢查 nonce → 401。
share.py:55 有特殊處理：當 --nonce 字面值為 "None" 時會停用檢查。所以這裡固定
傳 --nonce None 繞過 upstream bug。

worker 是 server 再 spawn 出來的孫進程（port+1），terminate 時要殺整個 process tree
不然 8002 會卡住下次起不來。Windows 用 taskkill /T /F。
"""
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from config import (
    MANGA_TRANSLATOR_AUTOSTART,
    MANGA_TRANSLATOR_BACKEND,
    MANGA_TRANSLATOR_DIR,
    MANGA_TRANSLATOR_OPENAI_API_BASE,
    MANGA_TRANSLATOR_OPENAI_MODEL,
    MANGA_TRANSLATOR_PYTHON,
    MANGA_TRANSLATOR_URL,
    MANGA_TRANSLATOR_USE_GPU,
    MANGA_TRANSLATOR_USE_LOCAL,
)

_proc: subprocess.Popen | None = None
_IS_WINDOWS = os.name == 'nt'
_CREATE_NEW_PGROUP = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0


def _port_from_url(url: str) -> int:
    return urlparse(url).port or 8001


def _port_in_use(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pids_listening_on(port: int) -> list[int]:
    """回傳 LISTENING 在 port 上的 PID 清單。Windows 用 netstat，POSIX 用 lsof。"""
    pids: list[int] = []
    try:
        if _IS_WINDOWS:
            out = subprocess.check_output(
                ['netstat', '-ano', '-p', 'TCP'],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode('utf-8', errors='replace')
            needle = f':{port}'
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == 'TCP' and parts[3] == 'LISTENING':
                    if parts[1].endswith(needle):
                        try:
                            pids.append(int(parts[4]))
                        except ValueError:
                            pass
        else:
            out = subprocess.check_output(
                ['lsof', '-tiTCP:' + str(port), '-sTCP:LISTEN'],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode().strip()
            pids = [int(p) for p in out.splitlines() if p.strip().isdigit()]
    except Exception:
        pass
    return pids


def _kill_pid(pid: int) -> None:
    """taskkill /F /T 整棵 tree（Windows）或 kill -9（POSIX）。"""
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        else:
            subprocess.run(['kill', '-9', str(pid)], check=False, timeout=5)
    except Exception as e:
        print(f'[MANGA] kill PID {pid} 失敗: {e}')


def _kill_tree(proc: subprocess.Popen) -> None:
    """殺掉 proc 跟它所有子孫進程。"""
    _kill_pid(proc.pid)


def start() -> None:
    """非阻塞 spawn server 子進程；偵測不到必要檔案時 silently skip。"""
    global _proc
    if not MANGA_TRANSLATOR_AUTOSTART:
        return
    if _proc is not None and _proc.poll() is None:
        return  # 已經在跑

    port = _port_from_url(MANGA_TRANSLATOR_URL)

    # 殘留清理：上次 bot 被強殺時 stop() 沒跑到，孫進程 worker 會卡在 port+1，
    # 害這次 server 起來後 bind 8002 失敗 → 整個 pipeline 死亡。
    # 既然這支腳本獨佔 8001/8002，看到殘留就直接清掉。
    for p in (port, port + 1):
        orphans = _pids_listening_on(p)
        if orphans:
            print(f'[MANGA] 清除 port {p} 殘留進程 PIDs={orphans}')
            for pid in orphans:
                _kill_pid(pid)

    # 殺完再確認一次（kill 是 async 的，給 OS 一點時間釋放 socket）
    import time as _t
    for _ in range(10):
        if not _port_in_use(port) and not _port_in_use(port + 1):
            break
        _t.sleep(0.2)

    if _port_in_use(port):
        print(f'[MANGA] port {port} 仍占用清不掉，跳過 autostart（手動處理）')
        return

    repo = Path(MANGA_TRANSLATOR_DIR)
    server_main = repo / 'server' / 'main.py'
    if not server_main.is_file():
        print(f'[MANGA] {server_main} 不存在，autostart 取消（先 git clone 並裝好相依）')
        return

    py_path = Path(MANGA_TRANSLATOR_PYTHON)
    py = str(py_path) if py_path.is_file() else sys.executable
    if py == sys.executable:
        print('[MANGA] 警告：找不到專屬 venv，將用 bot 的 Python，可能有版本衝突')

    cmd = [
        py, str(server_main),
        '--port', str(port),
        '--host', '127.0.0.1',
        '--start-instance',
        '--nonce', 'None',  # 繞過 upstream nonce mismatch bug，see module docstring
    ]
    if MANGA_TRANSLATOR_USE_GPU:
        cmd.append('--use-gpu')

    log_dir = Path('data') / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / 'manga_translator_server.log'

    # GeminiTranslator 預設用已下架的 'gemini-1.5-flash-002'，強制蓋掉成現役型號
    # （setdefault 不夠保險 — 萬一系統環境繼承到別的舊值會中招）
    child_env = os.environ.copy()
    if MANGA_TRANSLATOR_USE_LOCAL:
        # 把 gemini_2stage translator 的 OpenAI client 改指到本地 LM Studio。
        # 程式碼讀 GEMINI_API_BASE，預設是 Gemini 雲端 endpoint，這邊覆寫成 LM Studio。
        child_env['GEMINI_API_BASE'] = MANGA_TRANSLATOR_OPENAI_API_BASE
        child_env['GEMINI_API_KEY'] = 'lm-studio'  # LM Studio 不檢查 key，placeholder
        child_env['GEMINI_MODEL'] = MANGA_TRANSLATOR_OPENAI_MODEL
        child_env['GEMINI_VISION_MODEL'] = MANGA_TRANSLATOR_OPENAI_MODEL
    else:
        # Gemini 雲端模式：GEMINI_API_KEY/KEY1..N 直接從 bot env 繼承
        if 'GEMINI_API_KEY' not in child_env and 'GOOGLE_API_KEY' in child_env:
            child_env['GEMINI_API_KEY'] = child_env['GOOGLE_API_KEY']
    # Windows 預設 console codepage cp950 寫日文/中文會變亂碼 → log 不可讀。
    # 強制 child Python 走 UTF-8（PYTHONUTF8 是 PEP 540 的 UTF-8 mode 開關）。
    child_env['PYTHONUTF8'] = '1'
    child_env['PYTHONIOENCODING'] = 'utf-8'

    print(f'[MANGA] 啟動 server: {" ".join(cmd)}')
    if MANGA_TRANSLATOR_USE_LOCAL:
        print(f'[MANGA] backend={MANGA_TRANSLATOR_BACKEND} → 本地 LM Studio '
              f'{MANGA_TRANSLATOR_OPENAI_API_BASE} (model={MANGA_TRANSLATOR_OPENAI_MODEL})')
    else:
        print(f'[MANGA] backend={MANGA_TRANSLATOR_BACKEND} → Gemini 雲端')
    print(f'[MANGA] log → {log_path}')
    log_fp = open(log_path, 'ab')
    _proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=child_env,
        creationflags=_CREATE_NEW_PGROUP,
    )


def stop() -> None:
    """關閉 server 子進程跟它的孫進程（worker），wait 5s 後強殺整棵 tree。"""
    global _proc
    if _proc is None:
        return
    if _proc.poll() is None:
        print('[MANGA] terminating server tree...')
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print('[MANGA] terminate 超時，強殺整棵 tree')
            _kill_tree(_proc)
        except Exception as e:
            print(f'[MANGA] stop 失敗: {e}')
            _kill_tree(_proc)
        else:
            # server 自己 terminate 掉但 worker (孫進程) 不會自動死，補殺
            _kill_tree(_proc)
    _proc = None
