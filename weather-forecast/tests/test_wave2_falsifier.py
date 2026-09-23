"""The Wave 2 reads (i)-(iv) and the stop condition, on a fixture DB, through a read-only connection."""
import sqlite3
from datetime import date, timedelta

import pytest

import calibration
import config
import storage
import wave2_falsifier
from models import EntryDecision, ObservedReading, PointForecast, Position

BOUNDARY = "2026-09-21"
B = date(2026, 9, 21)


def _forecast(day, temp, local_hour):
    fetched = config.local_day_bounds_utc("WSSS", day)[0] + timedelta(hours=local_hour)
    return PointForecast(station_icao="WSSS", source="open_meteo_ecmwf", target_date=day,
                         max_temp_c=temp, fetched_at=fetched.isoformat())


def _paper(pid, day, bucket):
    return Position(
        position_id=pid, station_icao="WSSS", target_date=day, bucket_c=bucket, side="YES",
        entry_price=0.30, size_usd=10.0, entry_time=f"{day}T22:00:00+00:00", status="open",
        high_water_mark=0.30, is_paper=True, execution_mode="paper",
    )


def _decision(rule_id, admission_edge):
    return EntryDecision(
        station_icao="WSSS", target_date=B, bucket_c=32, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=0.0,
        available_depth_usd=None, slippage_at_size_pct=None, net_ev_at_size=None,
        approved=False, reason="r", station_maturity="mature", entry_price=0.05,
        rule_id=rule_id, admission_edge=admission_edge,
    )


def _seed_paper(days, bucket, prefix):
    for i, day in enumerate(days):
        pid = f"{prefix}{i}"
        storage.open_position(_paper(pid, day, bucket))
        storage.close_position(pid, 0.30, f"{day}T10:00:00+00:00", "closed_resolution", "t")
        storage.save_settled_bucket("WSSS", day, 32, 30, 34, "test")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "DB_PATH", path)
    storage.migrate()
    # (i)/(ii): six dates, truth 32.0. Morning (05:00) forecasts err -1,0,+1,
    # -1,0,+1 -> sd sqrt(4/5) = 0.894; a 14:00 row at 32.0 on every day
    # halves the all-day error -> sd sqrt(1/5) = 0.447.
    source = config.get_station("WSSS").resolution_grade_source
    for i, err in enumerate([-1.0, 0.0, 1.0, -1.0, 0.0, 1.0]):
        day = date(2026, 9, 10) + timedelta(days=i)
        storage.save_observation(ObservedReading(station_icao="WSSS", target_date=day,
                                                 max_temp_c=32.0, source=source))
        storage.save_forecast(_forecast(day, 32.0 + err, 5))
        storage.save_forecast(_forecast(day, 32.0, 14))
    # (iv)
    storage.record_entry_decisions([_decision("0a2", -0.05), _decision("0a2", 0.01),
                                    _decision("kelly_nonpositive", -0.02)],
                                   book="paper", cycle_ts="2026-09-22T05:00:10+00:00", config_sha="s")
    storage.record_entry_decisions([_decision("kelly_nonpositive", -0.02)],
                                   book="paper", cycle_ts="2026-09-15T05:00:10+00:00", config_sha="s")
    return path


@pytest.fixture
def worse_after(db):
    """Before: three winning station-days (bucket 32 settles). After: three
    losing ones (bucket 31). Held return +233% -> -100%: STOP."""
    _seed_paper([B - timedelta(days=d) for d in (3, 2, 1)], 32, "w")
    _seed_paper([B + timedelta(days=d) for d in (0, 1, 2)], 31, "l")
    return db


@pytest.fixture
def same_after(db):
    _seed_paper([B - timedelta(days=d) for d in (3, 2, 1)], 32, "w")
    _seed_paper([B + timedelta(days=d) for d in (0, 1, 2)], 32, "x")
    return db


