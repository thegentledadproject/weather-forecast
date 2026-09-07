"""
tests/test_band_sweep.py

Guards for backtest/band_sweep.py.

WHAT THE TOOL IS FOR. config.ENTRY_PRICE_BLOCK_BAND ships None, and the block
of measurement above it ends: "Do not turn this on without a replay whose
ordering holds per station AND across windows." That is the acceptance bar
these tests exist to make the tool capable of meeting -- which is why the
per-station split, not the pooled number, is the thing pinned here.

WHY THE POOLED NUMBER IS THE DANGEROUS ONE. config.py records what happened
last time in as many words: pooled, blocking looked like a +$32.34 win, and
reading only that line was the trap -- the freed capital flowed into the other
bands and lost ~$42 there, the median got WORSE (-42.6% -> -55.3%), and
per-station the ordering was five-three, which is not a result. So the tool
must not be able to report the pooled figure without the per-station table
beside it.
"""

import pytest

import config
import entry_manager
from backtest import band_sweep, entry_sim


# --- the contextmanager --------------------------------------------------

def test_band_context_sets_and_restores_the_band():
    assert config.ENTRY_PRICE_BLOCK_BAND is None
    with band_sweep._blocked_band((0.15, 0.25)):
        assert config.ENTRY_PRICE_BLOCK_BAND == (0.15, 0.25)
    assert config.ENTRY_PRICE_BLOCK_BAND is None


def test_band_context_restores_on_the_raising_path():
    """
    config is process-global and the daemon imports the same module, so a
    leaked band is a live trading change made by a crashed analysis script --
    and this one DELETES a slice of the book rather than merely reordering it.
    """
    with pytest.raises(RuntimeError):
        with band_sweep._blocked_band((0.15, 0.25)):
            raise RuntimeError("boom")
    assert config.ENTRY_PRICE_BLOCK_BAND is None


def test_band_context_off_cell_really_means_off():
    """The control arm. None is a cell, not a missing argument."""
    with band_sweep._blocked_band(None):
        assert config.ENTRY_PRICE_BLOCK_BAND is None
        assert not config.entry_price_is_blocked(0.20)


# --- both gates see the same band ----------------------------------------

def test_live_gate_and_replay_gate_agree_under_the_same_band():
    """
    The band is enforced twice -- entry_manager's Veto 00b and entry_sim's
    gate 0b -- and both read config.entry_price_is_blocked(). If they ever
    stop agreeing, the replay scores a rule live does not run.
    """
    assert "entry_price_is_blocked" in entry_manager.evaluate_entry.__code__.co_names
    assert "entry_price_is_blocked" in entry_sim.evaluate_entry_sim.__code__.co_names

    with band_sweep._blocked_band((0.15, 0.25)):
        assert config.entry_price_is_blocked(0.20)
        assert not config.entry_price_is_blocked(0.25)   # high is EXCLUSIVE
        assert config.entry_price_is_blocked(0.15)       # low is inclusive
        assert not config.entry_price_is_blocked(0.14)


# --- per-station scoring is the point ------------------------------------

class _Run:
    """Minimal stand-in for a BacktestRun: what _score reads and nothing else."""
    def __init__(self, station, positions):
        self.station_icao = station
        self.closed_positions = positions
        self.unresolved_positions = []


class _Pos:
    def __init__(self, station, entry, exit_price, size, status="closed_resolution"):
        self.station_icao = station
        self.entry_price = entry
        self.exit_price = exit_price
        self.size_usd = size
        self.status = status


def test_scoring_splits_by_station():
    """
    The acceptance bar is a PER-STATION ordering, so a scorer that only
    aggregates cannot answer the question the tool was built for.
    """
    runs = [
        _Run("WSSS", [_Pos("WSSS", 0.20, 1.0, 10.0)]),          # +400%
        _Run("WMKK", [_Pos("WMKK", 0.50, 0.0, 10.0)]),          # -100%
    ]
    by_station = band_sweep.score_by_station(runs)

    assert set(by_station) == {"WSSS", "WMKK"}
    assert by_station["WSSS"]["n"] == 1
    assert by_station["WSSS"]["ret"] == pytest.approx(4.0)
    assert by_station["WMKK"]["ret"] == pytest.approx(-1.0)


