"""
tests/test_forecast_lead_window.py

Day-ahead forecast storage, and the window that keeps it from moving the
bias correction.

Two changes that only make sense together, both from the 2026-08-21
spread audit:

  1. Open-Meteo is ALREADY asked for forecast_days=3 and the extra two
     days were being thrown away, so every stored forecast sat in a
     14-19h lead band and "does lead time matter?" was unanswerable.
     _fetch_daily_max_series() keeps all of them.

  2. storage.forecast_error_samples() cut lookahead at
     `date(fetched_at) <= target_date` -- a UTC comparison, which at
     UTC+8 admits rows fetched up to 8 hours AFTER the local day ended
     (72 real WSSS rows) and, once (1) ships, would also start averaging
     a 41h-lead forecast into the same mean as a 17h one. Both are fixed
     by the same rule: the sample is the forecasts ISSUED DURING THE
     STATION'S OWN LOCAL TARGET DAY, which is exactly the set the live
     blend uses.

Without (2), (1) silently rewrites the bias correction on the live path.
That coupling is what these tests exist to pin.
"""

import sqlite3
from datetime import date

import pytest

import config
import pipeline
import storage
from clients import openmeteo_client
from models import PointForecast


PAYLOAD = {
    "daily": {
        "time": ["2026-08-10", "2026-08-11", "2026-08-12"],
        "temperature_2m_max": [32.0, 33.0, 34.0],
    }
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def at_2026_08_10(monkeypatch):
    monkeypatch.setattr(config, "local_today", lambda station: date(2026, 8, 10))


# --- (1) the extra days survive the fetch ----------------------------------

def test_the_series_keeps_every_day_the_api_returned(monkeypatch, at_2026_08_10):
    """
    forecast_days=3 was always being requested. One PointForecast per day
    must come back, each carrying ITS OWN target_date -- three rows all
    stamped "today" would be three copies of the same claim.
    """
    monkeypatch.setattr(
        openmeteo_client.requests, "get", lambda *a, **k: _Resp(PAYLOAD)
    )
    station = config.get_station("WSSS")

    series = openmeteo_client.get_ecmwf_forecast_series(station)

    assert [f.target_date for f in series] == [
        date(2026, 8, 10),
        date(2026, 8, 11),
        date(2026, 8, 12),
    ]
    assert [f.max_temp_c for f in series] == [32.0, 33.0, 34.0]
    assert {f.source for f in series} == {"open_meteo_ecmwf"}


def test_a_day_already_over_is_not_stored_as_a_forecast(monkeypatch, at_2026_08_10):
    """
    Open-Meteo's window can open before local_today when the station's
    timezone and the API's disagree at the boundary. A past date coming
    back would be an OBSERVATION wearing a forecast's source name, and it
    would enter the error sample with perfect hindsight.
    """
    payload = {
        "daily": {
            "time": ["2026-08-09", "2026-08-10", "2026-08-11"],
            "temperature_2m_max": [31.0, 32.0, 33.0],
        }
    }
    monkeypatch.setattr(
        openmeteo_client.requests, "get", lambda *a, **k: _Resp(payload)
    )

    series = openmeteo_client.get_ecmwf_forecast_series(config.get_station("WSSS"))

    assert [f.target_date for f in series] == [date(2026, 8, 10), date(2026, 8, 11)]


def test_gather_forecasts_stores_the_day_ahead_rows_but_blends_only_today(
    monkeypatch, at_2026_08_10
):
    """
    THE SAFETY PROPERTY OF THE WHOLE CHANGE. gather_forecasts()' return
    value goes straight into calibration.blend_central_estimate(), which
    averages whatever it is handed with no target-date filter of its own.
    Letting tomorrow's forecast into that list would blend tomorrow's
    weather into today's price.
    """
    monkeypatch.setattr(
        openmeteo_client.requests, "get", lambda *a, **k: _Resp(PAYLOAD)
    )

    class _Official:
        def get_24hr_forecast(self, station):
            return None

    monkeypatch.setattr(pipeline, "get_official_client", lambda key: _Official())

    saved = []
    monkeypatch.setattr(storage, "save_forecast", saved.append)

    blended = pipeline.gather_forecasts(config.get_station("WSSS"))

    # Everything reached storage, including the two day-ahead rows.
    assert {f.target_date for f in saved} == {
        date(2026, 8, 10),
        date(2026, 8, 11),
        date(2026, 8, 12),
    }
    # Only today's reached the blend.
    assert blended, "today's forecasts must still be blended"
    assert {f.target_date for f in blended} == {date(2026, 8, 10)}


def test_the_request_asks_for_four_days(monkeypatch, at_2026_08_10):
    """
    THE PARAMETER IS LOAD-BEARING AND NOTHING ELSE PINS IT. Fetches land in
    UTC hours 21-23, which at UTC+8 is already the next local day, so
    forecast_days:N reaches targets fetch_day+1..+N. At N=3 the furthest
    lead to the end of that local day is 67h and spread_audit.py's 72h
    cutoff can NEVER return a row -- a silent dead end that looks exactly
    like "not enough data yet" and cannot be fixed by waiting. Raised to 4
    on 2026-08-24 (~91h) for that reason alone.

    Dropping this back to 3 must fail here rather than quietly re-opening
    the dead end months later.
    """
    seen = {}

    def _capture(*args, **kwargs):
        seen.update(kwargs.get("params", {}))
        return _Resp(PAYLOAD)

    monkeypatch.setattr(openmeteo_client.requests, "get", _capture)

    openmeteo_client.get_ecmwf_forecast_series(config.get_station("WSSS"))

    assert seen.get("forecast_days") == 4, (
        f"expected forecast_days=4, got {seen.get('forecast_days')} -- "
        "at 3 the 72h lead row is structurally unreachable"
    )


# --- (2) the local-day window ----------------------------------------------

def _db_with(tmp_path, monkeypatch, forecast_rows):
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
    conn.execute(
        "INSERT INTO observations VALUES ('WSSS','2026-08-10',32.0,'metar_daily_max')"
    )
    for source, temp, fetched_at in forecast_rows:
        conn.execute(
            "INSERT INTO forecasts VALUES ('WSSS',?,'2026-08-10',?,?,'')",
            (source, temp, fetched_at),
        )
    conn.commit()
    conn.close()


# WSSS is UTC+8, so its 2026-08-10 spans 2026-08-09T16:00Z .. 2026-08-10T16:00Z.
IN_WINDOW = "2026-08-09T22:00:00+00:00"     # 06:00 local on the target day
AFTER_DAY_ENDED = "2026-08-10T23:50:00+00:00"   # 07:50 local the NEXT day
DAY_AHEAD = "2026-08-08T22:00:00+00:00"     # issued a day before the day began


def test_error_sample_drops_a_forecast_fetched_after_the_local_day_ended(
    tmp_path, monkeypatch
):
    """
    The 72 real WSSS rows. `date(fetched_at) <= target_date` is a UTC
    comparison and passes them: 2026-08-10T23:50Z shares a UTC date with
    the target while sitting 7h50m past the local day's end. That row has
    SEEN the maximum it claims to forecast, and it drags the measured
    spread down -- lookahead in the direction that makes the model look
    more certain than it is.
    """
    _db_with(
        tmp_path,
        monkeypatch,
        [("a", 30.0, IN_WINDOW), ("b", 32.0, AFTER_DAY_ENDED)],
    )
    # Only the in-window row counts: 30.0 - 32.0. Averaging both would
    # give 31.0 - 32.0 = -1.0.
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


def test_error_sample_drops_a_day_ahead_forecast(tmp_path, monkeypatch):
    """
    THE COUPLING. Once day-ahead rows are stored, a 41h-lead forecast sits
    in the same table as the 17h one the blend actually used. Averaging
    them measures a bias for a forecast set nobody trades on -- the exact
    drift forecast_means_by_date()'s docstring warns about.
    """
    _db_with(
        tmp_path,
        monkeypatch,
        [("a", 30.0, IN_WINDOW), ("a", 26.0, DAY_AHEAD)],
    )
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


def test_forecast_means_by_date_uses_the_same_window(tmp_path, monkeypatch):
    """
    The two must not drift. bucket_bias.py scores against this one, and a
    bias measured over a different forecast set than the blend averages is
    not the number anybody thinks it is.
    """
    _db_with(
        tmp_path,
        monkeypatch,
        [("a", 30.0, IN_WINDOW), ("b", 32.0, AFTER_DAY_ENDED), ("c", 26.0, DAY_AHEAD)],
    )
    assert storage.forecast_means_by_date("WSSS") == {date(2026, 8, 10): 30.0}


def test_a_target_date_with_only_out_of_window_forecasts_drops_out(
    tmp_path, monkeypatch
):
    """
    No forecast in the window means no blended estimate that day, so
    there is no error to measure -- the date leaves the sample rather than
    being scored on a row the live path would never have used.
    """
    _db_with(tmp_path, monkeypatch, [("b", 32.0, AFTER_DAY_ENDED)])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == []
    assert storage.forecast_means_by_date("WSSS") == {}
