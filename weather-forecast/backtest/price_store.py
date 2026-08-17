"""
backtest/price_store.py

PURPOSE
-------
Local SQLite store for HISTORICAL market data: which token traded which
(station, date, bucket, side), and what it was quoted at over time.
This is the missing input backtest_engine.run_trading_strategy_backtest()
refuses to run without -- no historical Polymarket price data existed
anywhere in this codebase, so no price-based strategy backtest was
honestly possible. This module is where that data lands.

SEPARATE DATABASE, ON PURPOSE
-----------------------------
Every function writes to settings.MARKET_DATA_DB, NEVER config.DB_PATH.
The live trading database holds positions; this one holds bulk research
data written by fetchers that get re-run, interrupted and schema-churned
freely. They must not share a file. Nothing here should ever be given
config.DB_PATH as db_path.

THE LOOK-AHEAD GUARD
--------------------
get_price_at() returns the newest row with ts <= the requested instant,
never the nearest one. "Nearest" is the single easiest way to build a
backtest that quietly trades on prices from the future and prints a
wonderful Sharpe ratio. It also refuses rows that are too old to be
credible (see settings.MAX_STALENESS_FACTOR): a gap in the history is
"no quote", not "the price never moved", because treating it as the
latter manufactures fills and hold-throughs that never happened.

DEPENDENCIES
------------
sqlite3, statistics, datetime, typing (standard library)
backtest/settings.py (local)
"""

import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from backtest import settings

# Source string for quotes captured from a genuinely live order book, as
# opposed to reconstructed/interpolated series. Preferred on ties in
# get_price_at() and the basis of coverage reporting -- a run built mostly
# on non-live sources is a different (weaker) claim than one built on
# observed books, and the two must stay distinguishable.
LIVE_SNAPSHOT_SOURCE = "live_snapshot"

# Quotes captured by the EXIT path (position_manager) rather than the entry
# path (ev_engine.run_for_station). Both are live order-book reads; they are
# kept as separate sources for two reasons.
#
# WHY THIS SOURCE EXISTS AT ALL. Snapshot capture used to piggyback ONLY on
# entry cycles, which run in the 05:00-10:00 local windows. The daemon goes
# on watching open positions until 22:45 in monitor_only/risk_only windows,
# and exits fire there -- but nothing recorded a price, so a replay could not
# see them. Measured 2026-08-17 across all 13 stations: exactly 5 of 24 UTC
# hours had any coverage, ~20 snapshots per token per day against 288 for a
# real 5-minute series. The distortion is not symmetric. At RCSS every one of
# 6 stop-losses fell inside the recorded window while 7 of 11 take-profits
# fell outside it, because EDGE_DECAY_TIGHTEN_HOUR_LOCAL halves the
# profit-take threshold at 10:00 -- the exact hour recording stopped. The
# replay therefore saw nearly every loss and few of the gains, and reported
# -6.08% where the live ledger showed +10.6%.
#
# WHY IT IS A DISTINCT STRING. These rows carry no ask_price and no depth
# (the exit path fetches a bid and nothing else, and must not grow /book
# calls), and they arrive on the monitor windows' 15-30 min cadence rather
# than the entry windows' 5. On an exact ts tie get_price_at() prefers
# LIVE_SNAPSHOT_SOURCE, so where both paths captured the same instant the
# richer entry-path row still wins. Keeping them separable is also what lets
# coverage reporting say which part of the day a run's prices came from,
# instead of averaging a dense morning together with a sparse afternoon.
EXIT_SNAPSHOT_SOURCE = "live_exit_check"


