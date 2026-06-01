from __future__ import annotations

from polymarket_wallet_analyzer.analyzer import analyze_wallet, classify_market
from polymarket_wallet_analyzer.polymarket_api import WalletData


def test_analyze_wallet_flags_one_hit_wonder() -> None:
    wallet_data = WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        positions=[
            {
                "conditionId": "m1",
                "title": "Will Bitcoin hit 100k?",
                "outcome": "Yes",
                "totalBought": 100,
                "avgPrice": 0.5,
                "currentValue": 120,
                "initialValue": 50,
                "cashPnl": 70,
                "realizedPnl": 0,
            },
            {
                "conditionId": "m2",
                "title": "Will Team A win?",
                "outcome": "No",
                "totalBought": 100,
                "avgPrice": 0.5,
                "currentValue": 30,
                "initialValue": 50,
                "cashPnl": -20,
                "realizedPnl": 0,
            },
        ],
        closed_positions=[],
        trades=[],
        activity=[],
        position_value=150,
        traded=100,
    )

    report = analyze_wallet(wallet_data)
    summary = report["summary"]

    assert summary["market_count"] == 2
    assert summary["total_cost"] == 100
    assert summary["total_pnl"] == 50
    assert summary["top1_contribution"] == 1.4
    assert summary["roi_ex_top1"] == -0.4
    assert summary["is_one_hit_wonder"] is True
    assert summary["is_probably_skilled"] is False


def test_analyze_wallet_uses_closed_position_realized_pnl() -> None:
    wallet_data = WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        positions=[],
        closed_positions=[
            {
                "conditionId": "m1",
                "title": "Will Donald Trump win?",
                "totalBought": 200,
                "avgPrice": 0.25,
                "realizedPnl": 25,
            }
        ],
        trades=[],
        activity=[],
        position_value=0,
        traded=50,
    )

    market = analyze_wallet(wallet_data)["markets"][0]

    assert market["cost"] == 50
    assert market["realized_pnl"] == 25
    assert market["unrealized_pnl"] == 0
    assert market["pnl"] == 25
    assert market["roi"] == 0.5
    assert market["category"] == "Politics"


def test_classify_market() -> None:
    assert classify_market("Will Ethereum ETF be approved?") == "Crypto"
    assert classify_market("NBA finals winner") == "Sports"
    assert classify_market("Fed cuts interest rate") == "Economy/Fed"
    assert classify_market("Some niche market") == "Other"



def test_analyze_market_detects_resolved_loss_with_zero_price() -> None:
    # A losing closed position settles to curPrice 0.0; it must still be
    # recognised as resolved (won=0) and not silently dropped.
    wallet_data = WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        positions=[],
        closed_positions=[
            {
                "conditionId": "m1",
                "title": "Will it happen?",
                "totalBought": 100,
                "avgPrice": 0.6,
                "curPrice": 0.0,
                "realizedPnl": -60,
            }
        ],
        trades=[],
        activity=[],
        position_value=0,
        traded=60,
    )

    market = analyze_wallet(wallet_data)["markets"][0]

    assert market["resolved"] is True
    assert market["won"] == 0
    assert market["avg_entry_price"] == 0.6
    assert market["total_shares"] == 100


def test_open_position_is_not_treated_as_resolved() -> None:
    # A live long-shot at 0.05 with no closed position / redeem must stay open.
    wallet_data = WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        positions=[
            {
                "conditionId": "m1",
                "title": "Long shot",
                "totalBought": 100,
                "avgPrice": 0.05,
                "curPrice": 0.05,
                "currentValue": 5,
                "initialValue": 5,
            }
        ],
        closed_positions=[],
        trades=[],
        activity=[],
        position_value=5,
        traded=5,
    )

    market = analyze_wallet(wallet_data)["markets"][0]

    assert market["resolved"] is False
    assert market["won"] is None
