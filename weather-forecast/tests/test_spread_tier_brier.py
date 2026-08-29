"""
tests/test_spread_tier_brier.py

spread_tier_brier.py asks which WIDTH the probability step should cut buckets
from, scored over every settled station-day and every bucket rather than over
the buckets a position was opened in.

That distinction is the whole point. Both existing measurements of this
question -- spread_audit.py's 2026-08-20 table and the 2026-08-29 per-station
model_prob-minus-truth table -- are computed over BOUGHT buckets, which are
selected on model_prob > price. That is the upper tail of the model's own
error, so both read as "the model is over-confident" no matter which way the
width is wrong. An unselected score cannot be fooled that way.

These tests pin the pure core: the Brier scorer, the leave-one-out estimators
that keep a day out of its own correction, and the sweep.
"""
from datetime import date

import pytest

from models import BucketProbability
import spread_tier_brier as stb


def _probs(mapping):
    return [BucketProbability(bucket_c=b, probability=p) for b, p in sorted(mapping.items())]


class TestBrierScore:
    def test_scores_a_hand_computable_distribution(self):
        # (0.2-0)^2 + (0.5-1)^2 + (0.3-0)^2 = 0.04 + 0.25 + 0.09
        score = stb.brier_score(_probs({30: 0.2, 31: 0.5, 32: 0.3}), settled_bucket=31)

        assert score == pytest.approx(0.38)

    def test_a_perfect_call_scores_zero(self):
        assert stb.brier_score(_probs({30: 0.0, 31: 1.0}), settled_bucket=31) == pytest.approx(0.0)

    def test_a_confident_miss_scores_worse_than_an_unsure_one(self):
        confident = stb.brier_score(_probs({30: 0.9, 31: 0.1}), settled_bucket=31)
        unsure = stb.brier_score(_probs({30: 0.5, 31: 0.5}), settled_bucket=31)

        assert confident > unsure

    def test_a_settled_bucket_outside_the_listed_range_is_refused(self):
        """
        Scoring a day whose settlement is off the listed axis would silently
        credit the model for mass it never had to place.
        """
        with pytest.raises(ValueError, match="settled bucket 40"):
            stb.brier_score(_probs({30: 0.5, 31: 0.5}), settled_bucket=40)


class TestLeaveOneOut:
    def test_bias_for_a_day_excludes_that_day(self):
        errors = {date(2026, 8, 1): 1.0, date(2026, 8, 2): 2.0, date(2026, 8, 3): 3.0}

        loo = stb.leave_one_out_bias(errors)

        assert loo[date(2026, 8, 1)] == pytest.approx(2.5)
        assert loo[date(2026, 8, 2)] == pytest.approx(2.0)
        assert loo[date(2026, 8, 3)] == pytest.approx(1.5)

    def test_bias_is_none_for_the_only_day(self):
        assert stb.leave_one_out_bias({date(2026, 8, 1): 1.0}) == {date(2026, 8, 1): None}

    def test_spread_for_a_day_excludes_that_day(self):
        errors = {date(2026, 8, i): e for i, e in enumerate([0.0, 2.0, 0.0, 2.0, 0.0, 2.0], start=1)}

        loo = stb.leave_one_out_spread(errors, min_pairs=5)

        # dropping one 0.0 leaves [2,0,2,0,2]: mean 1.2, sample stdev ~1.0954
        assert loo[date(2026, 8, 1)] == pytest.approx(1.0954, abs=1e-4)

    def test_spread_is_none_when_the_remaining_days_are_too_few(self):
        errors = {date(2026, 8, 1): 1.0, date(2026, 8, 2): 2.0}

        assert stb.leave_one_out_spread(errors, min_pairs=5) == {
            date(2026, 8, 1): None, date(2026, 8, 2): None,
        }


class TestSweep:
    def _day(self, center, settled):
        return stb.ScoredDay(
            target_date=date(2026, 8, 1), center_c=center, settled_bucket=settled,
            bucket_min=28, bucket_max=34, axis=stb.DEFAULT_AXIS,
        )

    def test_a_center_that_is_always_right_prefers_the_narrowest_width(self):
        days = [self._day(31.0, 31), self._day(30.0, 30), self._day(32.0, 32)]

        best_width, _ = stb.best_width(days, grid=[0.3, 1.0, 2.0])

        assert best_width == 0.3

    def test_a_center_that_is_always_a_bucket_off_prefers_a_wider_width(self):
        days = [self._day(31.0, 32), self._day(30.0, 31), self._day(32.0, 33)]

        best_width, _ = stb.best_width(days, grid=[0.3, 1.0, 2.0])

        assert best_width > 0.3

    def test_sweep_returns_one_mean_brier_per_grid_width(self):
        days = [self._day(31.0, 31), self._day(30.0, 30)]

        swept = stb.sweep_widths(days, grid=[0.5, 1.5])

        assert [w for w, _ in swept] == [0.5, 1.5]
        assert all(0.0 <= b <= 2.0 for _, b in swept)

    def test_sweep_over_no_days_is_empty_rather_than_a_division_by_zero(self):
        assert stb.sweep_widths([], grid=[0.5, 1.5]) == []
