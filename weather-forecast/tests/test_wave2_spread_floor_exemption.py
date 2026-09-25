"""
Wave 2 item 2b. SPREAD_FLOOR_C (0.70) stays on every tier EXCEPT
corrected_error: ensemble, pooled_error, fallback_default, replay_constant,
AND the naive measured_error tier. corrected_error_rmse() prices its own
value, subject to (i) the existing upper gate
config.MAX_ERROR_RMSE_PER_BUCKET in entry_manager.collection_only_reason and
(ii) MEASURED_SPREAD_MIN_C = 0.30, the settlement-rounding sd sqrt(1/12),
below which no error sd is a measurement.

WHY corrected_error ONLY, not measured_error TOO (controller ruling). Both
are this station's own error record, but MAX_ERROR_RMSE_PER_BUCKET is built
on corrected_error_rmse, which returns None below
MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE (15) residuals -- and
entry_manager.collection_only_reason() treats that None as "no gate", i.e.
FAILS OPEN. measured_error_spread() can fire on as few as MIN_SPREAD_PAIRS
(5) pairs, a range corrected_error cannot see. Exempting it too would leave
a 5-14-pair station both under-priced and completely ungated -- and Task 1's
narrower fetch window only lowers n, making that band more populated, not
less.

WHY THE FLOOR MATTERS AT ALL. EDDM's priced sd pinned to EXACTLY 0.700
against a measured 0.445; the model underpriced the WINNING bucket on 10 of
10 days and sold it as NO (EDDM 0-for-11 on NO). A too-wide spread
manufactures NO-side edges on the bucket most likely to win. See
config.SPREAD_FLOOR_C's note.

THE REPLAY CANNOT SCORE THIS: backtest/engine.py passes
allow_measured_spread=False unconditionally. Pinned below so nobody reads a
sweep as evidence about it.
"""
from datetime import date, timedelta

import pytest

import calibration
import config

ASIA = "ZSPD"      # ceiling 2.0
EUROPE = "EDDM"    # ceiling 2.0
AMERICAS = "KSEA"  # ceiling None


def _tiers(monkeypatch, corrected, measured):
    monkeypatch.setattr(calibration, "corrected_error_rmse", lambda icao: corrected)
    monkeypatch.setattr(calibration, "measured_error_spread", lambda icao: measured)


# --- measured tiers price their own value ------------------------------------

def test_a_corrected_rmse_under_the_old_floor_is_priced_as_is(monkeypatch):
    """EDDM: 0.445 measured, 0.700 priced. Now 0.45 (rounded to the grid)."""
    _tiers(monkeypatch, (0.445, 20), (0.452, 20))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "corrected_error"
    assert sd == 0.45


def test_the_naive_sd_tier_keeps_the_floor_until_the_gate_can_see_it(monkeypatch):
    """measured_error is ALSO this station's own error record, but it is
    reachable on as few as MIN_SPREAD_PAIRS (5) pairs -- below
    MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE (15), where corrected_error_rmse (and
    therefore MAX_ERROR_RMSE_PER_BUCKET) returns None and
    entry_manager.collection_only_reason() fails OPEN on that None. A
    5-14-pair station exempted here would be both under-priced and
    completely ungated, so this tier keeps SPREAD_FLOOR_C."""
    _tiers(monkeypatch, (None, 9), (0.452, 14))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "measured_error"
    assert sd == config.SPREAD_FLOOR_C


def test_a_measured_value_below_the_rounding_sd_is_raised_to_it(monkeypatch):
    """sqrt(1/12) = 0.289: a 0.20 'measurement' against whole-degree truth
    is a sample artefact, not a sharper forecast."""
    _tiers(monkeypatch, (0.20, 20), (0.20, 20))
    sd, source = calibration.estimate_std_dev([], [], station_icao=ASIA)
    assert source == "corrected_error"
    assert sd == config.MEASURED_SPREAD_MIN_C == 0.30


