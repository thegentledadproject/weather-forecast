"""
backtest/as_of.py

GAP 3: run production's own decision readers POINT-IN-TIME. One call pins
both seams -- storage's as-of views (storage.set_as_of) and the one clock
(config.pin_now_utc) -- so entry_manager.forecast_bias_stats,
calibration.estimate_std_dev / error_width_ratio,
probability_calibration.calibration_for and config.station_maturity answer
as they would have at `ts`. Both seams refuse in a writable process.
"""

from datetime import datetime
from typing import Optional

import calibration
import config
import entry_manager
import probability_calibration
import storage


def clear_caches() -> None:
    """Every production cache that memoises a storage read. Production keys
    them per day (or per process); a replay that moves the clock must drop
    them or it serves one instant's answer at another."""
    config._maturity_cache.clear()
    calibration._pooled_spread_cache.clear()
    probability_calibration.clear_cache()
    entry_manager._error_width_cache.clear()


def pin(ts: Optional[datetime], clear: bool = True) -> None:
    """Pin reads and the clock to aware UTC `ts`; None releases both."""
    storage.set_as_of(ts)
    config.pin_now_utc(ts)
    if ts is not None:
        # Fail LOUD here: the production readers swallow storage errors and
        # degrade to "unmeasured", so a broken view would look like a result.
        storage._connect().close()
    if clear:
        clear_caches()


def release() -> None:
    pin(None)