def test_pooled_and_per_station_stake_agree():
    """
    The two views must be the same dollars seen two ways, or the per-station
    table is decoration rather than a decomposition of the pooled line.
    """
    runs = [
        _Run("WSSS", [_Pos("WSSS", 0.20, 1.0, 10.0), _Pos("WSSS", 0.40, 0.0, 5.0)]),
        _Run("WMKK", [_Pos("WMKK", 0.50, 0.0, 10.0)]),
    ]
    pooled = band_sweep.score(runs)
    by_station = band_sweep.score_by_station(runs)

    assert pooled["n"] == sum(s["n"] for s in by_station.values())
    assert pooled["stake"] == pytest.approx(sum(s["stake"] for s in by_station.values()))
    assert pooled["pnl"] == pytest.approx(sum(s["pnl"] for s in by_station.values()))


def test_ordering_verdict_counts_stations_both_ways():
    """
    The project's bar, made mechanical: blocking must not merely win pooled,
    it must win at MOST STATIONS. Last time it was five-three, which config.py
    records as "not a result" -- so the helper reports the split rather than a
    boolean anyone could round in their favour.
    """
    off = {"WSSS": {"ret": -0.10, "n": 33}, "WMKK": {"ret": -0.20, "n": 19},
           "ZGGG": {"ret": -0.30, "n": 17}}
    on = {"WSSS": {"ret": -0.05, "n": 31}, "WMKK": {"ret": -0.40, "n": 14},
          "ZGGG": {"ret": -0.10, "n": 13}}

    better, worse, tied = band_sweep.station_ordering(off, on)

    assert better == ["WSSS", "ZGGG"]
    assert worse == ["WMKK"]
    assert tied == []


def test_ordering_ignores_stations_absent_from_either_side():
    """
    A station that traded under one cell and not the other is not evidence
    about the ordering, and silently scoring it as a win would be the easiest
    way for this tool to flatter the band.
    """
    off = {"WSSS": {"ret": -0.10, "n": 33}, "RPLL": {"ret": -0.50, "n": 6}}
    on = {"WSSS": {"ret": -0.05, "n": 31}}

    better, worse, tied = band_sweep.station_ordering(off, on)

    assert better == ["WSSS"]
    assert worse == []
    assert "RPLL" not in better + worse + tied


# --- the guards are shared, not copied -----------------------------------

def test_reuses_the_entry_bar_sweep_guards():
    """
    Both guards are safety-critical and a second copy will drift. This tool
    imports them; it does not restate them.
    """
    from backtest import entry_bar_sweep
    assert band_sweep.assert_cohort_holds is entry_bar_sweep.assert_cohort_holds
    assert band_sweep.assert_measured_something is entry_bar_sweep.assert_measured_something


def test_ordering_excludes_stations_that_traded_on_neither_side():
    """
    RPLL is force-collection-only, so it replays 0 entries under every cell and
    scores ret 0.0 on both sides -- which the equality branch counted as
    "tied". A tie means "the band changed nothing here"; this station means
    "there was nothing here". Reporting it as a tie makes an absence read as
    evidence, and the ordering line is the number this whole tool exists to
    produce.
    """
    off = {"WSSS": {"ret": -0.10, "n": 33}, "RPLL": {"ret": 0.0, "n": 0}}
    on = {"WSSS": {"ret": -0.05, "n": 31}, "RPLL": {"ret": 0.0, "n": 0}}

    better, worse, tied = band_sweep.station_ordering(off, on)

    assert better == ["WSSS"]
    assert tied == [], "a station that never traded is not a tie"
    assert "RPLL" not in better + worse + tied


def test_a_genuine_tie_is_still_reported():
    """The exclusion must be about absence, not about equal returns."""
    off = {"VHHH": {"ret": -0.055, "n": 26}}
    on = {"VHHH": {"ret": -0.055, "n": 26}}

    better, worse, tied = band_sweep.station_ordering(off, on)

    assert tied == ["VHHH"]
