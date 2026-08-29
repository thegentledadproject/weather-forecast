"""
tests/test_spread_tier_brier_days.py

spread_tier_brier.build_days() turns the stored record into the settled
station-days the sweep scores. Everything that could quietly flatter a tier
lives here rather than in the arithmetic:

  - the day being scored must not appear in its own bias correction,
  - only observations from STRICTLY EARLIER dates may enter the observed
    term (a same-day reading is the answer),
  - the listed bounds and the axis must come from the settlement row that
    day actually recorded, not from today's registry.
"""
from datetime import date, timedelta

import pytest

import config
import spread_tier_brier as stb
import storage
from models import ObservedReading, PointForecast

STATION = "WSSS"
SOURCE = "open_meteo_ecmwf"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))


def _seed_forecast(target_date, value, fetched_at=None):
    storage.save_forecast(PointForecast(
        station_icao=STATION, source=SOURCE, target_date=target_date,
        max_temp_c=value,
        fetched_at=fetched_at or f"{target_date.isoformat()}T02:00:00+00:00",
    ))


def _seed_truth(target_date, value, bucket, bucket_min=28, bucket_max=34):
    station = config.get_station(STATION)
    storage.save_observation(ObservedReading(
        station_icao=STATION, target_date=target_date, max_temp_c=value,
        source=station.resolution_grade_source,
    ))
    storage.save_settled_bucket(
        STATION, target_date, bucket_c=bucket,
        bucket_min_c=bucket_min, bucket_max_c=bucket_max, source="metar",
    )


def _seed_run(days, start=date(2026, 8, 1), forecast=32.0, truth=31.0, bucket=31):
    for i in range(days):
        d = start + timedelta(days=i)
        _seed_forecast(d, forecast)
        _seed_truth(d, truth, bucket)


class TestBuildDays:
    def test_one_scored_day_per_settled_date_with_a_forecast(self, db):
        _seed_run(4)

        days = stb.build_days(STATION)

        assert [d.target_date for d in days] == [
            date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4),
        ]
        assert all(d.settled_bucket == 31 for d in days)

    def test_a_settled_day_with_no_stored_forecast_is_skipped(self, db):
        _seed_run(3)
        _seed_truth(date(2026, 8, 9), 31.0, 31)  # settled, but never forecast

        assert date(2026, 8, 9) not in [d.target_date for d in stb.build_days(STATION)]

    def test_a_forecast_day_that_never_settled_is_skipped(self, db):
        _seed_run(3)
        _seed_forecast(date(2026, 8, 9), 32.0)  # forecast, but no settlement

        assert date(2026, 8, 9) not in [d.target_date for d in stb.build_days(STATION)]

    def test_bounds_and_axis_come_from_the_settlement_row(self, db):
        _seed_run(3)
        _seed_forecast(date(2026, 8, 4), 32.0)
        _seed_truth(date(2026, 8, 4), 31.0, 31, bucket_min=25, bucket_max=29)

        day = next(d for d in stb.build_days(STATION) if d.target_date == date(2026, 8, 4))

        assert (day.bucket_min, day.bucket_max) == (25, 29)

    def test_the_scored_day_is_absent_from_its_own_bias_correction(self, db):
        """
        Four days run 1C warm; the fifth runs 5C warm. If day five's own
        error reached its correction, its centre would be pulled toward
        truth by more than the other four days alone can explain.
        """
        _seed_run(4, forecast=32.0, truth=31.0, bucket=31)
        odd = date(2026, 8, 5)
        _seed_forecast(odd, 36.0)
        _seed_truth(odd, 31.0, 31)

        days = {d.target_date: d for d in stb.build_days(STATION)}
        loo_bias = 1.0  # the four other days, each +1.0

        weight = config.forecast_blend_weight(STATION)
        observed_term = 31.0  # every earlier day settled at 31.0
        expected = round(weight * (36.0 - loo_bias) + (1 - weight) * observed_term, 1)
        assert days[odd].center_c == pytest.approx(expected)

    def test_a_same_day_observation_never_enters_the_observed_term(self, db):
        """
        The observed term is persistence, not the answer. A reading dated
        the target day is exactly the value being predicted.
        """
        _seed_run(3, forecast=32.0, truth=31.0, bucket=31)
        target = date(2026, 8, 4)
        _seed_forecast(target, 32.0)
        _seed_truth(target, 99.0, 31)  # absurd same-day truth

        day = next(d for d in stb.build_days(STATION) if d.target_date == target)

        assert day.center_c < 40.0, "same-day settled truth leaked into the observed term"

    def test_a_station_with_nothing_stored_yields_no_days(self, db):
        assert stb.build_days("RJTT") == []


class TestStationReport:
    def test_measured_tier_is_scored_on_clamped_leave_one_out_widths(self, db):
        """
        The measured tier's row must be what that tier would REALLY have
        produced: its own leave-one-out width, put through the same
        _clamp_spread the live path applies. An unclamped row would score a
        width production can never emit.
        """
        import calibration

        _seed_run(3, forecast=32.0, truth=31.0, bucket=31)
        for i, (f, t) in enumerate([(34.0, 31.0), (30.0, 31.0), (33.0, 31.0)], start=4):
            d = date(2026, 8, i)
            _seed_forecast(d, f)
            _seed_truth(d, t, 31)

        report = stb.station_report(STATION, grid=[0.5, 1.0, 1.5])

        assert report["measured"]["n"] > 0
        for width in report["measured"]["widths"].values():
            if width is not None:
                assert width == calibration._clamp_spread(width, STATION)

    def test_grid_optimum_is_reported_alongside_the_tier_rows(self, db):
        _seed_run(6)

        report = stb.station_report(STATION, grid=[0.5, 1.0, 1.5])

        assert report["best_grid"][0] in (0.5, 1.0, 1.5)
        assert report["n_days"] == 6

    def test_a_station_with_no_settled_days_reports_nothing_rather_than_raising(self, db):
        assert stb.station_report("RJTT", grid=[0.5, 1.0])["n_days"] == 0
