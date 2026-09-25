"""Deterministic risk guard. Every order the agent wants to place goes through ``RiskGuard.check``.

The guard is the safety boundary: it works only from a fresh account snapshot fetched by
the caller (never from anything the model claims) and from limits fixed in config.

Position sign convention (matches Kalshi): positive = YES contracts held, negative = NO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]

TRADABLE_STATUSES = {"open", "active"}


@dataclass(frozen=True)
class RiskLimits:
    max_order_cost_cents: int = 2500
    max_position_contracts_per_market: int = 100
    max_total_exposure_cents: int = 20000
    max_orders_per_session: int = 20
    max_session_loss_cents: int = 5000
    max_slippage_cents: int = 3
    allowed_ticker_prefixes: tuple[str, ...] = ()
    blocked_ticker_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderIntent:
    ticker: str
    side: Side
    action: Action
    count: int
    price_cents: int

    @property
    def position_delta(self) -> int:
        """Change in signed position if this order fully fills."""
        yes_direction = 1 if self.side == "yes" else -1
        return self.count * yes_direction * (1 if self.action == "buy" else -1)

    @property
    def cost_cents(self) -> int:
        return self.count * self.price_cents if self.action == "buy" else 0


@dataclass(frozen=True)
class MarketSnapshot:
    status: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    position: int  # signed position in the order's market
    total_exposure_cents: int  # cost of all open positions + resting buy orders
    account_value_cents: int  # cash balance + marked value of positions


@dataclass
class GuardState:
    session_start_value_cents: int | None = None
    risk_increasing_orders: int = 0
    halted: bool = False


@dataclass(frozen=True)
class Verdict:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    risk_reducing: bool = False

    def __str__(self) -> str:
        if self.approved:
            return "APPROVED" + (" (risk-reducing)" if self.risk_reducing else "")
        return "REJECTED: " + "; ".join(self.reasons)


def is_risk_reducing(order: OrderIntent, position: int) -> bool:
    """True if the order only moves the position toward zero without flipping it."""
    new_position = position + order.position_delta
    return abs(new_position) < abs(position) and (new_position == 0 or
                                                  (new_position > 0) == (position > 0))


class RiskGuard:
    def __init__(self, limits: RiskLimits, *, kill_switch: bool = False):
        self.limits = limits
        self.state = GuardState(halted=kill_switch)

    def halt(self) -> None:
        self.state.halted = True

    def resume(self) -> None:
        self.state.halted = False

    def record_session_start(self, account_value_cents: int) -> None:
        if self.state.session_start_value_cents is None:
            self.state.session_start_value_cents = account_value_cents

    def record_order_placed(self, verdict: Verdict) -> None:
        if not verdict.risk_reducing:
            self.state.risk_increasing_orders += 1

    def check(self, order: OrderIntent, account: AccountSnapshot,
              market: MarketSnapshot) -> Verdict:
        lim = self.limits
        reasons: list[str] = []

        if self.state.halted:
            return Verdict(False, ["trading is halted (kill switch / /halt)"])

        if order.side not in ("yes", "no") or order.action not in ("buy", "sell"):
            return Verdict(False, ["side must be yes|no and action must be buy|sell"])
        if order.count < 1:
            reasons.append("count must be >= 1")
        if not 1 <= order.price_cents <= 99:
            reasons.append("limit price must be between 1 and 99 cents")
        if market.status.lower() not in TRADABLE_STATUSES:
            reasons.append(f"market is not open for trading (status={market.status})")

        held_on_side = (max(account.position, 0) if order.side == "yes"
                        else max(-account.position, 0))
        if order.action == "sell" and order.count > held_on_side:
            reasons.append(
                f"cannot sell {order.count} {order.side.upper()}: only {held_on_side} held "
                "(no short/flip via sell)"
            )
        if reasons:
            return Verdict(False, reasons)

        reducing = is_risk_reducing(order, account.position)

        # Slippage applies to every order, including exits.
        if order.action == "buy":
            ask = market.yes_ask if order.side == "yes" else market.no_ask
            if ask is not None and order.price_cents - ask > lim.max_slippage_cents:
                reasons.append(f"limit {order.price_cents}c is more than "
                               f"{lim.max_slippage_cents}c above best ask {ask}c")
        else:
            bid = market.yes_bid if order.side == "yes" else market.no_bid
            if bid is not None and bid - order.price_cents > lim.max_slippage_cents:
                reasons.append(f"limit {order.price_cents}c is more than "
                               f"{lim.max_slippage_cents}c below best bid {bid}c")

        if reducing:
            return Verdict(not reasons, reasons, risk_reducing=True)

        ticker = order.ticker.upper()
        if any(ticker.startswith(p) for p in lim.blocked_ticker_prefixes):
            reasons.append(f"ticker {order.ticker} is blocked")
        if lim.allowed_ticker_prefixes and not any(
            ticker.startswith(p) for p in lim.allowed_ticker_prefixes
        ):
            reasons.append(f"ticker {order.ticker} is not in the allowlist")
        if self.state.risk_increasing_orders >= lim.max_orders_per_session:
            reasons.append(f"session order limit reached ({lim.max_orders_per_session})")
        if order.cost_cents > lim.max_order_cost_cents:
            reasons.append(f"order cost {order.cost_cents}c exceeds per-order max "
                           f"{lim.max_order_cost_cents}c")
        new_position = account.position + order.position_delta
        if abs(new_position) > lim.max_position_contracts_per_market:
            reasons.append(f"resulting position {abs(new_position)} exceeds per-market max "
                           f"{lim.max_position_contracts_per_market}")
        new_exposure = account.total_exposure_cents + order.cost_cents
        if new_exposure > lim.max_total_exposure_cents:
            reasons.append(f"total exposure would be {new_exposure}c, above max "
                           f"{lim.max_total_exposure_cents}c")
        start = self.state.session_start_value_cents
        if start is not None:
            loss = start - account.account_value_cents
            if loss >= lim.max_session_loss_cents:
                reasons.append(f"session loss {loss}c has hit the max "
                               f"{lim.max_session_loss_cents}c; only risk-reducing orders allowed")

        return Verdict(not reasons, reasons, risk_reducing=False)
