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

import datetime

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

    def test_europe_paper_pools_are_funded(self):
        """
        Europe's PAPER pools are funded, deliberately. They shipped at 0.0
        and were raised the same day: paper sizes off Kelly too, so a zero
        pool blocked paper positions as well as real ones, and paper
        positions are the evidence a promotion case is built from.

        What stays at zero is the real-money blast radius --
        REGION_LIVE_MAX_*, a separate mechanism, asserted in
        TestRegionScopedLiveBlastRadius::test_europe_is_locked_at_zero.
        """
        assert config.REGION_BANKROLL_USD["europe"] == config.BANKROLL_USD
        assert (config.REGION_MAX_DAILY_EXPOSURE_USD["europe"]
                == config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD)

    def test_the_two_pools_are_separate_even_though_equal(self):
        """
        Equal values are not one pool. entry_manager sums exposure per
        region, so an Asian drawdown cannot consume Europe's budget -- the
        equality here is a sizing choice, not a shared pot.
        """
        assert config.REGION_BANKROLL_USD is not config.REGION_MAX_DAILY_EXPOSURE_USD
        assert set(config.REGION_BANKROLL_USD) == {"asia", "europe"}

    def test_region_lookup_helpers_resolve_through_the_station(self, monkeypatch):
        st = _station(icao="TEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        assert config.region_of("TEST") == "europe"
        assert config.region_bankroll_usd("TEST") == config.REGION_BANKROLL_USD["europe"]
        assert (config.region_max_daily_exposure_usd("TEST")
                == config.REGION_MAX_DAILY_EXPOSURE_USD["europe"])

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

    def test_an_unknown_region_fails_loudly_for_the_exposure_cap_too(self, monkeypatch):
        """
        Same fail-closed rule as region_bankroll_usd, for the OTHER
        helper. Not exercising this one separately would leave a typo'd
        region free to fall back to Asia's exposure cap even after this
        exact failure mode was closed for the bankroll helper.
        """
        st = _station(icao="TEST", region="atlantis")
        monkeypatch.setitem(config.STATIONS, "TEST", st)

        with pytest.raises(KeyError):
            config.region_max_daily_exposure_usd("TEST")


class TestRegionScopedPortfolioExposure:
    def test_exposure_sums_only_the_named_region(self, monkeypatch):
        """
        Both directions must hold: an Asia station's spend must not
        consume Europe's remaining budget, AND a Europe station's spend
        must not inflate Asia's sum. Giving EUTEST a zero faked exposure
        (the original version of this test) only proves the first
        direction -- a filter that let European stations leak into the
        Asia sum would still produce exactly 100.0 and this test would
        not have noticed. EUTEST is given a nonzero exposure so leakage
        in either direction changes an assertion.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def fake_station_exposure(icao, target_date, is_paper=None):
            if icao == "WSSS":
                return 100.0
            if icao == "EUTEST":
                return 50.0
            return 0.0

        monkeypatch.setattr(entry_manager, "station_day_exposure_usd", fake_station_exposure)

        assert entry_manager.portfolio_day_exposure_usd(region="europe") == 50.0
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


import storage
from datetime import date as _date
from models import EntryDecision, EVResult


def _eu_ev(station="EUTEST", bucket=32, side="YES") -> EVResult:
    return EVResult(
        station_icao=station, target_date=_date(2026, 8, 6), bucket_c=bucket, side=side,
        model_prob=0.55, market_price=0.35, raw_edge=0.20,
        estimated_slippage_pct=0.01, fee_rate_pct=0.02,
        net_ev_per_dollar=0.30, spread_source="ensemble",
    )


def _canned_decision(station="EUTEST", size=50.0) -> EntryDecision:
    """
    A pre-sized, pre-approved decision, standing in for whatever
    decide_entries() would have produced. Used to bypass Kelly sizing
    entirely -- a Europe-region station already sizes to $0 there (region
    bankroll is 0.0), which would zero recommended_size_usd for a reason
    that has nothing to do with the portfolio budget call site this test
    is about.
    """
    return EntryDecision(
        station_icao=station,
        target_date=_date(2026, 8, 6),
        bucket_c=32,
        side="YES",
        kelly_fraction_raw=0.1,
        kelly_fraction_applied=0.025,
        recommended_size_usd=size,
        available_depth_usd=1000.0,
        slippage_at_size_pct=0.01,
        net_ev_at_size=0.2,
        approved=True,
        reason="approved",
        station_maturity="mature",
        entry_price=0.30,
        token_id="tok",
    )


class TestPortfolioCapArgumentIsActuallyApplied:
    """
    entry_manager.decide_portfolio_entries() passes
    max_portfolio_usd=config.region_max_daily_exposure_usd(station_icao)
    into apply_portfolio_budget(). THAT argument is the capital isolation
    -- without it, apply_portfolio_budget falls back to its own default,
    config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD (400.0).

    No test above catches that argument being dropped: Asia's region cap
    IS that same 400.0 default, so an Asia-scoped test cannot tell the
    two apart. Only a region whose cap differs from the global default --
    Europe's 0.0 -- distinguishes "the region's own cap was applied" from
    "the global default was applied instead." This test drives the real
    call site (decide_portfolio_entries) with a Europe station and a
    nonzero, pre-approved candidate, and asserts the candidate is
    rejected for exhausting the *portfolio/day* budget -- the observable
    consequence of a $0.00 cap actually binding, not $400.
    """

    def test_a_europe_candidate_is_rejected_by_its_own_zero_cap_not_the_global_default(
        self, monkeypatch
    ):
        _pin_zero_region(monkeypatch)
        eu = _station(icao="EUTEST", region=ZERO_REGION, iana_timezone="Europe/London")
        monkeypatch.setitem(config.STATIONS, "EUTEST", eu)

        # Nothing deployed yet today, for the one station this cycle
        # actually looks at (EUTEST) -- the loop skips every other
        # station once scoped to this region.
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
        monkeypatch.setattr(storage, "load_position_history", lambda *a, **kw: [])

        # Graduate EUTEST past the collection-first gate so this test
        # reaches the budget stage at all -- same stubs
        # test_portfolio_caps.py uses to graduate a station.
        monkeypatch.setattr(storage, "count_observations_from_source",
                            lambda icao, source: config.MIN_RESOLUTION_OBS_BEFORE_ENTRY)
        monkeypatch.setattr(storage, "forecast_error_samples_dated",
                            lambda icao, source: [(date(2026, 8, 20), 0.2)] * config.MIN_BIAS_PAIRS_BEFORE_ENTRY)

        # Bypass decide_entries()/Kelly sizing: this region's $0 bankroll
        # would already zero recommended_size_usd on its own, which would
        # make this test pass for the wrong reason. Feed a
        # canned, already-approved, nonzero-sized decision straight into
        # the budget stage instead, exactly as decide_entries() would
        # have handed it to decide_portfolio_entries().
        canned = _canned_decision(station="EUTEST", size=50.0)
        monkeypatch.setattr(
            entry_manager, "decide_entries",
            lambda ev_results, token_map, min_net_ev=0.15: [canned],
        )

        ev_results = [_eu_ev(station="EUTEST")]
        token_map = {32: {"yes_token_id": "y32", "no_token_id": "n32"}}

        decisions = entry_manager.decide_portfolio_entries(ev_results, token_map, min_net_ev=0.15)

        assert len(decisions) == 1
        assert decisions[0].approved is False
        assert decisions[0].recommended_size_usd == 0.0
        assert "portfolio/day" in decisions[0].reason
        assert "budget exhausted" in decisions[0].reason



# --- synthetic zero-funded region -----------------------------------------
# Three regression guards below prove that a region-scoped cap is actually
# THREADED THROUGH to its consumer, and each works by showing the consumer
# used a value that differs from the global default. They originally leaned
# on Europe being funded at 0.0 while Asia was 1000.0.
#
# Europe is now funded at the SAME value as Asia, which would make all three
# vacuous -- region-aware and region-blind become indistinguishable, which is
# precisely the condition that let the backtest region-blindness bug survive
# (tests/test_parity_entry.py runs on WSSS, where the two agree).
#
# So they pin a synthetic region at zero instead. The guards no longer depend
# on any real region's funding level, and cannot silently rot if one changes.
ZERO_REGION = "testzero"


def _pin_zero_region(monkeypatch):
    """Register a zero-funded synthetic region across all five pools."""
    monkeypatch.setitem(config.REGION_BANKROLL_USD, ZERO_REGION, 0.0)
    monkeypatch.setitem(config.REGION_MAX_DAILY_EXPOSURE_USD, ZERO_REGION, 0.0)
    monkeypatch.setitem(config.REGION_LIVE_MAX_CONCURRENT_POSITIONS, ZERO_REGION, 0)
    monkeypatch.setitem(config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD, ZERO_REGION, 0.0)
    monkeypatch.setitem(config.REGION_LIVE_MAX_ORDERS_PER_DAY, ZERO_REGION, 0)


import executor
import storage
from models import Position


class TestRegionScopedLiveBlastRadius:
    def test_asia_values_equal_the_pre_existing_flat_constants(self):
        assert (config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["asia"]
                == config.LIVE_MAX_CONCURRENT_POSITIONS)
        assert (config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["asia"]
                == config.LIVE_MAX_TOTAL_EXPOSURE_USD)
        assert (config.REGION_LIVE_MAX_ORDERS_PER_DAY["asia"]
                == config.LIVE_MAX_ORDERS_PER_DAY)

    def test_europe_is_locked_at_zero(self):
        assert config.REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"] == 0
        assert config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["europe"] == 0.0
        assert config.REGION_LIVE_MAX_ORDERS_PER_DAY["europe"] == 0

    def test_a_europe_station_is_refused_even_on_an_empty_live_book(
        self, monkeypatch
    ):
        """
        THE POINT OF THIS TASK. Zero open positions, zero orders today, and
        the entry is still refused -- because the region's concurrent cap
        is 0. Kelly-side isolation (Task 4) does nothing here: live orders
        never pass through Kelly sizing at all.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts",
                            lambda kind, since, station_icaos=None: 0)
        monkeypatch.setattr(
            executor.wallet_client, "reconcile_cached",
            lambda positions, **_: executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed"),
        )

        breach = executor._live_budget_breach(1.00, "EUTEST")

        assert breach is not None
        assert "europe" in breach

    def test_asia_positions_do_not_consume_a_europe_budget(self, monkeypatch):
        """
        Once Europe IS funded, the two regions count independently. Three
        open Asia positions fill Asia's cap of 3 and leave Europe's own
        budget untouched.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})
        monkeypatch.setitem(config.REGION_LIVE_MAX_CONCURRENT_POSITIONS, "europe", 3)
        monkeypatch.setitem(config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD, "europe", 8.00)
        monkeypatch.setitem(config.REGION_LIVE_MAX_ORDERS_PER_DAY, "europe", 10)

        asia_positions = [
            Position(
                position_id=f"p{i}", station_icao="WSSS", target_date=date(2026, 8, 24),
                bucket_c=32, side="YES", entry_price=0.30, size_usd=3.75,
                entry_time="2026-08-24T00:00:00+00:00", status="open",
                token_id=f"TOK{i}", is_paper=False, size_shares=5.0,
                execution_mode="live",
            )
            for i in range(3)
        ]
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: asia_positions)
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts",
                            lambda kind, since, station_icaos=None: 0)
        monkeypatch.setattr(
            executor.wallet_client, "reconcile_cached",
            lambda positions, **_: executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed"),
        )

        # Asia is full at 3 concurrent...
        assert executor._live_budget_breach(1.00, "WSSS") is not None
        # ...and Europe, now funded, is unaffected by them.
        assert executor._live_budget_breach(1.00, "EUTEST") is None

    def test_the_order_rate_limit_counts_only_the_region(self, monkeypatch):
        """
        The daily order cap is counted from an audit table keyed by
        station_icao. It must be filtered to the region too, or Asia's ten
        orders would exhaust Europe's separate allowance.
        """
        seen = {}

        def fake_count(kind, since, station_icaos=None):
            seen["station_icaos"] = station_icaos
            return 0

        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts", fake_count)
        monkeypatch.setattr(
            executor.wallet_client, "reconcile_cached",
            lambda positions, **_: executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed"),
        )

        executor._live_budget_breach(1.00, "WSSS")

        assert seen["station_icaos"] is not None
        assert "WSSS" in seen["station_icaos"]
        assert all(config.region_of(i) == "asia" for i in seen["station_icaos"])

    def test_reconciliation_sees_the_WHOLE_live_book_not_just_this_region(
        self, monkeypatch
    ):
        """
        THE ORDERING CONSTRAINT, PINNED.

        reconcile_cached() compares the database's ENTIRE live book against
        the exchange's actual holdings. The region filter must therefore run
        AFTER it: filtering first would hand reconciliation one region's
        positions, so every OTHER region's real holdings would read as
        unrecorded exposure and every entry would be refused.

        This is inert while Europe is locked at 0 -- no live European
        position can exist, so the whole book and the Asia book are the same
        set. It stops being inert the day a second region is funded, and a
        refactor that moves the filter one block up would pass every other
        test in the suite. Hence a test that asserts on what reconciliation
        was actually HANDED, rather than on the breach result.
        """
        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def _pos(position_id, station_icao):
            return Position(
                position_id=position_id, station_icao=station_icao,
                target_date=date(2026, 8, 24), bucket_c=32, side="YES",
                entry_price=0.30, size_usd=1.00,
                entry_time="2026-08-24T00:00:00+00:00", status="open",
                token_id=f"TOK-{position_id}", is_paper=False,
                size_shares=5.0, execution_mode="live",
            )

        book = [_pos("asia1", "WSSS"), _pos("eu1", "EUTEST")]
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: book)
        monkeypatch.setattr(storage, "load_settled_live_tokens", lambda: {})
        monkeypatch.setattr(storage, "count_live_order_attempts",
                            lambda kind, since, station_icaos=None: 0)

        seen = {}

        def _capturing_reconcile(positions, **_):
            # RECORD what reconciliation was handed -- the whole point.
            seen["positions"] = list(positions)
            return executor.wallet_client.Reconciliation(
                ok=True, checked=True, reason="stubbed")

        monkeypatch.setattr(executor.wallet_client, "reconcile_cached",
                            _capturing_reconcile)

        executor._live_budget_breach(1.00, "WSSS")

        handed = {p.station_icao for p in seen["positions"]}
        assert handed == {"WSSS", "EUTEST"}, (
            f"reconciliation was handed {handed} -- it must see the WHOLE live "
            f"book. If this fails, the region filter has been moved above the "
            f"reconcile_cached() call."
        )


class TestCountLiveOrderAttemptsFilter:
    def test_the_station_filter_narrows_the_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))

        storage.record_live_order_attempt(
            kind="entry", station_icao="WSSS", outcome="filled", notional_usd=1.0)
        storage.record_live_order_attempt(
            kind="entry", station_icao="RCSS", outcome="filled", notional_usd=1.0)

        assert storage.count_live_order_attempts("entry", "2000-01-01") == 2
        assert storage.count_live_order_attempts(
            "entry", "2000-01-01", station_icaos=["WSSS"]) == 1
        assert storage.count_live_order_attempts(
            "entry", "2000-01-01", station_icaos=["WSSS", "RCSS"]) == 2

    def test_an_empty_station_list_counts_nothing(self, tmp_path, monkeypatch):
        """
        A region with no registered stations has no orders, and must read 0
        rather than degrading to the unfiltered total.
        """
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        storage.record_live_order_attempt(
            kind="entry", station_icao="WSSS", outcome="filled", notional_usd=1.0)

        assert storage.count_live_order_attempts(
            "entry", "2000-01-01", station_icaos=[]) == 0


import calibration


class TestPooledSpreadIsRegionScoped:
    def test_the_pool_excludes_other_regions(self, monkeypatch, tmp_path):
        """
        A European station's errors must not enter the pool an Asian
        station falls back on. Spread feeds EV feeds entries, so this is
        the path by which registering Europe could silently move Asian
        trading behavior.
        """
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        calibration._pooled_spread_cache.clear()

        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        # Asia errors are tight; the European station's are wild. If the
        # European rows leak into Asia's pool, Asia's spread inflates.
        def fake_samples(icao, source):
            if icao == "EUTEST":
                return [-8.0, 8.0, -8.0, 8.0, -8.0, 8.0]
            if icao == "WSSS":
                return [-0.5, 0.5, -0.5, 0.5, -0.5, 0.5]
            return []

        monkeypatch.setattr(storage, "forecast_error_samples", fake_samples)

        asia_spread, asia_n = calibration.pooled_error_spread(region="asia")
        calibration._pooled_spread_cache.clear()
        eu_spread, eu_n = calibration.pooled_error_spread(region="europe")

        assert asia_n == 6, "asia pool must contain only WSSS's six samples"
        assert eu_n == 6, "europe pool must contain only EUTEST's six samples"
        assert asia_spread < 1.0, f"asia spread {asia_spread} inflated by European errors"
        assert eu_spread > 5.0, f"europe spread {eu_spread} diluted by Asian errors"

    def test_the_cache_key_separates_regions(self, monkeypatch, tmp_path):
        """
        The cache is keyed on DB_PATH. Without the region in the key, the
        first region to compute would serve its spread to the other -- the
        leak this task exists to close, reintroduced by the cache.
        """
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        calibration._pooled_spread_cache.clear()

        eu = _station(icao="EUTEST", region="europe", iana_timezone="Europe/London")
        monkeypatch.setattr(config, "STATIONS", {**config.STATIONS, "EUTEST": eu})

        def fake_samples(icao, source):
            if icao == "EUTEST":
                return [-8.0, 8.0, -8.0, 8.0, -8.0, 8.0]
            if icao == "WSSS":
                return [-0.5, 0.5, -0.5, 0.5, -0.5, 0.5]
            return []

        monkeypatch.setattr(storage, "forecast_error_samples", fake_samples)

        # No clear() between these two calls -- the cache must not confuse them.
        asia_spread, _ = calibration.pooled_error_spread(region="asia")
        eu_spread, _ = calibration.pooled_error_spread(region="europe")

        assert asia_spread != eu_spread

    def test_no_region_still_pools_everything(self, monkeypatch, tmp_path):
        """Back-compat for callers that predate regions."""
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
        calibration._pooled_spread_cache.clear()

        monkeypatch.setattr(storage, "forecast_error_samples",
                            lambda icao, source: [-1.0, 1.0])

        _, n = calibration.pooled_error_spread()

        assert n == 2 * len(config.STATIONS)


class TestEuropeRegistry:
    def test_every_europe_station_sets_an_iana_timezone(self):
        """
        A DST region without a tz name is the exact bug this framework
        exists to prevent -- it would trade on a static offset that is
        wrong for half the year.
        """
        for icao, st in config.STATIONS.items():
            if st.region == "europe":
                assert st.iana_timezone, f"{icao}: region=europe requires an iana_timezone"

    def test_every_declared_timezone_actually_resolves(self):
        """
        A typo'd iana_timezone is a STATIC config error -- it needs no clock
        to detect, so it should be caught here at collection time and not at
        05:00 in the daemon.

        scheduler.stations_by_utc_offset() skips such a station and logs it
        loudly (Task 3), which keeps the other stations trading but means the
        typo'd one silently does not trade at all. This assertion is what
        stops that state from ever reaching a deployment.
        """
        for icao, st in config.STATIONS.items():
            if not st.iana_timezone:
                continue
            # Raises ZoneInfoNotFoundError if the name is not in the tz db.
            config.current_utc_offset_hours(st)

    def test_every_europe_station_also_keeps_a_static_offset(self):
        """
        backtest/engine.py reads station.utc_offset_hours directly and has
        no notion of a moving clock. The static field must stay set to the
        standard-time value.
        """
        for icao, st in config.STATIONS.items():
            if st.region == "europe":
                assert st.utc_offset_hours in (0, 1), (
                    f"{icao}: expected a European standard-time offset, "
                    f"got {st.utc_offset_hours}"
                )

    def test_no_europe_station_is_allowlisted_for_real_money(self):
        for icao in config.LIVE_TRADING_STATIONS:
            assert config.region_of(icao) == "asia", (
                f"{icao} is a non-Asian station on the real-money allowlist; "
                f"its region is funded at zero and this is a contradiction"
            )

    def test_europe_stations_are_present_and_all_exploratory(self):
        europe = config.stations_in_region("europe")
        assert europe, "no European station registered"
        for icao in europe:
            assert config.MATURITY_SNAPSHOT[icao] == "exploratory", icao

    def test_every_named_region_has_funding_entries(self):
        """
        Every region named by a station must have funding entries in all
        five dicts, or a lookup raises at trade time.
        """
        regions = {st.region for st in config.STATIONS.values()}
        for region in regions:
            assert region in config.REGION_BANKROLL_USD, region
            assert region in config.REGION_MAX_DAILY_EXPOSURE_USD, region
            assert region in config.REGION_LIVE_MAX_CONCURRENT_POSITIONS, region
            assert region in config.REGION_LIVE_MAX_TOTAL_EXPOSURE_USD, region
            assert region in config.REGION_LIVE_MAX_ORDERS_PER_DAY, region


import backtest.engine as engine
from backtest import entry_sim as entry_sim_module
from backtest.portfolio import PortfolioState
from backtest.simclock import SimClock, Tick


class _StopAfterPortfolioConstruction(Exception):
    """Sentinel raised the instant PortfolioState's args are captured, so the
    test never reaches engine.run()'s storage/price_store I/O below it."""


