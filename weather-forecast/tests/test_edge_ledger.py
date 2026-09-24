"""edge_ledger.py (plan P10A): per-trade edge columns and the predeclared
bucket test, on a hand-built database so every number is checkable."""
import sqlite3
from datetime import date

import pytest

import edge_ledger
import risk_manager

POS_DDL = """CREATE TABLE positions (position_id TEXT, station_icao TEXT, target_date TEXT,
  bucket_c INTEGER, side TEXT, entry_price REAL, size_usd REAL, entry_time TEXT, status TEXT,
  exit_price REAL, execution_mode TEXT, model_prob REAL, calibrated_prob REAL,
  entry_fee_per_share REAL)"""


def _db(tmp_path, with_decisions=True, p_robust=False):
    path = tmp_path / "ledger.sqlite3"
    c = sqlite3.connect(path)
    c.execute(POS_DDL)
    c.execute("CREATE TABLE settled_buckets (station_icao TEXT, target_date TEXT, bucket_c INTEGER)")
    c.execute("CREATE TABLE ev_snapshots (station_icao TEXT, target_date TEXT, bucket_c INTEGER, side TEXT,"
              " generated_at TEXT, model_prob REAL, market_price REAL, slippage_pct REAL)")
    if with_decisions:
        extra = ", p_robust REAL" if p_robust else ""
        c.execute("CREATE TABLE entry_decisions (cycle_ts TEXT, station_icao TEXT, target_date TEXT,"
                  " bucket_c INTEGER, side TEXT, book TEXT, approved INTEGER, entry_price REAL,"
                  f" model_prob REAL, calibrated_prob REAL{extra})")
    fee = risk_manager.taker_fee_per_share
    # 1: live YES won; decided at ask 0.40, filled at 0.42.
    c.execute("INSERT INTO positions VALUES ('a','WSSS','2026-09-21',30,'YES',0.42,4.2,"
              "'2026-09-20T21:00:05+00:00','closed_resolution',1.0,'live',0.55,0.50,?)", (fee(0.42),))
    # 2: paper NO lost (settled bucket == its bucket), sold earlier at 0.30.
    c.execute("INSERT INTO positions VALUES ('b','WSSS','2026-09-10',31,'NO',0.60,6.0,"
              "'2026-09-09T21:00:05+00:00','closed_stop_loss',0.30,'paper',0.62,NULL,?)", (fee(0.60),))
    # 3: still open -> excluded.
    c.execute("INSERT INTO positions VALUES ('c','WSSS','2026-09-25',30,'YES',0.10,1.0,"
              "'2026-09-24T21:00:05+00:00','open',NULL,'paper',0.20,NULL,?)", (fee(0.10),))
    c.execute("INSERT INTO settled_buckets VALUES ('WSSS','2026-09-21',30)")
    c.execute("INSERT INTO settled_buckets VALUES ('WSSS','2026-09-10',31)")
    c.execute("INSERT INTO ev_snapshots VALUES ('WSSS','2026-09-10',31,'NO','2026-09-09T21:00:01+00:00',"
              "0.62,0.60,0.05)")
    if with_decisions:
        vals = "('2026-09-20T21:00:00+00:00','WSSS','2026-09-21',30,'YES','live',1,0.40,0.55,0.50" + (
            ",0.47)" if p_robust else ")")
        c.execute(f"INSERT INTO entry_decisions VALUES {vals}")
        # A LATER cycle's decision for the same key must not be picked (1:N pairing).
        later = "('2026-09-20T21:10:00+00:00','WSSS','2026-09-21',30,'YES','live',1,0.20,0.90,0.90" + (
            ",0.9)" if p_robust else ")")
        c.execute(f"INSERT INTO entry_decisions VALUES {later}")
    c.commit()
    c.close()
    return path


def test_per_trade_edges(tmp_path):
    rows, skipped = edge_ledger.load_ledger(_db(tmp_path, p_robust=True))
    assert skipped == {"unsettled": 1}
    by = {r["position_id"]: r for r in rows}
    a, b = by["a"], by["b"]
    fee = risk_manager.taker_fee_per_share
    assert a["ask"] == 0.40 and a["ask_source"] == "entry_decisions"
    assert a["raw_edge"] == pytest.approx(0.15)
    assert a["calibrated_edge"] == pytest.approx(0.10)
    assert a["robust_edge"] == pytest.approx(0.07)
    assert a["executable_edge"] == pytest.approx(0.15 - fee(0.40))
    assert a["filled_edge"] == pytest.approx(0.55 - 0.42 - fee(0.42))
    assert a["realized_edge"] == pytest.approx(1.0 - 0.42 - fee(0.42))
    assert a["target_date"] == date(2026, 9, 21)
    # b: slippage recorded on the snapshot (5% of notional).
    assert b["ask_source"] == "ev_snapshots"
    assert b["executable_edge"] == pytest.approx(0.02 - fee(0.60) - 0.05 * 0.60)
    assert b["outcome"] == 0.0
    assert b["exit_edge"] == pytest.approx(0.30 - 0.60 - fee(0.60))
    assert b["realized_edge"] == pytest.approx(0.0 - 0.60 - fee(0.60))
    assert b["robust_edge"] is None


def test_tolerates_missing_decision_table_and_p_robust(tmp_path):
    rows, _ = edge_ledger.load_ledger(_db(tmp_path, with_decisions=False))
    a = {r["position_id"]: r for r in rows}["a"]
    # No decision and no snapshot: the ask falls back to the fill, and says so.
    assert a["ask_source"] == "fill" and a["ask"] == 0.42 and a["robust_edge"] is None


def test_buckets_are_predeclared_and_half_open():
    assert [edge_ledger.bucket_label(e) for e in (-0.5, 0.0299, 0.03, 0.0999, 0.10, 0.2, 0.9)] == [
        "<0.03", "<0.03", "0.03-0.06", "0.06-0.10", "0.10-0.20", ">=0.20", ">=0.20"]


def test_slope_recovers_a_known_line_and_clusters_by_date():
    rows = [{"executable_edge": x / 100, "realized_edge": 2 * x / 100 + 0.01,
             "target_date": date(2026, 9, 1 + x % 7)} for x in range(30)]
    s = edge_ledger.slope(rows, "executable_edge", "realized_edge")
    assert s["slope"] == pytest.approx(2.0)
    assert s["ci"][0] == pytest.approx(2.0) and s["ci"][1] == pytest.approx(2.0)
    assert s["n_days"] == 7


def test_main_runs_read_only_on_a_file(tmp_path, capsys):
    path = _db(tmp_path)
    assert edge_ledger.main(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "<0.03" in out and "slope" in out
