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

EXPECTED_STATION_COUNT = 13


def test_thirteen_stations_registered():
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
    for icao, st in config.STATIONS.items():
        span = st.bucket_max_c - st.bucket_min_c + 1
        assert span == config.EXPECTED_BUCKET_COUNT, (
            f"{icao}: bucket span {st.bucket_min_c}-{st.bucket_max_c} is {span} values, "
            f"expected {config.EXPECTED_BUCKET_COUNT}"
        )


def test_utc_offset_hours_in_registered_timezones():
    for icao, st in config.STATIONS.items():
        assert st.utc_offset_hours in (5, 8, 9), f"{icao}: unexpected utc_offset_hours {st.utc_offset_hours}"


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


def test_maturity_overrides_are_empty_by_default():
    """
    An override trades on something other than the evidence. It should be
    visible in a diff and require a written reason -- never be the quiet
    default the whole thing used to be.
    """
    assert config.MATURITY_OVERRIDE == {}, (
        f"maturity overrides in effect: {config.MATURITY_OVERRIDE} -- each bypasses "
        f"the measured criteria for the gate that authorises real money"
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


def test_wwis_stations_have_city_name_except_taipei():
    # Every station served by the generic "wwis" client must name its WWIS
    # city -- EXCEPT RCSS (Taipei), which is honestly absent from WWIS (a
    # UN service that does not cover Taiwan; see config.py's RCSS entry).
    for icao, st in config.STATIONS.items():
        if st.official_client_key != "wwis":
            continue
        if icao == "RCSS":
            assert st.wwis_city_name == "", "RCSS is expected to have an empty wwis_city_name (Taiwan absent from WWIS)"
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
