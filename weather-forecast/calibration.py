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
from typing import List, Optional

import config
import storage
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
    forecast_weight: float = None,
) -> float:
    """
    Combine available forecast points and recent observed history into a
    single central estimate, in degrees C.

    forecast_bias_c is this station's MEASURED mean (forecast - settled
    truth) and is subtracted from the forecast term before blending, so a
    source that habitually runs 1.7C cool is read as what it has actually
    meant historically rather than at face value. It corrects only the
    FORECAST term: the observed term is settled truth and has no bias to
    remove. Defaults to 0.0 = the pre-2026-08-09 uncorrected behaviour.

    forecast_weight is that term's share of the blend; None takes the
    station-agnostic default. It used to be a hardcoded 0.4, justified by
    the forecasts' known bias -- which made it a workaround for the very
    thing forecast_bias_c now fixes properly, and left the two stacking:
    at 0.4 a measured -1.66C bias moved the estimate only +0.66C. Measured
    over 52 station-days (see config.FORECAST_BLEND_WEIGHT_DEFAULT), the
    observed term alone is about half as accurate as the forecast alone,
    so the old split put most of the weight on the worse predictor. The
    exception is Singapore, where the daily max barely moves and
    persistence genuinely is informative -- which is exactly where the 0.4
    was originally tuned.
    """
    if forecast_weight is None:
        forecast_weight = config.FORECAST_BLEND_WEIGHT_DEFAULT
    valid_forecasts = [f.max_temp_c for f in forecasts if f.max_temp_c is not None]
    forecast_mean = statistics.fmean(valid_forecasts) if valid_forecasts else None
    if forecast_mean is not None and forecast_bias_c:
        forecast_mean -= forecast_bias_c

    observed_mean = statistics.fmean(o.max_temp_c for o in observations) if observations else None

    if forecast_mean is not None and observed_mean is not None:
        return round(forecast_weight * forecast_mean + (1 - forecast_weight) * observed_mean, 1)
    if observed_mean is not None:
        return round(observed_mean, 1)
    if forecast_mean is not None:
        return round(forecast_mean, 1)
    return round(long_term_normal_c, 1)


def _clamp_spread(value: float) -> float:
    """Hold any spread inside config's band -- see SPREAD_FLOOR_C."""
    return round(min(max(value, config.SPREAD_FLOOR_C), config.SPREAD_CEILING_C), 2)


def measured_error_spread(station_icao: str) -> tuple:
    """
    (std_dev_c, n_pairs) of this station's own forecast errors, or
    (None, n) when there are too few pairs to estimate it.

    Uses exactly the pairs the bias correction already measures --
    storage.forecast_error_samples(), which is lookahead-guarded (only
    forecasts fetched on or before the target date count) and measured
    against the station's own settlement source. The bias correction takes
    the MEAN of that distribution; this takes its standard deviation, which
    is precisely the width the probability step needs and which nothing was
    using.
    """
    import storage  # local: keeps calibration importable without a db

    try:
        station = config.get_station(station_icao)
        errors = storage.forecast_error_samples(station_icao, station.resolution_grade_source)
    except Exception as exc:  # noqa: BLE001 - unregistered station or storage failure
        print(f"[calibration] could not measure error spread for {station_icao}: {exc}")
        return None, 0

    if len(errors) < max(2, config.MIN_SPREAD_PAIRS):
        return None, len(errors)
    return statistics.stdev(errors), len(errors)


# Keyed on the database path, NOT just on time. backtest/engine.py and the
# test fixtures repoint config.DB_PATH at a throwaway database, and a cache
# keyed on time alone would serve a pooled spread computed from the LIVE db
# into a replay -- lookahead of the worst kind, since the live db contains
# every day the replay is pretending not to know about.
_pooled_spread_cache = {}


def pooled_error_spread(region: Optional[str] = None) -> tuple:
    """
    (std_dev_c, n_pairs) of forecast errors pooled across every registered
    station, each station's errors CENTRED on its own mean first.

    Centring per station matters: the stations have genuinely different
    biases, and pooling raw errors would fold that between-station spread
    into what is meant to be a within-station spread, inflating it.

    This is what a station without enough history of its own gets. It is a
    real measurement rather than a hardcoded guess -- but it is still not
    THIS station's number, which is why "pooled_error" is in
    config.LOW_CONFIDENCE_SPREAD_SOURCES and costs an entry the doubled
    edge bar.

    Cached: it reads every station, and recomputing it per station per
    cycle would multiply the query count by the size of the registry for a
    figure that barely moves day to day.

    `region` scopes the pool to one cohort of stations. This is not a
    refinement, it is a correctness requirement once the registry spans
    more than one climate: temperate and tropical stations have genuinely
    different error distributions, and a pool spanning both describes
    neither. It is also the path by which a newly registered region would
    otherwise change an existing region's trading behaviour without
    touching a single line of that region's code -- spread feeds
    probability feeds EV feeds entries.

    None pools every registered station, the original meaning, kept for
    callers that predate regions.
    """
    import time

    import storage  # local, as above

    now = time.time()
    # The region is part of the key, not just the db path. Without it the
    # first region to compute would serve its spread to every other one --
    # reintroducing the exact leak the filter below removes.
    key = (str(config.DB_PATH), region)
    hit = _pooled_spread_cache.get(key)
    if hit is not None and now - hit[2] < config.POOLED_SPREAD_CACHE_TTL_S:
        return hit[0], hit[1]

    centred = []
    for icao in config.STATIONS:
        if region is not None and config.region_of(icao) != region:
            continue
        try:
            station = config.get_station(icao)
            errors = storage.forecast_error_samples(icao, station.resolution_grade_source)
        except Exception:  # noqa: BLE001 -- one bad station must not sink the pool
            continue
        if len(errors) > 1:
            mean = statistics.fmean(errors)
            centred.extend(e - mean for e in errors)

    if len(centred) < max(2, config.MIN_SPREAD_PAIRS):
        # Cached too: an empty database is the common case in tests and in a
        # fresh deployment, and re-scanning every station to rediscover that
        # on each call is the expensive way to learn nothing.
        _pooled_spread_cache[key] = (None, len(centred), now)
        return None, len(centred)

    value = (statistics.fmean([e * e for e in centred])) ** 0.5
    _pooled_spread_cache[key] = (value, len(centred), now)
    return value, len(centred)


