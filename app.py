from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from polymarket_wallet_analyzer.analyzer import analyze_wallet
from polymarket_wallet_analyzer.polymarket_api import PolymarketAPIError, PolymarketClient, validate_wallet


st.set_page_config(page_title="Polymarket Wallet Analyzer", page_icon="📈", layout="wide")


def money(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:,.2f}%"


def fetch_and_analyze(wallet: str, max_records: int) -> dict:
    client = PolymarketClient()
    wallet_data = client.fetch_wallet_data(wallet, max_records=max_records)
    return analyze_wallet(wallet_data)


st.title("📈 Polymarket Wallet Analyzer")
st.caption("Phân tích PnL, ROI, độ tập trung lợi nhuận và dấu hiệu one-hit wonder cho một ví Polymarket.")

with st.sidebar:
    st.header("Thiết lập")
    wallet = st.text_input("Wallet", placeholder="0x...")
    max_records = st.slider(
        "Số record tối đa mỗi endpoint",
        min_value=500,
        max_value=10000,
        value=5000,
        step=500,
        help="Tăng giá trị này nếu ví có lịch sử giao dịch lớn. API /trades giới hạn offset nên app ưu tiên dữ liệu mới nhất API trả về.",
    )
    analyze = st.button("Phân tích ví", type="primary", use_container_width=True)

if not analyze:
    st.info("Nhập địa chỉ ví EVM rồi bấm **Phân tích ví** để bắt đầu.")
    st.stop()

try:
    wallet = validate_wallet(wallet)
except ValueError as exc:
    st.error(str(exc))
    st.stop()

try:
    with st.spinner("Đang fetch dữ liệu từ Polymarket Data API..."):
        report = fetch_and_analyze(wallet, max_records=max_records)
except PolymarketAPIError as exc:
    st.error(str(exc))
    st.stop()

summary = report["summary"]
markets = report["markets"]
df = pd.DataFrame(markets)

st.subheader("Tổng quan")
metric_cols = st.columns(5)
metric_cols[0].metric("Tổng PnL", money(summary["total_pnl"]))
metric_cols[1].metric("Tổng ROI", pct(summary["total_roi"]))
metric_cols[2].metric("Win rate", pct(summary["market_win_rate"]))
metric_cols[3].metric("Số market", f"{summary['market_count']:,}")
metric_cols[4].metric("Median ROI", pct(summary["median_roi"]))

metric_cols = st.columns(5)
metric_cols[0].metric("Cost basis", money(summary["total_cost"]))
metric_cols[1].metric("Current value", money(summary["total_current_value"]))
metric_cols[2].metric("Realized PnL", money(summary["total_realized_pnl"]))
metric_cols[3].metric("Unrealized PnL", money(summary["total_unrealized_pnl"]))
metric_cols[4].metric("Traded API", money(summary["traded_api"]))

st.subheader("Đánh giá nhanh")
if summary["is_probably_skilled"]:
    st.success("Ví này có dấu hiệu **có kỹ năng/edge ổn định** theo bộ rule hiện tại.")
elif summary["is_one_hit_wonder"]:
    st.warning("Ví này có dấu hiệu **one-hit wonder**: lợi nhuận phụ thuộc quá nhiều vào một vài market hoặc ROI âm khi bỏ top market.")
else:
    st.info("Chưa đủ bằng chứng để kết luận skilled hoặc one-hit wonder theo bộ rule hiện tại.")

detail_cols = st.columns(4)
detail_cols[0].metric("Top 1 contribution", pct(summary["top1_contribution"]))
detail_cols[1].metric("Top 3 contribution", pct(summary["top3_contribution"]))
detail_cols[2].metric("ROI ex top 1", pct(summary["roi_ex_top1"]))
detail_cols[3].metric("ROI ex top 3", pct(summary["roi_ex_top3"]))

if df.empty:
    st.warning("Không tìm thấy market nào cho ví này trong dữ liệu API trả về.")
    st.stop()

display_df = df.copy()
display_df["roi_pct"] = display_df["roi"].map(lambda value: value * 100 if value is not None else None)

chart_cols = st.columns(2)
top_pnl = display_df.sort_values("pnl", ascending=False).head(15)
chart_cols[0].plotly_chart(
    px.bar(
        top_pnl,
        x="pnl",
        y="title",
        color="category",
        orientation="h",
        title="Top market theo PnL",
        labels={"pnl": "PnL ($)", "title": "Market"},
    ).update_layout(yaxis={"categoryorder": "total ascending"}),
    use_container_width=True,
)

category_df = display_df.groupby("category", as_index=False).agg(pnl=("pnl", "sum"), cost=("cost", "sum"), markets=("market_id", "count"))
chart_cols[1].plotly_chart(
    px.bar(
        category_df.sort_values("pnl", ascending=False),
        x="category",
        y="pnl",
        color="category",
        title="PnL theo category",
        labels={"pnl": "PnL ($)", "category": "Category"},
    ),
    use_container_width=True,
)

st.plotly_chart(
    px.histogram(
        display_df.dropna(subset=["roi_pct"]),
        x="roi_pct",
        nbins=50,
        title="Phân phối ROI theo market",
        labels={"roi_pct": "ROI (%)"},
    ),
    use_container_width=True,
)

st.subheader("Bảng market")
columns = [
    "title",
    "category",
    "outcomes",
    "cost",
    "proceeds",
    "current_value",
    "realized_pnl",
    "unrealized_pnl",
    "pnl",
    "roi",
    "trade_count",
    "open_positions",
    "closed_positions",
    "market_id",
]
st.dataframe(
    df[columns],
    use_container_width=True,
    column_config={
        "cost": st.column_config.NumberColumn("Cost", format="$%.2f"),
        "proceeds": st.column_config.NumberColumn("Proceeds", format="$%.2f"),
        "current_value": st.column_config.NumberColumn("Current", format="$%.2f"),
        "realized_pnl": st.column_config.NumberColumn("Realized", format="$%.2f"),
        "unrealized_pnl": st.column_config.NumberColumn("Unrealized", format="$%.2f"),
        "pnl": st.column_config.NumberColumn("PnL", format="$%.2f"),
        "roi": st.column_config.NumberColumn("ROI", format="%.2%"),
    },
)

csv = df.to_csv(index=False).encode("utf-8")
st.download_button("Tải CSV", data=csv, file_name=f"polymarket_wallet_{wallet}.csv", mime="text/csv")

with st.expander("Dữ liệu fetch được"):
    st.json(report["raw_counts"])
    st.caption("Nguồn: Polymarket Data API `/trades`, `/positions`, `/closed-positions`, `/activity`, `/value`, `/traded`.")
