"""
tests/test_cycle_calibration.py

One station-cycle must calibrate ONCE, and the estimate it prints must be
the estimate it trades on.

WHAT WENT WRONG. _run_full_cycle() called pipeline.run() -- a full fetch
and calibration whose only product was the printed table -- and then
immediately built a SECOND estimate for the EV leg, because only that one
knew the station's measured forecast bias. Two consequences, both live:

  * Every station-cycle fetched every forecast source twice. Verified in
    the live `forecasts` table 2026-08-29: 18 rows per station-cycle, two
    identical batches 2.3 seconds apart.

  * The printed table was the UNCORRECTED model. RCSS on 2026-08-28
    21:28 printed p(36C) = 0.41% and, in the same cycle, placed a real
    $1.10 live order on 36 YES at model_prob 0.2691. Anyone reading the
    log to sanity-check the book was reading a different model from the
    one spending money.

Both are the same defect: the bias belongs in pipeline.run(), and the
cycle should use what it returns.
"""

from datetime import date

import pytest

import config
import pipeline
import scheduler
from models import CalibratedEstimate, PointForecast

STATION = "WSSS"
TARGET = date(2026, 8, 30)


def _forecast(source: str, temp: float) -> PointForecast:
    return PointForecast(
        station_icao=STATION,
        source=source,
        target_date=TARGET,
        max_temp_c=temp,
        fetched_at="2026-08-30T05:00:00+00:00",
    )


@pytest.fixture
def offline_pipeline(monkeypatch):
    """
    pipeline.run() with every network seam replaced. Returns a dict
    counting how often each seam was reached, so a test can assert on the
    number of fetches as well as on the estimate.
    """
    calls = {"forecasts": 0, "observations": 0, "ensemble": 0}

    def _forecasts(station):
        calls["forecasts"] += 1
        return [_forecast("open_meteo_ecmwf", 32.0), _forecast("open_meteo_gfs", 32.0)]

    def _observations(station, target_date):
        calls["observations"] += 1
        return []

    def _ensemble(station, target_date):
        calls["ensemble"] += 1
        return []

    monkeypatch.setattr(pipeline, "gather_forecasts", _forecasts)
    monkeypatch.setattr(pipeline, "gather_observations", _observations)
    monkeypatch.setattr(pipeline, "ensemble_spread_for", _ensemble)
    monkeypatch.setattr(pipeline, "gather_same_day_signal", lambda station: "none")
    monkeypatch.setattr(config, "ENABLE_FORECAST_BIAS_CORRECTION", True)
    return calls


class TestPipelineRunAppliesTheBias:
    def test_a_measured_hot_bias_lowers_the_printed_estimate(self, offline_pipeline):
        uncorrected = pipeline.run(station_icao=STATION, target_date=TARGET)
        corrected = pipeline.run(
            station_icao=STATION, target_date=TARGET, forecast_bias_c=1.5
        )

        # forecast_bias_c is measured (forecast - settled truth), so a
        # positive bias means the sources run hot and the estimate comes
        # DOWN. Any other sign here would correct in the wrong direction.
        assert corrected["central_estimate_c"] < uncorrected["central_estimate_c"]

    def test_the_returned_estimate_is_the_one_the_table_was_built_from(
        self, offline_pipeline
    ):
        result = pipeline.run(
            station_icao=STATION, target_date=TARGET, forecast_bias_c=1.5
        )

        estimate = result["estimate"]
        assert isinstance(estimate, CalibratedEstimate)
        assert estimate.central_estimate_c == result["central_estimate_c"]


class TestOneCalibrationPerCycle:
    """The scheduler's half: use what pipeline.run() returned."""

    @pytest.fixture
    def cycle(self, monkeypatch, offline_pipeline):
        """_run_full_cycle with the market and entry legs stubbed out."""
        seen = {"priced": [], "calibrated": [], "fetches": offline_pipeline}

        # Both references, because they are resolved differently: pipeline
        # binds `calibrate` at import, the scheduler imports it inside the
        # function. Patching one alone would count half the calibrations.
        import calibration

        real_calibrate = calibration.calibrate

        def _spy_calibrate(*args, **kwargs):
            estimate = real_calibrate(*args, **kwargs)
            seen["calibrated"].append(estimate)
            return estimate

        monkeypatch.setattr(calibration, "calibrate", _spy_calibrate)
        monkeypatch.setattr(pipeline, "calibrate", _spy_calibrate)

        monkeypatch.setattr(pipeline, "print_summary", lambda result: None)
        monkeypatch.setattr(scheduler, "_run_exit_check", lambda *a, **kw: None)
        monkeypatch.setattr(
            scheduler, "_ingest_resolution_observations", lambda icaos: None
        )

        import entry_manager

        monkeypatch.setattr(
            entry_manager, "forecast_bias_stats", lambda icao: (1.5, 20, 0.2)
        )

        def _price(estimate, **kwargs):
            seen["priced"].append(estimate)
            raise RuntimeError("EV leg stubbed -- this test only checks calibration")

        import ev_engine

        monkeypatch.setattr(ev_engine, "run_for_station_with_map", _price)
        return seen

    def test_the_cycle_fetches_its_forecasts_exactly_once(self, cycle):
        scheduler._run_full_cycle(STATION, min_net_ev=0.15)

        assert cycle["fetches"]["forecasts"] == 1, (
            "the cycle fetched every forecast source more than once -- the "
            "duplicate fetch is back"
        )

    def test_the_ev_leg_prices_the_estimate_that_was_printed(self, cycle):
        scheduler._run_full_cycle(STATION, min_net_ev=0.15)

        assert cycle["priced"], "the EV leg never ran"
        assert len(cycle["calibrated"]) == 1, (
            "the cycle calibrated more than once -- the printed table and "
            "the traded table can drift apart again"
        )
        # The SAME object, not merely an equal one: that is what makes the
        # printed table and the priced table the same model by
        # construction rather than by coincidence.
        assert cycle["priced"][0] is cycle["calibrated"][0]

    def test_the_measured_station_bias_reaches_the_printed_estimate(self, cycle):
        scheduler._run_full_cycle(STATION, min_net_ev=0.15)

        estimate = cycle["calibrated"][0]
        # The one calibration is the corrected one: 32.0 blended, less the
        # 1.5C the station's record says its sources run hot.
        assert estimate.forecast_bias_c == pytest.approx(1.5)
        assert estimate.central_estimate_c < 32.0
