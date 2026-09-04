"""
tests/test_resolved_size_budget.py

P1-1 · the day budget, re-checked at the size that will actually be submitted.

THE DEFECT. entry_manager.apply_portfolio_budget() scales each leg to fit
what remains of the station/day and portfolio/day budget, and everything
downstream is supposed to be about "the order that actually gets built".
One layer lower, wallet_client.build_entry_order() raises the share count to
the exchange minimum -- up to LIVE_SIZE_OVERSHOOT_CEILING_USD on a $1
request. executor._resolved_size_ok() re-runs depth, slippage and net EV at
that resolved notional. It did not re-run the budgets. _live_budget_breach()
does re-check at resolved size, but it covers only the region caps and only
in live mode.

WHY THIS IS NOT THEORETICAL. Measured on the box over 69 recorded live entry
attempts: 38 of them (55%) resolved more than 5% above the $1.00 fixed size,
with a maximum resolved notional of $3.60 against a $1.00 request. The
exchange-minimum upsizing is ROUTINE, so the unchecked path is exercised
constantly rather than rarely.

WHAT THESE TESTS PIN.

  * The refusal, and that its reason NAMES THE BINDING CAP -- a journal
    reader has to be able to tell a station/day refusal from a portfolio/day
    one without re-deriving the arithmetic.
  * FAIL CLOSED on an unreadable exposure, matching the convention in
    decide_portfolio_entries: "I could not look" and "there is no room" are
    the same answer when the question is whether to spend more.
  * EVERY ORDER-PATH MODE, not just live. Simulation writes rows at the
    resolved notional and those rows feed the next cycle's exposure total,
    so a simulation breach corrupts the very number the live cap reads.
  * The budget is checked against the RESOLVED notional, never the requested
    one -- the whole point.

THE TRACK MATTERS. Exposure is scoped by is_paper per
entry_manager._candidate_is_paper, the same way the per-bucket cap and the
executor's own stamping scope it. A paper leg must not be refused because
the live book spent its budget, or vice versa.
"""
from datetime import date

import pytest

import config
import entry_manager
import executor
from clients import market_client, wallet_client
from models import EntryDecision

STATION = "WSSS"
TARGET_DATE = date(2026, 9, 3)


@pytest.fixture(autouse=True)
def _gates_that_are_not_under_test(monkeypatch):
    """
    Depth and slippage wide open, so the only thing that can refuse an order
    in this module is the budget. Without this every test here would pass for
    the wrong reason the moment a book fixture changed.
    """
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.01)


def _decision(size_usd=0.30, net_ev=0.30):
    return EntryDecision(
        station_icao=STATION, target_date=TARGET_DATE, bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1,
        recommended_size_usd=size_usd, available_depth_usd=100_000.0,
        slippage_at_size_pct=0.01, net_ev_at_size=net_ev,
        approved=True, reason="test", station_maturity="mature",
        entry_price=0.30, token_id="TOK",
    )


def _spec(notional, price=0.30, shares=5.0):
    return wallet_client.OrderSpec(
        ok=True, token_id="TOK", side="BUY", limit_price=price,
        size_shares=shares, notional_usd=notional, expected_price=price,
    )


def _budget(monkeypatch, station_remaining=None, portfolio_remaining=None):
    """
    Pin what the two exposure readers report, expressed as REMAINING budget
    so the tests read the way the caps do. None means "unreadable".
    """
    def station(icao, target_date, is_paper=None):
        if station_remaining is None:
            return None
        return config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD - station_remaining

    def portfolio(is_paper=None, region=None):
        if portfolio_remaining is None:
            return None
        return config.region_max_daily_exposure_usd(STATION) - portfolio_remaining

    monkeypatch.setattr(entry_manager, "station_day_exposure_usd", station)
    monkeypatch.setattr(entry_manager, "portfolio_day_exposure_usd", portfolio)


# ---------------------------------------------------------------------------
# The acceptance case
# ---------------------------------------------------------------------------

def test_a_leg_scaled_to_thirty_cents_is_refused_when_the_exchange_forces_two_and_a_quarter(monkeypatch):
    """
    THE ACCEPTANCE CASE, exactly as specified: a decision scaled to $0.30
    that resolves to $2.25 against a budget with $1.00 remaining is refused,
    and the reason names the binding cap.
    """
    _budget(monkeypatch, station_remaining=1.00, portfolio_remaining=1000.0)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "station" in note.lower()
    assert "2.25" in note
    assert "1.00" in note


def test_the_same_leg_is_approved_when_the_budget_has_room_for_the_resolved_size(monkeypatch):
    """
    The other half of the acceptance case. The gate must bind on the resolved
    notional and nothing else -- a $2.25 order against $3.00 of remaining
    budget is a legal order.
    """
    _budget(monkeypatch, station_remaining=3.00, portfolio_remaining=1000.0)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert ok
    assert "abandoned" not in note