def _add_missing_columns(conn, table: str, columns: dict) -> None:
    """
    Idempotent ALTER TABLE ... ADD COLUMN for each column not already
    present. Mirrors storage._connect()'s migration step.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _connect(db_path=None) -> sqlite3.Connection:
    """
    Open the market-data database, creating the schema if absent -- same
    lazy-creation style as storage._connect(), so there is no separate
    migration step to forget to run.
    """
    conn = sqlite3.connect(db_path or settings.MARKET_DATA_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_tokens (
            token_id TEXT PRIMARY KEY,
            station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            bucket_c INTEGER NOT NULL,
            side TEXT NOT NULL,
            event_slug TEXT NOT NULL DEFAULT '',
            discovered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_mt_lookup ON market_tokens(station_icao, target_date, bucket_c, side)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            token_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            price REAL NOT NULL,
            depth_usd REAL,
            source TEXT NOT NULL,
            fidelity_min INTEGER NOT NULL,
            ask_price REAL,
            PRIMARY KEY (token_id, ts, source)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ps_lookup ON price_snapshots(token_id, ts)")

    # ask_price added 2026-08-10. CREATE TABLE IF NOT EXISTS does nothing to
    # a table that already exists, so an existing market_data.sqlite3 keeps
    # the old 6-column schema and every read of the new field fails.
    _add_missing_columns(conn, "price_snapshots", {"ask_price": "REAL"})
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_log (
            token_id TEXT,
            start_ts INTEGER,
            end_ts INTEGER,
            fidelity_requested INTEGER,
            fidelity_observed INTEGER,
            n_rows INTEGER,
            fetched_at TEXT,
            http_status INTEGER
        )
        """
    )
    return conn


@contextmanager
def _db(db_path=None):
    """
    Transaction scope AND connection lifetime in one context manager.

    sqlite3's own `with conn:` only delimits a transaction -- it never
    closes the connection, so using `with _connect(...)` directly leaks
    one file descriptor per call. A backtest run performs tens of
    thousands of get_price_at() lookups, which exhausted the default
    1024-fd limit on the EC2 instance ("unable to open database file")
    the first time a real multi-day run was attempted. Every function in
    this module must use _db(), never _connect() directly.
    """
    conn = _connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    """UTC ISO timestamp, same format executor.py writes for entry_time/exit_time."""
    return datetime.now(timezone.utc).isoformat()


def _date_str(target_date) -> str:
    """Accept either a date object or an already-ISO string, store the ISO string (storage.py convention)."""
    return target_date if isinstance(target_date, str) else target_date.isoformat()


def upsert_token(
    token_id: str,
    station_icao: str,
    target_date,
    bucket_c: int,
    side: str,
    event_slug: str = "",
    discovered_at: Optional[str] = None,
    db_path=None,
) -> None:
    """
    Record (or refresh) the mapping from a CLOB token id to the exact
    (station, target_date, bucket, side) it represents. side is stored
    lower-cased ("yes"/"no") so load_token_map() can key off it without
    every caller having to agree on case -- note models.Position.side
    uses upper-case "YES"/"NO", so conversion happens at this boundary.
    """
    with _db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                station_icao,
                _date_str(target_date),
                bucket_c,
                side.lower(),
                event_slug,
                discovered_at or _now_iso(),
            ),
        )


_INSERT_SNAPSHOT = (
    "INSERT OR REPLACE INTO price_snapshots "
    "(token_id, ts, price, depth_usd, source, fidelity_min, ask_price) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def save_snapshot(
    token_id: str,
    ts: int,
    price: float,
    depth_usd: Optional[float],
    source: str,
    fidelity_min: int,
    db_path=None,
    ask_price: Optional[float] = None,
) -> None:
    """
    Persist one observed quote. INSERT OR REPLACE on (token_id, ts,
    source), so re-fetching a range is idempotent -- fetchers get
    interrupted and re-run constantly, and a re-run must not duplicate
    history or fail.

    WHICH SIDE OF THE BOOK EACH COLUMN HOLDS
    -----------------------------------------
    `price` is the BID -- what a sale receives, and what every row written
    before 2026-08-10 contains. It keeps that meaning permanently: switching
    the column to the ask would have made every historical row incomparable
    to every new one, silently, with nothing in the data to say which was
    which.

    `ask_price` is what a purchase PAYS, and is the correct price for an
    ENTRY. It is nullable and NULL on every historical row, which is not a
    defect but the honest representation of "not captured" -- see
    engine._entry_price() for how a replay handles the gap and reports it.

    The column names are deliberately asymmetric (`price` vs `ask_price`)
    rather than a tidy bid_price/ask_price pair: renaming `price` would
    touch every reader and every stored row for cosmetic gain, and the
    docstring is a cheaper place to carry the meaning than a migration.
    """
    with _db(db_path) as conn:
        conn.execute(
            _INSERT_SNAPSHOT,
            (token_id, int(ts), price, depth_usd, source, int(fidelity_min), ask_price),
        )


