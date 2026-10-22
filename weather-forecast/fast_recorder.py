"""
fast_recorder.py

PURPOSE
-------
Measure how fast Polymarket weather-market prices react to a new airport
METAR reading landing. This is pure measurement: it never places an order
and never touches config.DB_PATH (the live trading database). It has its
own SQLite file so nothing here can corrupt or contend with trading state.

WHAT IT RECORDS
----------------
- metar: every METAR observation the moment we first see it (first_seen is
  the wall-clock time WE polled it, not the observation time -- that gap is
  the thing this tool exists to measure downstream).
- books: order-book top-of-book snapshots, but only when the top of book
  actually changed since the last poll. Polymarket's /books endpoint
  returns bids/asks in no guaranteed order, so best_bid/best_ask are always
  taken as max(bid prices)/min(ask prices), never list position 0.

DEPENDENCIES
------------
requests (already a project dependency), sqlite3, standard library.
config.py, market_discovery.py (read-only use: station registry + token
discovery).
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

import config
import market_discovery

DB_PATH = config.DATA_DIR / "fast_recorder.sqlite3"

METAR_URL = "https://aviationweather.gov/api/data/metar"
BOOKS_URL = "https://clob.polymarket.com/books"
BOOKS_BATCH_SIZE = 200

TOKEN_MAP_REFRESH_SECONDS = 30 * 60
BOOK_POLL_SECONDS = 15
METAR_POLL_SECONDS = 30

# Self-stop: a measurement, not a service. ~2 weeks from the 2026-09-28 build;
# the loop exits (and systemd's Restart=on-failure leaves it down) after this.
STOP_AFTER_UTC = datetime(2026, 10, 13, tzinfo=timezone.utc)


def _connect(path=None) -> sqlite3.Connection:
    """Open the fast-recorder DB, creating its schema if absent. Separate
    database, own DDL -- same lazy-creation pattern as backtest/price_store.py."""
    conn = sqlite3.connect(path or DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar (
            icao TEXT NOT NULL,
            obs_time INTEGER NOT NULL,
            temp_c REAL NOT NULL,
            receipt_time TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            PRIMARY KEY (icao, obs_time)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            bucket INTEGER NOT NULL,
            side TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            book_ts INTEGER,
            best_bid REAL,
            bid_size REAL,
            best_ask REAL,
            ask_size REAL,
            last_trade REAL
        )
        """
    )
    # NO asset_id and NO index, on purpose: measured 2026-09-28 at ~45 changed
    # books per 15s poll, the 77-char token id plus an index made ~90MB/day
    # against 1.8GB free on the box. (icao, target_date, bucket, side) already
    # names the token; analysis runs on a pulled copy and can index there.
    return conn


def fast_recorder_stations():
    """Every registered station with a real, poll-able METAR feed -- excludes
    Hong Kong (resolution_grade_source="hko_daily_max"), collection-only there."""
    return [
        s for s in config.STATIONS.values()
        if s.resolution_grade_source == "metar_daily_max"
    ]


