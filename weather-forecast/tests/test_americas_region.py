"""
tests/test_americas_region.py

The americas region draws on its own capital and its own real-money blast
radius. Mirrors tests/test_region_isolation.py, which does the same job for
europe -- see docs/superpowers/specs/2026-08-27-americas-market-isolation-design.md.
"""
from datetime import date, datetime, timezone

import pytest

import config


class TestAmericasHasEveryPool:
    def test_it_is_present_in_all_five_region_dicts(self):
        for name in (
            "REGION_BANKROLL_USD",
            "REGION_MAX_DAILY_EXPOSURE_USD",
            "REGION_LIVE_MAX_CONCURRENT_POSITIONS",
            "REGION_LIVE_MAX_TOTAL_EXPOSURE_USD",
            "REGION_LIVE_MAX_ORDERS_PER_DAY",
        ):
            assert "americas" in getattr(config, name), name

    def test_its_paper_pools_are_funded_like_europes(self):
        assert config.REGION_BANKROLL_USD["americas"] == config.BANKROLL_USD
        assert (config.REGION_MAX_DAILY_EXPOSURE_USD["americas"]
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_its_live_blast_radius_is_locked_at_zero(self):
        assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["americas"] == 0
        assert config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["americas"] == 0.0
        assert config.REGION_LIVE_MAX_ORDERS_PER_DAY["americas"] == 0

    def test_it_authorises_no_live_orders(self):
        assert not config.region_authorises_live_orders("americas")

    def test_asia_and_europe_are_untouched(self):
        assert config.REGION_BANKROLL_USD["asia"] == config.BANKROLL_USD
        assert config.REGION_BANKROLL_USD["europe"] == config.BANKROLL_USD
        assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"] == 0


class TestSpreadCeilingIsPerRegion:
    """
    config.py's own comment: "a too-NARROW spread is the dangerous
    direction: it makes the model look certain, which inflates the gap
    between model probability and market price, which is an edge the entry
    gates will happily size into." A 2.0C ceiling tuned on tropical
    stations would clamp continental and winter spreads in exactly that
    direction.
    """

    def test_asia_and_europe_keep_two_point_zero_verbatim(self):
        assert config.REGION_SPREAD_CEILING_C["asia"] == 2.0
        assert config.REGION_SPREAD_CEILING_C["europe"] == 2.0

    def test_americas_is_none_meaning_no_clamp_not_a_guessed_number(self):
        assert config.REGION_SPREAD_CEILING_C["americas"] is None

    def test_a_wide_spread_is_clamped_for_asia(self):
        import calibration

        assert calibration._clamp_spread(5.0, "WSSS") == 2.0

    def test_a_wide_spread_is_left_alone_for_americas(self, monkeypatch):
        import calibration
        from models import StationConfig

        st = StationConfig(
            icao="KORD", display_name="O'Hare", country="United States",
            lat=41.98, lon=-87.90, wunderground_slug="us/chicago/KORD",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="chicago", region="americas",
        )
        monkeypatch.setitem(config.STATIONS, "KORD", st)
        assert calibration._clamp_spread(5.0, "KORD") == 5.0

    def test_the_floor_still_applies_everywhere(self, monkeypatch):
        import calibration
        from models import StationConfig

        st = StationConfig(
            icao="KORD", display_name="O'Hare", country="United States",
            lat=41.98, lon=-87.90, wunderground_slug="us/chicago/KORD",
            long_term_normal_max_c=28.0, official_client_key="wwis",
            polymarket_city_slug="chicago", region="americas",
        )
        monkeypatch.setitem(config.STATIONS, "KORD", st)
        assert calibration._clamp_spread(0.1, "KORD") == config.SPREAD_FLOOR_C

    def test_an_unknown_station_keeps_the_legacy_global_ceiling(self):
        import calibration

        assert calibration._clamp_spread(5.0) == 2.0


class TestMetarDayWindowFollowsDst:
    """
    EGLC is utc_offset_hours=0 (GMT, the STANDARD-time field) but
    Europe/London observes BST, +1, roughly late March to late October. A
    day window built on the static field is shifted an hour and
    mis-attributes the last hour of the previous local day for most of the
    year.

    Every case here freezes config._now_utc() to a fixed instant and
    asserts a LITERAL expected offset -- not one re-derived from
    config.current_utc_offset_hours(), which would make the test pass or
    fail in lockstep with the code under test rather than against a known
    answer. A live "now" also self-passes roughly five months a year (GMT
    season, when the static field and the live offset agree), which is
    exactly when a regression would go undetected.
    """

    def _wire_ingest(self, monkeypatch):
        """Common stubs so ingest_missing_recent never touches the real DB
        or network, and isn't skipped by the per-day throttle cache."""
        import storage
        from clients import metar_client

        monkeypatch.setattr(metar_client, "_last_ingest_by_station", {})
        monkeypatch.setattr(storage, "load_observations_since", lambda icao, cutoff: [])
        monkeypatch.setattr(storage, "save_observation", lambda o: None)
        monkeypatch.setattr(metar_client, "fetch_metars", lambda icao, hours, timeout=15: [])
        return metar_client

    def _freeze_now(self, monkeypatch, iso_utc):
        instant = datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc)
        monkeypatch.setattr(config, "_now_utc", lambda: instant)

    def test_summer_instant_resolves_bst_not_static_gmt(self, monkeypatch):
        metar_client = self._wire_ingest(monkeypatch)
        self._freeze_now(monkeypatch, "2026-08-15T12:00:00")

        seen = []
        monkeypatch.setattr(
            metar_client, "_local_day_window_utc",
            lambda day, offset: (seen.append(offset) or (0, 0)),
        )
        metar_client.ingest_missing_recent(["EGLC"], days_back=1)

        assert seen == [1], f"expected BST (+1) for every day, got {seen}"

    def test_winter_instant_resolves_gmt(self, monkeypatch):
        metar_client = self._wire_ingest(monkeypatch)
        self._freeze_now(monkeypatch, "2026-01-15T12:00:00")

        seen = []
        monkeypatch.setattr(
            metar_client, "_local_day_window_utc",
            lambda day, offset: (seen.append(offset) or (0, 0)),
        )
        metar_client.ingest_missing_recent(["EGLC"], days_back=1)

        assert seen == [0], f"expected GMT (0) for every day, got {seen}"

    def test_offset_is_resolved_per_day_across_a_dst_transition(self, monkeypatch):
        """
        UK clocks go back (BST -> GMT) at 2026-10-25T01:00:00Z. Freeze
        "now" a few days after that instant so days_back=3 pulls a
        `missing` set that straddles the transition: one day still BST,
        two days already GMT. A single offset resolved once (at "now", or
        at any one instant) for the whole sweep gets at least one of these
        three days wrong -- which is the failure mode this test is for,
        distinct from the static-field defect the class above covers.
        """
        metar_client = self._wire_ingest(monkeypatch)
        self._freeze_now(monkeypatch, "2026-10-28T10:00:00")

        seen = {}
        monkeypatch.setattr(
            metar_client, "_local_day_window_utc",
            lambda day, offset: (seen.__setitem__(day, offset) or (0, 0)),
        )
        metar_client.ingest_missing_recent(["EGLC"], days_back=3)

        assert seen == {
            date(2026, 10, 27): 0,  # GMT: after the transition
            date(2026, 10, 26): 0,  # GMT: after the transition
            date(2026, 10, 25): 1,  # BST: transition instant is 01:00Z,
                                     # AFTER this day's UTC-midnight anchor
        }, f"per-day offsets were {seen}, expected each day resolved on its own side of the transition"


