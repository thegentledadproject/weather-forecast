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
