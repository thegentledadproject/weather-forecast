"""
Gap 1 of the 2026-09-24 plan gap audit: re-arming live must hit a real gate.

Live needs the measured maturity criteria (no MATURITY_OVERRIDE) AND a proven
redemption (config.REDEMPTION_PROVEN_TX). Simulation/paper are unaffected.
"""
import importlib.util

import config


def _fresh_config():
    """config as shipped, without conftest's pins."""
    spec = importlib.util.spec_from_file_location("_fresh_config", config.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh


def test_shipped_config_has_no_override_and_no_proven_redemption():
    fresh = _fresh_config()
    assert fresh.MATURITY_OVERRIDE == {}
    assert fresh.REDEMPTION_PROVEN_TX is None


def test_live_refused_when_the_criteria_fail(monkeypatch):
    monkeypatch.setattr(config, "MATURITY_OVERRIDE", _fresh_config().MATURITY_OVERRIDE)
    monkeypatch.setattr(config, "_maturity_cache", {})
    monkeypatch.setattr(config, "maturity_report",
                        lambda icao: {"station": icao, "mature": False, "criteria": {}})
    monkeypatch.setattr(config, "REDEMPTION_PROVEN_TX", "0xabc")
    for icao in ("WSSS", "RCSS"):
        assert config.station_maturity(icao) == "exploratory"
        assert config.live_mode_is_permitted(icao, "live") is False


def test_live_refused_until_a_redemption_is_proven(monkeypatch):
    monkeypatch.setattr(config, "_maturity_cache", {"WSSS": "mature"})
    monkeypatch.setattr(config, "REDEMPTION_PROVEN_TX", None)
    assert config.live_mode_is_permitted("WSSS", "live") is False
    # The zero-spend rungs do not need it.
    assert config.live_mode_is_permitted("WSSS", "simulation") is True
    assert config.live_mode_is_permitted("WSSS", "paper") is True


def test_live_allowed_when_both_hold(monkeypatch):
    monkeypatch.setattr(config, "_maturity_cache", {"WSSS": "mature"})
    monkeypatch.setattr(config, "REDEMPTION_PROVEN_TX", "0xabc")
    assert config.live_mode_is_permitted("WSSS", "live") is True


def test_no_station_is_live_permitted_so_the_scheduler_falls_back(monkeypatch):
    """
    scheduler.__main__ assigns --fallback-mode to every station this returns
    False for (a per-station if/else, no raise), so an unproven redemption
    boots an all-paper daemon rather than crashing it.
    """
    monkeypatch.setattr(config, "REDEMPTION_PROVEN_TX", None)
    assert not any(config.live_mode_is_permitted(i, "live") for i in config.STATIONS)
