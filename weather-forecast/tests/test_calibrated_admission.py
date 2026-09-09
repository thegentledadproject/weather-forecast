"""
tests/test_calibrated_admission.py

Move the entry ADMISSION bar onto the calibrated probability that the sizing
path (P3-6) already uses.

THE DEFECT THIS CLOSES. Measured 2026-09-09 over the candidate universe in
`ev_snapshots`, causally and against settlement: the model is CALIBRATED on the
1,539 candidates it did not buy (settle rate minus model_prob = +0.007, CI
[-0.001,+0.014]) and TWELVE POINTS HOT on the 153 it bought (-0.120, CI
[-0.190,-0.051]). The market's own prices are calibrated in every bin with real
n, so `model_prob - market_price` is very nearly the model's own ERROR, and a
rule that admits on the largest such disagreement is selecting that error. This
is the winner's curse, and it is why `raw_edge` quintiles do not rank, why
`net_ev_at_size` ranks worst-first, and why the model's Brier on TRADED tickets
(0.1924) is worse than the ask it traded against (0.1805).

WHAT MOVES, AND ONLY THIS. Veto 0a2 -- the edge MATERIALITY bar,
config.MIN_ABS_RAW_EDGE -- reads the calibrated edge where a map exists. Nothing
else does.

WHAT DELIBERATELY DOES NOT MOVE:
  * THE RANKING. Replayed top-k per station-day over the same universe, the
    calibrated gate helped at every k (+0.041 / +0.018 / +0.019 at k=1/2/3
    against +0.019 / -0.000 / -0.002 today) and calibrating the RANK hurt at
    every k and cancelled the gate's gain. So the gate moves and the sort does
    not, and that split is the measurement, not a convenience.
  * Veto 0a, the plausibility CEILING. That is a data-error detector; it has to
    read the raw number it is checking for corruption.
  * net_ev_per_dollar and config.clears_entry_bar(). Unmeasured here, and
    bundling them would make the result unattributable -- the same reason P3-6
    shipped sizing-only.

HONEST ABOUT THE EVIDENCE. No CI above excludes zero: `ev_snapshots` only
reaches back to 2026-09-03, so this rests on six days. It ships because it is
conservative by construction -- it can only ever REFUSE an entry, never add one
-- and because the mechanism is measured even where the P&L is not. Re-score
after ~2026-09-20, when the universe has a fortnight.
"""
from datetime import date

import pytest

import config
import entry_manager
import probability_calibration as pc
from backtest import entry_sim
from models import EVResult

DAY = date(2026, 9, 9)


def _ev(model_prob=0.432, price=0.30, calibrated=None, source=pc.NO_TIER):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side="YES",
        model_prob=model_prob, market_price=price,
        raw_edge=model_prob - price, estimated_slippage_pct=0.0,
        fee_rate_pct=0.0, net_ev_per_dollar=(model_prob - price) / price,
        calibrated_prob=calibrated, calibration_source=source,
    )


@pytest.fixture
def no_live_io(monkeypatch):
    """Everything evaluate_entry() would reach the network or the book for."""
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)


# A book that says 0.432 at an ask of 0.30 claims a 13.2c edge. The map, fitted
# on what this book's own stated probabilities actually came true at, says the
# truth is 0.301 -- a 0.1c edge, well inside MIN_ABS_RAW_EDGE (0.03).
HOT = dict(model_prob=0.432, price=0.30, calibrated=0.301, source=pc.STATION_TIER)


# ---------------------------------------------------------------------------
# admission_edge(): which number the bar is applied to
# ---------------------------------------------------------------------------

def test_the_admission_edge_is_the_calibrated_one_when_a_map_exists():
    assert entry_manager.admission_edge(_ev(**HOT)) == pytest.approx(0.001, abs=1e-9)


def test_the_admission_edge_falls_back_to_raw_when_uncalibrated():
    """
    NO_TIER means nothing measured this station or this book, so there is no
    correction to apply -- and pre-change behaviour is exactly what "no
    correction" means.
    """
    row = _ev(model_prob=0.432, price=0.30, calibrated=0.301, source=pc.NO_TIER)
    assert entry_manager.admission_edge(row) == pytest.approx(0.132, abs=1e-9)


def test_the_admission_edge_is_the_raw_one_when_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    assert entry_manager.admission_edge(_ev(**HOT)) == pytest.approx(0.132, abs=1e-9)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

def test_an_entry_whose_calibrated_edge_misses_the_bar_is_refused(no_live_io):
    """
    THE POINT OF THE CHANGE. 13.2c of claimed edge, 0.1c of measured edge. The
    old rule admitted this; the winner's-curse measurement says it is the
    model's error, not a disagreement worth paying an ask for.
    """
    decision = entry_manager.evaluate_entry(
        _ev(**HOT), token_id="tok", min_net_ev=-9.0,
    )

    assert not decision.approved
    assert "below required minimum" in decision.reason


