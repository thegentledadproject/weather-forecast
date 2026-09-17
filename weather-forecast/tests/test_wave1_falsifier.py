# tests/test_wave1_falsifier.py
"""The four Wave 1 falsifier checks, on a fixture database, through a read-only connection."""
import sqlite3
from datetime import date

import pytest

import config
import storage
import wave1_falsifier
from models import EntryDecision, Position

DEPLOY = "2026-09-19T05:00:00+00:00"


def _decision(bucket, approved, rule_id):
    return EntryDecision(
        station_icao="WSSS", target_date=date(2026, 9, 19), bucket_c=bucket, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=1.0 if approved else 0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=approved, reason="r", station_maturity="mature", entry_price=0.3, rule_id=rule_id,
    )


def _position(pid, entry_time, calibrated_prob, source, station="WSSS"):
    return Position(
        position_id=pid, station_icao=station, target_date=date(2026, 9, 19), bucket_c=32, side="YES",
        entry_price=0.3, size_usd=1.0, entry_time=entry_time, status="open", is_paper=False,
        execution_mode="live", calibrated_prob=calibrated_prob, calibration_source=source,
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "DB_PATH", path)
    storage._connect().close()
    storage.record_entry_decisions([_decision(32, True, "approved"), _decision(33, False, "0a2")],
                                   book="live", cycle_ts="2026-09-19T05:00:10+00:00", config_sha="s")
    storage.record_entry_decisions([_decision(32, False, "0b"), _decision(33, False, "0a2")],
                                   book="live", cycle_ts="2026-09-19T05:10:10+00:00", config_sha="s")
    storage.record_entry_decisions([_decision(32, True, "approved")],
                                   book="paper_shadow", cycle_ts="2026-09-19T05:00:10+00:00", config_sha="s")
    storage.open_position(_position("before-1", "2026-09-15T05:00:00+00:00", None, None))
    storage.open_position(_position("after-ok", "2026-09-19T05:30:00+00:00", 0.4, "pooled_isotonic"))
    storage.open_position(_position("after-uncal", "2026-09-19T05:31:00+00:00", None, "uncalibrated"))
    storage.open_position(_position("after-gap", "2026-09-19T05:32:00+00:00", None, "station_isotonic"))
    return path


def test_the_four_checks(db):
    out = wave1_falsifier.run(db, DEPLOY)

    assert out["refusals_total"] == 3
    assert out["refusals_by_cycle"] == [("2026-09-19T05:00:10+00:00", 1), ("2026-09-19T05:10:10+00:00", 2)]
    assert out["calibration_gap_rows"] == 1            # after-gap only; pre-deploy NULL source is not a gap
    assert out["paper_shadow_rows"] == 1
    assert out["entries_per_station_day_before"] == {"WSSS": {"2026-09-15": 1}}
    assert out["entries_per_station_day_after"] == {"WSSS": {"2026-09-19": 3}}


def test_the_connection_is_read_only(db, monkeypatch):
    calls = []
    real = sqlite3.connect

    def _spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(wave1_falsifier.sqlite3, "connect", _spy)
    wave1_falsifier.run(db, DEPLOY)
    assert calls and calls[0][0][0].endswith("?mode=ro") and calls[0][1].get("uri") is True


def test_write_attempt_raises_on_the_ro_connection(db):
    con = wave1_falsifier._ro_connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO entry_decisions (cycle_ts) VALUES ('x')")
    finally:
        con.close()


def test_main_prints_every_check(db, capsys):
    wave1_falsifier.main(["--db", db, "--deploy-ts", DEPLOY])
    out = capsys.readouterr().out
    for label in ("refused decisions", "calibration gap", "paper_shadow rows", "entries per station-day"):
        assert label in out
