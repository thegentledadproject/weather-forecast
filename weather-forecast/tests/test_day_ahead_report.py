"""
day_ahead_report scores the evening-before book (YES side) against the bucket
the day settled into. Pinned: ONE recording per day (the latest) is used, its
asks are normalized to sum to 1, a day whose latest recording lacks the settled
bucket is skipped, and days without settled truth are skipped, never scored as "no".
"""
from datetime import date
from types import SimpleNamespace

import config
import day_ahead_report
import storage
from models import ObservedReading


def _rec(day, gen, probs):
    rows = [SimpleNamespace(bucket_c=b, side="YES", model_prob=m, market_price=p, market_bid=None)
            for b, (m, p) in probs.items()]
    storage.save_day_ahead_rows("WSSS", day, gen, rows, 32.0, 0.8)


def test_scores_latest_recording_and_skips_unsettled(tmp_db):
    src = config.get_station("WSSS").resolution_grade_source
    storage.save_observation(ObservedReading("WSSS", date(2026, 10, 1), 32.2, src))
    _rec(date(2026, 10, 1), "2026-09-30T12:00:00Z", {32: (0.1, 0.1), 33: (0.9, 0.9)})   # superseded
    _rec(date(2026, 10, 1), "2026-09-30T13:00:00Z", {32: (0.8, 0.5), 33: (0.2, 0.5)})
    _rec(date(2026, 10, 2), "2026-10-01T12:00:00Z", {32: (0.5, 0.5)})                   # no truth yet
    scored = day_ahead_report.score("WSSS")
    assert list(scored) == [date(2026, 10, 1)]
    model, market = scored[date(2026, 10, 1)]
    assert round(model, 3) == round(((0.8 - 1) ** 2 + 0.2 ** 2) / 2, 3)
    assert round(market, 3) == 0.25


def test_asks_are_normalized_before_scoring(tmp_db):
    src = config.get_station("WSSS").resolution_grade_source
    storage.save_observation(ObservedReading("WSSS", date(2026, 10, 1), 32.2, src))
    # asks 0.66 + 0.44 = 1.1 -> normalized 0.6 / 0.4; bucket 32 settled:
    # market = ((0.6 - 1)^2 + 0.4^2) / 2 = 0.16   (raw asks would give 0.1546)
    _rec(date(2026, 10, 1), "2026-09-30T13:00:00Z", {32: (0.8, 0.66), 33: (0.2, 0.44)})
    _, market = day_ahead_report.score("WSSS")[date(2026, 10, 1)]
    assert round(market, 4) == 0.16


def test_latest_recording_missing_settled_bucket_skips_day(tmp_db):
    src = config.get_station("WSSS").resolution_grade_source
    storage.save_observation(ObservedReading("WSSS", date(2026, 10, 1), 32.2, src))
    # earlier evening has bucket 32, the latest one quotes only 33 -> no mixing, skip
    _rec(date(2026, 10, 1), "2026-09-30T12:00:00Z", {32: (0.8, 0.5), 33: (0.2, 0.5)})
    _rec(date(2026, 10, 1), "2026-09-30T13:00:00Z", {33: (0.2, 1.0)})
    assert day_ahead_report.score("WSSS") == {}


def test_fahrenheit_station_settles_on_its_own_axis(tmp_db):
    # KLGA is a 2-degree F market: 25.0 C = 77.0 F settles into bucket 76 (76-77 F).
    # Before the fix this raised: bucket_for_temp() refuses F bounds without an axis.
    st = config.get_station("KLGA")
    storage.save_observation(ObservedReading("KLGA", date(2026, 10, 1), 25.0, st.resolution_grade_source))
    rows = [SimpleNamespace(bucket_c=b, side="YES", model_prob=0.5, market_price=0.5, market_bid=None)
            for b in (76, 78)]
    storage.save_day_ahead_rows("KLGA", date(2026, 10, 1), "2026-09-30T23:00:00Z", rows, 25.0, 1.0)
    model, market = day_ahead_report.score("KLGA")[date(2026, 10, 1)]
    assert round(model, 3) == 0.25 and round(market, 3) == 0.25


def test_pooled_bootstrap_clusters_on_date():
    # Two dates. Date A: 3 stations where the model is better by 0.1; date B: one
    # station where it is worse by 0.1. Resampling DATES, a draw is all-A (+),
    # all-B (-) or mixed; never "3 copies of A's stations scattered independently".
    a, b = date(2026, 10, 1), date(2026, 10, 2)
    rows = {(s, a): (0.1, 0.2) for s in ("X", "Y", "Z")}
    rows[("X", b)] = (0.3, 0.2)
    p, lo, hi, n_dates = day_ahead_report.pooled_bootstrap(rows, draws=2000)
    assert n_dates == 2
    assert round(lo, 3) == -0.1 and round(hi, 3) == 0.1    # all-A and all-B draws
    assert 0.6 < p < 0.9                                   # P(mean < 0): AA, AB, BA -> ~0.75
