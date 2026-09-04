"""
tests/test_pad_in_ev.py

P1-2 · charge the limit pad against expected value.

THE DEFECT. Three separate things consume the slippage budget on the way to
a submitted order:

  1. tick alignment  -- rounding the quote onto the exchange's grid,
  2. _pad_limit()    -- widening the limit by up to LIVE_LIMIT_PAD_MAX_PCT
                        so one adverse tick does not kill the order,
  3. book-walk slippage -- what estimate_slippage() measures.

They are measured at two different layers and never summed. _resolved_size_ok
re-derives net EV as `net_ev - (slippage_new - slippage_old)`, so only (3)
ever reaches the number the approval is tested against. The pad -- up to 3% of
the price, on a book where the whole net-EV bar is often 15% -- is invisible
to the gate that is supposed to be checking "is this still the trade that was
approved".

THE PAD IS A WORST CASE, NOT AN EXPECTATION, and that matters for how this
should be read. A Polymarket limit is a worst-price bound and the FOK fills at
the best price available up to it, so on a book that has not moved the pad
costs exactly nothing. Subtracting it in full is therefore CONSERVATIVE -- it
tests the entry against the worst price we have agreed to accept rather than
the price we expect to pay. That is the correct direction for a gate that
spends money, and it is stated rather than implied.

WHAT THESE TESTS PIN.

  * pad_cost_pct is the pad as a fraction of the expected price, and is zero
    when the builder did not pad.
  * The re-derived net EV is reduced by it, and is tested against
    decision.min_net_ev exactly as before -- the bar is not moved, only the
    number it is compared against.
  * The three components are logged SEPARATELY, so a journal reader can see
    which one ate the budget rather than only that something did.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
from clients import market_client, wallet_client
from models import EntryDecision


@pytest.fixture(autouse=True)
def _gates_that_are_not_under_test(monkeypatch):
    """
    Depth, slippage and the day budget wide open, so net EV is the only thing
    that can refuse an order here.
    """
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    monkeypatch.setattr(entry_manager, "station_day_exposure_usd",
                        lambda icao, target_date, is_paper=None: 0.0)
    monkeypatch.setattr(entry_manager, "portfolio_day_exposure_usd",
                        lambda is_paper=None, region=None: 0.0)


def _decision(net_ev=0.16, bar=0.15, slippage=0.01):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 3), bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=1.00, available_depth_usd=100_000.0,
        slippage_at_size_pct=slippage, net_ev_at_size=net_ev, min_net_ev=bar,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id="TOK",
    )


def _spec(expected_price=0.30, limit_price=0.30, notional=1.00):
    return wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=limit_price,
        size_shares=notional / expected_price, notional_usd=notional,
        expected_price=expected_price, tick_size="0.01",
    )


# ---------------------------------------------------------------------------
# pad_cost_pct itself
# ---------------------------------------------------------------------------

def test_pad_cost_pct_is_the_pad_as_a_fraction_of_the_expected_price():
    """A 2-tick pad on a $0.30 share: 0.32 vs 0.30 is 6.67%."""
    spec = _spec(expected_price=0.30, limit_price=0.32)
    assert spec.pad_cost_pct == pytest.approx(0.0666667, abs=1e-6)


def test_pad_cost_pct_is_zero_when_the_builder_did_not_pad():
    """
    _pad_limit returns the unpadded price when the cap lands below one tick,
    which is the common case on a cheap share. That must read as zero cost,
    not as a missing value.
    """
    assert _spec(expected_price=0.30, limit_price=0.30).pad_cost_pct == 0.0


def test_pad_cost_pct_is_zero_when_no_expected_price_was_recorded():
    """
    expected_price defaults to 0.0 on a spec built before the field existed
    or by a refusal. Dividing by it would raise inside a gate; the honest
    answer is "no pad measurable", and the gate then behaves exactly as it
    did before this change.
    """
    spec = wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=0.32,
        size_shares=3.0, notional_usd=1.00,
    )
    assert spec.pad_cost_pct == 0.0


def test_pad_cost_pct_is_never_negative():
    """
    A SELL pads DOWN, so limit < expected. The pad is a cost either way --
    accepting less on a sale is the same direction of harm as paying more on
    a buy -- so the magnitude is what is charged.
    """
    spec = _spec(expected_price=0.30, limit_price=0.28)
    assert spec.pad_cost_pct == pytest.approx(0.0666667, abs=1e-6)


# ---------------------------------------------------------------------------
# The pad reaching the approval test
# ---------------------------------------------------------------------------

def test_a_decision_at_the_bar_is_refused_once_a_two_tick_pad_is_charged():
    """
    THE ACCEPTANCE CASE. Net EV 0.16 against a 0.15 bar: 1 point of headroom,
    and a 2-tick pad on a $0.30 share costs 6.7 points. Today this is
    approved because the pad never enters the number.
    """
    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32), _decision(net_ev=0.16, bar=0.15)
    )

    assert not ok
    assert "net EV" in note


def test_the_same_decision_is_approved_with_a_sub_tick_pad():
    """
    The other half of the acceptance case: no pad, no charge, and the entry
    stands exactly as it did before this change.
    """
    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.30), _decision(net_ev=0.16, bar=0.15)
    )

    assert ok


def test_the_bar_itself_is_not_moved():
    """
    The pad is charged against the NUMBER, never added to the BAR. A decision
    with enough headroom to absorb the pad is still approved against its own
    min_net_ev -- 0.30 net EV less 6.7 points of pad is 0.233, over the 0.15
    bar.
    """
    ok, _ = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32), _decision(net_ev=0.30, bar=0.15)
    )

    assert ok


def test_the_pad_is_charged_on_top_of_the_slippage_increase(monkeypatch):
    """
    The two are separate costs and both must land. Slippage rises 1 -> 9
    points (8 points of cost) and the pad adds 6.7: together 14.7 points off
    28, leaving 13.3 against a 15-point bar. EITHER COST ALONE WOULD PASS
    (20.0 and 21.3 respectively), which is what makes this a test of the sum
    rather than of either term.
    """
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.09)

    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32),
        _decision(net_ev=0.28, bar=0.15, slippage=0.01),
    )

    assert not ok

    # Each cost alone clears the bar, so the refusal above is the sum.
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.09)
    slippage_only, _ = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.30),
        _decision(net_ev=0.28, bar=0.15, slippage=0.01),
    )
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)
    pad_only, _ = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32),
        _decision(net_ev=0.28, bar=0.15, slippage=0.01),
    )
    assert slippage_only and pad_only


def test_a_decision_carrying_no_bar_still_has_the_pad_charged():
    """
    manual_trigger leaves net_ev_at_size None and skips the test entirely, but
    a decision with a net EV and no bar falls back to the positive floor. The
    pad must be charged there too -- otherwise the weaker test is also the
    laxer one.
    """
    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32),
        _decision(net_ev=0.05, bar=None),
    )

    assert not ok
    assert "positive floor" in note


# ---------------------------------------------------------------------------
# The three components, logged separately
# ---------------------------------------------------------------------------

def test_the_resize_note_names_the_pad_separately_from_slippage():
    """
    "something ate the budget" is not actionable. The note has to say which
    of the three components did, because the fix differs: a tick-alignment
    cost is structural, a pad cost is a config knob
    (LIVE_LIMIT_PAD_MAX_PCT), and book-walk slippage is the market.
    """
    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32), _decision(net_ev=0.40, bar=0.15)
    )

    assert ok
    assert "pad" in note.lower()
    assert "slippage" in note.lower()


def test_the_note_reports_the_pad_cost_as_a_percentage():
    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.32), _decision(net_ev=0.40, bar=0.15)
    )

    assert ok
    assert "6.7%" in note


def test_an_unpadded_order_does_not_clutter_the_note_with_a_zero():
    ok, note = executor._resolved_size_ok(
        _spec(expected_price=0.30, limit_price=0.30), _decision(net_ev=0.40, bar=0.15)
    )

    assert ok
    assert "pad" not in note.lower()
