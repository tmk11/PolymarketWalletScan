from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

from .analyzer import analyze_wallet
from .polymarket_api import PolymarketAPIError, PolymarketClient
from .token_resolver import DEFAULT_TOKEN_CACHE_PATH, TokenResolver


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a Polymarket wallet.")
    parser.add_argument("wallet", help="EVM wallet/proxy wallet address, e.g. 0x...")
    parser.add_argument("--max-records", type=int, default=5000, help="Max records per Data API endpoint")
    parser.add_argument("--csv", type=Path, help="Optional path to export market rows as CSV")
    parser.add_argument("--json", type=Path, help="Optional path to export the full report as JSON")
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument("--resolve-tokens", dest="resolve_tokens", action="store_true", default=True, help="Resolve token-only records via CLOB metadata")
    token_group.add_argument("--no-resolve-tokens", dest="resolve_tokens", action="store_false", help="Disable token metadata resolver")
    parser.add_argument("--token-cache-path", type=Path, default=DEFAULT_TOKEN_CACHE_PATH, help="Token resolver cache path")
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

    token_resolver = TokenResolver(cache_path=args.token_cache_path, enabled=args.resolve_tokens) if args.resolve_tokens else None
    report = analyze_wallet(wallet_data, max_records=args.max_records, token_resolver=token_resolver)
    summary = report["summary"]
    skill = report["skill"]

    print(f"Wallet: {summary.get('wallet')}")
    print(f"Markets: {summary.get('market_count', summary.get('total_markets', 0))}")
    print(f"Trading PnL: ${summary.get('trading_pnl', 0.0):,.2f}")
    print(f"Rewards PnL: ${summary.get('rewards_pnl', 0.0):,.2f}")
    print(f"Total incl. rewards: ${summary.get('total_pnl_including_rewards', 0.0):,.2f}")
    print(f"ROI cost basis: {_fmt_pct(summary.get('roi_cost_basis'))}")
    print(f"ROI buy notional: {_fmt_pct(summary.get('roi_buy_notional'))}")
    print(f"ROI max capital at risk: {_fmt_pct(summary.get('roi_max_capital_at_risk'))}")
    print(f"Win rate: {_fmt_pct(summary.get('market_win_rate'))}")
    print(f"Median market ROI: {_fmt_pct(summary.get('median_market_roi'))}")
    print(
        "ROI ex top1/top3/top5 buy: "
        f"{_fmt_pct(summary.get('roi_ex_top1_buy_notional'))} / "
        f"{_fmt_pct(summary.get('roi_ex_top3_buy_notional'))} / "
        f"{_fmt_pct(summary.get('roi_ex_top5_buy_notional'))}"
    )
    print(f"Top1 contribution net PnL: {_fmt_pct(summary.get('top1_contribution_net_pnl'))}")
    print(f"Top1 share of gross profit: {_fmt_pct(summary.get('top1_share_of_gross_profit'))}")
    print(f"Unmapped records: {summary.get('unmapped_records_count', 0)}")
    print(
        "Token resolver: "
        f"{'on' if summary.get('token_resolver_enabled') else 'off'}, "
        f"resolved {summary.get('resolved_from_token_high_confidence_count', 0)}, "
        f"cache hits {summary.get('token_resolver_cache_hits', 0)}, "
        f"API calls {summary.get('token_resolver_api_calls', 0)}, "
        f"failures {summary.get('token_resolver_failures', 0)}"
    )
    print(f"Top market: {summary.get('top_market_title', '')} (${summary.get('top_market_pnl', 0.0):,.2f})")

    print()
    score = skill.get("skill_score")
    raw_score = skill.get("raw_skill_score")
    score_adjustment = skill.get("score_adjustment") or {}
    print(f"Skill score: {score if score is not None else 'N/A'}/100  [{skill.get('verdict_label', 'N/A')}]")
    if score_adjustment.get("applied") and raw_score is not None:
        print(f"Raw score: {raw_score}/100  (capped at {score_adjustment.get('cap')}/100 due to verdict/concentration risk)")
    print(f"Confidence: {skill.get('confidence', 'medium')}" + ("  (DỮ LIỆU BỊ CẮT)" if skill.get("data_truncated") else ""))
    print(f"-> {skill.get('verdict_detail', '')}")
    print("Breakdown:")
    for component in skill.get("components", []):
        score_txt = "N/A" if component["normalized"] is None else f"{component['normalized'] * 100:5.0f}%"
        contribution = component["contribution"]
        contribution_txt = "  -  " if contribution is None else f"+{contribution:.1f}"
        print(f"  - {component['label']:<34} {score_txt}  (đóng góp {contribution_txt} điểm) | {component['detail']}")

    print()
    print("Legacy rules:")
    print(f"  One-hit wonder: {summary.get('is_one_hit_wonder')}")
    print(f"  Top3 dependent: {summary.get('is_top3_dependent')}")
    print(f"  Probably skilled: {summary.get('is_probably_skilled')}")
    print()
    print(f"Category read: {summary.get('category_skill_summary', '')}")
    if report.get("warnings"):
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning}")

    if args.csv:
        export_csv(report["markets"], args.csv)
        print(f"Exported CSV: {args.csv}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(f"Exported JSON: {args.json}")

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


def json_safe(value):
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


if __name__ == "__main__":
    sys.exit(main())
