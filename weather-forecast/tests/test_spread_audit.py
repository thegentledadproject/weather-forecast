"""
tests/test_spread_audit.py

The lead-time spread measurement (spread_audit.py).

Background: calibration.measured_error_spread() takes the standard
deviation of storage.forecast_error_samples(), which averages EVERY
forecast a station stored for a target date as long as it was fetched on
or before that date in UTC. Entries happen in a narrow window a few hours
before the day's peak, so the forecast set actually in hand at decision
time is a SUBSET -- the freshest one -- of what that average pools. If
recency helps, the spread the probability step cuts buckets from is wider
than the spread the trading path is entitled to, which puts too much mass
in the tails and manufactures the cheap-ticket edge measured on
2026-08-20.

These tests pin the four things the measurement decides: what "lead"
means (station-local, not UTC), that the cutoff is what selects the
forecast set, that the blend variant cannot see the day it is scoring,
and that a date with nothing left after the cutoff leaves the sample
rather than scoring as zero error.
"""

import sqlite3

import pytest

import config
import spread_audit


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE observations (station_icao TEXT, target_date TEXT, "
        "max_temp_c REAL, source TEXT)"
    )
    conn.execute(
        "CREATE TABLE forecasts (station_icao TEXT, source TEXT, target_date TEXT, "
        "max_temp_c REAL, fetched_at TEXT, raw_note TEXT)"
    )
    return conn


def _obs(conn, icao, day, temp, source="metar_daily_max"):
    conn.execute(
        "INSERT INTO observations VALUES (?,?,?,?)", (icao, day, temp, source)
    )


def _fc(conn, icao, day, temp, fetched_at, source="a"):
    conn.execute(
        "INSERT INTO forecasts VALUES (?,?,?,?,?,'')",
        (icao, source, day, temp, fetched_at),
    )


# --- what "lead" means -----------------------------------------------------

def test_lead_is_measured_to_the_end_of_the_station_local_day(tmp_path, monkeypatch):
    """
    WSSS is UTC+8, so 2026-08-10 ends at 2026-08-10T16:00Z. A forecast
    fetched at 12:00Z that day therefore has 4h of lead, not 12h, and must
    survive a 4h cutoff while a 5h cutoff drops it.
    """
    conn = _make_db(tmp_path, monkeypatch)
    _obs(conn, "WSSS", "2026-08-10", 32.0)
    _fc(conn, "WSSS", "2026-08-10", 30.0, "2026-08-10T12:00:00+00:00")
    conn.commit()

    assert spread_audit.forecast_errors_at_lead("WSSS", 4) == {"2026-08-10": -2.0}
    assert spread_audit.forecast_errors_at_lead("WSSS", 5) == {}


def test_the_same_fetch_has_less_lead_at_a_station_further_east(tmp_path, monkeypatch):
    """
    The cutoff is a property of the station's calendar day, not of UTC.
    RJTT is UTC+9, so its 2026-08-10 ends an hour EARLIER in UTC than
    WSSS's -- the identical fetched_at buys one hour less lead there.
    """
    assert config.get_station("WSSS").utc_offset_hours == 8
    assert config.get_station("RJTT").utc_offset_hours == 9

    conn = _make_db(tmp_path, monkeypatch)
    for icao in ("WSSS", "RJTT"):
        _obs(conn, icao, "2026-08-10", 32.0)
        _fc(conn, icao, "2026-08-10", 30.0, "2026-08-10T11:00:00+00:00")
    conn.commit()

    # WSSS: day ends 16:00Z -> 5h lead. RJTT: day ends 15:00Z -> 4h lead.
    assert spread_audit.forecast_errors_at_lead("WSSS", 5) == {"2026-08-10": -2.0}
    assert spread_audit.forecast_errors_at_lead("RJTT", 5) == {}


def test_a_forecast_fetched_after_the_day_ended_never_counts(tmp_path, monkeypatch):
    """
    Negative lead is lookahead: the row has seen the day it claims to
    predict. A zero cutoff must already exclude it, with no separate
    guard needed.
    """
    conn = _make_db(tmp_path, monkeypatch)
    _obs(conn, "WSSS", "2026-08-10", 32.0)
    _fc(conn, "WSSS", "2026-08-10", 30.0, "2026-08-10T18:00:00+00:00")
    conn.commit()

    assert spread_audit.forecast_errors_at_lead("WSSS", 0) == {}


# --- the cutoff selects the forecast set -----------------------------------

def test_the_cutoff_reaverages_rather_than_dropping_the_date(tmp_path, monkeypatch):
    """
    Two forecasts at different leads: the wide cutoff must score the
    early one ALONE, not the mean of both. This is the whole experiment --
    if the surviving set were not re-averaged there would be nothing to
    measure.
    """
    conn = _make_db(tmp_path, monkeypatch)
    _obs(conn, "WSSS", "2026-08-10", 32.0)
    _fc(conn, "WSSS", "2026-08-10", 28.0, "2026-08-09T16:00:00+00:00", source="early")
    _fc(conn, "WSSS", "2026-08-10", 32.0, "2026-08-10T14:00:00+00:00", source="late")
    conn.commit()

    # 24h lead: only "early" survives -> 28.0 - 32.0
    assert spread_audit.forecast_errors_at_lead("WSSS", 24) == {"2026-08-10": -4.0}
    # 2h lead: both survive -> mean 30.0 - 32.0
    assert spread_audit.forecast_errors_at_lead("WSSS", 2) == {"2026-08-10": -2.0}


