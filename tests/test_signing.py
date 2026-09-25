import base64

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_agent.kalshi_client import KalshiClient, auth_headers, best_prices, cents, contracts


def _verify(key, signature_b64: str, message: str) -> None:
    key.public_key().verify(
        base64.b64decode(signature_b64),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises InvalidSignature on mismatch


def test_signature_covers_timestamp_method_and_path_without_query(rsa_key):
    url = "https://demo-api.kalshi.co/trade-api/v2/portfolio/orders?status=resting&limit=5"
    h = auth_headers(rsa_key, "key-123", "get", url, now_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    _verify(rsa_key, h["KALSHI-ACCESS-SIGNATURE"],
            "1700000000000GET/trade-api/v2/portfolio/orders")


def test_client_signs_every_request(rsa_key):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"balance": 1234, "portfolio_value": 100})

    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "kid", rsa_key,
                          transport=httpx.MockTransport(handler))
    assert client.get_balance()["balance"] == 1234
    req = seen[0]
    assert req.url.path == "/trade-api/v2/portfolio/balance"
    ts = req.headers["KALSHI-ACCESS-TIMESTAMP"]
    _verify(rsa_key, req.headers["KALSHI-ACCESS-SIGNATURE"],
            ts + "GET/trade-api/v2/portfolio/balance")


def test_post_is_not_retried_on_5xx(rsa_key):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    client = KalshiClient("https://x.test/trade-api/v2", "kid", rsa_key,
                          transport=httpx.MockTransport(handler), max_retries=3)
    try:
        client.create_order(ticker="T", side="yes", action="buy", count=1, price_cents=40)
    except Exception as e:
        assert "503" in str(e)
    assert len(calls) == 1


def test_field_normalization_reads_legacy_and_dollar_fields():
    assert cents({"yes_bid": 42}, "yes_bid") == 42
    assert cents({"yes_bid_dollars": "0.4200"}, "yes_bid") == 42
    assert cents({}, "yes_bid", None) is None
    assert contracts({"position": -3}, "position") == -3
    assert contracts({"position_fp": "7.00"}, "position") == 7


def test_best_prices_derives_asks_from_opposite_bids():
    book = {"yes": [[40, 10], [42, 5]], "no": [[55, 3], [50, 8]]}
    assert best_prices(book) == {"yes_bid": 42, "yes_ask": 45, "no_bid": 55, "no_ask": 58}
    assert best_prices({"yes": None, "no": None})["yes_ask"] is None
    dollars = {"yes_dollars": [["0.42", 5]], "no_dollars": [["0.55", 3]]}
    assert best_prices(dollars)["yes_ask"] == 45
