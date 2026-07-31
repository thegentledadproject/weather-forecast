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
            is_paper INTEGER NOT NULL DEFAULT 0
        )
        """
    )
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
    """Mark a position closed -- status should be one of 'closed_profit', 'closed_stop', 'closed_manual', 'closed_resolution'."""
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
