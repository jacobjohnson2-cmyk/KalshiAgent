import json
from types import SimpleNamespace

import httpx
import pytest

from kalshi_agent.agent import sanitize_assistant_content
from kalshi_agent.audit import AuditLog
from kalshi_agent.kalshi_client import KalshiClient
from kalshi_agent.risk import RiskGuard, RiskLimits
from kalshi_agent.tools import Toolbox, tool_definitions


class FakeKalshi:
    """Tiny in-memory stand-in for the Kalshi REST API, served via httpx.MockTransport."""

    def __init__(self):
        self.position = 0
        self.exposure = 0
        self.orders_posted = []
        self.cancelled = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance": 10000, "portfolio_value": 0})
        if path == "/portfolio/positions":
            mp = ([{"ticker": "KXTEST-1", "position": self.position,
                    "market_exposure": self.exposure, "realized_pnl": 0, "fees_paid": 0,
                    "resting_orders_count": 0}] if self.position else [])
            return httpx.Response(200, json={"market_positions": mp, "cursor": ""})
        if path == "/portfolio/orders" and request.method == "GET":
            return httpx.Response(200, json={"orders": [], "cursor": ""})
        if path == "/portfolio/orders" and request.method == "POST":
            body = json.loads(request.content)
            self.orders_posted.append(body)
            return httpx.Response(201, json={"order": {"order_id": "ord-1", "status": "resting",
                                                        "remaining_count": body["count"],
                                                        "fill_count": 0}})
        if path.startswith("/portfolio/orders/") and request.method == "DELETE":
            self.cancelled.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"order": {"status": "canceled"}})
        if path == "/markets/KXTEST-1":
            return httpx.Response(200, json={"market": {
                "ticker": "KXTEST-1", "title": "Test market", "status": "active",
                "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200",
                "no_bid_dollars": "0.5800", "no_ask_dollars": "0.6000",
                "close_time": "2026-12-31T00:00:00Z"}})
        if path == "/markets/KXTEST-1/orderbook":
            return httpx.Response(200, json={"orderbook": {"yes": [[40, 100]],
                                                           "no": [[58, 100]]}})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def setup(rsa_key, tmp_path):
    fake = FakeKalshi()
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "kid", rsa_key,
                          transport=httpx.MockTransport(fake))
    events = []
    guard = RiskGuard(RiskLimits(max_order_cost_cents=1000))
    audit_path = tmp_path / "audit.jsonl"
    tb = Toolbox(client, guard, AuditLog(str(audit_path), "demo"), dry_run=False,
                 on_trade_event=lambda k, m: events.append((k, m)))
    tb.start_session()
    return SimpleNamespace(fake=fake, tb=tb, events=events, audit_path=audit_path)


def _place(tb, **kw):
    args = {"ticker": "KXTEST-1", "side": "yes", "action": "buy", "count": 5,
            "limit_price_cents": 42, "reason": "test", **kw}
    text, is_error = tb.run("place_order", args)
    return json.loads(text) if not is_error else text, is_error


def test_tool_definitions_are_strict_and_complete():
    defs = tool_definitions()
    names = {d["name"] for d in defs}
    assert {"place_order", "cancel_order", "get_portfolio_summary"} <= names
    for d in defs:
        schema = d["input_schema"]
        assert d["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_approved_order_is_sent_and_audited(setup):
    result, is_error = _place(setup.tb)
    assert not is_error and result["status"] == "placed"
    posted = setup.fake.orders_posted[0]
    assert posted["yes_price"] == 42 and posted["type"] == "limit" and posted["count"] == 5
    assert setup.events[0][0] == "placed"
    events = [json.loads(line)["event"] for line in setup.audit_path.read_text().splitlines()]
    assert events == ["session_start", "order_check", "order_placed"]


def test_oversized_order_rejected_and_not_sent(setup):
    result, is_error = _place(setup.tb, count=50)
    assert not is_error and result["status"] == "rejected_by_risk_guard"
    assert setup.fake.orders_posted == []
    assert setup.events[0][0] == "rejected"


def test_dry_run_never_sends(setup):
    setup.tb.dry_run = True
    result, _ = _place(setup.tb)
    assert result["status"] == "dry_run_not_sent"
    _, _ = setup.tb.run("cancel_order", {"order_id": "abc", "reason": "x"})
    assert setup.fake.orders_posted == [] and setup.fake.cancelled == []


def test_exit_uses_live_position_not_model_claims(setup):
    setup.fake.position, setup.fake.exposure = 20, 800
    result, _ = _place(setup.tb, action="sell", count=20, limit_price_cents=40)
    assert result["status"] == "placed"
    result, _ = _place(setup.tb, action="sell", count=21, limit_price_cents=40)
    assert result["status"] == "rejected_by_risk_guard"


def test_invalid_input_is_tool_error(setup):
    text, is_error = setup.tb.run("place_order", {"ticker": "KXTEST-1", "side": "maybe"})
    assert is_error and "Invalid input" in text
    text, is_error = setup.tb.run("nope", {})
    assert is_error


def test_portfolio_summary_marks_positions(setup):
    setup.fake.position, setup.fake.exposure = 10, 350
    text, is_error = setup.tb.run("get_portfolio_summary", {})
    s = json.loads(text)
    p = s["positions"][0]
    assert not is_error and p["held_side"] == "yes" and p["yes_bid"] == 40
    assert p["marked_value_cents"] == 400 and p["unrealized_pnl_cents_est"] == 50


def test_kalshi_error_surfaces_as_tool_error(setup):
    text, is_error = setup.tb.run("get_market", {"ticker": "MISSING"})
    assert is_error and "404" in text


def test_sanitize_drops_pre_fallback_internal_blocks():
    b = lambda t: SimpleNamespace(type=t)  # noqa: E731
    content = [b("thinking"), b("text"), b("tool_use"), b("fallback"), b("thinking"),
               b("tool_use")]
    out = [x.type for x in sanitize_assistant_content(content)]
    assert out == ["text", "fallback", "thinking", "tool_use"]
    out = [x.type for x in sanitize_assistant_content([b("text"), b("tool_use")],
                                                      drop_tool_use=True)]
    assert out == ["text"]
