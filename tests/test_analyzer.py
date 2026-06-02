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


def _test_wallet(positions=None, closed_positions=None, trades=None, activity=None) -> WalletData:
    return WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        positions=positions or [],
        closed_positions=closed_positions or [],
        trades=trades or [],
        activity=activity or [],
        position_value=0,
        traded=0,
    )


def _closed_pnl_market(market_id: str, pnl: float, buy_notional: float = 1000.0) -> dict:
    return {
        "conditionId": market_id,
        "title": f"Weather market {market_id}",
        "eventSlug": market_id,
        "asset": f"asset-{market_id}",
        "outcome": "Yes",
        "totalBought": buy_notional,
        "avgPrice": 1.0,
        "realizedPnl": pnl,
        "curPrice": 1.0 if pnl > 0 else 0.0,
    }


def test_summary_output_has_new_contract_fields() -> None:
    report = analyze_wallet(_test_wallet(closed_positions=[_closed_pnl_market("m1", 25, buy_notional=100)]))
    summary = report["summary"]

    for key in (
        "trading_pnl",
        "rewards_pnl",
        "total_pnl_including_rewards",
        "realized_pnl",
        "unrealized_pnl",
        "total_markets",
        "winning_markets",
        "losing_markets",
        "market_win_rate",
        "total_cost_basis",
        "total_buy_notional",
        "total_max_capital_at_risk",
        "roi_cost_basis",
        "roi_buy_notional",
        "roi_max_capital_at_risk",
        "roi_ex_top1_cost_basis",
        "roi_ex_top1_buy_notional",
        "roi_ex_top3_cost_basis",
        "roi_ex_top3_buy_notional",
        "roi_ex_top5_cost_basis",
        "roi_ex_top5_buy_notional",
        "top1_contribution_net_pnl",
        "top3_contribution_net_pnl",
        "top1_share_of_gross_profit",
        "top3_share_of_gross_profit",
        "median_market_roi",
        "mean_market_roi_unweighted",
        "mean_market_roi_cost_weighted",
        "profit_factor",
        "hhi_profit_concentration",
        "gini_profit_concentration",
        "effective_bets",
        "max_drawdown",
        "confidence_level",
        "verdict",
        "resolved_from_token_count",
        "resolved_from_token_high_confidence_count",
        "low_confidence_resolved_count",
        "token_resolver_enabled",
        "token_resolver_cache_hits",
        "token_resolver_api_calls",
        "token_resolver_failures",
        "recent_trade_windows",
        "recent_buy_trade_windows",
        "recent_market_windows",
        "recent_trade_frequency",
        "recent_trade_3d",
        "recent_buy_trade_3d",
        "recent_market_3d",
        "recent_3d_trade_count",
        "recent_3d_buy_count",
        "recent_3d_estimated_pnl",
        "recent_3d_buy_estimated_pnl",
        "recent_3d_market_pnl",
        "recent_3d_frequency_label",
        "recent_7d_trade_count",
        "recent_7d_avg_trades_per_day",
        "recent_7d_trade_notional",
        "recent_7d_frequency_label",
        "recent_buy_trade_50_estimated_pnl",
        "recent_buy_trade_50_roi",
        "recent_copy_risk_level",
        "unmapped_records_count",
        "warnings",
    ):
        assert key in summary

    assert summary["total_pnl"] == summary["trading_pnl"]
    assert summary["total_roi"] == summary["roi_cost_basis"]
    assert summary["roi_ex_top1"] == summary["roi_ex_top1_cost_basis"]
    assert summary["top1_contribution"] == summary["top1_contribution_net_pnl"]


def test_asset_only_record_is_unmapped_in_analyzer_contract() -> None:
    report = analyze_wallet(_test_wallet(positions=[{"asset": "YES_TOKEN_123", "cashPnl": 100}]))

    assert report["summary"]["total_markets"] == 0
    assert report["summary"]["unmapped_records_count"] == 1
    assert report["unmapped_records_count"] == 1
    assert any("asset/token" in warning or "unmapped" in warning.lower() for warning in report["summary"]["warnings"])


