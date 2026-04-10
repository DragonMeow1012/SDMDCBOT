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
st.title("🎨 Pixiv 爬取狀態")

placeholder = st.empty()

while True:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    with placeholder.container():
        running = data.get("running", False)
        status_label = "🟢 執行中" if running else "🔴 已停止"

        st.metric("狀態", status_label)

        col1, col2, col3 = st.columns(3)
        col1.metric("作品總數", data.get("total", 0))
        col2.metric("已下載", data.get("downloaded", 0))
        col3.metric("已建立索引", data.get("indexed", 0))

        if data.get("priority_queue"):
            st.info(f"優先作者佇列：{data['priority_queue']} 位")

        if data.get("round_downloaded") is not None:
            st.subheader("本次啟動進度")
            c1, c2, c3 = st.columns(3)
            c1.metric("✅ 成功", data.get("round_downloaded", 0))
            c2.metric("⏭ 跳過", data.get("round_skipped", 0))
            c3.metric("❌ 失敗", data.get("round_failed", 0))
            if "round" in data:
                st.caption(f"輪次：{data['round']}")

        st.caption(f"最後更新：{data.get('updated_at', '—')}　每 5 秒自動刷新")

    time.sleep(5)
    st.rerun()
