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


class TestDiscoveryParsesTheMarketsAxis:
    F_AXIS = BucketAxis(unit="F", step=2)
    NYC_LABELS = [
        "69°F or below", "70-71°F", "72-73°F", "74-75°F",
        "76-77°F", "78-79°F", "80-81°F", "82-83°F",
        "84-85°F", "86-87°F", "88°F or higher",
    ]

    def test_every_real_nyc_label_parses_onto_the_grid(self):
        import market_discovery as md

        got = [
            md.parse_bucket_label({"groupItemTitle": lab}, axis=self.F_AXIS)
            for lab in self.NYC_LABELS
        ]
        assert got == [68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88]

    def test_a_non_consecutive_pair_is_rejected_not_guessed(self):
        import market_discovery as md

        assert md.parse_bucket_label(
            {"groupItemTitle": "70-73°F"}, axis=self.F_AXIS
        ) is None

    def test_a_celsius_label_is_not_parsed_by_the_f_branch(self):
        import market_discovery as md

        assert md.parse_bucket_label(
            {"groupItemTitle": "31°C"}, axis=self.F_AXIS
        ) is None

    def test_the_date_in_a_question_is_still_thrown_out(self):
        import market_discovery as md

        q = ("Will the highest temperature in NYC on August 27, 2026 be "
             "80-81°F?")
        assert md.parse_bucket_label({"question": q}, axis=self.F_AXIS) == 80

    def test_sub_zero_celsius_keeps_its_sign(self):
        # Toronto and Buenos Aires. Today "-2C" parses as 2.
        import market_discovery as md

        assert md.parse_bucket_label({"groupItemTitle": "-2°C"}) == -2

    def test_celsius_parsing_is_otherwise_unchanged(self):
        import market_discovery as md

        assert md.parse_bucket_label({"groupItemTitle": "31°C"}) == 31
        assert md.parse_bucket_label(
            {"groupItemTitle": "27°C or below"}
        ) == 27


class TestDeriveBucketBoundsIsStepAware:
    def test_a_step_two_grid_is_accepted(self):
        import market_discovery as md

        tm = {k: {} for k in [68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88]}
        assert md.derive_bucket_bounds(tm, step=2) == (68, 88)

    def test_a_step_one_map_at_a_step_two_station_is_rejected(self):
        import market_discovery as md

        tm = {k: {} for k in range(78, 89)}
        assert md.derive_bucket_bounds(tm, step=2) is None

    def test_a_uniformly_shifted_odd_grid_is_rejected(self):
        import market_discovery as md

        tm = {k: {} for k in [69, 71, 73, 75, 77, 79, 81, 83, 85, 87, 89]}
        assert md.derive_bucket_bounds(tm, step=2) is None

    def test_a_short_map_is_still_rejected(self):
        import market_discovery as md

        tm = {k: {} for k in [68, 70, 72, 74, 76, 78, 80, 82, 84]}
        assert md.derive_bucket_bounds(tm, step=2) is None

    def test_celsius_behaviour_is_unchanged(self):
        import market_discovery as md

        assert md.derive_bucket_bounds({k: {} for k in range(27, 38)}) == (27, 37)
        assert md.derive_bucket_bounds({k: {} for k in range(27, 37)}) is None


