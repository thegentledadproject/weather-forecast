"""
Two no-network tests for fast_recorder.py's pure recording functions:
record_books (dedup on unchanged top-of-book, max/min picked from an
unsorted book) and record_metars (dedup on (icao, obs_time), first_seen
kept from the first poll).
"""
import fast_recorder


def test_record_books_dedup_and_top_of_book(tmp_path):
    conn = fast_recorder._connect(tmp_path / "fr.sqlite3")
    meta = {
        "tokenA": ("WSSS", "2026-09-28", 30, "YES"),
        "tokenB": ("WSSS", "2026-09-28", 31, "NO"),
    }
    last = {}

    books_1 = [
        {
            "asset_id": "tokenA",
            "timestamp": "1000",
            "bids": [{"price": "0.40", "size": "10"}, {"price": "0.55", "size": "5"}],
            "asks": [{"price": "0.70", "size": "8"}, {"price": "0.60", "size": "12"}],
            "last_trade_price": "0.58",
        },
        {
            "asset_id": "tokenB",
            "timestamp": "1000",
            "bids": [{"price": "0.20", "size": "3"}],
            "asks": [{"price": "0.30", "size": "4"}],
            "last_trade_price": "0.25",
        },
    ]
    n1 = fast_recorder.record_books(conn, books_1, meta, last, "2026-09-28T00:00:00Z")
    assert n1 == 2

    rows = conn.execute("SELECT bucket, side, best_bid, best_ask FROM books WHERE bucket=30").fetchall()
    assert rows == [(30, "YES", 0.55, 0.60)]  # max bid, min ask -- not list order

    # Second poll: tokenA unchanged, tokenB's ask moved.
    books_2 = [
        books_1[0],
        {
            "asset_id": "tokenB",
            "timestamp": "1015",
            "bids": [{"price": "0.20", "size": "3"}],
            "asks": [{"price": "0.35", "size": "4"}],
            "last_trade_price": "0.25",
        },
    ]
    n2 = fast_recorder.record_books(conn, books_2, meta, last, "2026-09-28T00:00:15Z")
    assert n2 == 1

    total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    assert total == 3


def test_record_metars_dedup_and_first_seen(tmp_path):
    conn = fast_recorder._connect(tmp_path / "fr.sqlite3")

    rows_1 = [
        {"icaoId": "WSSS", "obsTime": 100, "temp": 28.0, "receiptTime": "t1"},
        {"icaoId": "WSSS", "obsTime": 200, "temp": None, "receiptTime": "t2"},
    ]
    n1 = fast_recorder.record_metars(conn, rows_1, "2026-09-28T00:00:00Z")
    assert n1 == 1  # the temp=None row is skipped

    rows_2 = [
        {"icaoId": "WSSS", "obsTime": 100, "temp": 28.0, "receiptTime": "t1"},  # duplicate
        {"icaoId": "WSSS", "obsTime": 300, "temp": 29.0, "receiptTime": "t3"},
    ]
    n2 = fast_recorder.record_metars(conn, rows_2, "2026-09-28T00:00:30Z")
    assert n2 == 1

    first_seen = conn.execute(
        "SELECT first_seen FROM metar WHERE icao='WSSS' AND obs_time=100"
    ).fetchone()[0]
    assert first_seen == "2026-09-28T00:00:00Z"

    total = conn.execute("SELECT COUNT(*) FROM metar").fetchone()[0]
    assert total == 2
