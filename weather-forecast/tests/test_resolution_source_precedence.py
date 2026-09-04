"""
tests/test_resolution_source_precedence.py

P1-7 · settle from the observation record FIRST, the book second.

THE DEFECT. When Gamma reports a market closed and the book still quotes,
_check_one_position() called _close_as_resolved() with the book quote, which
rounds at 0.5 to decide who won. The market does not settle on the book. It
settles on the airport thermometer. _close_from_settlement_source() -- the
honest path, which reads this station's own settlement-grade record and maps it
onto the event's live bucket bounds -- ran ONLY when the price feed was down.
So the authoritative source was consulted exactly when it was the last resort,
and ignored whenever a quote happened to be available.

HOW BIG IS IT. The cohort monitor (P0-5) measured this directly over
2026-08-03..09-01: 80 resolution-closed rows booked $21.65 MORE than clean
settlement value. That figure is what this defect is worth over one month, and
it is why the fix makes the P&L record correct rather than more profitable --
the direction happens to have flattered the book.

WHAT MUST NOT REGRESS. Today a resolved market with a readable quote always
closes. After this change the settlement path is tried first, and it can decline
for reasons that have nothing to do with the quote -- no reading published yet,
or event bounds that discovery cannot supply. In every one of those cases the
quote must still close the position. Stranding a position that closes fine today
would be a worse bug than the one being fixed, so most of this file is about the
fallback rather than the preference.

AND THE BASIS STRING IS PART OF THE FIX. "the book said 0.99" and "the airport
thermometer said 31C" are different claims about the same dollar, and the
permanent record has to say which one it used.
"""
from datetime import date, timedelta

import pytest

import config
import position_manager
import storage
from clients import market_client
from models import ObservedReading, Position

STATION = "RKSI"
SETTLED_DAY = date.today() - timedelta(days=2)


def _pos(bucket_c=33, side="YES", entry_price=0.11) -> Position:
    return Position(
        position_id=f"{STATION}:{SETTLED_DAY}:{bucket_c}:{side}:x",
        station_icao=STATION,
        target_date=SETTLED_DAY,
        bucket_c=bucket_c,
        side=side,
        entry_price=entry_price,
        size_usd=7.60,
        entry_time="2026-08-12T20:02:17+00:00",
        status="open",
        token_id="tok",
        execution_mode="paper",
    )


def _reading(temp_c):
    return ObservedReading(
        station_icao=STATION,
        target_date=SETTLED_DAY,
        max_temp_c=temp_c,
        source=config.get_station(STATION).resolution_grade_source,
    )


def _observations(monkeypatch, rows):
    monkeypatch.setattr(storage, "load_observations_since", lambda icao, since: rows)


def _event_bounds(monkeypatch, bounds="config"):
    """None presents an event whose bounds discovery cannot supply."""
    if bounds == "config":
        st = config.get_station(STATION)
        bounds = (st.bucket_min_c, st.bucket_max_c)
    monkeypatch.setattr(position_manager, "_event_bounds", lambda position, station: bounds)


def _no_recorded_settlement(monkeypatch):
    """The second tier absent, so the tests are about tier one vs the quote."""
    monkeypatch.setattr(storage, "load_settled_buckets", lambda icao: {})


def _capture_closes(monkeypatch):
    closed = []
    monkeypatch.setattr(
        position_manager.executor, "close_position",
        lambda position, decision, status=None, exit_reason=None:
            closed.append((position.position_id, decision.current_price, status, exit_reason)),
    )
    return closed


@pytest.fixture(autouse=True)
def _resolved_market(monkeypatch):
    """Gamma says closed, bounds match config, no recorded settlement."""
    monkeypatch.setattr(position_manager, "_market_reported_closed", lambda p: True)
    _event_bounds(monkeypatch)
    _no_recorded_settlement(monkeypatch)


# ---------------------------------------------------------------------------
# The reading wins over the quote
# ---------------------------------------------------------------------------

def test_the_reading_decides_when_it_disagrees_with_the_quote(monkeypatch, capsys):
    """
    THE ACCEPTANCE CASE. The book quotes 0.98 -- which rounds to a WIN -- while
    the thermometer read 31.0C on a position holding bucket 33. The reading
    says this position lost. The reading is what the market settles on.
    """
    _observations(monkeypatch, [_reading(31.0)])
    closed = _capture_closes(monkeypatch)

    decision = position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)

    assert decision is not None and decision.should_exit
    assert decision.reason == "resolution"
    assert closed[0][1] == 0.0, "the quote rounded to a win; the reading says it lost"


def test_the_log_names_the_reading_it_used(monkeypatch, capsys):
    _observations(monkeypatch, [_reading(31.0)])
    _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)
    out = capsys.readouterr().out

    assert "31.0" in out
    assert config.get_station(STATION).resolution_grade_source in out


