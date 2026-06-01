"""Skill-vs-luck scoring for a Polymarket wallet.

The core question this module answers: is a profitable wallet *skilled* (a
repeatable edge) or just *lucky* (profit concentrated in a few outcomes that
could be explained by chance)?

It combines five families of evidence, using only the Python standard library:

1. Edge vs price   - did the wallet buy outcomes that were underpriced relative
   to how they actually resolved? This is the cleanest skill signal in a
   prediction market, because beating the price is what an edge *is*.
2. Statistical significance - is the average per-market ROI distinguishable from
   zero given the sample size and variance (bootstrap confidence interval)?
3. Consistency over time   - is profit spread across months, or bunched into one
   hot streak?
4. Breadth / effective sample - how many *independent* decisions (grouped by
   event) back the result?
5. Concentration & risk   - how dependent is the result on the single best
   market, and how good is the risk-adjusted return (Sharpe, profit factor)?

These are blended into a transparent 0-100 ``skill_score`` with a per-component
breakdown so the verdict is explainable rather than a black box.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any, Sequence

from .polymarket_api import WalletData


# Relative weight of each component in the composite score (renormalised when a
# component cannot be computed for a given wallet).
COMPONENT_WEIGHTS: dict[str, float] = {
    "significance": 25.0,
    "edge": 20.0,
    "consistency": 15.0,
    "breadth": 15.0,
    "concentration": 15.0,
    "risk": 10.0,
}

_BOOTSTRAP_RESAMPLES = 2000
_BOOTSTRAP_SEED = 1_234_567


def compute_skill(
    results: list[dict[str, Any]],
    wallet_data: WalletData,
    summary: dict[str, Any],
    max_records: int | None = None,
) -> dict[str, Any]:
    """Compute the full skill report for an already-analysed wallet."""
    edge = _edge_metrics(results)
    significance = _significance_metrics(results)
    consistency = _consistency_metrics(results)
    breadth = _breadth_metrics(results)
    concentration = _concentration_metrics(results, summary)
    risk = _risk_metrics(results)

    components = _build_components(edge, significance, consistency, breadth, concentration, risk)
    skill_score = _composite_score(components)

    data_truncated = _detect_truncation(wallet_data, max_records)
    confidence = _confidence_level(breadth["effective_bets"], edge["n_resolved"], data_truncated)
    verdict, verdict_label, verdict_detail = _verdict(skill_score, summary, concentration)

    return {
        "skill_score": skill_score,
        "verdict": verdict,
        "verdict_label": verdict_label,
        "verdict_detail": verdict_detail,
        "confidence": confidence,
        "data_truncated": data_truncated,
        "components": components,
        "edge": edge,
        "significance": significance,
        "consistency": consistency,
        "breadth": breadth,
        "concentration": concentration,
        "risk": risk,
    }


# --------------------------------------------------------------------------- #
# 1. Edge vs price (calibration / Brier skill score)
# --------------------------------------------------------------------------- #
def _edge_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [
        row
        for row in results
        if row.get("resolved")
        and row.get("won") in (0, 1)
        and row.get("avg_entry_price") is not None
    ]
    n = len(resolved)
    if n == 0:
        return {
            "n_resolved": 0,
            "avg_entry_price": None,
            "win_rate": None,
            "edge_per_share": None,
            "edge_per_share_weighted": None,
        }

    prices = [_clamp(float(row["avg_entry_price"]), 0.0, 1.0) for row in resolved]
    outcomes = [int(row["won"]) for row in resolved]
    shares = [max(0.0, float(row.get("total_shares") or 0.0)) for row in resolved]

    edges = [y - p for p, y in zip(prices, outcomes)]
    edge_per_share = mean(edges)

    total_shares = sum(shares)
    if total_shares > 0:
        edge_weighted = sum(s * e for s, e in zip(shares, edges)) / total_shares
    else:
        edge_weighted = edge_per_share

    win_rate = mean(outcomes)
    return {
        "n_resolved": n,
        "avg_entry_price": mean(prices),
        "win_rate": win_rate,
        "edge_per_share": edge_per_share,
        "edge_per_share_weighted": edge_weighted,
    }


# --------------------------------------------------------------------------- #
# 2. Statistical significance of per-market ROI
# --------------------------------------------------------------------------- #
def _significance_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    rois = [float(row["roi"]) for row in results if row.get("roi") is not None]
    n = len(rois)
    if n < 2:
        return {
            "n": n,
            "mean_roi": rois[0] if rois else None,
            "std_roi": None,
            "t_stat": None,
            "ci_low": None,
            "ci_high": None,
            "significant": False,
        }

    mean_roi = mean(rois)
    std_roi = stdev(rois)
    standard_error = std_roi / math.sqrt(n) if std_roi > 0 else 0.0
    t_stat = mean_roi / standard_error if standard_error > 0 else None
    ci_low, ci_high = _bootstrap_mean_ci(rois)

    significant = ci_low is not None and ci_low > 0
    return {
        "n": n,
        "mean_roi": mean_roi,
        "std_roi": std_roi,
        "t_stat": t_stat,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "significant": significant,
    }


def _bootstrap_mean_ci(
    values: Sequence[float], resamples: int = _BOOTSTRAP_RESAMPLES
) -> tuple[float | None, float | None]:
    n = len(values)
    if n < 2:
        return None, None
    rng = random.Random(_BOOTSTRAP_SEED)
    means: list[float] = []
    for _ in range(resamples):
        sample_sum = 0.0
        for _ in range(n):
            sample_sum += values[rng.randrange(n)]
        means.append(sample_sum / n)
    means.sort()
    return _percentile(means, 2.5), _percentile(means, 97.5)


# --------------------------------------------------------------------------- #
# 3. Consistency over time
# --------------------------------------------------------------------------- #
def _consistency_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    monthly: dict[str, float] = defaultdict(float)
    for row in results:
        ts = row.get("timestamp")
        if ts is None:
            continue
        month = _month_key(float(ts))
        if month:
            monthly[month] += float(row.get("pnl") or 0.0)

    active_months = len(monthly)
    if active_months < 2:
        return {
            "active_months": active_months,
            "profitable_months": sum(1 for value in monthly.values() if value > 0),
            "profitable_period_ratio": None,
            "monthly_pnl": _sorted_monthly(monthly),
        }

    profitable_months = sum(1 for value in monthly.values() if value > 0)
    return {
        "active_months": active_months,
        "profitable_months": profitable_months,
        "profitable_period_ratio": profitable_months / active_months,
        "monthly_pnl": _sorted_monthly(monthly),
    }


def _sorted_monthly(monthly: dict[str, float]) -> list[dict[str, Any]]:
    return [{"month": key, "pnl": monthly[key]} for key in sorted(monthly)]


def _month_key(timestamp: float) -> str | None:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m")
    except (ValueError, OverflowError, OSError):
        return None


# --------------------------------------------------------------------------- #
# 4. Breadth / effective sample size (group correlated markets by event)
# --------------------------------------------------------------------------- #
def _breadth_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    events = set()
    for row in results:
        key = row.get("event_slug") or row.get("market_id")
        if key:
            events.add(str(key))
    return {
        "market_count": len(results),
        "effective_bets": len(events),
    }


# --------------------------------------------------------------------------- #
# 5. Concentration & risk-adjusted return
# --------------------------------------------------------------------------- #
def _concentration_metrics(results: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    positive_pnls = [float(row["pnl"]) for row in results if float(row.get("pnl") or 0.0) > 0]
    return {
        "top1_contribution": summary.get("top1_contribution"),
        "top3_contribution": summary.get("top3_contribution"),
        "roi_ex_top1": summary.get("roi_ex_top1"),
        "hhi": _hhi(positive_pnls),
        "gini": _gini(positive_pnls),
    }


def _risk_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    rois = [float(row["roi"]) for row in results if row.get("roi") is not None]
    pnls = [float(row.get("pnl") or 0.0) for row in results]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)

    sharpe = None
    if len(rois) >= 2:
        spread = stdev(rois)
        if spread > 0:
            sharpe = mean(rois) / spread

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    return {
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }


def _hhi(values: list[float]) -> float | None:
    total = sum(values)
    if total <= 0:
        return None
    return sum((value / total) ** 2 for value in values)


def _gini(values: list[float]) -> float | None:
    positive = sorted(value for value in values if value > 0)
    n = len(positive)
    total = sum(positive)
    if n == 0 or total <= 0:
        return None
    weighted = sum((index + 1) * value for index, value in enumerate(positive))
    return (2.0 * weighted) / (n * total) - (n + 1) / n


# --------------------------------------------------------------------------- #
# Composite score & verdict
# --------------------------------------------------------------------------- #
def _build_components(
    edge: dict[str, Any],
    significance: dict[str, Any],
    consistency: dict[str, Any],
    breadth: dict[str, Any],
    concentration: dict[str, Any],
    risk: dict[str, Any],
) -> list[dict[str, Any]]:
    components = [
        {
            "key": "significance",
            "label": "Ý nghĩa thống kê (edge có thật?)",
            "normalized": _significance_score(significance),
            "detail": _significance_detail(significance),
        },
        {
            "key": "edge",
            "label": "Edge so với giá vào lệnh",
            "normalized": _edge_score(edge),
            "detail": _edge_detail(edge),
        },
        {
            "key": "consistency",
            "label": "Ổn định theo thời gian",
            "normalized": _consistency_score(consistency),
            "detail": _consistency_detail(consistency),
        },
        {
            "key": "breadth",
            "label": "Số quyết định độc lập",
            "normalized": _breadth_score(breadth),
            "detail": f"{breadth['effective_bets']} event / {breadth['market_count']} market",
        },
        {
            "key": "concentration",
            "label": "Không phụ thuộc 1 kèo",
            "normalized": _concentration_score(concentration),
            "detail": _concentration_detail(concentration),
        },
        {
            "key": "risk",
            "label": "Hiệu suất điều chỉnh rủi ro",
            "normalized": _risk_score(risk),
            "detail": _risk_detail(risk),
        },
    ]
    for component in components:
        component["weight"] = COMPONENT_WEIGHTS[component["key"]]
    return components


def _composite_score(components: list[dict[str, Any]]) -> int | None:
    present = [(c["weight"], c["normalized"]) for c in components if c["normalized"] is not None]
    total_weight = sum(weight for weight, _ in present)
    if total_weight <= 0:
        for component in components:
            component["contribution"] = None
        return None

    score = 0.0
    for component in components:
        if component["normalized"] is None:
            component["contribution"] = None
            continue
        effective_weight = component["weight"] * 100.0 / total_weight
        contribution = component["normalized"] * effective_weight
        component["contribution"] = round(contribution, 1)
        score += contribution
    return int(round(score))


def _significance_score(metrics: dict[str, Any]) -> float | None:
    t_stat = metrics.get("t_stat")
    if t_stat is None:
        return None
    # Logistic on the t-statistic: t=0 -> 0.5, t=2 -> ~0.88, t=-2 -> ~0.12.
    return _clamp(1.0 / (1.0 + math.exp(-t_stat)), 0.0, 1.0)


def _edge_score(metrics: dict[str, Any]) -> float | None:
    edge = metrics.get("edge_per_share_weighted")
    if edge is None:
        return None
    # An edge of +0.05/share (5 cents underpriced) already maps to a strong 0.75.
    return _clamp(0.5 + 5.0 * edge, 0.0, 1.0)


def _consistency_score(metrics: dict[str, Any]) -> float | None:
    return metrics.get("profitable_period_ratio")


def _breadth_score(metrics: dict[str, Any]) -> float | None:
    effective = metrics.get("effective_bets") or 0
    if effective <= 0:
        return None
    # Diminishing returns: ~0.5 at 20 events, ~0.75 at 40, ~0.875 at 60.
    return _clamp(1.0 - 0.5 ** (effective / 20.0), 0.0, 1.0)


def _concentration_score(metrics: dict[str, Any]) -> float | None:
    top1 = metrics.get("top1_contribution")
    if top1 is not None:
        return _clamp(1.0 - top1, 0.0, 1.0)
    hhi = metrics.get("hhi")
    if hhi is not None:
        return _clamp(1.0 - hhi, 0.0, 1.0)
    return None


def _risk_score(metrics: dict[str, Any]) -> float | None:
    parts: list[float] = []
    sharpe = metrics.get("sharpe")
    if sharpe is not None:
        parts.append(_clamp(0.5 + 0.5 * math.tanh(sharpe), 0.0, 1.0))
    profit_factor = metrics.get("profit_factor")
    if profit_factor is not None:
        parts.append(_clamp((profit_factor - 1.0) / 2.0, 0.0, 1.0))
    elif metrics.get("gross_profit", 0) > 0:
        parts.append(1.0)  # profit with zero losses
    if not parts:
        return None
    return mean(parts)


def _verdict(
    skill_score: int | None,
    summary: dict[str, Any],
    concentration: dict[str, Any],
) -> tuple[str, str, str]:
    total_pnl = float(summary.get("total_pnl") or 0.0)
    top1 = concentration.get("top1_contribution")
    roi_ex_top1 = concentration.get("roi_ex_top1")
    one_hit_pattern = (top1 is not None and top1 > 0.5) or (roi_ex_top1 is not None and roi_ex_top1 < 0)

    if total_pnl <= 0:
        return (
            "unprofitable",
            "Chưa có lợi nhuận / chưa có edge",
            "Ví đang lỗ hoặc hòa, nên chưa có cơ sở kết luận có kỹ năng.",
        )
    if skill_score is None:
        return (
            "inconclusive",
            "Chưa đủ dữ liệu",
            "Không đủ dữ liệu để chấm điểm kỹ năng đáng tin cậy.",
        )
    if one_hit_pattern and skill_score < 60:
        return (
            "lucky",
            "Nhiều khả năng ĂN MAY (one-hit wonder)",
            "Lợi nhuận dồn vào số ít kèo; bỏ kèo tốt nhất ra thì edge biến mất.",
        )
    if skill_score >= 65 and not one_hit_pattern:
        return (
            "skilled",
            "Có dấu hiệu KỸ NĂNG / edge ổn định",
            "Lợi nhuận trải rộng, có ý nghĩa thống kê và không phụ thuộc một kèo.",
        )
    return (
        "inconclusive",
        "Chưa kết luận rõ ràng",
        "Có tín hiệu tích cực nhưng chưa đủ mạnh để khẳng định kỹ năng.",
    )


def _confidence_level(effective_bets: int, n_resolved: int, data_truncated: bool) -> str:
    if data_truncated or effective_bets < 10 or n_resolved < 10:
        return "low"
    if effective_bets >= 30 and n_resolved >= 30:
        return "high"
    return "medium"


def _detect_truncation(wallet_data: WalletData, max_records: int | None) -> bool:
    if not max_records:
        return False
    return any(count >= max_records for count in wallet_data.counts.values())


# --------------------------------------------------------------------------- #
# Human-readable detail strings
# --------------------------------------------------------------------------- #
def _significance_detail(metrics: dict[str, Any]) -> str:
    if metrics.get("ci_low") is None:
        return "Chưa đủ market để kiểm định."
    ci_low = metrics["ci_low"]
    ci_high = metrics["ci_high"]
    verdict = "có ý nghĩa (CI dưới > 0)" if metrics.get("significant") else "chưa có ý nghĩa"
    return f"95% CI ROI/market: [{ci_low * 100:.1f}%, {ci_high * 100:.1f}%] — {verdict}."


def _edge_detail(metrics: dict[str, Any]) -> str:
    if metrics.get("edge_per_share") is None:
        return "Chưa có market đã resolve để đo edge."
    edge = metrics["edge_per_share"] * 100
    win_rate = metrics.get("win_rate")
    price = metrics.get("avg_entry_price")
    win_txt = ""
    if win_rate is not None and price is not None:
        win_txt = f"Thắng {win_rate * 100:.0f}% kèo mua TB giá {price:.2f} → "
    return f"{win_txt}edge ~{edge:+.1f}¢/share trên {metrics['n_resolved']} kèo đã resolve."


def _consistency_detail(metrics: dict[str, Any]) -> str:
    ratio = metrics.get("profitable_period_ratio")
    if ratio is None:
        return "Chưa đủ lịch sử theo thời gian (cần ≥ 2 tháng)."
    return f"Lãi {metrics['profitable_months']}/{metrics['active_months']} tháng hoạt động."


def _concentration_detail(metrics: dict[str, Any]) -> str:
    top1 = metrics.get("top1_contribution")
    if top1 is None:
        hhi = metrics.get("hhi")
        return f"HHI lợi nhuận: {hhi:.2f}." if hhi is not None else "Không đủ dữ liệu."
    return f"Kèo tốt nhất chiếm {top1 * 100:.0f}% tổng lãi."


def _risk_detail(metrics: dict[str, Any]) -> str:
    sharpe = metrics.get("sharpe")
    pf = metrics.get("profit_factor")
    sharpe_txt = f"Sharpe (ROI) {sharpe:.2f}" if sharpe is not None else "Sharpe N/A"
    pf_txt = f"profit factor {pf:.2f}" if pf is not None else "profit factor ∞"
    return f"{sharpe_txt}, {pf_txt}."


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (percentile / 100.0) * (len(sorted_values) - 1)
    low_index = math.floor(rank)
    high_index = math.ceil(rank)
    if low_index == high_index:
        return sorted_values[int(rank)]
    weight = rank - low_index
    return sorted_values[low_index] * (1 - weight) + sorted_values[high_index] * weight
