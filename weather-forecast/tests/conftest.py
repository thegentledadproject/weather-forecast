"""Shared pytest fixtures for the backtest test suite.

Run from the code dir with: python -m pytest tests -q
"""
import sys
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

# NOTE: the synthetic end-to-end engine fixture (used by test_no_lookahead.py,
# test_determinism.py, test_backtest_stack.py) is appended below this line by
# a separate task adapting scratchpad/smoke_engine_e2e.py. Do not remove the
# sys.path setup above when editing.

# --------------------------------------------------------------------------
# Shared synthetic end-to-end scenario
# --------------------------------------------------------------------------
"""
ONE miniature three-day scenario for WSSS, seeded into throwaway sqlite
files, shared by every backtest test in this directory.

Adapted from scratchpad/smoke_engine_e2e.py's builder. The scenario is
tuned so a single run exercises all four interesting paths:

  D1  entry, then an intraday trailing-stop exit on a price spike/fall
  D2  two entries held overnight
  D3  NO token map at all -- yet D2's positions must still resolve

Two properties the tests depend on, and which the builder must preserve:

  * REPEATABLE -- build_scenario() can be called twice with identical
    arguments and produce identical data. Nothing in it reads the wall
    clock; every timestamp is derived from simclock.local_minute_to_ts().
    engine._make_run_id() hashes only the run PARAMETERS, so two builds
    in two different tmp directories also share a run_id.

  * POISONABLE -- everything written at or after `window_end_ts` (the
    instant the simulated window closes) is written through the same two
    helpers, so poison_future=True can shift all of it by +10C in one
    place. A run that reads any of it will diverge loudly.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

STATION_ICAO = "WSSS"
D1 = date(2026, 8, 10)
D2 = date(2026, 8, 11)
D3 = date(2026, 8, 12)
BUCKETS = (30, 31, 32, 33, 34)

# Snapshots every 10 simulated minutes from 05:30 local, with
# fidelity_min=10 so price_store's staleness guard (MAX_STALENESS_FACTOR x
# fidelity) tolerates a 20-minute gap -- wide enough for every schedule
# interval, narrow enough that the ticks before 05:30 genuinely have NO
# price. That gap is what test_no_lookahead's "nothing opened before the
# first snapshot" assertion is measured against.
SNAP_START_MIN = 5 * 60 + 30
SNAP_END_MIN = 23 * 60 + 50
SNAP_STEP_MIN = 10
FIDELITY_MIN = 10
DEPTH_USD = 1000.0

# The +10C shift applied to every future-dated observation/forecast when
# poison_future=True.
POISON_SHIFT_C = 10.0


@dataclass
class SyntheticScenario:
    """Everything a test needs to run and interrogate the shared scenario."""
    station_icao: str
    start_date: date
    end_date: date
    market_db_path: str
    trading_db_path: str
    depth_regime: str
    fee_rate_pct: float
    bankroll_mode: str
    first_snapshot_ts: int      # earliest price snapshot in the seeded data
    window_end_ts: int          # first instant AFTER the simulated window
    tripwire_ts: int            # in-window late forecast, see build_scenario
    poisoned: bool

    @property
    def run_kwargs(self) -> dict:
        """Splat straight into backtest.engine.run()."""
        return {
            "station_icao": self.station_icao,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "depth_regime": self.depth_regime,
            "fee_rate_pct": self.fee_rate_pct,
            "bankroll_mode": self.bankroll_mode,
            "market_db_path": self.market_db_path,
        }


def _token_id(day: date, bucket: int, side: str) -> str:
    return f"{day.isoformat()}-B{bucket}-{side.upper()}"


def _yes_price(day: date, bucket: int, minute: int) -> float:
    """
    D1's bucket-32 YES is the only mover: 0.35 at entry, spiking to 0.70 at
    06:00 (arming the trailing stop) and falling to 0.58 at 06:10 (breaching
    it), then flat -- flat AND below the model's own probability, so nothing
    re-enters after the exit.
    """
    if day == D1 and bucket == 32:
        if minute < 6 * 60:
            return 0.35
        if minute < 6 * 60 + 10:
            return 0.70
        return 0.58
    if day == D1:
        return {30: 0.02, 31: 0.30, 32: 0.35, 33: 0.45, 34: 0.02}[bucket]
    # D2: bucket 32 becomes the eventual winner, bucket 31 the eventual loser.
    return {30: 0.02, 31: 0.10, 32: 0.35, 33: 0.45, 34: 0.02}[bucket]


def _no_price(day: date, bucket: int, minute: int) -> float:
    """Every NO side quoted at 0.99, so no NO leg ever clears the EV screen."""
    return 0.99


def build_scenario(
    tmp_path,
    monkeypatch,
    poison_future: bool = False,
    depth_regime: str = "strict",
    fee_rate_pct: float = 0.0,
    bankroll_mode: str = "static",
) -> SyntheticScenario:
    """
    Seed a fresh pair of sqlite files under `tmp_path` and return the handle.

    config.DB_PATH is monkeypatched to the throwaway trading db BEFORE
    storage is touched -- storage._connect() reads config.DB_PATH at call
    time, so the patch has to be live for both the seeding below and the
    later engine.run(). The market db is passed explicitly instead
    (price_store takes db_path per call), which keeps the two stores as
    separate as backtest/settings.py insists they are.

    poison_future=True shifts every observation for a date AFTER end_date,
    and every forecast fetched at or after window_end_ts, by +POISON_SHIFT_C.
    Poisoned data sits strictly outside the simulated window; a run whose
    output moves because of it has read the future.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    trading_db = tmp_path / "trading.sqlite3"
    market_db = tmp_path / "market_data.sqlite3"

    import config

    monkeypatch.setattr(config, "DB_PATH", str(trading_db))

    import storage
    from models import ObservedReading, PointForecast
    from backtest import price_store, simclock

    start_date, end_date = D1, D3
    window_end_ts = simclock.local_minute_to_ts(end_date + timedelta(days=1), 0)
    first_snapshot_ts = simclock.local_minute_to_ts(D1, SNAP_START_MIN)
    tripwire_ts = simclock.local_minute_to_ts(D1, 6 * 60 + 30)

    # --- market data: D1 and D2 only. D3 lists no market on purpose. -------
    for day in (D1, D2):
        for bucket in BUCKETS:
            for side in ("yes", "no"):
                price_store.upsert_token(
                    token_id=_token_id(day, bucket, side),
                    station_icao=STATION_ICAO,
                    target_date=day,
                    bucket_c=bucket,
                    side=side,
                    event_slug=f"synthetic-{day.isoformat()}",
                    # Fixed, not now() -- the fixture must be byte-repeatable.
                    discovered_at="2026-08-01T00:00:00+00:00",
                    db_path=str(market_db),
                )

        rows = []
        for minute in range(SNAP_START_MIN, SNAP_END_MIN + 1, SNAP_STEP_MIN):
            ts = simclock.local_minute_to_ts(day, minute)
            for bucket in BUCKETS:
                rows.append((_token_id(day, bucket, "yes"), ts, _yes_price(day, bucket, minute), DEPTH_USD))
                rows.append((_token_id(day, bucket, "no"), ts, _no_price(day, bucket, minute), DEPTH_USD))
        price_store.save_snapshots(
            rows,
            source=price_store.LIVE_SNAPSHOT_SOURCE,
            fidelity_min=FIDELITY_MIN,
            db_path=str(market_db),
        )

    # --- forecasts: two sources per day, published 04:50 local -------------
    for day in (D1, D2, D3):
        fetched = simclock.SimClock(simclock.local_minute_to_ts(day, 4 * 60 + 50)).now_iso()
        for source, temp in (("open_meteo_ecmwf", 31.53), ("open_meteo_gfs", 32.47)):
            storage.save_forecast(PointForecast(
                station_icao=STATION_ICAO, source=source, target_date=day,
                max_temp_c=temp, fetched_at=fetched, raw_note="synthetic",
            ))

    # IN-WINDOW TRIPWIRE (never poisoned): an absurd 40C forecast published
    # at 06:30 local on D1. Ticks BEFORE 06:30 that let it into calibration
    # are reading the future, and the recorded central estimate says so.
    late = simclock.SimClock(tripwire_ts).now_iso()
    storage.save_forecast(PointForecast(
        station_icao=STATION_ICAO, source="open_meteo_ecmwf", target_date=D1,
        max_temp_c=40.0, fetched_at=late, raw_note="in-window tripwire",
    ))

    # POST-WINDOW FORECAST: targets D1 (an in-window day) but is fetched
    # after the window closed. engine._entry_pass filters on fetched_at, so
    # this must never reach calibration -- poisoning it is the direct test
    # of that filter.
    post_window = simclock.SimClock(window_end_ts + 3600).now_iso()
    storage.save_forecast(PointForecast(
        station_icao=STATION_ICAO, source="open_meteo_gfs", target_date=D1,
        max_temp_c=32.0 + (POISON_SHIFT_C if poison_future else 0.0),
        fetched_at=post_window, raw_note="post-window",
    ))

    # --- observations ------------------------------------------------------
    # Stable recent history, then the two outcome days. D1 and D2 both land
    # in bucket 32, so D2's bucket-32 YES wins and its bucket-31 YES loses.
    for dom in range(1, 10):
        storage.save_observation(ObservedReading(
            station_icao=STATION_ICAO, target_date=date(2026, 8, dom),
            max_temp_c=32.0, source="synthetic_actual",
        ))
    for day in (D1, D2):
        storage.save_observation(ObservedReading(
            station_icao=STATION_ICAO, target_date=day,
            max_temp_c=32.0, source="synthetic_actual",
        ))

    # FUTURE OBSERVATIONS: dated after the window. Only resolution.
    # observation_visible() and the engine's date filters keep these out.
    for offset in (1, 2, 3):
        future_day = end_date + timedelta(days=offset)
        storage.save_observation(ObservedReading(
            station_icao=STATION_ICAO, target_date=future_day,
            max_temp_c=32.0 + (POISON_SHIFT_C if poison_future else 0.0),
            source="synthetic_actual",
        ))

    return SyntheticScenario(
        station_icao=STATION_ICAO,
        start_date=start_date,
        end_date=end_date,
        market_db_path=str(market_db),
        trading_db_path=str(trading_db),
        depth_regime=depth_regime,
        fee_rate_pct=fee_rate_pct,
        bankroll_mode=bankroll_mode,
        first_snapshot_ts=first_snapshot_ts,
        window_end_ts=window_end_ts,
        tripwire_ts=tripwire_ts,
        poisoned=poison_future,
    )


