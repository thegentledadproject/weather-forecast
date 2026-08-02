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

import config

from backtest import settings


def bucket_for_temp(t: float) -> int:
    """
    The resolution bucket a max temperature of `t` degrees C falls in,
    clamped into [config.BUCKET_MIN_C, config.BUCKET_MAX_C].

    Half-UP rounding via math.floor(t + 0.5), deliberately NOT round() --
    see the module docstring: round() is banker's rounding and would
    disagree with probability.py's [b - 0.5, b + 0.5) bucket edges on
    exactly the half-degree values.

    The clamp mirrors the real market structure: the edge buckets are
    catch-alls ("25 or below", "35 or above"), which is also how
    probability.bucket_probabilities() folds the distribution's tails.
    """
    bucket = math.floor(t + 0.5)
    return max(config.BUCKET_MIN_C, min(config.BUCKET_MAX_C, bucket))


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
