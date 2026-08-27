"""
pipeline.py

PURPOSE
-------
Orchestrates the full framework flow end-to-end for one station on a
target date (defaults to today). This is the module that made the
codebase generalize cleanly: it never mentions "Changi" or "WSSS"
directly -- it takes a station_icao, looks up the StationConfig via
config.get_station(), and looks up the right official-source adapter
via clients.official.registry.get_official_client(). Adding WMKK (or
any future station) means this file needs zero changes.

Flow:
  1. Pull whatever forecasts are available (Tier 1 Open-Meteo, Tier 2
     the station's registered official client) -- Step A
  2. Load recent observed history for calibration -- Step D input
  3. Run calibration.py -> bias-corrected central estimate -- Step B/D
  4. Run probability.py -> bucket probabilities -- Step C
  5. Pull the official client's same-day signal -- Step E
  6. Persist everything via storage.py, keyed by station
  7. Print a human-readable summary table

DEPENDENCIES
------------
config.py, models.py, calibration.py, probability.py, storage.py (local)
clients/* (local)
"""

from datetime import date, datetime, timezone

import bucket_axis
import config
from calibration import calibrate
from probability import bucket_probabilities, most_likely_bucket
from clients import openmeteo_client, climate_monitor_client, wunderground_client
from clients.official.registry import get_official_client
import storage


def gather_forecasts(station) -> list:
    """
    Step A: pull every available forecast source for this station, skipping
    failures silently.

    STORES everything fetched, RETURNS only what this station blends TODAY.
    The two now differ in two ways:

      * config.FORECAST_SOURCES_EXCLUDED_BY_STATION names a source that is
        wrong at this station (RKSI/GFS runs 3-7C cold) -- collection
        continues so the exclusion stays re-checkable against accruing
        data, while the caller's central estimate stops reading it.

      * Open-Meteo returns three days per request and all three are now
        stored (see openmeteo_client._fetch_daily_max_series). ONLY
        local_today's rows are returned.

    THAT SECOND FILTER IS LOAD-BEARING. This return value goes straight
    into calibration.blend_central_estimate(), which averages whatever
    list it is handed and has no target-date filter of its own. Letting a
    day-ahead row through would blend tomorrow's weather into today's
    central estimate, and therefore into today's bucket probabilities and
    today's orders. tests/test_forecast_lead_window.py pins it.
    """
    forecasts = []
    forecasts.extend(openmeteo_client.get_ecmwf_forecast_series(station))
    forecasts.extend(openmeteo_client.get_gfs_forecast_series(station))

    official = get_official_client(station.official_client_key)
    official_forecast = official.get_24hr_forecast(station)
    if official_forecast:
        forecasts.append(official_forecast)

    for f in forecasts:
        storage.save_forecast(f)

    today = config.local_today(station)
    todays = [f for f in forecasts if f.target_date == today]
    return config.blendable_forecasts(station.icao, todays)


def gather_same_day_signal(station) -> str:
    """Step E: delegate to whichever official client this station is registered with."""
    official = get_official_client(station.official_client_key)
    return official.get_same_day_signal(station)


def gather_observations(station, target_date: date) -> list:
    """
    The observation set calibration should blend for one station/date:
    climate-monitor seeds plus everything stored (which now includes the
    settlement-grade METAR daily maxima metar_client ingests), deduped so
    two sources reporting the same day cannot double-count in the 60/40
    blend -- settlement-grade rows win per config.observation_source_rank.

    Shared by pipeline.run() and scheduler._run_full_cycle() so the
    live trading path and manual pipeline runs calibrate on the SAME
    inputs -- the trading path previously used seeds only, leaving it
    blind to every reading the system had actually collected.
    """
    observations = climate_monitor_client.load_recent_observations(station, days=30)
    observations += storage.load_observations_since(station.icao, target_date.replace(day=1))
    return storage.dedupe_observations(observations)


