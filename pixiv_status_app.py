"""
Pixiv 爬取狀態 Streamlit 頁面
讀取 pixivdata/data/status.json，每 5 秒自動刷新。
"""
import json
import time
from pathlib import Path

import streamlit as st

STATUS_FILE = Path("pixivdata/data/status.json")

st.set_page_config(page_title="Pixiv 爬取狀態", page_icon="🎨", layout="centered")
st.title("Pixiv 爬取狀態")

placeholder = st.empty()

while True:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    running = data.get("running", False)
    state = "執行中" if running else "已停止"
    total = data.get("total", 0)
    downloaded = data.get("downloaded", 0)
    indexed = data.get("indexed", 0)
    r_ok = data.get("round_downloaded", 0)
    r_skip = data.get("round_skipped", 0)
    r_fail = data.get("round_failed", 0)
    round_n = data.get("round", 1)
    pq = data.get("priority_queue")
    updated = data.get("updated_at", "—")

    with placeholder.container():
        lines = [
            f"**Pixiv 爬取狀態：{state}**",
            f"作品總數：{total}",
            f"已下載：{downloaded}",
            f"已建立索引：{indexed}",
            f"本輪進度：新增 {r_ok} / 跳過 {r_skip} / 失敗 {r_fail}",
            f"輪次：{round_n}",
        ]
        if pq:
            lines.append(f"優先作者佇列：{pq} 位")
        st.markdown("  \n".join(lines))
        st.caption(f"最後更新：{updated}　每 5 秒自動刷新")

    time.sleep(5)
    st.rerun()