def estimate_std_dev(
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    ensemble_members: List[float] = None,
    station_icao: str = None,
    allow_measured: bool = True,
) -> tuple:
    """
    Estimate spread (std dev, in degrees C) for the probability step, and
    report WHICH tier produced it.

    Returns (std_dev_c, source), source being one of "ensemble",
    "measured_error", "pooled_error", "replay_constant",
    "fallback_default". The source
    matters as much as the number -- see
    config.LOW_CONFIDENCE_SPREAD_SOURCES, which makes an entry computed on
    a non-station-specific spread clear a doubled edge bar.

    WHY THE OLD SECOND TIER IS GONE
    --------------------------------
    The chain used to fall from ensemble spread to stdev ACROSS THE POINT
    FORECASTS, then to stdev across observed history. Measured on 50
    station-days scoring Brier against settled buckets, the forecast-
    variance tier fired for every single sample and was WORSE THAN A
    CONSTANT: Brier 0.8040 against 0.7228 for a flat 1.0C.

    It was measuring the wrong thing. Spread across two or three point
    forecasts is how much the MODELS DISAGREE, estimated from two or three
    numbers -- not how wrong the blend tends to be. It emitted 0.25C to
    4.33C against a 1.0C bucket width. Observed-history variance has the
    same defect in the other direction: it is the weather's variability,
    not the forecast's error.

    The replacement measures the error distribution directly, which scores
    0.7225 -- and 0.5448 on WSSS alone, the only station that trades.

    station_icao is optional so existing callers keep working; without it
    the measured tier cannot be reached and the estimate falls to pooled.
    """
    if ensemble_members and len(ensemble_members) > 1:
        # A real ensemble spread is the one honest physical spread available
        # -- distinct model runs of the same atmosphere. Still clamped:
        # ensembles are known to be under-dispersive at short lead times.
        return _clamp_spread(statistics.stdev(ensemble_members)), "ensemble"

    if not allow_measured:
        # THE BACKTEST PATH. Both measured tiers read the WHOLE stored error
        # record, so using either inside a replay would leak days the
        # simulated instant has not reached -- the identical leak the engine
        # already refuses for forecast_bias_c, which it pins at 0.0 for
        # exactly this reason. Doing it honestly means reconstructing the
        # spread as of each simulated instant; until that exists, a replay
        # gets the measured pooled CONSTANT.
        #
        # Reported as "replay_constant" and deliberately NOT placed in
        # config.LOW_CONFIDENCE_SPREAD_SOURCES: it is a real measured
        # number, and marking it low-confidence would make every replayed
        # entry clear a doubled edge bar that live never faces, which is a
        # live/replay divergence in the one direction the backtest exists
        # to rule out.
        return _clamp_spread(config.POOLED_SPREAD_FALLBACK_C), "replay_constant"

    if station_icao:
        measured, _ = measured_error_spread(station_icao)
        if measured is not None:
            return _clamp_spread(measured), "measured_error"

    pooled, _ = pooled_error_spread(
        region=config.region_of(station_icao) if station_icao else None,
    )
    if pooled is not None:
        return _clamp_spread(pooled), "pooled_error"

    # Nothing measured anywhere -- an empty database. Deliberately NOT
    # derived from the forecasts or observations in hand, both of which
    # were shown to be worse than a constant.
    return _clamp_spread(config.POOLED_SPREAD_FALLBACK_C), "fallback_default"


def calibrate(
    station: StationConfig,
    target_date: date,
    forecasts: List[PointForecast],
    observations: List[ObservedReading],
    ensemble_members: List[float] = None,
    forecast_bias_c: float = 0.0,
    allow_measured_spread: bool = True,
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
    forecast_weight = config.forecast_blend_weight(station.icao)
    central = blend_central_estimate(
        forecasts, observations, station.long_term_normal_max_c,
        forecast_bias_c=applied_bias, forecast_weight=forecast_weight,
    )
    # allow_measured_spread=False is the replay path -- see estimate_std_dev.
    # It is the spread-side twin of forecast_bias_c being pinned at 0.0 by
    # backtest/engine.py: both read the whole stored record, and both would
    # leak the future into a replayed day.
    spread, spread_source = estimate_std_dev(
        forecasts, observations, ensemble_members, station_icao=station.icao,
        allow_measured=allow_measured_spread,
    )

    notes = []
    if not forecasts:
        notes.append("No live model forecasts available -- fell back to observed/normal.")
    if not observations:
        notes.append("No recent observed data for this station -- calibration is forecast-only.")
    if spread_source in config.LOW_CONFIDENCE_SPREAD_SOURCES:
        notes.append(
            f"Spread is not measured for this station (source={spread_source}, "
            f"{spread:.2f}C) -- edge gate tightened accordingly."
        )
    if applied_bias:
        notes.append(
            f"Forecast term bias-corrected by {-applied_bias:+.2f}C "
            f"(measured mean forecast-minus-settled = {applied_bias:+.2f}C)."
        )
    if forecasts and observations:
        notes.append(f"Blend: {forecast_weight:.0%} forecast / {1 - forecast_weight:.0%} observed.")

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
