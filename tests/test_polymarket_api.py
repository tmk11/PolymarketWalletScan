from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import pytest
import requests

from polymarket_wallet_analyzer.analyzer import analyze_wallet
from polymarket_wallet_analyzer.polymarket_api import PolymarketAPIError, PolymarketClient, WalletData


class FakeResponse:
    def __init__(self, status_code: int, payload: Any, url: str) -> None:
        self.status_code = status_code
        self.payload = payload
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Bad Request for url: {self.url}")

    def json(self) -> Any:
        return self.payload


class PaginatedSession:
    def __init__(self, fail_at_offset: int | None = None) -> None:
        self.fail_at_offset = fail_at_offset
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int | None = None) -> FakeResponse:
        params = params or {}
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        full_url = f"{url}?{urlencode(params)}"
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 500))
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            return FakeResponse(400, {"error": "offset too high"}, full_url)
        return FakeResponse(200, [{"conditionId": f"m-{offset}-{index}"} for index in range(limit)], full_url)


def test_fetch_activity_stops_before_known_activity_offset_cap() -> None:
    client = PolymarketClient(retries=0)
    session = PaginatedSession()
    client.session = session  # type: ignore[assignment]

    records = client.fetch_activity("0x0000000000000000000000000000000000000001", max_records=5000)

    assert len(records) == 3500
    assert max(call["params"]["offset"] for call in session.calls) == 3000
    assert any("/activity stopped at offset 3500" in warning for warning in client.fetch_warnings)


def test_fetch_paginated_stops_on_terminal_400_after_records() -> None:
    client = PolymarketClient(retries=0)
    session = PaginatedSession(fail_at_offset=1000)
    client.session = session  # type: ignore[assignment]

    records = client._fetch_paginated("/activity", {"user": "0xabc"}, page_limit=500, max_records=5000, max_offset=10000)

    assert len(records) == 1000
    assert any("/activity stopped at offset 1000" in warning for warning in client.fetch_warnings)


def test_fetch_paginated_raises_400_when_first_page_fails() -> None:
    client = PolymarketClient(retries=0)
    client.session = PaginatedSession(fail_at_offset=0)  # type: ignore[assignment]

    with pytest.raises(PolymarketAPIError):
        client._fetch_paginated("/activity", {"user": "0xabc"}, page_limit=500, max_records=5000, max_offset=10000)


def test_analyzer_marks_fetch_warning_as_truncated() -> None:
    wallet = WalletData(
        wallet="0x0000000000000000000000000000000000000001",
        trades=[],
        positions=[],
        closed_positions=[],
        activity=[],
        position_value=0,
        traded=0,
        fetch_warnings=("/activity stopped at offset 3500; results may be truncated.",),
    )

    summary = analyze_wallet(wallet, max_records=5000)["summary"]

    assert summary["data_truncated"] is True
    assert summary["confidence_level"] == "low"
    assert any("/activity stopped at offset 3500" in warning for warning in summary["warnings"])
