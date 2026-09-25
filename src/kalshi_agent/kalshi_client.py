"""Minimal signed client for the Kalshi Trade API v2.

Every request is signed with RSA-PSS (SHA-256) over ``timestamp_ms + METHOD + path``,
where ``path`` is the full URL path (including ``/trade-api/v2``) without the query string.

Kalshi has been migrating numeric fields from integer cents / integer counts to
``*_dollars`` / ``*_fp`` string fields. The ``cents()`` / ``contracts()`` helpers read
either form so the rest of the code can work in integer cents and whole contracts.
"""

from __future__ import annotations

import base64
import time
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

JSON = dict[str, Any]


class KalshiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Kalshi API error {status}: {message}")
        self.status = status
        self.message = message


def cents(obj: JSON, key: str, default: int | None = 0) -> int | None:
    """Read a price/money field as integer cents from ``key`` or ``key_dollars``."""
    if obj.get(key) is not None:
        return int(obj[key])
    dollars = obj.get(f"{key}_dollars")
    if dollars is not None:
        return int((Decimal(str(dollars)) * 100).to_integral_value())
    return default


def contracts(obj: JSON, key: str, default: int = 0) -> int:
    """Read a contract-count field from ``key`` or ``key_fp``."""
    if obj.get(key) is not None:
        return int(obj[key])
    fp = obj.get(f"{key}_fp")
    if fp is not None:
        return int(Decimal(str(fp)))
    return default


def load_private_key(path: str) -> rsa.RSAPrivateKey:
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("Kalshi private key must be an RSA key")
    return key


def sign_message(private_key: rsa.RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def auth_headers(
    private_key: rsa.RSAPrivateKey, key_id: str, method: str, url: str, now_ms: int | None = None
) -> dict[str, str]:
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    path = urlsplit(url).path
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign_message(private_key, ts + method.upper() + path),
    }


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
    ):
        self._key_id = key_id
        self._private_key = private_key
        self._max_retries = max_retries
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=15.0,
                                  transport=transport)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ core

    def _request(self, method: str, path: str, *, params: JSON | None = None,
                 json: JSON | None = None) -> JSON:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(self._max_retries + 1):
            request = self._http.build_request(method, path.lstrip("/"), params=params, json=json)
            request.headers.update(
                auth_headers(self._private_key, self._key_id, method, str(request.url))
            )
            try:
                resp = self._http.send(request)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise KalshiError(0, f"network error: {e}") from e
                time.sleep(2**attempt * 0.5)
                continue
            # Only GETs are retried on 5xx; a retried POST could double-submit an order.
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and method == "GET")
            if retryable and attempt < self._max_retries:
                time.sleep(2**attempt * 0.5)
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text[:500])
            return resp.json() if resp.content else {}
        raise AssertionError("unreachable")

    def _paginate(self, path: str, key: str, params: JSON, limit: int) -> list[JSON]:
        out: list[JSON] = []
        cursor = None
        while len(out) < limit:
            data = self._request("GET", path, params={**params, "cursor": cursor,
                                                       "limit": min(200, limit - len(out))})
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out[:limit]

    # ------------------------------------------------------------- portfolio

    def get_balance(self) -> JSON:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, limit: int = 1000) -> list[JSON]:
        """Market positions with a non-zero position or resting orders."""
        positions = self._paginate("/portfolio/positions", "market_positions",
                                   {"count_filter": "position,total_traded"}, limit)
        return [p for p in positions
                if contracts(p, "position") != 0 or contracts(p, "resting_orders_count") > 0]

    def get_orders(self, status: str | None = "resting", ticker: str | None = None,
                   limit: int = 1000) -> list[JSON]:
        return self._paginate("/portfolio/orders", "orders",
                              {"status": status, "ticker": ticker}, limit)

    def get_fills(self, ticker: str | None = None, limit: int = 50) -> list[JSON]:
        return self._paginate("/portfolio/fills", "fills", {"ticker": ticker}, limit)

    def create_order(self, *, ticker: str, side: str, action: str, count: int,
                     price_cents: int) -> JSON:
        body: JSON = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "client_order_id": str(uuid.uuid4()),
            ("yes_price" if side == "yes" else "no_price"): price_cents,
        }
        return self._request("POST", "/portfolio/orders", json=body).get("order", {})

    def cancel_order(self, order_id: str) -> JSON:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    # --------------------------------------------------------------- markets

    def get_market(self, ticker: str) -> JSON:
        return self._request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> JSON:
        return self._request("GET", f"/markets/{ticker}/orderbook",
                             params={"depth": depth}).get("orderbook", {})

    def get_event(self, event_ticker: str) -> JSON:
        return self._request("GET", f"/events/{event_ticker}")

    def search_markets(self, *, status: str | None = "open", event_ticker: str | None = None,
                       series_ticker: str | None = None, limit: int = 50) -> list[JSON]:
        return self._paginate("/markets", "markets", {"status": status,
                                                      "event_ticker": event_ticker,
                                                      "series_ticker": series_ticker}, limit)


def best_prices(orderbook: JSON) -> dict[str, int | None]:
    """Best bid/ask in cents for each side from a Kalshi orderbook.

    Kalshi books list only bids: ``yes`` = bids for YES, ``no`` = bids for NO.
    A YES ask at p is equivalent to a NO bid at 100 - p.
    """

    def levels(side: str) -> list[int]:
        raw = orderbook.get(side)
        if raw:
            return [int(lvl[0]) for lvl in raw]
        raw = orderbook.get(f"{side}_dollars") or []
        return [int((Decimal(str(lvl[0])) * 100).to_integral_value()) for lvl in raw]

    yes_bids, no_bids = levels("yes"), levels("no")
    yes_bid = max(yes_bids) if yes_bids else None
    no_bid = max(no_bids) if no_bids else None
    return {
        "yes_bid": yes_bid,
        "yes_ask": 100 - no_bid if no_bid is not None else None,
        "no_bid": no_bid,
        "no_ask": 100 - yes_bid if yes_bid is not None else None,
    }
