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


def bucket_probabilities(
    estimate: CalibratedEstimate,
    bucket_min: int = config.BUCKET_MIN_C,
    bucket_max: int = config.BUCKET_MAX_C,
) -> List[BucketProbability]:
    """
    Return probability mass for each whole-degree bucket in
    [bucket_min, bucket_max], plus implicit tails folded into the end
    buckets (mirroring how Polymarket lists "X or below" / "Y or above"
    catch-all outcomes at the distribution's edges).

    Each bucket_c represents the range [bucket_c - 0.5, bucket_c + 0.5),
    consistent with whole-degree-C rounding used by the resolution
    source.
    """
    mean = estimate.central_estimate_c
    sd = estimate.std_dev_c

    results = []
    for b in range(bucket_min, bucket_max + 1):
        lower = b - 0.5
        upper = b + 0.5
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
