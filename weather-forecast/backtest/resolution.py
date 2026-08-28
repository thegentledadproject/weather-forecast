"""
backtest/resolution.py

PURPOSE
-------
How a simulated day settles: which bucket an observed max temperature
lands in, when that observation could first have been known, and what
a held position is therefore worth at resolution.

THREE SMALL FUNCTIONS, THREE REAL TRAPS
---------------------------------------
1. bucket_for_temp() must NOT use round(). Python's round() is
   banker's rounding: round(30.5) == 30 and round(31.5) == 32, so
   exactly the half-degree values that sit on bucket edges would land
   in different buckets depending on parity. probability.py assigns
   each bucket the range [b - 0.5, b + 0.5), i.e. half-UP, so a
   simulator using round() would score some days against a bucket the
   probability model never assigned that mass to. math.floor(t + 0.5)
   is half-up for the values in range here and matches probability.py
   exactly.

2. observation_visible() exists because a daily maximum is not known
   until the day is over and the official source has published it.
   Settling a position using an observation the simulator could not
   have had yet is look-ahead bias in its purest form -- the outcome
   leaking backwards into the decision.

3. resolution_exit_price() pays par or nothing, exactly as
   position_manager._close_as_resolved() does, rather than recording
   whatever near-1.00/near-0.00 quote happened to be on the book.

DEPENDENCIES
------------
math, datetime (standard library)
config.py (local)
backtest/settings.py (local)
"""

import math
from datetime import date, datetime, timedelta

import bucket_axis
import config
from bucket_axis import BucketAxis

from backtest import settings


def bucket_for_temp(
    t: float,
    bucket_min: int = None,
    bucket_max: int = None,
    edge_mode: str = "half_up",
    *,
    axis: BucketAxis = None,
    station=None,
) -> int:
    """
    The resolution bucket a max temperature of `t` degrees C falls in,
    clamped into [bucket_min, bucket_max].

    bucket_min/bucket_max default to None, which falls back to the legacy
    station-agnostic globals config.BUCKET_MIN_C/BUCKET_MAX_C -- so calling
    this with no new args reproduces the exact pre-multi-station behavior.
    Real multi-station callers (backtest/engine.py's resolution sweep,
    backtest/report.py's Brier scoring) must pass the station's own
    bucket_min_c/bucket_max_c instead: WSSS backtests would otherwise
    settle on the 25/35 clamp while the live book trades 27/37.

    edge_mode picks how the raw reading maps onto a whole-degree bucket,
    matching models.StationConfig.bucket_edge_mode:
      "half_up" (default) -- source reports whole degrees C (METAR/
                 Wunderground): bucket = math.floor(t + 0.5), deliberately
                 NOT round() -- see the module docstring: round() is
                 banker's rounding and would disagree with probability.py's
                 [b - 0.5, b + 0.5) bucket edges on exactly the half-degree
                 values. This is the ONLY mode this function supported
                 before multi-station support, so it stays the default.
      "floor"  -- source reports 0.1 C precision and the market resolves
                 to the range that CONTAINS the reading (Hong Kong
                 Observatory's climate extract, not a whole-degree
                 rounding): bucket = math.floor(t), intervals [b, b + 1),
                 so 33.9 C is bucket 33, never 34.

    The clamp mirrors the real market structure in both modes: the edge
    buckets are catch-alls ("25 or below", "35 or above"), which is also
    how probability.bucket_probabilities() folds the distribution's tails.

    axis is the market's bucket axis; passing it supersedes edge_mode and
    is REQUIRED for any market that is not Celsius whole-degree. `t` is
    Celsius in every case -- the conversion into the market's unit happens
    inside the axis, so no caller ever handles a Fahrenheit temperature.

    station is optional and purely a safety rail: this function (unlike
    probability.bucket_probabilities(), which takes a CalibratedEstimate
    carrying station_icao) has no station identity of its own to check
    against, so it cannot fail closed the way bucket_probabilities does
    just from bucket_min/bucket_max -- those are plain ints in the axis's
    own unit, and an F-station's bounds (e.g. KLGA's 70/90) are not
    distinguishable from valid Celsius bounds by value alone. If a caller
    DOES have the station in hand, passing it here lets this function
    check its axis the same way bucket_probabilities checks estimate's:
    when axis is None and station is not None, a non-default station axis
    raises rather than silently building a Celsius/1 axis and clamping
    every reading into the bottom catch-all bucket. Passing no station
    (every legacy call site, and every call before axis existed) leaves
    this check off, exactly as before -- axis remains the only REQUIRED
    guard; station is a caller's opt-in extra one.
    """
    if axis is None and station is not None:
        station_axis = bucket_axis.for_station(station)
        if not station_axis.is_default:
            icao = getattr(station, "icao", station)
            raise ValueError(
                f"{icao} is on a {station_axis.unit}/step-{station_axis.step} "
                f"bucket axis but bucket_for_temp() was called with no axis. "
                f"Refusing to settle it on the Celsius whole-degree clamp: "
                f"key_for_temp_c() would then clamp every reading this "
                f"station can actually report into the bottom catch-all "
                f"bucket, silently, because that bucket's number never "
                f"lines up with a real Celsius reading. Pass "
                f"axis=bucket_axis.for_station(station)."
            )
    lo = config.BUCKET_MIN_C if bucket_min is None else bucket_min
    hi = config.BUCKET_MAX_C if bucket_max is None else bucket_max
    resolved = BucketAxis(edge_mode=edge_mode) if axis is None else axis
    return resolved.key_for_temp_c(t, lo, hi)


def observation_visible(obs_target_date: date, sim_local_dt: datetime) -> bool:
    """
    Could the simulator legitimately know the observed max for
    obs_target_date at simulated local time sim_local_dt?

    True only from settings.OBS_PUBLISH_LAG_DAYS local days after the
    observation's own date -- the daily max isn't final until the day
    ends, and the confirmed figure publishes the following morning.
    Comparison is on the LOCAL date, since that is the calendar the
    stations, the markets and config.SCHEDULE_WINDOWS all run on.
    """
    visible_from = obs_target_date + timedelta(days=settings.OBS_PUBLISH_LAG_DAYS)
    return sim_local_dt.date() >= visible_from


def resolution_exit_price(side: str, bucket_c: int, winning_bucket: int) -> float:
    """
    What one dollar of this position is worth once the day has resolved:
    1.0 if the position was right, 0.0 if it was wrong.

    YES on the winning bucket pays par; NO pays par on every bucket
    EXCEPT the winner (a NO position is a bet the day lands anywhere
    else). Par-or-nothing exactly matches how
    position_manager._close_as_resolved() books a resolution, instead of
    baking the last noisy quote into the permanent P&L record.

    side is case-insensitive: models.Position.side is "YES"/"NO" while
    price_store stores token sides lower-case.
    """
    side_upper = side.upper()
    if side_upper == "YES":
        return 1.0 if bucket_c == winning_bucket else 0.0
    if side_upper == "NO":
        return 1.0 if bucket_c != winning_bucket else 0.0
    raise ValueError(f"Unknown side '{side}' -- expected 'YES' or 'NO'.")
