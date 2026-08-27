"""
tests/test_observed_half_life.py

Recency weighting for the OBSERVED term of the central estimate.

That term carries 60% of the blend for WSSS and was a plain unweighted mean,
so a regime change could not move it: WSSS settled 33.0 on each of
2026-08-19..25 while the term sat at 32.538, the estimate stayed pinned near
32.5, and the book bought 32:YES and 33:NO against a seven-day run of 33s.

config.OBSERVED_HALF_LIFE_DAYS SHIPS INERT (None = the unweighted mean,
bit-for-bit). The value is chosen by measurement, not here. See
docs/superpowers/specs/2026-08-28-recency-weighted-observed-term-design.md.
"""

import random
import statistics
from datetime import date, timedelta

import pytest

import calibration
import config
from models import ObservedReading, PointForecast


def _obs(day, temp):
    return ObservedReading("WSSS", day, temp, config.RESOLUTION_GRADE_OBSERVATION_SOURCE)


AS_OF = date(2026, 8, 28)


def test_none_reproduces_the_unweighted_mean_bit_for_bit():
    """
    The no-op this ships on. Randomised rather than hand-picked: the claim is
    about every input, and a float mean is exactly where a "harmless"
    reformulation silently drifts in the last place.
    """
    rng = random.Random(20260828)
    for _ in range(200):
        n = rng.randint(1, 40)
        rows = [(AS_OF - timedelta(days=rng.randint(0, 60)), rng.uniform(24.0, 38.0))
                for _ in range(n)]
        got = calibration.observed_mean_weighted(rows, AS_OF, None)
        assert got == statistics.fmean(t for _, t in rows)


def test_a_recent_regime_outweighs_an_older_one():
    """
    THE 2026-08-21..27 CASE. Seven 33s after a month of 32s must pull the
    term toward 33 -- which the unweighted mean does not do.
    """
    older = [(AS_OF - timedelta(days=d), 32.0) for d in range(8, 30)]
    recent = [(AS_OF - timedelta(days=d), 33.0) for d in range(1, 8)]
    rows = older + recent

    unweighted = calibration.observed_mean_weighted(rows, AS_OF, None)
    weighted = calibration.observed_mean_weighted(rows, AS_OF, 3.0)

    assert unweighted < 32.4, unweighted
    assert weighted > 32.8, weighted


def test_a_shorter_half_life_tracks_harder():
    """Monotone in the knob, which is what makes a sweep of it meaningful."""
    rows = ([(AS_OF - timedelta(days=d), 32.0) for d in range(8, 30)]
            + [(AS_OF - timedelta(days=d), 33.0) for d in range(1, 8)])

    by_hl = [calibration.observed_mean_weighted(rows, AS_OF, hl) for hl in (14, 7, 5, 3, 2)]

    assert by_hl == sorted(by_hl), f"not monotone in half-life: {by_hl}"


def test_weights_depend_on_age_not_on_absolute_date():
    """Shifting the whole sample and as_of together must change nothing."""
    rows = [(AS_OF - timedelta(days=d), 30.0 + d) for d in range(0, 20)]
    shifted = [(d - timedelta(days=365), t) for d, t in rows]

    assert calibration.observed_mean_weighted(rows, AS_OF, 5.0) == pytest.approx(
        calibration.observed_mean_weighted(shifted, AS_OF - timedelta(days=365), 5.0)
    )


def test_all_samples_on_one_day_equal_the_unweighted_mean_at_any_half_life():
    rows = [(AS_OF - timedelta(days=3), t) for t in (31.0, 32.0, 33.0)]
    for hl in (None, 14, 7, 2):
        assert calibration.observed_mean_weighted(rows, AS_OF, hl) == pytest.approx(32.0)


def test_a_future_dated_sample_is_treated_as_today_not_weighted_up():
    """
    0.5 ** negative is > 1. bias_stats_weighted clamps age at 0 for exactly
    this reason; a future-dated row is an upstream bug, not the most
    informative sample in the set.
    """
    rows = [(AS_OF + timedelta(days=5), 40.0), (AS_OF, 30.0)]
    assert calibration.observed_mean_weighted(rows, AS_OF, 3.0) == pytest.approx(35.0)


def test_empty_returns_none_so_the_existing_fallbacks_still_fire():
    assert calibration.observed_mean_weighted([], AS_OF, 7.0) is None


def test_blend_central_estimate_is_unchanged_at_the_shipped_default():
    """
    The integration no-op. With config.OBSERVED_HALF_LIFE_DAYS at its shipped
    value, the blended estimate must equal what the unweighted mean produced.
    """
    assert config.OBSERVED_HALF_LIFE_DAYS is None

    observations = ([_obs(AS_OF - timedelta(days=d), 32.0) for d in range(8, 30)]
                    + [_obs(AS_OF - timedelta(days=d), 33.0) for d in range(1, 8)])
    forecasts = [PointForecast("WSSS", "open_meteo_gfs", AS_OF, 31.0, "2026-08-27T21:00:00Z")]

    got = calibration.blend_central_estimate(
        forecasts, observations, 31.5, forecast_weight=0.4
    )
    expected = round(
        0.4 * 31.0 + 0.6 * statistics.fmean(o.max_temp_c for o in observations), 1
    )
    assert got == expected
