"""
tests/test_observation_lookback.py

The observed term's sample window.

pipeline.gather_observations() used to load stored observations from
`target_date.replace(day=1)`. That is not a lookback -- it is
days-since-the-1st: 30 days on the 31st and ZERO on the 1st. Measured on
the live database 2026-08-27, the WSSS observed term held 26 readings
(mean 32.538); as-of 2026-09-01 and 2026-09-02 it held none, and
calibration.blend_central_estimate() then falls through to
`return round(forecast_mean, 1)` -- so the central estimate of a
real-money station silently stopped being a 40/60 blend and became 100%
forecast, on the 1st of every month.

climate_monitor_client.load_recent_observations() returns ZERO rows for
WSSS, so nothing else was holding that term up.

See docs/superpowers/specs/2026-08-28-recency-weighted-observed-term-design.md.
This file covers the lookback half only; the recency weighting is a
separate change gated on a measurement this fix is a precondition of.
"""

from datetime import date, timedelta

import pytest

import calibration
import config
import pipeline
import storage
from backtest import engine
from clients import climate_monitor_client
from models import ObservedReading, PointForecast


def _stored(icao, days_of_history, end_day):
    """One settlement-grade reading per day, ending at end_day."""
    return [
        ObservedReading(icao, end_day - timedelta(days=i), 33.0,
                        config.RESOLUTION_GRADE_OBSERVATION_SOURCE)
        for i in range(days_of_history)
    ]


@pytest.fixture
def no_seeds(monkeypatch):
    """
    Climate seeds return nothing -- which is not a hypothetical. Verified
    against the live database for WSSS: load_recent_observations() -> 0 rows,
    so the stored query is the whole observed term.
    """
    monkeypatch.setattr(
        climate_monitor_client, "load_recent_observations", lambda st, days=30: []
    )


def test_the_first_of_the_month_still_has_a_lookback(monkeypatch, no_seeds):
    """
    THE 2026-09-01 FAILURE, stated directly. A month boundary must not empty
    the observed term.
    """
    station = config.get_station("WSSS")
    history = _stored("WSSS", 30, date(2026, 8, 31))
    captured = {}

    def _since(icao, cutoff):
        captured["cutoff"] = cutoff
        return [o for o in history if o.target_date >= cutoff]

    monkeypatch.setattr(storage, "load_observations_since", _since)

    obs = pipeline.gather_observations(station, date(2026, 9, 1))

    assert captured["cutoff"] < date(2026, 9, 1), (
        f"cutoff {captured['cutoff']} is not a lookback -- it is the month start"
    )
    assert len(obs) >= 28, f"observed term nearly empty on the 1st: {len(obs)} readings"


def test_the_window_length_does_not_depend_on_the_day_of_the_month(monkeypatch, no_seeds):
    """
    The defect in one assertion: the cutoff's DISTANCE from the target date
    swung with the calendar -- 30 days on the 31st, 0 on the 1st. It must now
    be the same offset on every date.

    Asserted on the cutoff rather than on how many rows come back, because
    the row count also depends on how much history happens to exist, which is
    a fact about the database and not about this function.
    """
    station = config.get_station("WSSS")
    seen = {}

    def _since(icao, cutoff):
        seen[_since.day] = cutoff
        return []

    monkeypatch.setattr(storage, "load_observations_since", _since)

    for d in (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2),
              date(2026, 9, 15), date(2027, 1, 1)):
        _since.day = d
        pipeline.gather_observations(station, d)

    offsets = {d: (d - cutoff).days for d, cutoff in seen.items()}
    assert set(offsets.values()) == {config.OBSERVATION_LOOKBACK_DAYS}, (
        f"lookback still varies by date: {offsets}"
    )


def test_the_estimate_stays_a_blend_across_the_month_boundary(monkeypatch, no_seeds):
    """
    The consequence that reached the book. With no observations,
    blend_central_estimate() returns the forecast mean alone -- so on the 1st
    a station weighted 60% observed silently became 100% forecast.
    """
    station = config.get_station("WSSS")
    history = _stored("WSSS", 30, date(2026, 8, 31))
    monkeypatch.setattr(
        storage, "load_observations_since",
        lambda icao, cutoff: [o for o in history if o.target_date >= cutoff],
    )
    forecasts = [
        PointForecast("WSSS", "open_meteo_gfs", date(2026, 9, 1), 31.0, "2026-08-31T21:00:00Z")
    ]

    obs = pipeline.gather_observations(station, date(2026, 9, 1))
    central = calibration.blend_central_estimate(
        forecasts, obs, station.long_term_normal_max_c,
        forecast_weight=config.forecast_blend_weight("WSSS"),
    )

    forecast_only = round(31.0, 1)
    assert central != forecast_only, "estimate collapsed to forecast-only on the 1st"
    assert central == pytest.approx(0.4 * 31.0 + 0.6 * 33.0, abs=0.05)


def test_live_and_replay_agree_on_the_lookback():
    """
    backtest/engine.py's OBSERVATION_WINDOW_DAYS comment claims it mirrors
    the live call sites. It did not: the replay used a fixed 30-day cutoff
    while live used replace(day=1), so the two computed the observed term
    over different samples -- most extremely on the 1st, where the replay had
    30 days and live had none.

    Any half-life scored in the replay against a window live does not use
    would be measuring the wrong thing, so this equality is a precondition of
    the recency-weighting measurement, not decoration.
    """
    assert engine.OBSERVATION_WINDOW_DAYS == config.OBSERVATION_LOOKBACK_DAYS
