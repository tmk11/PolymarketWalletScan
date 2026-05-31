from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from .polymarket_api import WalletData


MarketBucket = dict[str, list[dict[str, Any]]]


def analyze_wallet(wallet_data: WalletData) -> dict[str, Any]:
    markets = group_by_market(wallet_data)
    results = [analyze_market(condition_id, bucket) for condition_id, bucket in markets.items()]
    results.sort(key=lambda row: row["pnl"], reverse=True)
    summary = summarize_results(results, wallet_data)
    return {"summary": summary, "markets": results, "raw_counts": wallet_data.counts}


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
    proceeds = sell_proceeds + redeem_proceeds or inferred_closed_proceeds

    current_value = sum(num(row, "currentValue", "current_value") for row in positions)
    realized_pnl = sum(num(row, "realizedPnl", "realized_pnl") for row in positions + closed_positions)
    unrealized_pnl = sum(unrealized_position_pnl(row) for row in positions)
    total_pnl = realized_pnl + unrealized_pnl
    roi = safe_div(total_pnl, cost)

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
