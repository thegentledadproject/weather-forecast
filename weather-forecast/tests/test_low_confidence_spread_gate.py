"""
tests/test_low_confidence_spread_gate.py

P3-4 · close the low-confidence gate gap.

WHAT THE SET IS ABOUT. config.LOW_CONFIDENCE_SPREAD_SOURCES is documented, in
config.py's own words, as the sources that mean "not measured FOR THIS
STATION". An entry computed on one has to clear LOW_CONFIDENCE_EDGE_MULTIPLIER
x the normal minimum edge, because the probability the edge is derived from is
only as good as the spread behind it.

"ensemble" was not in the set, and it is not a measured spread. It is the
dispersion across ECMWF ensemble members -- a forecast-model property, not this
station's own forecast-error history -- and the repo has already measured it
losing to the measured tier by 0.035 Brier over 248 unselected days (t = -3.15),
which is why the tier order was inverted on 2026-08-29.

THAT REORDERING IS WHAT MAKES THIS URGENT RATHER THAN COSMETIC. The chain is
now replay_constant -> measured_error -> ensemble -> pooled_error ->
fallback_default, so "ensemble" fires in exactly one situation: a station with
too few error pairs to measure its own spread. That is precisely the population
the gate exists to protect against, and it was the one tier in that position
without the multiplier.

THE INCONSISTENCY THIS REMOVES, measured on the deployed registry 2026-09-04.
34 of 35 stations resolve to measured_error. Exactly one, OPKC, does not -- and
which tier it lands on depends on whether an ensemble fetch happened to
succeed:

    ensemble present  -> "ensemble"      -> normal edge bar   (before this fix)
    ensemble absent   -> "pooled_error"  -> DOUBLED edge bar

Same station, same missing measurement, and the confidence gate flipped on the
success of a network call. A fetch succeeding does not make a spread
station-specific.

"replay_constant" MUST STAY OUT, and that is pinned below. Putting it in would
create a live/replay divergence in the one direction the backtest exists to
rule out -- see config.py and calibration.estimate_std_dev's backtest branch,
which calls it a real measured value.
"""
from datetime import date

import pytest

import calibration
import config
import entry_manager
from models import EVResult

STATION = "WSSS"


def _ev_result(spread_source, raw_edge=0.04, price=0.30):
    return EVResult(
        station_icao=STATION,
        target_date=date(2026, 9, 3),
        bucket_c=32,
        side="YES",
        model_prob=price + raw_edge,
        market_price=price,
        raw_edge=raw_edge,
        estimated_slippage_pct=0.0,
        fee_rate_pct=0.0,
        net_ev_per_dollar=raw_edge / price,
        spread_source=spread_source,
    )


@pytest.fixture(autouse=True)
def _no_market(monkeypatch):
    """The edge veto runs before any sizing, but pin the rest anyway."""
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def test_the_ensemble_tier_counts_as_not_measured_for_this_station():
    assert "ensemble" in config.LOW_CONFIDENCE_SPREAD_SOURCES


def test_the_sources_that_were_already_in_the_set_stay_in_it():
    assert {"fallback_default", "pooled_error"} <= config.LOW_CONFIDENCE_SPREAD_SOURCES


def test_a_station_specific_measured_spread_is_not_low_confidence():
    """
    measured_error IS this station's own forecast-error history. Putting it in
    the set would double the bar for every station on the book -- 34 of 35 as
    of 2026-09-04 -- which is a different change entirely.
    """
    assert "measured_error" not in config.LOW_CONFIDENCE_SPREAD_SOURCES


def test_the_replay_constant_stays_out():
    """
    Documented in config.py and in calibration.estimate_std_dev's backtest
    branch: it is a real measured pooled value, and gating on it would create a
    live/replay divergence in the one direction the backtest exists to rule
    out.
    """
    assert "replay_constant" not in config.LOW_CONFIDENCE_SPREAD_SOURCES


# ---------------------------------------------------------------------------
# The behavioural consequence
# ---------------------------------------------------------------------------

def _edge_at_the_normal_bar():
    """An edge that clears MIN_ABS_RAW_EDGE but not twice it."""
    return config.MIN_ABS_RAW_EDGE * 1.5


def test_an_ensemble_spread_now_has_to_clear_the_doubled_edge_bar():
    decision = entry_manager.evaluate_entry(
        _ev_result("ensemble", raw_edge=_edge_at_the_normal_bar()),
        token_id="tok",
        min_net_ev=-9.0,
    )

    assert not decision.approved
    assert "below required minimum" in decision.reason


def test_the_same_edge_on_a_measured_spread_still_clears():
    """
    The other half: this is a tightening aimed at one tier, not a raise of the
    edge bar for the whole book.
    """
    decision = entry_manager.evaluate_entry(
        _ev_result("measured_error", raw_edge=_edge_at_the_normal_bar()),
        token_id="tok",
        min_net_ev=-9.0,
    )

    assert "below required minimum" not in decision.reason


def test_the_refusal_names_the_spread_source():
    """
    An entry refused for a bar it did not know had moved is the kind of thing
    that gets debugged as a model problem. The note says which source raised it.
    """
    decision = entry_manager.evaluate_entry(
        _ev_result("ensemble", raw_edge=_edge_at_the_normal_bar()),
        token_id="tok",
        min_net_ev=-9.0,
    )

    assert "spread_source=ensemble" in decision.reason


def test_a_big_enough_edge_still_trades_on_an_ensemble_spread():
    """
    The gate raises the bar; it does not ban the tier. A station with no
    measured spread can still trade on a disagreement large enough to survive
    a spread that may be wrong.
    """
    decision = entry_manager.evaluate_entry(
        _ev_result("ensemble", raw_edge=config.MIN_ABS_RAW_EDGE * config.LOW_CONFIDENCE_EDGE_MULTIPLIER + 0.01),
        token_id="tok",
        min_net_ev=-9.0,
    )

    assert "below required minimum" not in decision.reason


# ---------------------------------------------------------------------------
# The inconsistency this closes
# ---------------------------------------------------------------------------

def test_the_bar_no_longer_flips_on_whether_an_ensemble_fetch_succeeded():
    """
    THE MEASURED CASE. A station with no error pairs of its own resolves to
    "ensemble" when the fetch worked and "pooled_error" when it did not. Both
    describe the same fact -- no spread measured for this station -- so both
    must carry the same bar. Before this change one of them did and one did
    not.
    """
    with_fetch = "ensemble"
    without_fetch = "pooled_error"

    assert (with_fetch in config.LOW_CONFIDENCE_SPREAD_SOURCES) == (
        without_fetch in config.LOW_CONFIDENCE_SPREAD_SOURCES
    )


def test_the_calibration_note_still_calls_it_not_measured_for_this_station():
    """
    calibration.py annotates a low-confidence estimate with "Spread is not
    measured for this station". That wording is the set's definition, and it
    has to stay true of every member -- it is now the justification for the
    newest one.
    """
    import inspect

    src = inspect.getsource(calibration)
    assert "not measured for this station" in src