def save_snapshots(rows: Iterable[tuple], source: str, fidelity_min: int, db_path=None) -> int:
    """
    Bulk variant of save_snapshot(). Returns the number of rows written.

    rows are either
        (token_id, ts, price, depth_usd)              -- ask not captured
        (token_id, ts, price, depth_usd, ask_price)   -- both sides

    Both arities are accepted on purpose. The 4-tuple form is what
    price_history_client produces (CLOB's history endpoint returns a
    single traded-price series with no book behind it, so there IS no ask
    to record) and what the synthetic test scenario builds. Rejecting it
    would force every caller to pass a None that means the same thing.
    """
    payload = []
    for row in rows:
        if len(row) == 5:
            token_id, ts, price, depth_usd, ask_price = row
        elif len(row) == 4:
            (token_id, ts, price, depth_usd), ask_price = row, None
        else:
            raise ValueError(
                f"save_snapshots expects 4- or 5-tuples, got {len(row)} fields: {row!r}"
            )
        payload.append(
            (token_id, int(ts), price, depth_usd, source, int(fidelity_min), ask_price)
        )
    if not payload:
        return 0
    with _db(db_path) as conn:
        conn.executemany(_INSERT_SNAPSHOT, payload)
    return len(payload)


def get_price_at(
    token_id: str,
    ts: int,
    max_staleness_s: Optional[int] = None,
    db_path=None,
) -> Optional[dict]:
    """
    The most recent quote AT OR BEFORE ts -- the look-ahead guard.

    Never returns a row from after ts, not even one microsecond after and
    not even if it is closer in time. A "nearest quote" lookup is how a
    backtest ends up trading on prices it could not have seen.

    Staleness: if max_staleness_s is given, a row older than that is
    treated as no quote at all. If it is None, the limit is derived from
    the row's own recorded fidelity
    (settings.MAX_STALENESS_FACTOR x fidelity_min x 60) -- a 5-minute
    series going quiet for an hour is a data gap, and pretending the last
    print is still the market invents both fills and hold-throughs.

    On an exact ts tie between sources, live order-book snapshots win over
    reconstructed series.

    Returns {"price", "ask_price", "ts", "source", "depth_usd",
    "fidelity_min"} or None. "price" is the BID; "ask_price" is what an
    entry pays and is None on every row captured before 2026-08-10 --
    see save_snapshot() for why the two are not symmetric.
    """
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT price, ts, source, depth_usd, fidelity_min, ask_price FROM price_snapshots "
            "WHERE token_id = ? AND ts <= ? "
            "ORDER BY ts DESC, (source = ?) DESC LIMIT 1",
            (token_id, int(ts), LIVE_SNAPSHOT_SOURCE),
        ).fetchone()

    if row is None:
        return None

    price, row_ts, source, depth_usd, fidelity_min, ask_price = row

    limit_s = max_staleness_s
    if limit_s is None:
        limit_s = settings.MAX_STALENESS_FACTOR * (fidelity_min or settings.DEFAULT_SNAPSHOT_FIDELITY_MIN) * 60
    if int(ts) - row_ts > limit_s:
        return None

    return {
        "price": price,
        "ask_price": ask_price,
        "ts": row_ts,
        "source": source,
        "depth_usd": depth_usd,
        "fidelity_min": fidelity_min,
    }


def load_token_map(station_icao: str, target_date, db_path=None) -> Dict[int, Dict[str, str]]:
    """
    {bucket_c: {"yes_token_id": ..., "no_token_id": ...}} for one
    station/date -- the same shape market_discovery.discover_token_map()
    returns live, so entry-side code can be handed either one.

    Buckets with only one side recorded still appear, with the missing
    side absent from the inner dict; callers that need both must check.
    """
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT bucket_c, side, token_id FROM market_tokens WHERE station_icao = ? AND target_date = ?",
            (station_icao, _date_str(target_date)),
        ).fetchall()

    token_map: Dict[int, Dict[str, str]] = {}
    for bucket_c, side, token_id in rows:
        entry = token_map.setdefault(bucket_c, {})
        if side == "yes":
            entry["yes_token_id"] = token_id
        elif side == "no":
            entry["no_token_id"] = token_id
    return token_map


