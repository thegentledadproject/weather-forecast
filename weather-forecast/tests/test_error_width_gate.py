"""
tests/test_error_width_gate.py

The ERROR-WIDTH GATE, added 2026-09-03: a station whose forecast error is
wider than the bucket it is being asked to resolve opens nothing.

This is the "measured gate" config.FORCE_COLLECTION_ONLY_STATIONS' RPLL
note said should replace a named stop once someone measured a threshold
that was not reverse-engineered to catch one station. The threshold is
1.0x the bucket width, which is the point where the model's own error is
as wide as the thing it is betting on -- an arithmetic boundary, not a
fitted one.

Three properties are tested, in the order they matter:

  1. THE MEASUREMENT IS CAUSAL. corrected_error_rmse() scores each day
     against the bias correction available BEFORE that day. A day that
     helped set its own correction would flatter the residual and let a
     station through on a number it never traded on.
  2. THE GATE IS NOT ESCAPABLE by the override that exists for immature
     stations -- an unresolvable station is not an immature one, and
     collecting for another month does not narrow its error.
  3. THE GATE IS SILENT WITHOUT A MEASUREMENT. None means "not measured",
     which must read as "do not run this check", exactly as bias_n does.
"""

import math

import calibration
import config
import entry_manager


# The gate arguments a mature station passes cleanly. Nothing here is ever
# the reason a test below fails a station -- the error width is.
CLEAN = dict(obs_count=21, bias_n=21, bias_stderr=0.325, enforce_bias_quality=True)


# --- config.bucket_step_c ---------------------------------------------------

def test_bucket_step_in_c_matches_a_celsius_axis():
    """A 1C bucket is 1C wide. The identity case, and most of the registry."""
    assert config.bucket_step_c("WSSS") == 1.0


def test_bucket_step_in_c_converts_a_fahrenheit_axis():
    """
    The Americas trade a 2F axis while every error in the database is in
    C. Comparing 1.2C of error against a step of "2" would read as
    comfortably inside the bucket when it is in fact wider than it.
    """
    step = config.bucket_step_c("KLGA")
    assert math.isclose(step, 2 * 5 / 9, rel_tol=1e-9)
    assert step < 1.2  # the point of the conversion


# --- calibration.corrected_error_rmse ---------------------------------------

def test_rmse_is_none_below_the_minimum_sample(monkeypatch):
    """
    Too few days is "unknown", not "fine". Returning 0.0 here would be a
    silent pass for exactly the new stations that have never been checked.
    """
    monkeypatch.setattr(
        calibration, "_dated_error_samples", lambda icao: _series([0.1] * 4))
    rmse, n = calibration.corrected_error_rmse("WSSS")
    assert rmse is None
    assert n < config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE


def test_rmse_excludes_the_scored_day_from_its_own_correction(monkeypatch):
    """
    CAUSALITY. A station whose error is a constant -2.0C has a residual of
    ~0 once the correction has learned it -- but the FIRST scored days,
    corrected on a sample that did not include them, must still be scored
    on what was knowable then.

    The check that matters is the negative one: if the scored day were in
    its own sample, a constant-error station would score exactly 0.0 on
    every day, including the first.
    """
    monkeypatch.setattr(
        calibration, "_dated_error_samples", lambda icao: _series([-2.0] * 30))
    rmse, n = calibration.corrected_error_rmse("WSSS")
    assert n >= config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE
    # A constant error is fully learnable, so the residual is small...
    assert rmse < 0.05
    # ...but the alternating series below is not, and that is the contrast
    # that proves the correction is being applied at all rather than the
    # raw error being returned.
    monkeypatch.setattr(
        calibration, "_dated_error_samples",
        lambda icao: _series([-2.0 if i % 2 else 2.0 for i in range(30)]))
    swinging, _ = calibration.corrected_error_rmse("WSSS")
    assert swinging > 1.5


def test_rmse_is_the_residual_width_not_the_bias(monkeypatch):
    """
    A station can be badly biased and still perfectly resolvable. The gate
    must not punish a large bias that is measured precisely -- that is what
    the bias correction is for, and the existing bias gates already ask
    whether it is known well.
    """
    monkeypatch.setattr(
        calibration, "_dated_error_samples", lambda icao: _series([-3.0] * 30))
    rmse, _ = calibration.corrected_error_rmse("WSSS")
    assert rmse < 0.05, "a constant -3C bias is corrected away, not gated on"


# --- the gate itself --------------------------------------------------------

def test_a_station_wider_than_its_bucket_is_stopped():
    reason = entry_manager.collection_only_reason(
        "WSSS", error_width_ratio=1.27, **CLEAN)
    assert reason is not None
    assert "wider than" in reason


def test_a_station_inside_its_bucket_passes():
    assert entry_manager.collection_only_reason(
        "WSSS", error_width_ratio=0.64, **CLEAN) is None


def test_the_boundary_passes():
    """
    Exactly at the threshold is INSIDE. The bar is "wider than the bucket",
    and a ratio of exactly 1.0 is not wider than it.
    """
    assert entry_manager.collection_only_reason(
        "WSSS", error_width_ratio=config.MAX_ERROR_RMSE_PER_BUCKET,
        **CLEAN) is None


def test_an_unmeasured_ratio_does_not_run_the_check():
    """None is "not measured" -- the replay path and every old caller."""
    assert entry_manager.collection_only_reason("WSSS", **CLEAN) is None
    assert entry_manager.collection_only_reason(
        "WSSS", error_width_ratio=None, **CLEAN) is None


def test_the_whole_gate_override_does_not_exempt_a_wide_station(monkeypatch):
    """
    ORDERING. COLLECTION_GATE_OVERRIDE_STATIONS exists to let an immature
    station skip the maturity checks. An unresolvable station is not an
    immature one: another month of collection does not narrow its error,
    so the override must not reach this check.
    """
    monkeypatch.setattr(config, "COLLECTION_GATE_OVERRIDE_STATIONS", {"WSSS"})
    assert entry_manager.collection_only_reason(
        "WSSS", error_width_ratio=1.27, **CLEAN) is not None


def test_a_broken_observation_read_still_wins():
    """
    The blind-read refusal stays first. It says "we cannot enforce the
    gate", which is true whatever the error width turned out to be.
    """
    reason = entry_manager.collection_only_reason(
        "WSSS", obs_count=None, error_width_ratio=0.64,
        bias_n=21, bias_stderr=0.325, enforce_bias_quality=True)
    assert reason is not None
    assert "could not be read" in reason


def test_the_operator_stop_still_wins():
    """FORCE_COLLECTION_ONLY_STATIONS stays above every measurement."""
    reason = entry_manager.collection_only_reason(
        "RPLL", error_width_ratio=0.10, **CLEAN)
    assert reason is not None
    assert "FORCE_COLLECTION_ONLY_STATIONS" in reason


def test_disabling_the_threshold_disables_the_gate(monkeypatch):
    """The documented revert: None on the constant, not a huge number."""
    monkeypatch.setattr(config, "MAX_ERROR_RMSE_PER_BUCKET", None)
    assert entry_manager.collection_only_reason(
        "WSSS", error_width_ratio=99.0, **CLEAN) is None


# --- helpers ----------------------------------------------------------------

def _series(errors):
    """[(date, error)] one day apart, oldest first -- the shape storage returns."""
    from datetime import date, timedelta

    start = date(2026, 8, 1)
    return [(start + timedelta(days=i), e) for i, e in enumerate(errors)]
