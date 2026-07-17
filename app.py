"""
DS_DCF — 多情景估值与七类型量化诊断
"""

import os

import streamlit as st

from desktop.version import __version__

st.set_page_config(
    page_title="DS_DCF · 估值与七类型诊断",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.markdown("## ⚙️ 控制")
st.sidebar.caption(f"DS_DCF v{__version__}")
if st.sidebar.button("🔄 刷新数据", type="primary", use_container_width=True):
    # 只设置刷新请求；新快照与分析都成功前，最后一份结果不会被删除。
    st.session_state["force_data_refresh"] = True
    st.session_state["page_radio"] = "🎯 七种买入类型"
    st.rerun()

st.sidebar.caption("刷新会保留上一份完整结果；只有数据、分析质量闸门和快照晋级全部成功后才替换。")
st.sidebar.caption("结果是量化筛选与诊断，不构成投资建议。")
if os.environ.get("DS_DCF_DESKTOP") == "1":
    st.sidebar.caption("版本更新与退出请使用 DS_DCF 桌面控制窗口。")
else:
    st.sidebar.caption("停止程序请在启动终端按 Ctrl+C。")

page = st.sidebar.radio("页面", ["🎯 七种买入类型", "🏆 行业买入候选 · 个股查询"], key="page_radio")

if page == "🎯 七种买入类型":
    from ui.buy_types_page import show

    show()
else:
    from ui.leaders_page import show

    show()