def test_the_requested_size_is_never_what_is_tested(monkeypatch):
    """
    The defect in one assertion. $0.30 fits in $1.00 and $2.25 does not; a
    check written against decision.recommended_size_usd would approve this.
    """
    _budget(monkeypatch, station_remaining=1.00, portfolio_remaining=1000.0)

    ok, _ = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok


# ---------------------------------------------------------------------------
# Which cap bound, and saying so
# ---------------------------------------------------------------------------

def test_the_portfolio_cap_can_bind_on_its_own(monkeypatch):
    """
    Plenty of station budget, no portfolio budget. The station cap is
    per-station and the portfolio cap is per-region; either can be the one
    that runs out first.
    """
    _budget(monkeypatch, station_remaining=1000.0, portfolio_remaining=1.00)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "portfolio" in note.lower()


def test_the_station_cap_is_named_when_it_is_the_tighter_of_the_two(monkeypatch):
    """
    Both caps exhausted. One refusal, naming the cap that actually bound --
    not a generic "over budget" that leaves a reader to work out which.
    """
    _budget(monkeypatch, station_remaining=0.50, portfolio_remaining=1.00)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "station" in note.lower()
    assert "portfolio" not in note.lower()


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_an_unreadable_station_exposure_refuses_rather_than_assuming_room(monkeypatch):
    _budget(monkeypatch, station_remaining=None, portfolio_remaining=1000.0)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "could not" in note.lower() or "unreadable" in note.lower()


def test_an_unreadable_portfolio_exposure_refuses_rather_than_assuming_room(monkeypatch):
    _budget(monkeypatch, station_remaining=1000.0, portfolio_remaining=None)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "could not" in note.lower() or "unreadable" in note.lower()


def test_a_raising_exposure_reader_refuses_rather_than_escaping(monkeypatch):
    """
    The readers already catch their own storage errors and return None, but a
    gate that spends money must not depend on that staying true.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("storage is gone")

    monkeypatch.setattr(entry_manager, "station_day_exposure_usd", boom)
    monkeypatch.setattr(entry_manager, "portfolio_day_exposure_usd", lambda **k: 0.0)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "could not" in note.lower()


# ---------------------------------------------------------------------------
# Track scoping, and every mode
# ---------------------------------------------------------------------------

def test_the_exposure_is_read_on_the_candidate_track(monkeypatch):
    """
    A paper leg must not be refused because the live book spent its budget.
    Scoped by _candidate_is_paper, the same way the per-bucket cap and the
    executor's stamping are.
    """
    seen = {}

    def station(icao, target_date, is_paper=None):
        seen["station_is_paper"] = is_paper
        return 0.0

    def portfolio(is_paper=None, region=None):
        seen["portfolio_is_paper"] = is_paper
        return 0.0

    monkeypatch.setattr(entry_manager, "station_day_exposure_usd", station)
    monkeypatch.setattr(entry_manager, "portfolio_day_exposure_usd", portfolio)
    monkeypatch.setattr(entry_manager, "_candidate_is_paper", lambda icao: False)

    executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert seen["station_is_paper"] is False
    assert seen["portfolio_is_paper"] is False


def test_the_check_runs_without_being_told_a_mode(monkeypatch):
    """
    _resolved_size_ok takes no mode argument and is called before the live /
    simulation branch, so the budget re-check applies on every order path by
    construction. Pinned because moving it inside `if mode == "live"` would
    be an easy and invisible regression -- simulation writes rows at the
    resolved notional, and those rows are what the next cycle's exposure
    total is built from.
    """
    import inspect

    assert "mode" not in inspect.signature(executor._resolved_size_ok).parameters

    _budget(monkeypatch, station_remaining=1.00, portfolio_remaining=1000.0)
    ok, _ = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))
    assert not ok


# ---------------------------------------------------------------------------
# Interaction with the gates that were already there
# ---------------------------------------------------------------------------

def test_a_budget_refusal_does_not_mask_a_depth_refusal(monkeypatch):
    """
    Order of checks: the book is re-read first, so a thin book is still
    reported as a thin book. Both are true here; the depth one is the more
    specific fact about why this order cannot be submitted at all.
    """
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 2.00)
    _budget(monkeypatch, station_remaining=1.00, portfolio_remaining=1000.0)

    ok, note = executor._resolved_size_ok(_spec(2.25), _decision(size_usd=0.30))

    assert not ok
    assert "visible depth" in note


def test_an_unchanged_size_is_still_budget_checked(monkeypatch):
    """
    The size not moving is not evidence the budget still has room: exposure
    accrues from OTHER legs written earlier in the same cycle, so the number
    this reads moves even when the order does not. The early return that used
    to skip everything here was closed by P1-3 for the same reason.
    """
    _budget(monkeypatch, station_remaining=0.10, portfolio_remaining=1000.0)

    ok, note = executor._resolved_size_ok(_spec(1.00), _decision(size_usd=1.00))

    assert not ok
    assert "station" in note.lower()
