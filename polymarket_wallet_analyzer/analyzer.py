from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

from .polymarket_api import WalletData

MarketBucket = dict[str, list[dict[str, Any]]]

BUCKET_SOURCES = ("trades", "positions", "closed_positions", "activity")
REWARD_ACTIVITY_TYPES = {"REWARD", "MAKER_REBATE", "REFERRAL_REWARD"}
NON_TRADING_ACTIVITY_TYPES = {"MERGE", "SPLIT", "CONVERSION"}

DEFAULT_SKILL_CONFIG: dict[str, float] = {
    "one_hit_top1_contribution": 0.50,
    "top3_dependency_contribution": 0.70,
    "skilled_roi_buy_notional": 0.05,
    "skilled_min_markets": 30,
    "skilled_market_win_rate": 0.50,
    "skilled_median_roi_floor": -0.02,
    "skilled_top1_contribution_max": 0.40,
    "category_skilled_min_markets": 10,
    "max_unmapped_ratio_medium": 0.05,
    "max_other_category_ratio_medium": 0.50,
    "max_unrealized_pnl_ratio_medium": 0.50,
}

CATEGORY_ORDER = [
    "Weather",
    "Politics",
    "Sports",
    "Crypto",
    "Economy/Fed",
    "Tech/AI",
    "Geopolitics",
    "Entertainment",
    "Other",
]


@dataclass
class GroupedRecords:
    markets: dict[str, MarketBucket]
    unmapped_records: list[dict[str, Any]]


@dataclass
class LedgerMetrics:
    realized_pnl: float = 0.0
    sell_proceeds: float = 0.0
    redeem_proceeds: float = 0.0
    total_buy_notional: float = 0.0
    max_capital_at_risk: float = 0.0
    ending_inventory_cost: float = 0.0
    has_buy_data: bool = False
    has_realized_data: bool = False
    incomplete: bool = False


def analyze_wallet(
    wallet_data: WalletData,
    max_records: int | None = None,
    skill_config: dict[str, float] | None = None,
) -> dict[str, Any]:
    config = {**DEFAULT_SKILL_CONFIG, **(skill_config or {})}
    grouped = group_records_by_market(wallet_data)
    markets = [analyze_market(market_id, bucket) for market_id, bucket in grouped.markets.items()]
    markets.sort(key=lambda row: float(row["trading_pnl"]), reverse=True)

    warnings = build_warnings(markets, grouped.unmapped_records, wallet_data, max_records, config)
    category_breakdown = build_category_breakdown(markets, config)
    summary = summarize_results(markets, wallet_data, grouped.unmapped_records, warnings, max_records, config)
    summary["verdict"] = final_verdict(summary, category_breakdown, config)
    summary["skill_verdict"] = summary["verdict"]
    summary["skill_verdict_label"] = verdict_label(summary["verdict"])
    summary["category_skill_summary"] = category_skill_sentence(category_breakdown)
    summary["is_probably_skilled"] = summary["verdict"] == "skilled"

    skill = build_skill_report(markets, summary, category_breakdown)

    return {
        "wallet": wallet_data.wallet,
        "summary": summary,
        "category_breakdown": category_breakdown,
        "top_winning_markets": [compact_market(row) for row in markets if row["trading_pnl"] > 0][:10],
        "top_losing_markets": [compact_market(row) for row in sorted(markets, key=lambda item: item["trading_pnl"]) if row_pnl(row) < 0][
            :10
        ],
        "markets": markets,
        "market_level_pnl": [compact_market(row) for row in markets],
        "outcome_level_edge": flatten_outcome_edges(markets),
        "unmapped_records_count": len(grouped.unmapped_records),
        "unmapped_records": grouped.unmapped_records[:100],
        "warnings": warnings,
        "raw_counts": wallet_data.counts,
        "skill": skill,
    }


def group_by_market(wallet_data: WalletData) -> dict[str, MarketBucket]:
    return group_records_by_market(wallet_data).markets


def group_records_by_market(wallet_data: WalletData) -> GroupedRecords:
    token_condition_map = build_token_condition_map(wallet_data)
    grouped: dict[str, MarketBucket] = defaultdict(new_market_bucket)
    unmapped: list[dict[str, Any]] = []

    for source_name in BUCKET_SOURCES:
        for record in getattr(wallet_data, source_name):
            market_key = get_market_key(record, token_condition_map)
            if market_key:
                grouped[market_key][source_name].append(record)
            else:
                unmapped.append(unmapped_record(source_name, record))

    return GroupedRecords(markets=dict(grouped), unmapped_records=unmapped)


def new_market_bucket() -> MarketBucket:
    return {"trades": [], "positions": [], "closed_positions": [], "activity": []}


