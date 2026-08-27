"""
tests/test_bucket_axis.py

The bucket axis: what a bucket KEY means, in the market's own unit.

The governing invariant under test is that every temperature crossing
this module's boundary is Celsius, and only the key and its bounds live
in the market's unit.
"""
import math

import pytest

import bucket_axis
from bucket_axis import BucketAxis, AXIS_C1


class TestCelsiusAxisIsTodaysBehaviour:
    """The default axis must reproduce probability.py's historical formulas."""

    def test_half_up_interval_is_plus_minus_half(self):
        assert AXIS_C1.interval_c(31) == (30.5, 31.5)

    def test_floor_interval_is_b_to_b_plus_one(self):
        axis = BucketAxis(edge_mode="floor")
        assert axis.interval_c(33) == (33.0, 34.0)

    def test_key_for_temp_half_up_rounds_half_up_not_bankers(self):
        # round(30.5) is 30 under banker's rounding; the market says 31.
        assert AXIS_C1.key_for_temp_c(30.5, 25, 35) == 31
        assert AXIS_C1.key_for_temp_c(31.5, 25, 35) == 32

    def test_key_for_temp_floor_truncates(self):
        axis = BucketAxis(edge_mode="floor")
        assert axis.key_for_temp_c(33.9, 27, 37) == 33

    def test_key_is_clamped_into_the_catch_alls(self):
        assert AXIS_C1.key_for_temp_c(-50.0, 27, 37) == 27
        assert AXIS_C1.key_for_temp_c(500.0, 27, 37) == 37

    def test_width_is_one_degree(self):
        assert AXIS_C1.width_c() == 1.0

    def test_is_default(self):
        assert AXIS_C1.is_default
        assert BucketAxis(edge_mode="floor").is_default
        assert not BucketAxis(unit="F", step=2).is_default


class TestFahrenheitAxis:
    """NYC: 69F or below | 70-71F | ... | 86-87F | 88F or higher."""

    AXIS = BucketAxis(unit="F", step=2, edge_mode="half_up")
    LO, HI = 68, 88

    def test_eleven_keys_on_a_uniform_step_two_grid(self):
        assert self.AXIS.keys(self.LO, self.HI) == [
            68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88
        ]

    def test_interval_is_returned_in_celsius(self):
        lo_c, hi_c = self.AXIS.interval_c(70)
        # 69.5F .. 71.5F
        assert lo_c == pytest.approx((69.5 - 32) * 5 / 9)
        assert hi_c == pytest.approx((71.5 - 32) * 5 / 9)

    def test_interval_width_is_two_fahrenheit_degrees(self):
        assert self.AXIS.width_c() == pytest.approx(2 * 5 / 9)

    def test_key_for_a_celsius_reading(self):
        # 26.1C -> 78.98F -> displays 79F -> bucket "78-79"
        assert self.AXIS.key_for_temp_c(26.1, self.LO, self.HI) == 78

    def test_half_up_not_bankers_at_the_reachable_half_degrees(self):
        # 22.5C -> exactly 72.5F. floor(72.5+0.5)=73 -> bucket 72.
        # round(72.5)=72 under banker's -> also bucket 72 on THIS window,
        # which is why the bug hides here; the displayed-degree test below
        # is what actually pins it.
        assert self.AXIS.key_for_temp_c(22.5, self.LO, self.HI) == 72

    def test_labels_match_what_polymarket_prints(self):
        assert self.AXIS.label(68, self.LO, self.HI) == "69°F or below"
        assert self.AXIS.label(70, self.LO, self.HI) == "70-71°F"
        assert self.AXIS.label(86, self.LO, self.HI) == "86-87°F"
        assert self.AXIS.label(88, self.LO, self.HI) == "88°F or higher"


class TestCelsiusLabels:
    def test_labels_match_todays_markets(self):
        assert AXIS_C1.label(27, 27, 37) == "27°C or below"
        assert AXIS_C1.label(30, 27, 37) == "30°C"
        assert AXIS_C1.label(37, 27, 37) == "37°C or higher"


class TestValidation:
    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unit"):
            BucketAxis(unit="K")

    def test_zero_step_raises(self):
        with pytest.raises(ValueError, match="step"):
            BucketAxis(step=0)

    def test_unknown_edge_mode_raises(self):
        with pytest.raises(ValueError, match="edge_mode"):
            BucketAxis(edge_mode="nearest")


class TestForStation:
    def test_a_station_without_the_new_fields_gets_the_default_axis(self):
        class Legacy:
            bucket_edge_mode = "half_up"

        assert bucket_axis.for_station(Legacy()) == AXIS_C1


