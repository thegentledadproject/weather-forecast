"""clients/metar_client.py: which METAR reports count toward the daily max."""
from clients import metar_client


def test_fetch_metars_skips_speci(monkeypatch):
    """Contracts since 2026-10-23: 'A report whose line begins with SPECI ... does not count.'"""
    rows = [
        {"obsTime": 100, "temp": 30.1, "metarType": "METAR", "rawOb": "METAR WSSS 010600Z ..."},
        {"obsTime": 200, "temp": 33.9, "metarType": "SPECI", "rawOb": "SPECI WSSS 010612Z ..."},
        {"obsTime": 300, "temp": 31.4, "rawOb": "WSSS 010630Z ..."},  # unlabelled = routine
    ]

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return rows

    monkeypatch.setattr(metar_client.requests, "get", lambda *a, **kw: R())
    assert metar_client.fetch_metars("WSSS", hours=6) == [(100, 30.1), (300, 31.4)]
