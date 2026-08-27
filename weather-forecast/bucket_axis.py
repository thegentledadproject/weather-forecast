"""
bucket_axis.py

PURPOSE
-------
What a bucket KEY means, for one market.

THE GOVERNING INVARIANT
-----------------------
    Every temperature in this codebase is Celsius.
    Only the bucket KEY and its BOUNDS live in the market's own unit.

Forecasts, std_dev, observations, midpoints, bias -- all Celsius, always.
This module is the ONE boundary where the market's unit is converted, and
every function here that returns a temperature returns Celsius.

WHY THIS EXISTS
---------------
Every market registered before 2026-08 was Celsius with whole-degree
buckets, and that assumption was spelled into an identifier (`bucket_c`).
Eleven of the fifteen American cities list Fahrenheit in two-degree
buckets. This is the same shape of latent assumption as
StationConfig.utc_offset_hours being a static int, and it is repaired the
same way: leave the field carrying the market's own datum alone, add a
descriptor carrying the general truth, route every semantic use through it.

DEPENDENCIES
------------
math, dataclasses, typing (standard library ONLY -- this module is
imported by probability.py, market_discovery.py, bucket_bias.py and
backtest/resolution.py, so any project import would create a cycle).
"""

import math
from dataclasses import dataclass
from typing import List, Tuple

UNIT_C = "C"
UNIT_F = "F"
_UNITS = (UNIT_C, UNIT_F)
_EDGE_MODES = ("half_up", "floor")


@dataclass(frozen=True)
class BucketAxis:
    """
    unit      -- the unit of the market's bucket LABELS, and therefore of
                 every bucket key and of bucket_min_c/bucket_max_c. NOT the
                 unit of any temperature.
    step      -- width of one listed bucket, in `unit` degrees.
    edge_mode -- how a raw reading maps onto a listed bucket:
                 "half_up" the source reports whole degrees, so the bucket
                           wins for any reading that rounds to a degree
                           inside it;
                 "floor"   the source reports 0.1 precision and the market
                           resolves to the range CONTAINING the reading.
    """

    unit: str = UNIT_C
    step: int = 1
    edge_mode: str = "half_up"

    def __post_init__(self):
        if self.unit not in _UNITS:
            raise ValueError(
                f"unknown bucket unit {self.unit!r} -- expected one of {_UNITS}. "
                f"Refusing to guess: a wrong unit mis-prices every bucket."
            )
        if not isinstance(self.step, int) or self.step < 1:
            raise ValueError(
                f"bucket step must be a positive int, got {self.step!r}."
            )
        if self.edge_mode not in _EDGE_MODES:
            raise ValueError(
                f"unknown bucket edge_mode {self.edge_mode!r} -- expected one of "
                f"{_EDGE_MODES} (see models.StationConfig.bucket_edge_mode)."
            )

    # --- unit conversion -------------------------------------------------

    def to_axis(self, temp_c: float) -> float:
        """Celsius -> this axis's unit."""
        if self.unit == UNIT_C:
            return temp_c
        return temp_c * 9 / 5 + 32

    def to_celsius(self, axis_value: float) -> float:
        """This axis's unit -> Celsius."""
        if self.unit == UNIT_C:
            return axis_value
        return (axis_value - 32) * 5 / 9

    def width_c(self) -> float:
        """How wide one listed bucket is, in DEGREES CELSIUS."""
        if self.unit == UNIT_C:
            return float(self.step)
        return self.step * 5 / 9

    @property
    def is_default(self) -> bool:
        """True for the Celsius whole-degree axis every pre-2026-08 market uses."""
        return self.unit == UNIT_C and self.step == 1

    # --- key <-> temperature ---------------------------------------------

    def interval_c(self, key: int) -> Tuple[float, float]:
        """
        The temperature interval bucket `key` covers, IN CELSIUS.

        A key is the bucket's LOWER EDGE in the axis unit, so:
            half_up  [key - 0.5, key - 0.5 + step)
            floor    [key,       key + step)
        Both reduce to probability.py's historical formulas at step == 1.
        """
        lower_axis = key - 0.5 if self.edge_mode == "half_up" else float(key)
        upper_axis = lower_axis + self.step
        return self.to_celsius(lower_axis), self.to_celsius(upper_axis)

    def key_for_temp_c(self, t_c: float, lo: int, hi: int) -> int:
        """
        The bucket key a Celsius reading falls in, clamped into [lo, hi]
        because the edge buckets are catch-alls.
        """
        if self.is_default:
            # SHORT-CIRCUIT, deliberately literal. The general branch below is
            # algebraically identical here, but keeping the original
            # expressions makes "unchanged for every existing station" a
            # property of the code rather than of an algebra argument.
            bucket = (
                math.floor(t_c)
                if self.edge_mode == "floor"
                else math.floor(t_c + 0.5)
            )
            return max(lo, min(hi, bucket))

        axis_value = self.to_axis(t_c)
        # The settlement source displays a whole degree in the axis unit.
        # floor(x + 0.5), never round(): round() is banker's rounding and
        # disagrees on exactly the half-degree values the bucket edges sit on.
        displayed = (
            math.floor(axis_value)
            if self.edge_mode == "floor"
            else math.floor(axis_value + 0.5)
        )
        key = lo + self.step * math.floor((displayed - lo) / self.step)
        return max(lo, min(hi, key))

    def keys(self, lo: int, hi: int) -> List[int]:
        """Every listed bucket key from lo to hi inclusive, on this axis's grid."""
        return list(range(lo, hi + 1, self.step))

    def label(self, key: int, lo: int, hi: int) -> str:
        """
        The label the market itself prints for this bucket.

        REQUIRED at every human-facing site. A key is the bucket's lower
        edge, so on a step-2 axis the bottom catch-all's key (68) is a
        number the market never prints ("69F or below"). Rendering the raw
        key with a hardcoded degree suffix is how a human ends up told to
        buy the wrong contract.
        """
        suffix = "°C" if self.unit == UNIT_C else "°F"
        if key <= lo:
            return f"{key + self.step - 1}{suffix} or below"
        if key >= hi:
            return f"{key}{suffix} or higher"
        if self.step == 1:
            return f"{key}{suffix}"
        return f"{key}-{key + self.step - 1}{suffix}"


AXIS_C1 = BucketAxis()
"""The axis every market registered before 2026-08 uses. The default everywhere."""


def for_station(station) -> BucketAxis:
    """
    The axis for a StationConfig. getattr with defaults so this works on
    any station-shaped object, including test doubles predating the fields.
    """
    return BucketAxis(
        unit=getattr(station, "bucket_unit", UNIT_C),
        step=getattr(station, "bucket_step", 1),
        edge_mode=getattr(station, "bucket_edge_mode", "half_up"),
    )
