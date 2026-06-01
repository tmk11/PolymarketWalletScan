from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from .polymarket_api import WalletData
from .skill_score import compute_skill


MarketBucket = dict[str, list[dict[str, Any]]]


def analyze_wallet(wallet_data: WalletData, max_records: int | None = None) -> dict[str, Any]:
    markets = group_by_market(wallet_data)
    results = [analyze_market(condition_id, bucket) for condition_id, bucket in markets.items()]
    results.sort(key=lambda row: row["pnl"], reverse=True)
    summary = summarize_results(results, wallet_data)
    skill = compute_skill(results, wallet_data, summary, max_records=max_records)
    summary["skill_score"] = skill["skill_score"]
    summary["skill_verdict"] = skill["verdict"]
    summary["skill_verdict_label"] = skill["verdict_label"]
    summary["skill_confidence"] = skill["confidence"]
    summary["data_truncated"] = skill["data_truncated"]
    return {
        "summary": summary,
        "markets": results,
        "raw_counts": wallet_data.counts,
        "skill": skill,
    }


def group_by_market(wallet_data: WalletData) -> dict[str, MarketBucket]:
    grouped: dict[str, MarketBucket] = defaultdict(
        lambda: {"trades": [], "positions": [], "closed_positions": [], "activity": []}
    )

    for source_name in ("trades", "positions", "closed_positions", "activity"):
        for row in getattr(wallet_data, source_name):
            key = get_market_key(row)
            if key:
                grouped[key][source_name].append(row)

    return dict(grouped)


def analyze_market(condition_id: str, bucket: MarketBucket) -> dict[str, Any]:
    trades = bucket["trades"]
    positions = bucket["positions"]
    closed_positions = bucket["closed_positions"]
    activity = bucket["activity"]
    all_rows = positions + closed_positions + trades + activity

    reported_cost = sum(record_cost(row) for row in positions + closed_positions)
    buy_cost = sum(trade_notional(row) for row in trades if text(row, "side").upper() == "BUY")
    cost = reported_cost if reported_cost > 0 else buy_cost

    sell_proceeds = sum(trade_notional(row) for row in trades if text(row, "side").upper() == "SELL")
    redeem_proceeds = sum(num(row, "usdcSize", "usdc_size") for row in activity if text(row, "type").upper() == "REDEEM")
    closed_cost = sum(record_cost(row) for row in closed_positions)
    closed_realized = sum(num(row, "realizedPnl", "realized_pnl") for row in closed_positions)
    inferred_closed_proceeds = max(0.0, closed_cost + closed_realized) if closed_cost else 0.0
    realized_proceeds = sell_proceeds + redeem_proceeds
    proceeds = realized_proceeds if realized_proceeds > 0 else inferred_closed_proceeds

    current_value = sum(num(row, "currentValue", "current_value") for row in positions)
    realized_pnl = sum(num(row, "realizedPnl", "realized_pnl") for row in positions + closed_positions)
    unrealized_pnl = sum(unrealized_position_pnl(row) for row in positions)
    total_pnl = realized_pnl + unrealized_pnl
    roi = safe_div(total_pnl, cost)

    total_shares, avg_entry_price = entry_price_and_shares(positions, closed_positions, trades)
    resolved, won = infer_resolution(positions, closed_positions, activity, total_pnl)
    timestamp = market_timestamp(all_rows)

    title = first_text(all_rows, "title", "question") or condition_id
    slug = first_text(all_rows, "slug") or ""
    event_slug = first_text(all_rows, "eventSlug", "event_slug") or ""
    outcomes = sorted({text(row, "outcome") for row in all_rows if text(row, "outcome")})

    return {
        "market_id": condition_id,
        "title": title,
        "slug": slug,
        "event_slug": event_slug,
        "category": classify_market(title, slug, event_slug),
        "outcomes": ", ".join(outcomes),
        "cost": cost,
        "buy_cost_from_trades": buy_cost,
        "proceeds": proceeds,
        "current_value": current_value,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "pnl": total_pnl,
        "roi": roi,
        "avg_entry_price": avg_entry_price,
        "total_shares": total_shares,
        "resolved": resolved,
        "won": won,
        "timestamp": timestamp,
        "open_positions": len(positions),
        "closed_positions": len(closed_positions),
        "trade_count": len(trades),
        "activity_count": len(activity),
        "end_date": first_text(all_rows, "endDate", "end_date") or "",
    }