class TestDeriveBucketBoundsReachesStepFromTheCallSites:
    """
    derive_bucket_bounds() defaults to step=1. Two callers outside
    market_discovery.py -- ev_engine.run_for_station_with_map() and
    position_manager._event_bounds() -- used to call it with no step at
    all, so on a Fahrenheit station the 11 real keys were checked against
    a 21-element step-1 range and vetoed EVERY cycle: the station would
    never trade, silently, because nothing raises -- derive_bucket_bounds
    is designed to fail closed. These pin that the station's own axis
    step actually reaches the call, not just that derive_bucket_bounds()
    accepts a step argument when given one directly.

    Driven with SPIES on derive_bucket_bounds rather than a real EV/quote
    pass: the property under test is narrowly "what step value reaches
    this one call", and going through fetch_market_quotes for real pulls
    in 22 live per-bucket lookups (both sides of 11 buckets, plus depth
    and slippage paths a couple of targeted mocks don't cover) for a fact
    that has nothing to do with quotes at all.
    """

    F_TOKEN_MAP = {
        b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"}
        for b in [68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88]
    }
    C_TOKEN_MAP = {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in range(27, 38)}

    def _f_station(self):
        from models import StationConfig

        return StationConfig(
            icao="KLGA", display_name="LaGuardia", country="United States",
            lat=40.777, lon=-73.872, wunderground_slug="us/new-york/KLGA",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="nyc",
            bucket_unit="F", bucket_step=2, bucket_min_c=68, bucket_max_c=88,
        )

    def _step_spy(self, monkeypatch, target):
        """Patches target.derive_bucket_bounds with a recorder that always
        succeeds -- returning the token map's own (min, max) so the bounds
        stay consistent with whichever map the test passed in -- and
        returns the dict the captured step lands in."""
        captured = {}

        def _spy(token_map, step=1):
            captured["step"] = step
            return (min(token_map), max(token_map))

        monkeypatch.setattr(target.market_discovery, "derive_bucket_bounds", _spy)
        return captured

    @staticmethod
    def _empty_quotes(token_map):
        """fetch_market_quotes()'s real contract: one entry per token_map
        key, price fields None rather than the key simply being absent --
        compute_ev_table indexes quotes[bucket_c] directly."""
        from models import MarketQuote

        return {b: MarketQuote(bucket_c=b, yes_price=None, no_price=None) for b in token_map}

    def test_ev_engine_passes_the_fahrenheit_stations_step(self, monkeypatch):
        from datetime import date

        import config
        import ev_engine
        from models import CalibratedEstimate

        station = self._f_station()
        monkeypatch.setitem(config.STATIONS, "KLGA", station)
        captured = self._step_spy(monkeypatch, ev_engine)
        monkeypatch.setattr(
            ev_engine.market_discovery, "discover_token_map",
            lambda st, d, lo=None, hi=None: self.F_TOKEN_MAP,
        )
        # The one stub at the boundary: what quotes come back is irrelevant
        # to this test, only that fetching them is never attempted for real.
        monkeypatch.setattr(ev_engine, "fetch_market_quotes", self._empty_quotes)

        estimate = CalibratedEstimate(
            station_icao="KLGA", target_date=date(2026, 8, 27),
            central_estimate_c=26.1, std_dev_c=1.0, monsoon_phase="unknown",
        )
        result = ev_engine.run_for_station_with_map(estimate)

        assert captured["step"] == 2
        assert not result.veto_reason, result.veto_reason

    def test_ev_engine_passes_one_for_a_celsius_station(self, monkeypatch):
        from datetime import date

        import config
        import ev_engine
        from models import CalibratedEstimate

        station = config.get_station("WSSS")
        captured = self._step_spy(monkeypatch, ev_engine)
        monkeypatch.setattr(
            ev_engine.market_discovery, "discover_token_map",
            lambda st, d, lo=None, hi=None: self.C_TOKEN_MAP,
        )
        monkeypatch.setattr(ev_engine, "fetch_market_quotes", self._empty_quotes)

        estimate = CalibratedEstimate(
            station_icao=station.icao, target_date=date(2026, 8, 27),
            central_estimate_c=32.0, std_dev_c=1.0, monsoon_phase="unknown",
        )
        ev_engine.run_for_station_with_map(estimate)

        assert captured["step"] == 1

    def test_position_manager_passes_the_fahrenheit_stations_step(self, monkeypatch):
        from datetime import date

        import position_manager
        from models import Position

        station = self._f_station()
        captured = self._step_spy(monkeypatch, position_manager)
        monkeypatch.setattr(
            position_manager.market_discovery, "discover_token_map",
            lambda st, d, lo=None, hi=None: self.F_TOKEN_MAP,
        )

        position = Position(
            position_id="KLGA:2026-08-27:70:YES:x",
            station_icao="KLGA",
            target_date=date(2026, 8, 27),
            bucket_c=70,
            side="YES",
            entry_price=0.30,
            size_usd=10.0,
            entry_time="2026-08-27T14:00:00",
            status="open",
            token_id="tok",
        )
        bounds = position_manager._event_bounds(position, station)

        assert captured["step"] == 2
        assert bounds == (68, 88)

    def test_position_manager_passes_one_for_a_celsius_station(self, monkeypatch):
        from datetime import date

        import config
        import position_manager
        from models import Position

        station = config.get_station("WSSS")
        captured = self._step_spy(monkeypatch, position_manager)
        monkeypatch.setattr(
            position_manager.market_discovery, "discover_token_map",
            lambda st, d, lo=None, hi=None: self.C_TOKEN_MAP,
        )

        position = Position(
            position_id="WSSS:2026-08-27:32:YES:x",
            station_icao="WSSS",
            target_date=date(2026, 8, 27),
            bucket_c=32,
            side="YES",
            entry_price=0.30,
            size_usd=10.0,
            entry_time="2026-08-27T14:00:00",
            status="open",
            token_id="tok",
        )
        position_manager._event_bounds(position, station)

        assert captured["step"] == 1


