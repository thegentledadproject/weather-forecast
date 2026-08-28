"""
tests/test_americas_region.py

The americas region draws on its own capital and its own real-money blast
radius. Mirrors tests/test_region_isolation.py, which does the same job for
europe -- see docs/superpowers/specs/2026-08-27-americas-market-isolation-design.md.
"""
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
    def test_the_window_uses_the_live_offset_not_the_static_field(self, monkeypatch):
        """
        EGLC is utc_offset_hours=0 (GMT, the STANDARD-time field) but
        Europe/London in August is BST, +1. A day window built on 0 is
        shifted an hour and mis-attributes the last hour of the previous
        local day.
        """
        import storage
        from clients import metar_client

        st = config.get_station("EGLC")
        assert st.utc_offset_hours == 0
        assert st.iana_timezone == "Europe/London"

        monkeypatch.setattr(metar_client, "_last_ingest_by_station", {})
        monkeypatch.setattr(storage, "load_observations_since", lambda icao, cutoff: [])
        monkeypatch.setattr(storage, "save_observation", lambda o: None)

        seen = []
        monkeypatch.setattr(
            metar_client, "_local_day_window_utc",
            lambda day, offset: (seen.append(offset) or (0, 0)),
        )
        monkeypatch.setattr(metar_client, "fetch_metars", lambda icao, hours, timeout=15: [])
        metar_client.ingest_missing_recent(["EGLC"], days_back=1)

        assert seen, "the day window was never built"
        assert all(
            o == config.current_utc_offset_hours("EGLC") for o in seen
        ), f"day window built on {seen}, expected the live DST-aware offset"
