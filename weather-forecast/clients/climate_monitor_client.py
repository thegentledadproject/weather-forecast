"""
clients/climate_monitor_client.py

PURPOSE
-------
Provides the historical-observed baseline used by calibration.py for
any station. In the MVP this reads StationConfig.seed_observations
(manually maintained per-station in config.py) rather than scraping a
live source, since:

  - weather.gov.sg's historical-records page is JS-rendered and not
    fetchable with a simple HTTP client (confirmed during testing)
  - equivalent Malaysia sources haven't been vetted yet (WMKK ships
    with an empty seed list -- see config.py's WMKK entry)

For an MVP, correctness of the calibration logic matters more than
automating this fetch per-country. Extend this module with a real
per-country scraper/API later without touching calibration.py -- it
only depends on the ObservedReading shape.

DEPENDENCIES
------------
Standard library only.
"""

from datetime import date, timedelta
from typing import List

from models import StationConfig, ObservedReading


def load_recent_observations(station: StationConfig, days: int = 30) -> List[ObservedReading]:
    """
    Return recent ObservedReading records for a station, from its
    StationConfig.seed_observations, filtered to the requested window.
    Returns an empty list for stations with no seed data yet (e.g.
    WMKK) -- calibration.py already handles that gracefully.
    """
    cutoff = date.today() - timedelta(days=days)
    results = []
    for date_iso, temp_c in station.seed_observations:
        d = date.fromisoformat(date_iso)
        if d >= cutoff:
            results.append(
                ObservedReading(
                    station_icao=station.icao,
                    target_date=d,
                    max_temp_c=temp_c,
                    source="seed_data",
                )
            )
    return results


def long_term_normal_max_c(station: StationConfig) -> float:
    """Climatological reference daily max for the station, from its StationConfig."""
    return station.long_term_normal_max_c