class TestBacktestEngineIsRegionAware:
    """
    Regression coverage for the fix to Finding 2 of the final whole-branch
    review: backtest/engine.py used to size and cap EVERY replay off Asia's
    flat config.BANKROLL_USD / config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD
    constants, regardless of the station's region. A European replay
    therefore ran on Asia's $1,000 Kelly bankroll and $400 daily cap instead
    of the region's $0.00/$0.00, and could report sized entries and a P&L
    number the live path structurally cannot produce -- exactly the number
    someone would read when deciding whether to fund Europe.

    tests/test_parity_entry.py runs on WSSS, where
    config.region_bankroll_usd("WSSS") == config.BANKROLL_USD makes
    region-aware and region-blind code indistinguishable. These tests use a
    region whose numbers differ from the global defaults, which is the only
    way to tell "the region's own value was used" from "the global default
    was used instead."

    Two call sites are covered, matching the two lines named in the review:
      * engine.run() constructing PortfolioState (backtest/engine.py, was
        line 498).
      * engine._entry_pass() calling entry_sim.decide_portfolio_entries_sim()
        (backtest/engine.py, was line 523/1118) -- specifically, that it now
        passes max_portfolio_usd rather than leaving it at the function's
        None default, which silently falls back to Asia's global
        MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD inside
        entry_manager.apply_portfolio_budget().

    Both are asserted at the narrowest honest level -- the value actually
    threaded into the PortfolioState constructor / the
    decide_portfolio_entries_sim() call -- rather than by running a full
    replay to completion. A full replay would need its own seeded
    forecast/observation/market-data scenario (see tests/conftest.py's
    build_scenario(), which is hardcoded to WSSS and to Asia-shaped data)
    and would exercise far more machinery than this fix touches. Both tests
    below short-circuit immediately after the call site of interest, via a
    capturing stand-in that raises a sentinel exception the instant it has
    recorded its arguments -- so neither test needs a database, a market
    data file, or any forecast/observation history at all.
    """

    def test_run_constructs_portfolio_state_with_the_region_bankroll(self, monkeypatch):
        _pin_zero_region(monkeypatch)
        eu = _station(icao="EUTEST", region=ZERO_REGION, iana_timezone="Europe/London")
        monkeypatch.setitem(config.STATIONS, "EUTEST", eu)

        captured = {}

        def _capturing_portfolio_state(bankroll_usd, bankroll_mode="static"):
            captured["bankroll_usd"] = bankroll_usd
            raise _StopAfterPortfolioConstruction

        monkeypatch.setattr(engine, "PortfolioState", _capturing_portfolio_state)

        d = _date(2026, 8, 24)
        with pytest.raises(_StopAfterPortfolioConstruction):
            engine.run(station_icao="EUTEST", start_date=d, end_date=d)

        assert captured["bankroll_usd"] == 0.0
        assert captured["bankroll_usd"] == config.region_bankroll_usd("EUTEST")
        assert captured["bankroll_usd"] != config.BANKROLL_USD, (
            "a replay must size off its own region's bankroll, not Asia's. "
            "This assertion is why the station above uses a synthetic "
            "zero-funded region rather than 'europe': Europe and Asia are "
            "funded identically, so a europe station could not tell "
            "region-aware from region-blind."
        )

    def test_run_constructs_portfolio_state_with_asias_bankroll_for_an_asia_station(
        self, monkeypatch
    ):
        """Back-compat: an Asia station's replay is unchanged by the fix."""
        captured = {}

        def _capturing_portfolio_state(bankroll_usd, bankroll_mode="static"):
            captured["bankroll_usd"] = bankroll_usd
            raise _StopAfterPortfolioConstruction

        monkeypatch.setattr(engine, "PortfolioState", _capturing_portfolio_state)

        d = _date(2026, 8, 24)
        with pytest.raises(_StopAfterPortfolioConstruction):
            engine.run(station_icao="WSSS", start_date=d, end_date=d)

        assert captured["bankroll_usd"] == config.BANKROLL_USD

    def test_entry_pass_caps_a_europe_cycle_at_the_region_daily_exposure(self, monkeypatch):
        """
        Drives the real engine._entry_pass() end to end (no DB, no market
        data -- an empty token_map means the per-candidate loop never runs,
        so decide_portfolio_entries_sim() is reached with zero candidates,
        exactly like an ordinary cycle that screened nothing in). Spies on
        entry_sim.decide_portfolio_entries_sim() to capture the
        max_portfolio_usd it was actually called with.
        """
        _pin_zero_region(monkeypatch)
        eu = _station(icao="EUTEST", region=ZERO_REGION, iana_timezone="Europe/London",
                      utc_offset_hours=0)
        monkeypatch.setitem(config.STATIONS, "EUTEST", eu)

        captured = {}
        real_decide = entry_sim_module.decide_portfolio_entries_sim

        def _capturing_decide(*args, **kwargs):
            captured["max_portfolio_usd"] = kwargs.get("max_portfolio_usd")
            return real_decide(*args, **kwargs)

        monkeypatch.setattr(entry_sim_module, "decide_portfolio_entries_sim", _capturing_decide)

        day = _date(2026, 8, 24)
        clock = SimClock(0, utc_offset_hours=0)
        tick = Tick(ts=clock.ts, mode="primary", min_net_ev=0.15, interval_min=10)
        portfolio = PortfolioState(bankroll_usd=config.region_bankroll_usd("EUTEST"))

        engine._entry_pass(
            station=eu,
            day=day,
            clock=clock,
            tick=tick,
            token_map={},
            portfolio=portfolio,
            fill_model=None,
            prices=None,
            forecast_history={},
            all_observations=[],
            fee_rate_pct=0.02,
            counters={
                "n_candidates_screened": 0,
                "n_decisions": 0,
                "n_entries": 0,
                "n_entries_missing_token": 0,
                "n_ev_rows_no_price": 0,
            },
            rejections={},
            entry_records={},
            decisions_log=[],
            last_observed={},
        )

        assert "max_portfolio_usd" in captured, "decide_portfolio_entries_sim was never called"
        assert captured["max_portfolio_usd"] == 0.0
        assert captured["max_portfolio_usd"] == config.region_max_daily_exposure_usd("EUTEST")
        assert captured["max_portfolio_usd"] != config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD


class TestEntrySimThreadsMaxPortfolioUsd:
    """
    Narrower unit-level counterpart to TestBacktestEngineIsRegionAware: pins
    the plumbing inside entry_sim.decide_portfolio_entries_sim() itself --
    that its max_portfolio_usd parameter reaches
    entry_manager.apply_portfolio_budget() unchanged. Without this parameter
    (added by the same fix), apply_portfolio_budget() falls back to its own
    default, config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD -- Asia's
    number, wrong for any other region.
    """

    def test_max_portfolio_usd_reaches_apply_portfolio_budget(self, monkeypatch):
        captured = {}

        def _capturing_apply_portfolio_budget(decisions, **kwargs):
            captured.update(kwargs)
            return decisions

        monkeypatch.setattr(
            entry_sim_module, "apply_portfolio_budget", _capturing_apply_portfolio_budget
        )

        entry_sim_module.decide_portfolio_entries_sim(
            candidates=[],
            portfolio=None,
            fill_model=None,
            price_lookup=lambda token_id: None,
            min_net_ev=0.15,
            sizing_bankroll=0.0,
            max_portfolio_usd=0.0,
        )

        assert captured.get("max_portfolio_usd") == 0.0

    def test_max_portfolio_usd_left_at_default_is_forwarded_as_none(self, monkeypatch):
        """
        Back-compat: a caller that does not pass max_portfolio_usd (every
        existing call site before this fix) must still get apply_portfolio_
        budget()'s own global-default behaviour, not an accidental 0.0.
        """
        captured = {}

        def _capturing_apply_portfolio_budget(decisions, **kwargs):
            captured.update(kwargs)
            return decisions

        monkeypatch.setattr(
            entry_sim_module, "apply_portfolio_budget", _capturing_apply_portfolio_budget
        )

        entry_sim_module.decide_portfolio_entries_sim(
            candidates=[],
            portfolio=None,
            fill_model=None,
            price_lookup=lambda token_id: None,
            min_net_ev=0.15,
            sizing_bankroll=0.0,
        )

        assert captured.get("max_portfolio_usd") is None
