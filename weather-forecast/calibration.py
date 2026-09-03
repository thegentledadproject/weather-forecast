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


def bias_stats_weighted(
    dated_errors,
    as_of,
    half_life_days: float,
) -> tuple:
    """
    bias_stats() with exponential recency weighting: the same
    (bias_c, n, standard_error_c) triple, over the same signed
    (forecast - settled truth) errors, with a sample from `half_life_days`
    ago counting half as much as today's.

    dated_errors is [(target_date, error_c), ...]; `as_of` is the date ages
    are measured from.

    WHY THIS IS A SEPARATE FUNCTION AND NOT A FLAG ON bias_stats().
    bias_stats() is documented as shared ON PURPOSE so that live and the
    backtest cannot compute the statistic differently. Adding a mode to it
    would put a knob inside the one thing that is supposed to be identical
    on both sides, and every replay's meaning would then depend on how the
    knob happened to be set. This is a different estimator with a different
    name; the backtest keeps calling the unweighted one and its history
    keeps meaning what it meant.

    WHY DECAY RATHER THAN A ROLLING WINDOW. A hard window drops samples,
    and dropping samples can push a station under
    MIN_BIAS_PAIRS_BEFORE_ENTRY -- so a change meant to keep the bias
    HONEST would instead stop stations trading. Decay keeps every sample
    and merely discounts it, so the sample count degrades smoothly instead
    of falling off a cliff.

    n IS THE EFFECTIVE SAMPLE SIZE, NOT THE ROW COUNT -- Kish's
    (sum w)^2 / sum(w^2), rounded down. That is the honest answer to the
    question the gate actually asks ("is this measured well enough?"): 20
    rows of which 15 are heavily discounted do not carry 20 rows of
    information, and reporting 20 there would let a stale sample hold a
    station's certification open. It is always <= the row count, and equals
    it exactly when every weight is equal.

    Returns (None, 0, None) for an empty sample, and (bias, n, None) when
    n_eff < 2 -- matching bias_stats() so callers need no new branch.
    """
    if not dated_errors:
        return None, 0, None

    weights = []
    errors = []
    for target_date, err in dated_errors:
        age = (as_of - target_date).days
        if age < 0:
            # A sample from the future is a bug upstream, not something to
            # weight up. Treat it as today rather than letting 0.5**negative
            # hand it more influence than any real observation.
            age = 0
        weights.append(0.5 ** (age / half_life_days))
        errors.append(float(err))

    total_w = sum(weights)
    if total_w <= 0:
        return None, 0, None

    mean = sum(w * e for w, e in zip(weights, errors)) / total_w
    # Floor, not round: a fractional effective size should read as the
    # smaller integer, because overstating it is the direction that lets a
    # thin sample hold a station's certification open. The epsilon is for
    # floating point only -- equal weights must give the row count back
    # exactly, and (3w)^2/(3w^2) lands on 2.9999999999999996 without it.
    n_eff = int((total_w ** 2) / sum(w * w for w in weights) + 1e-9)
    if n_eff < 2:
        return round(mean, 3), n_eff, None

    # Weighted variance about the weighted mean, then the standard error at
    # the EFFECTIVE size -- the same sd/sqrt(n) shape bias_stats() uses.
    var = sum(w * (e - mean) ** 2 for w, e in zip(weights, errors)) / total_w
    stderr = math.sqrt(var) / math.sqrt(n_eff)
    return round(mean, 3), n_eff, round(stderr, 3)


