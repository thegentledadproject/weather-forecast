"""
tests/test_corrected_error_spread_tier.py

The priced spread and the error-width GATE must be the same statistic.

WHAT WENT WRONG. calibration.estimate_std_dev's measured tier used
measured_error_spread() -- the standard deviation about the sample's own
mean, i.e. what would be left if the bias correction were perfect and
instantaneous. The error-width gate added 2026-09-03 scores stations on
corrected_error_rmse(), which replays the correction the entry path
really had on each day and scores no day against a bias that saw it.

A station was therefore GATED on one number and TRADED on another. They
are not close, and they do not err in a consistent direction. Measured on
the live book 2026-09-09:

    WSSS   0.624 -> 0.589   (the lag-aware number is NARROWER)
    ZSPD   0.696 -> 0.758   (the lag-aware number is WIDER)

So this is not a licence to narrow. ZSPD is the case that proves it, and
it is the case pinned first below.

WHY IT MATTERS BEYOND TIDINESS. A spread that is too wide is not free on a
two-sided bucket market: it underprices the market's favourite bucket,
which the entry path reads as a NO-side edge on the bucket most likely to
win. See config.SPREAD_FLOOR_C and the EDDM/MMMX finding of 2026-09-09.

WHAT THIS CHANGE DELIBERATELY DOES NOT DO. It does not touch
SPREAD_FLOOR_C, so a station measuring under 0.70 is still floored to
0.70 -- only the number the floor is applied TO changes. Standing the
floor down is a separate, larger change.
"""

import config
import calibration


# The stations below are real registry entries, because _clamp_spread reads
# the region ceiling off the station. Nothing here depends on their live
# measurements -- both estimators are monkeypatched in every test.
ASIA = "ZSPD"
EUROPE = "EDDM"


# --- the tier order ---------------------------------------------------------

def test_the_lag_aware_residual_is_preferred_when_it_exists(monkeypatch):
    """
    ZSPD, and it is the WIDENING direction on purpose: 0.696 -> 0.758.
    A change that only ever narrowed the spread would be a change to how
    confident the model is, which is not what this is.
    """
    monkeypatch.setattr(calibration, "corrected_error_rmse",
                        lambda icao: (0.758, 29))
    monkeypatch.setattr(calibration, "measured_error_spread",
                        lambda icao: (0.696, 29))

    sd, source = calibration.estimate_std_dev(
        [], [], station_icao=ASIA, allow_measured=True)

    assert source == "corrected_error"
    assert sd == 0.76


def test_it_falls_back_to_the_naive_sd_below_the_pair_floor(monkeypatch):
    """
    corrected_error_rmse() returns None under
    MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE. That must read as "use the older
    tier", never as "no measurement at all" -- every Europe and Americas
    station is under the floor today and must keep pricing.
    """
    monkeypatch.setattr(calibration, "corrected_error_rmse",
                        lambda icao: (None, 9))
    monkeypatch.setattr(calibration, "measured_error_spread",
                        lambda icao: (0.452, 14))

    sd, source = calibration.estimate_std_dev(
        [], [], station_icao=EUROPE, allow_measured=True)

    assert source == "measured_error"
    # SPREAD_FLOOR_C still binds on the fallback. Standing it down is a
    # separate change; this test pins that it did NOT happen here.
    assert sd == config.SPREAD_FLOOR_C


def test_neither_measurement_falls_through_to_the_lower_tiers(monkeypatch):
    """
    A station with no error record at all must still reach the ensemble
    tier, not raise and not silently price on None.
    """
    monkeypatch.setattr(calibration, "corrected_error_rmse",
                        lambda icao: (None, 0))
    monkeypatch.setattr(calibration, "measured_error_spread",
                        lambda icao: (None, 0))

    sd, source = calibration.estimate_std_dev(
        [], [], ensemble_members=[30.0, 31.0, 32.5, 29.5],
        station_icao=EUROPE, allow_measured=True)

    assert source == "ensemble"
    assert sd > 0


# --- what must NOT change ---------------------------------------------------

def test_the_backtest_path_is_untouched(monkeypatch):
    """
    allow_measured=False still short-circuits to the replay constant.
    Both measured tiers read the WHOLE stored error record, so either one
    inside a replay leaks days the simulated instant has not reached --
    and corrected_error_rmse() leaks exactly as much as the tier it
    replaces.
    """
    def _boom(icao):  # pragma: no cover - must never be called
        raise AssertionError("the replay path must not read the error record")

    monkeypatch.setattr(calibration, "corrected_error_rmse", _boom)
    monkeypatch.setattr(calibration, "measured_error_spread", _boom)

    sd, source = calibration.estimate_std_dev(
        [], [], station_icao=ASIA, allow_measured=False)

    assert source == "replay_constant"
    assert sd == config.POOLED_SPREAD_FALLBACK_C


def test_a_caller_without_a_station_still_skips_both_measured_tiers(monkeypatch):
    """
    station_icao is optional so pre-existing callers keep working. Without
    one, neither measured tier is reachable -- unchanged by this work.
    """
    def _boom(icao):  # pragma: no cover - must never be called
        raise AssertionError("no station means no station-specific measurement")

    monkeypatch.setattr(calibration, "corrected_error_rmse", _boom)
    monkeypatch.setattr(calibration, "measured_error_spread", _boom)

    sd, source = calibration.estimate_std_dev(
        [], [], ensemble_members=[30.0, 31.0, 32.5], allow_measured=True)

    assert source == "ensemble"


def test_the_new_tier_is_not_low_confidence():
    """
    corrected_error IS this station's own forecast-error history, measured
    more honestly than the tier it replaces. Putting it in
    LOW_CONFIDENCE_SPREAD_SOURCES would make every station on the book
    clear a DOUBLED edge bar -- see tests/test_low_confidence_spread_gate.py,
    which makes the identical argument for measured_error.
    """
    assert "corrected_error" not in config.LOW_CONFIDENCE_SPREAD_SOURCES
    assert "measured_error" not in config.LOW_CONFIDENCE_SPREAD_SOURCES


def test_the_documented_tier_list_names_the_new_source():
    """
    estimate_std_dev's docstring is where the tier order is documented.
    A source string that exists in code but not in that list is how the
    next reader learns the wrong chain.
    """
    assert "corrected_error" in calibration.estimate_std_dev.__doc__