def summarize_results(results: list[dict[str, Any]], wallet_data: WalletData) -> dict[str, Any]:
    total_cost = sum(row["cost"] for row in results)
    total_pnl = sum(row["pnl"] for row in results)
    total_current_value = sum(row["current_value"] for row in results)
    total_realized_pnl = sum(row["realized_pnl"] for row in results)
    total_unrealized_pnl = sum(row["unrealized_pnl"] for row in results)
    total_roi = safe_div(total_pnl, total_cost)

    market_count = len(results)
    profitable_markets = [row for row in results if row["pnl"] > 0]
    rois = [row["roi"] for row in results if row["roi"] is not None]
    top1 = results[0] if results else None
    top3 = results[:3]
    top1_pnl = top1["pnl"] if top1 else 0.0
    top1_cost = top1["cost"] if top1 else 0.0
    top3_pnl = sum(row["pnl"] for row in top3)
    top3_cost = sum(row["cost"] for row in top3)

    top1_contribution = safe_div(top1_pnl, total_pnl) if total_pnl > 0 and top1_pnl > 0 else None
    top3_contribution = safe_div(top3_pnl, total_pnl) if total_pnl > 0 and top3_pnl > 0 else None
    roi_ex_top1 = safe_div(total_pnl - top1_pnl, total_cost - top1_cost)
    roi_ex_top3 = safe_div(total_pnl - top3_pnl, total_cost - top3_cost)
    market_win_rate = safe_div(len(profitable_markets), market_count) or 0.0
    median_roi = median(rois) if rois else None

    is_one_hit_wonder = any(
        predicate
        for predicate in (
            top1_contribution is not None and top1_contribution > 0.5,
            roi_ex_top1 is not None and roi_ex_top1 < 0,
            roi_ex_top3 is not None and roi_ex_top3 < 0,
        )
    )
    is_probably_skilled = all(
        predicate
        for predicate in (
            total_roi is not None and total_roi > 0.05,
            market_count >= 30,
            roi_ex_top1 is not None and roi_ex_top1 > 0,
            top1_contribution is not None and top1_contribution < 0.4,
            market_win_rate > 0.5,
            median_roi is not None and median_roi > -0.02,
        )
    )

    return {
        "wallet": wallet_data.wallet,
        "market_count": market_count,
        "total_cost": total_cost,
        "total_current_value": total_current_value,
        "total_realized_pnl": total_realized_pnl,
        "total_unrealized_pnl": total_unrealized_pnl,
        "total_pnl": total_pnl,
        "total_roi": total_roi,
        "position_value_api": wallet_data.position_value,
        "traded_api": wallet_data.traded,
        "market_win_rate": market_win_rate,
        "median_roi": median_roi,
        "top1_contribution": top1_contribution,
        "top3_contribution": top3_contribution,
        "roi_ex_top1": roi_ex_top1,
        "roi_ex_top3": roi_ex_top3,
        "is_one_hit_wonder": is_one_hit_wonder,
        "is_probably_skilled": is_probably_skilled,
        "top_market_title": top1["title"] if top1 else "",
        "top_market_pnl": top1_pnl,
    }


def classify_market(*values: str) -> str:
    haystack = " ".join(value for value in values if value).lower()
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
        "Economy": (
            "fed",
            "interest rate",
            "cpi",
            "inflation",
            "recession",
            "unemployment",
            "gdp",
            "stock market",
            "s&p",
            "nasdaq",
        ),
        "Entertainment": (
            "oscar",
            "grammy",
            "movie",
            "album",
            "song",
            "box office",
            "netflix",
            "celebrity",
            "gta",
        ),
        "Geopolitics": (
            "war",
            "ceasefire",
            "ukraine",
            "russia",
            "israel",
            "china",
            "taiwan",
            "iran",
            "nato",
        ),
        "Weather": ("hurricane", "temperature", "rain", "snow", "weather", "climate"),
    }
    for category, keywords in categories.items():
        if any(keyword in haystack for keyword in keywords):
            return category
    return "Other"


