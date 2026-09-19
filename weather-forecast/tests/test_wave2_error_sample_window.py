# tests/test_wave2_error_sample_window.py
"""
Wave 2 item 2a. The bias / RMSE / spread / source-mix error sample is fitted
on forecast rows fetched INSIDE the pre-entry window -- local 04:00-08:00 of
the target day (config.SCHEDULE_WINDOWS: collection 04:00-05:00, entries
05:00-08:00) -- not on every row fetched during the local day. A row fetched
at 14:00 local has, at most stations, already seen the afternoon maximum it
claims to forecast, and the sd measured on it is not the sd the 05:00 entry
decision faces (edge review 2026-09-15: morning-only sd +6.5%).

ONE window, ONE implementation: storage.forecast_rows_in_error_sample is the
pure filter, and every consumer (bias, corrected RMSE, measured spread, the
pooled spread, bucket_bias, the mix guard) reaches it through
storage._forecast_rows_in_sample_window. wave2_falsifier.py reuses the pure
function on rows it fetched read-only.
"""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import config
import storage

TARGET = date(2026, 8, 10)
# WSSS is UTC+8: its 2026-08-10 runs 2026-08-09T16:00Z .. 2026-08-10T16:00Z,
# so local 04:00 is 2026-08-09T20:00Z and local 08:00 is 2026-08-10T00:00Z.
AT_0359 = "2026-08-09T19:59:00+00:00"
AT_0500 = "2026-08-09T21:00:00+00:00"
AT_0800 = "2026-08-10T00:00:00+00:00"
AT_1400 = "2026-08-10T06:00:00+00:00"


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


def test_the_window_bounds_are_local_04_to_08():
    lo, hi = config.error_sample_fetch_bounds_utc("WSSS", TARGET)
    assert lo == datetime(2026, 8, 9, 20, tzinfo=timezone.utc)
    assert hi == datetime(2026, 8, 10, 0, tzinfo=timezone.utc)


def test_disabled_bounds_are_the_whole_local_day():
    assert config.error_sample_fetch_bounds_utc("WSSS", TARGET, enabled=False) == \
        config.local_day_bounds_utc("WSSS", TARGET)


def test_the_flag_is_the_default_for_the_bounds(monkeypatch):
    monkeypatch.setattr(config, "ERROR_SAMPLE_FETCH_WINDOW_ENABLED", False)
    assert config.error_sample_fetch_bounds_utc("WSSS", TARGET) == \
        config.local_day_bounds_utc("WSSS", TARGET)


def test_a_1400_local_fetch_is_excluded_and_a_0500_fetch_included(tmp_path, monkeypatch):
    """THE CHANGE. Only the 05:00 row counts: 30.0 - 32.0. Averaging both
    would give 32.0 - 32.0 = 0.0, i.e. the afternoon row -- which has seen
    the maximum -- would zero the measured error."""
    _db_with(tmp_path, monkeypatch, [("a", 30.0, AT_0500), ("b", 34.0, AT_1400)])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


def test_the_window_is_half_open_at_both_ends(tmp_path, monkeypatch):
    """03:59 is before collection opens; 08:00 is the instant entries shut.
    Neither is a fetch an entry decision could have used."""
    _db_with(tmp_path, monkeypatch, [
        ("a", 30.0, AT_0500), ("b", 20.0, AT_0359), ("c", 40.0, AT_0800),
    ])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [-2.0]


def test_flag_off_restores_the_local_day_sample(tmp_path, monkeypatch):
    """The revert path: every row inside the local day counts again."""
    monkeypatch.setattr(config, "ERROR_SAMPLE_FETCH_WINDOW_ENABLED", False)
    _db_with(tmp_path, monkeypatch, [("a", 30.0, AT_0500), ("b", 34.0, AT_1400)])
    assert storage.forecast_error_samples("WSSS", "metar_daily_max") == [0.0]


def test_the_means_and_the_mix_share_the_window(tmp_path, monkeypatch):
    """bucket_bias scores forecast_means_by_date and the mix guard reads
    forecast_source_mix_by_date; both must see the same rows the bias saw."""
    _db_with(tmp_path, monkeypatch, [("a", 30.0, AT_0500), ("b", 34.0, AT_1400)])
    assert storage.forecast_means_by_date("WSSS") == {TARGET: 30.0}
    assert storage.forecast_source_mix_by_date("WSSS") == {TARGET: frozenset({"a"})}


def test_the_pure_filter_is_what_the_storage_path_runs(tmp_path, monkeypatch):
    _db_with(tmp_path, monkeypatch, [
        ("a", 30.0, AT_0500), ("b", 34.0, AT_1400), ("c", 31.0, AT_0359),
    ])
    rows = storage.forecast_rows_with_fetch_time("WSSS")
    assert storage.forecast_rows_in_error_sample("WSSS", rows) == \
        storage._forecast_rows_in_sample_window("WSSS")
    assert storage.forecast_rows_in_error_sample("WSSS", rows, window_enabled=False) == {
        TARGET: [("a", 30.0), ("b", 34.0), ("c", 31.0)],
    }


def test_the_old_local_day_name_is_gone():
    """The rename is deliberate: a function called _in_local_day that applies
    a four-hour window is the stale-comment problem in a function name."""
    assert not hasattr(storage, "_forecast_rows_in_local_day")


def test_the_stale_lookahead_comments_are_corrected():
    """
    Spec 2a: 'storage.py comment at the day filter corrected'. Three
    docstrings still described the pre-2026-08-21 UTC comparison, and one
    said the sample 'mirrors blend_central_estimate's own forecast term' --
    which it no longer does once the window is narrower than the blend's.
    """
    assert "date(fetched_at) <= target_date" not in storage.forecast_rows_with_fetch_time.__doc__
    assert "date(f.fetched_at) <= target_date" not in storage.forecast_means_by_date.__doc__
    assert "on or before the target date" not in storage.forecast_error_samples.__doc__
    assert "mirrors blend_central_estimate" not in storage.forecast_error_samples.__doc__
    assert "ERROR_SAMPLE_FETCH_WINDOW_LOCAL" in storage.forecast_error_samples.__doc__
