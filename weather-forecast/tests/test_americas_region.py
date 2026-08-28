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