def analyze_market(market_id: str, bucket: MarketBucket) -> dict[str, Any]:
    trades = bucket["trades"]
    positions = bucket["positions"]
    closed_positions = bucket["closed_positions"]
    activity = bucket["activity"]
    all_records = positions + closed_positions + trades + activity

    ledger = reconstruct_ledger(trades, activity)
    rewards_pnl = sum(num(record, "usdcSize", "usdc_size", "amount") for record in activity if activity_type(record) in REWARD_ACTIVITY_TYPES)
    current_value = sum(num(record, "currentValue", "current_value") for record in positions)
    unrealized_pnl = sum(unrealized_position_pnl(record) for record in positions)

    open_cost = sum(record_cost(record) for record in positions)
    closed_cost = sum(record_cost(record) for record in closed_positions)
    api_record_cost = open_cost + closed_cost
    buy_notional_fallback = api_record_cost
    total_buy_notional = ledger.total_buy_notional if ledger.has_buy_data else buy_notional_fallback

    realized_pnl, realized_source = choose_realized_pnl(ledger, positions, closed_positions)
    trading_pnl = realized_pnl + unrealized_pnl
    total_pnl_including_rewards = trading_pnl + rewards_pnl

    cost_basis = cost_basis_for_market(ledger, api_record_cost, open_cost, current_value, total_buy_notional)
    max_capital_at_risk = ledger.max_capital_at_risk if ledger.has_buy_data else cost_basis
    max_capital_at_risk_estimated = not ledger.has_buy_data
    proceeds = realized_proceeds(ledger, closed_positions)
    roi_cost_basis = safe_div(trading_pnl, cost_basis)
    roi_buy_notional = safe_div(trading_pnl, total_buy_notional)
    roi_max_capital_at_risk = safe_div(trading_pnl, max_capital_at_risk)
    outcome_edges = outcome_level_edges(market_id, positions, closed_positions, trades, activity)
    total_shares, avg_entry_price = entry_price_and_shares(positions, closed_positions, trades)
    resolved, won = infer_resolution(positions, closed_positions, activity, trading_pnl)

    title = first_text(all_records, "title", "question") or market_id
    slug = first_text(all_records, "slug", "marketSlug", "market_slug") or ""
    event_slug = first_text(all_records, "eventSlug", "event_slug") or ""
    outcomes = sorted({text(record, "outcome") for record in all_records if text(record, "outcome")})
    category = classify_market(title, slug, event_slug)

    return {
        "market_id": market_id,
        "title": title,
        "slug": slug,
        "event_slug": event_slug,
        "category": category,
        "outcomes": ", ".join(outcomes),
        "cost": cost_basis,
        "cost_basis": cost_basis,
        "total_buy_notional": total_buy_notional,
        "buy_notional": total_buy_notional,
        "buy_cost_from_trades": ledger.total_buy_notional,
        "max_capital_at_risk": max_capital_at_risk,
        "max_capital_at_risk_estimated": max_capital_at_risk_estimated,
        "proceeds": proceeds,
        "current_value": current_value,
        "realized_pnl": realized_pnl,
        "realized_pnl_source": realized_source,
        "unrealized_pnl": unrealized_pnl,
        "rewards_pnl": rewards_pnl,
        "trading_pnl": trading_pnl,
        "total_pnl_including_rewards": total_pnl_including_rewards,
        "pnl": trading_pnl,
        "total_pnl": trading_pnl,
        "roi": roi_cost_basis,
        "roi_cost_basis": roi_cost_basis,
        "roi_buy_notional": roi_buy_notional,
        "roi_max_capital_at_risk": roi_max_capital_at_risk,
        "avg_entry_price": avg_entry_price,
        "total_shares": total_shares,
        "outcome_level_edge": outcome_edges,
        "market_level_pnl": trading_pnl,
        "resolved": resolved,
        "won": won,
        "timestamp": market_timestamp(all_records),
        "open_positions": len(positions),
        "closed_positions": len(closed_positions),
        "trade_count": len(trades),
        "activity_count": len(activity),
        "end_date": first_text(all_records, "endDate", "end_date") or "",
        "warnings": market_warnings(ledger, positions, closed_positions),
    }


def choose_realized_pnl(
    ledger: LedgerMetrics,
    positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
) -> tuple[float, str]:
    if ledger.has_realized_data and not ledger.incomplete:
        return ledger.realized_pnl, "reconstructed_from_trades_activity"

    closed_realized = sum(num(record, "realizedPnl", "realized_pnl") for record in closed_positions)
    position_realized = sum(num(record, "realizedPnl", "realized_pnl") for record in positions)

    if closed_positions:
        return closed_realized, "closed_positions_api"
    if position_realized:
        return position_realized, "open_positions_api"
    if ledger.has_realized_data:
        return ledger.realized_pnl, "partial_reconstruction"
    return 0.0, "none"


def reconstruct_ledger(trades: list[dict[str, Any]], activity: list[dict[str, Any]]) -> LedgerMetrics:
    inventory_quantity: dict[str, float] = defaultdict(float)
    inventory_cost: dict[str, float] = defaultdict(float)
    current_capital = 0.0
    metrics = LedgerMetrics()

    events: list[tuple[float, int, dict[str, Any], str]] = []
    for trade in trades:
        events.append((num(trade, "timestamp"), 0, trade, "trade"))
    for record in activity:
        record_type = activity_type(record)
        if record_type in {"REDEEM"} | NON_TRADING_ACTIVITY_TYPES | REWARD_ACTIVITY_TYPES:
            events.append((num(record, "timestamp"), 1, record, "activity"))

    for _timestamp, _priority, record, source in sorted(events, key=lambda event: (event[0], event[1])):
        if source == "trade":
            side = text(record, "side").upper()
            outcome_key = get_outcome_key(record)
            quantity = num(record, "size")
            notional = trade_notional(record)
            if not outcome_key or quantity <= 0:
                continue
            if side == "BUY":
                inventory_quantity[outcome_key] += quantity
                inventory_cost[outcome_key] += notional
                current_capital += notional
                metrics.total_buy_notional += notional
                metrics.max_capital_at_risk = max(metrics.max_capital_at_risk, current_capital)
                metrics.has_buy_data = True
            elif side == "SELL":
                removed_cost = remove_inventory_cost(inventory_quantity, inventory_cost, outcome_key, quantity)
                if removed_cost is None:
                    removed_cost = notional
                    metrics.incomplete = True
                current_capital = max(0.0, current_capital - removed_cost)
                metrics.sell_proceeds += notional
                metrics.realized_pnl += notional - removed_cost
                metrics.has_realized_data = True
        else:
            record_type = activity_type(record)
            if record_type == "REDEEM":
                outcome_key = get_outcome_key(record)
                quantity = num(record, "size")
                proceeds = num(record, "usdcSize", "usdc_size")
                removed_cost = None
                if outcome_key and quantity > 0:
                    removed_cost = remove_inventory_cost(inventory_quantity, inventory_cost, outcome_key, quantity)
                if removed_cost is None:
                    removed_cost = 0.0
                    metrics.incomplete = True
                current_capital = max(0.0, current_capital - removed_cost)
                metrics.redeem_proceeds += proceeds
                metrics.realized_pnl += proceeds - removed_cost
                metrics.has_realized_data = True

    metrics.ending_inventory_cost = sum(inventory_cost.values())
    return metrics