def test_the_same_entry_is_admitted_when_the_flag_is_off(no_live_io, monkeypatch):
    """The flag is a real revert path, not decoration."""
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)

    decision = entry_manager.evaluate_entry(
        _ev(**HOT), token_id="tok", min_net_ev=-9.0,
    )

    assert "below required minimum" not in decision.reason


def test_an_uncalibrated_entry_is_admitted_exactly_as_before(no_live_io):
    """
    CONSERVATIVE FALLBACK. A station with no map is not a station with a
    corrected probability, and refusing it would be a trading change nothing
    here measured. probability_calibration.calibration_for() already degrades
    this way on any storage failure.
    """
    decision = entry_manager.evaluate_entry(
        _ev(model_prob=0.432, price=0.30, calibrated=0.301, source=pc.NO_TIER),
        token_id="tok", min_net_ev=-9.0,
    )

    assert "below required minimum" not in decision.reason


def test_a_calibrated_edge_that_clears_the_bar_still_trades(no_live_io):
    """The gate must refuse the inflated, not everything."""
    decision = entry_manager.evaluate_entry(
        _ev(model_prob=0.46, price=0.30, calibrated=0.38, source=pc.STATION_TIER),
        token_id="tok", min_net_ev=-9.0,
    )

    assert decision.approved


def test_the_refusal_says_the_bar_was_applied_to_the_calibrated_edge(no_live_io):
    """
    These rejections are read off the journal when a station stops entering, so
    the line has to say WHICH NUMBER refused it -- the same reason
    config.entry_bar_label() exists. "Edge 0.001 below minimum" against a
    stored raw_edge of 0.132 is otherwise unreadable.
    """
    decision = entry_manager.evaluate_entry(
        _ev(**HOT), token_id="tok", min_net_ev=-9.0,
    )

    assert pc.STATION_TIER in decision.reason
    assert "0.432" in decision.reason  # what the model claimed


# ---------------------------------------------------------------------------
# What must NOT move
# ---------------------------------------------------------------------------

def test_the_plausibility_ceiling_still_reads_the_raw_edge(no_live_io):
    """
    Veto 0a is a DATA-ERROR detector, not an alpha filter. A raw edge of 0.60
    is a corrupt quote whatever a map would shrink it to, and calibrating the
    ceiling would hide exactly the corruption it exists to catch.
    """
    decision = entry_manager.evaluate_entry(
        _ev(model_prob=0.95, price=0.30, calibrated=0.31, source=pc.STATION_TIER),
        token_id="tok", min_net_ev=-9.0,
    )

    assert not decision.approved
    assert "plausibility ceiling" in decision.reason


def test_sizing_still_reads_the_calibrated_edge_whatever_the_flag_says(monkeypatch):
    """P3-6 is not conditional on this change and must not become so."""
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    assert entry_manager.sizing_edge(_ev(**HOT)) == pytest.approx(0.001, abs=1e-9)


def test_the_stored_raw_edge_is_untouched():
    """
    model_prob and raw_edge are what the EV table, the snapshots and every
    stored row MEAN, and the cohort scores against them. The correction travels
    beside the record, never over it.
    """
    row = _ev(**HOT)
    assert row.model_prob == 0.432
    assert row.raw_edge == pytest.approx(0.132)


# ---------------------------------------------------------------------------
# The replay replica
# ---------------------------------------------------------------------------

def test_the_replay_admission_is_uncalibrated_and_says_so():
    """
    backtest/entry_sim.py is the pure replica of evaluate_entry(), and the
    replay feeds it EV rows that carry no fitted map -- so it admits on the RAW
    edge and CANNOT score this change. That is a limitation, not a bug, and it
    is pinned here because this project has twice shipped a replay that
    silently scored the old rule while reporting the new one (see
    backtest/engine.py's unconditional allow_measured_spread=False).

    The sim reads the edge through the SAME helper as live, so the two cannot
    drift on the arithmetic; what keeps the sim raw is the absence of a map on
    its rows, which is a property of the replay's inputs.
    """
    uncalibrated_row = _ev(model_prob=0.432, price=0.30)  # no map, as the replay feeds it
    assert uncalibrated_row.calibration_source == pc.NO_TIER

    decision = entry_sim.evaluate_entry_sim(
        uncalibrated_row,
        token_id="tok",
        open_count_for_bucket=0,
        opposite_count_for_bucket=0,
        stop_outs_for_bucket=0,
        depth_usd=100_000.0,
        slippage_fn=lambda size: 0.0,
        min_net_ev=-9.0,
        sizing_bankroll=1_000.0,
    )

    assert "below required minimum" not in decision.reason
