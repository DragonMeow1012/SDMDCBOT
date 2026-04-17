"""
文字後處理工具。
從 gemini_worker.py 提取的純文字轉換函式，無外部依賴。
"""
import re


def filter_ghost_stores(text: str) -> str:
    """移除推薦清單中地址模糊或資訊不確定的幽靈店家區塊。"""
    _VAGUE_ADDR = ('附近', '周邊', '一帶', '地區', '不確定')
    _VAGUE_TIME = ('不確定', '請確認', '請洽', '待確認', '未知')

    pattern = re.compile(
        r'\[[^\]]+\]\n地址：[^\n]+\ngoogle地圖：[^\n]+\n時間：[^\n]+\n特色：[^\n]+'
    )

    def keep(m: re.Match) -> str:
        block = m.group(0)
        am = re.search(r'地址：([^\n]+)', block)
        tm = re.search(r'時間：([^\n]+)', block)
        addr = am.group(1).strip() if am else ''
        time_val = tm.group(1).strip() if tm else ''
        if any(k in addr for k in _VAGUE_ADDR) or any(k in time_val for k in _VAGUE_TIME):
            return ''
        return block

    result = pattern.sub(keep, text)
    return re.sub(r'\n{3,}', '\n\n', result).strip()


def suppress_url_embeds(text: str) -> str:
    """將回應中所有裸網址包上 <> 以抑制 Discord 嵌入式預覽。"""
    protected: dict[str, str] = {}
    idx = [0]

    def protect(m: re.Match) -> str:
        key = f'\x00{idx[0]}\x00'
        protected[key] = m.group(0)
        idx[0] += 1
        return key

    text = re.sub(r'<https?://[^>]+>', protect, text)
    text = re.sub(r'\*\*https?://\S+?\*\*', protect, text)
    text = re.sub(r'https?://\S+', lambda m: f'<{m.group(0)}>', text)
    for k, v in protected.items():
        text = text.replace(k, v)
    return text


def strip_thinking_output(text: str) -> str:
    """
    過濾 LM Studio 思考模型的推理過程，只保留最終回應。
    支援：
    1. <think>...</think> 標籤（QwQ / DeepSeek-R1 等）
    2. 結構化思考節（Drafting thoughts:、Refining:、Final Polish: 等）
    """
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()

    final_polish = re.search(
        r'Final\s+Polish\s*:.*\n+([\s\S]+)$',
        cleaned, re.IGNORECASE,
    )
    if final_polish:
        candidate = final_polish.group(1).strip()
        if candidate:
            print(f"[LMSTUDIO] 偵測到思考模型輸出，已擷取 Final Polish 後的內容（{len(candidate)} 字）")
            return candidate

    thinking_markers = ('Drafting thoughts', 'Refining:', 'Final answer:', 'Final response:')
    if any(m in cleaned for m in thinking_markers):
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', cleaned) if p.strip()]
        if len(paragraphs) > 1:
            last = paragraphs[-1]
            print(f"[LMSTUDIO] 偵測到思考模型輸出，已擷取最後段落（{len(last)} 字）")
            return last

    return cleaned


def postprocess_response(text: str, is_lmstudio: bool = False) -> str:
    """統一的回應後處理鏈。"""
    if is_lmstudio:
        text = strip_thinking_output(text)
    text = filter_ghost_stores(text)
    text = suppress_url_embeds(text)
    text = re.sub(r'@(\d{15,20})', r'<@\1>', text)
    return text