def get_market_key(row: dict[str, Any]) -> str | None:
    for key in ("conditionId", "condition_id", "market_id", "marketId", "slug", "asset"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def record_cost(row: dict[str, Any]) -> float:
    total_bought = num(row, "totalBought", "total_bought")
    avg_price = num(row, "avgPrice", "avg_price")
    if total_bought > 0 and avg_price > 0:
        return total_bought * avg_price
    return num(row, "initialValue", "initial_value")


def trade_notional(row: dict[str, Any]) -> float:
    return num(row, "size") * num(row, "price")


def position_shares(row: dict[str, Any]) -> float:
    shares = num(row, "totalBought", "total_bought")
    if shares > 0:
        return shares
    return num(row, "size")


def entry_price_and_shares(
    positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> tuple[float, float | None]:
    """Return (total_shares, size-weighted average entry price).

    Prefers avgPrice from position/closed-position records; falls back to BUY
    trades. Entry price is the cleanest signal of the odds the wallet bet at,
    which is what separates skill (buying underpriced outcomes) from luck.
    """
    weighted_cost = 0.0
    total_shares = 0.0
    for row in positions + closed_positions:
        shares = position_shares(row)
        price = num(row, "avgPrice", "avg_price")
        if shares > 0 and price > 0:
            weighted_cost += shares * price
            total_shares += shares

    if total_shares > 0:
        return total_shares, weighted_cost / total_shares

    buy_shares = 0.0
    buy_cost = 0.0
    for row in trades:
        if text(row, "side").upper() != "BUY":
            continue
        shares = num(row, "size")
        price = num(row, "price")
        if shares > 0 and price > 0:
            buy_shares += shares
            buy_cost += shares * price

    if buy_shares > 0:
        return buy_shares, buy_cost / buy_shares
    return 0.0, None


def infer_resolution(
    positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    total_pnl: float,
) -> tuple[bool, int | None]:
    """Infer whether a market resolved and whether the held outcome won.

    Resolution is only asserted when there is hard evidence the position is
    settled - a closed position or a REDEEM activity. Open positions are
    deliberately *not* treated as resolved from their price alone, because a
    live long-shot trading near 0 is indistinguishable from a settled loss, and
    guessing would bias the edge calculation. The win/loss is read from the
    final price when it sits near 0/1, otherwise inferred from the PnL sign.
    Returns (resolved, won) where ``won`` is 1, 0, or None when undecidable.
    """
    has_redeem = any(text(row, "type").upper() == "REDEEM" for row in activity)
    if not closed_positions and not has_redeem:
        return False, None

    final_price = weighted_final_price(closed_positions)
    if final_price is not None:
        if final_price >= 0.9:
            return True, 1
        if final_price <= 0.1:
            return True, 0

    return True, 1 if total_pnl > 0 else 0


def weighted_final_price(rows: list[dict[str, Any]]) -> float | None:
    """Size-weighted final/current price across rows that actually report one.

    Uses presence checks so a legitimate price of ``0.0`` (a resolved loss) is
    counted instead of being dropped as if the field were missing.
    """
    price_keys = ("curPrice", "cur_price", "currentPrice", "current_price")
    weighted = 0.0
    total_weight = 0.0
    for row in rows:
        if not any(has_number(row, key) for key in price_keys):
            continue
        price = num(row, *price_keys)
        weight = position_shares(row) or 1.0
        weighted += weight * price
        total_weight += weight
    if total_weight > 0:
        return weighted / total_weight
    return None


def market_timestamp(rows: list[dict[str, Any]]) -> float | None:
    """Representative unix timestamp (seconds) for a market, used for time-based
    consistency analysis. Normalises millisecond timestamps and falls back to
    parsing an ISO end date."""
    latest: float | None = None
    for row in rows:
        for key in ("timestamp", "matchTime", "match_time", "createdAt", "created_at", "lastUpdate"):
            value = num(row, key)
            if value > 0:
                normalized = value / 1000.0 if value > 1e12 else value
                latest = normalized if latest is None else max(latest, normalized)
    if latest is not None:
        return latest

    iso = first_text(rows, "endDate", "end_date")
    return parse_iso_timestamp(iso)


def parse_iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    text_value = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text_value).timestamp()
    except ValueError:
        return None


def unrealized_position_pnl(row: dict[str, Any]) -> float:
    if has_number(row, "cashPnl"):
        return num(row, "cashPnl")
    return num(row, "currentValue", "current_value") - num(row, "initialValue", "initial_value")


def first_text(rows: list[dict[str, Any]], *keys: str) -> str | None:
    for row in rows:
        for key in keys:
            value = text(row, key)
            if value:
                return value
    return None


def text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    return str(value).strip()


def num(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def has_number(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if value is None or value == "":
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def safe_div(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator
