"""
Gap 8, part B: an entry_decisions row carries the estimate it was decided on
-- central estimate, spread, bias, spread tier, and the fetched_at of every
forecast row the estimate actually blended -- so a bad trade can be rebuilt
from rows instead of guessed at.
"""
import json
from datetime import date

import calibration
import config
import entry_manager
import ev_engine
import storage
from models import CalibratedEstimate, EVResult, MarketQuote, PointForecast

DAY = date(2026, 9, 20)
FIVE = ("mu_c", "sd_c", "bias_c", "spread_source", "forecast_fetched_at")


def _forecasts():
    return [
        PointForecast("WSSS", "open_meteo_ecmwf", DAY, 32.1, "2026-09-20T01:00:00.100000+00:00"),
        PointForecast("WSSS", "open_meteo_gfs", DAY, 31.7, "2026-09-20T01:00:00.200000+00:00"),
        # No value: the blend skips it, so the record must too.
        PointForecast("WSSS", "nea_24hr", DAY, None, "2026-09-20T01:00:00.300000+00:00"),
    ]


def test_calibrate_records_the_fetched_at_of_the_rows_it_blended():
    est = calibration.calibrate(config.get_station("WSSS"), DAY, _forecasts(), [])
    assert est.forecast_fetched_at == [
        "2026-09-20T01:00:00.100000+00:00", "2026-09-20T01:00:00.200000+00:00",
    ]


def test_ev_table_rows_carry_their_estimate(monkeypatch):
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.0)
    est = CalibratedEstimate("WSSS", DAY, 32.0, 1.0, "southwest", spread_source="measured_error")
    rows = ev_engine.compute_ev_table(
        est, {32: {"yes_token_id": "y", "no_token_id": "n"}},
        quotes={32: MarketQuote(bucket_c=32, yes_price=0.30, no_price=None)},
    )
    assert {id(r.estimate) for r in rows} == {id(est)}  # priced and unpriced rows alike


def test_a_decision_row_round_trips_all_five_inputs(tmp_db):
    est = calibration.calibrate(
        config.get_station("WSSS"), DAY, _forecasts(), [], forecast_bias_c=0.4,
    )
    ev = EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side="YES",
        model_prob=0.5, market_price=0.3, raw_edge=0.2, estimated_slippage_pct=0.0,
        fee_rate_pct=0.02, net_ev_per_dollar=0.5, spread_source=est.spread_source,
        estimate=est,
    )
    d = entry_manager.collection_only_decision(ev, "TOK", "collecting")

    storage.record_entry_decisions([d], book="paper", cycle_ts="2026-09-20T01:05:00+00:00",
                                   config_sha="abc")
    (row,) = storage.load_entry_decisions()

    assert row["mu_c"] == est.central_estimate_c
    assert row["sd_c"] == est.std_dev_c
    assert row["bias_c"] == est.forecast_bias_c
    assert row["spread_source"] == est.spread_source
    assert json.loads(row["forecast_fetched_at"]) == est.forecast_fetched_at
    assert len(est.forecast_fetched_at) == 2


def test_a_decision_without_an_estimate_records_nulls(tmp_db):
    """Duck-typed replay stubs and manual_trigger have no estimate: NULL, not a crash."""
    ev = EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side="YES",
        model_prob=0.5, market_price=0.3, raw_edge=0.2, estimated_slippage_pct=0.0,
        fee_rate_pct=0.02, net_ev_per_dollar=0.5,
    )
    d = entry_manager.collection_only_decision(ev, "TOK", "collecting")
    storage.record_entry_decisions([d], book="paper", cycle_ts="c", config_sha=None)
    (row,) = storage.load_entry_decisions()
    assert [row[c] for c in FIVE if c != "spread_source"] == [None] * 4
