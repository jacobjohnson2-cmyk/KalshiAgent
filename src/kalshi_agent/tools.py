"""Tools exposed to Claude. Order tools always pass through the RiskGuard."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .audit import AuditLog
from .kalshi_client import JSON, KalshiClient, KalshiError, best_prices, cents, contracts
from .risk import AccountSnapshot, MarketSnapshot, OrderIntent, RiskGuard

MAX_MARKETS_ENRICHED = 50


# --------------------------------------------------------------- tool inputs


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_Input):
    pass


class TickerInput(_Input):
    ticker: str = Field(min_length=1)


class FillsInput(_Input):
    ticker: str | None
    limit: int = Field(ge=1, le=200)


class SearchInput(_Input):
    event_ticker: str | None
    series_ticker: str | None
    limit: int = Field(ge=1, le=100)


class PlaceOrderInput(_Input):
    ticker: str = Field(min_length=1)
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    count: int = Field(ge=1)
    limit_price_cents: int = Field(ge=1, le=99)
    reason: str


class CancelOrderInput(_Input):
    order_id: str = Field(min_length=1)
    reason: str


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_TICKER = {"type": "string", "description": "Kalshi market ticker, e.g. KXFEDDECISION-25DEC-H0"}
_NULLABLE_STR = {"type": ["string", "null"]}

TOOL_SPECS: list[tuple[str, str, dict[str, Any], type[_Input]]] = [
    ("get_portfolio_summary",
     "Cash balance, account value, and every open position with current bid/ask, marked "
     "value, estimated unrealized P&L, market status and close time. Call this before "
     "making any trading decision.",
     _schema({}), NoInput),
    ("get_open_orders", "All resting (unfilled) orders.", _schema({}), NoInput),
    ("get_recent_fills", "Recent fills (executions), newest first, optionally for one ticker.",
     _schema({"ticker": {**_NULLABLE_STR, "description": "Ticker filter, or null for all"},
              "limit": {"type": "integer", "description": "Max fills to return (1-200)"}}),
     FillsInput),
    ("get_market", "Full details for one market: title, rules, status, prices, volume, "
     "close/expiry time.", _schema({"ticker": _TICKER}), TickerInput),
    ("get_orderbook", "Order book for one market with best bid/ask for YES and NO in cents.",
     _schema({"ticker": _TICKER}), TickerInput),
    ("search_markets", "List open markets, filtered by event and/or series ticker.",
     _schema({"event_ticker": {**_NULLABLE_STR, "description": "Event ticker or null"},
              "series_ticker": {**_NULLABLE_STR, "description": "Series ticker or null"},
              "limit": {"type": "integer", "description": "Max markets (1-100)"}}),
     SearchInput),
    ("get_risk_limits", "The hard risk limits every order is checked against, current "
     "session usage, dry-run and halt status.", _schema({}), NoInput),
    ("place_order",
     "Place a LIMIT order. It is checked against the hard risk limits first and rejected "
     "with the reason if it breaches any. Prices are in cents (1-99) for the chosen side. "
     "To exit a YES position, sell YES; to exit a NO position, sell NO.",
     _schema({"ticker": _TICKER,
              "side": {"type": "string", "enum": ["yes", "no"]},
              "action": {"type": "string", "enum": ["buy", "sell"]},
              "count": {"type": "integer", "description": "Number of contracts (>= 1)"},
              "limit_price_cents": {"type": "integer",
                                    "description": "Limit price in cents for `side` (1-99)"},
              "reason": {"type": "string",
                         "description": "One-sentence rationale, recorded in the audit log"}}),
     PlaceOrderInput),
    ("cancel_order", "Cancel a resting order by id.",
     _schema({"order_id": {"type": "string"},
              "reason": {"type": "string", "description": "Why, recorded in the audit log"}}),
     CancelOrderInput),
]


def tool_definitions() -> list[dict[str, Any]]:
    return [{"name": name, "description": desc, "input_schema": schema, "strict": True,
             "eager_input_streaming": True}
            for name, desc, schema, _ in TOOL_SPECS]


# ------------------------------------------------------------------ toolbox


TradeCallback = Callable[[str, str], None]


class Toolbox:
    def __init__(self, client: KalshiClient, guard: RiskGuard, audit: AuditLog, *,
                 dry_run: bool, on_trade_event: TradeCallback | None = None):
        self.client = client
        self.guard = guard
        self.audit = audit
        self.dry_run = dry_run
        self.on_trade_event = on_trade_event or (lambda kind, msg: None)
        self._inputs = {name: model for name, _, _, model in TOOL_SPECS}

    # Called once at startup so session-loss tracking has a baseline.
    def start_session(self) -> int:
        value = self._account_value(self.client.get_balance())
        self.guard.record_session_start(value)
        self.audit.write("session_start", account_value_cents=value, dry_run=self.dry_run)
        return value

    def run(self, name: str, raw_input: Any) -> tuple[str, bool]:
        """Execute a tool. Returns (result_text, is_error)."""
        model = self._inputs.get(name)
        if model is None:
            return f"Unknown tool: {name}", True
        try:
            args = model.model_validate(raw_input)
        except ValidationError as e:
            return f"Invalid input for {name}: {e}", True
        try:
            result = getattr(self, name)(args)
        except KalshiError as e:
            return str(e), True
        return json.dumps(result, default=str), False

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _account_value(balance: JSON) -> int:
        return (cents(balance, "balance") or 0) + (cents(balance, "portfolio_value") or 0)

    @staticmethod
    def _order_price(order: JSON) -> int:
        key = "yes_price" if order.get("side") == "yes" else "no_price"
        return cents(order, key) or 0

    def _resting_buy_cost(self, orders: list[JSON]) -> int:
        return sum(contracts(o, "remaining_count") * self._order_price(o)
                   for o in orders if o.get("action") == "buy")

    @staticmethod
    def _market_quote(market: JSON) -> dict[str, Any]:
        return {k: cents(market, k, None)
                for k in ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price")}

    # ------------------------------------------------------------- read tools

    def get_portfolio_summary(self, _: NoInput) -> JSON:
        balance = self.client.get_balance()
        positions = self.client.get_positions()
        orders = self.client.get_orders()
        rows = []
        for i, p in enumerate(positions):
            ticker = p.get("ticker")
            pos = contracts(p, "position")
            exposure = cents(p, "market_exposure") or 0
            row: JSON = {
                "ticker": ticker,
                "position": pos,
                "held_side": "yes" if pos > 0 else "no" if pos < 0 else None,
                "contracts": abs(pos),
                "cost_basis_cents": exposure,
                "realized_pnl_cents": cents(p, "realized_pnl"),
                "fees_paid_cents": cents(p, "fees_paid"),
                "resting_orders": contracts(p, "resting_orders_count"),
            }
            if i < MAX_MARKETS_ENRICHED and ticker:
                m = self.client.get_market(ticker)
                quote = self._market_quote(m)
                bid = quote["yes_bid"] if pos > 0 else quote["no_bid"]
                row.update(title=m.get("title"), subtitle=m.get("subtitle"),
                           status=m.get("status"), close_time=m.get("close_time"), **quote)
                if bid is not None and pos:
                    row["marked_value_cents"] = abs(pos) * bid
                    row["unrealized_pnl_cents_est"] = abs(pos) * bid - exposure
            rows.append(row)
        total_exposure = sum(cents(p, "market_exposure") or 0 for p in positions)
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "cash_balance_cents": cents(balance, "balance"),
            "portfolio_value_cents": cents(balance, "portfolio_value"),
            "account_value_cents": self._account_value(balance),
            "position_cost_basis_cents": total_exposure,
            "resting_buy_order_cost_cents": self._resting_buy_cost(orders),
            "positions": rows,
        }

    def get_open_orders(self, _: NoInput) -> JSON:
        orders = self.client.get_orders()
        return {"orders": [{
            "order_id": o.get("order_id"), "ticker": o.get("ticker"), "side": o.get("side"),
            "action": o.get("action"), "price_cents": self._order_price(o),
            "remaining_count": contracts(o, "remaining_count"),
            "created_time": o.get("created_time"), "status": o.get("status"),
        } for o in orders]}

    def get_recent_fills(self, args: FillsInput) -> JSON:
        fills = self.client.get_fills(ticker=args.ticker, limit=args.limit)
        return {"fills": [{
            "ticker": f.get("ticker"), "side": f.get("side"), "action": f.get("action"),
            "count": contracts(f, "count"),
            "price_cents": cents(f, "yes_price" if f.get("side") == "yes" else "no_price"),
            "is_taker": f.get("is_taker"), "created_time": f.get("created_time"),
        } for f in fills]}

    def get_market(self, args: TickerInput) -> JSON:
        m = self.client.get_market(args.ticker)
        keep = ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "no_sub_title",
                "status", "open_time", "close_time", "expiration_time", "result",
                "rules_primary", "rules_secondary")
        return {**{k: m.get(k) for k in keep if k in m}, **self._market_quote(m),
                "volume": contracts(m, "volume"), "volume_24h": contracts(m, "volume_24h"),
                "open_interest": contracts(m, "open_interest")}

    def get_orderbook(self, args: TickerInput) -> JSON:
        book = self.client.get_orderbook(args.ticker)
        return {"ticker": args.ticker, **best_prices(book), "raw": book}

    def search_markets(self, args: SearchInput) -> JSON:
        markets = self.client.search_markets(event_ticker=args.event_ticker,
                                             series_ticker=args.series_ticker, limit=args.limit)
        return {"markets": [{"ticker": m.get("ticker"), "title": m.get("title"),
                             "subtitle": m.get("subtitle"), "status": m.get("status"),
                             "close_time": m.get("close_time"), **self._market_quote(m)}
                            for m in markets]}

    def get_risk_limits(self, _: NoInput) -> JSON:
        lim, st = self.guard.limits, self.guard.state
        return {
            "limits": {
                "max_order_cost_cents": lim.max_order_cost_cents,
                "max_position_contracts_per_market": lim.max_position_contracts_per_market,
                "max_total_exposure_cents": lim.max_total_exposure_cents,
                "max_risk_increasing_orders_per_session": lim.max_orders_per_session,
                "max_session_loss_cents": lim.max_session_loss_cents,
                "max_slippage_cents": lim.max_slippage_cents,
                "allowed_ticker_prefixes": list(lim.allowed_ticker_prefixes),
                "blocked_ticker_prefixes": list(lim.blocked_ticker_prefixes),
            },
            "session": {"risk_increasing_orders_placed": st.risk_increasing_orders,
                        "session_start_value_cents": st.session_start_value_cents},
            "halted": st.halted,
            "dry_run": self.dry_run,
            "notes": "Risk-reducing orders (trimming/closing without flipping) bypass the "
                     "size, exposure, allowlist, session-count and loss limits but not the "
                     "slippage limit. Halt blocks everything except cancels.",
        }

    # ------------------------------------------------------------ write tools

    def place_order(self, args: PlaceOrderInput) -> JSON:
        intent = OrderIntent(ticker=args.ticker, side=args.side, action=args.action,
                             count=args.count, price_cents=args.limit_price_cents)

        # Fresh state straight from Kalshi; never trust model-supplied numbers.
        market = self.client.get_market(args.ticker)
        book = best_prices(self.client.get_orderbook(args.ticker))
        quote = self._market_quote(market)
        market_snap = MarketSnapshot(
            status=str(market.get("status", "unknown")),
            **{k: book[k] if book[k] is not None else quote[k]
               for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")},
        )
        positions = self.client.get_positions()
        orders = self.client.get_orders()
        balance = self.client.get_balance()
        position = next((contracts(p, "position") for p in positions
                         if p.get("ticker") == args.ticker), 0)
        account = AccountSnapshot(
            position=position,
            total_exposure_cents=sum(cents(p, "market_exposure") or 0 for p in positions)
            + self._resting_buy_cost(orders),
            account_value_cents=self._account_value(balance),
        )

        verdict = self.guard.check(intent, account, market_snap)
        order_desc = (f"{args.action.upper()} {args.count} {args.side.upper()} {args.ticker} "
                      f"@ {args.limit_price_cents}c")
        self.audit.write("order_check", order=intent.__dict__, reason=args.reason,
                         verdict=str(verdict), position_before=position,
                         market=market_snap.__dict__, dry_run=self.dry_run)

        if not verdict.approved:
            self.on_trade_event("rejected", f"{order_desc} — {'; '.join(verdict.reasons)}")
            return {"status": "rejected_by_risk_guard", "reasons": verdict.reasons}

        if self.dry_run:
            self.guard.record_order_placed(verdict)
            self.on_trade_event("dry_run", f"{order_desc} (dry run, not sent)")
            return {"status": "dry_run_not_sent", "order": intent.__dict__,
                    "risk_reducing": verdict.risk_reducing}

        try:
            order = self.client.create_order(ticker=args.ticker, side=args.side,
                                             action=args.action, count=args.count,
                                             price_cents=args.limit_price_cents)
        except KalshiError as e:
            self.audit.write("order_error", order=intent.__dict__, error=str(e))
            self.on_trade_event("error", f"{order_desc} — {e}")
            raise
        self.guard.record_order_placed(verdict)
        self.audit.write("order_placed", order=intent.__dict__, response=order)
        self.on_trade_event("placed", f"{order_desc} → {order.get('status')} "
                                      f"(id {order.get('order_id')})")
        return {"status": "placed", "order_id": order.get("order_id"),
                "order_status": order.get("status"),
                "filled_count": contracts(order, "fill_count"),
                "remaining_count": contracts(order, "remaining_count")}

    def cancel_order(self, args: CancelOrderInput) -> JSON:
        # Cancels only reduce risk, so they are allowed even when halted.
        self.audit.write("cancel_request", order_id=args.order_id, reason=args.reason,
                         dry_run=self.dry_run)
        if self.dry_run:
            self.on_trade_event("dry_run", f"CANCEL {args.order_id} (dry run, not sent)")
            return {"status": "dry_run_not_sent", "order_id": args.order_id}
        resp = self.client.cancel_order(args.order_id)
        self.audit.write("order_cancelled", order_id=args.order_id, response=resp)
        self.on_trade_event("cancelled", f"CANCEL {args.order_id}")
        return {"status": "cancelled", "order_id": args.order_id}
