"""
clients/wunderground_client.py

PURPOSE
-------
Represents the Polymarket RESOLUTION SOURCE (ground truth) for any
registered station. Station-agnostic: every function takes a
StationConfig and uses its wunderground_history_url property, so
adding a new station's resolution URL is just adding the right
wunderground_slug in config.py.

IMPORTANT CAVEAT (confirmed during framework testing): Wunderground's
history pages render dynamically / are frequently served from cache
when fetched by simple HTTP clients, so scraping this reliably is not
guaranteed for ANY station. Do NOT build automated trading logic that
assumes this client returns a trustworthy live value.

DEPENDENCIES
------------
requests, beautifulsoup4   (pip install requests beautifulsoup4)
models.py (local)
"""

from typing import Optional

import requests
from bs4 import BeautifulSoup

from models import StationConfig


def get_resolution_url(station: StationConfig) -> str:
    """Return the canonical resolution-source URL for manual verification."""
    return station.wunderground_history_url


def best_effort_scrape_daily_high(station: StationConfig, timeout: int = 10) -> Optional[float]:
    """
    Attempt to scrape today's daily high directly from the station's
    Wunderground history page. UNRELIABLE -- may return None even when
    the page has the data, due to client-side rendering. Treat any
    non-None result as a hint to double check manually, not as ground
    truth to trade on without human confirmation.
    """
    try:
        resp = requests.get(
            station.wunderground_history_url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Wunderground's summary table structure changes often; this is a
        # best-effort selector, expected to need maintenance, for any station.
        high_cell = soup.select_one("[aria-label*='High Temperature'] span")
        if high_cell and high_cell.text.strip():
            return float(high_cell.text.strip().replace("°", ""))
        return None
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"[wunderground_client] scrape failed for {station.icao} (expected to be fragile): {exc}")
        return None