def test_the_log_also_names_the_quote_it_overrode(monkeypatch, capsys):
    """
    The disagreement is the interesting part of the record. A basis that named
    only the winner would leave a reader unable to tell this close apart from
    one where the two agreed.
    """
    _observations(monkeypatch, [_reading(31.0)])
    _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)
    out = capsys.readouterr().out

    assert "0.98" in out


def test_the_reading_decides_the_other_way_too(monkeypatch):
    """
    The quote rounds to a LOSS at 0.02 while the thermometer says this bucket
    won. Pinned in both directions so nothing can pass by coincidentally
    agreeing with the quote.
    """
    _observations(monkeypatch, [_reading(33.0)])
    closed = _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.02, True)

    assert closed[0][1] == 1.0


def test_the_basis_does_not_claim_the_book_is_gone(monkeypatch, capsys):
    """
    _close_from_settlement_source was written for the feed-down path and its
    basis string opened with "no book left to read". Reached from here that is
    simply false, and a permanent record that misstates why it distrusted the
    book is worse than one that says nothing.
    """
    _observations(monkeypatch, [_reading(31.0)])
    _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)
    out = capsys.readouterr().out

    assert "no book left to read" not in out


# ---------------------------------------------------------------------------
# The quote still closes when the reading cannot
# ---------------------------------------------------------------------------

def test_no_reading_falls_back_to_the_quote(monkeypatch):
    """
    Twelve of thirteen stations publish the next day, but VHHH's source
    publishes a month at a time in arrears. Those positions must keep closing
    on the quote exactly as they do today.
    """
    _observations(monkeypatch, [])
    closed = _capture_closes(monkeypatch)

    decision = position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)

    assert decision is not None
    assert closed[0][1] == 1.0


def test_the_fallback_says_in_the_basis_that_no_reading_existed(monkeypatch, capsys):
    _observations(monkeypatch, [])
    _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)
    out = capsys.readouterr().out

    assert "no settlement-grade reading" in out.lower()


def test_undiscoverable_bounds_fall_back_to_the_quote_rather_than_stranding(monkeypatch):
    """
    THE REGRESSION THIS FILE EXISTS TO PREVENT. A reading exists but discovery
    cannot supply the event's bounds, so the settlement path refuses -- rightly,
    since config's bounds drift and clamping on them can write a winner off as
    a loser. But this position closes fine today on its quote, and leaving it
    open instead would turn a correctness fix into a stranded-position bug.
    """
    _observations(monkeypatch, [_reading(31.0)])
    _event_bounds(monkeypatch, None)
    closed = _capture_closes(monkeypatch)

    decision = position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)

    assert decision is not None, "a readable quote must still close the position"
    assert closed[0][1] == 1.0


def test_a_recorded_settlement_is_still_preferred_over_the_quote(monkeypatch):
    """
    The second tier -- what the exchange actually paid -- outranks the book
    quote for the same reason the first does. Bucket 31 won; the position holds
    33 and the book says 0.98.
    """
    _observations(monkeypatch, [])
    st = config.get_station(STATION)
    monkeypatch.setattr(
        storage, "load_settled_buckets",
        lambda icao: {SETTLED_DAY: (31, st.bucket_min_c, st.bucket_max_c, "src", 1)},
    )
    closed = _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), 0.98, True)

    assert closed[0][1] == 0.0


# ---------------------------------------------------------------------------
# Par-or-nothing, whichever source decided
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quote", [0.02, 0.51, 0.98])
def test_the_exit_price_is_always_par_or_nothing(monkeypatch, quote):
    """
    A resolved market pays 1.0 or 0.0. Whichever tier decided, the recorded
    price must never be the noisy quote that happened to be on the book.
    """
    _observations(monkeypatch, [_reading(33.0)])
    closed = _capture_closes(monkeypatch)

    position_manager._close_resolved_market(_pos(bucket_c=33), quote, True)

    assert closed[0][1] in (0.0, 1.0)


# ---------------------------------------------------------------------------
# The live path actually goes through it
# ---------------------------------------------------------------------------

def test_the_resolution_path_routes_through_the_precedence_helper(monkeypatch):
    """
    The whole change is worthless if _check_one_position still calls
    _close_as_resolved directly. Pinned by source inspection because the
    alternative is standing up the entire price-confirmation path to observe
    one call.
    """
    import inspect

    src = inspect.getsource(position_manager._check_one_position)
    assert "_close_resolved_market(" in src
    assert "_close_as_resolved(" not in src, (
        "the resolved-market branch must go through the precedence helper, "
        "not straight to the quote-based close"
    )
