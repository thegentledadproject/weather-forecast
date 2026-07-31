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

import statistics
from datetime import date
from typing import List

from models import StationConfig, PointForecast, ObservedReading, CalibratedEstimate


def _monsoon_phase(station: StationConfig, target_date: date) -> str:
    return station.monsoon_phase_by_month.get(target_date.month, "unknown")


def blend_central_estimate(
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    long_term_normal_c: float,
) -> float:
    """
    Combine available forecast points and recent observed history into
    a single central estimate, in degrees C. Weighting rationale (see
    framework doc Section 3B): recent observed data is the
    confirmed-accurate signal, so it gets more weight than forecast
    text alone once we have enough of it.
    """
    valid_forecasts = [f.max_temp_c for f in forecasts if f.max_temp_c is not None]
    forecast_mean = statistics.fmean(valid_forecasts) if valid_forecasts else None

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
) -> float:
    """
    Estimate spread (std dev, in degrees C) for the probability step.
    Prefers real ensemble spread when available; otherwise falls back
    to variance across point forecasts, then to observed-history
    variance, then to a conservative default.
    """
    if ensemble_members and len(ensemble_members) > 1:
        return round(statistics.stdev(ensemble_members), 2)

    valid_forecasts = [f.max_temp_c for f in forecasts if f.max_temp_c is not None]
    if len(valid_forecasts) > 1:
        return round(statistics.stdev(valid_forecasts), 2)

    if observations and len(observations) > 1:
        return round(statistics.stdev(o.max_temp_c for o in observations), 2)

    # Conservative MVP default: reflects the ~1-1.5C spread typically
    # seen in tropical daily-max readings across the stations tested so far.
    return 1.2


def calibrate(
    station: StationConfig,
    target_date: date,
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    ensemble_members: List[float] = None,
) -> CalibratedEstimate:
    """Top-level entry point: build a CalibratedEstimate for one station/date."""
    central = blend_central_estimate(forecasts, observations, station.long_term_normal_max_c)
    spread = estimate_std_dev(forecasts, observations, ensemble_members)

    notes = []
    if not forecasts:
        notes.append("No live model forecasts available -- fell back to observed/normal.")
    if not observations:
        notes.append("No recent observed data for this station -- calibration is forecast-only.")

    return CalibratedEstimate(
        station_icao=station.icao,
        target_date=target_date,
        central_estimate_c=central,
        std_dev_c=spread,
        monsoon_phase=_monsoon_phase(station, target_date),
        inputs_used=[f.source for f in forecasts if f.max_temp_c is not None],
        notes="; ".join(notes),
    )
