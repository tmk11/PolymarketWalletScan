from __future__ import annotations

import random
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, median, stdev
from typing import Any, Sequence

from .polymarket_api import WalletData
from .token_resolver import TokenResolver

MarketBucket = dict[str, list[dict[str, Any]]]

BUCKET_SOURCES = ("trades", "positions", "closed_positions", "activity")
REWARD_ACTIVITY_TYPES = {"REWARD", "MAKER_REBATE", "REFERRAL_REWARD"}
NON_TRADING_ACTIVITY_TYPES = {"MERGE", "SPLIT", "CONVERSION"}
RECENT_WINDOWS = (10, 25, 50)
SECONDS_PER_DAY = 86_400

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 1_234_567

DEFAULT_SKILL_CONFIG: dict[str, float] = {
    "one_hit_top1_contribution": 0.50,
    "top3_dependency_contribution": 0.70,
    "skilled_roi_buy_notional": 0.05,
    "skilled_min_markets": 30,
    "skilled_market_win_rate": 0.50,
    "skilled_median_roi_floor": -0.02,
    "skilled_top1_contribution_max": 0.40,
    "category_skilled_min_markets": 10,
    "max_unmapped_ratio_medium": 0.10,
    "max_other_category_ratio_medium": 0.50,
    "max_unrealized_pnl_ratio_medium": 0.50,
    "low_confidence_top1_contribution": 0.70,
    "one_hit_skill_score_cap": 65,
    "low_sample_one_hit_skill_score_cap": 55,
    "insufficient_data_skill_score_cap": 60,
    "inconclusive_skill_score_cap": 72,
    "category_skilled_skill_score_cap": 78,
    "low_confidence_skill_score_cap": 70,
    "data_truncated_skill_score_cap": 60,
    "recent_copy_risk_high_skill_score_cap": 55,
    "recent_copy_risk_medium_skill_score_cap": 70,
    "weak_hit_rate_skill_score_cap": 75,
    "win_rate_padding_skill_score_cap": 60,
    "low_value_win_max_pnl": 1.0,
    "low_value_win_max_roi": 0.02,
    "low_value_win_max_pnl_for_low_roi": 10.0,
    "meaningful_win_min_pnl": 1.0,
    "meaningful_win_min_roi": 0.02,
    "near_certain_entry_price": 0.95,
    "win_rate_padding_min_wins": 8,
    "win_rate_padding_min_tiny_win_ratio": 0.50,
    "win_rate_padding_max_profit_share": 0.05,
    "win_rate_padding_min_quality_gap": 0.25,
    "metric_gaming_skill_score_cap": 65,
    "small_bet_max_buy_notional": 25.0,
    "small_bet_high_roi_min": 0.50,
    "small_bet_high_roi_min_count": 5,
    "small_bet_high_roi_min_ratio": 0.30,
    "small_bet_profit_share_max": 0.10,
    "roi_padding_unweighted_to_weighted_ratio": 5.0,
    "correlated_cluster_min_markets": 5,
    "correlated_cluster_profit_share": 0.40,
    "market_to_effective_bets_ratio": 2.0,
    "tail_risk_min_win_rate": 0.70,
    "tail_risk_loss_to_median_win": 20.0,
    "tail_risk_min_loss": 100.0,
    "unrealized_dominance_ratio": 0.50,
    "reward_dependency_ratio": 0.20,
    "copy_score_high_risk_cap": 45,
    "copy_score_medium_risk_cap": 65,
    "copy_score_low_confidence_cap": 60,
    "copy_score_insufficient_data_cap": 55,
    "copy_score_one_hit_cap": 65,
    "copy_score_unprofitable_cap": 30,
    "recent_trade_warning_window": 50,
    "recent_trade_warning_min_marked": 20,
    "recent_trade_warning_roi": 0.0,
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
    low_confidence_resolved_records: list[dict[str, Any]]
    resolver_stats: dict[str, int | bool]


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
    token_resolver: TokenResolver | None = None,
) -> dict[str, Any]:
    config = {**DEFAULT_SKILL_CONFIG, **(skill_config or {})}
    grouped = group_records_by_market(wallet_data, token_resolver=token_resolver)
    markets = [analyze_market(market_id, bucket) for market_id, bucket in grouped.markets.items()]
    markets.sort(key=lambda row: float(row["trading_pnl"]), reverse=True)

    warnings = build_warnings(markets, grouped.unmapped_records, wallet_data, max_records, config)
    category_breakdown = build_category_breakdown(markets, config)
    summary = summarize_results(
        markets, wallet_data, grouped.unmapped_records, warnings, max_records, config, grouped.resolver_stats
    )
    summary["verdict"] = final_verdict(summary, category_breakdown, config)
    warnings = summary["warnings"]
    summary["skill_verdict"] = summary["verdict"]
    summary["skill_verdict_label"] = verdict_label(summary["verdict"])
    summary["category_skill_summary"] = category_skill_sentence(category_breakdown)
    summary["is_probably_skilled"] = summary["verdict"] == "skilled"

    skill = build_skill_report(markets, summary, category_breakdown, config)

    return {
        "wallet": wallet_data.wallet,
        "summary": summary,
        "category_breakdown": category_breakdown,
        "top_winning_markets": [compact_market(row) for row in markets if row["trading_pnl"] > 0][:10],
        "top_losing_markets": [
            compact_market(row) for row in sorted(markets, key=lambda item: item["trading_pnl"]) if row_pnl(row) < 0
        ][:10],
        "markets": markets,
        "market_level_pnl": [compact_market(row) for row in markets],
        "outcome_level_edge": flatten_outcome_edges(markets),
        "unmapped_records_count": len(grouped.unmapped_records),
        "unmapped_records": grouped.unmapped_records[:100],
        "low_confidence_resolved_records": grouped.low_confidence_resolved_records[:100],
        "warnings": warnings,
        "raw_counts": wallet_data.counts,
        "skill": skill,
    }


def group_by_market(wallet_data: WalletData) -> dict[str, MarketBucket]:
    return group_records_by_market(wallet_data).markets


def group_records_by_market(wallet_data: WalletData, token_resolver: TokenResolver | None = None) -> GroupedRecords:
    token_condition_map = build_token_condition_map(wallet_data)
    grouped: dict[str, MarketBucket] = defaultdict(new_market_bucket)
    unmapped: list[dict[str, Any]] = []
    low_confidence_resolved: list[dict[str, Any]] = []
    resolver_enabled = bool(token_resolver and token_resolver.enabled)
    resolver_stats: dict[str, int | bool] = {
        "resolved_from_token_count": 0,
        "resolved_from_token_high_confidence_count": 0,
        "low_confidence_resolved_count": 0,
        "token_resolver_enabled": resolver_enabled,
        "token_resolver_cache_hits": 0,
        "token_resolver_api_calls": 0,
        "token_resolver_failures": 0,
    }

    for source_name in BUCKET_SOURCES:
        for record in getattr(wallet_data, source_name):
            market_key, market_key_source = get_market_key_with_source(record, token_condition_map)
            if market_key:
                grouped[market_key][source_name].append({**record, "market_key_source": market_key_source})
                continue

            resolved = token_resolver.resolve_record(record) if resolver_enabled and token_resolver else None
            if resolved and resolved.get("conditionId") and resolved.get("resolver_confidence") == "high":
                resolved_record = {
                    **record,
                    "conditionId": str(resolved["conditionId"]),
                    "resolved_marketId": resolved.get("marketId"),
                    "resolved_slug": resolved.get("slug"),
                    "resolved_question": resolved.get("question"),
                    "resolved_outcome": resolved.get("outcome"),
                    "market_key_source": "resolved_from_token",
                    "resolver_confidence": resolved["resolver_confidence"],
                    "resolver_source": resolved.get("source"),
                }
                grouped[str(resolved["conditionId"])][source_name].append(resolved_record)
                resolver_stats["resolved_from_token_count"] = int(resolver_stats["resolved_from_token_count"]) + 1
                resolver_stats["resolved_from_token_high_confidence_count"] = (
                    int(resolver_stats["resolved_from_token_high_confidence_count"]) + 1
                )
            elif resolved and resolved.get("conditionId"):
                resolver_stats["low_confidence_resolved_count"] = (
                    int(resolver_stats["low_confidence_resolved_count"]) + 1
                )
                low_confidence = unmapped_record(source_name, record)
                low_confidence["resolved_conditionId"] = resolved.get("conditionId")
                low_confidence["resolver_confidence"] = resolved.get("resolver_confidence")
                low_confidence["resolver_source"] = resolved.get("source")
                low_confidence_resolved.append(low_confidence)
                unmapped.append(low_confidence)
            else:
                unmapped.append(unmapped_record(source_name, record))

    if token_resolver:
        resolver_stats.update(token_resolver.stats())

    return GroupedRecords(
        markets=dict(grouped),
        unmapped_records=unmapped,
        low_confidence_resolved_records=low_confidence_resolved,
        resolver_stats=resolver_stats,
    )


def new_market_bucket() -> MarketBucket:
    return {"trades": [], "positions": [], "closed_positions": [], "activity": []}


def analyze_market(market_id: str, bucket: MarketBucket) -> dict[str, Any]:
    trades = bucket["trades"]
    positions = bucket["positions"]
    closed_positions = bucket["closed_positions"]
    activity = bucket["activity"]
    all_records = positions + closed_positions + trades + activity

    ledger = reconstruct_ledger(trades, activity)
    rewards_pnl = sum(
        num(record, "usdcSize", "usdc_size", "amount")
        for record in activity
        if activity_type(record) in REWARD_ACTIVITY_TYPES
    )
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
        "market_key_source": first_text(all_records, "market_key_source") or "unknown",
        "resolver_confidence": first_text(all_records, "resolver_confidence") or "",
        "resolver_source": first_text(all_records, "resolver_source") or "",
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
    resolver_stats: dict[str, int | bool] | None = None,
) -> dict[str, Any]:
    config = {**DEFAULT_SKILL_CONFIG, **(skill_config or {})}
    summary = aggregate_market_metrics(results, config)
    raw_record_count = sum(wallet_data.counts.values())
    unmapped_count = len(unmapped_records)
    fetch_warnings = list(getattr(wallet_data, "fetch_warnings", ()))
    data_truncated = bool(max_records and any(count >= max_records for count in wallet_data.counts.values())) or bool(fetch_warnings)

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
    summary.update(default_resolver_stats() | (resolver_stats or {}))
    summary.update(recent_performance_metrics(results, wallet_data))
    add_recent_performance_warnings(summary, config)
    if low_sample_one_hit_pattern(summary, config):
        summary["warnings"].append(
            "low_sample_one_hit_pattern_detected: top market concentration/ROI ex-top1 looks one-hit-like, "
            "but sample size is too small for a lucky verdict."
        )
    add_metric_gaming_flags(summary, config)
    summary["confidence_level"] = confidence_level(summary, config)
    summary["skill_confidence"] = summary["confidence_level"]
    return add_legacy_summary_aliases(summary)


