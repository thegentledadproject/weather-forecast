"""
Wave 3 item 3e. The 2026-09-15 reviews listed comments that describe rules
that no longer exist: "both tighten" (only the take-profit tightens since
2026-08-18), "skip BOTH price-noise exits" (the lottery carve-out skips the
stop; the take is kept), and four references to a trailing stop removed
2026-08-17. Text is pinned so the corrections cannot rot back.
"""
import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1]


def _src(name):
    return (PKG / name).read_text(encoding="utf-8")


def test_risk_manager_no_longer_claims_both_thresholds_tighten():
    src = _src("risk_manager.py")
    assert "Both tighten after the edge-decay hour" not in src
    assert "Only the TAKE-PROFIT tightens after the edge-decay hour" in src
    assert "skip BOTH price-noise exits" not in src
    assert "skip the STOP-LOSS" in src


def test_config_no_longer_claims_both_thresholds_tighten():
    src = _src("config.py")
    assert "After this local hour, tighten both thresholds" not in src
    assert "After this local hour, tighten the take-profit target" in src
    assert "10:00 the stop tightens from 30% to 15%" not in src
    assert "UNTIL 2026-08-18 the stop tightened at 10:00" in src


def test_models_describe_the_trailing_stop_as_history():
    src = _src("models.py")
    assert "-- drives the trailing stop" not in src
    assert "no exit rule reads it since the trailing stop was removed 2026-08-17" in src
    assert '"trailing_stop", "take_profit" (should_exit=True) and "trailing_active",' not in src
    assert '"closed_trailing_stop" -- HISTORICAL rows only' in src
    assert "The first two closed_* strings are derived" in src


def test_executor_and_storage_no_longer_reference_a_live_trailing_stop():
    assert "typical trailing-stop gain" not in _src("executor.py")
    assert "typical take-profit gain" in _src("executor.py")
    assert "'closed_trailing_stop' (historical rows only" in _src("storage.py")
