"""
Order-amount precision on the real-money submit path.

THE BUG THIS PINS (2026-08-11..14, 5 of the first 8 live orders killed):
submit_order() built its order with create_order() -- the LIMIT builder,
which allows the maker amount round_config.amount decimals (4 at a 0.01
tick) -- and then posted it with OrderType.FOK. The exchange validates an
FOK order as a MARKET order, and get_market_order_amounts() rounds the
maker amount with round_config.SIZE, i.e. 2 decimals. Every BUY whose
price x shares happened to land past 2 decimals came back:

    invalid amounts, the market buy orders maker amount supports a max
    accuracy of 2 decimals, taker amount a max of 4 decimals

It failed intermittently -- whether the product lands on 2 decimals
depends on the price -- which is exactly the kind of failure that reads
as bad luck rather than a bug.

The fix builds with create_market_order() so the builder matches the
order type, and pre-rounds MarketOrderArgs.amount, which is USDC for a
BUY and SHARES for a SELL.
"""

import math

import pytest

from clients import wallet_client


class _FakeSide:
    BUY = "BUY"
    SELL = "SELL"


class _FakeOrderType:
    FOK = "FOK"
    GTC = "GTC"


class _MarketOrderArgs:
    def __init__(self, token_id, amount, side, price=0, order_type=None):
        self.token_id, self.amount, self.side = token_id, amount, side
        self.price, self.order_type = price, order_type


class _FakeLib:
    Side = _FakeSide
    OrderType = _FakeOrderType
    MarketOrderArgs = _MarketOrderArgs

    class OrderArgs:  # must NOT be used -- limit builder is the bug
        def __init__(self, **kw):
            raise AssertionError("submit_order built a LIMIT order for an FOK post")


class _FakeClient:
    def __init__(self):
        self.built = None

    def create_market_order(self, args):
        self.built = args
        return {"signed": True}

    def create_order(self, args):  # pragma: no cover - must not be called
        raise AssertionError("create_order() must not be used for an FOK order")

    def post_order(self, signed, order_type):
        return {"success": True, "orderID": "0xabc", "status": "matched",
                "makingAmount": "1.00", "takingAmount": "5.00"}


@pytest.fixture()
def submitted(monkeypatch):
    """Capture the MarketOrderArgs submit_order hands the client."""
    client = _FakeClient()
    monkeypatch.setattr(wallet_client, "_clob", lambda: _FakeLib)
    monkeypatch.setattr(wallet_client, "get_client", lambda: client)
    monkeypatch.setattr(wallet_client, "_wait_for_balance", lambda c: True)
    monkeypatch.setattr(wallet_client, "live_trading_enabled", lambda: True)
    return client


def _spec(side="BUY", notional=1.0, shares=5.0, limit=0.30):
    return wallet_client.OrderSpec(
        ok=True, token_id="TOK", side=side, limit_price=limit,
        size_shares=shares, notional_usd=notional,
        requested_size_usd=notional, requested_price=limit,
        expected_price=limit, tick_size="0.01",
    )


def _decimals(x):
    s = repr(float(x))
    return len(s.split(".")[1].rstrip("0")) if "." in s and not s.endswith(".0") else 0


# --- the failing case ------------------------------------------------------

def test_buy_amount_never_exceeds_two_decimals():
    """The exact shape that was rejected: notional landing on 4 decimals."""
    # 14.29 shares @ 0.07 = 1.0003 -- the old path submitted this verbatim.
    spec = _spec(side="BUY", notional=1.0003, shares=14.29, limit=0.07)
    amount = wallet_client._round_up_to_grid(spec.notional_usd, wallet_client.SHARE_DECIMALS)
    assert _decimals(amount) <= 2
    assert amount >= spec.notional_usd  # never truncated below what cleared the gates


def test_buy_submits_usdc_amount_rounded_to_cents(submitted):
    spec = _spec(side="BUY", notional=1.0003, shares=14.29, limit=0.07)
    result = wallet_client.submit_order(spec, live=True)

    args = submitted.built
    assert isinstance(args, _MarketOrderArgs), "must build via create_market_order"
    # BUY amount is USDC (builder.get_market_order_amounts), on the cent grid,
    # and rounded UP so the order is never smaller than what was approved.
    assert _decimals(args.amount) <= 2
    assert args.amount == pytest.approx(1.01)
    assert result.submitted is True


def test_sell_submits_share_count_floored(submitted):
    # SELL amount is SHARES, floored -- offering shares the position does
    # not hold fails outright, and the dust is worth less than a dead exit.
    spec = _spec(side="SELL", notional=1.6497, shares=5.4991, limit=0.30)
    wallet_client.submit_order(spec, live=True)

    args = submitted.built
    assert args.side == _FakeSide.SELL
    assert _decimals(args.amount) <= 2
    assert args.amount == pytest.approx(5.49)
    assert args.amount <= spec.size_shares


def test_limit_price_and_fok_survive_the_change(submitted):
    """Price protection and fill-or-kill semantics must be unchanged."""
    spec = _spec(side="BUY", notional=1.50, shares=5.0, limit=0.31)
    wallet_client.submit_order(spec, live=True)

    args = submitted.built
    assert args.price == 0.31           # padded, tick-aligned limit passed through
    assert args.order_type == _FakeOrderType.FOK
    assert args.token_id == "TOK"


@pytest.mark.parametrize("shares,price", [
    (14.29, 0.07), (3.03, 0.33), (5.0, 0.30), (7.77, 0.13), (33.34, 0.03),
])
def test_no_price_share_combination_produces_an_illegal_amount(shares, price):
    """
    The old failure was intermittent across prices. Sweep the shape of it:
    whatever the raw notional, the submitted BUY amount is always legal.
    """
    notional = shares * price
    amount = wallet_client._round_up_to_grid(notional, wallet_client.SHARE_DECIMALS)
    assert _decimals(amount) <= 2
    assert amount >= notional - 1e-9


def test_unresolved_spec_still_refuses(submitted):
    spec = _spec()
    spec.ok = False
    spec.reason = "book unavailable"
    result = wallet_client.submit_order(spec, live=True)
    assert result.submitted is False and submitted.built is None
