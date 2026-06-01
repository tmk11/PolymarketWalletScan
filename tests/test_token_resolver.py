from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from polymarket_wallet_analyzer.analyzer import analyze_wallet
from polymarket_wallet_analyzer.polymarket_api import WalletData
from polymarket_wallet_analyzer.token_resolver import TokenResolver, extract_token_id


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.response


class StaticResolver(TokenResolver):
    def __init__(self, result: dict[str, Any] | None, cache_path: Path) -> None:
        super().__init__(cache_path=cache_path, enabled=True)
        self.result = result

    def resolve_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        return self.result

    def stats(self) -> dict[str, int | bool]:
        return {
            "token_resolver_enabled": True,
            "token_resolver_cache_hits": 0,
            "token_resolver_api_calls": 0,
            "token_resolver_failures": 0,
        }


def wallet_with_position(position: dict[str, Any]) -> WalletData:
    return WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        trades=[],
        positions=[position],
        closed_positions=[],
        activity=[],
        position_value=0,
        traded=0,
    )


def test_extract_token_id_prefers_clob_token_id_and_strips() -> None:
    assert extract_token_id({"clobTokenId": " 123 "}) == "123"
    assert extract_token_id({"asset": "abc"}) == "abc"
    assert extract_token_id({"asset": ""}) is None


def test_token_resolver_cache_hit_does_not_call_api(tmp_path: Path) -> None:
    cache_path = tmp_path / "token_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "123": {
                    "token_id": "123",
                    "conditionId": "0xabc",
                    "source": "clob_markets_by_token",
                    "resolver_confidence": "high",
                }
            }
        ),
        encoding="utf-8",
    )
    session = cast(Any, FakeSession(FakeResponse(500, {})))
    resolver = TokenResolver(cache_path=cache_path, session=session)

    result = resolver.resolve_token("123")

    assert result is not None
    assert result["conditionId"] == "0xabc"
    assert resolver.api_calls == 0
    assert resolver.cache_hits == 1
    assert session.calls == []


def test_token_resolver_successful_resolve(tmp_path: Path) -> None:
    response = FakeResponse(
        200,
        {
            "conditionId": "0xabc",
            "id": "m1",
            "slug": "will-x-happen",
            "question": "Will X happen?",
            "tokens": [
                {"token_id": "123", "outcome": "Yes"},
                {"token_id": "456", "outcome": "No"},
            ],
        },
    )
    session = cast(Any, FakeSession(response))
    resolver = TokenResolver(cache_path=tmp_path / "token_cache.json", session=session)

    result = resolver.resolve_token("123")

    assert result is not None
    assert result["conditionId"] == "0xabc"
    assert result["marketId"] == "m1"
    assert result["slug"] == "will-x-happen"
    assert result["question"] == "Will X happen?"
    assert result["outcome"] == "Yes"
    assert result["resolver_confidence"] == "high"
    assert result["source"] == "clob_markets_by_token"
    assert resolver.api_calls == 1


def test_token_resolver_failed_token_is_not_retried_in_same_run(tmp_path: Path) -> None:
    session = cast(Any, FakeSession(FakeResponse(404, {"error": "not found"})))
    resolver = TokenResolver(cache_path=tmp_path / "token_cache.json", session=session)

    assert resolver.resolve_token("missing") is None
    assert resolver.resolve_token("missing") is None
    assert resolver.api_calls == 2
    assert len(session.calls) == 2


def test_failed_resolve_remains_unmapped(tmp_path: Path) -> None:
    resolver = StaticResolver(None, tmp_path / "token_cache.json")

    report = analyze_wallet(wallet_with_position({"asset": "unknown_token", "cashPnl": 100}), token_resolver=resolver)

    assert report["summary"]["total_markets"] == 0
    assert report["summary"]["unmapped_records_count"] == 1
    assert report["summary"]["resolved_from_token_count"] == 0
    assert report["unmapped_records"][0]["asset"] == "unknown_token"


def test_resolved_token_enters_market(tmp_path: Path) -> None:
    resolver = StaticResolver(
        {
            "conditionId": "0xabc",
            "marketId": "m1",
            "slug": "will-x-happen",
            "question": "Will X happen?",
            "outcome": "Yes",
            "source": "clob_markets_by_token",
            "resolver_confidence": "high",
        },
        tmp_path / "token_cache.json",
    )

    report = analyze_wallet(wallet_with_position({"asset": "123", "cashPnl": 100}), token_resolver=resolver)
    market = report["markets"][0]

    assert report["summary"]["total_markets"] == 1
    assert report["summary"]["resolved_from_token_count"] == 1
    assert report["summary"]["resolved_from_token_high_confidence_count"] == 1
    assert report["summary"]["unmapped_records_count"] == 0
    assert market["market_id"] == "0xabc"
    assert market["market_key_source"] == "resolved_from_token"


def test_low_confidence_resolve_is_excluded(tmp_path: Path) -> None:
    resolver = StaticResolver(
        {
            "conditionId": "0xabc",
            "source": "clob_markets_by_token",
            "resolver_confidence": "low",
        },
        tmp_path / "token_cache.json",
    )

    report = analyze_wallet(wallet_with_position({"asset": "123", "cashPnl": 100}), token_resolver=resolver)

    assert report["summary"]["total_markets"] == 0
    assert report["summary"]["low_confidence_resolved_count"] == 1
    assert report["summary"]["unmapped_records_count"] == 1
    assert report["low_confidence_resolved_records"][0]["resolved_conditionId"] == "0xabc"
