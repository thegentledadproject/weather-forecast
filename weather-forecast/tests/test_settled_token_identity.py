"""
storage.load_settled_live_tokens() returned token_id -> (size_shares,
exit_price), and every downstream message could only print a token-id
prefix -- "5051499713... won at 1.00" -- which identifies nothing to a
human. The station, target date, bucket and side were sitting on the same
row the whole time.

This widens the value to a small record carrying that identity, WITHOUT
changing the meaning of the map or the closed_resolution-only filter --
that filter is load-bearing against the 2026-08-22 halt (see
test_settled_token_wiring.py), so it is re-asserted here against the new
return shape rather than assumed to still hold.
"""
import sqlite3
from datetime import date

import pytest

import config
from models import SettledToken


HELD_TOKEN = "5712315774911947" + "0" * 20
SOLD_TOKEN = "7258321014310216" + "0" * 20


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    import storage

    storage.load_open_positions()
    return storage


def _insert(storage, position_id, token_id, status, exit_price,
            shares=9.181817, execution_mode="live", is_paper=0,
            station="WSSS", target_date="2026-08-21", bucket_c=32, side="YES"):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO positions (position_id, station_icao, target_date, "
            "bucket_c, side, entry_price, size_usd, entry_time, status, "
            "high_water_mark, exit_price, token_id, is_paper, size_shares, "
            "execution_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, station, target_date, bucket_c, side, 0.11, 1.01,
             "2026-08-20T21:40:54+00:00", status, 0.11, exit_price, token_id,
             is_paper, shares, execution_mode),
        )


def test_the_record_carries_station_date_bucket_and_side(db):
    _insert(db, "WSSS:a", HELD_TOKEN, "closed_resolution", 1.0, shares=5.0,
            station="WSSS", target_date="2026-08-20", bucket_c=32, side="NO")

    tokens = db.load_settled_live_tokens()

    assert tokens == {
        HELD_TOKEN: SettledToken(
            station_icao="WSSS", target_date=date(2026, 8, 20), bucket_c=32,
            side="NO", size_shares=5.0, exit_price=1.0,
        )
    }


def test_a_stop_loss_row_is_still_not_returned(db):
    """The closed_resolution-only filter must survive the widening."""
    _insert(db, "WSSS:s", SOLD_TOKEN, "closed_stop_loss", 0.27)
    assert db.load_settled_live_tokens() == {}


def test_a_paper_resolution_is_still_not_returned(db):
    _insert(db, "WSSS:p", HELD_TOKEN, "closed_resolution", 0.0,
            execution_mode="paper", is_paper=1)
    assert db.load_settled_live_tokens() == {}


def test_a_row_with_no_token_id_is_still_skipped(db):
    _insert(db, "WSSS:n", None, "closed_resolution", 0.0)
    assert db.load_settled_live_tokens() == {}


def test_no_rows_is_still_an_empty_dict(db):
    assert db.load_settled_live_tokens() == {}
