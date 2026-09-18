"""
Wave 2 item 2d. probability_calibration.calibration_for cached
(None, NO_TIER, 0) on ANY exception, so one transient storage failure at
05:00 left every station uncalibrated -- sized on the raw model_prob with
the double buffer, admitted on the raw edge -- for the rest of the day
(trade-logic review 2026-09-15: 'calibration failures cached all day').
The failure is now logged and returned WITHOUT caching, so the next cycle
retries. Success is still cached per station-day as before.
"""
from datetime import date, timedelta

import config
import probability_calibration as pc

DAY_N = date(2026, 9, 21)


def _rows(n, model_prob, outcome, day, station="WSSS"):
    return [
        {"station_icao": station, "target_date": day, "model_prob": model_prob, "outcome": outcome}
        for _ in range(n)
    ]


def _flaky_cohort(fail_first):
    """A load_cohort that raises on the first `fail_first` calls, then fits."""
    calls = {"n": 0}

    def _load(**kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise RuntimeError("storage is briefly gone")
        return _rows(40, 0.60, 1.0, DAY_N - timedelta(days=1)), {}

    return _load, calls


def test_a_failed_fit_is_retried_on_the_next_call(monkeypatch):
    load, calls = _flaky_cohort(fail_first=1)
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", load)
    pc.clear_cache()

    first = pc.calibration_for("WSSS", DAY_N)
    second = pc.calibration_for("WSSS", DAY_N)

    assert first == (None, pc.NO_TIER, 0)
    assert second[1] == pc.STATION_TIER and second[0] is not None
    assert calls["n"] == 2


def test_a_successful_fit_is_still_cached(monkeypatch):
    load, calls = _flaky_cohort(fail_first=0)
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", load)
    pc.clear_cache()
    pc.calibration_for("WSSS", DAY_N)
    pc.calibration_for("WSSS", DAY_N)
    assert calls["n"] == 1


def test_the_failure_still_degrades_to_uncalibrated_not_an_exception(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("storage is gone")
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", _boom)
    pc.clear_cache()
    assert pc.calibration_for("WSSS", DAY_N) == (None, pc.NO_TIER, 0)


def test_flag_off_caches_the_failure_as_before(monkeypatch):
    monkeypatch.setattr(config, "RETRY_FAILED_CALIBRATION_FITS", False)
    load, calls = _flaky_cohort(fail_first=1)
    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", load)
    pc.clear_cache()

    pc.calibration_for("WSSS", DAY_N)
    second = pc.calibration_for("WSSS", DAY_N)

    assert second == (None, pc.NO_TIER, 0)
    assert calls["n"] == 1
