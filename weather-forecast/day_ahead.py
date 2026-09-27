"""
Day-ahead recording: what the model and the book said about TOMORROW's
market, the evening before. Collection only -- nothing here trades.

Question it exists to answer (2026-09-28): the bot has never looked at a
market before its own local day, so "is the model closer to the truth than
the ask a day out?" had no data. This records one row per bucket and side,
once per station per evening, into its OWN table (ev_snapshots_day_ahead).

KEPT OUT OF THE TRADING PATH ON PURPOSE:
  * its own table -- ev_snapshots feeds the shrink fit, lock_score and the
    first-sighting analyses, and a row a day early would become every
    contract's "first" snapshot;
  * no calibration map, no shrink -- both cache per target day, so calling
    them for tomorrow tonight would freeze tomorrow's trading fit before
    today's settlements land. model_prob here is the raw model;
  * no ensemble -- the ensemble fetch is today-only, and recording it under
    tomorrow would put today's spread in tomorrow's ensemble_spread row;
  * no price_store capture, no contract check, no dashboard JSON.
central_c and std_dev_c are stored so a later analysis can re-price with a
day-ahead width measured from the (already stored) day-ahead forecasts.
"""

from datetime import datetime, timedelta, timezone

import bucket_axis
import config
import ev_engine
import market_discovery
import pipeline
import storage
from calibration import calibrate
from probability import bucket_probabilities

# {icao: target_date already recorded}. In-process only: a restart after
# the hour records again, which just adds one more timestamped cycle.
_recorded = {}


def due(station_icao: str) -> bool:
    """True once per station per local evening, from DAY_AHEAD_RECORD_HOUR_LOCAL."""
    hour = config.DAY_AHEAD_RECORD_HOUR_LOCAL
    if hour is None:
        return False
    station = config.get_station(station_icao)
    offset = config.current_utc_offset_hours(station)
    local_hour = (datetime.now(timezone.utc) + timedelta(hours=offset)).hour
    tomorrow = config.local_today(station) + timedelta(days=1)
    return local_hour >= hour and _recorded.get(station_icao) != tomorrow


def record(station_icao: str) -> int:
    """Record tomorrow's book + raw model for one station. Returns rows written."""
    station = config.get_station(station_icao)
    tomorrow = config.local_today(station) + timedelta(days=1)
    _recorded[station_icao] = tomorrow  # set first: a failure is not retried every cycle

    import entry_manager  # lazy, as scheduler does: heavy import chain

    bias = entry_manager.forecast_bias_stats(station_icao)[0] or 0.0
    forecasts = pipeline.gather_forecasts(station, target_date=tomorrow)
    if not forecasts:
        print(f"[day_ahead] {station_icao}: no forecast for {tomorrow} -- nothing recorded.")
        return 0
    estimate = calibrate(
        station=station, target_date=tomorrow, forecasts=forecasts,
        observations=pipeline.gather_observations(station, tomorrow),
        ensemble_members=None, forecast_bias_c=bias,
    )

    axis = bucket_axis.for_station(station)
    token_map = market_discovery.discover_token_map(
        station, tomorrow, station.bucket_min_c, station.bucket_max_c
    )
    bounds = market_discovery.derive_bucket_bounds(token_map, step=axis.step) if token_map else None
    if bounds is None:
        print(f"[day_ahead] {station_icao}: no complete market listed for {tomorrow} -- nothing recorded.")
        return 0

    model_probs = {
        b.bucket_c: b.probability
        for b in bucket_probabilities(estimate, bounds[0], bounds[1], axis=axis)
    }
    results = ev_engine.compute_ev_table(
        estimate, token_map, quotes=ev_engine.fetch_market_quotes(token_map),
        model_probs=model_probs,
    )
    storage.save_day_ahead_rows(
        station_icao, tomorrow, datetime.now(timezone.utc).isoformat(), results,
        central_c=estimate.central_estimate_c, std_dev_c=estimate.std_dev_c,
        config_sha=ev_engine._config_fingerprint(),
    )
    print(f"[day_ahead] {station_icao}: recorded {len(results)} row(s) for {tomorrow}.")
    return len(results)
