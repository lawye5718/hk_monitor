import os

import pandas as pd
import sqlite3
import streamlit as st
from dotenv import load_dotenv

from price import get_market_view

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False

# 页面基本配置
st.set_page_config(
    page_title="港股 DI 权益披露监控看板",
    page_icon="📈",
    layout="wide",
)

DB_PATH = "hk_monitor.db"
load_dotenv()

# 港股代码 -> 中文名称映射（与 .env 股票池对应）
STOCK_NAMES = {
    "00700": "腾讯控股", "03690": "美团-W", "01810": "小米集团-W", "00981": "中芯国际",
    "00883": "中国海洋石油", "01339": "中国人民保险集团", "01288": "农业银行",
    "00992": "联想集团", "02600": "中国铝业", "00728": "中国电信", "00857": "中国石油股份",
    "00941": "中国移动", "02318": "中国平安", "01088": "中国神华",
    "00386": "中国石油化工股份", "02899": "紫金矿业", "00390": "中国中铁",
    "01816": "中广核电力", "01171": "兖矿能源", "09988": "阿里巴巴-W",
    "00576": "浙江沪杭甬",
}


def stock_display(code: str) -> str:
    """显示格式: '00700 腾讯控股'，未映射则仅显示代码"""
    name = STOCK_NAMES.get(code, "")
    return f"{code} {name}".strip()


def get_stocks_from_env():
    stocks = os.getenv("STOCK_POOL", "")
    return [s.strip().zfill(5) for s in stocks.split(",") if s.strip()]


@st.cache_data(ttl=30, show_spinner=False)
def cached_market_view(code: str) -> dict:
    return get_market_view(code)


def load_data(stock_code: str):
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT event_date AS '事件日期',
               shareholder_name AS '大股东/董事',
               reason AS '披露原因',
               shares_change AS '本次买卖股数',
               holding_shares AS '变动后持股数',
               holding_percent AS '变动后持股比例',
               event_open AS '当日开盘',
               event_high AS '当日最高',
               event_low AS '当日最低',
               event_close AS '当日收盘',
               date_fetched AS '系统抓取日期'
        FROM di_disclosure
        WHERE stock_code = ?
        ORDER BY date_fetched DESC, event_date DESC
    """
    df = pd.read_sql_query(query, conn, params=(stock_code,))
    conn.close()
    return df


def render_quote_bar(stock_code: str):
    """顶部实时行情栏：盘中=开盘+最新；已收盘=开盘+收盘；未开市=前一交易日OHLC"""
    view = cached_market_view(stock_code)
    name = view.get("name") or STOCK_NAMES.get(stock_code, "")
    bars = view.get("bars")
    status = view.get("status", "error")

    if status == "error" or bars is None:
        st.warning(f"⚠️ {stock_code} {name} 实时行情暂不可用（{view.get('status_text', '')}），"
                   f"下方披露记录中的变动日行情为历史数据不受影响。")
        return

    if status == "trading":
        latest_label = "最新价"
        latest_value = bars["latest"]
        close_value = None
    else:
        latest_label = "收盘价"
        latest_value = bars["close"]
        close_value = bars["close"]

    st.markdown(
        f"<span style='background:#1f77b4;color:white;padding:2px 12px;border-radius:12px;font-size:0.9rem'>"
        f"{view['status_text']}</span>　**{name} ({stock_code})**　HK$",
        unsafe_allow_html=True,
    )
    cols = st.columns(4)
    cols[0].metric("开盘价", f"{bars['open']:.3f}" if bars["open"] is not None else "—")
    cols[1].metric(latest_label, f"{latest_value:.3f}" if latest_value is not None else "—")
    cols[2].metric("最高价", f"{bars['high']:.3f}" if bars["high"] is not None else "—")
    cols[3].metric("最低价", f"{bars['low']:.3f}" if bars["low"] is not None else "—")
    st.caption(f"行情源：东方财富（港股 HKD）｜数据约 30 秒内自动刷新")


# ----------------- 界面布局 -----------------

st.title("📊 港股指定上市公司 DI (权益披露) 监控看板")

# 侧边栏：股票切换
stock_list = get_stocks_from_env()

if not stock_list:
    st.error("⚠️ 未在 .env 文件中配置 STOCK_POOL 变量！")
    st.stop()

selected_stock = st.sidebar.selectbox("🎯 选择要查看的股票：", stock_list, format_func=stock_display)

# 侧边栏：自动刷新选项
auto_refresh = st.sidebar.checkbox("⏱ 每 60 秒自动刷新行情", value=True)
st.sidebar.markdown("---")
st.sidebar.info("💡 系统提示：\n- 环境变量中新增股票时，后台将自动追溯过去 6 个月的历史披露。\n"
                "- 每个变动日期会标注当日开盘/最高/最低/收盘价。")

# 自动刷新（若组件已安装）
if auto_refresh and HAS_AUTOREFRESH:
    st_autorefresh(interval=60_000, key="di_auto_refresh")

st.header(f"📈 {stock_display(selected_stock)} 数据看板")

# 顶部实时行情栏
render_quote_bar(selected_stock)
st.markdown("---")

# 加载选中股票的数据
df = load_data(selected_stock)

if df.empty:
    st.warning(f"暂无股票 {selected_stock} 的披露记录。请先运行 `python data_fetcher.py` 抓取数据。")
else:
    # ----------------- 1. 最新 DI 重点提示（Header Highlights） -----------------
    st.subheader("🔥 最新 DI 信息变动（重点提示）")

    # 提取最新的前 3 条记录作为重点提示
    recent_records = df.head(3)

    cols = st.columns(len(recent_records))
    for idx, (_, row) in enumerate(recent_records.iterrows()):
        with cols[idx]:
            reason_text = row["披露原因"] or "—"
            st.warning(f"**{row['大股东/董事']}**")
            st.markdown(f"📅 事件日期：**{row['事件日期']}**")
            st.markdown(f"📋 披露原因：{reason_text}")
            st.markdown(f"🔁 本次买卖股数：**{row['本次买卖股数'] or '—'}**")
            st.metric(
                label=f"变动后持股: {row['变动后持股数']}",
                value=f"{row['变动后持股比例']}",
            )
            ohlc = (f"开 {row['当日开盘'] or '—'} / 高 {row['当日最高'] or '—'} / "
                    f"低 {row['当日最低'] or '—'} / 收 {row['当日收盘'] or '—'}")
            st.caption(f"💹 变动日行情: {ohlc}")

    st.markdown("---")

    # ----------------- 2. 历史数据表格与筛选器 -----------------
    st.subheader("📜 历史权益披露记录")

    # 搜索框筛选股东名称
    search_term = st.text_input("🔍 搜索大股东/董事姓名：", "")
    if search_term:
        filtered_df = df[df["大股东/董事"].str.contains(search_term, na=False)]
    else:
        filtered_df = df

    # 展示可排序交互表格
    st.dataframe(filtered_df, use_container_width=True, height=480)

    # 下载 CSV 按钮
    csv = filtered_df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label="📥 导出当前筛选数据为 CSV",
        data=csv,
        file_name=f"{selected_stock}_DI_Records.csv",
        mime="text/csv",
    )
