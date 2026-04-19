"""
Log 初始化模組。
將 stdout / stderr 同時輸出到 console 及 data/logs/bot_YYYY-MM-DD.log。
在 main.py 最早期呼叫 setup_logger() 即可，無需修改其他模組的 print()。
"""
import os
import sys
import datetime


_LOG_DIR = os.path.join('data', 'logs')


class _Tee:
    """console 原樣輸出；log 檔每行前綴 [HH:MM:SS] 時間戳。"""
    def __init__(self, console, log_file):
        self._console = console
        self._log = log_file
        self._buffer = ''

    def write(self, data: str) -> None:
        self._console.write(data)
        self._console.flush()
        self._buffer += data
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            self._log.write(f'[{ts}] {line}\n')
        self._log.flush()

    def flush(self) -> None:
        self._console.flush()
        self._log.flush()

    def reconfigure(self, **kwargs) -> None:
        # 讓 main.py 裡的 reconfigure() 呼叫不報錯
        pass

    @property
    def encoding(self) -> str:
        return 'utf-8'


def setup_logger() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)

    date_str  = datetime.date.today().strftime('%Y-%m-%d')
    log_path  = os.path.join(_LOG_DIR, f'bot_{date_str}.log')
    log_file  = open(log_path, 'a', encoding='utf-8', buffering=1)

    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)

    print(f'[LOG] 日誌輸出至 {log_path}')