def remove_inventory_cost(
    inventory_quantity: dict[str, float],
    inventory_cost: dict[str, float],
    outcome_key: str,
    quantity: float,
) -> float | None:
    existing_quantity = inventory_quantity.get(outcome_key, 0.0)
    existing_cost = inventory_cost.get(outcome_key, 0.0)
    if existing_quantity <= 0 or existing_cost < 0:
        return None
    quantity_to_remove = min(quantity, existing_quantity)
    removed_cost = existing_cost * (quantity_to_remove / existing_quantity)
    inventory_quantity[outcome_key] = existing_quantity - quantity_to_remove
    inventory_cost[outcome_key] = max(0.0, existing_cost - removed_cost)
    if quantity > existing_quantity + 1e-9:
        return None
    return removed_cost


def cost_basis_for_market(
    ledger: LedgerMetrics,
    api_record_cost: float,
    open_cost: float,
    current_value: float,
    total_buy_notional: float,
) -> float:
    if ledger.has_buy_data:
        return ledger.max_capital_at_risk or ledger.ending_inventory_cost or total_buy_notional
    if api_record_cost > 0:
        return api_record_cost
    if open_cost > 0:
        return open_cost
    return current_value


def realized_proceeds(ledger: LedgerMetrics, closed_positions: list[dict[str, Any]]) -> float:
    proceeds = ledger.sell_proceeds + ledger.redeem_proceeds
    if proceeds > 0:
        return proceeds
    closed_cost = sum(record_cost(record) for record in closed_positions)
    closed_realized = sum(num(record, "realizedPnl", "realized_pnl") for record in closed_positions)
    return max(0.0, closed_cost + closed_realized) if closed_cost else 0.0


def summarize_results(
    results: list[dict[str, Any]],
    wallet_data: WalletData,
    unmapped_records: list[dict[str, Any]],
    warnings: list[str] | None = None,
    max_records: int | None = None,
    skill_config: dict[str, float] | None = None,
) -> dict[str, Any]:
    config = {**DEFAULT_SKILL_CONFIG, **(skill_config or {})}
    summary = aggregate_market_metrics(results, config)
    raw_record_count = sum(wallet_data.counts.values())
    unmapped_count = len(unmapped_records)
    data_truncated = bool(max_records and any(count >= max_records for count in wallet_data.counts.values()))

    summary.update(
        {
            "wallet": wallet_data.wallet,
            "position_value_api": wallet_data.position_value,
            "traded_api": wallet_data.traded,
            "unmapped_records_count": unmapped_count,
            "unmapped_records_ratio": safe_div(unmapped_count, raw_record_count) or 0.0,
            "data_truncated": data_truncated,
            "warnings": warnings or [],
        }
    )
    summary["confidence_level"] = confidence_level(summary, config)
    summary["skill_confidence"] = summary["confidence_level"]
    return add_legacy_summary_aliases(summary)


