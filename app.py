from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from polymarket_wallet_analyzer.analyzer import analyze_wallet
from polymarket_wallet_analyzer.polymarket_api import PolymarketAPIError, PolymarketClient, validate_wallet
from polymarket_wallet_analyzer.token_resolver import TokenResolver

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
    return analyze_wallet(wallet_data, max_records=max_records, token_resolver=TokenResolver())


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
skill = report["skill"]
markets = report["markets"]
df = pd.DataFrame(markets)

if skill.get("data_truncated"):
    st.warning(
        "⚠️ Dữ liệu fetch về đã chạm giới hạn `max_records` ở ít nhất một endpoint, "
        "nên đây có thể chỉ là **một phần** lịch sử của ví. Hãy tăng giá trị slider và "
        "đọc kết luận một cách thận trọng (độ tin cậy thấp)."
    )

st.subheader("Tổng quan")
metric_cols = st.columns(5)
metric_cols[0].metric("Trading PnL", money(summary.get("trading_pnl")))
metric_cols[1].metric("ROI buy notional", pct(summary.get("roi_buy_notional")))
metric_cols[2].metric("Win rate", pct(summary.get("market_win_rate")))
metric_cols[3].metric("Số market", f"{summary.get('market_count', summary.get('total_markets', 0)):,}")
metric_cols[4].metric("Verdict", summary.get("verdict", "inconclusive"))

metric_cols = st.columns(5)
metric_cols[0].metric("ROI cost basis", pct(summary.get("roi_cost_basis")))
metric_cols[1].metric("ROI ex top1", pct(summary.get("roi_ex_top1_buy_notional")))
metric_cols[2].metric("Realized PnL", money(summary.get("total_realized_pnl", summary.get("realized_pnl"))))
metric_cols[3].metric("Unrealized PnL", money(summary.get("total_unrealized_pnl", summary.get("unrealized_pnl"))))
metric_cols[4].metric("Rewards PnL", money(summary.get("rewards_pnl")))

metric_cols = st.columns(5)
metric_cols[0].metric("Top1/net PnL", pct(summary.get("top1_contribution_net_pnl")))
metric_cols[1].metric("Top1/gross profit", pct(summary.get("top1_share_of_gross_profit")))
profit_factor = summary.get("profit_factor")
metric_cols[2].metric(
    "Profit factor",
    "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}" if profit_factor is not None else "N/A",
)
metric_cols[3].metric("Confidence", summary.get("confidence_level", "medium"))
metric_cols[4].metric("Unmapped", f"{summary.get('unmapped_records_count', 0):,}")

st.subheader("🎯 Skilled hay Ăn may?")

verdict_styles = {
    "skilled": st.success,
    "category_skilled": st.success,
    "lucky_or_one_hit_wonder": st.warning,
    "unprofitable": st.error,
    "insufficient_data": st.warning,
    "inconclusive": st.info,
}
confidence_labels = {"high": "Độ tin cậy cao", "medium": "Độ tin cậy trung bình", "low": "Độ tin cậy thấp"}

score = skill.get("skill_score")
raw_score = skill.get("raw_skill_score")
score_adjustment = skill.get("score_adjustment") or {}
verdict_box = verdict_styles.get(skill.get("verdict", "inconclusive"), st.info)

score_col, gauge_col = st.columns([1, 2])
score_label = "Skill score"
if score_adjustment.get("applied"):
    score_label = "Adjusted skill score"
score_col.metric(score_label, f"{score}/100" if score is not None else "N/A")
score_col.caption(confidence_labels.get(skill.get("confidence", "medium"), skill.get("confidence", "medium")))
if score_adjustment.get("applied") and raw_score is not None:
    score_col.caption(
        f"Raw score {raw_score}/100 bị cap ở {score_adjustment.get('cap')}/100 vì verdict/concentration risk."
    )

if score is not None:
    gauge = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score,
            number={"suffix": "/100"},
            gauge={
                "axis": {"range": [0, 100]},
                "bar": {"color": "#2b8a3e"},
                "steps": [
                    {"range": [0, 45], "color": "#ffe3e3"},
                    {"range": [45, 65], "color": "#fff3bf"},
                    {"range": [65, 100], "color": "#d3f9d8"},
                ],
            },
        )
    )
    gauge.update_layout(height=220, margin=dict(l=20, r=20, t=20, b=10))
    gauge_col.plotly_chart(gauge, use_container_width=True)

verdict_box(f"**{skill.get('verdict_label', 'Chưa kết luận')}** — {skill.get('verdict_detail', '')}")
st.caption(summary.get("category_skill_summary", ""))

st.markdown("**Vì sao có điểm này?** (đóng góp của từng tiêu chí vào tổng điểm)")
breakdown_rows = [
    {
        "Tiêu chí": component["label"],
        "Điểm thành phần": None if component["normalized"] is None else component["normalized"] * 100,
        "Trọng số": component["weight"],
        "Đóng góp": component["contribution"],
        "Chi tiết": component["detail"],
    }
    for component in skill.get("components", [])
]
st.dataframe(
    pd.DataFrame(breakdown_rows),
    use_container_width=True,
    hide_index=True,
    column_config={
        "Điểm thành phần": st.column_config.NumberColumn("Điểm thành phần", format="%.0f%%"),
        "Trọng số": st.column_config.NumberColumn("Trọng số", format="%.0f"),
        "Đóng góp": st.column_config.NumberColumn("Đóng góp (điểm)", format="%.1f"),
    },
)

