"""
storage.py

PURPOSE
-------
Local SQLite persistence for forecasts and observed actuals, across
ALL registered stations. Every table is keyed (in part) by
station_icao, so history for WSSS and WMKK (or any future station)
lives side-by-side in one database without interfering with each
other's calibration.

DEPENDENCIES
------------
sqlite3 (standard library)
config.py, models.py (local)
"""

import sqlite3
from datetime import date
from typing import List, Optional

import config
from models import PointForecast, ObservedReading, Position


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasts (
            station_icao TEXT NOT NULL,
            source TEXT NOT NULL,
            target_date TEXT NOT NULL,
            max_temp_c REAL,
            fetched_at TEXT NOT NULL,
            raw_note TEXT,
            PRIMARY KEY (station_icao, source, target_date, fetched_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            max_temp_c REAL NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (station_icao, target_date, source)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            position_id TEXT PRIMARY KEY,
            station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            bucket_c INTEGER NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            size_usd REAL NOT NULL,
            entry_time TEXT NOT NULL,
            status TEXT NOT NULL,
            high_water_mark REAL NOT NULL,
            exit_price REAL,
            exit_time TEXT,
            exit_reason TEXT,
            token_id TEXT,
            is_paper INTEGER NOT NULL DEFAULT 0,
            size_shares REAL,
            execution_mode TEXT NOT NULL DEFAULT 'paper',
            order_id TEXT
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS is a no-op against a database that already has
    # a `positions` table from before these three columns existed -- it does
    # NOT add columns to an existing table. Without this migration, an
    # existing deployed database silently keeps the old schema and every read
    # of the new fields (size_shares, execution_mode, order_id) fails or, for
    # SELECT *, just returns short rows. Run on every connection so every
    # code path (backtest scripts, tests, the live executor) gets migrated,
    # and check PRAGMA table_info first so this stays idempotent -- ALTER
    # TABLE ADD COLUMN errors if the column is already there.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    for column_name, column_ddl in (
        ("size_shares", "size_shares REAL"),
        ("execution_mode", "execution_mode TEXT NOT NULL DEFAULT 'paper'"),
        ("order_id", "order_id TEXT"),
    ):
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {column_ddl}")
    return conn


def save_forecast(forecast: PointForecast) -> None:
    """Persist a single forecast pull. Safe to call repeatedly (idempotent per fetch)."""
    if forecast is None:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO forecasts VALUES (?, ?, ?, ?, ?, ?)",
            (
                forecast.station_icao,
                forecast.source,
                forecast.target_date.isoformat(),
                forecast.max_temp_c,
                forecast.fetched_at,
                forecast.raw_note,
            ),
        )


def save_observation(observation: ObservedReading) -> None:
    """Persist a confirmed actual reading, e.g. once verified manually against Wunderground."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?)",
            (
                observation.station_icao,
                observation.target_date.isoformat(),
                observation.max_temp_c,
                observation.source,
            ),
        )


def load_observations_since(station_icao: str, cutoff: date) -> List[ObservedReading]:
    """Load all stored observations for one station on or after cutoff."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT station_icao, target_date, max_temp_c, source FROM observations "
            "WHERE station_icao = ? AND target_date >= ?",
            (station_icao, cutoff.isoformat()),
        ).fetchall()
    return [
        ObservedReading(station_icao=r[0], target_date=date.fromisoformat(r[1]), max_temp_c=r[2], source=r[3])
        for r in rows
    ]


def _resolution_grade_source_for(station_icao: str) -> str:
    """
    Which ObservedReading.source counts as settlement truth for THIS
    station. Not a constant any more: Hong Kong settles on the HK
    Observatory's climate extract ("hko_daily_max"), not on any airport
    METAR, so ranking VHHH's rows by the METAR default would rank the
    settlement source at 1 and let a lower-grade reading win the dedup.

    Falls back to the global default for an unregistered station rather
    than raising -- dedup is called on whatever rows storage holds,
    including history for a station someone removed from the registry,
    and a KeyError there would take down calibration for every station in
    the batch.
    """
    try:
        return config.get_station(station_icao).resolution_grade_source
    except KeyError:
        return config.RESOLUTION_GRADE_OBSERVATION_SOURCE