def test_open_and_closed_realized_pnl_is_not_double_counted() -> None:
    report = analyze_wallet(
        _test_wallet(
            positions=[
                {
                    "conditionId": "abc",
                    "title": "Weather overlap",
                    "asset": "yes-abc",
                    "realizedPnl": 100,
                    "cashPnl": 20,
                    "totalBought": 100,
                    "avgPrice": 0.5,
                }
            ],
            closed_positions=[
                {
                    "conditionId": "abc",
                    "title": "Weather overlap",
                    "asset": "yes-abc",
                    "realizedPnl": 100,
                    "totalBought": 200,
                    "avgPrice": 0.5,
                }
            ],
        )
    )
    market = report["markets"][0]

    assert market["realized_pnl"] == 100
    assert market["unrealized_pnl"] == 20
    assert market["trading_pnl"] == 120
    assert any("possible_overlap_realized_pnl" in warning for warning in market["warnings"])


def test_top1_contribution_above_100_percent_is_not_capped() -> None:
    summary = analyze_wallet(
        _test_wallet(
            closed_positions=[
                _closed_pnl_market("A", 1500, buy_notional=1000),
                _closed_pnl_market("B", -300, buy_notional=1000),
                _closed_pnl_market("C", -200, buy_notional=1000),
            ]
        )
    )["summary"]

    assert summary["trading_pnl"] == 1000
    assert summary["top1_contribution_net_pnl"] == 1.5
    assert summary["top1_share_of_gross_profit"] == 1.0
    assert summary["verdict"] == "insufficient_data"
    assert any("low_sample_one_hit_pattern_detected" in warning for warning in summary["warnings"])


def test_roi_ex_top1_buy_notional_negative_flags_lucky() -> None:
    summary = analyze_wallet(
        _test_wallet(
            closed_positions=[
                _closed_pnl_market("A", 1500, buy_notional=1000),
                _closed_pnl_market("B", -300, buy_notional=1000),
                _closed_pnl_market("C", -200, buy_notional=1000),
            ]
        )
    )["summary"]

    assert summary["roi_ex_top1_buy_notional"] < 0
    assert summary["verdict"] == "insufficient_data"
    assert any("low_sample_one_hit_pattern_detected" in warning for warning in summary["warnings"])


def test_single_market_big_win_is_insufficient_data_not_lucky() -> None:
    summary = analyze_wallet(_test_wallet(closed_positions=[_closed_pnl_market("A", 1500, buy_notional=1000)]))["summary"]

    assert summary["total_markets"] == 1
    assert summary["top1_contribution_net_pnl"] == 1.0
    assert summary["verdict"] == "insufficient_data"
    assert any("low_sample_one_hit_pattern_detected" in warning for warning in summary["warnings"])


def test_evenly_profitable_50_market_wallet_is_skilled() -> None:
    closed_positions = []
    for index in range(50):
        won = index % 5 < 3
        shares = 100.0
        entry = 0.45
        closed_positions.append(
            {
                "conditionId": f"m{index}",
                "eventSlug": f"weather-independent-{index}",
                "title": f"Weather market {index}",
                "asset": f"asset-{index}",
                "outcome": "Yes",
                "totalBought": shares,
                "avgPrice": entry,
                "curPrice": 1.0 if won else 0.0,
                "realizedPnl": shares * (1 - entry) if won else -(shares * entry),
            }
        )

    summary = analyze_wallet(_test_wallet(closed_positions=closed_positions))["summary"]

    assert summary["verdict"] == "skilled"
    assert summary["roi_ex_top1_buy_notional"] > 0
    assert summary["top1_contribution_net_pnl"] < 0.4


def test_buy_notional_roi_can_differ_from_cost_basis_roi() -> None:
    market = analyze_wallet(
        _test_wallet(
            positions=[
                {
                    "conditionId": "turnover",
                    "title": "Weather turnover",
                    "asset": "yes-turnover",
                    "totalBought": 100,
                    "avgPrice": 0.5,
                    "currentValue": 70,
                    "initialValue": 50,
                    "cashPnl": 20,
                }
            ],
            trades=[
                {"conditionId": "turnover", "asset": "yes-turnover", "side": "BUY", "size": 100, "price": 0.5, "timestamp": 1},
                {"conditionId": "turnover", "asset": "yes-turnover", "side": "SELL", "size": 100, "price": 0.6, "timestamp": 2},
                {"conditionId": "turnover", "asset": "yes-turnover", "side": "BUY", "size": 100, "price": 0.5, "timestamp": 3},
            ],
        )
    )["markets"][0]

    assert market["roi_buy_notional"] != market["roi_cost_basis"]
    assert market["roi_buy_notional"] == 0.3
    assert market["roi_cost_basis"] == 0.6
