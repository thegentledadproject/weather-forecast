"""
tests/test_region_isolation.py

Regression tests for the 2026-08-24 Europe market isolation framework
(docs/superpowers/specs/2026-08-24-europe-market-isolation-design.md).

Three separate isolation mechanisms are covered here, and they are NOT
the same mechanism -- conflating them is how a gap gets left open:

  1. StationConfig.region + iana_timezone -- the keys everything else
     is looked up by.
  2. Region-scoped SIMULATION/PAPER budget (Kelly bankroll, portfolio
     daily exposure).
  3. Region-scoped LIVE blast radius (concurrent positions, total live
     exposure, daily order rate). Live orders never pass through Kelly
     sizing at all (config.LIVE_TRADE_SIZE_USD replaces it), so (2)
     does nothing whatsoever for (3).

Plus the DST-aware offset that makes any of it correct for a region
whose clock moves twice a year.
"""

import pytest

import config
import scheduler
from models import StationConfig


def _station(**overrides) -> StationConfig:
    """A minimal valid StationConfig; override only what a test is about."""
    base = dict(
        icao="TEST",
        display_name="Test Station",
        country="Testland",
        lat=0.0,
        lon=0.0,
        wunderground_slug="tl/test/TEST",
        long_term_normal_max_c=30.0,
        official_client_key="wwis",
    )
    base.update(overrides)
    return StationConfig(**base)


class TestStationConfigRegionFields:
    def test_region_defaults_to_asia(self):
        assert _station().region == "asia"

    def test_iana_timezone_defaults_to_none(self):
        assert _station().iana_timezone is None

    def test_region_and_timezone_are_settable(self):
        st = _station(region="europe", iana_timezone="Europe/London")
        assert st.region == "europe"
        assert st.iana_timezone == "Europe/London"

    def test_every_registered_station_today_is_asia(self):
        """
        The default is load-bearing: it is what keeps all 13 existing
        entries in one pool with zero edits to them.
        """
        for icao, st in config.STATIONS.items():
            if st.iana_timezone is None:
                assert st.region == "asia", f"{icao}: non-Asia station must set iana_timezone"


from datetime import date, datetime, timezone


class TestCurrentUtcOffsetHours:
    def test_station_without_iana_timezone_returns_the_static_int(self):
        st = _station(utc_offset_hours=9)
        assert config.current_utc_offset_hours(st) == 9

    def test_none_returns_the_legacy_default(self):
        assert config.current_utc_offset_hours(None) == config.LOCAL_UTC_OFFSET_HOURS

    def test_icao_string_is_accepted(self):
        # WSSS is UTC+8 and sets no iana_timezone.
        assert config.current_utc_offset_hours("WSSS") == 8

    def test_every_existing_station_is_unchanged_by_the_new_helper(self):
        """
        The helper must be a strict superset of the old field read. If this
        ever fails, an Asia station's trading day just moved.
        """
        for icao, st in config.STATIONS.items():
            if st.iana_timezone is None:
                assert config.current_utc_offset_hours(st) == st.utc_offset_hours, icao

    def test_london_is_plus_one_in_summer_and_zero_in_winter(self):
        """
        The whole reason this design exists. Europe/London is BST (+1) in
        August and GMT (+0) in December; a static int is wrong for one of
        them no matter which value is chosen.
        """
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)

        summer = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)

        assert config.current_utc_offset_hours(st, at=summer) == 1
        assert config.current_utc_offset_hours(st, at=winter) == 0

    def test_warsaw_is_plus_two_in_summer_and_plus_one_in_winter(self):
        st = _station(region="europe", iana_timezone="Europe/Warsaw", utc_offset_hours=1)

        summer = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)

        assert config.current_utc_offset_hours(st, at=summer) == 2
        assert config.current_utc_offset_hours(st, at=winter) == 1

    def test_an_unknown_timezone_name_fails_loudly(self):
        """
        A typo'd tz name must not silently fall back to the static int --
        that would trade a DST-observing station on a wrong clock while
        looking fine.
        """
        st = _station(region="europe", iana_timezone="Europe/Nowhere", utc_offset_hours=1)
        # ZoneInfoNotFoundError subclasses KeyError -- assert the specific
        # type, so this cannot pass because of some unrelated failure.
        with pytest.raises(KeyError):
            config.current_utc_offset_hours(st)


class TestLocalTodayUsesTheHelper:
    def test_local_today_respects_a_dst_offset(self, monkeypatch):
        """
        local_today() must route through current_utc_offset_hours(), not
        read the field. At 23:30 UTC on 2026-08-24, a BST (+1) station is
        already on 2026-08-25.
        """
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        frozen = datetime(2026, 8, 24, 23, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(config, "_now_utc", lambda: frozen)

        assert config.local_today("TEST") == date(2026, 8, 25)

    def test_local_day_bounds_use_the_offset_of_the_day_being_bounded(self, monkeypatch):
        """
        The BST local day for 2026-08-24 starts at 23:00Z on the 23rd.

        _now_utc is frozen to DECEMBER on purpose while target_date is in
        AUGUST. The two are in opposite DST states, so this test can only
        pass if the offset is resolved from target_date. Resolving it from
        the wall clock -- what a bare current_utc_offset_hours(station) call
        does -- returns GMT here and moves both bounds by an hour.

        Getting this wrong is the lookahead bug local_day_bounds_utc's own
        docstring was written about, one region over. Note also that a test
        which did NOT freeze the clock would pass or fail depending on which
        month it happened to run in.
        """
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "TEST", st)
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc))

        start, end = config.local_day_bounds_utc("TEST", date(2026, 8, 24))

        assert start == datetime(2026, 8, 23, 23, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc)

    def test_local_day_bounds_in_the_other_direction_too(self, monkeypatch):
        """The mirror: a GMT target_date bounded while the wall clock says August."""
        st = _station(region="europe", iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "TEST", st)
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))

        start, end = config.local_day_bounds_utc("TEST", date(2026, 12, 24))

        assert start == datetime(2026, 12, 24, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 12, 25, 0, 0, tzinfo=timezone.utc)


