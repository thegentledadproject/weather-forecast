"""
day_ahead_report scores the evening-before book (YES side) against the bucket
the day settled into. Pinned: the LATEST recording per (day, bucket) is used;
days without settled truth are skipped, never scored as "no".
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
