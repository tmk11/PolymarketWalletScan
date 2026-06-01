from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_TOKEN_CACHE_PATH = Path(".cache/polymarket_token_resolver_cache.json")
TOKEN_FIELDS = ("clobTokenId", "clob_token_id", "tokenId", "token_id", "asset")


def extract_token_id(record: dict[str, Any]) -> str | None:
    for field in TOKEN_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        token_id = str(value).strip()
        if token_id:
            return token_id
    return None


class TokenResolver:
    def __init__(
        self,
        cache_path: str | Path | None = None,
        timeout: float = 10.0,
        enabled: bool = True,
        clob_base_url: str = CLOB_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path is not None else DEFAULT_TOKEN_CACHE_PATH
        self.timeout = timeout
        self.enabled = enabled
        self.clob_base_url = clob_base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-wallet-analyzer/0.1"})
        self.cache: dict[str, dict[str, Any]] = self._load_cache()
        self.misses: set[str] = set()
        self.cache_hits = 0
        self.api_calls = 0
        self.failures = 0

    def resolve_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        token_id = extract_token_id(record)
        if not token_id:
            return None
        return self.resolve_token(token_id)

    def resolve_token(self, token_id: str) -> dict[str, Any] | None:
        cleaned = str(token_id).strip()
        if not self.enabled or not cleaned:
            return None

        cached = self.cache.get(cleaned)
        if cached:
            self.cache_hits += 1
            return cached
        if cleaned in self.misses:
            return None

        for endpoint in self._endpoints(cleaned):
            data = self._fetch_json(endpoint["path"], endpoint.get("params"))
            if data is None:
                continue
            result = build_resolution(data, cleaned, str(endpoint["source"]))
            if result is None:
                continue
            self.cache[cleaned] = result
            self._save_cache()
            return result

        self.misses.add(cleaned)
        return None

    def stats(self) -> dict[str, int | bool]:
        return {
            "token_resolver_enabled": self.enabled,
            "token_resolver_cache_hits": self.cache_hits,
            "token_resolver_api_calls": self.api_calls,
            "token_resolver_failures": self.failures,
        }

    def _endpoints(self, token_id: str) -> list[dict[str, Any]]:
        return [
            {"path": f"/markets/{token_id}", "params": None, "source": "clob_markets_by_token"},
            {"path": "/book", "params": {"token_id": token_id}, "source": "clob_orderbook_by_token"},
        ]

    def _fetch_json(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        self.api_calls += 1
        try:
            response = self.session.get(f"{self.clob_base_url}{path}", params=params, timeout=self.timeout)
            if response.status_code != 200:
                self.failures += 1
                return None
            return response.json()
        except (requests.RequestException, ValueError):
            self.failures += 1
            return None

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return


def build_resolution(data: Any, token_id: str, source: str) -> dict[str, Any] | None:
    market_data = market_payload(data)
    if not market_data:
        return None

    condition_id = first_value(market_data, "conditionId", "condition_id")
    if not condition_id and source == "clob_orderbook_by_token":
        asset_id = first_value(market_data, "asset_id", "assetId")
        market = first_value(market_data, "market")
        if asset_id and str(asset_id) == token_id and market:
            condition_id = market
    if not condition_id:
        return None

    if source != "clob_orderbook_by_token" and not token_matches_market(market_data, token_id):
        return None

    return {
        "token_id": token_id,
        "conditionId": str(condition_id),
        "marketId": first_value(market_data, "id", "marketId", "market_id"),
        "slug": first_value(market_data, "slug", "marketSlug", "market_slug"),
        "question": first_value(market_data, "question", "title"),
        "outcome": infer_outcome_from_token(market_data, token_id),
        "source": source,
        "resolver_confidence": "high",
    }


def market_payload(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    for key in ("data", "market"):
        nested = data.get(key)
        if isinstance(nested, dict):
            return nested
    return data


def token_matches_market(data: dict[str, Any], token_id: str) -> bool:
    tokens = token_entries(data)
    if not tokens:
        return True
    return any(token_entry_id(token) == token_id for token in tokens)


def infer_outcome_from_token(data: dict[str, Any], token_id: str) -> str | None:
    direct = first_value(data, "outcome")
    if direct:
        return str(direct)
    for token in token_entries(data):
        if token_entry_id(token) == token_id:
            outcome = first_value(token, "outcome", "name")
            return None if outcome is None else str(outcome)
    return None


def token_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("tokens", "outcomes", "clobTokens"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def token_entry_id(token: dict[str, Any]) -> str | None:
    value = first_value(token, "token_id", "tokenId", "clobTokenId", "asset_id", "asset")
    if value is None:
        return None
    token_id = str(value).strip()
    return token_id or None


def first_value(data: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None