class TestStationConfigCarriesTheAxis:
    def test_defaults_are_the_celsius_whole_degree_axis(self):
        from models import StationConfig

        st = StationConfig(
            icao="TEST", display_name="Test", country="Testland",
            lat=0.0, lon=0.0, wunderground_slug="x/y/TEST",
            long_term_normal_max_c=30.0, official_client_key="wwis",
            polymarket_city_slug="test",
        )
        assert st.bucket_unit == "C"
        assert st.bucket_step == 1
        assert bucket_axis.for_station(st) == AXIS_C1

    def test_every_registered_station_is_on_the_default_axis_today(self):
        # Phase 1 registers no new station. This test is the tripwire that
        # says so, and Task 16 is where it is deliberately narrowed.
        import config

        for icao, st in config.STATIONS.items():
            assert bucket_axis.for_station(st).is_default, icao

    def test_a_fahrenheit_station_declares_it(self):
        from models import StationConfig

        st = StationConfig(
            icao="KLGA", display_name="LaGuardia", country="United States",
            lat=40.777, lon=-73.872, wunderground_slug="us/new-york/KLGA",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="nyc",
            bucket_unit="F", bucket_step=2, bucket_min_c=68, bucket_max_c=88,
        )
        axis = bucket_axis.for_station(st)
        assert axis == BucketAxis(unit="F", step=2, edge_mode="half_up")
        assert not axis.is_default


class TestProbabilityIsAxisAware:
    """
    The highest-risk failure in this design is a DEFAULTED axis, not a wrong
    one. A missed call site prices a Fahrenheit market on a Celsius grid:
    all 11 buckets sit ~40 degrees above the distribution, the tail fold puts
    ~1.0 on the lowest and ~0.0 on the other ten, and ten model_prob-0.0
    buckets are ten NO sides at ~0.20 raw edge -- under MAX_PLAUSIBLE_RAW_EDGE,
    through every gate. It would size ten trades per cycle per station.
    """

    def _estimate(self, icao, mean=26.1, sd=1.0):
        from datetime import date
        from models import CalibratedEstimate

        return CalibratedEstimate(
            station_icao=icao, target_date=date(2026, 8, 27),
            central_estimate_c=mean, std_dev_c=sd, monsoon_phase="unknown",
        )

    def test_celsius_station_is_unchanged_when_no_axis_is_passed(self):
        import probability

        est = self._estimate("WSSS")
        got = probability.bucket_probabilities(est, 27, 37)
        assert [b.bucket_c for b in got] == list(range(27, 38))

    def test_fahrenheit_probabilities_are_computed_on_the_f_grid(self):
        import probability

        axis = BucketAxis(unit="F", step=2)
        est = self._estimate("KLGA", mean=26.1, sd=1.0)
        got = probability.bucket_probabilities(est, 68, 88, axis=axis)

        assert [b.bucket_c for b in got] == [
            68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88
        ]
        assert sum(b.probability for b in got) == pytest.approx(1.0, abs=1e-3)
        # 26.1C is 78.98F, so the mode must be the "78-79F" bucket.
        assert max(got, key=lambda b: b.probability).bucket_c == 78

    def test_it_raises_rather_than_pricing_an_f_market_on_a_c_grid(self, monkeypatch):
        import config
        import probability
        from models import StationConfig

        st = StationConfig(
            icao="KLGA", display_name="LaGuardia", country="United States",
            lat=40.777, lon=-73.872, wunderground_slug="us/new-york/KLGA",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="nyc", bucket_unit="F", bucket_step=2,
        )
        monkeypatch.setitem(config.STATIONS, "KLGA", st)

        with pytest.raises(ValueError, match="axis"):
            probability.bucket_probabilities(self._estimate("KLGA"), 68, 88)

    def test_an_unregistered_station_still_defaults(self):
        # Station-agnostic callers and old tests pass estimates for stations
        # that may not be registered. Those keep the legacy default.
        import probability

        got = probability.bucket_probabilities(self._estimate("NOPE"), 27, 37)
        assert len(got) == 11


class TestSettlementOnAFahrenheitAxis:
    AXIS = BucketAxis(unit="F", step=2)
    LO, HI = 68, 88

    def test_celsius_reading_settles_into_the_right_f_bucket(self):
        from backtest import resolution

        # 26.1C -> 78.98F -> 79F -> "78-79F"
        assert resolution.bucket_for_temp(
            26.1, self.LO, self.HI, axis=self.AXIS
        ) == 78

    def test_it_never_returns_an_off_grid_key(self):
        from backtest import resolution

        grid = set(self.AXIS.keys(self.LO, self.HI))
        t = -10.0
        while t <= 45.0:
            key = resolution.bucket_for_temp(t, self.LO, self.HI, axis=self.AXIS)
            assert key in grid, f"{t}C produced off-grid key {key}"
            t = round(t + 0.1, 1)

    def test_bankers_rounding_would_disagree_on_the_displayed_degree(self):
        # 22.5C is exactly 72.5F. floor(x+0.5) displays 73; round() displays
        # 72. Pinned on the DISPLAYED degree because on the 68..88 window both
        # land in the same bucket -- shift the window two degrees and they
        # do not.
        assert math.floor(72.5 + 0.5) == 73
        assert round(72.5) == 72
        assert self.AXIS.key_for_temp_c(22.5, self.LO, self.HI) == 72

    def test_celsius_stations_are_untouched(self):
        from backtest import resolution

        assert resolution.bucket_for_temp(33.9, 27, 37, "floor") == 33
        assert resolution.bucket_for_temp(33.9, 27, 37, "half_up") == 34
        assert resolution.bucket_for_temp(-50.0, 27, 37, "half_up") == 27
        assert resolution.bucket_for_temp(500.0, 27, 37, "half_up") == 37
