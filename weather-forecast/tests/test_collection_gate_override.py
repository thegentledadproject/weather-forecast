"""
tests/test_collection_gate_override.py

Regression tests for config.COLLECTION_GATE_OVERRIDE_STATIONS -- the
2026-08-26 override that lets the European cohort open PAPER positions
before it has accumulated MIN_RESOLUTION_OBS_BEFORE_ENTRY settlement-grade
observations.

The whole justification for the override is that it cannot reach money, so
the tests that matter here are the REFUSALS, not the exemption. Three
independent ways in which it must stop working, each tested on its own:

  1. the station joins LIVE_TRADING_STATIONS,
  2. its region's REGION_LIVE_MAX_* blast radius is raised above zero,
  3. it is not in the registry at all.

Plus the ordering property that a broken observation-count READ still
refuses, override or not: that branch is about a failed query, not an
immature station, and no allowlist should answer it.
"""

import config
import entry_manager


EUROPE = ("EGLC", "LFPB", "LEMD", "EHAM", "LIMC", "EDDM", "EPWA")


def test_every_european_station_is_exempt_and_can_open():
    """The override does what it was added for: obs 4 < 10 opens anyway."""
    for icao in EUROPE:
        assert config.collection_gate_is_overridden(icao), icao
        assert entry_manager.collection_only_reason(
            icao, obs_count=4, bias_n=0, bias_stderr=None,
            enforce_bias_quality=True,
        ) is None, icao


def test_asian_stations_are_untouched():
    """Asia authorises live orders, so nothing there is exempt."""
    for icao in ("WSSS", "RCSS", "VHHH", "WMKK", "ZBAA"):
        assert not config.collection_gate_is_overridden(icao), icao
    reason = entry_manager.collection_only_reason(
        "VHHH", obs_count=4, bias_n=0, bias_stderr=None,
        enforce_bias_quality=True,
    )
    assert reason is not None and "below the" in reason


def test_override_refuses_a_station_on_the_real_money_allowlist(monkeypatch):
    """Arming EGLC for real money re-blocks it rather than inheriting the exemption."""
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", {"WSSS", "RCSS", "EGLC"})
    assert not config.collection_gate_is_overridden("EGLC")
    assert entry_manager.collection_only_reason(
        "EGLC", obs_count=4, enforce_bias_quality=True
    ) is not None


def test_override_refuses_once_the_region_authorises_any_live_order(monkeypatch):
    """Funding europe's blast radius disarms the override for the whole cohort."""
    for dict_name, live_value in (
        ("REGION_LIVE_MAX_CONCURRENT_POSITIONS", 1),
        ("REGION_LIVE_MAX_TOTAL_EXPOSURE_USD", 0.01),
        ("REGION_LIVE_MAX_ORDERS_PER_DAY", 1),
    ):
        raised = dict(getattr(config, dict_name))
        raised["europe"] = live_value
        monkeypatch.setattr(config, dict_name, raised)
        for icao in EUROPE:
            assert not config.collection_gate_is_overridden(icao), (dict_name, icao)
        monkeypatch.undo()


def test_region_missing_from_a_blast_radius_dict_is_treated_as_live(monkeypatch):
    """An unknown region fails toward 'could spend money', so it is not exempt."""
    stripped = {k: v for k, v in config.REGION_LIVE_MAX_ORDERS_PER_DAY.items()
                if k != "europe"}
    monkeypatch.setattr(config, "REGION_LIVE_MAX_ORDERS_PER_DAY", stripped)
    assert config.region_authorises_live_orders("europe")
    assert not config.collection_gate_is_overridden("EGLC")


def test_unregistered_icao_is_never_exempt(monkeypatch):
    monkeypatch.setattr(
        config, "COLLECTION_GATE_OVERRIDE_STATIONS",
        set(config.COLLECTION_GATE_OVERRIDE_STATIONS) | {"ZZZZ"},
    )
    assert not config.collection_gate_is_overridden("ZZZZ")


def test_unreadable_observation_count_still_refuses_despite_the_override():
    """A broken READ is not an immature station; the override must not answer it."""
    reason = entry_manager.collection_only_reason(
        "EGLC", obs_count=None, enforce_bias_quality=True
    )
    assert reason is not None and "refusing to open blind" in reason


def test_override_applies_even_when_bias_quality_is_not_enforced():
    """The replay path (enforce_bias_quality=False) sees the same exemption."""
    assert entry_manager.collection_only_reason(
        "EGLC", obs_count=0, enforce_bias_quality=False
    ) is None