@pytest.fixture
def scenario_builder(tmp_path, monkeypatch):
    """
    Factory for build_scenario(), so one test can build several variants
    (clean vs. future-poisoned) into separate sub-directories.

    Each call re-points config.DB_PATH at its own trading db, so callers
    must build-then-run one variant at a time rather than building both up
    front and running them afterwards.
    """
    counter = {"n": 0}

    def _build(poison_future: bool = False, name: str = None, **kwargs):
        counter["n"] += 1
        sub = tmp_path / (name or f"scenario_{counter['n']}")
        return build_scenario(sub, monkeypatch, poison_future=poison_future, **kwargs)

    return _build


@pytest.fixture
def synthetic_scenario(scenario_builder):
    """The plain, unpoisoned shared scenario -- the common case."""
    return scenario_builder(name="scenario")


def canonical_run(run, include_manifest: bool = False) -> str:
    """
    Everything about a run that must be reproducible, as one sorted JSON
    string.

    Excluded by default:
      * manifest -- it embeds the git SHA and the two absolute db paths,
        which are environment, not experiment. Callers comparing two runs
        over the SAME seeded databases can pass include_manifest=True; the
        git SHA is popped either way.

    default=str is a deliberate belt-and-braces: date objects inside
    Position/entry records serialise as their ISO form, and any type that
    sneaks in later stringifies rather than silently dropping out of the
    comparison.
    """
    import dataclasses
    import json

    payload = {
        "run_id": run.run_id,
        "station_icao": run.station_icao,
        "start_date": run.start_date,
        "end_date": run.end_date,
        "params": run.params,
        "closed_positions": [dataclasses.asdict(p) for p in run.closed_positions],
        "unresolved_positions": [dataclasses.asdict(p) for p in run.unresolved_positions],
        "cash": run.portfolio.cash,
        "equity_curve": run.portfolio.equity_curve,
        "max_drawdown_pct": run.portfolio.max_drawdown_pct,
        "counters": run.counters,
        "provenance": run.provenance,
        "entry_records": run.entry_records,
        "decisions_log": run.decisions_log,
    }
    if include_manifest:
        manifest = dict(run.manifest)
        manifest.pop("git_sha", None)
        payload["manifest"] = manifest
    return json.dumps(payload, sort_keys=True, default=str)


@pytest.fixture
def canonical():
    """canonical_run() as a fixture, for tests that prefer injection."""
    return canonical_run


@pytest.fixture
def quiet_run():
    """
    engine.run() with entry_manager's per-cycle veto/budget chatter
    swallowed. Those prints are live-path behaviour worth keeping, but they
    bury pytest output; the return value is untouched.
    """
    import contextlib
    import io

    from backtest import engine

    def _run(scenario, **overrides):
        kwargs = dict(scenario.run_kwargs)
        kwargs.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            return engine.run(**kwargs)

    return _run