def test_the_regional_ceiling_still_applies_to_a_measured_tier(monkeypatch):
    _tiers(monkeypatch, (2.6, 20), (2.6, 20))
    sd, _ = calibration.estimate_std_dev([], [], station_icao=ASIA)
    assert sd == config.SPREAD_CEILING_C
    sd, _ = calibration.estimate_std_dev([], [], station_icao=AMERICAS)
    assert sd == 2.6


# --- unmeasured tiers keep the floor -----------------------------------------

def test_the_ensemble_tier_keeps_the_floor(monkeypatch):
    _tiers(monkeypatch, (None, 0), (None, 0))
    sd, source = calibration.estimate_std_dev(
        [], [], ensemble_members=[32.0, 32.1, 31.9, 32.05], station_icao=EUROPE)
    assert source == "ensemble"
    assert sd == config.SPREAD_FLOOR_C


def test_the_pooled_tier_keeps_the_floor(monkeypatch):
    _tiers(monkeypatch, (None, 0), (None, 0))
    monkeypatch.setattr(calibration, "pooled_error_spread", lambda region=None: (0.41, 200))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "pooled_error"
    assert sd == config.SPREAD_FLOOR_C


def test_the_fallback_and_replay_constants_are_untouched(monkeypatch):
    _tiers(monkeypatch, (None, 0), (None, 0))
    monkeypatch.setattr(calibration, "pooled_error_spread", lambda region=None: (None, 0))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert (sd, source) == (config.POOLED_SPREAD_FALLBACK_C, "fallback_default")
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE, allow_measured=False)
    assert (sd, source) == (config.POOLED_SPREAD_FALLBACK_C, "replay_constant")


# --- the flag ----------------------------------------------------------------

def test_flag_off_restores_the_unconditional_floor(monkeypatch):
    monkeypatch.setattr(config, "SPREAD_FLOOR_MEASURED_TIERS_EXEMPT", False)
    _tiers(monkeypatch, (0.445, 20), (0.452, 20))
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "corrected_error"
    assert sd == config.SPREAD_FLOOR_C


@pytest.mark.parametrize("measured", [True, False])
@pytest.mark.parametrize("value", [0.10, 0.30, 0.45, 0.70, 0.90, 2.60])
def test_flag_off_makes_clamp_spread_byte_identical_for_every_tier(monkeypatch, measured, value):
    """With the flag off, _clamp_spread must reproduce the PRE-WAVE-2 clamp
    -- max(value, SPREAD_FLOOR_C), then the ceiling -- for every value and
    regardless of `measured`. Computed from the pre-2b formula rather than
    hardcoded, so this is a byte-identical check, not an inspection of
    today's numbers."""
    monkeypatch.setattr(config, "SPREAD_FLOOR_MEASURED_TIERS_EXEMPT", False)
    pre_wave2 = round(min(max(value, config.SPREAD_FLOOR_C), config.SPREAD_CEILING_C), 2)
    assert calibration._clamp_spread(value, measured=measured) == pre_wave2


def test_spread_source_strings_are_unchanged():
    assert calibration.MEASURED_SPREAD_SOURCES == frozenset({"corrected_error"})
    assert not (calibration.MEASURED_SPREAD_SOURCES & config.LOW_CONFIDENCE_SPREAD_SOURCES)


