"""
day_ahead.py records tomorrow's book the evening before, collection only.
Pinned: rows land in their OWN table (never ev_snapshots), under tomorrow's
date, priced with no calibration map and no shrink (both cache per day and
would freeze tomorrow's trading fit early), and once per station per evening.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import config
import day_ahead
import ev_engine
import market_discovery
import pipeline
import storage


def _stub_network(monkeypatch, calls):
    monkeypatch.setattr(pipeline, "gather_forecasts", lambda st, target_date=None: (calls.setdefault("fc_date", target_date), ["f"])[1])
    monkeypatch.setattr(pipeline, "gather_observations", lambda st, d: [])
    monkeypatch.setattr(day_ahead, "calibrate", lambda **kw: SimpleNamespace(central_estimate_c=31.2, std_dev_c=0.9, **kw))
    monkeypatch.setattr(market_discovery, "discover_token_map", lambda *a: {30: {}, 31: {}})
    monkeypatch.setattr(market_discovery, "derive_bucket_bounds", lambda tm, step: (30, 31))
    monkeypatch.setattr(day_ahead, "bucket_probabilities", lambda est, lo, hi, axis: [SimpleNamespace(bucket_c=30, probability=0.4), SimpleNamespace(bucket_c=31, probability=0.6)])
    monkeypatch.setattr(ev_engine, "fetch_market_quotes", lambda tm: {})

    def fake_table(estimate, token_map, **kw):
        calls["ev_kwargs"] = kw
        return [SimpleNamespace(bucket_c=b, side=s, model_prob=p, market_price=0.5, market_bid=0.48)
                for b, p in kw["model_probs"].items() for s in ("YES", "NO")]
    monkeypatch.setattr(ev_engine, "compute_ev_table", fake_table)
    import entry_manager
    monkeypatch.setattr(entry_manager, "forecast_bias_stats", lambda icao: (0.1, 20, 0.05))


def test_records_tomorrow_into_own_table_without_calibration(tmp_db, monkeypatch):
    calls = {}
    _stub_network(monkeypatch, calls)
    day_ahead._recorded.clear()
    tomorrow = config.local_today(config.get_station("WSSS")) + timedelta(days=1)

    assert day_ahead.record("WSSS") == 4

    assert calls["fc_date"] == tomorrow
    assert "calibration" not in calls["ev_kwargs"] and "shrink" not in calls["ev_kwargs"]
    with storage._db() as conn:
        rows = conn.execute("SELECT target_date, bucket_c, side, model_prob, central_c, std_dev_c "
                            "FROM ev_snapshots_day_ahead ORDER BY bucket_c, side").fetchall()
        assert conn.execute("SELECT COUNT(*) FROM ev_snapshots").fetchone()[0] == 0
    assert rows[0] == (tomorrow.isoformat(), 30, "NO", 0.4, 31.2, 0.9)
    assert len(rows) == 4


def test_due_once_per_evening_and_off_switch(monkeypatch):
    day_ahead._recorded.clear()
    monkeypatch.setattr(config, "DAY_AHEAD_RECORD_HOUR_LOCAL", 0)  # any hour qualifies
    assert day_ahead.due("WSSS")
    day_ahead._recorded["WSSS"] = config.local_today(config.get_station("WSSS")) + timedelta(days=1)
    assert not day_ahead.due("WSSS")
    day_ahead._recorded.clear()
    monkeypatch.setattr(config, "DAY_AHEAD_RECORD_HOUR_LOCAL", None)
    assert not day_ahead.due("WSSS")


def test_no_market_listed_records_nothing(tmp_db, monkeypatch):
    calls = {}
    _stub_network(monkeypatch, calls)
    monkeypatch.setattr(market_discovery, "discover_token_map", lambda *a: {})
    day_ahead._recorded.clear()
    assert day_ahead.record("WSSS") == 0


def test_gather_forecasts_filters_to_requested_day(monkeypatch):
    today = config.local_today(config.get_station("WSSS"))
    rows = [SimpleNamespace(target_date=today + timedelta(days=i), source="open_meteo_ecmwf") for i in range(3)]
    monkeypatch.setattr(pipeline.openmeteo_client, "get_ecmwf_forecast_series", lambda st: rows)
    monkeypatch.setattr(pipeline.openmeteo_client, "get_gfs_forecast_series", lambda st: [])
    monkeypatch.setattr(pipeline, "get_official_client", lambda key: SimpleNamespace(get_24hr_forecast=lambda st: None))
    monkeypatch.setattr(pipeline.storage, "save_forecast", lambda f: None)
    monkeypatch.setattr(config, "blendable_forecasts", lambda icao, fs: fs)
    st = config.get_station("WSSS")
    assert [f.target_date for f in pipeline.gather_forecasts(st)] == [today]
    assert [f.target_date for f in pipeline.gather_forecasts(st, target_date=today + timedelta(days=1))] == [today + timedelta(days=1)]
