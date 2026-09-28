"""
nea.py stamps a forecast with the local day whose afternoon it covers.
The evening issue (period 18:00 -> 18:00 next day) is TOMORROW's high.
Without a usable valid_period it keeps the old behaviour: local_today.
"""
from datetime import date
from types import SimpleNamespace

import config
from clients.official import nea


def _payload(start, end, high=33):
    vp = None if start is None else {"start": start, "end": end}
    item = {"general": {"temperature": {"high": high}, "forecast": "Fair"}}
    if vp is not None:
        item["valid_period"] = vp
    return {"items": [item]}


def _run(monkeypatch, payload, today=date(2026, 9, 28)):
    monkeypatch.setattr(nea.requests, "get",
                        lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload))
    monkeypatch.setattr(config, "local_today", lambda st=None: today)
    return nea.NEAClient().get_24hr_forecast(config.get_station("WSSS"))


def test_morning_issue_is_today(monkeypatch):
    f = _run(monkeypatch, _payload("2026-09-28T06:00:00+08:00", "2026-09-29T06:00:00+08:00"))
    assert f.target_date == date(2026, 9, 28)


def test_evening_issue_is_tomorrow(monkeypatch):
    f = _run(monkeypatch, _payload("2026-09-28T18:00:00+08:00", "2026-09-29T18:00:00+08:00"))
    assert f.target_date == date(2026, 9, 29)


def test_missing_valid_period_falls_back_to_local_today(monkeypatch):
    f = _run(monkeypatch, _payload(None, None))
    assert f.target_date == date(2026, 9, 28) and f.max_temp_c == 33.0


def test_malformed_valid_period_falls_back(monkeypatch):
    f = _run(monkeypatch, _payload("not-a-time", "x"))
    assert f.target_date == date(2026, 9, 28)