def aggregate_market_metrics(results: list[dict[str, Any]], config: dict[str, float]) -> dict[str, Any]:
    total_markets = len(results)
    trading_pnl = sum(row_pnl(row) for row in results)
    rewards_pnl = sum(float(row.get("rewards_pnl") or 0.0) for row in results)
    realized_pnl = sum(float(row.get("realized_pnl") or 0.0) for row in results)
    unrealized_pnl = sum(float(row.get("unrealized_pnl") or 0.0) for row in results)
    cost_basis = sum(float(row.get("cost_basis") or 0.0) for row in results)
    buy_notional = sum(float(row.get("total_buy_notional") or 0.0) for row in results)
    max_capital_at_risk = sum(float(row.get("max_capital_at_risk") or 0.0) for row in results)
    current_value = sum(float(row.get("current_value") or 0.0) for row in results)
    gross_profit = sum(max(0.0, row_pnl(row)) for row in results)
    gross_loss = sum(min(0.0, row_pnl(row)) for row in results)
    positive_rows = [row for row in sorted(results, key=row_pnl, reverse=True) if row_pnl(row) > 0]
    roi_values = [float(row["roi_cost_basis"]) for row in results if row.get("roi_cost_basis") is not None]
    buy_roi_values = [float(row["roi_buy_notional"]) for row in results if row.get("roi_buy_notional") is not None]
    monthly_pnl = monthly_pnl_rows(results)

    top1_pnl = sum(row_pnl(row) for row in positive_rows[:1])
    top3_pnl = sum(row_pnl(row) for row in positive_rows[:3])
    top5_pnl = sum(row_pnl(row) for row in positive_rows[:5])
    top1_cost = sum(float(row.get("cost_basis") or 0.0) for row in positive_rows[:1])
    top3_cost = sum(float(row.get("cost_basis") or 0.0) for row in positive_rows[:3])
    top5_cost = sum(float(row.get("cost_basis") or 0.0) for row in positive_rows[:5])
    top1_buy = sum(float(row.get("total_buy_notional") or 0.0) for row in positive_rows[:1])
    top3_buy = sum(float(row.get("total_buy_notional") or 0.0) for row in positive_rows[:3])
    top5_buy = sum(float(row.get("total_buy_notional") or 0.0) for row in positive_rows[:5])
    positive_profits = [row_pnl(row) for row in positive_rows]

    roi_ex_top1_cost = safe_div(trading_pnl - top1_pnl, cost_basis - top1_cost)
    roi_ex_top3_cost = safe_div(trading_pnl - top3_pnl, cost_basis - top3_cost)
    roi_ex_top5_cost = safe_div(trading_pnl - top5_pnl, cost_basis - top5_cost)
    roi_ex_top1_buy = safe_div(trading_pnl - top1_pnl, buy_notional - top1_buy)
    roi_ex_top3_buy = safe_div(trading_pnl - top3_pnl, buy_notional - top3_buy)
    roi_ex_top5_buy = safe_div(trading_pnl - top5_pnl, buy_notional - top5_buy)

    top1_contribution_net = safe_div(top1_pnl, trading_pnl) if trading_pnl > 0 and top1_pnl > 0 else None
    top3_contribution_net = safe_div(top3_pnl, trading_pnl) if trading_pnl > 0 and top3_pnl > 0 else None
    top1_share_gross = safe_div(top1_pnl, gross_profit) if gross_profit > 0 else None
    top3_share_gross = safe_div(top3_pnl, gross_profit) if gross_profit > 0 else None

    is_one_hit = any(
        predicate
        for predicate in (
            top1_contribution_net is not None and top1_contribution_net > config["one_hit_top1_contribution"],
            roi_ex_top1_cost is not None and roi_ex_top1_cost < 0,
            roi_ex_top1_buy is not None and roi_ex_top1_buy < 0,
        )
    )
    is_top3_dependent = any(
        predicate
        for predicate in (
            top3_contribution_net is not None and top3_contribution_net > config["top3_dependency_contribution"],
            roi_ex_top3_cost is not None and roi_ex_top3_cost < 0,
            roi_ex_top3_buy is not None and roi_ex_top3_buy < 0,
        )
    )

    total_active_months = len(monthly_pnl)
    profitable_months = sum(1 for row in monthly_pnl if row["pnl"] > 0)
    cost_weighted_roi = safe_div(
        sum(float(row.get("roi_cost_basis") or 0.0) * float(row.get("cost_basis") or 0.0) for row in results),
        cost_basis,
    )

    return {
        "trading_pnl": trading_pnl,
        "rewards_pnl": rewards_pnl,
        "total_pnl_including_rewards": trading_pnl + rewards_pnl,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_markets": total_markets,
        "winning_markets": sum(1 for row in results if row_pnl(row) > 0),
        "losing_markets": sum(1 for row in results if row_pnl(row) < 0),
        "market_win_rate": safe_div(sum(1 for row in results if row_pnl(row) > 0), total_markets) or 0.0,
        "cost_basis": cost_basis,
        "total_buy_notional": buy_notional,
        "max_capital_at_risk": max_capital_at_risk,
        "max_capital_at_risk_estimated": any(row.get("max_capital_at_risk_estimated") for row in results),
        "total_current_value": current_value,
        "roi_cost_basis": safe_div(trading_pnl, cost_basis),
        "roi_buy_notional": safe_div(trading_pnl, buy_notional),
        "roi_max_capital_at_risk": safe_div(trading_pnl, max_capital_at_risk),
        "roi_ex_top1": roi_ex_top1_cost,
        "roi_ex_top3": roi_ex_top3_cost,
        "roi_ex_top5": roi_ex_top5_cost,
        "roi_ex_top1_cost_basis": roi_ex_top1_cost,
        "roi_ex_top3_cost_basis": roi_ex_top3_cost,
        "roi_ex_top5_cost_basis": roi_ex_top5_cost,
        "roi_ex_top1_buy_notional": roi_ex_top1_buy,
        "roi_ex_top3_buy_notional": roi_ex_top3_buy,
        "roi_ex_top5_buy_notional": roi_ex_top5_buy,
        "top1_contribution_net_pnl": top1_contribution_net,
        "top3_contribution_net_pnl": top3_contribution_net,
        "top1_share_of_gross_profit": top1_share_gross,
        "top3_share_of_gross_profit": top3_share_gross,
        "median_market_roi": median(roi_values) if roi_values else None,
        "mean_market_roi_unweighted": mean(roi_values) if roi_values else None,
        "mean_market_roi_cost_weighted": cost_weighted_roi,
        "mean_market_roi_buy_notional_unweighted": mean(buy_roi_values) if buy_roi_values else None,
        "profit_factor": safe_div(gross_profit, abs(gross_loss)) if gross_loss < 0 else (None if gross_profit == 0 else float("inf")),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "hhi_profit_concentration": hhi(positive_profits),
        "gini_profit_concentration": gini(positive_profits),
        "effective_bets": effective_bets(results),
        "max_drawdown": max_drawdown(monthly_pnl),
        "profitable_months_count": profitable_months,
        "total_active_months": total_active_months,
        "monthly_pnl": monthly_pnl,
        "is_one_hit_wonder": is_one_hit,
        "is_top3_dependent": is_top3_dependent,
        "top_market_title": positive_rows[0]["title"] if positive_rows else "",
        "top_market_pnl": top1_pnl,
        "outcome_level_edge": aggregate_outcome_edges(results),
    }


def add_legacy_summary_aliases(summary: dict[str, Any]) -> dict[str, Any]:
    summary["market_count"] = summary["total_markets"]
    summary["total_cost"] = summary["cost_basis"]
    summary["total_realized_pnl"] = summary["realized_pnl"]
    summary["total_unrealized_pnl"] = summary["unrealized_pnl"]
    summary["total_pnl"] = summary["trading_pnl"]
    summary["total_roi"] = summary["roi_cost_basis"]
    summary["median_roi"] = summary["median_market_roi"]
    summary["top1_contribution"] = summary["top1_contribution_net_pnl"]
    summary["top3_contribution"] = summary["top3_contribution_net_pnl"]
    return summary


