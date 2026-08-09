"""
calibration.py

PURPOSE
-------
Implements framework Steps B + D: turns a set of raw PointForecasts
plus recent ObservedReadings into a single CalibratedEstimate (central
value + spread) for one station on a target date. Already
station-agnostic -- it never reads config.py directly, only the
StationConfig-derived values passed in by pipeline.py. That means
adding a new station requires zero changes here.

DEPENDENCIES
------------
statistics (standard library)
models.py (local)
"""

import math
import statistics
from datetime import date
from typing import List

import config
from models import StationConfig, PointForecast, ObservedReading, CalibratedEstimate


def _monsoon_phase(station: StationConfig, target_date: date) -> str:
    return station.monsoon_phase_by_month.get(target_date.month, "unknown")


def bias_stats(errors: List[float]) -> tuple:
    """
    Reduce a list of per-day (forecast - settled truth) errors, in degrees
    C, to (bias_c, n, standard_error_c).

    bias_c is the mean signed error: POSITIVE means the forecasts run WARM
    of what actually settled, so blend_central_estimate() subtracts it.
    standard_error_c is sd/sqrt(n) -- how well-pinned the bias number is,
    which is the thing worth gating on. n < 2 yields (bias, n, None):
    a single sample has a mean but no measurable precision, and None is
    read downstream as "unknown", never as "zero error".

    Pure, and shared by both callers ON PURPOSE: live measures the errors
    from storage, the backtest measures them from its visibility-filtered
    replay lists, and neither may compute the statistic its own way.
    """
    if not errors:
        return None, 0, None
    n = len(errors)
    mean = statistics.fmean(errors)
    if n < 2:
        return round(mean, 3), n, None
    return round(mean, 3), n, round(statistics.stdev(errors) / math.sqrt(n), 3)


def blend_central_estimate(
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    long_term_normal_c: float,
    forecast_bias_c: float = 0.0,
) -> float:
    """
    Combine available forecast points and recent observed history into
    a single central estimate, in degrees C. Weighting rationale (see
    framework doc Section 3B): recent observed data is the
    confirmed-accurate signal, so it gets more weight than forecast
    text alone once we have enough of it.

    forecast_bias_c is this station's MEASURED mean (forecast - settled
    truth) and is subtracted from the forecast term before blending, so a
    source that habitually runs 1.7C cool is read as what it has actually
    meant historically rather than at face value. It corrects only the
    FORECAST term: the observed term is settled truth and has no bias to
    remove. Defaults to 0.0 = the pre-2026-08-09 uncorrected behaviour.

    Note the 60/40 tilt toward observed history is NOT itself a bias
    correction -- it is a level anchor tuned on Singapore, where the daily
    max barely moves. At a station with real day-to-day variance it anchors
    to recent weather instead, which is why the explicit term above is
    needed rather than leaning harder on the blend.
    """
    valid_forecasts = [f.max_temp_c for f in forecasts if f.max_temp_c is not None]
    forecast_mean = statistics.fmean(valid_forecasts) if valid_forecasts else None
    if forecast_mean is not None and forecast_bias_c:
        forecast_mean -= forecast_bias_c

    observed_mean = statistics.fmean(o.max_temp_c for o in observations) if observations else None

    if forecast_mean is not None and observed_mean is not None:
        # 60/40 weight toward observed history over raw forecast text --
        # tunable, but directionally justified by the measured NEA bias.
        return round(0.4 * forecast_mean + 0.6 * observed_mean, 1)
    if observed_mean is not None:
        return round(observed_mean, 1)
    if forecast_mean is not None:
        return round(forecast_mean, 1)
    return round(long_term_normal_c, 1)


def estimate_std_dev(
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    ensemble_members: List[float] = None,
) -> tuple:
    """
    Estimate spread (std dev, in degrees C) for the probability step, and
    report WHICH tier of the fallback chain produced it. Prefers real
    ensemble spread when available; otherwise falls back to variance
    across point forecasts, then to observed-history variance, then to a
    conservative flat default.

    Returns (std_dev_c, source), where source is one of "ensemble",
    "forecast_variance", "observed_variance", "fallback_default" -- see
    CalibratedEstimate.spread_source for how this gets used downstream.
    The source matters as much as the number: a std dev is an input to
    the probability distribution the edge is measured against, so an
    edge computed on a guessed spread is only as trustworthy as the guess.
    """
    if ensemble_members and len(ensemble_members) > 1:
        return round(statistics.stdev(ensemble_members), 2), "ensemble"

    valid_forecasts = [f.max_temp_c for f in forecasts if f.max_temp_c is not None]
    if len(valid_forecasts) > 1:
        return round(statistics.stdev(valid_forecasts), 2), "forecast_variance"

    if observations and len(observations) > 1:
        return round(statistics.stdev(o.max_temp_c for o in observations), 2), "observed_variance"

    # Conservative MVP default: reflects the ~1-1.5C spread typically
    # seen in tropical daily-max readings across the stations tested so far.
    # No real spread signal behind this number at all.
    return 1.2, "fallback_default"


def calibrate(
    station: StationConfig,
    target_date: date,
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    ensemble_members: List[float] = None,
    forecast_bias_c: float = 0.0,
) -> CalibratedEstimate:
    """
    Top-level entry point: build a CalibratedEstimate for one station/date.

    forecast_bias_c is the station's measured (forecast - settled truth)
    mean; see blend_central_estimate. Callers that have not measured it
    pass 0.0 and get the uncorrected estimate. config.ENABLE_FORECAST_BIAS_
    CORRECTION is honoured here rather than at every call site, so turning
    the correction off is one flag and not a code change.
    """
    applied_bias = forecast_bias_c if config.ENABLE_FORECAST_BIAS_CORRECTION else 0.0
    central = blend_central_estimate(
        forecasts, observations, station.long_term_normal_max_c, forecast_bias_c=applied_bias
    )
    spread, spread_source = estimate_std_dev(forecasts, observations, ensemble_members)

    notes = []
    if not forecasts:
        notes.append("No live model forecasts available -- fell back to observed/normal.")
    if not observations:
        notes.append("No recent observed data for this station -- calibration is forecast-only.")
    if spread_source == "fallback_default":
        notes.append("Spread has no real signal behind it (flat 1.2C default) -- edge gate tightened accordingly.")
    if applied_bias:
        notes.append(
            f"Forecast term bias-corrected by {-applied_bias:+.2f}C "
            f"(measured mean forecast-minus-settled = {applied_bias:+.2f}C)."
        )

    return CalibratedEstimate(
        station_icao=station.icao,
        target_date=target_date,
        central_estimate_c=central,
        std_dev_c=spread,
        monsoon_phase=_monsoon_phase(station, target_date),
        inputs_used=[f.source for f in forecasts if f.max_temp_c is not None],
        notes="; ".join(notes),
        spread_source=spread_source,
        forecast_bias_c=applied_bias,
    )