def observed_mean_weighted(dated_observations, as_of, half_life_days):
    """
    The OBSERVED term of the central estimate, with exponential recency
    weighting: a reading `half_life_days` old counts half as much as today's.

    dated_observations is [(target_date, max_temp_c), ...]; `as_of` is the
    date ages are measured from. Returns None for an empty sample, so
    blend_central_estimate()'s existing "no observed term" fallbacks fire
    unchanged.

    half_life_days=None means NO DECAY and returns statistics.fmean exactly.
    That is the shipped default and the reason this can land without
    changing a single stored estimate.

    WHY THIS EXISTS. The observed term carries 60% of the blend for WSSS
    (FORECAST_BLEND_WEIGHT_BY_STATION) and was a plain unweighted mean over
    a ~30-day window, so it could not track a regime: WSSS settled 33.0 on
    each of 2026-08-19..25 while the term sat at 32.538, and the book bought
    32:YES and 33:NO against that run for -45.2% over nine positions.
    blend_central_estimate's own docstring justifies WSSS's observed weight
    on the grounds that "persistence genuinely is informative" -- true, but
    a month-long unweighted mean is climatology, not persistence.

    DECAY RATHER THAN A ROLLING WINDOW, for the reason bias_stats_weighted()
    gives: a hard window DROPS samples, and dropping observations can push a
    station under MIN_RESOLUTION_OBS_BEFORE_ENTRY, so a change meant to make
    the estimate more honest would instead stop stations trading. Decay
    keeps every sample and merely discounts it.

    A SEPARATE FUNCTION rather than a flag on the existing arithmetic, again
    mirroring bias_stats_weighted(): the unweighted mean keeps meaning what
    it has always meant, and no stored estimate's interpretation depends on
    how a knob happened to be set when it was computed.

    Unlike bias_stats_weighted() this returns a bare float, not a
    (value, n, stderr) triple. Nothing gates on the observed term's
    precision today, and inventing such a gate here would be a second change
    smuggled in beside the first. Kish's effective-n is the precedent to
    copy if one is ever wanted.
    """
    if not dated_observations:
        return None
    if half_life_days is None:
        return statistics.fmean(float(t) for _, t in dated_observations)

    weights, temps = [], []
    for target_date, temp in dated_observations:
        age = (as_of - target_date).days
        if age < 0:
            # 0.5 ** negative is > 1. A future-dated reading is an upstream
            # bug, not the most informative sample in the set.
            age = 0
        weights.append(0.5 ** (age / half_life_days))
        temps.append(float(temp))

    total_w = sum(weights)
    if total_w <= 0:
        return None
    return sum(w * t for w, t in zip(weights, temps)) / total_w


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

    # Recency-weighted; config.OBSERVED_HALF_LIFE_DAYS is None by default,
    # which returns statistics.fmean exactly. as_of is the newest reading
    # in the sample rather than a wall clock, so this stays PURE and a
    # replay weights by the ages that were real at the simulated instant.
    dated = [(o.target_date, o.max_temp_c) for o in observations]
    observed_mean = observed_mean_weighted(
        dated, max((d for d, _ in dated), default=None), config.OBSERVED_HALF_LIFE_DAYS
    ) if observations else None

    if forecast_mean is not None and observed_mean is not None:
        return round(forecast_weight * forecast_mean + (1 - forecast_weight) * observed_mean, 1)
    if observed_mean is not None:
        return round(observed_mean, 1)
    if forecast_mean is not None:
        return round(forecast_mean, 1)
    return round(long_term_normal_c, 1)


def _clamp_spread(value: float, station_icao: str = None) -> float:
    """
    Hold a spread inside its REGION's band -- see SPREAD_FLOOR_C and
    config.REGION_SPREAD_CEILING_C.

    The floor is global: a spread below it is the dangerous direction
    everywhere. The ceiling is regional, and a region whose ceiling is None
    is not clamped at all. station_icao defaults to None for
    station-agnostic callers, which keeps the legacy global ceiling.
    """
    floored = max(value, config.SPREAD_FLOOR_C)
    if station_icao is None:
        return round(min(floored, config.SPREAD_CEILING_C), 2)
    ceiling = config.region_spread_ceiling_c(station_icao)
    if ceiling is None:
        return round(floored, 2)
    return round(min(floored, ceiling), 2)


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


def _dated_error_samples(station_icao: str):
    """
    [(target_date, error_c), ...] oldest first, or [] if it cannot be read.

    A one-line seam over storage so corrected_error_rmse() below can be
    tested on a known series without a database, and so both readers of this
    record go through the same lookahead-guarded query.
    """
    import storage  # local: keeps calibration importable without a db

    try:
        station = config.get_station(station_icao)
        return sorted(storage.forecast_error_samples_dated(
            station_icao, station.resolution_grade_source))
    except Exception as exc:  # noqa: BLE001 - unregistered station or storage failure
        print(f"[calibration] could not read error samples for {station_icao}: {exc}")
        return []


def corrected_error_rmse(station_icao: str) -> tuple:
    """
    (rmse_c, n_scored) -- how wrong this station's CORRECTED central
    estimate actually is, or (None, n_scored) when too few days have been
    scored to say. Feeds config.MAX_ERROR_RMSE_PER_BUCKET through
    entry_manager.collection_only_reason().

    THIS IS NOT measured_error_spread(). That one takes the standard
    deviation about the sample's own mean, i.e. what would be left if the
    bias correction were perfect and instantaneous. This one replays the
    correction the entry path would REALLY have had on each day -- weighted
    by config.BIAS_HALF_LIFE_DAYS over strictly earlier days only -- and
    measures what that leaves. The gap between the two is the correction's
    own lag, and a station whose bias drifts pays for it here. On
    2026-09-03 the two differed by up to 0.14C (ZGSZ 1.07 -> 1.21).

    NO DAY IS IN ITS OWN CORRECTION. Without that the number is scored
    against a bias that saw the answer, which flatters every station and
    flatters a drifting one most -- the same leak calibration.estimate_std_dev
    refuses for the backtest with allow_measured=False.

    The first config.MIN_BIAS_PAIRS_BEFORE_ENTRY days are SKIPPED rather
    than scored on a thin correction. They are the days a station is gated
    off anyway, and scoring them would charge the gate for a warm-up period
    no live entry ever traded through.

    Returns (None, n) below config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE.
    "Unknown" and "fine" are different answers, and the caller must not be
    able to confuse a new station with a resolvable one.
    """
    dated = _dated_error_samples(station_icao)
    warmup = max(1, config.MIN_BIAS_PAIRS_BEFORE_ENTRY)

    residuals = []
    for i, (target_date, error_c) in enumerate(dated):
        if i < warmup:
            continue
        bias, _, _ = bias_stats_weighted(
            dated[:i], target_date, config.BIAS_HALF_LIFE_DAYS)
        if bias is None:
            continue
        residuals.append(float(error_c) - bias)

    if len(residuals) < config.MIN_PAIRS_BEFORE_ERROR_WIDTH_GATE:
        return None, len(residuals)
    return math.sqrt(statistics.fmean(r * r for r in residuals)), len(residuals)


