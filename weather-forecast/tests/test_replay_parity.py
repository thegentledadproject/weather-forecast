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


# ---------------------------------------------------------------------------
# (b) the engine's entry decision == production's, on the same rows/prices
# ---------------------------------------------------------------------------

DEPTH_USD = 400.0


def _slip(size_usd, depth=DEPTH_USD):
    return 0.005 + 0.1 * size_usd / depth


class _Fill:
    """Duck-typed FillModel: the depth/slippage the market_client stubs report."""

    def depth_at(self, snapshot):
        return None if snapshot is None else snapshot["depth"]

    def slippage(self, size_usd, depth_usd):
        return _slip(size_usd, depth_usd)


def _ev_rows(icao, day):
    import ev_engine
    from models import EVResult

    cal = ev_engine._calibration_for(icao, day)
    rows = []
    model = {30: 0.05, 31: 0.20, 32: 0.45, 33: 0.25, 34: 0.05}
    ask = {30: 0.04, 31: 0.12, 32: 0.30, 33: 0.33, 34: 0.02}
    for b in sorted(model):
        for side in ("YES", "NO"):
            mp = model[b] if side == "YES" else 1 - model[b]
            px = ask[b] if side == "YES" else round(1 - ask[b] + 0.03, 3)
            fee = ev_engine.taker_fee_pct_of_notional(px)
            slip = _slip(ev_engine.DEFAULT_TRADE_SIZE_USD)
            cp, cs = ev_engine.apply_side_calibration(cal, side, mp)
            rows.append(EVResult(
                station_icao=icao, target_date=day, bucket_c=b, side=side, model_prob=mp,
                market_price=px, raw_edge=mp - px, estimated_slippage_pct=slip, fee_rate_pct=fee,
                net_ev_per_dollar=(mp - px) / px - slip - fee, spread_source="corrected_error",
                market_bid=round(px - 0.02, 3), calibrated_prob=cp, calibration_source=cs,
            ))
    return rows


@pytest.mark.parametrize("sources", [
    ["open_meteo_ecmwf", "open_meteo_gfs"],   # the fitted mix: stage 0 passes
    ["open_meteo_ecmwf"],                     # a different mix: stage 0 refuses
], ids=["gate_open", "gate_mix_refuses"])
def test_b_engine_entry_decisions_equal_production(dbs, monkeypatch, sources):
    import executor
    import ev_engine
    from backtest import engine
    from backtest.portfolio import PortfolioState
    from clients import market_client

    full, _cut = dbs
    day = (AS_OF + timedelta(hours=8)).date()
    # Today's own legs would sit in storage for live but not in the replay's
    # fresh PortfolioState; the unit here is the decision, so start both empty.
    b_db = full.parent / "b.sqlite3"
    shutil.copy(full, b_db)
    conn = sqlite3.connect(str(b_db))
    conn.execute("DELETE FROM positions WHERE target_date >= ?", (day.isoformat(),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(executor, "EXECUTION_MODE", {icao: "paper" for icao in config.STATIONS})
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda token, *a, **k: DEPTH_USD)
    monkeypatch.setattr(market_client, "estimate_slippage", lambda token, size, *a, **k: _slip(size))

    config.DB_PATH = str(b_db)
    as_of_mod.pin(AS_OF)
    try:
        station = config.get_station("WSSS")
        min_net_ev = 0.10
        screened = ev_engine.best_opportunities(_ev_rows("WSSS", day), min_net_ev=min_net_ev)
        token_map = {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in range(30, 35)}

        live = entry_manager.decide_portfolio_entries(
            screened, token_map, min_net_ev=min_net_ev, forecast_sources=sources,
            execution_mode="paper",
        )
        _cands, replay = engine.decide_entries(
            station=station, day=day, screened=screened, token_map=token_map,
            forecast_sources=sources,
            portfolio=PortfolioState(bankroll_usd=config.region_bankroll_usd("WSSS")),
            fill_model=_Fill(), price_lookup=lambda token_id: {"depth": DEPTH_USD},
            min_net_ev=min_net_ev,
        )
    finally:
        as_of_mod.release()

    key = lambda ds: [(d.bucket_c, d.side, d.approved, d.rule_id, d.recommended_size_usd)  # noqa: E731
                      for d in ds]
    assert key(replay) == key(live)
    assert len(live) >= 3
    rules = {d.rule_id for d in live}
    if sources == ["open_meteo_ecmwf"]:
        assert rules == {"collection_gate"}
    else:
        assert "collection_gate" not in rules and any(d.approved for d in live)


def test_engine_run_is_pinned_and_refuses_arrears_stations():
    from backtest import engine

    with pytest.raises(ValueError, match="arrears"):
        engine.run("VHHH", date(2026, 9, 1), date(2026, 9, 2))
    assert storage.get_as_of() is None and config._PINNED_NOW is None
