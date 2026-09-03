"""
The stop's basis is recorded, not just used.

THE DEFECT THIS PINS
---------------------
risk_manager.stop_basis_price() (see its docstring) picks between the
entry-side bid and the entry ask, and that choice silently narrows the stop
distance whenever no usable bid was recorded (pre-entry_bid rows,
manual_trigger). A bare float return means nothing downstream can tell which
basis fired -- the STORED row looks identical whether the stop measured from
a real bid or fell back to the ask.

Measured (see risk_manager.py's evaluate_exit docstring): of 207 scoreable
stop fires, 117 would not fire on the bid basis, 46 of those were eventual
winners. That gap is invisible in the record today.

WHAT THIS FILE PINS
--------------------
1. stop_basis_price() returns (price, basis), basis in
   {"entry_bid", "entry_ask_fallback"}.
2. A bid recorded but not usable (usable_entry_bid's own carve-outs -- zero,
   or crossed above the ask) reports "entry_ask_fallback", not "entry_bid":
   the label describes what the stop actually used, not merely whether a
   number was present.
3. evaluate_exit() threads that label onto ExitDecision so a caller can tell
   which basis a stop_loss fired on.
4. executor.close_position() persists the label into the STORED reason text,
   so the audit trail (not just an in-memory decision) shows the fallback.

Does NOT test refusing the stop on the fallback basis -- that is a
deliberately separate, deferred question (see risk_manager.py P1-9 notes).
"""
from datetime import date

import pytest

import config
import executor
import risk_manager
import storage
from models import ExitDecision, Position


ASK = 0.15
BID = 0.10
DISTANCE = 0.30 * 0.15   # config.STOP_LOSS_PCT x risk_unit(0.15)
LOOSE_HOUR = 6            # before config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL


def _position(entry_price: float = ASK, entry_bid=BID, side: str = "YES") -> Position:
    return Position(
        position_id=f"WMKK:2026-08-31:34:{side}:{entry_price}",
        station_icao="WMKK", target_date=date(2026, 8, 31), bucket_c=34,
        side=side, entry_price=entry_price, entry_bid=entry_bid,
        size_usd=10.0, entry_time="2026-08-30T21:01:00+00:00",
        status="open", is_paper=True,
        # ARMED: config.HOLD_TO_SETTLEMENT_MODES disarms the stop entirely on
        # a paper-mode book, which would test nothing here. Same reasoning
        # as tests/test_stop_basis.py's fixture.
        execution_mode="live",
    )


# --------------------------------------------------------------------------
# stop_basis_price() reports which basis it used
# --------------------------------------------------------------------------

def test_a_usable_bid_reports_as_the_entry_bid_basis():
    position = _position(entry_price=0.20, entry_bid=0.18)

    price, basis = risk_manager.stop_basis_price(position)

    assert price == pytest.approx(0.18)
    assert basis == "entry_bid"


def test_no_recorded_bid_reports_as_the_fallback_basis():
    """Rows predating Position.entry_bid, and manual_trigger."""
    position = _position(entry_price=0.20, entry_bid=None)

    price, basis = risk_manager.stop_basis_price(position)

    assert price == pytest.approx(0.20)
    assert basis == "entry_ask_fallback"


def test_a_zero_bid_reports_as_the_fallback_basis_not_entry_bid():
    """
    usable_entry_bid() refuses a bid of exactly 0.0 (a book with asks and no
    bids -- a real, observed shape, not None). A NUMBER was recorded, but the
    stop could not use it, so the label must say "entry_ask_fallback" -- the
    same as if nothing had been recorded at all -- not "entry_bid", which
    would claim a basis the stop never actually measured from.
    """
    position = _position(entry_price=0.30, entry_bid=0.0)

    price, basis = risk_manager.stop_basis_price(position)

    assert price == pytest.approx(0.30)
    assert basis == "entry_ask_fallback"


def test_a_crossed_bid_reports_as_the_fallback_basis_not_entry_bid():
    """usable_entry_bid() refuses a bid above the ask (a stale or moving book
    caught mid-update) for the same reason -- recorded, but not used."""
    position = _position(entry_price=0.20, entry_bid=0.60)

    price, basis = risk_manager.stop_basis_price(position)

    assert price == pytest.approx(0.20)
    assert basis == "entry_ask_fallback"


# --------------------------------------------------------------------------
# evaluate_exit() threads the basis onto the decision it returns
# --------------------------------------------------------------------------

def test_a_stop_fired_on_a_usable_bid_carries_that_basis_on_the_decision():
    with_bid = _position(entry_price=ASK, entry_bid=BID)

    decision = risk_manager.evaluate_exit(
        with_bid, current_price=BID - DISTANCE, local_hour=LOOSE_HOUR,
    )

    assert decision.should_exit is True
    assert decision.reason == "stop_loss"     # unchanged -- see module docstring
    assert decision.stop_basis == "entry_bid"


def test_a_stop_fired_on_the_fallback_carries_that_basis_on_the_decision():
    no_bid = _position(entry_price=ASK, entry_bid=None)

    decision = risk_manager.evaluate_exit(
        no_bid, current_price=ASK - DISTANCE, local_hour=LOOSE_HOUR,
    )

    assert decision.should_exit is True
    assert decision.reason == "stop_loss"
    assert decision.stop_basis == "entry_ask_fallback"


# --------------------------------------------------------------------------
# The basis reaches the STORED row, not just the in-memory decision
# --------------------------------------------------------------------------

def _closed_position(entry_price=0.30, entry_bid=None, size_shares=10.0):
    """A paper position, so executor.close_position() takes the simple
    "record and return" branch -- no order path, no wallet_client stub
    needed. Mirrors tests/test_live_execution.py's make_position()."""
    return Position(
        position_id="p1", station_icao="WSSS", target_date=date(2026, 8, 10),
        bucket_c=32, side="YES", entry_price=entry_price, entry_bid=entry_bid,
        size_usd=3.0, entry_time="2026-08-10T00:00:00+00:00", status="open",
        is_paper=True, size_shares=size_shares, execution_mode="paper",
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture what would be written to storage instead of writing it --
    same shape as test_live_execution.py's fixture of the same name."""
    closed = []
    monkeypatch.setattr(storage, "close_position", lambda **kw: closed.append(kw))
    return closed


def test_a_stop_on_a_usable_bid_stores_entry_bid_in_the_reason(captured):
    decision = ExitDecision(
        position_id="p1", should_exit=True, reason="stop_loss",
        current_price=0.20, pnl_pct=-0.33, stop_basis="entry_bid",
    )

    executor.close_position(_closed_position(entry_bid=0.28), decision)

    assert len(captured) == 1
    assert "entry_bid" in captured[0]["reason"]


def test_a_stop_on_the_fallback_stores_entry_ask_fallback_in_the_reason(captured):
    decision = ExitDecision(
        position_id="p1", should_exit=True, reason="stop_loss",
        current_price=0.20, pnl_pct=-0.33, stop_basis="entry_ask_fallback",
    )

    executor.close_position(_closed_position(entry_bid=None), decision)

    assert len(captured) == 1
    assert "entry_ask_fallback" in captured[0]["reason"]
    # The derived status must NOT carry the label -- it feeds
    # config.COOLDOWN_COUNTED_EXIT_STATUSES ("closed_stop_loss" exactly) and
    # backtest/engine.py's per-reason funnel, both of which match on the
    # literal string.
    assert captured[0]["status"] == "closed_stop_loss"
