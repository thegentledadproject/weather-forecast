"""
ev_engine.save_ev_snapshot() writes data/ev_latest_<ICAO>.json and OVERWRITES
it every cycle, so nothing survives to say what the model believed at the
moment a day was priced. promotion_dossier.live_calibration() consequently
scores CLOSED POSITIONS only -- the buckets where the model and the market
disagreed enough to trade -- which answers "does the model win where it thinks
it wins", not "is the model calibrated". config.py's calibration_vs_market
comment names this constraint directly and routes around it via the backtest.

These tests pin the retention side of the fix: every bucket of every cycle is
kept, dated, with nothing recomputed, so a later full-book scorer has a
point-in-time record to read off the live book.
"""
import json
import sqlite3
from datetime import date

import pytest

import config
import ev_engine
import storage
from models import EVResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    return tmp_path


def _ev(bucket_c, side, model_prob, market_price, **kw):
    """One EVResult shaped as compute_ev_table() builds them."""
    raw_edge = None if market_price is None else model_prob - market_price
    return EVResult(
        station_icao=kw.pop("station_icao", "WSSS"),
        target_date=kw.pop("target_date", date(2026, 9, 3)),
        bucket_c=bucket_c,
        side=side,
        model_prob=model_prob,
        market_price=market_price,
        raw_edge=raw_edge,
        estimated_slippage_pct=kw.pop("estimated_slippage_pct", 0.02),
        fee_rate_pct=kw.pop("fee_rate_pct", 0.0325),
        net_ev_per_dollar=kw.pop("net_ev_per_dollar", None),
        spread_source=kw.pop("spread_source", "measured_error"),
        market_bid=kw.pop("market_bid", None),
        notes=kw.pop("notes", ""),
    )


def test_saved_snapshot_rows_round_trip(db):
    storage.save_ev_snapshot_rows(
        "WSSS", date(2026, 9, 3), "2026-09-03T05:10:00+00:00",
        [_ev(31, "YES", model_prob=0.42, market_price=0.35)],
    )

    rows = storage.load_ev_snapshots("WSSS", date(2026, 9, 3))

    assert len(rows) == 1
    assert rows[0]["bucket_c"] == 31
    assert rows[0]["side"] == "YES"
    assert rows[0]["model_prob"] == 0.42
    assert rows[0]["market_price"] == 0.35
    assert rows[0]["generated_at"] == "2026-09-03T05:10:00+00:00"


def test_two_cycles_on_one_day_both_persist(db):
    """
    The point of the table. save_ensemble_spread REPLACES per station-day
    because it answers "what was the day priced from"; this answers "what did
    the model believe at each decision", and the 05:10 belief is not
    recoverable from the 06:10 row.
    """
    storage.save_ev_snapshot_rows(
        "WSSS", date(2026, 9, 3), "2026-09-03T05:10:00+00:00",
        [_ev(31, "YES", model_prob=0.42, market_price=0.35)],
    )
    storage.save_ev_snapshot_rows(
        "WSSS", date(2026, 9, 3), "2026-09-03T06:10:00+00:00",
        [_ev(31, "YES", model_prob=0.47, market_price=0.39)],
    )

    rows = storage.load_ev_snapshots("WSSS", date(2026, 9, 3))

    assert [r["generated_at"] for r in rows] == [
        "2026-09-03T05:10:00+00:00",
        "2026-09-03T06:10:00+00:00",
    ]
    assert [r["model_prob"] for r in rows] == [0.42, 0.47]