class TestBiasMidpointStaysCelsius:
    """
    The _c suffix on a RETURN VALUE is a promise. bucket_bias_samples
    subtracts this from a Celsius forecast mean and the result reaches
    calibration.blend_central_estimate, so a Fahrenheit number here is a
    live mispricing, not a display bug.
    """

    def test_a_fahrenheit_midpoint_is_returned_in_celsius(self):
        import bucket_bias

        axis = BucketAxis(unit="F", step=2)
        # Bucket "78-79F" spans 77.5F..79.5F, midpoint 78.5F = 25.833C
        got = bucket_bias.bucket_midpoint_c(78, (68, 88), "half_up", axis=axis)
        assert got == pytest.approx((78.5 - 32) * 5 / 9, abs=1e-6)

    def test_a_fahrenheit_midpoint_is_a_plausible_celsius_temperature(self):
        import bucket_bias

        axis = BucketAxis(unit="F", step=2)
        for key in axis.keys(70, 86):
            got = bucket_bias.bucket_midpoint_c(key, (68, 88), "half_up", axis=axis)
            assert -60.0 < got < 60.0, f"bucket {key} midpoint {got} is not Celsius"

    def test_celsius_midpoints_are_unchanged(self):
        import bucket_bias

        assert bucket_bias.bucket_midpoint_c(31, (27, 37), "half_up") == 31.0
        assert bucket_bias.bucket_midpoint_c(31, (27, 37), "floor") == 31.5

    def test_edge_buckets_are_still_censored(self):
        import bucket_bias

        axis = BucketAxis(unit="F", step=2)
        assert bucket_bias.bucket_midpoint_c(68, (68, 88), "half_up", axis=axis) is None
        assert bucket_bias.bucket_midpoint_c(88, (68, 88), "half_up", axis=axis) is None


class TestSettledBucketsAreSelfDescribing:
    def test_a_saved_row_round_trips_its_units(self, tmp_path, monkeypatch):
        from datetime import date
        import config
        import storage

        # No init step: storage._connect() creates the schema on first use.
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
        storage.save_settled_bucket(
            "KLGA", date(2026, 8, 27), 78, 68, 88, "metar_daily_max",
            bucket_unit="F", bucket_step=2,
        )
        got = storage.load_settled_buckets("KLGA")
        assert got[date(2026, 8, 27)] == (78, 68, 88, "F", 2)

    def test_legacy_rows_default_to_celsius_whole_degree(self, tmp_path, monkeypatch):
        from datetime import date
        import config
        import storage

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
        storage.save_settled_bucket(
            "WSSS", date(2026, 8, 27), 31, 27, 37, "metar_daily_max",
        )
        assert storage.load_settled_buckets("WSSS")[date(2026, 8, 27)] == (
            31, 27, 37, "C", 1
        )


def _all_axes_under_test():
    """Every registered station, plus the two axes no station has YET."""
    import config

    cases = [
        (icao, bucket_axis.for_station(st), st.bucket_min_c, st.bucket_max_c)
        for icao, st in config.STATIONS.items()
    ]
    cases.append(("SYNTH-F2", BucketAxis(unit="F", step=2), 68, 88))
    cases.append(("SYNTH-F2-COLD", BucketAxis(unit="F", step=2), 8, 28))
    return cases