def build_category_breakdown(results: list[dict[str, Any]], config: dict[str, float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        category_rows = [row for row in results if row.get("category") == category]
        if not category_rows:
            continue
        metrics = aggregate_market_metrics(category_rows, config)
        verdict = category_verdict(metrics, config)
        if category == "Other" and verdict == "skilled":
            verdict = "inconclusive"
        rows.append(
            {
                "category": category,
                "number_of_markets": metrics["total_markets"],
                "trading_pnl": metrics["trading_pnl"],
                "roi_cost_basis": metrics["roi_cost_basis"],
                "roi_buy_notional": metrics["roi_buy_notional"],
                "market_win_rate": metrics["market_win_rate"],
                "median_market_roi": metrics["median_market_roi"],
                "roi_ex_top1": metrics["roi_ex_top1"],
                "top1_contribution": metrics["top1_contribution_net_pnl"],
                "verdict": verdict,
                "effective_bets": metrics["effective_bets"],
                "profit_factor": metrics["profit_factor"],
            }
        )
    return sorted(rows, key=lambda row: float(row["trading_pnl"]), reverse=True)


def category_verdict(metrics: dict[str, Any], config: dict[str, float]) -> str:
    if metrics["trading_pnl"] <= 0:
        return "unprofitable"
    if metrics["total_markets"] < config["category_skilled_min_markets"]:
        return "inconclusive"
    if metrics["is_one_hit_wonder"]:
        return "lucky_or_one_hit_wonder"
    if (
        (metrics["roi_buy_notional"] or 0.0) > config["skilled_roi_buy_notional"]
        and (metrics["roi_ex_top1_buy_notional"] or 0.0) > 0
        and metrics["market_win_rate"] > config["skilled_market_win_rate"]
        and (metrics["median_market_roi"] or -1.0) > config["skilled_median_roi_floor"]
    ):
        return "skilled"
    return "inconclusive"


def final_verdict(summary: dict[str, Any], category_breakdown: list[dict[str, Any]], config: dict[str, float]) -> str:
    if summary["total_markets"] == 0:
        return "insufficient_data"
    if summary["trading_pnl"] <= 0:
        return "unprofitable"
    if summary["is_one_hit_wonder"]:
        return "lucky_or_one_hit_wonder"
    if summary["total_markets"] < 5 or summary["unmapped_records_ratio"] > 0.25:
        return "insufficient_data"

    is_skilled = all(
        (
            summary["trading_pnl"] > 0,
            (summary["roi_buy_notional"] or 0.0) > config["skilled_roi_buy_notional"],
            summary["total_markets"] >= config["skilled_min_markets"],
            (summary["roi_ex_top1_buy_notional"] or -1.0) > 0,
            (summary["roi_ex_top3_buy_notional"] or -1.0) >= 0,
            summary["market_win_rate"] > config["skilled_market_win_rate"],
            (summary["median_market_roi"] or -1.0) > config["skilled_median_roi_floor"],
            (summary["top1_contribution_net_pnl"] or 1.0) < config["skilled_top1_contribution_max"],
            summary["confidence_level"] != "low",
        )
    )
    if is_skilled:
        return "skilled"
    if any(row["verdict"] == "skilled" and row["category"] != "Other" for row in category_breakdown):
        return "category_skilled"
    return "inconclusive"


def confidence_level(summary: dict[str, Any], config: dict[str, float]) -> str:
    if summary["data_truncated"]:
        return "low"
    if summary["total_markets"] < config["skilled_min_markets"]:
        return "low"
    if summary["unmapped_records_ratio"] > config["max_unmapped_ratio_medium"]:
        return "low"
    if unrealized_ratio(summary) > config["max_unrealized_pnl_ratio_medium"]:
        return "low"
    if summary["is_one_hit_wonder"] or summary["is_top3_dependent"]:
        return "low"
    other_ratio = safe_div(summary.get("other_markets", 0.0), summary["total_markets"]) or 0.0
    if other_ratio > config["max_other_category_ratio_medium"]:
        return "low"
    if summary["total_markets"] >= 75 and summary["effective_bets"] >= 50:
        return "high"
    return "medium"


def build_warnings(
    results: list[dict[str, Any]],
    unmapped_records: list[dict[str, Any]],
    wallet_data: WalletData,
    max_records: int | None,
    config: dict[str, float],
) -> list[str]:
    warnings: list[str] = []
    if max_records and any(count >= max_records for count in wallet_data.counts.values()):
        warnings.append("Dữ liệu có thể bị truncate vì ít nhất một endpoint chạm max_records.")
    if unmapped_records:
        warnings.append(f"Có {len(unmapped_records)} record chỉ có asset/token hoặc thiếu market key; đã loại khỏi kết luận skill.")
    if any(row.get("max_capital_at_risk_estimated") for row in results):
        warnings.append("max_capital_at_risk là best-effort cho market thiếu lịch sử trades đầy đủ.")
    if sum(1 for row in results if row.get("category") == "Other") / max(1, len(results)) > config["max_other_category_ratio_medium"]:
        warnings.append("Nhiều market bị phân loại Other; kết luận theo category có thể yếu.")
    return warnings


def build_skill_report(
    markets: list[dict[str, Any]], summary: dict[str, Any], category_breakdown: list[dict[str, Any]]
) -> dict[str, Any]:
    components = skill_components(summary)
    usable_components = [component for component in components if component["normalized"] is not None]
    total_weight = sum(float(component["weight"]) for component in usable_components)
    skill_score = None
    if usable_components and summary["trading_pnl"] > 0:
        for component in usable_components:
            component["contribution"] = round(
                100.0 * float(component["normalized"]) * float(component["weight"]) / total_weight,
                1,
            )
        skill_score = int(round(sum(float(component["contribution"]) for component in usable_components)))
    for component in components:
        component.setdefault("contribution", None)

    return {
        "skill_score": skill_score,
        "verdict": summary["verdict"],
        "legacy_verdict": legacy_verdict(summary["verdict"]),
        "verdict_label": verdict_label(summary["verdict"]),
        "verdict_detail": verdict_detail(summary, category_breakdown),
        "confidence": summary["confidence_level"],
        "confidence_level": summary["confidence_level"],
        "data_truncated": summary["data_truncated"],
        "components": components,
        "edge": summary["outcome_level_edge"],
        "consistency": {
            "active_months": summary["total_active_months"],
            "profitable_months": summary["profitable_months_count"],
            "profitable_period_ratio": safe_div(summary["profitable_months_count"], summary["total_active_months"]),
            "monthly_pnl": summary["monthly_pnl"],
        },
        "breadth": {"market_count": summary["total_markets"], "effective_bets": summary["effective_bets"]},
        "concentration": {
            "top1_contribution_net_pnl": summary["top1_contribution_net_pnl"],
            "top3_contribution_net_pnl": summary["top3_contribution_net_pnl"],
            "hhi": summary["hhi_profit_concentration"],
            "gini": summary["gini_profit_concentration"],
        },
    }


def skill_components(summary: dict[str, Any]) -> list[dict[str, Any]]:
    profit_factor = summary["profit_factor"]
    risk_score = None
    if profit_factor == float("inf"):
        risk_score = 1.0
    elif profit_factor is not None:
        risk_score = clamp((float(profit_factor) - 1.0) / 2.0, 0.0, 1.0)

    concentration = summary["top1_share_of_gross_profit"]
    return [
        {
            "label": "Ý nghĩa thống kê / hit-rate",
            "normalized": clamp(((summary["market_win_rate"] or 0.0) - 0.45) / 0.25, 0.0, 1.0),
            "weight": 25,
            "detail": f"Win rate {format_percent(summary['market_win_rate'])}, median ROI {format_percent(summary['median_market_roi'])}.",
        },
        {
            "label": "Outcome-level edge",
            "normalized": edge_score(summary["outcome_level_edge"]),
            "weight": 20,
            "detail": outcome_edge_detail(summary["outcome_level_edge"]),
        },
        {
            "label": "Ổn định theo thời gian",
            "normalized": safe_div(summary["profitable_months_count"], summary["total_active_months"]),
            "weight": 15,
            "detail": f"Lãi {summary['profitable_months_count']}/{summary['total_active_months']} tháng hoạt động.",
        },
        {
            "label": "Số quyết định độc lập",
            "normalized": clamp(float(summary["effective_bets"]) / 50.0, 0.0, 1.0),
            "weight": 15,
            "detail": f"Effective bets ≈ {summary['effective_bets']} sau khi gom eventSlug.",
        },
        {
            "label": "Không phụ thuộc top market",
            "normalized": None if concentration is None else clamp(1.0 - float(concentration), 0.0, 1.0),
            "weight": 15,
            "detail": f"Top1/gross profit = {format_percent(concentration)}.",
        },
        {
            "label": "Hiệu suất điều chỉnh rủi ro",
            "normalized": risk_score,
            "weight": 10,
            "detail": f"Profit factor {format_number(profit_factor)}, max drawdown {format_money(summary['max_drawdown'])}.",
        },
    ]


def outcome_level_edges(
    market_id: str,
    positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    activity: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for record in positions + closed_positions:
        outcome_key = get_outcome_key(record)
        if not outcome_key:
            continue
        group = grouped.setdefault(outcome_key, new_outcome_edge_group(market_id, record, outcome_key))
        shares = num(record, "totalBought", "total_bought") or num(record, "size")
        cost = record_cost(record)
        group["shares"] += shares
        group["cost"] += cost
        group["records"].append(record)
        if record in closed_positions:
            group["resolved"] = True

    for trade in trades:
        if text(trade, "side").upper() != "BUY":
            continue
        outcome_key = get_outcome_key(trade)
        if not outcome_key:
            continue
        group = grouped.setdefault(outcome_key, new_outcome_edge_group(market_id, trade, outcome_key))
        group["shares"] += num(trade, "size")
        group["cost"] += trade_notional(trade)
        group["records"].append(trade)

    redeemed_outcomes = {get_outcome_key(record) for record in activity if activity_type(record) == "REDEEM"}
    edges: list[dict[str, Any]] = []
    for outcome_key, group in grouped.items():
        shares = float(group["shares"])
        if shares <= 0:
            continue
        avg_entry = float(group["cost"]) / shares if shares else None
        resolved = bool(group["resolved"] or outcome_key in redeemed_outcomes)
        outcome_result = infer_outcome_result(group["records"], resolved)
        edge_per_share = None if outcome_result is None or avg_entry is None else outcome_result - avg_entry
        edges.append(
            {
                "market_id": market_id,
                "outcome_key": outcome_key,
                "asset": first_text(group["records"], "asset", "token_id", "tokenId") or "",
                "outcome": first_text(group["records"], "outcome") or "",
                "outcome_index": first_text(group["records"], "outcomeIndex", "outcome_index") or "",
                "shares": shares,
                "avg_entry_price": avg_entry,
                "resolved": resolved,
                "outcome_result": outcome_result,
                "edge_per_share": edge_per_share,
            }
        )
    return edges


def new_outcome_edge_group(market_id: str, record: dict[str, Any], outcome_key: str) -> dict[str, Any]:
    return {"market_id": market_id, "outcome_key": outcome_key, "shares": 0.0, "cost": 0.0, "records": [], "resolved": False}


def infer_outcome_result(records: list[dict[str, Any]], resolved: bool) -> float | None:
    if not resolved:
        return None
    for record in records:
        cur_price = maybe_num(record, "curPrice", "cur_price")
        if cur_price is not None and (cur_price <= 0.01 or cur_price >= 0.99):
            return 1.0 if cur_price >= 0.99 else 0.0
    realized = sum(num(record, "realizedPnl", "realized_pnl") for record in records)
    return 1.0 if realized > 0 else 0.0


def aggregate_outcome_edges(results: list[dict[str, Any]]) -> dict[str, Any]:
    edges = [edge for row in results for edge in row.get("outcome_level_edge", []) if edge.get("edge_per_share") is not None]
    if not edges:
        return {"n_resolved": 0, "edge_per_share": None, "edge_per_share_weighted": None, "win_rate": None, "avg_entry_price": None}
    total_shares = sum(float(edge.get("shares") or 0.0) for edge in edges)
    return {
        "n_resolved": len(edges),
        "edge_per_share": mean(float(edge["edge_per_share"]) for edge in edges),
        "edge_per_share_weighted": safe_div(
            sum(float(edge["edge_per_share"]) * float(edge.get("shares") or 0.0) for edge in edges), total_shares
        ),
        "win_rate": mean(float(edge["outcome_result"]) for edge in edges if edge.get("outcome_result") is not None),
        "avg_entry_price": safe_div(sum(float(edge["avg_entry_price"]) * float(edge.get("shares") or 0.0) for edge in edges), total_shares),
    }


def flatten_outcome_edges(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [edge for row in results for edge in row.get("outcome_level_edge", [])]


def build_token_condition_map(wallet_data: WalletData) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for source_name in BUCKET_SOURCES:
        for record in getattr(wallet_data, source_name):
            market_id = explicit_market_key(record)
            if not market_id:
                continue
            for token in token_candidates(record):
                mapping[token] = market_id
    return mapping


def get_market_key(record: dict[str, Any], token_condition_map: dict[str, str] | None = None) -> str | None:
    explicit_key = explicit_market_key(record)
    if explicit_key:
        return explicit_key
    if token_condition_map:
        for token in token_candidates(record):
            if token in token_condition_map:
                return token_condition_map[token]
    return None


def explicit_market_key(record: dict[str, Any]) -> str | None:
    for path in (
        ("conditionId",),
        ("condition_id",),
        ("metadata", "conditionId"),
        ("metadata", "condition_id"),
        ("market", "conditionId"),
        ("market", "condition_id"),
        ("marketId",),
        ("market_id",),
        ("metadata", "marketId"),
        ("metadata", "market_id"),
        ("slug",),
        ("marketSlug",),
        ("market_slug",),
    ):
        value = nested_value(record, path)
        if value:
            return str(value)
    return None


def token_candidates(record: dict[str, Any]) -> list[str]:
    values = []
    for key in ("asset", "token_id", "tokenId", "clobTokenId", "oppositeAsset"):
        value = record.get(key)
        if value:
            values.append(str(value))
    return values


def get_outcome_key(record: dict[str, Any]) -> str | None:
    market_key = explicit_market_key(record) or text(record, "conditionId") or text(record, "condition_id")
    token = first_text([record], "asset", "token_id", "tokenId", "clobTokenId")
    if market_key and token:
        return f"{market_key}:{token}"
    outcome_index = first_text([record], "outcomeIndex", "outcome_index")
    outcome = first_text([record], "outcome")
    if market_key and outcome_index:
        return f"{market_key}:outcome_index:{outcome_index}"
    if market_key and outcome:
        return f"{market_key}:outcome:{outcome.lower()}"
    return token


def unmapped_record(source_name: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source_name,
        "asset": first_text([record], "asset", "token_id", "tokenId") or "",
        "title": first_text([record], "title", "question") or "",
        "timestamp": maybe_num(record, "timestamp"),
        "reason": "missing market-level conditionId/marketId/slug and token could not be mapped",
    }


def classify_market(*values: str) -> str:
    haystack = " ".join(value for value in values if value).lower()
    categories = {
        "Weather": ("hurricane", "temperature", "rain", "snow", "weather", "climate", "storm", "heat"),
        "Politics": (
            "election",
            "president",
            "trump",
            "biden",
            "senate",
            "congress",
            "democrat",
            "republican",
            "minister",
            "mayor",
            "governor",
            "parliament",
            "vote",
            "primary",
        ),
        "Sports": (
            "nba",
            "nfl",
            "mlb",
            "nhl",
            "ufc",
            "soccer",
            "football",
            "champions league",
            "world cup",
            "super bowl",
            "premier league",
            "tennis",
        ),
        "Crypto": (
            "bitcoin",
            "btc",
            "ethereum",
            "eth",
            "solana",
            "token",
            "airdrop",
            "binance",
            "coinbase",
            "crypto",
            "xrp",
            "doge",
        ),
        "Economy/Fed": (
            "fed",
            "interest rate",
            "rate cut",
            "cpi",
            "inflation",
            "recession",
            "unemployment",
            "gdp",
            "stock market",
            "s&p",
            "nasdaq",
        ),
        "Tech/AI": ("openai", "anthropic", "ai", "artificial intelligence", "nvidia", "apple", "google", "tesla", "spacex"),
        "Geopolitics": ("war", "ceasefire", "ukraine", "russia", "israel", "china", "taiwan", "iran", "nato"),
        "Entertainment": ("oscar", "grammy", "movie", "album", "song", "box office", "netflix", "celebrity", "gta"),
    }
    for category, keywords in categories.items():
        if any(keyword in haystack for keyword in keywords):
            return category
    return "Other"


def record_cost(record: dict[str, Any]) -> float:
    total_bought = num(record, "totalBought", "total_bought")
    avg_price = num(record, "avgPrice", "avg_price")
    if total_bought > 0 and avg_price > 0:
        return total_bought * avg_price
    initial_value = num(record, "initialValue", "initial_value")
    if initial_value > 0:
        return initial_value
    return num(record, "size") * avg_price


def trade_notional(record: dict[str, Any]) -> float:
    return num(record, "size") * num(record, "price")


def unrealized_position_pnl(record: dict[str, Any]) -> float:
    if has_number(record, "cashPnl"):
        return num(record, "cashPnl")
    return num(record, "currentValue", "current_value") - num(record, "initialValue", "initial_value")


def entry_price_and_shares(
    positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]], trades: list[dict[str, Any]]
) -> tuple[float, float | None]:
    shares = 0.0
    cost = 0.0
    source_records = positions + closed_positions
    if source_records:
        for record in source_records:
            record_shares = num(record, "totalBought", "total_bought") or num(record, "size")
            shares += record_shares
            cost += record_cost(record)
    else:
        for trade in trades:
            if text(trade, "side").upper() == "BUY":
                shares += num(trade, "size")
                cost += trade_notional(trade)
    return shares, safe_div(cost, shares)


def infer_resolution(
    positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]], activity: list[dict[str, Any]], trading_pnl: float
) -> tuple[bool, int | None]:
    resolved = bool(closed_positions) or any(activity_type(record) == "REDEEM" for record in activity)
    if not resolved:
        return False, None
    for record in closed_positions:
        cur_price = maybe_num(record, "curPrice", "cur_price")
        if cur_price is not None and (cur_price <= 0.01 or cur_price >= 0.99):
            return True, 1 if cur_price >= 0.99 else 0
    return True, 1 if trading_pnl > 0 else 0


