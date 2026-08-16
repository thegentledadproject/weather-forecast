"""
tests/test_hko_ingest.py

VHHH's settlement-grade observation ingest.

Background (2026-08-16): VHHH had been storing hko_fnd forecasts since
2026-08-06 against ZERO rows of its own resolution_grade_source
("hko_daily_max"), so the station could never mature and nothing about it
could be scored. The ingest was not broken, wired wrong, or failing --
it ran every cycle, fail-soft, and correctly reported that recent days
were unpublished.

The defect was arithmetic between two numbers nobody compared: it swept
the last 3 days (metar_client's default) while HKO's CLMMAXT climate
dataset publishes a COMPLETE MONTH AT A TIME, weeks in arrears. Probed
live, 2026-08-16 returned 31 rows for July and 0 for August, meaning
July 1st became readable about 46 days after the fact. A 3-day window
and a 46-day lag cannot overlap, so the sweep was guaranteed to save
nothing for as long as it ran, while the data sat in the API.

These tests pin the overlap, not the constant: what matters is that the
window reaches a day the source publishes late.
"""

from datetime import date, timedelta

import config
import storage
from clients.official import hko


class _Recorder:
    """Captures save_observation calls instead of touching a database."""

    def __init__(self):
        self.saved = []

    def __call__(self, observation):
        self.saved.append(observation)


def _setup(monkeypatch, published, existing=(), today=date(2026, 8, 16)):
    """
    published: {(y, m, d): temp_c} -- what CLMMAXT would return.
    existing:  dates already stored as hko_daily_max.
    """
    recorder = _Recorder()
    monkeypatch.setattr(storage, "save_observation", recorder)
    monkeypatch.setattr(
        storage, "load_observations_since",
        lambda icao, since: [
            type("O", (), {"target_date": d, "source": "hko_daily_max"})() for d in existing
        ],
    )
    monkeypatch.setattr(config, "local_today", lambda station: today)

    def _rows(year, month, timeout=15):
        return {k: v for k, v in published.items() if k[0] == year and (month is None or k[1] == month)}

    monkeypatch.setattr(hko, "_fetch_clmmaxt_rows", _rows)
    # The per-station throttle is module state and would suppress a second
    # call in the same test session.
    monkeypatch.setattr(hko, "_last_ingest_by_station", {})
    return recorder


def test_the_window_reaches_a_month_published_weeks_late(monkeypatch):
    """
    THE BUG, DIRECTLY.

    On 2026-08-16 the newest published CLMMAXT day is 2026-07-31 -- 16
    days back -- and August is entirely absent. The sweep must reach it.
    """
    published = {(2026, 7, d): 30.0 + d / 100 for d in range(1, 32)}
    recorder = _setup(monkeypatch, published)

    saved = hko.ingest_missing_recent(["VHHH"])

    assert saved == 31
    assert {o.target_date for o in recorder.saved} == {date(2026, 7, d) for d in range(1, 32)}
    assert all(o.source == "hko_daily_max" for o in recorder.saved)
    assert all(o.station_icao == "VHHH" for o in recorder.saved)


def test_a_three_day_window_would_have_saved_nothing(monkeypatch):
    """
    The old default, against the same data, to pin WHY this changed. Not a
    regression guard on the constant -- a record that the previous value
    could not have worked, so no future reader restores it as a tidy-up.
    """
    published = {(2026, 7, d): 30.0 + d / 100 for d in range(1, 32)}
    recorder = _setup(monkeypatch, published)

    assert hko.ingest_missing_recent(["VHHH"], days_back=3) == 0
    assert recorder.saved == []


def test_the_default_lookback_outruns_the_observed_lag():
    """
    The measured worst case was ~46 days (2026-07-01 first readable around
    2026-08-16). The default must clear that with room, or the window
    silently stops overlapping the source again the first slow month.
    """
    assert config.HKO_INGEST_LOOKBACK_DAYS > 46


def test_days_already_stored_are_not_refetched(monkeypatch):
    """A long window must not mean re-saving the same 70 days every sweep."""
    published = {(2026, 7, d): 30.0 for d in range(1, 32)}
    already = {date(2026, 7, d) for d in range(1, 31)}  # all but the 31st
    recorder = _setup(monkeypatch, published, existing=already)

    saved = hko.ingest_missing_recent(["VHHH"])

    assert saved == 1
    assert [o.target_date for o in recorder.saved] == [date(2026, 7, 31)]


def test_an_unpublished_span_saves_nothing_and_does_not_raise(monkeypatch):
    """
    The normal state for the current month. HKO publishing nothing is an
    honest gap, not an error -- this runs inside live trading cycles.
    """
    recorder = _setup(monkeypatch, published={})

    assert hko.ingest_missing_recent(["VHHH"]) == 0
    assert recorder.saved == []


def test_unpublished_days_are_logged_per_month_not_per_day(monkeypatch, capsys):
    """
    At a 75-day lookback, per-day logging would print ~46 identical lines
    every sweep and bury the saves. One line per (year, month).
    """
    _setup(monkeypatch, published={})
    hko.ingest_missing_recent(["VHHH"])

    lines = [l for l in capsys.readouterr().out.splitlines() if "no published" in l]
    # 75 days back from 2026-08-16 spans June, July and August.
    assert 1 <= len(lines) <= 3


def test_stations_that_do_not_settle_on_hko_are_skipped(monkeypatch):
    """
    Called unconditionally alongside the METAR ingest, so every
    metar-settled station must be a silent no-op -- not a wasted request,
    and never an hko_daily_max row written against a METAR station.
    """
    recorder = _setup(monkeypatch, published={(2026, 7, 15): 31.0})

    assert hko.ingest_missing_recent(["WSSS", "WMKK", "RJTT"]) == 0
    assert recorder.saved == []


def test_vhhh_still_settles_on_hko_and_skips_metar():
    """
    The premise of all of the above. If VHHH's registry entry ever changes
    these, this whole module is testing the wrong station.
    """
    vhhh = config.get_station("VHHH")
    assert vhhh.resolution_grade_source == "hko_daily_max"
    assert vhhh.metar_ingest_mode == "skip"