@pytest.mark.parametrize("icao,axis,lo,hi", _all_axes_under_test())
class TestAxisPropertiesHoldForEveryStation:

    def test_the_key_a_reading_settles_into_contains_that_reading(
        self, icao, axis, lo, hi
    ):
        from backtest import resolution

        lo_c, _ = axis.interval_c(lo)
        _, hi_c = axis.interval_c(hi)
        t = round(lo_c - 3.0, 1)
        while t <= hi_c + 3.0:
            key = resolution.bucket_for_temp(t, lo, hi, axis=axis)
            k_lo, k_hi = axis.interval_c(key)
            if key == lo:
                assert t < k_hi, f"{icao}: {t}C clamped to {key}, above its top edge"
            elif key == hi:
                assert t >= k_lo, f"{icao}: {t}C clamped to {key}, below its low edge"
            else:
                assert k_lo <= t < k_hi, (
                    f"{icao}: {t}C settled into bucket {key} = [{k_lo}, {k_hi})"
                )
            t = round(t + 0.1, 1)

    def test_the_listed_buckets_tile_the_line(self, icao, axis, lo, hi):
        keys = axis.keys(lo, hi)
        assert len(keys) == 11, f"{icao}: {len(keys)} buckets, expected 11"
        for left, right in zip(keys, keys[1:]):
            _, left_top = axis.interval_c(left)
            right_bottom, _ = axis.interval_c(right)
            assert left_top == pytest.approx(right_bottom, abs=1e-9), (
                f"{icao}: gap or overlap between bucket {left} and {right}"
            )

    def test_the_probabilities_sum_to_one_and_the_mode_is_where_it_should_be(
        self, icao, axis, lo, hi
    ):
        from datetime import date

        import probability
        from models import CalibratedEstimate

        lo_c, _ = axis.interval_c(lo)
        _, hi_c = axis.interval_c(hi)
        centre = (lo_c + hi_c) / 2
        est = CalibratedEstimate(
            station_icao=icao, target_date=date(2026, 8, 27),
            central_estimate_c=centre, std_dev_c=1.0, monsoon_phase="unknown",
        )
        got = probability.bucket_probabilities(est, lo, hi, axis=axis)

        assert sum(b.probability for b in got) == pytest.approx(1.0, abs=1e-3)
        mode = max(got, key=lambda b: b.probability)
        m_lo, m_hi = axis.interval_c(mode.bucket_c)
        assert m_lo <= centre < m_hi, (
            f"{icao}: mode bucket {mode.bucket_c} = [{m_lo}, {m_hi}) "
            f"does not contain the central estimate {centre}"
        )


class TestPhaseOneChangedNothing:
    """
    The byte-for-byte constraint, asserted directly. Every existing station
    is on the default axis, and on the default axis the new code path must
    reproduce the old formulas exactly.
    """

    def test_every_registered_station_is_still_on_the_default_axis(self):
        import config

        for icao, st in config.STATIONS.items():
            assert bucket_axis.for_station(st).is_default, icao

    def test_the_default_axis_reproduces_the_historical_interval_formulas(self):
        for b in range(-30, 56):
            assert AXIS_C1.interval_c(b) == (b - 0.5, b + 0.5)
            assert BucketAxis(edge_mode="floor").interval_c(b) == (
                float(b), float(b + 1)
            )

    def test_the_default_axis_reproduces_the_historical_rounding(self):
        t = -20.0
        while t <= 60.0:
            assert AXIS_C1.key_for_temp_c(t, -100, 100) == math.floor(t + 0.5), t
            assert BucketAxis(edge_mode="floor").key_for_temp_c(
                t, -100, 100
            ) == math.floor(t), t
            t = round(t + 0.1, 1)


class TestNothingRendersAHardcodedDegreeSuffix:
    """
    A key rendered with a hardcoded suffix is how a human ends up told to
    buy the wrong contract: "KLGA 78°C (YES)" for a bucket the market
    prints as "78-79°F".
    """

    PRODUCTION_FILES = [
        "executor.py", "ev_engine.py", "pipeline.py",
        "../deploy/generate_dashboard.py",
        "../deploy/generate_realmoney_dashboard.py",
    ]

    def test_no_bucket_value_is_formatted_with_a_literal_degree_c(self):
        import pathlib
        import re

        here = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        # An f-string interpolation of anything bucket-shaped immediately
        # followed by a literal degree suffix.
        pat = re.compile(r"\{[^{}]*bucket[^{}]*\}\s*(°C|&deg;C)", re.IGNORECASE)
        for rel in self.PRODUCTION_FILES:
            path = (here / rel).resolve()
            if not path.exists():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        assert not offenders, (
            "these render a bucket key with a hardcoded unit; use "
            "bucket_axis.for_station(station).label(key, lo, hi):\n  "
            + "\n  ".join(offenders)
        )