def test_measured_spread_sources_matches_the_call_sites(monkeypatch):
    """MEASURED_SPREAD_SOURCES must track exactly the estimate_std_dev
    branches that call _clamp_spread(measured=True) -- a frozenset that
    drifted from the call sites would silently exempt (or fail to exempt) a
    tier from the floor without any test noticing."""
    calls = []
    original_clamp = calibration._clamp_spread

    def spy(value, station_icao=None, measured=False):
        calls.append(measured)
        return original_clamp(value, station_icao, measured=measured)

    monkeypatch.setattr(calibration, "_clamp_spread", spy)

    measured_flags = {}

    _tiers(monkeypatch, (0.5, 20), (0.5, 20))
    calls.clear()
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "corrected_error" and len(calls) == 1
    measured_flags[source] = calls[0]

    _tiers(monkeypatch, (None, 9), (0.5, 14))
    calls.clear()
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "measured_error" and len(calls) == 1
    measured_flags[source] = calls[0]

    _tiers(monkeypatch, (None, 0), (None, 0))
    calls.clear()
    sd, source = calibration.estimate_std_dev(
        [], [], ensemble_members=[32.0, 32.1, 31.9, 32.05], station_icao=EUROPE)
    assert source == "ensemble" and len(calls) == 1
    measured_flags[source] = calls[0]

    monkeypatch.setattr(calibration, "pooled_error_spread", lambda region=None: (0.41, 200))
    calls.clear()
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "pooled_error" and len(calls) == 1
    measured_flags[source] = calls[0]

    monkeypatch.setattr(calibration, "pooled_error_spread", lambda region=None: (None, 0))
    calls.clear()
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE)
    assert source == "fallback_default" and len(calls) == 1
    measured_flags[source] = calls[0]

    calls.clear()
    sd, source = calibration.estimate_std_dev([], [], station_icao=EUROPE, allow_measured=False)
    assert source == "replay_constant" and len(calls) == 1
    measured_flags[source] = calls[0]

    assert {s for s, m in measured_flags.items() if m} == calibration.MEASURED_SPREAD_SOURCES


# --- the upper gate is the other half of the argument ------------------------

def test_the_error_width_gate_still_stops_a_station_wider_than_its_bucket():
    """The floor comes off the bottom; MAX_ERROR_RMSE_PER_BUCKET stays on top.
    A measured tier is priced as-is BETWEEN the two, never outside them."""
    import entry_manager
    reason = entry_manager.collection_only_reason(
        "ZSPD", 999, bias_n=99, bias_stderr=0.1, enforce_bias_quality=True,
        error_width_ratio=1.2,
    )
    assert reason is not None and "wider than" in reason


# --- the pure extractions the falsifier composes -----------------------------

def test_corrected_rmse_from_dated_is_what_the_io_function_computes(monkeypatch):
    day0 = date(2026, 8, 1)
    dated = [(day0 + timedelta(days=i), e) for i, e in enumerate(
        [0.5, -0.5, 0.4, -0.4, 0.6, -0.6, 0.3, -0.3, 0.5, -0.5, 0.2, -0.2,
         0.4, -0.4, 0.5, -0.5, 0.3, -0.3, 0.6, -0.6, 0.4, -0.4])]
    monkeypatch.setattr(calibration, "_dated_error_samples", lambda icao: dated)
    assert calibration.corrected_error_rmse("ZSPD") == calibration.corrected_error_rmse_from_dated(dated)
    rmse, n = calibration.corrected_error_rmse_from_dated(dated)
    assert rmse is not None and n >= config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE


def test_measured_spread_from_errors_matches_the_tier_rule():
    assert calibration.measured_error_spread_from_errors([0.5, -0.5]) == (None, 2)
    sd, n = calibration.measured_error_spread_from_errors([0.5, -0.5, 0.4, -0.4, 0.6, -0.6])
    assert n == 6 and sd == pytest.approx(0.5550, abs=1e-3)  # sqrt(1.54 / 5)


def test_priced_measured_spread_is_the_measured_clamp():
    assert calibration.priced_measured_spread(0.445, EUROPE) == 0.45
    assert calibration.priced_measured_spread(0.20, EUROPE) == config.MEASURED_SPREAD_MIN_C
    assert calibration.priced_measured_spread(2.6, ASIA) == config.SPREAD_CEILING_C


def test_the_replay_path_now_sees_this_change():
    # GAP 3: the replay prices on the point-in-time measured tiers, so it is
    # no longer blind to the spread-floor exemption.
    import inspect
    from backtest import engine
    assert "allow_measured_spread=False" not in inspect.getsource(engine)