def error_width_ratio(station_icao: str):
    """
    This station's corrected error RMSE as a MULTIPLE of its own bucket
    width, or None when it has not been measured on enough days.

    The ratio rather than the raw RMSE is what the gate compares, because
    1.2C of error means something different on a 1C Asian bucket than on a
    2F (1.11C) American one -- see config.bucket_step_c().
    """
    rmse, _ = corrected_error_rmse(station_icao)
    if rmse is None:
        return None
    try:
        step = config.bucket_step_c(station_icao)
    except Exception as exc:  # noqa: BLE001 - unregistered station
        print(f"[calibration] could not read {station_icao}'s bucket width: {exc}")
        return None
    if step <= 0:
        return None
    return rmse / step


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
    the measured tier cannot be reached and the estimate falls to the
    ensemble, then to pooled.

    TIER ORDER (changed 2026-08-29): replay constant, measured error,
    ensemble, pooled error, flat constant. The ensemble sat at the top until
    then, which made the measured tier unreachable live -- see the comment
    on that branch for the Brier comparison that moved it.
    """
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
        return _clamp_spread(config.POOLED_SPREAD_FALLBACK_C, station_icao), "replay_constant"

    if station_icao:
        measured, _ = measured_error_spread(station_icao)
        if measured is not None:
            return _clamp_spread(measured, station_icao), "measured_error"

    if ensemble_members and len(ensemble_members) > 1:
        # A real ensemble spread is the one honest physical spread available
        # -- distinct model runs of the same atmosphere. Still clamped:
        # ensembles are known to be under-dispersive at short lead times.
        #
        # BELOW the measured tier since 2026-08-29, having been above it
        # since this chain was written. The ordering was never measured, and
        # it was not a close call in practice: the ECMWF fetch returns 51
        # members for every station on every cycle, so this branch always
        # fired and measured_error_spread() had never once run on the live
        # path.
        #
        # spread_tier_brier.py scored the two over 248 paired settled
        # station-days -- every day and every listed bucket, selected on
        # nothing. The measured tier wins by 0.0352 mean Brier (t = -3.15),
        # it wins at 7 of the 11 stations that have enough pairs to compute
        # it, and dropping any single station leaves the gap between -0.027
        # and -0.040, so it is not one station's result.
        #
        # WHERE IT LOSES, IT LOSES SMALL: RKPK +0.013, ZSPD +0.004,
        # WMKK +0.001, WSSS 0.000. Where it wins it wins big: RKSI -0.118,
        # RPLL -0.084, RJTT -0.052. That asymmetry is the argument for the
        # order rather than a per-station toggle.
        #
        # WHAT IS STILL UNTESTED, and what would justify moving this back
        # above: whether the ensemble's DAY-TO-DAY movement carries
        # information its typical value does not. It cannot be tested yet --
        # the value was fetched and discarded on every cycle until
        # pipeline.ensemble_spread_for() started recording it, so the
        # comparison above had to score this tier at one standing width per
        # station. Re-run spread_tier_brier.py once that history exists.
        #
        # Note also that SPREAD_FLOOR_C (0.70) clamps UP most of what this
        # branch produces -- raw ensemble dispersion measured across the
        # registry on 2026-08-29 ran 0.24C to 1.20C, under the floor at 24
        # of 35 stations. For those, this tier and the floor are the same
        # number, which is why the comparison also reports a floor row.
        return _clamp_spread(statistics.stdev(ensemble_members), station_icao), "ensemble"

    pooled, _ = pooled_error_spread(
        region=config.region_of(station_icao) if station_icao else None,
    )
    if pooled is not None:
        return _clamp_spread(pooled, station_icao), "pooled_error"

    # Nothing measured anywhere -- an empty database. Deliberately NOT
    # derived from the forecasts or observations in hand, both of which
    # were shown to be worse than a constant.
    return _clamp_spread(config.POOLED_SPREAD_FALLBACK_C, station_icao), "fallback_default"


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