def market_timestamp(records: list[dict[str, Any]]) -> float | None:
    timestamps = [num(record, "timestamp") for record in records if maybe_num(record, "timestamp") is not None]
    return max(timestamps) if timestamps else None


def monthly_pnl_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    monthly: dict[str, float] = defaultdict(float)
    for row in results:
        timestamp = row.get("timestamp")
        month = month_key(timestamp)
        if month:
            monthly[month] += row_pnl(row)
    return [{"month": month, "pnl": monthly[month]} for month in sorted(monthly)]


def month_key(timestamp: Any) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def max_drawdown(monthly_pnl: list[dict[str, Any]]) -> float:
    peak = 0.0
    cumulative = 0.0
    worst = 0.0
    for row in monthly_pnl:
        cumulative += float(row["pnl"])
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


def effective_bets(results: list[dict[str, Any]]) -> int:
    keys = {str(row.get("event_slug") or row.get("market_id")) for row in results if row.get("event_slug") or row.get("market_id")}
    return len(keys)


def hhi(values: list[float]) -> float | None:
    total = sum(value for value in values if value > 0)
    if total <= 0:
        return None
    return sum((value / total) ** 2 for value in values if value > 0)


def gini(values: list[float]) -> float | None:
    positives = sorted(value for value in values if value > 0)
    value_count = len(positives)
    total = sum(positives)
    if value_count == 0 or total == 0:
        return None
    weighted_sum = sum((index + 1) * value for index, value in enumerate(positives))
    return (2 * weighted_sum) / (value_count * total) - (value_count + 1) / value_count


