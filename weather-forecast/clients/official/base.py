"""
clients/official/base.py

PURPOSE
-------
Defines the interface every national-source adapter implements. Kept
deliberately small: three methods covering what pipeline.py actually
needs (today's official forecast, a same-day nowcast signal, and a
live station reading). New adapters that can't support one of these
yet (e.g. MET Malaysia's nowcast) should return None rather than
raise -- pipeline.py already handles missing data by falling back to
other sources.

DEPENDENCIES
------------
Standard library only (abc, typing).
"""

from abc import ABC, abstractmethod
from typing import Optional

from models import StationConfig, PointForecast


class OfficialClient(ABC):
    """Interface for a national meteorological service's data access."""

    @abstractmethod
    def get_24hr_forecast(self, station: StationConfig) -> Optional[PointForecast]:
        """Return today's official max-temperature forecast for the station, or None."""
        raise NotImplementedError

    @abstractmethod
    def get_same_day_signal(self, station: StationConfig) -> str:
        """
        Return a short human-readable same-day nowcast/area signal for the
        station (framework Step E). Return a clear "not available" string
        rather than raising if the source doesn't support this yet.
        """
        raise NotImplementedError

    @abstractmethod
    def get_live_reading(self, station: StationConfig) -> Optional[float]:
        """Return the most recent live temperature reading at/near the station, or None."""
        raise NotImplementedError