class TestSchedulerGroupsOnResolvedOffset:
    def test_a_dst_station_groups_by_its_current_offset(self, monkeypatch):
        """
        In August a Europe/London station belongs in the +1 group, not the
        +0 group its static utc_offset_hours names.
        """
        st = _station(icao="TEST", region="europe",
                      iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setattr(config, "STATIONS", {"TEST": st})
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))

        groups = scheduler.stations_by_utc_offset()

        assert groups == {1: ["TEST"]}

    def test_the_same_station_groups_at_zero_in_winter(self, monkeypatch):
        st = _station(icao="TEST", region="europe",
                      iana_timezone="Europe/London", utc_offset_hours=0)
        monkeypatch.setattr(config, "STATIONS", {"TEST": st})
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc))

        groups = scheduler.stations_by_utc_offset()

        assert groups == {0: ["TEST"]}

    def test_asia_stations_group_exactly_as_before(self):
        """The 13 registered stations set no iana_timezone, so nothing moves."""
        groups = scheduler.stations_by_utc_offset()
        for offset, icaos in groups.items():
            for icao in icaos:
                st = config.STATIONS[icao]
                if st.iana_timezone is None:
                    assert offset == st.utc_offset_hours, icao

    def test_a_registered_station_with_a_bad_timezone_is_skipped_loudly(
        self, monkeypatch, capsys
    ):
        """
        A typo'd iana_timezone must not be reported as an "unknown station".
        The station IS registered; it simply cannot be scheduled, and the
        log has to say so or it silently stops trading with a message that
        sends the operator looking in the wrong place.
        """
        bad = _station(icao="BADTZ", region="europe", iana_timezone="Europe/Nowhere")
        good = _station(icao="OKTZ", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {"BADTZ": bad, "OKTZ": good})
        monkeypatch.setattr(config, "_now_utc",
                            lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))

        groups = scheduler.stations_by_utc_offset()

        # The healthy station still trades -- one bad config must not stop it.
        assert groups == {1: ["OKTZ"]}

        out = capsys.readouterr().out
        assert "BADTZ" in out
        assert "REGISTERED" in out
        assert "iana_timezone" in out
        assert "unknown station" not in out


import entry_manager


class TestRegionScopedCapital:
    def test_asia_values_equal_the_pre_existing_flat_constants(self):
        """
        The region dicts must REFERENCE the old constants, not restate
        them. If someone retunes BANKROLL_USD and Asia's pool does not
        move, this catches it.
        """
        assert config.REGION_BANKROLL_USD["asia"] == config.BANKROLL_USD
        assert (config.REGION_MAX_DAILY_EXPOSURE_USD["asia"]
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_europe_starts_at_zero(self):
        assert config.REGION_BANKROLL_USD["europe"] == 0.0
        assert config.REGION_MAX_DAILY_EXPOSURE_USD["europe"] == 0.0

    def test_region_lookup_helpers_resolve_through_the_station(self, monkeypatch):
        st = _station(icao="TEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        assert config.region_of("TEST") == "europe"
        assert config.region_bankroll_usd("TEST") == 0.0
        assert config.region_max_daily_exposure_usd("TEST") == 0.0

    def test_an_asia_station_reads_the_asia_pool(self):
        assert config.region_bankroll_usd("WSSS") == config.BANKROLL_USD
        assert (config.region_max_daily_exposure_usd("WSSS")
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_an_unknown_region_fails_loudly(self, monkeypatch):
        """
        A station naming a region with no funding entry must raise, not
        default to Asia's money.
        """
        st = _station(icao="TEST", region="atlantis")
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        with pytest.raises(KeyError):
            config.region_bankroll_usd("TEST")


class TestRegionScopedPortfolioExposure:
    def test_exposure_sums_only_the_named_region(self, monkeypatch):
        """
        An Asia station's spend must not consume Europe's remaining budget.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def fake_station_exposure(icao, target_date, is_paper=None):
            return 100.0 if icao == "WSSS" else 0.0

        monkeypatch.setattr(entry_manager, "station_day_exposure_usd", fake_station_exposure)

        assert entry_manager.portfolio_day_exposure_usd(region="europe") == 0.0
        assert entry_manager.portfolio_day_exposure_usd(region="asia") == 100.0

    def test_no_region_still_sums_everything(self, monkeypatch):
        """Back-compat: the parameterless call keeps its old meaning."""
        def fake_station_exposure(icao, target_date, is_paper=None):
            return 1.0

        monkeypatch.setattr(entry_manager, "station_day_exposure_usd", fake_station_exposure)

        assert entry_manager.portfolio_day_exposure_usd() == float(len(config.STATIONS))

    def test_an_unreadable_station_still_fails_closed(self, monkeypatch):
        """The fail-closed rule must survive the region filter."""
        monkeypatch.setattr(entry_manager, "station_day_exposure_usd",
                            lambda icao, target_date, is_paper=None: None)

        assert entry_manager.portfolio_day_exposure_usd(region="asia") is None
