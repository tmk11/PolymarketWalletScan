from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import requests

DATA_API_BASE_URL = "https://data-api.polymarket.com"
GAMMA_API_BASE_URL = "https://gamma-api.polymarket.com"
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class PolymarketAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class WalletData:
    wallet: str
    trades: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    closed_positions: list[dict[str, Any]]
    activity: list[dict[str, Any]]
    position_value: float | None
    traded: float | None
    fetch_warnings: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "trades": len(self.trades),
            "positions": len(self.positions),
            "closed_positions": len(self.closed_positions),
            "activity": len(self.activity),
        }


def validate_wallet(wallet: str) -> str:
    cleaned = wallet.strip()
    if not WALLET_RE.match(cleaned):
        raise ValueError("Wallet phải là địa chỉ EVM dạng 0x + 40 ký tự hex.")
    return cleaned.lower()


class PolymarketClient:
    def __init__(
        self,
        base_url: str = DATA_API_BASE_URL,
        timeout: int = 20,
        retries: int = 3,
        backoff_seconds: float = 0.6,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "polymarket-wallet-analyzer/0.1",
            }
        )
        self.fetch_warnings: list[str] = []

    def fetch_wallet_data(self, wallet: str, max_records: int = 5000) -> WalletData:
        user = validate_wallet(wallet)
        self.fetch_warnings = []
        return WalletData(
            wallet=user,
            trades=self.fetch_trades(user, max_records=max_records),
            positions=self.fetch_positions(user, max_records=max_records),
            closed_positions=self.fetch_closed_positions(user, max_records=max_records),
            activity=self.fetch_activity(user, max_records=max_records),
            position_value=self.fetch_position_value(user),
            traded=self.fetch_traded(user),
            fetch_warnings=tuple(self.fetch_warnings),
        )

    def fetch_trades(self, wallet: str, max_records: int = 5000) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "/trades",
            {"user": wallet},
            page_limit=10000,
            max_records=max_records,
            max_offset=10000,
        )

    def fetch_positions(self, wallet: str, max_records: int = 5000) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "/positions",
            {"user": wallet, "sizeThreshold": 0},
            page_limit=500,
            max_records=max_records,
            max_offset=10000,
        )

    def fetch_closed_positions(self, wallet: str, max_records: int = 5000) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "/closed-positions",
            {"user": wallet},
            page_limit=50,
            max_records=max_records,
            max_offset=100000,
        )

    def fetch_activity(self, wallet: str, max_records: int = 5000) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "/activity",
            {"user": wallet},
            page_limit=500,
            max_records=max_records,
            max_offset=3000,
        )

    def fetch_position_value(self, wallet: str) -> float | None:
        payload = self._get_json("/value", {"user": wallet})
        if isinstance(payload, list) and payload:
            return _to_float(payload[0].get("value"))
        if isinstance(payload, dict):
            return _to_float(payload.get("value"))
        return None

    def fetch_traded(self, wallet: str) -> float | None:
        payload = self._get_json("/traded", {"user": wallet})
        if isinstance(payload, dict):
            return _to_float(payload.get("traded"))
        return None

    def _fetch_paginated(
        self,
        path: str,
        params: dict[str, Any],
        page_limit: int,
        max_records: int,
        max_offset: int,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        target = max(0, max_records)

        while len(records) < target and offset <= max_offset:
            limit = min(page_limit, target - len(records))
            page_params = {**params, "limit": limit, "offset": offset}
            try:
                payload = self._get_json(path, page_params)
            except PolymarketAPIError as exc:
                if records and is_terminal_pagination_error(exc):
                    self._warn_fetch_truncated(path, offset, f"Data API returned {exc}")
                    break
                raise
            page = _extract_list(payload, path)
            records.extend(page)
            if len(page) < limit:
                break
            offset += limit

        if len(records) < target and offset > max_offset:
            self._warn_fetch_truncated(path, offset, f"Data API pagination cap reached at offset {offset}")

        return records[:target]

    def _warn_fetch_truncated(self, path: str, offset: int, reason: str) -> None:
        warning = f"{path} stopped at offset {offset}; results may be truncated. {reason}."
        if warning not in self.fetch_warnings:
            self.fetch_warnings.append(warning)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
                break

        raise PolymarketAPIError(f"Không fetch được {path}: {last_error}") from last_error


def _extract_list(payload: Any, path: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    raise PolymarketAPIError(f"Endpoint {path} trả về schema không phải list.")


def is_terminal_pagination_error(exc: Exception) -> bool:
    message = str(exc)
    return "400 Client Error" in message and "offset=" in message


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