AMERICAS_CELSIUS = ("CYYZ", "MMMX", "SBGR", "SAEZ")


class TestTheFourCelsiusCities:
    def test_they_are_registered_in_the_americas_region(self):
        for icao in AMERICAS_CELSIUS:
            assert icao in config.STATIONS, icao
            assert config.STATIONS[icao].region == "americas", icao

    def test_they_are_on_the_default_axis(self):
        import bucket_axis

        for icao in AMERICAS_CELSIUS:
            assert bucket_axis.for_station(config.STATIONS[icao]).is_default, icao

    def test_none_of_them_may_trade_real_money(self):
        for icao in AMERICAS_CELSIUS:
            assert icao not in config.LIVE_TRADING_STATIONS, icao
            assert not config.live_mode_is_permitted(icao, "live"), icao

    def test_only_toronto_observes_dst(self):
        assert config.STATIONS["CYYZ"].iana_timezone == "America/Toronto"
        for icao in ("MMMX", "SBGR", "SAEZ"):
            assert config.STATIONS[icao].iana_timezone is None, icao

    def test_they_land_in_their_own_scheduler_groups(self):
        import scheduler

        groups = scheduler.stations_by_utc_offset()
        asia_europe = {
            icao for icao, st in config.STATIONS.items()
            if st.region in ("asia", "europe")
        }
        for offset, icaos in groups.items():
            if any(i in AMERICAS_CELSIUS for i in icaos):
                assert not (set(icaos) & asia_europe), (
                    f"offset {offset} mixes an Americas station with "
                    f"another region's"
                )