def dedupe_observations(observations: List[ObservedReading]) -> List[ObservedReading]:
    """
    One reading per (station, target_date), keeping the best source per
    config.observation_source_rank -- THE STATION'S OWN settlement source
    first, seed constants last. The observations table's primary key
    allows one row PER SOURCE per day, so any consumer that averages
    readings -- the calibration blend above all -- must dedupe first or a
    day reported by two sources counts twice. Output sorted by date for
    determinism.
    """
    best: dict = {}
    grade_source_cache: dict = {}
    for obs in observations:
        key = (obs.station_icao, obs.target_date)
        if obs.station_icao not in grade_source_cache:
            grade_source_cache[obs.station_icao] = _resolution_grade_source_for(obs.station_icao)
        grade_source = grade_source_cache[obs.station_icao]

        if key not in best or (
            config.observation_source_rank(obs.source, grade_source)
            < config.observation_source_rank(best[key].source, grade_source)
        ):
            best[key] = obs
    return sorted(best.values(), key=lambda o: (o.station_icao, o.target_date))


def count_observations_from_source(station_icao: str, source: str) -> int:
    """
    How many stored observations a station has from ONE named source.

    Feeds entry_manager's collection-first gate, which asks "does this
    station have enough settlement-grade history for its model bias to be
    a measurement rather than a guess" -- so it must count only rows from
    the station's own resolution_grade_source. Counting every source
    would let 30 days of Open-Meteo analysis backfill graduate a station
    that has never once been compared against the record it settles on.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE station_icao = ? AND source = ?",
            (station_icao, source),
        ).fetchone()
    return int(row[0]) if row else 0


def forecast_error_samples(station_icao: str, source: str) -> List[float]:
    """
    One (forecast - settled truth) error in degrees C per target date this
    station has BOTH a settlement-grade observation and at least one stored
    forecast for. Feeds calibration.bias_stats() and, through it, the bias
    correction and the gate that decides whether the correction is
    trustworthy (entry_manager.forecast_bias_stats).

    Two deliberate choices:
      - `source` is the station's own resolution_grade_source, matching
        count_observations_from_source(): the error must be measured
        against the record the market actually settles on, not against a
        convenient proxy.
      - Only forecasts fetched on or before the target date count. A row
        fetched afterwards has seen the day it is "forecasting" and would
        flatter the bias toward zero -- the same lookahead the backtest
        goes to lengths to avoid.

    The per-date forecast mean mirrors blend_central_estimate's own
    forecast term, so the number measured is the number corrected.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT o.max_temp_c, AVG(f.max_temp_c) "
            "FROM observations o JOIN forecasts f "
            "  ON f.station_icao = o.station_icao AND f.target_date = o.target_date "
            "WHERE o.station_icao = ? AND o.source = ? "
            "  AND f.max_temp_c IS NOT NULL AND o.max_temp_c IS NOT NULL "
            "  AND date(f.fetched_at) <= o.target_date "
            "GROUP BY o.target_date",
            (station_icao, source),
        ).fetchall()
    return [float(forecast_mean) - float(observed) for observed, forecast_mean in rows]