def test_a_date_with_nothing_left_after_the_cutoff_leaves_the_sample(tmp_path, monkeypatch):
    """
    It must DROP OUT, not score as zero error. A zero would be pulled into
    the standard deviation as a real observation of a perfect forecast and
    would narrow every wide-lead row for free.
    """
    conn = _make_db(tmp_path, monkeypatch)
    _obs(conn, "WSSS", "2026-08-10", 32.0)
    _fc(conn, "WSSS", "2026-08-10", 28.0, "2026-08-10T14:00:00+00:00")
    _obs(conn, "WSSS", "2026-08-11", 33.0)
    _fc(conn, "WSSS", "2026-08-11", 30.0, "2026-08-10T00:00:00+00:00")
    conn.commit()

    errors = spread_audit.forecast_errors_at_lead("WSSS", 24)
    assert "2026-08-10" not in errors
    assert errors == {"2026-08-11": -3.0}


# --- the blend variant -----------------------------------------------------

def test_blend_error_weights_the_forecast_term_against_prior_observations(
    tmp_path, monkeypatch
):
    """
    The traded quantity is calibration.blend_central_estimate's output,
    not the raw forecast mean. At weight w the blend of a 30.0 forecast
    and a 34.0 prior observation is w*30 + (1-w)*34, and the error is that
    minus settled truth.
    """
    monkeypatch.setattr(config, "FORECAST_BLEND_WEIGHT_DEFAULT", 0.75)
    conn = _make_db(tmp_path, monkeypatch)
    _obs(conn, "WSSS", "2026-08-09", 34.0)          # prior day, feeds the blend
    _obs(conn, "WSSS", "2026-08-10", 32.0)          # settled truth for the score
    _fc(conn, "WSSS", "2026-08-10", 30.0, "2026-08-09T16:00:00+00:00")
    conn.commit()

    # 0.75*30 + 0.25*34 = 31.0 -> error -1.0
    assert spread_audit.blend_errors_at_lead("WSSS", 12) == {"2026-08-10": -1.0}


def test_blend_cannot_see_the_day_it_is_scoring(tmp_path, monkeypatch):
    """
    THE LOOKAHEAD TRAP. pipeline.gather_observations() loads every stored
    observation from the first of the target month, target date included.
    On the live path that is harmless -- the day's METAR does not exist at
    entry time -- but replayed over history it feeds settled truth into
    the estimate being scored against settled truth, and the blend error
    collapses toward the observed term's share of nothing.

    Poison the target day's own observation with a value no blend of the
    inputs could produce and assert the answer does not move.
    """
    monkeypatch.setattr(config, "FORECAST_BLEND_WEIGHT_DEFAULT", 0.75)

    def build(truth_for_target):
        conn = _make_db(tmp_path / truth_for_target[0], monkeypatch)
        _obs(conn, "WSSS", "2026-08-09", 34.0)
        _obs(conn, "WSSS", "2026-08-10", truth_for_target[1])
        _fc(conn, "WSSS", "2026-08-10", 30.0, "2026-08-09T16:00:00+00:00")
        conn.commit()
        return spread_audit.blend_errors_at_lead("WSSS", 12)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    clean = build(("a", 32.0))
    # Same truth, but if the target day's observation leaked into the
    # blend the estimate would move; it must not.
    assert clean == {"2026-08-10": -1.0}

    poisoned = build(("b", 99.0))
    # Truth changed, so the ERROR changes -- but only through the score,
    # never through the estimate: -1.0 shifted by the 67.0 truth change.
    assert poisoned == {"2026-08-10": -68.0}


# --- the sweep -------------------------------------------------------------

def test_sweep_reports_spread_and_sample_size_per_lead(tmp_path, monkeypatch):
    """
    One row per requested lead, carrying the two spreads and the n behind
    them. n must be the count of DATES that survived the cutoff, so a row
    whose sample thinned out is readable as thin rather than as precise.
    """
    conn = _make_db(tmp_path, monkeypatch)
    for day, truth, temp in (
        ("2026-08-10", 32.0, 30.0),
        ("2026-08-11", 33.0, 32.0),
        ("2026-08-12", 31.0, 30.0),
    ):
        _obs(conn, "WSSS", day, truth)
        _fc(conn, "WSSS", day, temp, f"{day}T14:00:00+00:00", source="late")
    # Only one day also has an early forecast. It agrees with that day's
    # late one, so the narrow row's spread is a fact about the three dates
    # rather than about this extra row.
    _fc(conn, "WSSS", "2026-08-10", 30.0, "2026-08-08T00:00:00+00:00", source="early")
    conn.commit()

    rows = {r.min_lead_h: r for r in spread_audit.sweep("WSSS", [2, 24])}
    assert rows[2].n == 3
    assert rows[24].n == 1
    assert rows[2].forecast_spread_c == pytest.approx(0.577, abs=0.001)
    # A single date has no standard deviation to report.
    assert rows[24].forecast_spread_c is None


def test_no_station_argument_audits_the_whole_registry(monkeypatch):
    """
    config.STATIONS is keyed by ICAO, so iterating it yields STRINGS. The
    default path must hand report() those keys -- iterating for objects
    raises AttributeError and takes out the no-argument invocation, which
    is the one an operator reaches for first.
    """
    seen = {}

    def _capture(station_icaos, leads, common_dates=False):
        seen["stations"] = list(station_icaos)

    monkeypatch.setattr(spread_audit, "report", _capture)
    spread_audit.main([])

    assert seen["stations"] == list(config.STATIONS)
    assert "WSSS" in seen["stations"]