def _parse_levels(levels):
    """(price, size) floats for every level with a parseable price -- live
    /books responses have been observed to include a level with an empty
    string price/size, which float() rejects. Skip those, don't crash the
    whole poll over one bad level."""
    out = []
    for lvl in levels:
        try:
            out.append((float(lvl["price"]), float(lvl["size"])))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def top_of_book(book: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Best bid/ask + their sizes from one /books response entry. bids/asks
    are NOT guaranteed sorted -- best_bid is max price, best_ask is min
    price. An empty side (or one with no parseable level) is (None, None)."""
    bids = _parse_levels(book.get("bids") or [])
    asks = _parse_levels(book.get("asks") or [])

    best_bid = best_bid_size = None
    if bids:
        best_bid, best_bid_size = max(bids, key=lambda lvl: lvl[0])

    best_ask = best_ask_size = None
    if asks:
        best_ask, best_ask_size = min(asks, key=lambda lvl: lvl[0])

    return best_bid, best_bid_size, best_ask, best_ask_size


def build_token_meta(stations) -> Dict[str, tuple]:
    """asset_id -> (icao, target_date_iso, bucket, "YES"/"NO") for every
    station's current token map. One station's discovery failing must not
    stop the rest, so each is wrapped individually."""
    meta = {}
    for station in stations:
        try:
            target_date = config.local_today(station)
            token_map = market_discovery.discover_token_map(
                station, target_date, station.bucket_min_c, station.bucket_max_c
            )
            for bucket, ids in token_map.items():
                yes_id = ids.get("yes_token_id")
                no_id = ids.get("no_token_id")
                if yes_id:
                    meta[yes_id] = (station.icao, target_date.isoformat(), bucket, "YES")
                if no_id:
                    meta[no_id] = (station.icao, target_date.isoformat(), bucket, "NO")
        except Exception as exc:
            print(f"[fast_recorder] token map failed for {station.icao}: {exc}")
    return meta


def fetch_books(asset_ids) -> list:
    """POST /books in batches of BOOKS_BATCH_SIZE. Returns the concatenated
    list of response dicts; a batch that fails is skipped, not fatal."""
    results = []
    asset_ids = list(asset_ids)
    for i in range(0, len(asset_ids), BOOKS_BATCH_SIZE):
        batch = asset_ids[i:i + BOOKS_BATCH_SIZE]
        payload = [{"token_id": tid} for tid in batch]
        resp = requests.post(BOOKS_URL, json=payload, timeout=15)
        resp.raise_for_status()
        results.extend(resp.json())
    return results


def record_books(conn, books, meta: Dict[str, tuple], last: Dict[str, tuple], seen_at: str) -> int:
    """Insert a books row per entry ONLY when (best_bid, bid_size, best_ask,
    ask_size) differs from the last recorded values for that asset_id.
    `last` is mutated in place (in-memory dedup cache)."""
    rows_inserted = 0
    for book in books:
        asset_id = book.get("asset_id")
        info = meta.get(asset_id)
        if info is None:
            continue
        icao, target_date, bucket, side = info

        best_bid, bid_size, best_ask, ask_size = top_of_book(book)
        current = (best_bid, bid_size, best_ask, ask_size)
        if last.get(asset_id) == current:
            continue
        last[asset_id] = current

        book_ts = book.get("timestamp")
        try:
            book_ts = int(book_ts) if book_ts is not None else None
        except (TypeError, ValueError):
            book_ts = None
        last_trade = book.get("last_trade_price")
        try:
            last_trade = float(last_trade) if last_trade else None
        except (TypeError, ValueError):
            last_trade = None

        conn.execute(
            """
            INSERT INTO books
                (icao, target_date, bucket, side, seen_at,
                 book_ts, best_bid, bid_size, best_ask, ask_size, last_trade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (icao, target_date, bucket, side, seen_at,
             book_ts, best_bid, bid_size, best_ask, ask_size, last_trade),
        )
        rows_inserted += 1
    return rows_inserted


def fetch_metars(icaos) -> list:
    """One GET for every station's ICAO at once. Rows with temp None are
    the caller's job to skip (see record_metars)."""
    resp = requests.get(
        METAR_URL,
        params={"ids": ",".join(icaos), "format": "json", "hours": 2},
        timeout=15,
        headers={"User-Agent": "polyweather/1.0"},
    )
    resp.raise_for_status()
    return resp.json()


def record_metars(conn, rows, first_seen: str) -> int:
    """Insert OR IGNORE per (icao, obs_time) -- first_seen is kept from
    whichever poll saw it first. Rows with temp None are skipped."""
    rows_inserted = 0
    for row in rows:
        temp = row.get("temp")
        if temp is None:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO metar (icao, obs_time, temp_c, receipt_time, first_seen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["icaoId"], int(row["obsTime"]), float(temp), row.get("receiptTime", ""), first_seen),
        )
        if cur.rowcount:
            rows_inserted += 1
    return rows_inserted


def run(seconds: Optional[int] = None, sleep=time.sleep, now=lambda: datetime.now(timezone.utc), db_path=None) -> None:
    """Single-threaded poll loop: token maps every 30 min, books every 15s,
    METARs every 30s. seconds=None runs forever. Each step is wrapped so one
    failing station/request never kills the loop."""
    conn = _connect(db_path)
    stations = fast_recorder_stations()
    icaos = [s.icao for s in stations]
    print(f"[fast_recorder] starting, {len(stations)} stations: {icaos}")

    meta: Dict[str, tuple] = {}
    last_books: Dict[str, tuple] = {}
    last_token_refresh = 0.0
    last_book_poll = 0.0
    last_metar_poll = 0.0

    start = time.monotonic()
    while (seconds is None or (time.monotonic() - start) < seconds) and now() < STOP_AFTER_UTC:
        t = time.monotonic()

        if t - last_token_refresh >= TOKEN_MAP_REFRESH_SECONDS or not meta:
            try:
                meta = build_token_meta(stations)
                print(f"[fast_recorder] token map refreshed, {len(meta)} assets")
            except Exception as exc:
                print(f"[fast_recorder] token map refresh failed: {exc}")
            last_token_refresh = t

        if t - last_book_poll >= BOOK_POLL_SECONDS:
            try:
                books = fetch_books(meta.keys())
                seen_at = now().isoformat()
                n = record_books(conn, books, meta, last_books, seen_at)
                conn.commit()
                if n:
                    print(f"[fast_recorder] books: {n} rows changed")
            except Exception as exc:
                print(f"[fast_recorder] book poll failed: {exc}")
            last_book_poll = t

        if t - last_metar_poll >= METAR_POLL_SECONDS:
            try:
                rows = fetch_metars(icaos)
                first_seen = now().isoformat()
                n = record_metars(conn, rows, first_seen)
                conn.commit()
                if n:
                    print(f"[fast_recorder] metar: {n} new rows")
            except Exception as exc:
                print(f"[fast_recorder] metar poll failed: {exc}")
            last_metar_poll = t

        sleep(1)

    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket weather book / METAR fast recorder")
    parser.add_argument("--seconds", type=int, default=None, help="run duration, omit to run forever")
    parser.add_argument("--db", type=str, default=None, help="override DB path")
    args = parser.parse_args()

    run(seconds=args.seconds, db_path=args.db)