def load_forecast_history(station_icao: str, source: str, limit: int = 90) -> List[PointForecast]:
    """Load past forecasts from one source for one station, most recent first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT station_icao, source, target_date, max_temp_c, fetched_at, raw_note "
            "FROM forecasts WHERE station_icao = ? AND source = ? ORDER BY fetched_at DESC LIMIT ?",
            (station_icao, source, limit),
        ).fetchall()
    return [
        PointForecast(
            station_icao=r[0],
            source=r[1],
            target_date=date.fromisoformat(r[2]),
            max_temp_c=r[3],
            fetched_at=r[4],
            raw_note=r[5] or "",
        )
        for r in rows
    ]


def _row_to_position(r) -> Position:
    # r may be short if it was read via a connection that opened before the
    # size_shares/execution_mode/order_id migration ran in this process (or,
    # in principle, before the migration ever ran against this file at all).
    # Default the trailing values instead of indexing straight into r so a
    # stale-length row degrades to "unknown" rather than raising IndexError.
    size_shares = r[15] if len(r) > 15 else None
    execution_mode = r[16] if len(r) > 16 else "paper"
    order_id = r[17] if len(r) > 17 else None
    return Position(
        position_id=r[0],
        station_icao=r[1],
        target_date=date.fromisoformat(r[2]),
        bucket_c=r[3],
        side=r[4],
        entry_price=r[5],
        size_usd=r[6],
        entry_time=r[7],
        status=r[8],
        high_water_mark=r[9],
        exit_price=r[10],
        exit_time=r[11],
        exit_reason=r[12] or "",
        token_id=r[13],
        is_paper=bool(r[14]),
        size_shares=size_shares,
        execution_mode=execution_mode,
        order_id=order_id,
    )


def open_position(position: Position) -> None:
    """Persist a newly-entered position. position.status should be 'open'."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                position.position_id,
                position.station_icao,
                position.target_date.isoformat(),
                position.bucket_c,
                position.side,
                position.entry_price,
                position.size_usd,
                position.entry_time,
                position.status,
                position.high_water_mark,
                position.exit_price,
                position.exit_time,
                position.exit_reason,
                position.token_id,
                int(position.is_paper),
            ),
        )


def update_high_water_mark(position_id: str, new_high_water_mark: float) -> None:
    """Persist an updated high-water-mark for an open position -- called every scan cycle the peak price moves."""
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET high_water_mark = ? WHERE position_id = ?",
            (new_high_water_mark, position_id),
        )


def close_position(position_id: str, exit_price: float, exit_time: str, status: str, reason: str) -> None:
    """Mark a position closed -- status should be one of 'closed_take_profit', 'closed_stop_loss', 'closed_trailing_stop', 'closed_resolution' (see models.Position.status)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET status = ?, exit_price = ?, exit_time = ?, exit_reason = ? WHERE position_id = ?",
            (status, exit_price, exit_time, reason, position_id),
        )


def load_open_positions(station_icao: Optional[str] = None, is_paper: Optional[bool] = None) -> List[Position]:
    """
    Load all currently-open positions, optionally filtered to one
    station and/or to paper vs. real positions. is_paper=None (default)
    returns both -- pass True/False to isolate one track. Keeping paper
    and real positions queryable separately matters: position_manager's
    exit-check loop should generally run over BOTH (paper positions
    still need live exit monitoring to be useful), but reporting and
    bankroll accounting must never silently mix them.
    """
    with _connect() as conn:
        query = "SELECT * FROM positions WHERE status = 'open'"
        params = []
        if station_icao:
            query += " AND station_icao = ?"
            params.append(station_icao)
        if is_paper is not None:
            query += " AND is_paper = ?"
            params.append(int(is_paper))
        rows = conn.execute(query, params).fetchall()
    return [_row_to_position(r) for r in rows]


def load_position_history(station_icao: str, limit: int = 100, is_paper: Optional[bool] = None) -> List[Position]:
    """Load closed positions for a station, most recent exit first, optionally filtered to paper vs. real."""
    with _connect() as conn:
        query = "SELECT * FROM positions WHERE station_icao = ? AND status != 'open'"
        params = [station_icao]
        if is_paper is not None:
            query += " AND is_paper = ?"
            params.append(int(is_paper))
        query += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [_row_to_position(r) for r in rows]
