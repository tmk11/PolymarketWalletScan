from __future__ import annotations

from datetime import datetime, timezone

from polymarket_wallet_analyzer.analyzer import analyze_wallet
from polymarket_wallet_analyzer.polymarket_api import WalletData
from polymarket_wallet_analyzer.skill_score import (
    _bootstrap_mean_ci,
    _gini,
    _hhi,
    _percentile,
    compute_skill,
)


def _ts(year: int, month: int) -> float:
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def _closed_position(
    market_id: str,
    entry_price: float,
    won: bool,
    shares: float = 100.0,
    month: int = 1,
    event: str | None = None,
) -> dict:
    cost = shares * entry_price
    payout = shares if won else 0.0
    return {
        "conditionId": market_id,
        "eventSlug": event or market_id,
        "title": f"Market {market_id}",
        "outcome": "Yes",
        "totalBought": shares,
        "avgPrice": entry_price,
        "curPrice": 1.0 if won else 0.0,
        "realizedPnl": payout - cost,
        "timestamp": _ts(2025, month),
    }


def _build_wallet(positions=None, closed_positions=None, **kwargs) -> WalletData:
    return WalletData(
        wallet="0x0000000000000000000000000000000000000abc",
        positions=positions or [],
        closed_positions=closed_positions or [],
        trades=kwargs.get("trades", []),
        activity=kwargs.get("activity", []),
        position_value=kwargs.get("position_value", 0.0),
        traded=kwargs.get("traded", 0.0),
    )


def test_skilled_wallet_scores_high_and_has_positive_edge() -> None:
    # 60% win rate while consistently buying at 0.40 -> a real +0.20/share edge,
    # spread across many independent events and several months.
    closed = []
    for index in range(20):
        won = index % 5 < 3  # 12 wins / 8 losses = 60%
        closed.append(
            _closed_position(
                market_id=f"m{index}",
                entry_price=0.40,
                won=won,
                shares=100.0,
                month=(index % 6) + 1,
                event=f"event-{index}",
            )
        )
    report = analyze_wallet(_build_wallet(closed_positions=closed), max_records=5000)
    skill = report["skill"]

    assert skill["edge"]["n_resolved"] == 20
    assert skill["edge"]["edge_per_share"] > 0.1
    assert skill["edge"]["win_rate"] is not None and skill["edge"]["win_rate"] > 0.5
    assert skill["consistency"]["profitable_period_ratio"] is not None
    assert skill["breadth"]["effective_bets"] == 20
    assert skill["skill_score"] is not None
    assert skill["skill_score"] >= 60
    assert skill["verdict"] in {"skilled", "category_skilled", "inconclusive"}
    # Components renormalise to the final score.
    contributions = [c["contribution"] for c in skill["components"] if c["contribution"] is not None]
    assert abs(sum(contributions) - skill["skill_score"]) <= 1.5


def test_one_hit_wonder_flagged_as_lucky() -> None:
    closed = [
        _closed_position("big", entry_price=0.5, won=True, shares=2000.0, month=1, event="e-big"),
        _closed_position("small1", entry_price=0.5, won=False, shares=50.0, month=1, event="e1"),
        _closed_position("small2", entry_price=0.5, won=False, shares=50.0, month=1, event="e2"),
    ]
    report = analyze_wallet(_build_wallet(closed_positions=closed), max_records=5000)
    summary = report["summary"]
    skill = report["skill"]

    assert summary["total_pnl"] > 0
    assert summary["top1_contribution"] > 0.5
    assert skill["verdict"] == "lucky_or_one_hit_wonder"
    assert skill["skill_score"] < 60


def test_unprofitable_wallet_is_not_skilled() -> None:
    closed = [
        _closed_position("a", entry_price=0.8, won=False, shares=100.0, month=1, event="ea"),
        _closed_position("b", entry_price=0.8, won=False, shares=100.0, month=2, event="eb"),
    ]
    report = analyze_wallet(_build_wallet(closed_positions=closed), max_records=5000)
    skill = report["skill"]

    assert report["summary"]["total_pnl"] < 0
    assert skill["verdict"] == "unprofitable"


def test_data_truncation_lowers_confidence() -> None:
    closed = [_closed_position(f"m{i}", 0.4, won=i % 2 == 0, month=(i % 4) + 1, event=f"e{i}") for i in range(40)]
    wallet = _build_wallet(closed_positions=closed)
    # closed-position count (40) hits the max_records cap -> truncated.
    report = analyze_wallet(wallet, max_records=40)
    skill = report["skill"]

    assert skill["data_truncated"] is True
    assert skill["confidence"] == "low"


def test_compute_skill_handles_empty_wallet() -> None:
    skill = compute_skill([], _build_wallet(), summary={"total_pnl": 0.0}, max_records=5000)
    assert skill["skill_score"] is None
    assert skill["verdict"] == "unprofitable"
    assert skill["edge"]["n_resolved"] == 0


def test_bootstrap_ci_is_deterministic_and_ordered() -> None:
    values = [0.1, -0.2, 0.5, 0.3, -0.1, 0.4, 0.2, 0.0]
    low_a, high_a = _bootstrap_mean_ci(values)
    low_b, high_b = _bootstrap_mean_ci(values)
    assert low_a == low_b and high_a == high_b
    assert low_a is not None and high_a is not None
    assert low_a <= high_a


def test_gini_and_hhi_edges() -> None:
    assert _gini([10.0, 10.0, 10.0]) == 0.0  # perfectly equal
    assert _gini([]) is None
    # All profit in one market -> maximal concentration.
    assert _hhi([100.0]) == 1.0
    assert _hhi([]) is None


def test_percentile_interpolates() -> None:
    data = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert _percentile(data, 0) == 0.0
    assert _percentile(data, 100) == 4.0
    assert _percentile(data, 50) == 2.0
