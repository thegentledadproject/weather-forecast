"""
probability.py

PURPOSE
-------
Implements framework Step C: turn a CalibratedEstimate (central value
+ std dev) into a probability distribution across whole-degree-C
Polymarket-style buckets, using a normal-distribution approximation.

Deliberately avoids a scipy dependency for the MVP -- uses the
standard-library math.erf to compute the normal CDF directly, since
that's the only piece of scipy.stats.norm this needs.

DEPENDENCIES
------------
math (standard library)
config.py, models.py (local)
"""

import math
from typing import List

import config
from models import CalibratedEstimate, BucketProbability


def _normal_cdf(x: float, mean: float, std_dev: float) -> float:
    """Standard normal CDF via math.erf -- no scipy needed."""
    if std_dev <= 0:
        return 1.0 if x >= mean else 0.0
    z = (x - mean) / (std_dev * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def _bucket_interval(bucket_c: int, edge_mode: str) -> tuple:
    """
    The temperature interval one bucket covers, per the settlement
    source's own precision:

      "half_up" -- the source reports WHOLE degrees (METAR/Wunderground),
                   so the market's "31°C" outcome wins for any reading
                   that rounds to 31: [30.5, 31.5).
      "floor"   -- the source reports 0.1°C and the market resolves to
                   the range CONTAINING the reading (Hong Kong
                   Observatory's climate extract), so "33°C" wins for
                   [33.0, 34.0) and 33.9°C is bucket 33, never 34.

    Getting this wrong is a half-degree shift of the entire distribution
    against the book -- a systematic mispricing of the two buckets either
    side of the mode, on every cycle, in the same direction.
    """
    if edge_mode == "half_up":
        return bucket_c - 0.5, bucket_c + 0.5
    if edge_mode == "floor":
        return float(bucket_c), float(bucket_c + 1)
    raise ValueError(
        f"unknown bucket edge_mode '{edge_mode}' -- expected 'half_up' or 'floor' "
        f"(see models.StationConfig.bucket_edge_mode). Refusing to guess a bucket "
        f"mapping: a wrong one silently mis-prices every bucket."
    )


def bucket_probabilities(
    estimate: CalibratedEstimate,
    bucket_min: int = config.BUCKET_MIN_C,
    bucket_max: int = config.BUCKET_MAX_C,
    edge_mode: str = "half_up",
) -> List[BucketProbability]:
    """
    Return probability mass for each whole-degree bucket in
    [bucket_min, bucket_max], plus implicit tails folded into the end
    buckets (mirroring how Polymarket lists "X or below" / "Y or above"
    catch-all outcomes at the distribution's edges).

    edge_mode selects what a bucket's number MEANS -- "half_up" (the
    default, and the historical behaviour of this function: intervals
    [b-0.5, b+0.5)) or "floor" ([b, b+1)); see _bucket_interval() and
    models.StationConfig.bucket_edge_mode. Tail folding is identical in
    both modes: everything below the lowest listed bucket's upper edge
    belongs to that bucket, everything at or above the highest listed
    bucket's lower edge belongs to that one.

    The bounds passed here should be the LIVE ones derived from the
    discovered token map on the trading path (see
    market_discovery.derive_bucket_bounds) -- the module-level defaults
    exist for station-agnostic callers and old tests only.
    """
    mean = estimate.central_estimate_c
    sd = estimate.std_dev_c

    results = []
    for b in range(bucket_min, bucket_max + 1):
        lower, upper = _bucket_interval(b, edge_mode)
        if b == bucket_min:
            # fold left tail into the lowest listed bucket
            prob = _normal_cdf(upper, mean, sd)
        elif b == bucket_max:
            # fold right tail into the highest listed bucket
            prob = 1 - _normal_cdf(lower, mean, sd)
        else:
            prob = _normal_cdf(upper, mean, sd) - _normal_cdf(lower, mean, sd)
        results.append(BucketProbability(bucket_c=b, probability=round(prob, 4)))

    return results


def most_likely_bucket(buckets: List[BucketProbability]) -> BucketProbability:
    """Convenience helper: return the single highest-probability bucket."""
    return max(buckets, key=lambda b: b.probability)