def run(station_icao: str = "WSSS", target_date: date = None) -> dict:
    """Run the full pipeline for one station/date. Returns a summary dict."""
    station = config.get_station(station_icao)
    # This station's own market day -- the registry spans UTC+5 to UTC+9,
    # so "today" is a per-station fact, not a process-wide one.
    target_date = target_date or config.local_today(station)

    forecasts = gather_forecasts(station)
    ensemble = openmeteo_client.get_ensemble_spread(station)
    observations = gather_observations(station, target_date)

    estimate = calibrate(
        station=station,
        target_date=target_date,
        forecasts=forecasts,
        observations=observations,
        ensemble_members=ensemble,
    )

    # The STATION's cross-check bounds and edge mode. Unlike the trading
    # path (ev_engine.run_for_station_with_map), this function has no live
    # token map to derive bounds from -- and that is fine here, because
    # pipeline.run() reports a forecast rather than pricing a book. The
    # bounds only decide which buckets the printed table spans; the edge
    # mode decides what each bucket MEANS, and that is a property of the
    # settlement source (0.1°C floor semantics for Hong Kong), not of the
    # event, so it must be passed even in this offline context.
    buckets = bucket_probabilities(
        estimate,
        station.bucket_min_c,
        station.bucket_max_c,
        axis=bucket_axis.for_station(station),
    )
    top_bucket = most_likely_bucket(buckets)
    same_day_signal = gather_same_day_signal(station)

    return {
        "station_icao": station.icao,
        "station_name": station.display_name,
        "target_date": target_date.isoformat(),
        "central_estimate_c": estimate.central_estimate_c,
        "std_dev_c": estimate.std_dev_c,
        "monsoon_phase": estimate.monsoon_phase,
        "inputs_used": estimate.inputs_used,
        "notes": estimate.notes,
        "top_bucket_c": top_bucket.bucket_c,
        "top_bucket_probability": top_bucket.probability,
        "all_buckets": buckets,
        "same_day_signal": same_day_signal,
        "resolution_source_url": wunderground_client.get_resolution_url(station),
        # Whether that Wunderground page is actually what the market
        # settles on. For Hong Kong it is NOT -- VHHH resolves on the HK
        # Observatory's own climate extract, and the airport page it links
        # reads systematically cooler. Printing a URL under the words
        # "verify against resolution source" while that URL is a proxy is
        # how someone confirms a trade against the wrong number.
        "resolution_source_is_settlement": station.resolution_grade_source == "metar_daily_max",
        "resolution_grade_source": station.resolution_grade_source,
        "run_at": datetime.now(timezone.utc).isoformat(),
    }


def print_summary(result: dict) -> None:
    """Human-readable console output."""
    print(f"\n=== {result['station_name']} ({result['station_icao']}) Forecast — {result['target_date']} ===")
    print(f"Monsoon phase:        {result['monsoon_phase']}")
    print(f"Inputs used:          {result['inputs_used'] or 'none (fell back to normal/observed)'}")
    print(f"Central estimate:     {result['central_estimate_c']}°C  (± {result['std_dev_c']}°C)")
    print(f"Most likely bucket:   {result['top_bucket_c']}°C  (p={result['top_bucket_probability']})")
    print(f"Same-day signal:      {result['same_day_signal']}")
    if result["notes"]:
        print(f"Notes:                {result['notes']}")
    print("\nBucket probabilities:")
    for b in result["all_buckets"]:
        bar = "#" * int(b.probability * 50)
        print(f"  {b.bucket_c:>3}°C  {b.probability:>6.2%}  {bar}")
    # .get() with a True default so an older/hand-built result dict still
    # prints the original line rather than crashing on a missing key.
    if result.get("resolution_source_is_settlement", True):
        print(f"\nVerify against resolution source before trading: {result['resolution_source_url']}\n")
    else:
        print(
            f"\nWunderground reference page (NOT the settlement source): "
            f"{result['resolution_source_url']}"
        )
        print(
            f"This market settles on '{result.get('resolution_grade_source', 'a different source')}', "
            f"not on the Wunderground page above -- that page is a nearby-station proxy and can differ "
            f"by whole buckets. Verify against the actual settlement record before trading.\n"
        )
