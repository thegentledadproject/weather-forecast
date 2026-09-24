"""
GAP 3: the replay runs production's own decision readers point-in-time.

(a) Every reader, under storage/config as-of pins on the FULL database,
    equals the same reader on a COPY of the database truncated to as_of by
    an independent (Python-side) cut.
(c) Deleting the trap rows (everything written after as_of) changes nothing
    under the pin.
Plus the guards: the pin refuses in a writable process.
"""

import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

import calibration
import config
import entry_manager
import probability_calibration
import storage
from backtest import as_of as as_of_mod

STATIONS = ("WSSS", "WMKK")  # both UTC+8, metar_daily_max
D0 = date(2026, 8, 1)
N_DAYS = 40
AS_OF = datetime(2026, 9, 5, 0, 30, tzinfo=timezone.utc)  # local 08:30 on Sep 5


def _iso(dt):
    return dt.isoformat()


def _local(day, hour, minute=0):
    """UTC instant of local (UTC+8) hour:minute on `day`."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=8)


def _build(path):
    config.DB_PATH = str(path)
    storage.migrate()
    conn = sqlite3.connect(str(path))
    for s_i, icao in enumerate(STATIONS):
        for i in range(N_DAYS):
            day = D0 + timedelta(days=i)
            truth = 31.0 + (i % 4) * 0.5 + s_i
            for src, off in (("open_meteo_ecmwf", 0.6), ("open_meteo_gfs", -0.2 + 0.1 * (i % 3))):
                # the morning fetch (inside the 04:00-08:00 error window) and a late one
                conn.execute("INSERT INTO forecasts VALUES (?,?,?,?,?,?)",
                             (icao, src, day.isoformat(), truth + off, _iso(_local(day, 5, 7)), ""))
                conn.execute("INSERT INTO forecasts VALUES (?,?,?,?,?,?)",
                             (icao, src, day.isoformat(), truth + off + 3, _iso(_local(day, 15)), ""))
            conn.execute("INSERT INTO observations VALUES (?,?,?,?)",
                         (icao, day.isoformat(), truth, "metar_daily_max"))
            bucket = int(round(truth))
            conn.execute(
                "INSERT INTO settled_buckets (station_icao, target_date, bucket_c, bucket_min_c, "
                "bucket_max_c, source, recorded_at) VALUES (?,?,?,?,?,?,?)",
                (icao, day.isoformat(), bucket, 27, 37, "test", _iso(_local(day + timedelta(days=1), 3))))
            conn.execute("INSERT INTO ensemble_spread VALUES (?,?,?,?,?)",
                         (icao, day.isoformat(), 0.5 + 0.01 * i, 51, _iso(_local(day, 5, 5))))
            for k, (b, side, mp) in enumerate(((bucket, "YES", 0.4 + 0.02 * (i % 5)),
                                                (bucket + 1, "YES", 0.3 + 0.03 * (i % 4)),
                                                (bucket - 1, "NO", 0.6 + 0.02 * (i % 6)))):
                won = (b == bucket) == (side == "YES")
                conn.execute(
                    "INSERT INTO positions (position_id, station_icao, target_date, bucket_c, side, "
                    "entry_price, size_usd, entry_time, status, high_water_mark, exit_price, exit_time, "
                    "exit_reason, is_paper, execution_mode, model_prob) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{icao}-{day}-{k}", icao, day.isoformat(), b, side, 0.4, 10.0,
                     _iso(_local(day, 5, 10)), "closed_resolution", 0.4,
                     1.0 if won else 0.0, _iso(_local(day + timedelta(days=1), 2)),
                     "resolved", 1, "paper", mp))
    # explicit traps: a late settlement and a late-closing position for a
    # day BEFORE as_of, and an observation that is not yet published.
    conn.execute("UPDATE settled_buckets SET recorded_at=? WHERE target_date='2026-09-02'",
                 (_iso(AS_OF + timedelta(hours=5)),))
    conn.execute("UPDATE positions SET exit_time=? WHERE target_date='2026-09-03'",
                 (_iso(AS_OF + timedelta(hours=2)),))
    conn.commit()
    conn.close()


def _truncate(path):
    """Independent cut: Python datetimes, not the SQL views under test."""
    conn = sqlite3.connect(str(path))
    after = lambda s: s is not None and datetime.fromisoformat(s) > AS_OF  # noqa: E731
    local_visible_to = (AS_OF + timedelta(hours=8)).date() - timedelta(days=1)
    for table, col in (("forecasts", "fetched_at"), ("ensemble_spread", "fetched_at"),
                       ("settled_buckets", "recorded_at")):
        rows = conn.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
        conn.executemany(f"DELETE FROM {table} WHERE rowid=?", [(r,) for r, ts in rows if after(ts)])
    rows = conn.execute("SELECT rowid, target_date FROM observations").fetchall()
    conn.executemany("DELETE FROM observations WHERE rowid=?",
                     [(r,) for r, d in rows if date.fromisoformat(d) > local_visible_to])
    rows = conn.execute("SELECT position_id, entry_time, exit_time FROM positions").fetchall()
    for pid, entry, exit_ in rows:
        if after(entry):
            conn.execute("DELETE FROM positions WHERE position_id=?", (pid,))
        elif after(exit_):
            conn.execute("UPDATE positions SET status='open', exit_price=NULL, exit_time=NULL, "
                         "exit_reason=NULL WHERE position_id=?", (pid,))
    conn.commit()
    conn.close()


def _readings():
    today = config.local_today("WSSS")
    out = {}
    for icao in STATIONS:
        out[icao] = (
            entry_manager.forecast_bias_stats(icao),
            entry_manager.forecast_bias_source_mix(icao),
            entry_manager.resolution_obs_count(icao),
            calibration.error_width_ratio(icao),
            entry_manager.station_error_width_ratio(icao),
            calibration.estimate_std_dev([], [], None, station_icao=icao),
            calibration.pooled_error_spread(config.region_of(icao)),
            probability_calibration.calibration_for(icao, today),
            probability_calibration.calibration_for(icao, today, "NO"),
            len(storage.load_open_positions(icao)),
            sorted(storage.load_settled_buckets(icao)),
            storage.load_ensemble_spreads(icao),
        )
    return out


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", config.DB_PATH)
    full = tmp_path / "full.sqlite3"
    _build(full)
    cut = tmp_path / "cut.sqlite3"
    shutil.copy(full, cut)
    _truncate(cut)
    monkeypatch.setattr(storage, "_WRITABLE", False)
    yield full, cut
    as_of_mod.release()


def _read(path, pinned):
    config.DB_PATH = str(path)
    as_of_mod.pin(None)
    config.pin_now_utc(AS_OF)
    if pinned:
        storage.set_as_of(AS_OF)
    as_of_mod.clear_caches()
    try:
        return _readings()
    finally:
        as_of_mod.release()


def test_a_readers_under_as_of_equal_a_truncated_copy(dbs):
    full, cut = dbs
    pinned = _read(full, pinned=True)
    reference = _read(cut, pinned=False)
    assert pinned == reference
    # teeth: the unpinned full database answers differently
    assert _read(full, pinned=False) != reference
    # and the fixture actually reaches the measured tiers it claims to test
    bias, n, _ = reference["WSSS"][0]
    assert n and n >= 15
    assert reference["WSSS"][5][1] == "corrected_error"
    assert reference["WSSS"][7][1] in probability_calibration.CALIBRATED_TIERS
    assert reference["WSSS"][9] == 6  # today's 3 legs + the late-closing Sep-03 legs read OPEN


def test_c_deleting_the_trap_rows_changes_nothing(dbs):
    full, cut = dbs
    assert _read(full, pinned=True) == _read(cut, pinned=True)


def test_pins_refuse_in_a_writable_process(monkeypatch):
    monkeypatch.setattr(storage, "_WRITABLE", True)
    with pytest.raises(storage.StorageReadOnlyError):
        storage.set_as_of(AS_OF)
    with pytest.raises(RuntimeError):
        config.pin_now_utc(AS_OF)
    assert storage.get_as_of() is None and config._PINNED_NOW is None


def test_writable_connect_refuses_a_leftover_pin(monkeypatch):
    monkeypatch.setattr(storage, "_WRITABLE", False)
    storage.set_as_of(AS_OF)
    try:
        monkeypatch.setattr(storage, "_WRITABLE", True)
        with pytest.raises(storage.StorageReadOnlyError):
            storage._connect()
    finally:
        storage.set_as_of(None)
