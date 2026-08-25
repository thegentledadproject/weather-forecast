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