def default_resolver_stats() -> dict[str, int | bool]:
    return {
        "resolved_from_token_count": 0,
        "resolved_from_token_high_confidence_count": 0,
        "low_confidence_resolved_count": 0,
        "token_resolver_enabled": False,
        "token_resolver_cache_hits": 0,
        "token_resolver_api_calls": 0,
        "token_resolver_failures": 0,
    }


def recent_performance_metrics(results: list[dict[str, Any]], wallet_data: WalletData) -> dict[str, Any]:
    mark_price_lookup = build_mark_price_lookup(wallet_data)
    market_windows = recent_market_windows(results)
    trade_windows = recent_trade_windows(wallet_data, mark_price_lookup=mark_price_lookup)
    buy_trade_windows = recent_trade_windows(wallet_data, side_filter="BUY", mark_price_lookup=mark_price_lookup)
    trade_3d = recent_trade_time_window(wallet_data, days=3, mark_price_lookup=mark_price_lookup)
    buy_trade_3d = recent_trade_time_window(wallet_data, days=3, side_filter="BUY", mark_price_lookup=mark_price_lookup)
    market_3d = recent_market_time_window(results, days=3)
    trade_frequency = recent_trade_frequency(wallet_data, days=7)
    copy_risk_level, copy_risk_reason = recent_copy_risk(
        market_windows,
        trade_windows,
        buy_trade_windows,
        trade_3d,
        buy_trade_3d,
        market_3d,
    )
    trade_50 = window_by_size(trade_windows, 50)
    buy_trade_50 = window_by_size(buy_trade_windows, 50)
    market_50 = window_by_size(market_windows, 50)
    return {
        "recent_market_windows": market_windows,
        "recent_trade_windows": trade_windows,
        "recent_buy_trade_windows": buy_trade_windows,
        "recent_trade_3d": trade_3d,
        "recent_buy_trade_3d": buy_trade_3d,
        "recent_market_3d": market_3d,
        "recent_3d_trade_count": trade_3d["trade_count"],
        "recent_3d_buy_count": trade_3d["buy_count"],
        "recent_3d_sell_count": trade_3d["sell_count"],
        "recent_3d_trade_notional": trade_3d["trade_notional"],
        "recent_3d_estimated_pnl": trade_3d["estimated_pnl"],
        "recent_3d_roi": trade_3d["roi_mark_to_market"],
        "recent_3d_marked_count": trade_3d["marked_count"],
        "recent_3d_avg_trades_per_day": trade_3d["avg_trades_per_day"],
        "recent_3d_frequency_label": trade_3d["frequency_label"],
        "recent_3d_buy_trade_count": buy_trade_3d["trade_count"],
        "recent_3d_buy_estimated_pnl": buy_trade_3d["estimated_pnl"],
        "recent_3d_buy_roi": buy_trade_3d["roi_mark_to_market"],
        "recent_3d_buy_marked_count": buy_trade_3d["marked_count"],
        "recent_3d_market_count": market_3d["market_count"],
        "recent_3d_market_pnl": market_3d["trading_pnl"],
        "recent_3d_market_roi_buy_notional": market_3d["roi_buy_notional"],
        "recent_trade_frequency": trade_frequency,
        "recent_7d_trade_count": trade_frequency["trade_count"],
        "recent_7d_buy_count": trade_frequency["buy_count"],
        "recent_7d_sell_count": trade_frequency["sell_count"],
        "recent_7d_trade_notional": trade_frequency["trade_notional"],
        "recent_7d_avg_trades_per_day": trade_frequency["avg_trades_per_day"],
        "recent_7d_trades_per_active_day": trade_frequency["trades_per_active_day"],
        "recent_7d_active_days": trade_frequency["active_days"],
        "recent_7d_frequency_label": trade_frequency["frequency_label"],
        "recent_trade_50_estimated_pnl": trade_50.get("estimated_pnl", 0.0),
        "recent_trade_50_roi": trade_50.get("roi_mark_to_market", 0.0),
        "recent_trade_50_marked_count": trade_50.get("marked_count", 0),
        "recent_trade_50_losing_rate": trade_50.get("losing_rate", 0.0),
        "recent_buy_trade_50_estimated_pnl": buy_trade_50.get("estimated_pnl", 0.0),
        "recent_buy_trade_50_roi": buy_trade_50.get("roi_mark_to_market", 0.0),
        "recent_buy_trade_50_marked_count": buy_trade_50.get("marked_count", 0),
        "recent_buy_trade_50_losing_rate": buy_trade_50.get("losing_rate", 0.0),
        "recent_market_50_pnl": market_50.get("trading_pnl", 0.0),
        "recent_market_50_roi_buy_notional": market_50.get("roi_buy_notional", 0.0),
        "recent_copy_risk_level": copy_risk_level,
        "recent_copy_risk_reason": copy_risk_reason,
    }


