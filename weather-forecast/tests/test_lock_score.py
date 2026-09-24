"""Gap 5: lock_score.py implements the pre-registered exam and nothing else."""
from datetime import date, timedelta

import pytest

import lock_score as ls


def test_three_bucket_brier_by_hand():
    # (0.2-0)^2 + (0.5-1)^2 + (0.3-0)^2 = 0.04 + 0.25 + 0.09
    assert ls.brier([0.2, 0.5, 0.3], 1) == pytest.approx(0.38)
    assert ls.brier([0.0, 1.0, 0.0], 1) == 0.0


def test_log_loss_is_floored():
    assert ls.log_loss([1.0, 0.0], 1) == pytest.approx(-__import__("math").log(1e-3))


def test_rps_by_hand():
    # cumulative f: 0.2, 0.7; cumulative o (k=1): 0, 1 -> (0.04 + 0.09) / 2
    assert ls.rps([0.2, 0.5, 0.3], 1) == pytest.approx(0.065)


def test_the_bootstrap_ci_is_reproducible_under_the_fixed_seed():
    per_date = {date(2026, 9, 1) + timedelta(days=k): ((-1) ** k * 0.01 * k, 5) for k in range(25)}
    a = ls.cluster_bootstrap(per_date, draws=2000)
    assert a == ls.cluster_bootstrap(per_date, draws=2000)
    assert a != ls.cluster_bootstrap(per_date, draws=2000, seed=1)
    assert a[1] <= a[0] <= a[2]


def test_e30_uses_only_prior_dates():
    day = date(2026, 9, 20)
    hist = {("WSSS", day - timedelta(days=k)): 31 for k in range(1, 31)}
    base = ls.e30(hist, "WSSS", day, [30, 31, 32])
    assert base == pytest.approx([0.5 / 31.5, 30.5 / 31.5, 0.5 / 31.5])
    leaky = dict(hist)
    leaky[("WSSS", day)] = 30                          # the day itself
    leaky[("WSSS", day + timedelta(days=1))] = 30     # the future
    leaky[("WSSS", day - timedelta(days=31))] = 30    # older than 30 days
    leaky[("RCSS", day - timedelta(days=1))] = 30     # another station
    assert ls.e30(leaky, "WSSS", day, [30, 31, 32]) == base


def test_test_dates_are_refused_without_a_locked_read_on_or_after_the_read_date(monkeypatch):
    train = (date(2026, 9, 4), date(2026, 9, 23))
    test = (date(2026, 10, 6), date(2026, 11, 2))
    assert ls.refuses(*train, False, date(2026, 9, 24)) is None
    assert ls.refuses(date(2026, 9, 25), date(2026, 10, 6), False, date(2026, 10, 7))    # overlap
    assert ls.refuses(*test, True, date(2026, 11, 4))                                     # too early
    monkeypatch.setattr(ls, "LOCK_SHA", None)
    assert ls.refuses(*test, True, date(2026, 11, 5))                                     # no lock sha
    monkeypatch.setattr(ls, "LOCK_SHA", "abc")
    assert ls.refuses(*test, True, date(2026, 11, 5)) is None
    assert ls.main(["--start", "2026-10-06", "--end", "2026-10-07"], today=date(2026, 12, 1)) == 2


def test_forecasters_are_distributions():
    unit = {"station": "WSSS", "date": date(2026, 9, 20), "buckets": [30, 31],
            "p": [0.7, 0.4], "m": [0.6, 0.5], "k": 0}
    f = ls.forecasts(unit, {}, 0.5)
    for v in f.values():
        assert sum(v) == pytest.approx(1.0)
    assert f["M"] == pytest.approx([0.6 / 1.1, 0.5 / 1.1])
    assert f["U"] == [0.5, 0.5]
    assert f["E30"] == [0.5, 0.5]


def test_lambda_zero_makes_q_exactly_m():
    unit = {"station": "WSSS", "date": date(2026, 9, 20), "buckets": [30, 31, 32],
            "p": [0.1, 0.7, 0.2], "m": [0.13, 0.61, 0.37], "k": 1}
    f = ls.forecasts(unit, {}, 0.0)
    assert f["Q"] == f["M"]
