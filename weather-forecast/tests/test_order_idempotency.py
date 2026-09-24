"""live_order_attempts carries a deterministic client_order_key under a UNIQUE
index: the same (kind, station, date, bucket, side, cycle, attempt) can be
submitted and recorded once. A repeat is a no-op plus a warning."""
import sqlite3
from datetime import date, datetime, timezone

import pytest

import config
import executor
import storage
from clients import wallet_client
from models import EntryDecision

TODAY = datetime.now(timezone.utc).date()
CYCLE = "2026-09-24T05:00:10+00:00"


def test_key_is_deterministic_and_discriminating():
    k = storage.client_order_key("entry", "WSSS", date(2026, 9, 24), 32, "YES", CYCLE)
    assert k == storage.client_order_key("entry", "WSSS", "2026-09-24", 32, "YES", CYCLE)
    assert k != storage.client_order_key("entry", "WSSS", "2026-09-24", 32, "NO", CYCLE)
    assert k != storage.client_order_key("entry", "WSSS", "2026-09-24", 32, "YES", CYCLE, attempt=1)
    assert k != storage.client_order_key("entry", "WSSS", "2026-09-24", 32, "YES", "2026-09-24T05:10:10+00:00")


def test_duplicate_insert_is_a_noop_with_a_warning(tmp_db, capsys):
    k = storage.client_order_key("entry", "WSSS", "2026-09-24", 32, "YES", CYCLE)
    assert storage.record_live_order_attempt("entry", "WSSS", "filled", client_order_key=k) is True
    assert storage.record_live_order_attempt("entry", "WSSS", "filled", client_order_key=k) is False
    assert "duplicate" in capsys.readouterr().out.lower()
    assert storage.live_order_key_exists(k)
    # Unkeyed rows (refusals, exits, history) never collide.
    assert storage.record_live_order_attempt("entry", "WSSS", "refused") is True
    assert storage.record_live_order_attempt("entry", "WSSS", "refused") is True
    assert len(storage.load_live_order_attempts()) == 3


def test_migrate_adds_the_key_to_an_old_table_with_rows(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE live_order_attempts (ts TEXT NOT NULL, kind TEXT NOT NULL, "
              "station_icao TEXT NOT NULL, target_date TEXT, bucket_c INTEGER, side TEXT, "
              "notional_usd REAL, size_shares REAL, limit_price REAL, outcome TEXT NOT NULL, "
              "order_id TEXT, detail TEXT)")
    row = ("2026-09-01T05:00:00+00:00", "entry", "WSSS", "2026-09-01", 32, "YES", 1, 5, 0.2, "killed", None, "")
    c.executemany("INSERT INTO live_order_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [row, row])
    c.commit()
    c.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    storage.migrate()
    storage.migrate()  # idempotent
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*), COUNT(client_order_key) FROM live_order_attempts").fetchone() == (2, 0)
    assert c.execute("SELECT \"unique\" FROM pragma_index_list('live_order_attempts') "
                     "WHERE name = 'ux_loa_client_key'").fetchone() == (1,)
    c.close()


def _decision():
    return EntryDecision(
        station_icao="WSSS", target_date=TODAY, bucket_c=32, side="YES",
        kelly_fraction_raw=0.4, kelly_fraction_applied=0.1, recommended_size_usd=1.0,
        available_depth_usd=1000.0, slippage_at_size_pct=0.01, net_ev_at_size=0.30,
        approved=True, reason="test", station_maturity="mature", entry_price=0.30,
        token_id="TOK",
    )


@pytest.fixture
def live_path(monkeypatch, tmp_db):
    monkeypatch.setattr(executor, "EXECUTION_MODE", {"WSSS": "live"})
    spec = wallet_client.OrderSpec(ok=True, token_id="TOK", side="BUY", limit_price=0.30,
                                   expected_price=0.30, size_shares=5.0, notional_usd=1.5)
    monkeypatch.setattr(wallet_client, "build_entry_order", lambda **kw: spec)
    monkeypatch.setattr(executor, "_price_drift_ok", lambda a, b: (True, ""))
    monkeypatch.setattr(executor, "_resolved_size_ok", lambda s, d, out=None: (True, ""))
    monkeypatch.setattr(executor, "_live_budget_breach", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_live_brake", lambda: None)
    submitted = []
    monkeypatch.setattr(wallet_client, "submit_order",
                        lambda spec, live=False: submitted.append(spec) or
                        wallet_client.OrderResult(submitted=True, filled=False, simulated=False,
                                                  spec=spec, error="stub"))
    return submitted


def test_same_decision_same_cycle_submits_once(live_path, capsys):
    executor.open_position(_decision(), cycle_ts=CYCLE)
    executor.open_position(_decision(), cycle_ts=CYCLE)
    assert len(live_path) == 1
    assert "duplicate" in capsys.readouterr().out.lower()
    rows = storage.load_live_order_attempts()
    assert [r["outcome"] for r in rows] == ["killed"]
    assert rows[0]["client_order_key"] == storage.client_order_key(
        "entry", "WSSS", TODAY, 32, "YES", CYCLE)


def test_a_new_cycle_is_a_new_attempt(live_path):
    executor.open_position(_decision(), cycle_ts=CYCLE)
    executor.open_position(_decision(), cycle_ts="2026-09-24T05:10:10+00:00")
    assert len(live_path) == 2


def test_an_unreadable_key_check_fails_closed(live_path, monkeypatch):
    monkeypatch.setattr(storage, "live_order_key_exists",
                        lambda k: (_ for _ in ()).throw(RuntimeError("locked")))
    executor.open_position(_decision(), cycle_ts=CYCLE)
    assert live_path == []


def test_no_cycle_means_no_dedup(live_path):
    """manual paths pass no cycle_ts: unkeyed, exactly as before."""
    executor.open_position(_decision())
    executor.open_position(_decision())
    assert len(live_path) == 2
