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

import config
from calibration import calibrate
from probability import bucket_probabilities, most_likely_bucket
from clients import openmeteo_client, climate_monitor_client, wunderground_client
from clients.official.registry import get_official_client
import storage


def gather_forecasts(station) -> list:
    """Step A: pull every available forecast source for this station, skipping failures silently."""
    forecasts = []

    ecmwf = openmeteo_client.get_ecmwf_forecast(station)
    if ecmwf:
        forecasts.append(ecmwf)

    gfs = openmeteo_client.get_gfs_forecast(station)
    if gfs:
        forecasts.append(gfs)

    official = get_official_client(station.official_client_key)
    official_forecast = official.get_24hr_forecast(station)
    if official_forecast:
        forecasts.append(official_forecast)

    for f in forecasts:
        storage.save_forecast(f)

    return forecasts


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
    target_date = target_date or config.local_today()

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

    buckets = bucket_probabilities(estimate)
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
    print(f"\nVerify against resolution source before trading: {result['resolution_source_url']}\n")
