"""
共享可變狀態模組。
各 commands 子模組與事件處理器皆從此處 import 全域狀態。
"""

# Discord chat session 字典
# { channel_id (int) -> {
#     'chat_obj': Chat | None,
#     'personality': str | None,
#     'raw_history': list,
#     'current_web_context': str | None,
# }}
chat_sessions: dict = {}

# Worker 啟動旗標（防止重連時重複建立）
_worker_started: bool = False
