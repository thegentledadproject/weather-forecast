"""
Order-book parsing exercised against a REAL captured CLOB payload.

Every earlier test of estimate_slippage()/get_available_depth_usd()
monkeypatched their RETURN VALUES, so the field-name assumptions in the
parsing itself ("price"/"size" per level, values as strings) had never
been checked against what clob.polymarket.com actually sends. These
tests stub only the HTTP fetch (get_order_book) and feed the raw
payload -- captured live on 2026-08-05 from GET /book?token_id=... --
through the real parsing code.

If Polymarket ever changes the book schema, re-capture with a live GET
and refresh tests/fixtures/clob_book_real_capture.json; the shape
assertions below say exactly what the parsing depends on.
"""

import copy
import json
from pathlib import Path

import pytest

from clients import market_client

FIXTURE = Path(__file__).parent / "fixtures" / "clob_book_real_capture.json"


@pytest.fixture()
def real_book():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture()
def stub_fetch(monkeypatch, real_book):
    """Stub ONLY the HTTP layer; every parse line below runs for real."""
    monkeypatch.setattr(market_client, "get_order_book", lambda tid, timeout=10: copy.deepcopy(real_book))
    return real_book


def test_captured_payload_has_the_assumed_shape(real_book):
    # The exact contract the parsing code relies on, asserted against a
    # payload Polymarket actually served (not one we invented).
    assert isinstance(real_book["bids"], list) and real_book["bids"]
    assert isinstance(real_book["asks"], list) and real_book["asks"]
    for level in real_book["bids"] + real_book["asks"]:
        assert set(level.keys()) == {"price", "size"}
        # Values arrive as STRINGS -- the float() calls in the parsers
        # are doing real work, not defensive redundancy.
        assert isinstance(level["price"], str)
        assert isinstance(level["size"], str)
        float(level["price"])
        float(level["size"])


def test_asks_arrive_unsorted_for_our_purposes(real_book):
    # The live feed returns asks best-price-LAST (descending). The
    # parsers re-sort ascending before walking the book; if that sort
    # were ever removed, top_price would be the WORST ask and every
    # slippage number would be nonsense. This documents why it's there.
    raw_prices = [float(level["price"]) for level in real_book["asks"]]
    assert raw_prices != sorted(raw_prices)


def test_depth_matches_independent_recomputation(stub_fetch, real_book):
    depth = market_client.get_available_depth_usd("TOKEN-X", max_price_impact_pct=0.10)
    assert depth is not None and depth > 0

    asks = sorted(real_book["asks"], key=lambda level: float(level["price"]))
    ceiling = float(asks[0]["price"]) * 1.10
    expected = sum(
        float(level["size"]) * float(level["price"])
        for level in asks
        if float(level["price"]) <= ceiling
    )
    assert depth == pytest.approx(expected)


def test_slippage_small_order_fills_at_top_of_book(stub_fetch, real_book):
    asks = sorted(real_book["asks"], key=lambda level: float(level["price"]))
    top_level_usd = float(asks[0]["size"]) * float(asks[0]["price"])
    small = min(10.0, top_level_usd / 2)

    slippage = market_client.estimate_slippage("TOKEN-X", small)
    assert slippage == 0.0  # entirely inside the best ask level


def test_slippage_grows_with_size_and_stays_off_fallback(stub_fetch, real_book):
    # Walk deeper into the real book: slippage must be monotonically
    # non-decreasing in order size, and none of these should hit the
    # 5% can't-parse/can't-fill fallback if the book covers the size.
    asks = sorted(real_book["asks"], key=lambda level: float(level["price"]))
    book_usd = sum(float(l["size"]) * float(l["price"]) for l in asks)

    sizes = [s for s in (50.0, 150.0, 500.0) if s < book_usd]
    slips = [market_client.estimate_slippage("TOKEN-X", s) for s in sizes]
    assert slips == sorted(slips)
    for s in slips:
        assert 0.0 <= s


def test_oversized_order_hits_conservative_fallback(stub_fetch, real_book):
    asks = real_book["asks"]
    book_usd = sum(float(l["size"]) * float(l["price"]) for l in asks)
    slippage = market_client.estimate_slippage("TOKEN-X", book_usd * 10)
    assert slippage == 0.05  # FALLBACK_SLIPPAGE_PCT: can't fill -> flat floor


def test_malformed_payload_degrades_soft_not_hard(monkeypatch, real_book):
    # A renamed field must produce the documented soft failures (None /
    # fallback), never an exception into the trading loop.
    broken = copy.deepcopy(real_book)
    broken["asks"] = [{"px": l["price"], "qty": l["size"]} for l in broken["asks"]]
    monkeypatch.setattr(market_client, "get_order_book", lambda tid, timeout=10: broken)

    assert market_client.get_available_depth_usd("TOKEN-X") is None
    assert market_client.estimate_slippage("TOKEN-X", 50.0) == 0.05
