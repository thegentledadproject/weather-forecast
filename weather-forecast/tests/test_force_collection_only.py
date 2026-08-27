"""
tests/test_force_collection_only.py

Regression tests for config.FORCE_COLLECTION_ONLY_STATIONS -- the operator
STOP added 2026-08-27, and the inverse of the two override sets it sits
above in entry_manager.collection_only_reason().

The override sets are justified by their REFUSALS, so their tests are about
the ways they must stop working. This one is justified by the opposite
property: it must work unconditionally. So the tests here are about the
ways it must NOT be escapable --

  1. a station that passes every gate cleanly is still stopped,
  2. being on COLLECTION_GATE_OVERRIDE_STATIONS does not undo it,
  3. being on BIAS_GATE_OVERRIDE_STATIONS does not undo it,
  4. enforce_bias_quality=False does not undo it (the replay path),
  5. an unregistered ICAO is still stopped,

plus the two boundaries it must NOT cross: it is entry-only, and it leaves
every station not named in it alone.
"""

import config
import entry_manager


# The gate arguments RPLL actually passes on, as of 2026-08-27: 21 bias
# pairs against a required 5, standard error 0.325C against a required
# 0.50C, observation count well past the minimum. Nothing here is the
# reason it is stopped -- that is the point of the test.
CLEAN = dict(obs_count=21, bias_n=21, bias_stderr=0.325, enforce_bias_quality=True)


def test_rpll_is_stopped():
    """The station the stop was added for opens nothing."""
    assert config.force_collection_only("RPLL")
    reason = entry_manager.collection_only_reason("RPLL", **CLEAN)
    assert reason is not None
    assert "FORCE_COLLECTION_ONLY_STATIONS" in reason


def test_a_station_that_passes_every_gate_is_still_stopped():
    """The stop is not a gate: clean measurements do not release it."""
    assert entry_manager.collection_only_reason("RPLL", **CLEAN) is not None
    # Same arguments, a station not on the list -> free to trade.
    assert entry_manager.collection_only_reason("WSSS", **CLEAN) is None


def test_collection_gate_override_does_not_undo_the_stop(monkeypatch):
    """Ordering property: the stop is checked before the stronger exemption."""
    monkeypatch.setattr(config, "FORCE_COLLECTION_ONLY_STATIONS", {"EGLC"})
    assert config.collection_gate_is_overridden("EGLC")      # still exempt...
    reason = entry_manager.collection_only_reason(
        "EGLC", obs_count=4, bias_n=0, bias_stderr=None, enforce_bias_quality=True,
    )
    assert reason is not None and "FORCE_COLLECTION_ONLY_STATIONS" in reason


def test_bias_gate_override_does_not_undo_the_stop(monkeypatch):
    """The weaker exemption cannot answer it either."""
    monkeypatch.setattr(config, "FORCE_COLLECTION_ONLY_STATIONS", {"WSSS"})
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"WSSS"})
    reason = entry_manager.collection_only_reason(
        "WSSS", obs_count=99, bias_n=1, bias_stderr=9.9, enforce_bias_quality=True,
    )
    assert reason is not None and "FORCE_COLLECTION_ONLY_STATIONS" in reason


def test_replay_path_is_stopped_too(monkeypatch):
    """enforce_bias_quality=False is backtest/entry_sim's call. It still stops."""
    monkeypatch.setattr(config, "FORCE_COLLECTION_ONLY_STATIONS", {"WSSS"})
    reason = entry_manager.collection_only_reason(
        "WSSS", obs_count=99, bias_n=99, bias_stderr=0.01, enforce_bias_quality=False,
    )
    assert reason is not None and "FORCE_COLLECTION_ONLY_STATIONS" in reason


def test_unregistered_icao_is_stopped(monkeypatch):
    """A name not in STATIONS still stops -- a typo must not be a silent no-op."""
    monkeypatch.setattr(config, "FORCE_COLLECTION_ONLY_STATIONS", {"ZZZZ"})
    assert config.force_collection_only("ZZZZ")
    reason = entry_manager.collection_only_reason("ZZZZ", **CLEAN)
    assert reason is not None and "FORCE_COLLECTION_ONLY_STATIONS" in reason


def test_no_other_station_is_affected():
    """Exactly the listed names, and nothing else in the registry."""
    for icao in config.STATIONS:
        stopped = entry_manager.collection_only_reason(icao, **CLEAN)
        named = icao in config.FORCE_COLLECTION_ONLY_STATIONS
        if named:
            assert stopped is not None and "FORCE_COLLECTION_ONLY" in stopped, icao
        elif stopped is not None:
            assert "FORCE_COLLECTION_ONLY" not in stopped, icao


def test_the_stop_is_entry_only():
    """
    It lives in collection_only_reason(), which nothing on the exit path
    calls. Pinned as a property of the module surface rather than of one
    call, because the cost of getting this wrong is a stopped station
    holding a book it can no longer manage.
    """
    import position_manager
    import inspect

    src = inspect.getsource(position_manager)
    assert "collection_only_reason" not in src
    assert "force_collection_only" not in src
    assert "FORCE_COLLECTION_ONLY" not in src
