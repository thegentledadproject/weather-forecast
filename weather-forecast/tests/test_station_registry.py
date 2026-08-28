"""
tests/test_station_registry.py

Sanity guard over config.STATIONS for the 2026-08-05 Asia expansion
(2 -> 13 stations, see design doc sections A/G). These are invariants
the registry itself must hold, not behavior of any one trading-path
function -- catches a copy-paste error in a new StationConfig entry
(duplicate slug, wrong bucket span, a station missing from
STATION_MATURITY) at collection time, long before it would show up as
a bad trade or a silently-skipped station-day.
"""

import config
from clients.official.registry import _CLIENTS

EXPECTED_STATION_COUNT = 20


def test_station_count_matches_expected():
    assert len(config.STATIONS) == EXPECTED_STATION_COUNT


def test_polymarket_city_slugs_unique_and_non_empty():
    slugs = [st.polymarket_city_slug for st in config.STATIONS.values()]
    assert all(slugs), f"empty polymarket_city_slug found: {slugs}"
    assert len(slugs) == len(set(slugs)), f"duplicate polymarket_city_slug: {slugs}"


def test_wunderground_slugs_unique_and_non_empty():
    slugs = [st.wunderground_slug for st in config.STATIONS.values()]
    assert all(slugs), f"empty wunderground_slug found: {slugs}"
    assert len(slugs) == len(set(slugs)), f"duplicate wunderground_slug: {slugs}"


def test_bucket_span_is_eleven_for_every_station():
    import bucket_axis

    for icao, st in config.STATIONS.items():
        axis = bucket_axis.for_station(st)
        span = (st.bucket_max_c - st.bucket_min_c) // axis.step + 1
        assert span == config.EXPECTED_BUCKET_COUNT, (
            f"{icao}: bucket window {st.bucket_min_c}-{st.bucket_max_c} at step "
            f"{axis.step} is {span} buckets, expected {config.EXPECTED_BUCKET_COUNT}"
        )
        assert (st.bucket_max_c - st.bucket_min_c) % axis.step == 0, (
            f"{icao}: window {st.bucket_min_c}-{st.bucket_max_c} is not a whole "
            f"number of step-{axis.step} buckets"
        )


def test_utc_offset_hours_in_registered_timezones():
    # 5/8/9 are the Asian registry. 0/1 are European STANDARD-time offsets.
    # -3..-8 are the Americas, also STANDARD time (see
    # StationConfig.iana_timezone -- the live path resolves DST via
    # config.current_utc_offset_hours(); this static field is what the
    # backtest reads).
    allowed = (-8, -7, -6, -5, -3, 0, 1, 5, 8, 9)
    for icao, st in config.STATIONS.items():
        assert st.utc_offset_hours in allowed, (
            f"{icao}: unexpected utc_offset_hours {st.utc_offset_hours}"
        )


def test_every_station_has_a_maturity_entry():
    for icao in config.STATIONS:
        assert icao in config.STATION_MATURITY, f"{icao}: missing from config.STATION_MATURITY"


def test_exactly_one_mature_station_in_the_frozen_snapshot():
    """
    The SNAPSHOT, which is what the backtest replica reads. The live answer
    comes from config.station_maturity() and is measured from storage -- it
    is not pinned here, because pinning a derived value to a literal is how
    the literal became the authority in the first place.
    """
    mature = [icao for icao, m in config.MATURITY_SNAPSHOT.items() if m == "mature"]
    assert mature == ["WSSS"], f"expected only WSSS in the snapshot, got: {mature}"


def test_the_snapshot_covers_the_whole_registry():
    """A station missing from the snapshot would replay as exploratory silently."""
    assert set(config.MATURITY_SNAPSHOT) == set(config.STATIONS)


def test_every_maturity_override_is_well_formed_and_justified():
    """
    An override trades on something other than the evidence, so the bar is
    that it must be DELIBERATE and EXPLAINED -- not that there are none.
    A bare string or an empty reason is how an override becomes the quiet
    default the whole thing used to be.
    """
    for icao, value in config.MATURITY_OVERRIDE.items():
        assert icao in config.STATIONS, f"override for unregistered station {icao}"
        assert isinstance(value, tuple) and len(value) == 2, (
            f"{icao}: override must be (maturity, justification), got {value!r}"
        )
        maturity, reason = value
        assert maturity in ("mature", "exploratory"), f"{icao}: bad maturity {maturity!r}"
        assert isinstance(reason, str) and len(reason.strip()) >= 10, (
            f"{icao}: override needs a real written reason, got {reason!r}"
        )


