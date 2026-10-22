"""
nea.py stamps the 24h forecast with the SGT day whose afternoon
(12:00-18:00) its valid period covers. The evening issue (18:00 -> 18:00
next day) is TOMORROW's high: the 20:00 SGT day-ahead recorder must see it
as tomorrow, and it must never be filed as today's nea_24hr row. A period
covering no afternoon, or a payload it can't read, gives None.

Fixtures are real data.gov.sg v2 captures (2026-10-23 00:07 SGT):
tests/fixtures/nea_v2_*.json.
"""
import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import config
from clients.official import nea

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / f"nea_v2_{name}.json").read_text(encoding="utf-8"))


def _patch(monkeypatch, payload):
    monkeypatch.setattr(nea.requests, "get",
                        lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload))


def _forecast(monkeypatch, start=None, end=None, payload=None):
    if payload is None:
        payload = copy.deepcopy(_load("twenty-four-hr-forecast"))
        vp = payload["data"]["records"][0]["general"]["validPeriod"]
        if start is not None:
            vp["start"], vp["end"] = start, end
    _patch(monkeypatch, payload)
    return nea.NEAClient().get_24hr_forecast(config.get_station("WSSS"))


def test_real_capture_is_stamped_from_its_period(monkeypatch):
    # 23:32 SGT issue on 10-22, period 00:00 -> 00:00: covers 10-23's afternoon.
    f = _forecast(monkeypatch)
    assert f.target_date == date(2026, 10, 23)
    assert f.max_temp_c == 34.0
    assert f.source == "nea_24hr"
    assert f.raw_note == "Thundery Showers"


def test_morning_issue_is_today(monkeypatch):
    f = _forecast(monkeypatch, "2026-10-23T06:00:00+08:00", "2026-10-24T06:00:00+08:00")
    assert f.target_date == date(2026, 10, 23)


def test_evening_issue_is_tomorrow(monkeypatch):
    f = _forecast(monkeypatch, "2026-10-23T18:00:00+08:00", "2026-10-24T18:00:00+08:00")
    assert f.target_date == date(2026, 10, 24)


def test_utc_stamps_are_read_in_sgt(monkeypatch):
    # 10:00Z = 18:00 SGT: still the evening issue.
    f = _forecast(monkeypatch, "2026-10-23T10:00:00Z", "2026-10-24T10:00:00Z")
    assert f.target_date == date(2026, 10, 24)


def test_period_covering_no_afternoon_is_none(monkeypatch):
    assert _forecast(monkeypatch, "2026-10-23T18:00:00+08:00", "2026-10-24T06:00:00+08:00") is None
    # Half an afternoon is not an afternoon.
    assert _forecast(monkeypatch, "2026-10-23T15:00:00+08:00", "2026-10-24T09:00:00+08:00") is None


def test_malformed_payloads_are_none_not_exceptions(monkeypatch):
    good = _load("twenty-four-hr-forecast")
    no_period = copy.deepcopy(good)
    del no_period["data"]["records"][0]["general"]["validPeriod"]
    for bad in ({}, {"data": None}, {"data": {"records": []}}, no_period, [], "x"):
        assert _forecast(monkeypatch, payload=bad) is None
    assert _forecast(monkeypatch, "not-a-time", "x") is None


def test_v1_payload_is_none(monkeypatch):
    v1 = {"items": [{"valid_period": {"start": "2026-10-23T06:00:00+08:00",
                                      "end": "2026-10-24T06:00:00+08:00"},
                     "general": {"temperature": {"high": 33}, "forecast": "Fair"}}]}
    assert _forecast(monkeypatch, payload=v1) is None


def test_nowcast_reads_v2(monkeypatch):
    _patch(monkeypatch, _load("two-hr-forecast"))
    assert nea.NEAClient().get_same_day_signal(config.get_station("WSSS")) == "Changi 2hr nowcast: Cloudy"


def test_live_reading_reads_v2(monkeypatch):
    _patch(monkeypatch, _load("air-temperature"))
    assert nea.NEAClient().get_live_reading(config.get_station("WSSS")) == 28.0