def recent_trade_frequency(wallet_data: WalletData, days: int = 7) -> dict[str, Any]:
    trades = [trade for trade in wallet_trade_records(wallet_data) if maybe_num(trade, "timestamp") is not None]
    if not trades:
        return {
            "window_days": days,
            "reference_timestamp": None,
            "window_start_timestamp": None,
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "trade_notional": 0.0,
            "avg_trades_per_day": 0.0,
            "active_days": 0,
            "trades_per_active_day": 0.0,
            "frequency_label": "none",
        }

    reference_timestamp = max(num(trade, "timestamp") for trade in trades)
    window_start = reference_timestamp - days * SECONDS_PER_DAY
    recent_trades = [trade for trade in trades if num(trade, "timestamp") >= window_start]
    buy_count = sum(1 for trade in recent_trades if text(trade, "side").upper() == "BUY")
    sell_count = sum(1 for trade in recent_trades if text(trade, "side").upper() == "SELL")
    active_days = len({datetime.fromtimestamp(num(trade, "timestamp"), tz=timezone.utc).strftime("%Y-%m-%d") for trade in recent_trades})
    avg_trades_per_day = safe_div_zero(float(len(recent_trades)), float(days))
    return {
        "window_days": days,
        "reference_timestamp": reference_timestamp,
        "window_start_timestamp": window_start,
        "trade_count": len(recent_trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "trade_notional": sum(trade_notional(trade) for trade in recent_trades),
        "avg_trades_per_day": avg_trades_per_day,
        "active_days": active_days,
        "trades_per_active_day": safe_div_zero(float(len(recent_trades)), float(active_days)),
        "frequency_label": trade_frequency_label(avg_trades_per_day),
    }


def recent_trade_time_window(
    wallet_data: WalletData,
    days: int,
    side_filter: str | None = None,
    mark_price_lookup: dict[str, float] | None = None,
) -> dict[str, Any]:
    trades = [trade for trade in wallet_trade_records(wallet_data) if maybe_num(trade, "timestamp") is not None]
    if side_filter:
        trades = [trade for trade in trades if text(trade, "side").upper() == side_filter]
    if not trades:
        return empty_recent_trade_time_window(days)

    reference_timestamp = max(num(trade, "timestamp") for trade in trades)
    window_start = reference_timestamp - days * SECONDS_PER_DAY
    recent_trades = [trade for trade in trades if num(trade, "timestamp") >= window_start]
    estimates = [estimate_trade_mark_to_market(trade, wallet_data, mark_price_lookup) for trade in recent_trades]
    marked_rows = [row for row in estimates if row["estimated_pnl"] is not None]
    estimated_pnl = sum(float(row["estimated_pnl"] or 0.0) for row in marked_rows)
    marked_notional = sum(float(row["notional"] or 0.0) for row in marked_rows)
    active_days = len({datetime.fromtimestamp(num(trade, "timestamp"), tz=timezone.utc).strftime("%Y-%m-%d") for trade in recent_trades})
    avg_trades_per_day = safe_div_zero(float(len(recent_trades)), float(days))
    buy_count = sum(1 for trade in recent_trades if text(trade, "side").upper() == "BUY")
    sell_count = sum(1 for trade in recent_trades if text(trade, "side").upper() == "SELL")
    return {
        "window_days": days,
        "reference_timestamp": reference_timestamp,
        "window_start_timestamp": window_start,
        "trade_count": len(recent_trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "trade_notional": sum(trade_notional(trade) for trade in recent_trades),
        "estimated_pnl": estimated_pnl,
        "marked_count": len(marked_rows),
        "unknown_count": len(estimates) - len(marked_rows),
        "marked_notional": marked_notional,
        "roi_mark_to_market": safe_div_zero(estimated_pnl, marked_notional),
        "losing_trades": sum(1 for row in marked_rows if float(row["estimated_pnl"] or 0.0) < 0),
        "losing_rate": safe_div(sum(1 for row in marked_rows if float(row["estimated_pnl"] or 0.0) < 0), len(marked_rows)) or 0.0,
        "active_days": active_days,
        "avg_trades_per_day": avg_trades_per_day,
        "trades_per_active_day": safe_div_zero(float(len(recent_trades)), float(active_days)),
        "frequency_label": trade_frequency_label(avg_trades_per_day),
        "side_filter": side_filter or "ALL",
        "is_estimated": True,
    }


def empty_recent_trade_time_window(days: int) -> dict[str, Any]:
    return {
        "window_days": days,
        "reference_timestamp": None,
        "window_start_timestamp": None,
        "trade_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "trade_notional": 0.0,
        "estimated_pnl": 0.0,
        "marked_count": 0,
        "unknown_count": 0,
        "marked_notional": 0.0,
        "roi_mark_to_market": 0.0,
        "losing_trades": 0,
        "losing_rate": 0.0,
        "active_days": 0,
        "avg_trades_per_day": 0.0,
        "trades_per_active_day": 0.0,
        "frequency_label": "none",
        "side_filter": "ALL",
        "is_estimated": True,
    }


def trade_frequency_label(avg_trades_per_day: float) -> str:
    if avg_trades_per_day <= 0:
        return "none"
    if avg_trades_per_day >= 20:
        return "high"
    if avg_trades_per_day >= 5:
        return "medium"
    return "low"


def recent_market_windows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dated_rows = [row for row in results if row.get("timestamp") is not None]
    dated_rows.sort(key=lambda row: float(row.get("timestamp") or 0.0), reverse=True)
    windows = []
    for window_size in RECENT_WINDOWS:
        rows = dated_rows[:window_size]
        pnl = sum(row_pnl(row) for row in rows)
        buy_notional = sum(float(row.get("total_buy_notional") or 0.0) for row in rows)
        windows.append(
            {
                "window": window_size,
                "available_count": len(rows),
                "trading_pnl": pnl,
                "buy_notional": buy_notional,
                "roi_buy_notional": safe_div_zero(pnl, buy_notional),
                "win_rate": safe_div(sum(1 for row in rows if row_pnl(row) > 0), len(rows)) or 0.0,
                "is_estimated": False,
            }
        )
    return windows


def recent_market_time_window(results: list[dict[str, Any]], days: int) -> dict[str, Any]:
    dated_rows = [row for row in results if row.get("timestamp") is not None]
    if not dated_rows:
        return {
            "window_days": days,
            "reference_timestamp": None,
            "window_start_timestamp": None,
            "market_count": 0,
            "trading_pnl": 0.0,
            "buy_notional": 0.0,
            "roi_buy_notional": 0.0,
            "win_rate": 0.0,
            "is_estimated": False,
        }
    reference_timestamp = max(float(row.get("timestamp") or 0.0) for row in dated_rows)
    window_start = reference_timestamp - days * SECONDS_PER_DAY
    rows = [row for row in dated_rows if float(row.get("timestamp") or 0.0) >= window_start]
    pnl = sum(row_pnl(row) for row in rows)
    buy_notional = sum(float(row.get("total_buy_notional") or 0.0) for row in rows)
    return {
        "window_days": days,
        "reference_timestamp": reference_timestamp,
        "window_start_timestamp": window_start,
        "market_count": len(rows),
        "trading_pnl": pnl,
        "buy_notional": buy_notional,
        "roi_buy_notional": safe_div_zero(pnl, buy_notional),
        "win_rate": safe_div(sum(1 for row in rows if row_pnl(row) > 0), len(rows)) or 0.0,
        "is_estimated": False,
    }


def recent_trade_windows(
    wallet_data: WalletData,
    side_filter: str | None = None,
    mark_price_lookup: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    trades = [trade for trade in wallet_trade_records(wallet_data) if maybe_num(trade, "timestamp") is not None]
    if side_filter:
        trades = [trade for trade in trades if text(trade, "side").upper() == side_filter]
    trades.sort(key=lambda trade: num(trade, "timestamp"), reverse=True)
    max_window = max(RECENT_WINDOWS)
    estimates = [estimate_trade_mark_to_market(trade, wallet_data, mark_price_lookup) for trade in trades[:max_window]]
    windows = []
    for window_size in RECENT_WINDOWS:
        rows = estimates[:window_size]
        marked_rows = [row for row in rows if row["estimated_pnl"] is not None]
        estimated_pnl = sum(float(row["estimated_pnl"] or 0.0) for row in marked_rows)
        marked_notional = sum(float(row["notional"] or 0.0) for row in marked_rows)
        windows.append(
            {
                "window": window_size,
                "trade_count": len(rows),
                "marked_count": len(marked_rows),
                "unknown_count": len(rows) - len(marked_rows),
                "estimated_pnl": estimated_pnl,
                "marked_notional": marked_notional,
                "roi_mark_to_market": safe_div_zero(estimated_pnl, marked_notional),
                "losing_trades": sum(1 for row in marked_rows if float(row["estimated_pnl"] or 0.0) < 0),
                "losing_rate": safe_div(
                    sum(1 for row in marked_rows if float(row["estimated_pnl"] or 0.0) < 0), len(marked_rows)
                )
                or 0.0,
                "is_estimated": True,
            }
        )
    return windows


def wallet_trade_records(wallet_data: WalletData) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    for source_name, source_records in (("trades", wallet_data.trades), ("activity", wallet_data.activity)):
        for record in source_records:
            if source_name == "activity" and activity_type(record) != "TRADE":
                continue
            key = trade_dedupe_key(record)
            if key in seen:
                continue
            seen.add(key)
            records.append({**record, "recent_trade_source": source_name})
    return records


def trade_dedupe_key(record: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    return (
        text(record, "transactionHash", "transaction_hash", "hash"),
        explicit_market_key(record) or "",
        first_text([record], "asset", "token_id", "tokenId", "clobTokenId") or "",
        text(record, "side").upper(),
        str(maybe_num(record, "size") or ""),
        str(maybe_num(record, "price") or ""),
        str(maybe_num(record, "timestamp") or ""),
    )


def estimate_trade_mark_to_market(
    trade: dict[str, Any],
    wallet_data: WalletData,
    mark_price_lookup: dict[str, float] | None = None,
) -> dict[str, Any]:
    mark_price = outcome_mark_price(trade, wallet_data, mark_price_lookup)
    price = num(trade, "price")
    size = num(trade, "size")
    side = text(trade, "side").upper()
    notional = trade_notional(trade)
    estimated_pnl = None
    if mark_price is not None and size > 0 and price >= 0:
        if side == "SELL":
            estimated_pnl = (price - mark_price) * size
        else:
            estimated_pnl = (mark_price - price) * size
    return {
        "timestamp": maybe_num(trade, "timestamp"),
        "market_id": explicit_market_key(trade) or "",
        "asset": first_text([trade], "asset", "token_id", "tokenId", "clobTokenId") or "",
        "title": first_text([trade], "title", "question") or "",
        "side": side,
        "price": price,
        "size": size,
        "notional": notional,
        "mark_price": mark_price,
        "estimated_pnl": estimated_pnl,
    }

def outcome_mark_price(
    trade: dict[str, Any],
    wallet_data: WalletData,
    mark_price_lookup: dict[str, float] | None = None,
) -> float | None:
    outcome_key = get_outcome_key(trade)
    token = first_text([trade], "asset", "token_id", "tokenId", "clobTokenId")
    fallback_key = outcome_fallback_key(trade)
    if mark_price_lookup is not None:
        for key in mark_price_lookup_keys(outcome_key, token, fallback_key):
            if key in mark_price_lookup:
                return mark_price_lookup[key]
        return None

    for record in wallet_data.positions + wallet_data.closed_positions:
        record_mark = mark_price_from_record(record)
        if record_mark is None:
            continue
        if outcome_key and get_outcome_key(record) == outcome_key:
            return record_mark
        if token and token in token_candidates(record):
            return record_mark
        if fallback_key and outcome_fallback_key(record) == fallback_key:
            return record_mark
    return None

def build_mark_price_lookup(wallet_data: WalletData) -> dict[str, float]:
    lookup: dict[str, float] = {}
    for record in wallet_data.positions + wallet_data.closed_positions:
        record_mark = mark_price_from_record(record)
        if record_mark is None:
            continue
        outcome_key = get_outcome_key(record)
        fallback_key = outcome_fallback_key(record)
        for key in mark_price_lookup_keys(outcome_key, None, fallback_key):
            lookup.setdefault(key, record_mark)
        for token in token_candidates(record):
            lookup.setdefault(f"token:{token}", record_mark)
    return lookup

def mark_price_lookup_keys(outcome_key: str | None, token: str | None, fallback_key: str | None) -> list[str]:
    keys = []
    if outcome_key:
        keys.append(f"outcome:{outcome_key}")
    if token:
        keys.append(f"token:{token}")
    if fallback_key:
        keys.append(f"fallback:{fallback_key}")
    return keys

def outcome_fallback_key(record: dict[str, Any]) -> str | None:
    market_key = explicit_market_key(record)
    outcome_index = first_text([record], "outcomeIndex", "outcome_index")
    outcome = first_text([record], "outcome")
    if market_key and outcome_index:
        return f"{market_key}:outcome_index:{outcome_index}"
    if market_key and outcome:
        return f"{market_key}:outcome:{outcome.lower()}"
    return None


def mark_price_from_record(record: dict[str, Any]) -> float | None:
    mark_price = maybe_num(record, "curPrice", "cur_price")
    if mark_price is not None:
        return mark_price
    if maybe_num(record, "realizedPnl", "realized_pnl") is not None:
        return 1.0 if num(record, "realizedPnl", "realized_pnl") > 0 else 0.0
    return None


def recent_copy_risk(
    market_windows: list[dict[str, Any]],
    trade_windows: list[dict[str, Any]],
    buy_trade_windows: list[dict[str, Any]],
    trade_3d: dict[str, Any],
    buy_trade_3d: dict[str, Any],
    market_3d: dict[str, Any],
) -> tuple[str, str]:
    has_enough_3d = any(
        (
            int(buy_trade_3d.get("marked_count") or 0) >= 10,
            int(trade_3d.get("marked_count") or 0) >= 10,
            int(market_3d.get("market_count") or 0) >= 10,
        )
    )
    if int(buy_trade_3d.get("marked_count") or 0) >= 10 and float(buy_trade_3d.get("estimated_pnl") or 0.0) < 0:
        return "high", "3 ngày gần nhất của BUY trades đang lỗ theo mark-to-market estimate."
    if int(market_3d.get("market_count") or 0) >= 10 and float(market_3d.get("trading_pnl") or 0.0) < 0:
        return "high", "3 ngày gần nhất của market-level PnL đang âm."
    if int(trade_3d.get("marked_count") or 0) >= 10 and float(trade_3d.get("estimated_pnl") or 0.0) < 0:
        return "medium", "Tổng all trades trong 3 ngày gần nhất đang âm, nhưng BUY-only/market-level chưa âm."
    if has_enough_3d:
        return "low", "3 ngày gần nhất không cho thấy drawdown rõ ở BUY-only hoặc market-level."

    buy_trade_50 = window_by_size(buy_trade_windows, 50)
    buy_trade_25 = window_by_size(buy_trade_windows, 25)
    trade_50 = window_by_size(trade_windows, 50)
    market_50 = window_by_size(market_windows, 50)
    trade_25 = window_by_size(trade_windows, 25)
    marked_buy_50 = int(buy_trade_50.get("marked_count") or 0)
    if marked_buy_50 >= 20 and float(buy_trade_50.get("estimated_pnl") or 0.0) < 0:
        return "high", "50 BUY trade gần nhất đang lỗ theo mark-to-market estimate."
    if int(buy_trade_25.get("marked_count") or 0) >= 10 and float(buy_trade_25.get("estimated_pnl") or 0.0) < 0:
        return "medium", "25 BUY trade gần nhất đang lỗ theo mark-to-market estimate."
    marked_50 = int(trade_50.get("marked_count") or 0)
    if marked_50 >= 20 and float(trade_50.get("estimated_pnl") or 0.0) < 0:
        return "high", "50 trade gần nhất đang lỗ theo mark-to-market estimate."
    if int(market_50.get("available_count") or 0) >= 20 and float(market_50.get("trading_pnl") or 0.0) < 0:
        return "high", "50 market gần nhất đang có trading PnL âm."
    if int(trade_25.get("marked_count") or 0) >= 10 and float(trade_25.get("estimated_pnl") or 0.0) < 0:
        return "medium", "25 trade gần nhất đang lỗ theo mark-to-market estimate."
    if marked_buy_50 < 20 and marked_50 < 20 and int(market_50.get("available_count") or 0) < 20:
        return "unknown", "Không đủ dữ liệu gần đây để đánh giá copy risk."
    return "low", "Không thấy recent drawdown rõ trong các cửa sổ gần đây."


def window_by_size(windows: list[dict[str, Any]], window_size: int) -> dict[str, Any]:
    return next((row for row in windows if row.get("window") == window_size), {})


def add_recent_performance_warnings(summary: dict[str, Any], config: dict[str, float]) -> None:
    buy_trade_3d = summary.get("recent_buy_trade_3d", {})
    trade_3d = summary.get("recent_trade_3d", {})
    market_3d = summary.get("recent_market_3d", {})
    has_enough_3d = any(
        (
            int(buy_trade_3d.get("marked_count") or 0) >= 10,
            int(trade_3d.get("marked_count") or 0) >= 10,
            int(market_3d.get("market_count") or 0) >= 10,
        )
    )
    if int(buy_trade_3d.get("marked_count") or 0) >= 10 and float(buy_trade_3d.get("estimated_pnl") or 0.0) < 0:
        summary["warnings"].append(
            "recent_3d_buy_loss_detected: BUY trades in the last 3 days have negative estimated mark-to-market PnL."
        )
    if int(market_3d.get("market_count") or 0) >= 10 and float(market_3d.get("trading_pnl") or 0.0) < 0:
        summary["warnings"].append("recent_3d_market_loss_detected: market-level PnL in the last 3 days is negative.")
    if has_enough_3d:
        return

    window_size = int(config["recent_trade_warning_window"])
    trade_window = window_by_size(summary.get("recent_trade_windows", []), window_size)
    min_marked = int(config["recent_trade_warning_min_marked"])
    if int(trade_window.get("marked_count") or 0) >= min_marked and float(trade_window.get("estimated_pnl") or 0.0) < 0:
        summary["warnings"].append(
            f"recent_trade_window_loss_detected: last {window_size} trades have estimated mark-to-market PnL "
            f"{float(trade_window.get('estimated_pnl') or 0.0):,.2f}; copy-trading risk is elevated."
        )
    buy_trade_window = window_by_size(summary.get("recent_buy_trade_windows", []), window_size)
    if int(buy_trade_window.get("marked_count") or 0) >= min_marked and float(buy_trade_window.get("estimated_pnl") or 0.0) < 0:
        summary["warnings"].append(
            f"recent_buy_trade_window_loss_detected: last {window_size} BUY trades have estimated mark-to-market PnL "
            f"{float(buy_trade_window.get('estimated_pnl') or 0.0):,.2f}; copy-trading risk is elevated."
        )
    market_window = window_by_size(summary.get("recent_market_windows", []), window_size)
    if int(market_window.get("available_count") or 0) >= min_marked and float(market_window.get("trading_pnl") or 0.0) < 0:
        summary["warnings"].append(
            f"recent_market_window_loss_detected: last {window_size} timestamped markets have trading PnL "
            f"{float(market_window.get('trading_pnl') or 0.0):,.2f}."
        )



def add_metric_gaming_flags(summary: dict[str, Any], config: dict[str, float]) -> None:
    summary["recent_performance_divergence_suspected"] = recent_performance_divergence(summary)
    flag_specs = [
        (
            "win_rate_padding",
            summary.get("win_rate_padding_suspected"),
            "Raw win rate cao nhưng meaningful win rate thấp do nhiều low-value wins.",
        ),
        (
            "small_bet_roi_padding",
            summary.get("small_bet_roi_padding_suspected"),
            "ROI/mean ROI có thể bị làm đẹp bởi nhiều kèo nhỏ ROI cao nhưng PnL đóng góp thấp.",
        ),
        (
            "correlated_cluster",
            summary.get("correlated_cluster_suspected"),
            "Nhiều market có thể cùng một event/cụm correlated, làm số market nhìn đa dạng hơn thực tế.",
        ),
        (
            "tail_risk",
            summary.get("tail_risk_suspected"),
            "Mẫu hình thắng nhiều khoản nhỏ nhưng có loss lớn; giống chiến lược tail-risk/pennies in front of steamroller.",
        ),
        (
            "unrealized_pnl_dominance",
            summary.get("unrealized_pnl_dominance_suspected"),
            "PnL phụ thuộc nhiều vào unrealized/open positions, chưa phải lợi nhuận đã chốt.",
        ),
        (
            "reward_dependency",
            summary.get("reward_dependency_suspected"),
            "Lợi nhuận phụ thuộc nhiều vào reward/rebate, không nên trộn với trading skill.",
        ),
        (
            "recent_performance_divergence",
            summary.get("recent_performance_divergence_suspected"),
            "Long-term PnL tốt nhưng recent/copy risk đang xấu; không nên copy chỉ vì leaderboard.",
        ),
    ]
    flags = [{"name": name, "detail": detail} for name, active, detail in flag_specs if active]
    summary["metric_gaming_flags"] = flags
    summary["metric_gaming_flags_count"] = len(flags)
    if flags:
        names = ", ".join(flag["name"] for flag in flags)
        summary["warnings"].append(f"metric_gaming_flags_detected: {names}.")


def recent_performance_divergence(summary: dict[str, Any]) -> bool:
    if float(summary.get("trading_pnl") or 0.0) <= 0:
        return False
    if summary.get("recent_copy_risk_level") == "high":
        return True
    if int(summary.get("recent_3d_buy_marked_count") or 0) >= 10 and float(summary.get("recent_3d_buy_estimated_pnl") or 0.0) < 0:
        return True
    if int(summary.get("recent_buy_trade_50_marked_count") or 0) >= 20 and float(summary.get("recent_buy_trade_50_estimated_pnl") or 0.0) < 0:
        return True
    return False

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
    padding_metrics = win_rate_quality_metrics(results, config, gross_profit)

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

    roi_ex_top1_cost = safe_div_zero(trading_pnl - top1_pnl, cost_basis - top1_cost)
    roi_ex_top3_cost = safe_div_zero(trading_pnl - top3_pnl, cost_basis - top3_cost)
    roi_ex_top5_cost = safe_div_zero(trading_pnl - top5_pnl, cost_basis - top5_cost)
    roi_ex_top1_buy = safe_div_zero(trading_pnl - top1_pnl, buy_notional - top1_buy)
    roi_ex_top3_buy = safe_div_zero(trading_pnl - top3_pnl, buy_notional - top3_buy)
    roi_ex_top5_buy = safe_div_zero(trading_pnl - top5_pnl, buy_notional - top5_buy)

    top1_contribution_net = safe_div_zero(top1_pnl, trading_pnl) if trading_pnl > 0 and top1_pnl > 0 else 0.0
    top3_contribution_net = safe_div_zero(top3_pnl, trading_pnl) if trading_pnl > 0 and top3_pnl > 0 else 0.0
    top1_share_gross = safe_div_zero(top1_pnl, gross_profit) if gross_profit > 0 else 0.0
    top3_share_gross = safe_div_zero(top3_pnl, gross_profit) if gross_profit > 0 else 0.0

    is_one_hit = any(
        predicate
        for predicate in (
            top1_contribution_net > config["one_hit_top1_contribution"],
            roi_ex_top1_cost < 0,
            roi_ex_top1_buy < 0,
        )
    )
    is_top3_dependent = any(
        predicate
        for predicate in (
            top3_contribution_net > config["top3_dependency_contribution"],
            roi_ex_top3_cost < 0,
            roi_ex_top3_buy < 0,
        )
    )

    total_active_months = len(monthly_pnl)
    profitable_months = sum(1 for row in monthly_pnl if row["pnl"] > 0)
    cost_weighted_roi = safe_div(
        sum(float(row.get("roi_cost_basis") or 0.0) * float(row.get("cost_basis") or 0.0) for row in results),
        cost_basis,
    )
    gaming_metrics = metric_gaming_quality_metrics(
        results,
        config,
        trading_pnl=trading_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        unrealized_pnl=unrealized_pnl,
        rewards_pnl=rewards_pnl,
        roi_buy_notional=safe_div_zero(trading_pnl, buy_notional),
        mean_buy_roi_unweighted=mean(buy_roi_values) if buy_roi_values else 0.0,
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
        **padding_metrics,
        "total_cost_basis": cost_basis,
        "cost_basis": cost_basis,
        "total_buy_notional": buy_notional,
        "total_max_capital_at_risk": max_capital_at_risk,
        "max_capital_at_risk": max_capital_at_risk,
        "max_capital_at_risk_estimated": any(row.get("max_capital_at_risk_estimated") for row in results),
        "total_current_value": current_value,
        "roi_cost_basis": safe_div_zero(trading_pnl, cost_basis),
        "roi_buy_notional": safe_div_zero(trading_pnl, buy_notional),
        "roi_max_capital_at_risk": safe_div_zero(trading_pnl, max_capital_at_risk),
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
        "median_market_roi": median(roi_values) if roi_values else 0.0,
        "mean_market_roi_unweighted": mean(roi_values) if roi_values else 0.0,
        "mean_market_roi_cost_weighted": cost_weighted_roi or 0.0,
        "mean_market_roi_buy_notional_unweighted": mean(buy_roi_values) if buy_roi_values else 0.0,
        "profit_factor": safe_div_zero(gross_profit, abs(gross_loss))
        if gross_loss < 0
        else (0.0 if gross_profit == 0 else float("inf")),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "hhi_profit_concentration": hhi(positive_profits) or 0.0,
        "gini_profit_concentration": gini(positive_profits) or 0.0,
        "effective_bets": effective_bets(results),
        "max_drawdown": max_drawdown(monthly_pnl),
        "profitable_months_count": profitable_months,
        "total_active_months": total_active_months,
        "monthly_pnl": monthly_pnl,
        "is_one_hit_wonder": is_one_hit,
        "is_top3_dependent": is_top3_dependent,
        **gaming_metrics,
        "top_market_title": positive_rows[0]["title"] if positive_rows else "",
        "top_market_pnl": top1_pnl,
        "outcome_level_edge": aggregate_outcome_edges(results),
    }



def win_rate_quality_metrics(
    results: list[dict[str, Any]],
    config: dict[str, float],
    gross_profit: float,
) -> dict[str, Any]:
    total_markets = len(results)
    winning_rows = [row for row in results if row_pnl(row) > 0]
    low_value_wins = [row for row in winning_rows if is_low_value_winning_market(row, config)]
    meaningful_wins = [row for row in results if is_meaningful_winning_market(row, config)]
    near_certain_wins = [row for row in winning_rows if is_near_certain_winning_market(row, config)]

    low_value_win_count = len(low_value_wins)
    winning_count = len(winning_rows)
    raw_win_rate = safe_div(winning_count, total_markets) or 0.0
    meaningful_win_rate = safe_div(len(meaningful_wins), total_markets) or 0.0
    non_low_value_denominator = total_markets - low_value_win_count
    non_low_value_win_rate = safe_div(winning_count - low_value_win_count, non_low_value_denominator) or 0.0
    low_value_profit = sum(row_pnl(row) for row in low_value_wins)
    low_value_buy_notional = sum(float(row.get("total_buy_notional") or 0.0) for row in low_value_wins)
    low_value_ratio = safe_div(low_value_win_count, winning_count) or 0.0
    low_value_profit_share = safe_div_zero(low_value_profit, gross_profit) if gross_profit > 0 else 0.0
    quality_gap = max(0.0, raw_win_rate - meaningful_win_rate)
    suspected = all(
        (
            winning_count >= config["win_rate_padding_min_wins"],
            low_value_ratio >= config["win_rate_padding_min_tiny_win_ratio"],
            low_value_profit_share <= config["win_rate_padding_max_profit_share"],
            quality_gap >= config["win_rate_padding_min_quality_gap"],
        )
    )
    severity = "none"
    if suspected:
        severity = "high" if low_value_ratio >= 0.70 and quality_gap >= 0.50 else "medium"

    return {
        "low_value_winning_markets": low_value_win_count,
        "low_value_winning_markets_ratio": low_value_ratio,
        "low_value_wins_profit": low_value_profit,
        "low_value_wins_profit_share": low_value_profit_share,
        "low_value_wins_buy_notional": low_value_buy_notional,
        "meaningful_winning_markets": len(meaningful_wins),
        "meaningful_market_win_rate": meaningful_win_rate,
        "non_low_value_market_win_rate": non_low_value_win_rate,
        "win_rate_quality_gap": quality_gap,
        "near_certain_winning_markets": len(near_certain_wins),
        "win_rate_padding_suspected": suspected,
        "low_value_win_padding_suspected": suspected,
        "win_rate_padding_severity": severity,
    }


def is_low_value_winning_market(row: dict[str, Any], config: dict[str, float]) -> bool:
    pnl = row_pnl(row)
    if pnl <= 0:
        return False
    roi_buy = row.get("roi_buy_notional")
    if pnl <= config["low_value_win_max_pnl"]:
        return True
    if roi_buy is not None and float(roi_buy) <= config["low_value_win_max_roi"]:
        return pnl <= config["low_value_win_max_pnl_for_low_roi"]
    return is_near_certain_winning_market(row, config) and pnl <= config["low_value_win_max_pnl_for_low_roi"]


def is_meaningful_winning_market(row: dict[str, Any], config: dict[str, float]) -> bool:
    pnl = row_pnl(row)
    if pnl <= 0:
        return False
    roi_buy = row.get("roi_buy_notional")
    if roi_buy is None:
        return pnl >= config["meaningful_win_min_pnl"]
    return pnl >= config["meaningful_win_min_pnl"] and float(roi_buy) >= config["meaningful_win_min_roi"]


def is_near_certain_winning_market(row: dict[str, Any], config: dict[str, float]) -> bool:
    if row_pnl(row) <= 0:
        return False
    avg_entry = row.get("avg_entry_price")
    return avg_entry is not None and float(avg_entry) >= config["near_certain_entry_price"]


def metric_gaming_quality_metrics(
    results: list[dict[str, Any]],
    config: dict[str, float],
    *,
    trading_pnl: float,
    gross_profit: float,
    gross_loss: float,
    unrealized_pnl: float,
    rewards_pnl: float,
    roi_buy_notional: float,
    mean_buy_roi_unweighted: float,
) -> dict[str, Any]:
    winners = [row for row in results if row_pnl(row) > 0]
    small_high_roi_wins = [row for row in winners if is_small_bet_high_roi_win(row, config)]
    small_high_roi_profit = sum(row_pnl(row) for row in small_high_roi_wins)
    small_high_roi_ratio = safe_div(len(small_high_roi_wins), len(winners)) or 0.0
    small_high_roi_profit_share = safe_div_zero(small_high_roi_profit, gross_profit) if gross_profit > 0 else 0.0
    roi_inflation_ratio = safe_div_zero(mean_buy_roi_unweighted, abs(roi_buy_notional)) if abs(roi_buy_notional) > 0 else 0.0
    small_bet_roi_padding = all(
        (
            len(small_high_roi_wins) >= config["small_bet_high_roi_min_count"],
            small_high_roi_ratio >= config["small_bet_high_roi_min_ratio"],
            small_high_roi_profit_share <= config["small_bet_profit_share_max"],
        )
    ) or (
        mean_buy_roi_unweighted > 0
        and roi_buy_notional > 0
        and roi_inflation_ratio >= config["roi_padding_unweighted_to_weighted_ratio"]
        and small_high_roi_ratio >= config["small_bet_high_roi_min_ratio"] / 2
    )

    cluster_metrics = correlated_cluster_metrics(results, config, gross_profit)
    tail_metrics = tail_risk_metrics(results, config, gross_profit, gross_loss)
    unrealized_dependency = (
        trading_pnl > 0 and abs(unrealized_pnl) > abs(trading_pnl) * config["unrealized_dominance_ratio"]
    )
    reward_dependency = rewards_pnl > 0 and safe_div_zero(rewards_pnl, abs(trading_pnl) + abs(rewards_pnl)) >= config[
        "reward_dependency_ratio"
    ]

    return {
        "small_bet_high_roi_wins": len(small_high_roi_wins),
        "small_bet_high_roi_wins_ratio": small_high_roi_ratio,
        "small_bet_high_roi_profit": small_high_roi_profit,
        "small_bet_high_roi_profit_share": small_high_roi_profit_share,
        "roi_inflation_ratio_unweighted_vs_weighted": roi_inflation_ratio,
        "small_bet_roi_padding_suspected": small_bet_roi_padding,
        "roi_padding_suspected": small_bet_roi_padding,
        "unrealized_pnl_dominance_suspected": unrealized_dependency,
        "reward_dependency_suspected": reward_dependency,
        **cluster_metrics,
        **tail_metrics,
    }


def is_small_bet_high_roi_win(row: dict[str, Any], config: dict[str, float]) -> bool:
    if row_pnl(row) <= 0:
        return False
    buy_notional = float(row.get("total_buy_notional") or row.get("buy_notional") or 0.0)
    roi_buy = row.get("roi_buy_notional")
    return (
        buy_notional > 0
        and buy_notional <= config["small_bet_max_buy_notional"]
        and roi_buy is not None
        and float(roi_buy) >= config["small_bet_high_roi_min"]
    )


def correlated_cluster_metrics(results: list[dict[str, Any]], config: dict[str, float], gross_profit: float) -> dict[str, Any]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        key = str(row.get("event_slug") or row.get("market_id") or "")
        if key:
            clusters[key].append(row)
    if not clusters:
        return {
            "market_to_effective_bets_ratio": 0.0,
            "top_event_cluster_key": "",
            "top_event_cluster_markets": 0,
            "top_event_cluster_pnl": 0.0,
            "top_event_cluster_profit_share": 0.0,
            "correlated_cluster_suspected": False,
        }

    top_key, top_rows = max(clusters.items(), key=lambda item: (sum(max(row_pnl(row), 0.0) for row in item[1]), len(item[1])))
    top_cluster_pnl = sum(row_pnl(row) for row in top_rows)
    top_cluster_profit = sum(max(row_pnl(row), 0.0) for row in top_rows)
    top_profit_share = safe_div_zero(top_cluster_profit, gross_profit) if gross_profit > 0 else 0.0
    effective_count = len(clusters)
    market_to_effective_ratio = safe_div_zero(float(len(results)), float(effective_count))
    suspected = (
        len(top_rows) >= config["correlated_cluster_min_markets"]
        and top_profit_share >= config["correlated_cluster_profit_share"]
    ) or (market_to_effective_ratio >= config["market_to_effective_bets_ratio"] and len(results) >= 20)
    return {
        "market_to_effective_bets_ratio": market_to_effective_ratio,
        "top_event_cluster_key": top_key,
        "top_event_cluster_markets": len(top_rows),
        "top_event_cluster_pnl": top_cluster_pnl,
        "top_event_cluster_profit_share": top_profit_share,
        "correlated_cluster_suspected": suspected,
    }


def tail_risk_metrics(
    results: list[dict[str, Any]], config: dict[str, float], gross_profit: float, gross_loss: float
) -> dict[str, Any]:
    winning_pnls = [row_pnl(row) for row in results if row_pnl(row) > 0]
    losing_pnls = [row_pnl(row) for row in results if row_pnl(row) < 0]
    median_winning_pnl = median(winning_pnls) if winning_pnls else 0.0
    largest_loss_abs = abs(min(losing_pnls)) if losing_pnls else 0.0
    loss_to_median_win = safe_div_zero(largest_loss_abs, median_winning_pnl) if median_winning_pnl > 0 else 0.0
    win_rate = safe_div(len(winning_pnls), len(results)) or 0.0
    loss_share_of_gross_profit = safe_div_zero(largest_loss_abs, gross_profit) if gross_profit > 0 else 0.0
    suspected = all(
        (
            win_rate >= config["tail_risk_min_win_rate"],
            largest_loss_abs >= config["tail_risk_min_loss"],
            loss_to_median_win >= config["tail_risk_loss_to_median_win"],
        )
    )
    return {
        "median_winning_pnl": median_winning_pnl,
        "largest_losing_market_loss": largest_loss_abs,
        "largest_loss_to_median_win": loss_to_median_win,
        "largest_loss_share_of_gross_profit": loss_share_of_gross_profit,
        "tail_risk_suspected": suspected,
        "pennies_in_front_of_steamroller_suspected": suspected,
    }

def add_legacy_summary_aliases(summary: dict[str, Any]) -> dict[str, Any]:
    summary["market_count"] = summary["total_markets"]
    summary["total_cost"] = summary["total_cost_basis"]
    summary["cost_basis"] = summary["total_cost_basis"]
    summary["max_capital_at_risk"] = summary["total_max_capital_at_risk"]
    summary["total_realized_pnl"] = summary["realized_pnl"]
    summary["total_unrealized_pnl"] = summary["unrealized_pnl"]
    summary["total_pnl"] = summary["trading_pnl"]
    summary["total_roi"] = summary["roi_cost_basis"]
    summary["roi_ex_top1"] = summary["roi_ex_top1_cost_basis"]
    summary["roi_ex_top3"] = summary["roi_ex_top3_cost_basis"]
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
                "meaningful_market_win_rate": metrics["meaningful_market_win_rate"],
                "low_value_winning_markets": metrics["low_value_winning_markets"],
                "low_value_winning_markets_ratio": metrics["low_value_winning_markets_ratio"],
                "low_value_wins_profit_share": metrics["low_value_wins_profit_share"],
                "win_rate_quality_gap": metrics["win_rate_quality_gap"],
                "win_rate_padding_suspected": metrics["win_rate_padding_suspected"],
                "win_rate_padding_severity": metrics["win_rate_padding_severity"],
                "small_bet_roi_padding_suspected": metrics["small_bet_roi_padding_suspected"],
                "small_bet_high_roi_wins": metrics["small_bet_high_roi_wins"],
                "correlated_cluster_suspected": metrics["correlated_cluster_suspected"],
                "top_event_cluster_markets": metrics["top_event_cluster_markets"],
                "top_event_cluster_profit_share": metrics["top_event_cluster_profit_share"],
                "tail_risk_suspected": metrics["tail_risk_suspected"],
                "largest_loss_to_median_win": metrics["largest_loss_to_median_win"],
                "median_market_roi": metrics["median_market_roi"],
                "roi_ex_top1": metrics["roi_ex_top1"],
                "roi_ex_top1_buy_notional": metrics["roi_ex_top1_buy_notional"],
                "top1_contribution": metrics["top1_contribution_net_pnl"],
                "top1_contribution_net_pnl": metrics["top1_contribution_net_pnl"],
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
    if metrics["win_rate_padding_suspected"]:
        return "inconclusive"
    if (
        (metrics["roi_buy_notional"] or 0.0) > config["skilled_roi_buy_notional"]
        and (metrics["roi_ex_top1_buy_notional"] or 0.0) > 0
        and metrics["meaningful_market_win_rate"] > config["skilled_market_win_rate"]
        and (metrics["median_market_roi"] or -1.0) > config["skilled_median_roi_floor"]
    ):
        return "skilled"
    return "inconclusive"


def final_verdict(summary: dict[str, Any], category_breakdown: list[dict[str, Any]], config: dict[str, float]) -> str:
    if summary["total_markets"] == 0:
        return "insufficient_data"
    if summary["total_markets"] < 10:
        return "insufficient_data"
    if summary["trading_pnl"] <= 0:
        return "unprofitable"
    if summary["confidence_level"] == "low" and summary["total_markets"] < config["skilled_min_markets"]:
        return "insufficient_data"
    if summary["unmapped_records_ratio"] > 0.25:
        return "insufficient_data"

    is_skilled = all(
        (
            summary["trading_pnl"] > 0,
            (summary["roi_buy_notional"] or 0.0) > config["skilled_roi_buy_notional"],
            summary["total_markets"] >= config["skilled_min_markets"],
            (summary["roi_ex_top1_buy_notional"] or -1.0) > 0,
            (summary["roi_ex_top3_buy_notional"] or -1.0) >= 0,
            summary["meaningful_market_win_rate"] > config["skilled_market_win_rate"],
            (summary["median_market_roi"] or -1.0) > config["skilled_median_roi_floor"],
            (summary["top1_contribution_net_pnl"] or 1.0) < config["skilled_top1_contribution_max"],
            not summary["win_rate_padding_suspected"],
            summary["confidence_level"] != "low",
        )
    )
    if is_skilled:
        return "skilled"
    if any(row["verdict"] == "skilled" and row["category"] != "Other" for row in category_breakdown):
        return "category_skilled"
    if (
        summary["roi_ex_top1_buy_notional"] < 0
        or summary["top1_contribution_net_pnl"] > config["one_hit_top1_contribution"]
        or summary["top3_contribution_net_pnl"] > config["top3_dependency_contribution"]
    ):
        return "lucky_or_one_hit_wonder"
    return "inconclusive"


def low_sample_one_hit_pattern(summary: dict[str, Any], config: dict[str, float]) -> bool:
    if summary["total_markets"] >= 10 or summary["trading_pnl"] <= 0:
        return False
    return (
        summary["roi_ex_top1_buy_notional"] < 0
        or summary["top1_contribution_net_pnl"] > config["one_hit_top1_contribution"]
        or summary["top3_contribution_net_pnl"] > config["top3_dependency_contribution"]
    )


def confidence_level(summary: dict[str, Any], config: dict[str, float]) -> str:
    if summary["data_truncated"]:
        return "low"
    if summary["total_markets"] < config["skilled_min_markets"]:
        return "low"
    if summary["unmapped_records_ratio"] > config["max_unmapped_ratio_medium"]:
        return "low"
    if unrealized_ratio(summary) > config["max_unrealized_pnl_ratio_medium"]:
        return "low"
    if summary["top1_contribution_net_pnl"] > config["low_confidence_top1_contribution"]:
        return "low"
    other_ratio = safe_div(summary.get("other_markets", 0.0), summary["total_markets"]) or 0.0
    if other_ratio > config["max_other_category_ratio_medium"]:
        return "low"
    if summary["warnings"]:
        return "medium"
    if summary["total_markets"] >= 50 and summary["effective_bets"] >= 50:
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
    warnings.extend(getattr(wallet_data, "fetch_warnings", ()))
    if max_records and any(count >= max_records for count in wallet_data.counts.values()):
        warnings.append("Dữ liệu có thể bị truncate vì ít nhất một endpoint chạm max_records.")
    if unmapped_records:
        warnings.append(
            f"Có {len(unmapped_records)} record chỉ có asset/token hoặc thiếu market key; đã loại khỏi kết luận skill."
        )
    if any(row.get("max_capital_at_risk_estimated") for row in results):
        warnings.append("max_capital_at_risk là best-effort cho market thiếu lịch sử trades đầy đủ.")
    if (
        sum(1 for row in results if row.get("category") == "Other") / max(1, len(results))
        > config["max_other_category_ratio_medium"]
    ):
        warnings.append("Nhiều market bị phân loại Other; kết luận theo category có thể yếu.")
    if results:
        wallet_metrics = aggregate_market_metrics(results, config)
        if wallet_metrics["win_rate_padding_suspected"]:
            warnings.append(
                "win_rate_padding_suspected: nhiều market thắng có PnL/ROI quá nhỏ, "
                "raw win rate có thể bị làm đẹp và không phản ánh edge thật."
            )
        padded_categories = []
        for category in CATEGORY_ORDER:
            category_rows = [row for row in results if row.get("category") == category]
            if not category_rows:
                continue
            category_metrics = aggregate_market_metrics(category_rows, config)
            if category_metrics["win_rate_padding_suspected"]:
                padded_categories.append(category)
        if padded_categories:
            warnings.append(
                "category_win_rate_padding_suspected: "
                + ", ".join(padded_categories)
                + " có nhiều low-value wins; không nên đọc raw category win rate như skill thật."
            )
    return warnings


def build_skill_report(
    markets: list[dict[str, Any]],
    summary: dict[str, Any],
    category_breakdown: list[dict[str, Any]],
    config: dict[str, float],
) -> dict[str, Any]:
    components = skill_components(summary)
    usable_components = [component for component in components if component["normalized"] is not None]
    total_weight = sum(float(component["weight"]) for component in usable_components)
    raw_skill_score = None
    if usable_components and summary["trading_pnl"] > 0:
        for component in usable_components:
            component["contribution"] = round(
                100.0 * float(component["normalized"]) * float(component["weight"]) / total_weight,
                1,
            )
        raw_skill_score = int(round(sum(float(component["contribution"]) for component in usable_components)))
    for component in components:
        component.setdefault("contribution", None)
    skill_score, score_adjustment = adjusted_skill_score(raw_skill_score, summary, config)
    copy_suitability = copy_suitability_report(summary, skill_score, config)

    return {
        "skill_score": skill_score,
        "raw_skill_score": raw_skill_score,
        "adjusted_skill_score": skill_score,
        "score_adjustment": score_adjustment,
        "copy_suitability_score": copy_suitability["score"],
        "copy_suitability_raw_score": copy_suitability["raw_score"],
        "copy_suitability_label": copy_suitability["label"],
        "copy_suitability_detail": copy_suitability["detail"],
        "copy_suitability_adjustment": copy_suitability["adjustment"],
        "copy_suitability_components": copy_suitability["components"],
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
            "top1_contribution": summary["top1_contribution_net_pnl"],
            "top3_contribution": summary["top3_contribution_net_pnl"],
            "roi_ex_top1": summary["roi_ex_top1"],
            "top1_contribution_net_pnl": summary["top1_contribution_net_pnl"],
            "top3_contribution_net_pnl": summary["top3_contribution_net_pnl"],
            "hhi": summary["hhi_profit_concentration"],
            "gini": summary["gini_profit_concentration"],
        },
        "significance": significance_metrics(markets, summary),
        "risk": risk_metrics(markets, summary),
    }


def significance_metrics(markets: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    roi_values = [float(row["roi_cost_basis"]) for row in markets if row.get("roi_cost_basis") is not None]
    n = len(roi_values)
    if n < 2:
        return {
            "n": n,
            "mean_roi": roi_values[0] if roi_values else None,
            "std_roi": None,
            "t_stat": None,
            "ci_low": None,
            "ci_high": None,
            "significant": False,
        }

    mean_roi = mean(roi_values)
    std_roi = stdev(roi_values)
    standard_error = std_roi / (n**0.5) if std_roi > 0 else 0.0
    t_stat = mean_roi / standard_error if standard_error > 0 else None
    ci_low, ci_high = bootstrap_mean_ci(roi_values)

    return {
        "n": n,
        "mean_roi": mean_roi,
        "std_roi": std_roi,
        "t_stat": t_stat,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "significant": ci_low is not None and ci_low > 0,
    }


def bootstrap_mean_ci(
    values: Sequence[float], resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None

    rng = random.Random(BOOTSTRAP_SEED)
    sample_means: list[float] = []
    for _ in range(resamples):
        sample_sum = 0.0
        for _ in values:
            sample_sum += values[rng.randrange(len(values))]
        sample_means.append(sample_sum / len(values))
    sample_means.sort()
    return percentile(sample_means, 2.5), percentile(sample_means, 97.5)


def percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]

    clamped_percent = clamp(percent, 0.0, 100.0)
    position = (len(values) - 1) * (clamped_percent / 100.0)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(values) - 1)
    fraction = position - lower_index
    return values[lower_index] * (1.0 - fraction) + values[upper_index] * fraction


def risk_metrics(markets: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    roi_values = [float(row["roi_cost_basis"]) for row in markets if row.get("roi_cost_basis") is not None]
    sharpe = None
    if len(roi_values) >= 2:
        spread = stdev(roi_values)
        if spread > 0:
            sharpe = mean(roi_values) / spread

    return {
        "sharpe": sharpe,
        "profit_factor": summary["profit_factor"],
        "gross_profit": summary["gross_profit"],
        "gross_loss": summary["gross_loss"],
        "max_drawdown": summary["max_drawdown"],
    }


def adjusted_skill_score(
    raw_score: int | None,
    summary: dict[str, Any],
    config: dict[str, float],
) -> tuple[int | None, dict[str, Any]]:
    if raw_score is None:
        return None, {
            "applied": False,
            "reason": None,
            "cap": None,
            "raw_score": None,
            "adjusted_score": None,
            "caps": [],
            "reasons": [],
        }

    caps = skill_score_caps(summary, config)
    adjusted_score = raw_score
    primary_cap: dict[str, Any] | None = None
    if caps:
        primary_cap = min(caps, key=lambda item: int(item["cap"]))
        adjusted_score = min(raw_score, int(primary_cap["cap"]))

    applied = adjusted_score < raw_score
    adjustment: dict[str, Any] = {
        "applied": applied,
        "reason": primary_cap["reason"] if applied and primary_cap else None,
        "cap": primary_cap["cap"] if applied and primary_cap else None,
        "raw_score": raw_score,
        "adjusted_score": adjusted_score,
        "caps": caps,
        "reasons": [str(item["reason"]) for item in caps],
    }
    return adjusted_score, adjustment

def skill_score_caps(summary: dict[str, Any], config: dict[str, float]) -> list[dict[str, Any]]:
    caps: list[dict[str, Any]] = []

    def add(reason: str, cap_key: str, detail: str) -> None:
        caps.append({"reason": reason, "cap": int(config[cap_key]), "detail": detail})

    verdict = str(summary.get("verdict") or "inconclusive")
    if verdict == "lucky_or_one_hit_wonder":
        add("one_hit_wonder_cap", "one_hit_skill_score_cap", "Lợi nhuận phụ thuộc quá nhiều vào top market/top3.")
    elif verdict == "insufficient_data" and any(
        "low_sample_one_hit_pattern_detected" in warning for warning in summary.get("warnings", [])
    ):
        add(
            "low_sample_one_hit_pattern_cap",
            "low_sample_one_hit_skill_score_cap",
            "Mẫu quá ít nhưng đã có dấu hiệu lợi nhuận tập trung kiểu one-hit.",
        )
    elif verdict == "insufficient_data":
        add("insufficient_data_cap", "insufficient_data_skill_score_cap", "Không đủ số market để kết luận skill chắc chắn.")
    elif verdict == "category_skilled":
        add(
            "category_skilled_cap",
            "category_skilled_skill_score_cap",
            "Chỉ thấy edge rõ ở một vài category, chưa phải skilled toàn ví.",
        )
    elif verdict == "inconclusive":
        add("inconclusive_cap", "inconclusive_skill_score_cap", "Các chỉ số chưa đủ đồng thuận để kết luận skilled.")

    if summary.get("data_truncated"):
        add("data_truncated_cap", "data_truncated_skill_score_cap", "Dữ liệu bị truncate nên điểm không nên quá cao.")
    if summary.get("confidence_level") == "low":
        add("low_confidence_cap", "low_confidence_skill_score_cap", "Độ tin cậy dữ liệu/thống kê thấp.")
    if summary.get("win_rate_padding_suspected"):
        add(
            "win_rate_padding_cap",
            "win_rate_padding_skill_score_cap",
            "Raw win rate có dấu hiệu bị làm đẹp bằng nhiều market thắng PnL/ROI rất nhỏ.",
        )
    elif int(summary.get("metric_gaming_flags_count") or 0) > 0:
        add(
            "metric_gaming_cap",
            "metric_gaming_skill_score_cap",
            "Có detector phát hiện pattern làm đẹp chỉ số hoặc rủi ro bị che khuất.",
        )

    copy_risk = summary.get("recent_copy_risk_level")
    if copy_risk == "high":
        add(
            "recent_copy_risk_high_cap",
            "recent_copy_risk_high_skill_score_cap",
            "Phong độ/copy risk gần đây đang xấu.",
        )
    elif copy_risk == "medium":
        add(
            "recent_copy_risk_medium_cap",
            "recent_copy_risk_medium_skill_score_cap",
            "Có cảnh báo copy risk gần đây nên giảm điểm hiển thị.",
        )

    if (summary.get("market_win_rate") or 0.0) < 0.50 and (summary.get("median_market_roi") or 0.0) < 0:
        add(
            "weak_hit_rate_cap",
            "weak_hit_rate_skill_score_cap",
            "Win rate dưới 50% và median ROI âm.",
        )
    return caps

def copy_suitability_report(
    summary: dict[str, Any],
    skill_score: int | None,
    config: dict[str, float],
) -> dict[str, Any]:
    components = copy_suitability_components(summary, skill_score)
    raw_score = int(round(sum(float(component["contribution"]) for component in components)))
    caps = copy_suitability_caps(summary, config)
    adjusted_score = raw_score
    primary_cap: dict[str, Any] | None = None
    if caps:
        primary_cap = min(caps, key=lambda item: int(item["cap"]))
        adjusted_score = min(raw_score, int(primary_cap["cap"]))
    adjustment = {
        "applied": adjusted_score < raw_score,
        "reason": primary_cap["reason"] if adjusted_score < raw_score and primary_cap else None,
        "cap": primary_cap["cap"] if adjusted_score < raw_score and primary_cap else None,
        "raw_score": raw_score,
        "adjusted_score": adjusted_score,
        "caps": caps,
        "reasons": [str(item["reason"]) for item in caps],
    }
    return {
        "score": adjusted_score,
        "raw_score": raw_score,
        "label": copy_suitability_label(adjusted_score),
        "detail": copy_suitability_detail(adjusted_score, summary),
        "adjustment": adjustment,
        "components": components,
    }

def copy_suitability_caps(summary: dict[str, Any], config: dict[str, float]) -> list[dict[str, Any]]:
    caps: list[dict[str, Any]] = []

    def add(reason: str, cap_key: str, detail: str) -> None:
        caps.append({"reason": reason, "cap": int(config[cap_key]), "detail": detail})

    verdict = str(summary.get("verdict") or "inconclusive")
    if verdict == "unprofitable":
        add("copy_unprofitable_cap", "copy_score_unprofitable_cap", "Wallet đang âm PnL tổng.")
    if verdict == "insufficient_data":
        add("copy_insufficient_data_cap", "copy_score_insufficient_data_cap", "Mẫu quá ít để copy tự tin.")
    if verdict == "lucky_or_one_hit_wonder":
        add("copy_one_hit_cap", "copy_score_one_hit_cap", "Lợi nhuận dài hạn phụ thuộc top market/top3.")
    if summary.get("confidence_level") == "low" or summary.get("data_truncated"):
        add("copy_low_confidence_cap", "copy_score_low_confidence_cap", "Dữ liệu/độ tin cậy thấp.")
    copy_risk = summary.get("recent_copy_risk_level")
    if copy_risk == "high":
        add("copy_recent_high_risk_cap", "copy_score_high_risk_cap", "Phong độ gần đây đang lỗ/rủi ro cao.")
    elif copy_risk == "medium":
        add("copy_recent_medium_risk_cap", "copy_score_medium_risk_cap", "Có cảnh báo phong độ gần đây.")
    return caps

def copy_suitability_components(summary: dict[str, Any], skill_score: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "label": "3 ngày BUY mark-to-market",
            "normalized": recent_roi_score(
                summary.get("recent_3d_buy_roi"),
                int(summary.get("recent_3d_buy_marked_count") or 0),
                min_count=10,
            ),
            "weight": 30,
            "detail": (
                f"BUY PnL {format_money(summary.get('recent_3d_buy_estimated_pnl'))}, "
                f"ROI {format_percent(summary.get('recent_3d_buy_roi'))}, "
                f"marked {int(summary.get('recent_3d_buy_marked_count') or 0)} trades."
            ),
        },
        {
            "label": "3 ngày market-level PnL",
            "normalized": recent_roi_score(
                summary.get("recent_3d_market_roi_buy_notional"),
                int(summary.get("recent_3d_market_count") or 0),
                min_count=5,
            ),
            "weight": 20,
            "detail": (
                f"Market PnL {format_money(summary.get('recent_3d_market_pnl'))}, "
                f"ROI {format_percent(summary.get('recent_3d_market_roi_buy_notional'))}, "
                f"{int(summary.get('recent_3d_market_count') or 0)} markets."
            ),
        },
        {
            "label": "Recent copy risk",
            "normalized": copy_risk_score(summary.get("recent_copy_risk_level")),
            "weight": 20,
            "detail": str(summary.get("recent_copy_risk_reason") or "Không có lý do copy risk."),
        },
        {
            "label": "Tần suất/liquidity gần đây",
            "normalized": frequency_score(summary),
            "weight": 10,
            "detail": (
                f"{int(summary.get('recent_3d_trade_count') or 0)} trades/3 ngày, "
                f"{float(summary.get('recent_3d_avg_trades_per_day') or 0.0):.1f} trades/ngày."
            ),
        },
        {
            "label": "Skill dài hạn đã điều chỉnh",
            "normalized": None if skill_score is None else clamp(float(skill_score) / 100.0, 0.0, 1.0),
            "weight": 10,
            "detail": f"Risk-adjusted skill score {skill_score}/100." if skill_score is not None else "Không có skill score.",
        },
        {
            "label": "Chất lượng dữ liệu",
            "normalized": data_quality_score(summary),
            "weight": 10,
            "detail": f"Confidence {summary.get('confidence_level')}, unmapped {int(summary.get('unmapped_records_count') or 0)} records.",
        },
    ]
    total_weight = sum(float(row["weight"]) for row in rows if row["normalized"] is not None)
    for row in rows:
        if row["normalized"] is None or total_weight <= 0:
            row["contribution"] = 0.0
        else:
            row["contribution"] = round(100.0 * float(row["normalized"]) * float(row["weight"]) / total_weight, 1)
    return rows

def recent_roi_score(value: Any, observed_count: int, min_count: int) -> float:
    if observed_count < min_count or value is None:
        return 0.35
    return clamp(0.50 + float(value) * 4.0, 0.0, 1.0)

def copy_risk_score(level: Any) -> float:
    return {"low": 1.0, "medium": 0.55, "high": 0.15, "unknown": 0.40}.get(str(level), 0.40)

def frequency_score(summary: dict[str, Any]) -> float:
    trades_per_day = float(summary.get("recent_3d_avg_trades_per_day") or 0.0)
    trade_count = float(summary.get("recent_3d_trade_count") or 0.0)
    return max(clamp(trades_per_day / 8.0, 0.0, 1.0), clamp(trade_count / 30.0, 0.0, 1.0) * 0.8)

def data_quality_score(summary: dict[str, Any]) -> float:
    score = {"high": 1.0, "medium": 0.75, "low": 0.35}.get(str(summary.get("confidence_level")), 0.50)
    if summary.get("data_truncated"):
        score = min(score, 0.25)
    unmapped_ratio = float(summary.get("unmapped_records_ratio") or 0.0)
    if unmapped_ratio > 0.10:
        score = min(score, 0.50)
    return score

def copy_suitability_label(score: int | None) -> str:
    if score is None:
        return "Không đủ dữ liệu"
    if score >= 75:
        return "Tốt để theo dõi/copy thận trọng"
    if score >= 55:
        return "Cần thận trọng"
    return "Rủi ro cao để copy ngay"

def copy_suitability_detail(score: int, summary: dict[str, Any]) -> str:
    risk = summary.get("recent_copy_risk_level", "unknown")
    if score >= 75:
        return f"Phong độ gần đây và skill dài hạn khá đồng thuận; copy risk hiện là {risk}."
    if score >= 55:
        return f"Có một số tín hiệu tốt nhưng vẫn có rủi ro gần đây/dữ liệu; copy risk hiện là {risk}."
    return f"Không nên copy mù lúc này; copy risk hiện là {risk} hoặc dữ liệu/score chưa đủ mạnh."

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
            "normalized": clamp(((summary["meaningful_market_win_rate"] or 0.0) - 0.45) / 0.25, 0.0, 1.0),
            "weight": 25,
            "detail": (
                f"Raw win rate {format_percent(summary['market_win_rate'])}, "
                f"meaningful win rate {format_percent(summary['meaningful_market_win_rate'])}, "
                f"low-value wins {summary['low_value_winning_markets']}."
            ),
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

    buy_trade_outcome_keys = {
        outcome_key
        for trade in trades
        if text(trade, "side").upper() == "BUY" and (outcome_key := get_outcome_key(trade))
    }

    for record in positions + closed_positions:
        outcome_key = get_outcome_key(record)
        if not outcome_key:
            continue
        group = grouped.setdefault(outcome_key, new_outcome_edge_group(market_id, record, outcome_key))
        if outcome_key not in buy_trade_outcome_keys:
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
    return {
        "market_id": market_id,
        "outcome_key": outcome_key,
        "shares": 0.0,
        "cost": 0.0,
        "records": [],
        "resolved": False,
    }


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
    edges = [
        edge for row in results for edge in row.get("outcome_level_edge", []) if edge.get("edge_per_share") is not None
    ]
    if not edges:
        return {
            "n_resolved": 0,
            "edge_per_share": None,
            "edge_per_share_weighted": None,
            "win_rate": None,
            "avg_entry_price": None,
        }
    total_shares = sum(float(edge.get("shares") or 0.0) for edge in edges)
    return {
        "n_resolved": len(edges),
        "edge_per_share": mean(float(edge["edge_per_share"]) for edge in edges),
        "edge_per_share_weighted": safe_div(
            sum(float(edge["edge_per_share"]) * float(edge.get("shares") or 0.0) for edge in edges), total_shares
        ),
        "win_rate": mean(float(edge["outcome_result"]) for edge in edges if edge.get("outcome_result") is not None),
        "avg_entry_price": safe_div(
            sum(float(edge["avg_entry_price"]) * float(edge.get("shares") or 0.0) for edge in edges), total_shares
        ),
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
    market_key, _source = get_market_key_with_source(record, token_condition_map)
    return market_key


def get_market_key_with_source(
    record: dict[str, Any], token_condition_map: dict[str, str] | None = None
) -> tuple[str | None, str | None]:
    explicit_key, explicit_source = explicit_market_key_with_source(record)
    if explicit_key:
        return explicit_key, explicit_source
    if token_condition_map:
        for token in token_candidates(record):
            if token in token_condition_map:
                return token_condition_map[token], "mapped_from_local_token_metadata"
    return None, None


def explicit_market_key(record: dict[str, Any]) -> str | None:
    market_key, _source = explicit_market_key_with_source(record)
    return market_key


def explicit_market_key_with_source(record: dict[str, Any]) -> tuple[str | None, str | None]:
    for path in (
        ("conditionId",),
        ("condition_id",),
        ("conditionID",),
        ("metadata", "conditionId"),
        ("metadata", "condition_id"),
        ("metadata", "conditionID"),
        ("market", "conditionId"),
        ("market", "condition_id"),
        ("market", "conditionID"),
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
            return str(value), market_key_source_for_path(path)
    return None, None


def market_key_source_for_path(path: tuple[str, ...]) -> str:
    field_name = path[-1]
    if field_name in {"conditionId", "condition_id", "conditionID"}:
        return "native_conditionId"
    if field_name in {"marketId", "market_id"}:
        return "native_marketId"
    return "native_slug"


def token_candidates(record: dict[str, Any]) -> list[str]:
    values = []
    for key in ("asset", "token_id", "tokenId", "clobTokenId", "clob_token_id", "oppositeAsset"):
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
    weather_patterns = (
        r"\bweather\b",
        r"\bclimate\b",
        r"\bhurricane\b",
        r"\btornado\b",
        r"\bstorm\b",
        r"\btemperature\b",
        r"highest temperature",
        r"lowest temperature",
        r"\bdegrees?\b",
        r"°\s*[cf]\b",
        r"\brain(?:fall)?\b",
        r"\bsnow(?:fall|storm)?\b",
        r"\bheat\s?wave\b",
    )
    if any(re.search(pattern, haystack) for pattern in weather_patterns):
        return "Weather"

    categories = {
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
            "olympic",
            "olympics",
            "gold medal",
            "snowboard",
            "rainbow six",
            "call of duty",
            "esports",
            "bo3",
            "game 1 winner",
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
        "Tech/AI": (
            "openai",
            "anthropic",
            "ai",
            "artificial intelligence",
            "nvidia",
            "apple",
            "google",
            "tesla",
            "spacex",
        ),
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
    positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    trading_pnl: float,
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
    keys = {
        str(row.get("event_slug") or row.get("market_id"))
        for row in results
        if row.get("event_slug") or row.get("market_id")
    }
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


def market_warnings(
    ledger: LedgerMetrics, positions: list[dict[str, Any]], closed_positions: list[dict[str, Any]]
) -> list[str]:
    warnings: list[str] = []
    position_realized = sum(num(record, "realizedPnl", "realized_pnl") for record in positions)
    closed_realized = sum(num(record, "realizedPnl", "realized_pnl") for record in closed_positions)
    if positions and closed_positions and position_realized != 0 and closed_realized != 0:
        warnings.append(
            "possible_overlap_realized_pnl: market has both open positions and closed positions with realizedPnl; "
            "analyzer avoided double-counting by choosing a single realized PnL source"
        )
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


def safe_div_zero(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def unrealized_ratio(summary: dict[str, Any]) -> float:
    total = abs(float(summary.get("trading_pnl") or 0.0))
    if total == 0:
        return 0.0
    return abs(float(summary.get("unrealized_pnl") or 0.0)) / total


def edge_score(edge: dict[str, Any]) -> float | None:
    value = (
        edge.get("edge_per_share_weighted")
        if edge.get("edge_per_share_weighted") is not None
        else edge.get("edge_per_share")
    )
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