def list_tokens(station_icao=None, target_date=None, db_path=None) -> List[dict]:
    """Every recorded token, optionally filtered to one station and/or target date."""
    query = (
        "SELECT token_id, station_icao, target_date, bucket_c, side, event_slug, discovered_at "
        "FROM market_tokens"
    )
    clauses = []
    params: list = []
    if station_icao:
        clauses.append("station_icao = ?")
        params.append(station_icao)
    if target_date:
        clauses.append("target_date = ?")
        params.append(_date_str(target_date))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY target_date, bucket_c, side"

    with _db(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {
            "token_id": r[0],
            "station_icao": r[1],
            "target_date": r[2],
            "bucket_c": r[3],
            "side": r[4],
            "event_slug": r[5],
            "discovered_at": r[6],
        }
        for r in rows
    ]


def log_fetch(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity_requested: int,
    fidelity_observed: int,
    n_rows: int,
    http_status: int,
    db_path=None,
) -> None:
    """
    Append-only record of what was fetched and what came back. Worth
    keeping because fidelity_requested vs fidelity_observed is the honest
    answer to "how good is this history really" -- an API that silently
    downgrades a 1-minute request to hourly bars changes what the
    backtest is allowed to claim, and that fact must survive the fetch.
    """
    with _db(db_path) as conn:
        conn.execute(
            "INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                None if start_ts is None else int(start_ts),
                None if end_ts is None else int(end_ts),
                fidelity_requested,
                fidelity_observed,
                n_rows,
                _now_iso(),
                http_status,
            ),
        )


def coverage_stats(token_ids: List[str], start_ts: int, end_ts: int, db_path=None) -> dict:
    """
    Data-quality summary for a set of tokens over a window: how many
    ticks exist, what share came from live order-book snapshots, what
    share carry depth, and the median sampling fidelity.

    Read this BEFORE reading a backtest's P&L. A result built on 12%
    coverage is not a weaker version of the same finding, it is a
    different and much smaller claim.
    """
    empty = {
        "n_ticks": 0,
        "pct_live_snapshot": 0.0,
        "pct_with_depth": 0.0,
        "median_fidelity_min": None,
    }
    if not token_ids:
        return empty

    placeholders = ",".join("?" for _ in token_ids)
    with _db(db_path) as conn:
        rows = conn.execute(
            f"SELECT source, depth_usd, fidelity_min FROM price_snapshots "
            f"WHERE token_id IN ({placeholders}) AND ts >= ? AND ts <= ?",
            list(token_ids) + [int(start_ts), int(end_ts)],
        ).fetchall()

    if not rows:
        return empty

    n = len(rows)
    n_live = sum(1 for r in rows if r[0] == LIVE_SNAPSHOT_SOURCE)
    n_depth = sum(1 for r in rows if r[1] is not None)
    fidelities = [r[2] for r in rows if r[2] is not None]

    return {
        "n_ticks": n,
        "pct_live_snapshot": round(n_live / n, 4),
        "pct_with_depth": round(n_depth / n, 4),
        "median_fidelity_min": statistics.median(fidelities) if fidelities else None,
    }


def observed_depth_median(station_icao: str, db_path=None) -> Optional[float]:
    """
    Median observed order-book depth across this station's live
    snapshots, or None if depth was never recorded. Feeds the
    "observed_median" depth regime (settings.DEPTH_REGIMES) -- a
    measured stand-in for missing depth, rather than an invented one.
    """
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT ps.depth_usd FROM price_snapshots ps "
            "JOIN market_tokens mt ON mt.token_id = ps.token_id "
            "WHERE mt.station_icao = ? AND ps.source = ? AND ps.depth_usd IS NOT NULL",
            (station_icao, LIVE_SNAPSHOT_SOURCE),
        ).fetchall()

    depths = [r[0] for r in rows]
    if not depths:
        return None
    return statistics.median(depths)