def test_row_carries_the_whole_ev_payload(db):
    """
    P0-1 breaks its reliability table out BY spread_source and BY price
    decile, so a row that keeps only the probability and the price cannot
    answer the question the table exists for.
    """
    storage.save_ev_snapshot_rows(
        "WSSS", date(2026, 9, 3), "2026-09-03T05:10:00+00:00",
        [_ev(31, "YES", model_prob=0.42, market_price=0.35,
             estimated_slippage_pct=0.018, fee_rate_pct=0.0325,
             net_ev_per_dollar=0.147, spread_source="ensemble",
             market_bid=0.31, notes="thin book")],
    )

    row = storage.load_ev_snapshots("WSSS", date(2026, 9, 3))[0]

    assert row["raw_edge"] == pytest.approx(0.07)
    assert row["slippage_pct"] == 0.018
    assert row["fee_rate_pct"] == 0.0325
    assert row["net_ev_per_dollar"] == 0.147
    assert row["spread_source"] == "ensemble"
    assert row["market_bid"] == 0.31
    assert row["notes"] == "thin book"


def test_unpriced_bucket_is_still_recorded(db):
    """
    compute_ev_table() emits an EVResult with no price for a bucket the book
    never quoted. A full-book scorer needs that row: "the model had a view and
    no market existed" is a different fact from "the bucket was not listed".
    """
    storage.save_ev_snapshot_rows(
        "WSSS", date(2026, 9, 3), "2026-09-03T05:10:00+00:00",
        [_ev(29, "YES", model_prob=0.04, market_price=None,
             notes="no price available")],
    )

    row = storage.load_ev_snapshots("WSSS", date(2026, 9, 3))[0]

    assert row["bucket_c"] == 29
    assert row["model_prob"] == 0.04
    assert row["market_price"] is None
    assert row["raw_edge"] is None


# ---------------------------------------------------------------------------
# ev_engine.save_ev_snapshot: the dashboard handoff must not change shape
# ---------------------------------------------------------------------------

@pytest.fixture
def engine_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _snapshot_json(tmp_path, station_icao="WSSS"):
    return json.loads((tmp_path / f"ev_latest_{station_icao}.json").read_text())


def test_save_ev_snapshot_also_records_dated_rows(engine_db):
    ev_engine.save_ev_snapshot("WSSS", [
        _ev(31, "YES", model_prob=0.42, market_price=0.35),
        _ev(31, "NO", model_prob=0.58, market_price=0.66),
    ])

    rows = storage.load_ev_snapshots("WSSS", date(2026, 9, 3))

    assert [(r["bucket_c"], r["side"]) for r in rows] == [(31, "NO"), (31, "YES")]


def test_json_handoff_still_holds_only_the_latest_cycle(engine_db):
    """
    The file is the dashboard's handoff and a dashboard only wants the latest.
    Retention is additive: the file keeps overwriting, the table accumulates.
    """
    ev_engine.save_ev_snapshot("WSSS", [_ev(31, "YES", model_prob=0.42, market_price=0.35)])
    ev_engine.save_ev_snapshot("WSSS", [_ev(31, "YES", model_prob=0.47, market_price=0.39)])

    payload = _snapshot_json(engine_db)

    assert len(payload["results"]) == 1
    assert payload["results"][0]["model_prob"] == 0.47
    assert len(storage.load_ev_snapshots("WSSS", date(2026, 9, 3))) == 2


def test_db_failure_does_not_break_the_cycle(engine_db, monkeypatch, capsys):
    """
    ev_engine.py's existing rule for the JSON write: "the EV table drives
    trading, the snapshot only drives reporting, so a disk error here must
    never break the cycle." A DB error inherits it.
    """
    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "save_ev_snapshot_rows", boom)

    ev_engine.save_ev_snapshot("WSSS", [_ev(31, "YES", model_prob=0.42, market_price=0.35)])

    assert _snapshot_json(engine_db)["results"][0]["model_prob"] == 0.42
    assert "database is locked" in capsys.readouterr().out


def test_empty_cycle_writes_the_file_but_no_rows(engine_db):
    """
    "computed at 05:01 and found nothing" and "never computed" are different
    facts for the dashboard -- but a row needs a bucket to be keyed by, and an
    empty table carries no target_date to file one under.
    """
    ev_engine.save_ev_snapshot("WSSS", [])

    assert _snapshot_json(engine_db)["results"] == []
    assert storage.load_ev_snapshots("WSSS", date(2026, 9, 3)) == []