with st.expander("Chỉ số chi tiết skill (edge, thống kê, rủi ro)"):
    edge = skill.get("edge", {})
    significance = skill.get("significance", {})
    risk = skill.get("risk", {})
    breadth = skill.get("breadth", {})
    info_cols = st.columns(4)
    info_cols[0].metric("Edge / share", pct(edge.get("edge_per_share")) if edge.get("edge_per_share") is not None else "N/A")
    info_cols[1].metric(
        "Win rate (đã resolve)",
        pct(edge.get("win_rate")) if edge.get("win_rate") is not None else "N/A",
    )
    info_cols[2].metric(
        "Sharpe (ROI)", f"{risk.get('sharpe'):.2f}" if risk.get("sharpe") is not None else "N/A"
    )
    info_cols[3].metric(
        "Profit factor", f"{risk.get('profit_factor'):.2f}" if risk.get("profit_factor") is not None else "∞"
    )
    info_cols = st.columns(4)
    info_cols[0].metric("Kèo đã resolve", f"{edge.get('n_resolved', 0):,}")
    info_cols[1].metric("Event độc lập", f"{breadth.get('effective_bets', 0):,}")
    if significance.get("ci_low") is not None:
        info_cols[2].metric("CI dưới (ROI)", pct(significance.get("ci_low")))
        info_cols[3].metric("CI trên (ROI)", pct(significance.get("ci_high")))

monthly = skill.get("consistency", {}).get("monthly_pnl", [])
if len(monthly) >= 2:
    monthly_df = pd.DataFrame(monthly)
    monthly_df["cumulative"] = monthly_df["pnl"].cumsum()
    st.markdown("**PnL theo tháng & lũy kế** (kiểm tra lợi nhuận có bền hay dồn vào một đợt)")
    fig = px.bar(monthly_df, x="month", y="pnl", labels={"pnl": "PnL ($)", "month": "Tháng"})
    fig.add_scatter(
        x=monthly_df["month"], y=monthly_df["cumulative"], mode="lines+markers", name="Lũy kế", yaxis="y"
    )
    st.plotly_chart(fig, use_container_width=True)

st.subheader("Chi tiết tập trung lợi nhuận")
detail_cols = st.columns(4)
detail_cols[0].metric("Top1/net PnL", pct(summary.get("top1_contribution_net_pnl")))
detail_cols[1].metric("Top3/net PnL", pct(summary.get("top3_contribution_net_pnl")))
detail_cols[2].metric("ROI ex top1 buy", pct(summary.get("roi_ex_top1_buy_notional")))
detail_cols[3].metric("ROI ex top3 buy", pct(summary.get("roi_ex_top3_buy_notional")))

if report.get("warnings"):
    with st.expander("Cảnh báo dữ liệu"):
        for warning in report["warnings"]:
            st.warning(warning)

if df.empty:
    st.warning("Không tìm thấy market nào cho ví này trong dữ liệu API trả về.")
    st.stop()

display_df = df.copy()
display_df["roi_pct"] = display_df["roi_cost_basis"].map(lambda value: value * 100 if value is not None else None)

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

st.subheader("Phân tích theo category")
category_breakdown_df = pd.DataFrame(report.get("category_breakdown", []))
if not category_breakdown_df.empty:
    st.dataframe(
        category_breakdown_df,
        use_container_width=True,
        column_config={
            "trading_pnl": st.column_config.NumberColumn("Trading PnL", format="$%.2f"),
            "roi_cost_basis": st.column_config.NumberColumn("ROI cost", format="%.2%"),
            "roi_buy_notional": st.column_config.NumberColumn("ROI buy", format="%.2%"),
            "market_win_rate": st.column_config.NumberColumn("Win rate", format="%.2%"),
            "median_market_roi": st.column_config.NumberColumn("Median ROI", format="%.2%"),
            "roi_ex_top1": st.column_config.NumberColumn("ROI ex top1", format="%.2%"),
            "top1_contribution": st.column_config.NumberColumn("Top1/net", format="%.2%"),
        },
    )

st.subheader("Bảng market")
columns = [
    "title",
    "category",
    "outcomes",
    "cost_basis",
    "total_buy_notional",
    "max_capital_at_risk",
    "proceeds",
    "current_value",
    "realized_pnl",
    "unrealized_pnl",
    "rewards_pnl",
    "trading_pnl",
    "roi_pct",
    "trade_count",
    "open_positions",
    "closed_positions",
    "market_id",
]
st.dataframe(
    display_df[columns],
    use_container_width=True,
    column_config={
        "cost_basis": st.column_config.NumberColumn("Cost basis", format="$%.2f"),
        "total_buy_notional": st.column_config.NumberColumn("Buy notional", format="$%.2f"),
        "max_capital_at_risk": st.column_config.NumberColumn("Max capital", format="$%.2f"),
        "proceeds": st.column_config.NumberColumn("Proceeds", format="$%.2f"),
        "current_value": st.column_config.NumberColumn("Current", format="$%.2f"),
        "realized_pnl": st.column_config.NumberColumn("Realized", format="$%.2f"),
        "unrealized_pnl": st.column_config.NumberColumn("Unrealized", format="$%.2f"),
        "rewards_pnl": st.column_config.NumberColumn("Rewards", format="$%.2f"),
        "trading_pnl": st.column_config.NumberColumn("Trading PnL", format="$%.2f"),
        "roi_pct": st.column_config.NumberColumn("ROI", format="%.2f%%"),
    },
)

csv = df.to_csv(index=False).encode("utf-8")
st.download_button("Tải CSV", data=csv, file_name=f"polymarket_wallet_{wallet}.csv", mime="text/csv")

with st.expander("Dữ liệu fetch được"):
    st.json(report["raw_counts"])
    st.caption("Nguồn: Polymarket Data API `/trades`, `/positions`, `/closed-positions`, `/activity`, `/value`, `/traded`.")
