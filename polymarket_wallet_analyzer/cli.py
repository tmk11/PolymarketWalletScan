from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .analyzer import analyze_wallet
from .polymarket_api import PolymarketAPIError, PolymarketClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a Polymarket wallet.")
    parser.add_argument("wallet", help="EVM wallet/proxy wallet address, e.g. 0x...")
    parser.add_argument("--max-records", type=int, default=5000, help="Max records per Data API endpoint")
    parser.add_argument("--csv", type=Path, help="Optional path to export market rows as CSV")
    args = parser.parse_args()

    client = PolymarketClient()
    try:
        wallet_data = client.fetch_wallet_data(args.wallet, max_records=args.max_records)
    except ValueError as exc:
        print(f"Địa chỉ ví không hợp lệ: {exc}", file=sys.stderr)
        return 1
    except PolymarketAPIError as exc:
        print(f"Lỗi gọi Polymarket API: {exc}", file=sys.stderr)
        return 1

    report = analyze_wallet(wallet_data, max_records=args.max_records)
    summary = report["summary"]
    skill = report["skill"]

    print(f"Wallet: {summary['wallet']}")
    print(f"Markets: {summary['market_count']}")
    print(f"Total cost: ${summary['total_cost']:,.2f}")
    print(f"Total PnL: ${summary['total_pnl']:,.2f}")
    print(f"Total ROI: {_fmt_pct(summary['total_roi'])}")
    print(f"Win rate: {_fmt_pct(summary['market_win_rate'])}")
    print(f"Median ROI: {_fmt_pct(summary['median_roi'])}")
    print(f"Top market: {summary['top_market_title']} (${summary['top_market_pnl']:,.2f})")

    print()
    score = skill["skill_score"]
    print(f"Skill score: {score if score is not None else 'N/A'}/100  [{skill['verdict_label']}]")
    print(f"Confidence: {skill['confidence']}" + ("  (DỮ LIỆU BỊ CẮT)" if skill["data_truncated"] else ""))
    print(f"-> {skill['verdict_detail']}")
    print("Breakdown:")
    for component in skill["components"]:
        score_txt = "N/A" if component["normalized"] is None else f"{component['normalized'] * 100:5.0f}%"
        contribution = component["contribution"]
        contribution_txt = "  -  " if contribution is None else f"+{contribution:.1f}"
        print(f"  - {component['label']:<34} {score_txt}  (đóng góp {contribution_txt} điểm) | {component['detail']}")

    print()
    print("Legacy rules:")
    print(f"  One-hit wonder: {summary['is_one_hit_wonder']}")
    print(f"  Probably skilled: {summary['is_probably_skilled']}")

    if args.csv:
        export_csv(report["markets"], args.csv)
        print(f"Exported CSV: {args.csv}")

    return 0


def export_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:,.2f}%"


if __name__ == "__main__":
    sys.exit(main())