def test_read_i_morning_vs_all_day_sd(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    sd = out["error_sd_by_station"]["WSSS"]
    assert sd["morning"]["n"] == 6 and sd["all_day"]["n"] == 6
    assert sd["morning"]["sd"] == pytest.approx(0.8944, abs=1e-3)
    assert sd["all_day"]["sd"] == pytest.approx(0.4472, abs=1e-3)


def test_read_ii_priced_spread_and_the_floor_pin(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    ps = out["priced_spread_by_station"]["WSSS"]
    assert ps["source"] == "measured_error"          # 6 pairs: under the 15-residual RMSE floor
    assert ps["measured"] == pytest.approx(0.8944, abs=1e-3)
    assert ps["priced"] == 0.89                      # naive tier: 0.89 exceeds the 0.70 floor it keeps (2b ruling), so unclamped either way
    assert ps["pinned_to_floor"] is False


def test_read_ii_reports_unmeasured_stations_as_none(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["priced_spread_by_station"]["EDDM"]["source"] is None
    assert out["priced_spread_by_station"]["EDDM"]["pinned_to_floor"] is None


def test_read_ii_naive_tier_keeps_the_confidence_floor(db, monkeypatch):
    """Only corrected_error is exempt from SPREAD_FLOOR_C (2b). The naive
    measured_error tier -- as few as 5 pairs, below the width gate's own
    visibility -- must still price at the 0.70 floor when its raw sd is
    under it. This is exactly the gate-blind case read (ii) exists to
    expose."""
    monkeypatch.setattr(calibration, "corrected_error_rmse_from_dated", lambda dated: (None, 0))
    monkeypatch.setattr(calibration, "measured_error_spread_from_errors", lambda errors: (0.45, 6))
    out = wave2_falsifier.run(db, BOUNDARY)
    ps = out["priced_spread_by_station"]["WSSS"]
    assert ps["source"] == "measured_error"
    assert ps["measured"] == 0.45
    assert ps["priced"] == 0.70
    assert ps["pinned_to_floor"] is True


def test_read_ii_corrected_tier_is_exempt_from_the_floor(db, monkeypatch):
    """corrected_error prices as-is between MEASURED_SPREAD_MIN_C and the
    regional ceiling -- NOT floored to SPREAD_FLOOR_C, unlike the naive
    tier above at the same raw value."""
    monkeypatch.setattr(calibration, "corrected_error_rmse_from_dated", lambda dated: (0.45, 20))
    out = wave2_falsifier.run(db, BOUNDARY)
    ps = out["priced_spread_by_station"]["WSSS"]
    assert ps["source"] == "corrected_error"
    assert ps["measured"] == 0.45
    assert ps["priced"] == 0.45
    assert ps["pinned_to_floor"] is False


def test_read_iii_stop_fires_when_after_is_worse_beyond_the_ci(worse_after):
    out = wave2_falsifier.run(worse_after, BOUNDARY)
    assert out["held_before"]["n"] == 3 and out["held_after"]["n"] == 3
    assert out["held_before"]["return_pct"] == pytest.approx(0.70 / 0.30)
    assert out["held_after"]["return_pct"] == pytest.approx(-1.0)
    lo, hi = out["held_difference_ci"]
    assert hi < 0
    assert out["stop_verdict"].startswith("STOP")


def test_read_iii_holds_when_nothing_moved(same_after):
    out = wave2_falsifier.run(same_after, BOUNDARY)
    assert out["held_difference_ci"] == (0.0, 0.0)
    assert out["stop_verdict"] == "holding"


def test_read_iii_has_no_verdict_without_rows_on_both_sides(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["held_before"]["n"] == 0
    assert out["stop_verdict"] == "NO VERDICT"


def test_read_iv_counts_negative_edge_refusals_by_rule(db):
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["refusals"]["0a2_negative_after"] == 1
    assert out["refusals"]["0a2_negative_before"] == 0
    assert out["refusals"]["kelly_negative_after"] == 1
    assert out["refusals"]["kelly_negative_before"] == 1


def test_read_iv_excludes_paper_shadow_book(db):
    """The shadow book's refusals are not real trades and must not inflate
    the count -- same exclusion rule as wave1_falsifier's approval rate."""
    storage.record_entry_decisions(
        [_decision("0a2", -0.09)],
        book="paper_shadow", cycle_ts="2026-09-22T05:00:10+00:00", config_sha="s",
    )
    out = wave2_falsifier.run(db, BOUNDARY)
    assert out["refusals"]["0a2_negative_after"] == 1  # unchanged by the shadow row above


def test_the_connection_is_read_only(db, monkeypatch):
    calls = []
    real = sqlite3.connect

    def _spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    wave2_falsifier.run(db, BOUNDARY)
    assert calls, "expected at least one sqlite3.connect call"
    for a, kw in calls:
        assert kw.get("uri") is True and "mode=ro" in a[0]


def test_write_attempt_raises_on_the_ro_connection(db):
    con = wave2_falsifier._ro_connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO entry_decisions (cycle_ts) VALUES ('x')")
    finally:
        con.close()


def test_a_pre_wave1_schema_is_refused_with_one_line(tmp_path, capsys):
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE positions (station_icao TEXT)")
    con.commit()
    con.close()
    assert wave2_falsifier.main(["--boundary", BOUNDARY, "--db", str(path)]) == 1
    assert "schema predates" in capsys.readouterr().out


def test_main_prints_the_verdict(worse_after, capsys):
    assert wave2_falsifier.main(["--boundary", BOUNDARY, "--db", worse_after]) == 0
    out = capsys.readouterr().out
    assert "STOP CONDITION" in out and "STOP" in out
    assert "WSSS" in out
