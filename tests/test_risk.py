import pytest

from kalshi_agent.risk import (
    AccountSnapshot,
    MarketSnapshot,
    OrderIntent,
    RiskGuard,
    RiskLimits,
    is_risk_reducing,
)

LIMITS = RiskLimits(
    max_order_cost_cents=1000,
    max_position_contracts_per_market=50,
    max_total_exposure_cents=5000,
    max_orders_per_session=3,
    max_session_loss_cents=2000,
    max_slippage_cents=2,
)
BOOK = MarketSnapshot(status="active", yes_bid=40, yes_ask=42, no_bid=58, no_ask=60)
FLAT = AccountSnapshot(position=0, total_exposure_cents=0, account_value_cents=10000)


def order(side="yes", action="buy", count=10, price=42, ticker="KXTEST-1"):
    return OrderIntent(ticker=ticker, side=side, action=action, count=count, price_cents=price)


def guard(limits=LIMITS, start=10000):
    g = RiskGuard(limits)
    g.record_session_start(start)
    return g


def test_basic_order_approved():
    v = guard().check(order(), FLAT, BOOK)
    assert v.approved and not v.risk_reducing


@pytest.mark.parametrize(
    "count,price,ok", [(23, 43, True), (25, 40, True), (24, 42, False), (26, 40, False)]
)
def test_per_order_cost_boundary(count, price, ok):
    lim = RiskLimits(**{**LIMITS.__dict__, "max_slippage_cents": 99})
    v = guard(lim).check(order(count=count, price=price), FLAT, BOOK)
    assert v.approved is ok


def test_position_limit_counts_existing_position():
    acct = AccountSnapshot(position=45, total_exposure_cents=0, account_value_cents=10000)
    assert guard().check(order(count=5, price=10), acct, BOOK).approved
    v = guard().check(order(count=6, price=10), acct, BOOK)
    assert not v.approved and "per-market max" in v.reasons[0]


def test_no_side_position_limit():
    acct = AccountSnapshot(position=-48, total_exposure_cents=0, account_value_cents=10000)
    v = guard().check(order(side="no", count=3, price=60), acct, BOOK)
    assert not v.approved


def test_total_exposure_limit():
    acct = AccountSnapshot(position=0, total_exposure_cents=4600, account_value_cents=10000)
    assert guard().check(order(count=9, price=42), acct, BOOK).approved  # 378 -> 4978
    assert not guard().check(order(count=10, price=42), acct, BOOK).approved  # 5020


def test_slippage_on_buys_and_sells():
    assert guard().check(order(price=44), FLAT, BOOK).approved
    assert not guard().check(order(price=45), FLAT, BOOK).approved
    held = AccountSnapshot(position=10, total_exposure_cents=400, account_value_cents=10000)
    assert guard().check(order(action="sell", count=5, price=38), held, BOOK).approved
    v = guard().check(order(action="sell", count=5, price=37), held, BOOK)
    assert not v.approved and "below best bid" in v.reasons[0]


def test_slippage_skipped_when_side_has_no_liquidity():
    empty = MarketSnapshot(status="open")
    assert guard().check(order(price=90, count=1), FLAT, empty).approved


def test_cannot_sell_more_than_held_or_sell_unheld_side():
    held = AccountSnapshot(position=5, total_exposure_cents=200, account_value_cents=10000)
    assert not guard().check(order(action="sell", count=6, price=40), held, BOOK).approved
    assert not guard().check(order(side="no", action="sell", count=1, price=58), held,
                             BOOK).approved


def test_risk_reducing_bypasses_size_exposure_allowlist_and_loss_limits():
    lim = RiskLimits(**{**LIMITS.__dict__, "allowed_ticker_prefixes": ("KXOTHER",)})
    g = guard(lim, start=10000)
    acct = AccountSnapshot(position=100, total_exposure_cents=99999, account_value_cents=1000)
    v = g.check(order(action="sell", count=100, price=40), acct, BOOK)
    assert v.approved and v.risk_reducing
    # buying the opposite side up to the held size also nets the position down
    v = g.check(order(side="no", action="buy", count=30, price=60), acct, BOOK)
    assert v.approved and v.risk_reducing
    # but not the same order on a flat account
    assert not g.check(order(action="buy", count=100, price=42), FLAT, BOOK).approved


def test_is_risk_reducing_rejects_flips():
    assert is_risk_reducing(order(side="no", count=10), 10)
    assert not is_risk_reducing(order(side="no", count=11), 10)
    assert is_risk_reducing(order(side="yes", action="sell", count=3), 5)
    assert not is_risk_reducing(order(), 0)


def test_allow_and_block_lists():
    lim = RiskLimits(**{**LIMITS.__dict__, "allowed_ticker_prefixes": ("KXFED",),
                        "blocked_ticker_prefixes": ("KXFEDX",)})
    assert guard(lim).check(order(ticker="kxfed-25dec"), FLAT, BOOK).approved
    assert not guard(lim).check(order(ticker="KXNBA-1"), FLAT, BOOK).approved
    assert not guard(lim).check(order(ticker="KXFEDX-1"), FLAT, BOOK).approved


def test_session_order_limit_counts_only_risk_increasing():
    g = guard()
    for _ in range(3):
        v = g.check(order(count=1), FLAT, BOOK)
        assert v.approved
        g.record_order_placed(v)
    assert not g.check(order(count=1), FLAT, BOOK).approved
    held = AccountSnapshot(position=3, total_exposure_cents=126, account_value_cents=10000)
    assert g.check(order(action="sell", count=3, price=40), held, BOOK).approved


def test_session_loss_blocks_new_risk():
    g = guard(start=10000)
    down = AccountSnapshot(position=0, total_exposure_cents=0, account_value_cents=8000)
    v = g.check(order(count=1), down, BOOK)
    assert not v.approved and "session loss" in v.reasons[0]
    ok = AccountSnapshot(position=0, total_exposure_cents=0, account_value_cents=8001)
    assert g.check(order(count=1), ok, BOOK).approved


def test_halt_blocks_everything_including_exits():
    g = guard()
    g.halt()
    held = AccountSnapshot(position=5, total_exposure_cents=200, account_value_cents=10000)
    assert not g.check(order(action="sell", count=5, price=40), held, BOOK).approved
    g.resume()
    assert g.check(order(action="sell", count=5, price=40), held, BOOK).approved


def test_kill_switch_starts_halted():
    assert not RiskGuard(LIMITS, kill_switch=True).check(order(), FLAT, BOOK).approved


@pytest.mark.parametrize("status", ["closed", "settled", "unopened", "finalized"])
def test_market_must_be_open(status):
    assert not guard().check(order(), FLAT, MarketSnapshot(status=status)).approved


@pytest.mark.parametrize("price", [0, 100, -5])
def test_price_bounds(price):
    assert not guard().check(order(price=price), FLAT, BOOK).approved


def test_zero_count_rejected():
    assert not guard().check(order(count=0), FLAT, BOOK).approved
