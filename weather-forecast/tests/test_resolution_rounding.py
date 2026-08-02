"""
tests/test_resolution_rounding.py

Covers backtest.resolution.bucket_for_temp() over a 0.1C grid spanning
every bucket boundary plus the tails.

Buckets are whole-degree-C, config.BUCKET_MIN_C..config.BUCKET_MAX_C
(25..35 today -- 11 buckets, 1C wide each, matching the real Polymarket
market structure per config.py's comment). bucket_for_temp() uses
math.floor(t + 0.5) -- half-UP, deliberately NOT round() (Python's
round() is banker's/round-half-even: round(30.5) == 30 but
round(31.5) == 32, which would disagree with probability.py's
[b - 0.5, b + 0.5) bucket edges on exactly the half-degree values).
Confirmed by reading both the code and its module docstring, so the
.5-boundary assertions below test genuine half-up behaviour, not an
assumption.
"""
import math

import config
import probability
from backtest import resolution
from models import CalibratedEstimate


def _grid(lo: float, hi: float, step: float = 0.1):
    n_steps = round((hi - lo) / step)
    return [round(lo + i * step, 1) for i in range(n_steps + 1)]


def _is_half_boundary(t: float) -> bool:
    frac = t - math.floor(t)
    return math.isclose(frac, 0.5, abs_tol=1e-9)


def test_half_up_rounding_at_exact_half_boundaries():
    # Explicit examples from the task spec, confirmed against the
    # actual bucket width (1C) found in resolution.py.
    assert resolution.bucket_for_temp(30.5) == 31
    assert resolution.bucket_for_temp(31.5) == 32

    # Generalize across every interior half-degree boundary: half-up
    # means x.5 always rounds UP to x+1, never down (round-half-even
    # would alternate depending on whether x+1 is odd/even).
    for b in range(config.BUCKET_MIN_C, config.BUCKET_MAX_C):
        half_point = b + 0.5
        assert resolution.bucket_for_temp(half_point) == b + 1, (
            f"{half_point} should round half-up into bucket {b + 1}"
        )


def test_clamping_at_the_tails():
    # Below the lowest bucket -- clamps to BUCKET_MIN_C rather than
    # erroring or returning an out-of-range bucket.
    for t in [-100.0, 0.0, 10.0, 24.9, config.BUCKET_MIN_C - 0.51]:
        assert resolution.bucket_for_temp(t) == config.BUCKET_MIN_C, t

    # Above the highest bucket -- clamps to BUCKET_MAX_C.
    for t in [config.BUCKET_MAX_C + 0.51, 40.0, 60.0, 200.0]:
        assert resolution.bucket_for_temp(t) == config.BUCKET_MAX_C, t


def test_grid_spanning_every_bucket_boundary_and_tails():
    # 0.1C grid from two degrees below the lowest bucket to two degrees
    # above the highest, so it spans every real boundary plus both tails.
    lo = config.BUCKET_MIN_C - 2.0
    hi = config.BUCKET_MAX_C + 2.0
    for t in _grid(lo, hi):
        bucket = resolution.bucket_for_temp(t)
        assert config.BUCKET_MIN_C <= bucket <= config.BUCKET_MAX_C
        # Cross-check against the half-up formula directly.
        expected = max(config.BUCKET_MIN_C, min(config.BUCKET_MAX_C, math.floor(t + 0.5)))
        assert bucket == expected, t


def test_agrees_with_bucket_probabilities_argmax():
    # Near-zero-std estimate centered at each grid point should put
    # (almost) all probability mass in the same bucket bucket_for_temp
    # assigns to that point. Skip exact .5 points -- both neighboring
    # buckets are legitimately close to a tie there under a
    # probability-mass argmax, even with a tiny std dev.
    lo = config.BUCKET_MIN_C - 2.0
    hi = config.BUCKET_MAX_C + 2.0
    for t in _grid(lo, hi):
        if _is_half_boundary(t):
            continue

        estimate = CalibratedEstimate(
            station_icao="TEST",
            target_date=None,
            central_estimate_c=t,
            std_dev_c=1e-6,
            monsoon_phase="inter_monsoon",
        )
        buckets = probability.bucket_probabilities(estimate)
        best = probability.most_likely_bucket(buckets)

        assert best.bucket_c == resolution.bucket_for_temp(t), (
            f"t={t}: bucket_probabilities argmax={best.bucket_c} "
            f"!= bucket_for_temp={resolution.bucket_for_temp(t)}"
        )