def test_an_override_only_matters_for_an_allowlisted_station():
    """
    Forcing maturity on a station outside LIVE_TRADING_STATIONS does nothing
    -- the AND still holds. Worth failing on, because it reads like it works.
    """
    for icao in config.MATURITY_OVERRIDE:
        if config.MATURITY_OVERRIDE[icao][0] == "mature":
            assert icao in config.LIVE_TRADING_STATIONS, (
                f"{icao} is forced mature but is not allowlisted, so the override "
                f"has no effect -- misleading rather than dangerous, but fix it"
            )


def test_vhhh_settlement_invariants():
    vhhh = config.get_station("VHHH")
    assert vhhh.bucket_edge_mode == "floor"
    assert vhhh.resolution_grade_source == "hko_daily_max"
    assert vhhh.metar_ingest_mode == "skip"
    assert vhhh.official_client_key == "hko"


def test_opkc_proxy_invariant():
    opkc = config.get_station("OPKC")
    assert opkc.metar_ingest_mode == "proxy"


def test_default_bucket_edge_mode_and_resolution_source_elsewhere():
    # Every station other than VHHH keeps the "resolution"/half_up/
    # metar_daily_max defaults -- VHHH is the one documented exception
    # (settles on HKO's climate extract, not the airport METAR record).
    for icao, st in config.STATIONS.items():
        if icao == "VHHH":
            continue
        assert st.bucket_edge_mode == "half_up", f"{icao}: expected half_up, got {st.bucket_edge_mode}"
        assert st.resolution_grade_source == "metar_daily_max", (
            f"{icao}: expected metar_daily_max, got {st.resolution_grade_source}"
        )
        expected_ingest_mode = "proxy" if icao == "OPKC" else "resolution"
        assert st.metar_ingest_mode == expected_ingest_mode, (
            f"{icao}: expected metar_ingest_mode={expected_ingest_mode!r}, got {st.metar_ingest_mode!r}"
        )


def test_official_client_key_is_registered():
    for icao, st in config.STATIONS.items():
        assert st.official_client_key in _CLIENTS, (
            f"{icao}: official_client_key {st.official_client_key!r} not registered in "
            f"clients.official.registry._CLIENTS ({list(_CLIENTS)})"
        )


def test_wwis_stations_have_city_name_except_taipei_and_london():
    # Every station served by the generic "wwis" client must name its WWIS
    # city -- EXCEPT two documented gaps, both honestly absent from WWIS
    # rather than guessed:
    #   RCSS (Taipei)  -- WWIS is a UN service that does not cover Taiwan.
    #   EGLC (London)  -- London is simply not in the WWIS city list (verified
    #                     twice, directly, against the live full city list).
    exempt = {"RCSS", "EGLC"}
    for icao, st in config.STATIONS.items():
        if st.official_client_key != "wwis":
            continue
        if icao in exempt:
            assert st.wwis_city_name == "", f"{icao} is expected to have an empty wwis_city_name (absent from WWIS)"
        else:
            assert st.wwis_city_name != "", f"{icao}: wwis-keyed station has no wwis_city_name set"


def test_local_today_per_station_never_differs_from_legacy_by_more_than_a_day():
    # config.local_today(st) can legitimately land on a different calendar
    # date than the zero-arg legacy UTC+8 default near either clock's own
    # midnight boundary (Tokyo/Seoul/Busan are UTC+9, Karachi is UTC+5) --
    # but never by more than one day, since every registered offset is
    # within +/-24h of UTC+8.
    legacy_today = config.local_today()
    for icao in config.STATIONS:
        station_today = config.local_today(icao)
        assert abs((station_today - legacy_today).days) <= 1, (
            f"{icao}: local_today() drifted more than one day from the legacy default "
            f"({station_today} vs {legacy_today})"
        )