def market_warnings(ledger: LedgerMetrics, positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    if positions and closed_positions:
        warnings.append("Market has both open and closed records; position realizedPnl is not double-counted.")
    if ledger.incomplete:
        warnings.append("Trade/activity reconstruction incomplete; API realizedPnl fallback may be used.")
    return warnings


def compact_market(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": row["market_id"],
        "title": row["title"],
        "category": row["category"],
        "trading_pnl": row["trading_pnl"],
        "rewards_pnl": row["rewards_pnl"],
        "realized_pnl": row["realized_pnl"],
        "unrealized_pnl": row["unrealized_pnl"],
        "roi_cost_basis": row["roi_cost_basis"],
        "roi_buy_notional": row["roi_buy_notional"],
        "event_slug": row["event_slug"],
    }


def row_pnl(row: dict[str, Any]) -> float:
    return float(row.get("trading_pnl") or row.get("pnl") or 0.0)


def activity_type(record: dict[str, Any]) -> str:
    return text(record, "type", "activityType", "activity_type").upper()


def first_text(records: list[dict[str, Any]], *keys: str) -> str | None:
    for record in records:
        for key in keys:
            value = text(record, key)
            if value:
                return value
    return None


def text(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return str(value).strip()
    return ""


def num(record: dict[str, Any], *keys: str) -> float:
    value = maybe_num(record, *keys)
    return value if value is not None else 0.0


def maybe_num(record: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = record.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def has_number(record: dict[str, Any], key: str) -> bool:
    return maybe_num(record, key) is not None


def nested_value(record: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def safe_div(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def unrealized_ratio(summary: dict[str, Any]) -> float:
    total = abs(float(summary.get("trading_pnl") or 0.0))
    if total == 0:
        return 0.0
    return abs(float(summary.get("unrealized_pnl") or 0.0)) / total


def edge_score(edge: dict[str, Any]) -> float | None:
    value = edge.get("edge_per_share_weighted") if edge.get("edge_per_share_weighted") is not None else edge.get("edge_per_share")
    if value is None:
        return None
    return clamp((float(value) + 0.02) / 0.12, 0.0, 1.0)


def outcome_edge_detail(edge: dict[str, Any]) -> str:
    if edge.get("edge_per_share") is None:
        return "Không đủ outcome đã resolve để đo edge."
    return f"Edge {float(edge['edge_per_share']) * 100:+.1f}¢/share trên {edge['n_resolved']} outcome đã resolve."


def verdict_label(verdict: str) -> str:
    labels = {
        "skilled": "Skilled toàn ví",
        "category_skilled": "Skilled theo category",
        "lucky_or_one_hit_wonder": "Có thể ăn may / one-hit wonder",
        "inconclusive": "Chưa kết luận",
        "unprofitable": "Không có lợi nhuận",
        "insufficient_data": "Thiếu dữ liệu",
    }
    return labels.get(verdict, verdict)


def legacy_verdict(verdict: str) -> str:
    if verdict == "lucky_or_one_hit_wonder":
        return "lucky"
    if verdict == "category_skilled":
        return "inconclusive"
    if verdict == "insufficient_data":
        return "inconclusive"
    return verdict


def verdict_detail(summary: dict[str, Any], category_breakdown: list[dict[str, Any]]) -> str:
    if summary["verdict"] == "skilled":
        return "PnL dương, ROI sau khi bỏ top winners vẫn ổn, win rate/median ROI tốt và confidence không thấp."
    if summary["verdict"] == "category_skilled":
        return category_skill_sentence(category_breakdown)
    if summary["verdict"] == "lucky_or_one_hit_wonder":
        return "Lợi nhuận phụ thuộc top market/top3 hoặc ROI âm sau khi loại market thắng lớn."
    if summary["verdict"] == "unprofitable":
        return "Trading PnL không dương nên chưa có bằng chứng skill."
    if summary["verdict"] == "insufficient_data":
        return "Quá ít market hoặc dữ liệu thiếu/unmapped quá nhiều để kết luận."
    return "Có tín hiệu nhưng chưa đủ mạnh hoặc confidence còn thấp."


def category_skill_sentence(category_breakdown: list[dict[str, Any]]) -> str:
    if not category_breakdown:
        return "Không đủ dữ liệu category."
    skilled = [row["category"] for row in category_breakdown if row["verdict"] == "skilled"]
    inconclusive = [row["category"] for row in category_breakdown if row["verdict"] == "inconclusive"]
    unprofitable = [row["category"] for row in category_breakdown if row["verdict"] == "unprofitable"]
    parts = []
    if skilled:
        parts.append(f"Wallet appears skilled in {', '.join(skilled)}")
    if inconclusive:
        parts.append(f"inconclusive in {', '.join(inconclusive)}")
    if unprofitable:
        parts.append(f"unprofitable in {', '.join(unprofitable)}")
    return ", and ".join(parts) + "." if parts else "Không có category nổi bật."


def format_percent(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def format_money(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"${float(value):,.2f}"


def format_number(value: Any) -> str:
    if value is None:
        return "N/A"
    if value == float("inf"):
        return "∞"
    return f"{float(value):.2f}"
