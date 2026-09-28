"""
WMKK's official forecast must be the entry WWIS dates as the station's
local today -- never forecastDay[0] blindly. After WWIS rolls its list
forward, [0] is tomorrow, and a row stamped target_date=today carrying
tomorrow's max pollutes both today's blend and WMKK's bias pairs.
"""

from datetime import date
from unittest import mock

import config
from clients.official import met_malaysia
from clients.official.met_malaysia import METMalaysiaClient


def _resp(payload):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


def _forecast(days):
    client = METMalaysiaClient()
    client._city_id_cache["Kuala Lumpur"] = "82"
    payload = {"city": {"forecast": {"forecastDay": days}}}
    with mock.patch.object(met_malaysia.config, "local_today", return_value=date(2026, 9, 28)), \
         mock.patch.object(met_malaysia.requests, "get", return_value=_resp(payload)):
        return client.get_24hr_forecast(config.STATIONS["WMKK"])


def test_picks_the_entry_dated_local_today_not_the_first():
    f = _forecast([
        {"forecastDate": "2026-09-27", "maxTemp": "31", "weather": "Rain"},
        {"forecastDate": "2026-09-28", "maxTemp": "34", "weather": "Fair"},
    ])
    assert f.target_date == date(2026, 9, 28)
    assert f.max_temp_c == 34.0
    assert f.raw_note == "Fair"
    assert f.source == "wwis_met_malaysia"


def test_list_already_rolled_to_tomorrow_returns_none():
    assert _forecast([
        {"forecastDate": "2026-09-29", "maxTemp": "33"},
        {"forecastDate": "2026-09-30", "maxTemp": "32"},
    ]) is None


def test_empty_max_temp_is_no_number_not_a_crash():
    f = _forecast([{"forecastDate": "2026-09-28", "maxTemp": ""}])
    assert f is not None and f.max_temp_c is None
