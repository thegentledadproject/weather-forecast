"""
Database connections must be closed deterministically, not left to the
garbage collector.

This bug has now happened twice. sqlite3's `with conn:` delimits a
TRANSACTION and never closes the connection, so `with _connect() as conn:`
depends on the connection object being collected to release its file
descriptor -- and sqlite3.Connection sits in reference cycles, so only the
CYCLIC collector frees it. The cyclic collector is triggered by allocation
volume, which a scheduler that spends most of its life asleep barely
generates, so descriptors pile up for hours.

It was fixed in backtest/price_store.py (4f72dd4) after a backtest died at
the 1024-fd limit, and the identical pattern was left in storage.py, which
is the module the LIVE daemon calls on every forecast, observation and
position read. That took the daemon down on 2026-08-07 and was still
leaking on 2026-08-10, when 26 of the process's 29 descriptors were open
handles to polyweather.sqlite3 minutes after a restart.

THESE TESTS RUN WITH gc DISABLED, ON PURPOSE. With the collector enabled
the leak is invisible in a short test -- it collects often enough under
pytest's allocation churn to hide it -- which is exactly why it survived
the first fix. Disabling gc is what makes "closed deterministically" a
testable claim rather than a hope.
"""

import gc
import sqlite3
from datetime import date

import pytest

import config
import storage
from models import ObservedReading, PointForecast, Position


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    storage._connect().close()
    return str(tmp_path / "trading.sqlite3")


@pytest.fixture
def no_gc():
    """
    Disable the cyclic collector for the duration of a test, so anything
    still alive at the end was NOT released deterministically.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _open_connections() -> int:
    return sum(1 for o in gc.get_objects() if isinstance(o, sqlite3.Connection))


def test_storage_writes_do_not_leak_connections(temp_db, no_gc):
    before = _open_connections()

    for i in range(100):
        storage.save_forecast(PointForecast(
            station_icao="WSSS", source=f"src{i}", target_date=date(2026, 8, 10),
            max_temp_c=32.0, fetched_at="2026-08-10T00:00:00+00:00",
        ))
        storage.save_observation(ObservedReading(
            station_icao="WSSS", target_date=date(2026, 8, i % 28 + 1),
            max_temp_c=32.0, source="metar_daily_max",
        ))

    assert _open_connections() == before, (
        "storage writes left connections open with gc disabled -- "
        "a `with _connect()` has been reintroduced somewhere"
    )


def test_storage_reads_do_not_leak_connections(temp_db, no_gc):
    before = _open_connections()

    for _ in range(100):
        storage.load_open_positions()
        storage.load_position_history("WSSS")
        storage.load_observations_since("WSSS", date(2026, 7, 1))
        storage.load_forecast_history("WSSS", "open_meteo_ecmwf")

    assert _open_connections() == before


def test_connection_is_closed_even_when_the_body_raises(temp_db, no_gc):
    """
    The daemon's normal condition, not an edge case: its journal is full of
    upstream fetch failures, and an exception unwinding through the block
    must still release the descriptor.
    """
    before = _open_connections()

    for _ in range(50):
        with pytest.raises(RuntimeError):
            with storage._db():
                raise RuntimeError("simulated fetch failure")

    # <= before + 1: the most recently raised exception's traceback can still
    # reference the frame holding one connection. That connection's fd is
    # already closed by the finally -- this bounds the object, not the fd.
    assert _open_connections() <= before + 1


def test_a_closed_connection_is_actually_unusable(temp_db):
    """
    Proves the finally really closed the descriptor rather than merely
    dropping the reference -- the whole point of the fix.
    """
    with storage._db() as conn:
        conn.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_storage_has_no_bare_with_connect():
    """
    Structural guard. The two previous incidents were both a call site
    using `with _connect()` directly; this fails the moment one reappears,
    without needing to exercise it.

    Parsed with ast rather than grepped: this module's docstrings discuss
    `with _connect()` at length precisely because it is the bug, and a
    textual search flags its own explanation.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(storage.__file__).read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        for item in node.items
        if isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Name)
        and item.context_expr.func.id == "_connect"
    ]
    assert not offenders, (
        f"storage.py must use _db(), not `with _connect()` -- lines {offenders}"
    )


def test_price_store_stays_fixed(tmp_path, no_gc):
    """
    price_store was fixed first (4f72dd4) and storage was not. Pin both, so
    the next person to touch either finds out immediately.
    """
    from backtest import price_store

    db = str(tmp_path / "market.sqlite3")
    before = _open_connections()

    for i in range(50):
        price_store.upsert_token(
            token_id=f"tok{i}", station_icao="WSSS", target_date="2026-08-10",
            bucket_c=32, side="yes", event_slug="e", discovered_at="2026-08-10T00:00:00+00:00",
            db_path=db,
        )

    assert _open_connections() == before
