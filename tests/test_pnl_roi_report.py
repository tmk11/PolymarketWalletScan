from __future__ import annotations

from datetime import datetime, timezone

from polymarket_wallet_analyzer.analyzer import analyze_wallet
from polymarket_wallet_analyzer.polymarket_api import WalletData


def _timestamp(month: int) -> float:
    return datetime(2025, month, 1, tzinfo=timezone.utc).timestamp()


def _wallet(positions=None, closed_positions=None, trades=None, activity=None) -> WalletData:
    return WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        positions=positions or [],
        closed_positions=closed_positions or [],
        trades=trades or [],
        activity=activity or [],
        position_value=0.0,
        traded=0.0,
    )


def _closed_market(market_id: str, pnl: float, cost: float = 500.0, title: str | None = None, month: int = 1) -> dict:
    won = pnl > 0
    avg_price = 0.5
    shares = cost / avg_price
    return {
        "conditionId": market_id,
        "eventSlug": market_id,
        "title": title or f"Weather market {market_id}",
        "outcome": "Yes",
        "asset": f"asset-{market_id}",
        "totalBought": shares,
        "avgPrice": avg_price,
        "curPrice": 1.0 if won else 0.0,
        "realizedPnl": pnl,
        "timestamp": _timestamp(month),
    }


def test_no_double_count_when_open_and_closed_share_market() -> None:
    wallet = _wallet(
        positions=[
            {
                "conditionId": "m1",
                "title": "Weather open leg",
                "asset": "yes-m1",
                "totalBought": 100,
                "avgPrice": 0.5,
                "currentValue": 60,
                "initialValue": 50,
                "cashPnl": 10,
                "realizedPnl": 100,
            }
        ],
        closed_positions=[
            {
                "conditionId": "m1",
                "title": "Weather open leg",
                "asset": "yes-m1",
                "totalBought": 200,
                "avgPrice": 0.5,
                "realizedPnl": 100,
                "curPrice": 1.0,
            }
        ],
    )

    market = analyze_wallet(wallet)["markets"][0]

    assert market["realized_pnl"] == 100
    assert market["unrealized_pnl"] == 10
    assert market["trading_pnl"] == 110
    assert "not double-counted" in market["warnings"][0]


def test_lucky_one_large_event_is_flagged() -> None:
    wallet = _wallet(
        closed_positions=[
            _closed_market("A", 1500, cost=1500, month=1),
            _closed_market("B", -200, cost=200, month=2),
            _closed_market("C", -300, cost=300, month=3),
        ]
    )

    summary = analyze_wallet(wallet)["summary"]

    assert summary["trading_pnl"] == 1000
    assert summary["top1_contribution_net_pnl"] == 1.5
    assert summary["verdict"] == "lucky_or_one_hit_wonder"
    assert summary["is_one_hit_wonder"] is True


def test_evenly_profitable_wallet_is_skilled() -> None:
    closed_positions = []
    for index in range(50):
        won = index % 5 < 3
        shares = 100.0
        avg_price = 0.45
        closed_positions.append(
            {
                "conditionId": f"m{index}",
                "eventSlug": f"independent-weather-{index}",
                "title": f"Weather event {index}",
                "outcome": "Yes",
                "asset": f"asset-{index}",
                "totalBought": shares,
                "avgPrice": avg_price,
                "curPrice": 1.0 if won else 0.0,
                "realizedPnl": shares * (1.0 - avg_price) if won else -(shares * avg_price),
                "timestamp": _timestamp((index % 12) + 1),
            }
        )

    summary = analyze_wallet(_wallet(closed_positions=closed_positions))["summary"]

    assert summary["total_markets"] == 50
    assert summary["market_win_rate"] == 0.6
    assert summary["top1_contribution_net_pnl"] < 0.4
    assert summary["roi_ex_top1_buy_notional"] is not None and summary["roi_ex_top1_buy_notional"] > 0
    assert summary["verdict"] == "skilled"


def test_top1_contribution_can_exceed_100_percent() -> None:
    summary = analyze_wallet(
        _wallet(
            closed_positions=[
                _closed_market("A", 1500, cost=1500),
                _closed_market("B", -200, cost=200),
                _closed_market("C", -300, cost=300),
            ]
        )
    )["summary"]

    assert summary["top1_contribution_net_pnl"] == 1.5


def test_yes_and_no_edge_is_outcome_level_but_pnl_is_market_level() -> None:
    wallet = _wallet(
        closed_positions=[
            {
                "conditionId": "same-market",
                "title": "Weather binary",
                "outcome": "Yes",
                "asset": "yes-token",
                "totalBought": 100,
                "avgPrice": 0.8,
                "curPrice": 1.0,
                "realizedPnl": 20,
            },
            {
                "conditionId": "same-market",
                "title": "Weather binary",
                "outcome": "No",
                "asset": "no-token",
                "totalBought": 100,
                "avgPrice": 0.2,
                "curPrice": 0.0,
                "realizedPnl": -20,
            },
        ]
    )

    report = analyze_wallet(wallet)
    market = report["markets"][0]

    assert report["summary"]["total_markets"] == 1
    assert market["trading_pnl"] == 0
    assert len(market["outcome_level_edge"]) == 2
    assert {edge["outcome"] for edge in market["outcome_level_edge"]} == {"Yes", "No"}


def test_asset_only_record_is_unmapped_not_market_key() -> None:
    wallet = _wallet(trades=[{"asset": "token-only", "side": "BUY", "size": 100, "price": 0.5}])

    report = analyze_wallet(wallet)

    assert report["summary"]["total_markets"] == 0
    assert report["unmapped_records_count"] == 1
    assert report["unmapped_records"][0]["asset"] == "token-only"


def test_roi_buy_notional_differs_from_cost_basis_for_round_trips() -> None:
    wallet = _wallet(
        positions=[
            {
                "conditionId": "roundtrip",
                "title": "Weather round trip",
                "asset": "yes-roundtrip",
                "totalBought": 100,
                "avgPrice": 0.5,
                "currentValue": 70,
                "initialValue": 50,
                "cashPnl": 20,
            }
        ],
        trades=[
            {"conditionId": "roundtrip", "asset": "yes-roundtrip", "side": "BUY", "size": 100, "price": 0.5, "timestamp": 1},
            {"conditionId": "roundtrip", "asset": "yes-roundtrip", "side": "SELL", "size": 100, "price": 0.6, "timestamp": 2},
            {"conditionId": "roundtrip", "asset": "yes-roundtrip", "side": "BUY", "size": 100, "price": 0.5, "timestamp": 3},
        ],
    )

    market = analyze_wallet(wallet)["markets"][0]

    assert market["trading_pnl"] == 30
    assert market["cost_basis"] == 50
    assert market["total_buy_notional"] == 100
    assert market["roi_cost_basis"] == 0.6
    assert market["roi_buy_notional"] == 0.3
