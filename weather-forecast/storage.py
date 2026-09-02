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
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import config
from models import PointForecast, ObservedReading, Position


@contextmanager
def _db():
    """
    Transaction scope AND connection lifetime in one context manager.
    EVERY function in this module must use _db(); never `with _connect()`.

    THIS IS THE SAME BUG 4f72dd4 FIXED IN price_store.py, AND IT WAS LEFT
    HERE. sqlite3's own `with conn:` delimits a TRANSACTION -- it commits or
    rolls back and then does nothing else. It never closes the connection.
    `with _connect() as conn:` therefore relies entirely on the connection
    object being garbage-collected to release its file descriptor.

    Refcounting does not reclaim it. Measured directly: with gc disabled,
    50 calls through `with _connect()` leave all 50 connections alive,
    whether or not the body raised. sqlite3.Connection sits in reference
    cycles, so only the CYCLIC collector frees it -- and the cyclic
    collector is triggered by allocation volume, which a scheduler that
    spends most of its life asleep barely generates. Descriptors then
    accumulate for hours between collections.

    That is the "[Errno 24] Too many open files" that took the daemon down
    on 2026-08-07 and was still happening 2026-08-10: 26 of the process's
    29 descriptors were open handles to polyweather.sqlite3 a few minutes
    after a restart. Once the limit is hit, EVERY outbound request fails to
    get a socket too, so whole station cycles die with "unable to open
    database file" and the failure looks like a network problem.

    Closing in a finally is deterministic and immune to all of it.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# Derived economics over `positions`, for ad-hoc SQL and reporting.
#
# WHY THIS EXISTS. size_shares is NULL on every paper/manual_review row, and
# that is correct rather than missing: it means SHARES ACTUALLY HELD ON THE
# EXCHANGE, and a paper fill holds none (see models.Position.size_shares).
# But every P&L query then has to hand-roll
# `coalesce(size_shares, size_usd/entry_price)`, and writing that by hand is
# how a query eventually gets it wrong -- or, worse, how someone "fixes" the
# NULL by storing a fabricated share count into the field that
# executor.close_position() reads to tell an operator what to sell.
#
# So the derived quantity gets its own name and lives here. `notional_shares`
# is "what size_usd bought at entry_price" -- a unit of account, true for
# every mode. `size_shares` is passed through UNCHANGED and stays NULL for
# paper, so the distinction the ledger is careful about survives the view.
# Never join or reconcile against notional_shares; it is not a holding.
#
# realized_pnl_usd is GROSS OF THE ENTRY-SIDE FEE. exit_price is already net
# of the exit taker fee (position_manager/engine subtract it before writing),
# but the entry leg is recorded gross, so this slightly overstates every
# closed trade by the entry fee. That asymmetry is a property of the stored
# data, not of this view -- it is named here so a reader does not rediscover
# it as a discrepancy.
# `p.*` RE-EXPANDS AT PREPARE TIME, so a column added to `positions` by the
# ALTER TABLE migration below shows up here IMMEDIATELY, on an existing view,
# with no rebuild -- the drift check compares this SQL text, which has not
# changed, and does not need to. Verified against SQLite 3.49: adding entry_bid
# takes the view from 24 columns to 25 on the same connection.
#
# THE CONSEQUENCE IS POSITIONAL, and it is the reason this note exists: the
# three computed columns are appended AFTER `p.*`, so every migration shifts
# them right by one. entry_bid (2026-09-01) moved notional_shares from index 21
# to 22. Nothing in this repo or in deploy/ reads the view positionally -- both
# dashboards go through this module's own loaders -- but ad-hoc SQL that does
# `select ... from position_economics` and indexes by number will read a
# different column after a deploy that adds a positions column. Name them.
#
# (An earlier version of this comment claimed the opposite -- that SQLite
# freezes the star at CREATE time and a migrated database would NOT show the
# new column. It does not, and it does. Said plainly here because a comment
# that is confidently wrong about schema behaviour is worse than no comment.)
_POSITION_ECONOMICS_VIEW_SQL = """CREATE VIEW position_economics AS
SELECT
    p.*,
    CASE WHEN p.entry_price > 0
         THEN p.size_usd / p.entry_price END AS notional_shares,
    CASE WHEN p.exit_price IS NOT NULL AND p.entry_price > 0
         THEN (p.exit_price - p.entry_price) * (p.size_usd / p.entry_price)
         END AS realized_pnl_usd,
    CASE WHEN p.exit_price IS NOT NULL AND p.entry_price > 0
         THEN (p.exit_price - p.entry_price) / p.entry_price
         END AS realized_pnl_pct
FROM positions p"""


def _ensure_position_economics_view(conn: sqlite3.Connection) -> None:
    """
    Create the view, and recreate it only when its definition has drifted.

    NOT `CREATE VIEW IF NOT EXISTS`: that silently keeps an old definition
    forever once the view exists, so editing the SQL above would change
    nothing on any database that already ran an earlier version -- the same
    trap the `positions` column migration below exists to avoid.

    NOT an unconditional DROP + CREATE either. This runs on EVERY connection
    (storage opens one per call site), and a schema write on each would take
    a write lock and bump the schema cookie, invalidating prepared statements
    across the daemon for a view that holds no data. Comparing the stored SQL
    first makes the common case a single read.

    THE REBUILD IS NOT ATOMIC, so the CREATE tolerates a peer having won the
    race. Each execute() here is its own transaction in autocommit, and more
    than one process reaches this code: the daemon and the dashboard
    generator both open connections, the latter on a 5-minute timer. Two of
    them interleaving as DROP, DROP, CREATE, CREATE would leave the second
    CREATE raising "view position_economics already exists" out of
    _connect(), i.e. out of the one function every storage call goes
    through. IF NOT EXISTS makes that loser a no-op, which is correct: the
    peer just wrote the definition this process was about to write. The
    window is only open until someone has stored the current definition, so
    in practice this is the first moments after a deploy or an edit to the
    SQL above.

    IF NOT EXISTS is injected HERE rather than kept in
    _POSITION_ECONOMICS_VIEW_SQL because sqlite_master stores the CREATE
    text with those words STRIPPED. Putting them in the constant would make
    the comparison above never match, and the view would then be dropped and
    rebuilt on every single connection -- precisely the schema-write storm
    the comparison exists to avoid.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'position_economics'"
    ).fetchone()
    if row is not None and (row[0] or "").strip() == _POSITION_ECONOMICS_VIEW_SQL.strip():
        return
    conn.execute("DROP VIEW IF EXISTS position_economics")
    conn.execute(_POSITION_ECONOMICS_VIEW_SQL.replace(
        "CREATE VIEW ", "CREATE VIEW IF NOT EXISTS ", 1))


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
            order_id TEXT,
            model_prob REAL,
            raw_edge REAL,
            net_ev_at_size REAL,
            entry_bid REAL
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS is a no-op against a database that already has
    # a `positions` table from before these columns existed -- it does
    # NOT add columns to an existing table. Without this migration, an
    # existing deployed database silently keeps the old schema and every read
    # of the new fields (size_shares, execution_mode, order_id) fails or, for
    # SELECT *, just returns short rows. Run on every connection so every
    # code path (backtest scripts, tests, the live executor) gets migrated,
    # and check PRAGMA table_info first so this stays idempotent -- ALTER
    # TABLE ADD COLUMN errors if the column is already there.
    #
    # model_prob/raw_edge/net_ev_at_size are NULL on every row written before
    # them, and that is the honest value: those trades really do have no
    # stored prediction. Do NOT backfill them by recomputing -- today's
    # calibration has seen the outcomes those rows were entered before.
    #
    # entry_bid is the same kind of column and carries the same rule with more
    # force: it is the entry-side BID, and no book from a past cycle can be
    # re-quoted. NULL means the position was opened before the column existed
    # (or by manual_trigger), and risk_manager falls back to the old
    # entry_price basis for exactly those rows.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    for column_name, column_ddl in (
        ("size_shares", "size_shares REAL"),
        ("execution_mode", "execution_mode TEXT NOT NULL DEFAULT 'paper'"),
        ("order_id", "order_id TEXT"),
        ("model_prob", "model_prob REAL"),
        ("raw_edge", "raw_edge REAL"),
        ("net_ev_at_size", "net_ev_at_size REAL"),
        ("entry_bid", "entry_bid REAL"),
    ):
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {column_ddl}")

    # Every real-money order SUBMISSION, filled or not. Separate from
    # `positions` because an unfilled FOK deliberately writes no position --
    # see record_live_order_attempt() for why that broke the daily cap.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_order_attempts (
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            station_icao TEXT NOT NULL,
            target_date TEXT,
            bucket_c INTEGER,
            side TEXT,
            notional_usd REAL,
            size_shares REAL,
            limit_price REAL,
            outcome TEXT NOT NULL,
            order_id TEXT,
            detail TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_loa_ts ON live_order_attempts(kind, ts)")

    # WHICH BUCKET THE MARKET SETTLED INTO, per station-day. DELIBERATELY
    # NOT `observations`, and the distinction is the whole point.
    #
    # `observations` holds TEMPERATURES from a named meteorological source,
    # and a station's resolution_grade_source rows there are what the bias
    # gate, the backtest's Brier scoring and position_manager's dead-book
    # fallback all treat as settlement truth. A bucket is not a temperature:
    # it is a 1-degree interval, and turning it into a number requires
    # taking a midpoint, which is an ESTIMATE. Writing that midpoint into
    # `observations` would make an estimate indistinguishable from a
    # reading, forever, in the one table where that distinction is load-
    # bearing.
    #
    # So the raw fact is stored -- bucket, and the event bounds that say
    # whether it is an interior bucket or a censored edge catch-all -- and
    # the midpoint is derived at read time by whoever needs it, in the open.
    # Storing the bucket rather than a computed bias also keeps the
    # evidence: the estimator can change without a re-ingest, and a wrong
    # number can be traced back to the day that produced it.
    #
    # bucket_min_c/bucket_max_c are the LIVE-DERIVED bounds from that day's
    # discovered token map, never config.STATIONS' -- config's are a
    # seasonal cross-check that drifts (ten of thirteen stations had by
    # 2026-08-14, two by 5C), and the bounds are what decide whether a
    # settlement is usable at all.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settled_buckets (
            station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            bucket_c INTEGER NOT NULL,
            bucket_min_c INTEGER NOT NULL,
            bucket_max_c INTEGER NOT NULL,
            source TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            bucket_unit TEXT NOT NULL DEFAULT 'C',
            bucket_step INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (station_icao, target_date)
        )
        """
    )

    # bucket_unit/bucket_step: added after the table already existed in
    # deployed databases. DEFAULT 'C' / DEFAULT 1 is what makes every row
    # recorded before this migration correct with no backfill -- every
    # settlement recorded before today WAS on the Celsius whole-degree axis.
    existing_settled_bucket_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(settled_buckets)").fetchall()
    }
    for column_name, column_ddl in (
        ("bucket_unit", "bucket_unit TEXT NOT NULL DEFAULT 'C'"),
        ("bucket_step", "bucket_step INTEGER NOT NULL DEFAULT 1"),
    ):
        if column_name not in existing_settled_bucket_columns:
            conn.execute(f"ALTER TABLE settled_buckets ADD COLUMN {column_ddl}")

    # The ECMWF ensemble spread the live path already fetches, kept instead
    # of discarded. calibration.estimate_std_dev() returns the "ensemble"
    # tier BEFORE it ever reaches measured_error_spread(), and the fetch
    # succeeds for every station on every cycle -- so the measured tier is
    # unreachable live, and no record exists to score the two against each
    # other. This table is that record, and nothing reads it on the trading
    # path: collection only.
    #
    # std_dev_c is the RAW stdev across members, deliberately NOT clamped by
    # calibration._clamp_spread(). SPREAD_FLOOR_C already lifts several
    # stations' ensembles, so storing the clamped number would hide the very
    # quantity a later comparison has to see.
    #
    # Keyed per (station, target_date), so re-scanning a day overwrites
    # rather than accumulating one row per cycle.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ensemble_spread (
            station_icao TEXT NOT NULL,
            target_date TEXT NOT NULL,
            std_dev_c REAL NOT NULL,
            member_count INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (station_icao, target_date)
        )
        """
    )

    # After the ALTER TABLE migration above, so the view is defined against
    # the migrated `positions` shape rather than a short one.
    _ensure_position_economics_view(conn)
    return conn


def save_forecast(forecast: PointForecast) -> None:
    """Persist a single forecast pull. Safe to call repeatedly (idempotent per fetch)."""
    if forecast is None:
        return
    with _db() as conn:
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
    with _db() as conn:
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
    with _db() as conn:
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
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE station_icao = ? AND source = ?",
            (station_icao, source),
        ).fetchone()
    return int(row[0]) if row else 0


def _forecast_means_in_local_day(station_icao: str) -> Dict[date, float]:
    """
    Per target date, the mean of the forecasts ISSUED DURING THAT
    STATION'S OWN LOCAL TARGET DAY -- the set the live blend actually
    averages, and the single definition both public callers below share.

    THE WINDOW REPLACED A UTC DATE COMPARISON, AND IT CUTS BOTH ENDS.
    `date(fetched_at) <= target_date` was wrong in two ways at once once
    day-ahead forecasts started being stored (2026-08-21):

      * its upper end leaked HINDSIGHT. A UTC+8 day ends at 16:00Z, so
        every row fetched 16:00-23:59Z on the target date had already seen
        the maximum it "forecast". 72 real WSSS rows qualified, and
        dropping them moved that station's measured spread 0.6437 ->
        0.6931 -- the lookahead was making the model look MORE certain,
        which is the dangerous direction (see config.SPREAD_FLOOR_C).

      * it had no lower end at all, so a 41h-lead day-ahead row would be
        averaged into the same mean as the 17h row the blend used. The
        bias correction would then be fitted on a forecast set nobody
        trades on -- exactly the drift forecast_means_by_date() warns
        about below.

    config.local_day_bounds_utc() owns the arithmetic; spread_audit.py
    measures against the same bound, so "lookahead" and "negative lead"
    are one concept with one implementation.
    """
    return {
        d: sum(t for _, t in rows) / len(rows)
        for d, rows in _forecast_rows_in_local_day(station_icao).items()
    }


def _forecast_rows_in_local_day(station_icao: str) -> Dict[date, List[Tuple[str, float]]]:
    """
    {target_date: [(source, max_temp_c)]} for rows fetched INSIDE the
    station's own local target day.

    The single implementation of that window. _forecast_means_in_local_day()
    averages it and forecast_source_mix_by_date() takes its key set, so the
    bias and the mix that guards it can never be measured over different
    forecast sets.
    """
    station = config.get_station(station_icao)
    buckets: Dict[date, List[Tuple[str, float]]] = {}
    for target_date_iso, fetched_at, temp_c, source in forecast_rows_with_fetch_time(
        station_icao
    ):
        try:
            target_date = date.fromisoformat(target_date_iso)
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            # A row we cannot place in time cannot be shown to be free of
            # lookahead, so it does not get the benefit of the doubt.
            continue
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        start, end = config.local_day_bounds_utc(station, target_date)
        if not (start <= fetched < end):
            continue
        buckets.setdefault(target_date, []).append((source, temp_c))
    return buckets


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

    That mirror is why this excludes config.FORECAST_SOURCES_EXCLUDED_BY_STATION
    too. A source kept out of the blend but left in this average would have the
    bias correction chasing an error the estimate no longer contains -- and for
    the case that motivated the exclusion list (RKSI/GFS, 3-7C cold) it would
    push the corrected estimate the wrong way by roughly half that gap.

    A date whose ONLY forecast came from an excluded source drops out of the
    sample entirely, which is the same thing config.blendable_forecasts() does
    on the live path: no blended forecast that day, so no error to measure.
    The pair stays consistent in both directions.
    """
    return [err for _, err in forecast_error_samples_dated(station_icao, source)]


def forecast_error_samples_dated(station_icao: str, source: str) -> List[Tuple[date, float]]:
    """
    forecast_error_samples() with each error's target date kept alongside it,
    so calibration.bias_stats_weighted() can age the sample.

    THE DEFINITION LIVES HERE AND forecast_error_samples() WRAPS IT. Both
    used to carry their own copy of the pairing rule, which is the drift
    _forecast_means_in_local_day()'s docstring is about: when that window
    moved from a UTC date comparison to the station's own local day, a
    second copy would have kept the old rule and quietly measured the bias
    over a forecast set nobody trades on.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date, max_temp_c FROM observations "
            "WHERE station_icao = ? AND source = ? AND max_temp_c IS NOT NULL",
            (station_icao, source),
        ).fetchall()
    truth = {str(r[0]): float(r[1]) for r in rows}

    means = _forecast_means_in_local_day(station_icao)
    return [
        (target_date, mean - truth[target_date.isoformat()])
        for target_date, mean in means.items()
        if target_date.isoformat() in truth
    ]


def forecast_means_by_date(station_icao: str) -> Dict[date, float]:
    """
    The per-date blended forecast mean for this station, in degrees C, for
    every target date it has any usable stored forecast for.

    THIS IS forecast_error_samples()' FORECAST TERM, LIFTED OUT. Same
    sources, same exclusion list, same `date(f.fetched_at) <= target_date`
    lookahead rule, same AVG -- the only thing dropped is the join to
    observations, because the caller supplies the truth instead.

    That caller is bucket_bias_audit.py, which measures the same bias
    against the bucket the MARKET settled into rather than against a
    settlement-grade temperature. Hong Kong is the case that needs it:
    HKO's climate extract publishes a month at a time, weeks late, so
    VHHH's observations and its forecasts cover disjoint date ranges and
    forecast_error_samples() returns nothing at all for it.

    It lives HERE, next to forecast_error_samples(), and not in the audit
    module, for exactly the reason that function's own docstring gives for
    mirroring blend_central_estimate: two copies of "which forecasts count"
    drift, and a bias measured over a different forecast set than the one
    the estimate blends is not the number anybody thinks it is.
    """
    return _forecast_means_in_local_day(station_icao)


def forecast_rows_with_fetch_time(station_icao: str) -> List[Tuple[str, str, float]]:
    """
    Every usable stored forecast for one station as
    (target_date, fetched_at, max_temp_c, source), UNAGGREGATED and with no
    lookahead filter of its own.

    `source` was added 2026-08-28 for forecast_source_mix_by_date(). It is
    carried here rather than fetched by a second query so that WHICH ROWS
    COUNT -- the FORECAST_SOURCES_EXCLUDED_BY_STATION filter below -- keeps
    exactly one implementation. Callers that do not need it unpack and
    discard it (spread_audit.py).

    THE TWO OMISSIONS ARE THE POINT, and neither is a relaxation.
    forecast_error_samples() and forecast_means_by_date() both AVG per
    target date and both cut lookahead at `date(fetched_at) <= target_date`
    -- a UTC calendar comparison. spread_audit.py needs to re-average the
    subset of rows that existed at a given LEAD, measured against the end
    of the station's own local day, so it needs the fetch times the AVG
    throws away and a cutoff strictly tighter than the UTC one (a station
    at UTC+8 finishes its day at 16:00Z, so 16:00Z-23:59Z on the target
    date is same-calendar-day and still lookahead). Handing back rows and
    letting the caller cut is the only way to get that; the caller is
    responsible for applying a non-negative lead, and its tests pin it.

    What is NOT delegated is WHICH FORECASTS COUNT. The
    FORECAST_SOURCES_EXCLUDED_BY_STATION filter is applied here, for the
    reason forecast_means_by_date() gives at length: two copies of that
    rule drift, and a spread measured over a different forecast set than
    the one blend_central_estimate() averages is not the number anybody
    thinks it is.
    """
    excluded = tuple(config.FORECAST_SOURCES_EXCLUDED_BY_STATION.get(station_icao, ()))
    exclusion_sql = ""
    params = [station_icao]
    if excluded:
        exclusion_sql = f"  AND source NOT IN ({','.join('?' * len(excluded))}) "
        params.extend(excluded)

    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date, fetched_at, max_temp_c, source FROM forecasts "
            "WHERE station_icao = ? AND max_temp_c IS NOT NULL "
            + exclusion_sql,
            params,
        ).fetchall()
    return [(str(r[0]), str(r[1]), float(r[2]), str(r[3])) for r in rows]


def forecast_source_mix_by_date(station_icao: str) -> Dict[date, frozenset]:
    """
    {target_date: frozenset of forecast sources} over exactly the rows the
    bias correction is fitted on.

    Shares _forecast_rows_in_local_day() with forecast_error_samples() ON
    PURPOSE, for the reason that function's docstring gives about drift: a
    second copy of the local-day window rule would eventually answer a
    different question, and a mix measured over a different forecast set
    than the bias was fitted on is exactly the mismatch this feeds
    entry_manager.collection_only_reason() to detect.
    """
    return {
        d: frozenset(src for src, _ in rows)
        for d, rows in _forecast_rows_in_local_day(station_icao).items()
    }


def save_settled_bucket(
    station_icao: str,
    target_date: date,
    bucket_c: int,
    bucket_min_c: int,
    bucket_max_c: int,
    source: str,
    bucket_unit: str = "C",
    bucket_step: int = 1,
) -> None:
    """
    Record which bucket one station-day's market settled into.

    Idempotent per (station, date): a settlement is immutable history, and
    re-running the ingest must not multiply rows. REPLACE rather than
    IGNORE so a bounds correction can be re-recorded, since the bounds come
    from a discovered token map that a later sweep may read more completely.

    bucket_unit/bucket_step record the axis this settlement was scored on,
    so a later re-score of an old day uses the axis that day actually
    settled on rather than today's registry.
    """
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settled_buckets "
            "(station_icao, target_date, bucket_c, bucket_min_c, bucket_max_c, source, recorded_at, "
            "bucket_unit, bucket_step) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (station_icao, target_date.isoformat(), int(bucket_c), int(bucket_min_c),
             int(bucket_max_c), source, datetime.now(timezone.utc).isoformat(),
             bucket_unit, int(bucket_step)),
        )


def load_settled_buckets(station_icao: str) -> Dict[date, Tuple[int, int, int, str, int]]:
    """
    {target_date: (bucket_c, bucket_min_c, bucket_max_c, bucket_unit, bucket_step)}
    for one station.

    The unit and step are stored per ROW, not read from the registry,
    because a settlement is immutable history: if a market's axis ever
    changes, the old rows must keep describing themselves.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date, bucket_c, bucket_min_c, bucket_max_c, "
            "bucket_unit, bucket_step "
            "FROM settled_buckets WHERE station_icao = ?",
            (station_icao,),
        ).fetchall()
    return {
        date.fromisoformat(str(r[0])): (
            int(r[1]), int(r[2]), int(r[3]), str(r[4]), int(r[5])
        )
        for r in rows
    }


def save_ensemble_spread(
    station_icao: str,
    target_date: date,
    std_dev_c: float,
    member_count: int,
    fetched_at: str,
) -> None:
    """
    Record the raw ensemble dispersion behind one station-day's estimate.

    REPLACE per (station, date): the scheduler re-fetches on every cycle of
    the morning, and the question this record exists to answer is about the
    number the day was priced from, not about every intraday re-read. The
    last write of the day wins, which is the one closest to the decision.

    std_dev_c is stored UNCLAMPED -- see the table comment in _connect().
    """
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ensemble_spread "
            "(station_icao, target_date, std_dev_c, member_count, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (station_icao, target_date.isoformat(), float(std_dev_c),
             int(member_count), fetched_at),
        )


def load_ensemble_spreads(station_icao: str) -> Dict[date, Tuple[float, int]]:
    """{target_date: (std_dev_c, member_count)} for one station."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date, std_dev_c, member_count "
            "FROM ensemble_spread WHERE station_icao = ?",
            (station_icao,),
        ).fetchall()
    return {
        date.fromisoformat(str(r[0])): (float(r[1]), int(r[2]))
        for r in rows
    }


def settled_bucket_dates(station_icao: str) -> set:
    """The target dates already recorded, so an ingest can skip them cheaply."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT target_date FROM settled_buckets WHERE station_icao = ?",
            (station_icao,),
        ).fetchall()
    return {date.fromisoformat(str(r[0])) for r in rows}


def load_forecast_history(station_icao: str, source: str, limit: int = 90) -> List[PointForecast]:
    """Load past forecasts from one source for one station, most recent first."""
    with _db() as conn:
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
    model_prob = r[18] if len(r) > 18 else None
    raw_edge = r[19] if len(r) > 19 else None
    net_ev_at_size = r[20] if len(r) > 20 else None
    entry_bid = r[21] if len(r) > 21 else None
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
        model_prob=model_prob,
        raw_edge=raw_edge,
        net_ev_at_size=net_ev_at_size,
        entry_bid=entry_bid,
    )


def open_position(position: Position) -> None:
    """Persist a newly-entered position. position.status should be 'open'."""
    with _db() as conn:
        # Columns NAMED, not positional. A bare VALUES(...) list binds by
        # ordinal, so it depends on the CREATE TABLE order matching the tuple
        # below -- and the migration above appends columns over time, which is
        # exactly how that pairing drifts. Naming them makes a mismatch a
        # loud error instead of a silent column swap.
        conn.execute(
            """
            INSERT OR REPLACE INTO positions (
                position_id, station_icao, target_date, bucket_c, side,
                entry_price, size_usd, entry_time, status, high_water_mark,
                exit_price, exit_time, exit_reason, token_id, is_paper,
                size_shares, execution_mode, order_id,
                model_prob, raw_edge, net_ev_at_size, entry_bid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
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
                position.size_shares,
                position.execution_mode,
                position.order_id,
                position.model_prob,
                position.raw_edge,
                position.net_ev_at_size,
                position.entry_bid,
            ),
        )


def update_high_water_mark(position_id: str, new_high_water_mark: float) -> None:
    """Persist an updated high-water-mark for an open position -- called every scan cycle the peak price moves."""
    with _db() as conn:
        conn.execute(
            "UPDATE positions SET high_water_mark = ? WHERE position_id = ?",
            (new_high_water_mark, position_id),
        )


def close_position(position_id: str, exit_price: float, exit_time: str, status: str, reason: str) -> None:
    """Mark a position closed -- status should be one of 'closed_take_profit', 'closed_stop_loss', 'closed_trailing_stop', 'closed_resolution' (see models.Position.status)."""
    with _db() as conn:
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
    with _db() as conn:
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


def load_settled_live_tokens() -> Dict[str, Tuple[float, float]]:
    """
    token_id -> (size_shares, exit_price) for LIVE positions this database
    closed as `closed_resolution`.

    These are the holdings that legitimately outlive their position row.
    Nothing in this repo redeems -- executor.close_position() prints
    "REDEEM IT ON THE EXCHANGE" and leaves it to the operator -- and a
    resolved market has no book to sell into either way, so the outcome
    tokens stay in the funding wallet: worthless if the position lost,
    worth par if it won. wallet_client.reconcile_live_positions() takes
    this map so those holdings stop reading as unrecorded exposure, which
    is what halted live trading on 2026-08-22.

    ONLY `closed_resolution`, deliberately. Every other closed status
    exited by SELLING, so shares still held there mean the sell did not
    happen -- a real divergence that must keep blocking entries. Widening
    this query is how the backstop gets a hole in it.

    exit_price is COALESCEd to 0.0: a NULL would propagate into the
    "uncollected money" arithmetic, and unknown is not the same as
    valuable. A row with no token_id is skipped rather than keyed on
    None -- paper rows and pre-token history both hit that case.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT token_id, size_shares, COALESCE(exit_price, 0.0) "
            "FROM positions "
            "WHERE status = 'closed_resolution' AND execution_mode = 'live' "
            "AND is_paper = 0 AND token_id IS NOT NULL"
        ).fetchall()
    return {r[0]: (r[1] or 0.0, r[2]) for r in rows}


def load_position_history(station_icao: str, limit: int = 100, is_paper: Optional[bool] = None) -> List[Position]:
    """Load closed positions for a station, most recent exit first, optionally filtered to paper vs. real."""
    with _db() as conn:
        query = "SELECT * FROM positions WHERE station_icao = ? AND status != 'open'"
        params = [station_icao]
        if is_paper is not None:
            query += " AND is_paper = ?"
            params.append(int(is_paper))
        query += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [_row_to_position(r) for r in rows]


# --------------------------------------------------------------------------
# Live order attempts
# --------------------------------------------------------------------------

def record_live_order_attempt(
    kind: str,
    station_icao: str,
    outcome: str,
    target_date=None,
    bucket_c: Optional[int] = None,
    side: str = "",
    notional_usd: Optional[float] = None,
    size_shares: Optional[float] = None,
    limit_price: Optional[float] = None,
    order_id: Optional[str] = None,
    detail: str = "",
) -> None:
    """
    Append one real-money order SUBMISSION to the audit trail.

    WHY THIS TABLE EXISTS SEPARATELY FROM `positions`. An unfilled FOK
    writes no position, deliberately -- a stored position with no shares
    behind it is the worst thing this codebase can produce. But that left
    config.LIVE_MAX_ORDERS_PER_DAY, documented as "submitted entries per UTC
    day", counting rows in `positions`, i.e. counting FILLS. A day of two
    hundred killed orders consumed none of the ten-order budget. The rate
    limit that exists to bound how hard this system hammers the exchange
    after an upstream fault was measured on the one outcome a fault does not
    produce.

    It is also the only record of a rejected order anywhere: before this,
    an order that was built, submitted and refused left no trace at all
    outside the process log.

    APPEND-ONLY, and never updated. `kind` is "entry" or "exit"; only
    entries feed the daily cap (an exit must never be rate-limited), but
    both are recorded because both are real requests to the exchange.
    """
    with _db() as conn:
        conn.execute(
            "INSERT INTO live_order_attempts "
            "(ts, kind, station_icao, target_date, bucket_c, side, notional_usd, "
            " size_shares, limit_price, outcome, order_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                kind,
                station_icao,
                target_date.isoformat() if hasattr(target_date, "isoformat") else (target_date or ""),
                bucket_c,
                side,
                notional_usd,
                size_shares,
                limit_price,
                outcome,
                order_id,
                detail[:500],
            ),
        )


def count_live_order_attempts(
    kind: str,
    since_iso: str,
    station_icaos: Optional[list] = None,
) -> Optional[int]:
    """
    How many live orders of one kind were SUBMITTED at or after `since_iso`.

    `station_icaos` narrows the count to a set of stations -- how the
    per-region daily order cap is enforced. None means every station, the
    original meaning, kept for callers that predate regions. An EMPTY list
    means no stations and correctly counts 0; it must not be conflated with
    None, or a region with no registered stations would read the global
    total.

    Returns None if the count could not be read. None is not zero: callers
    gating on this must treat an unreadable count as "cannot authorise",
    the same way the reconciliation check does -- a rate limit that fails
    open is not a rate limit.
    """
    try:
        with _db() as conn:
            if station_icaos is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM live_order_attempts WHERE kind = ? AND ts >= ?",
                    (kind, since_iso),
                ).fetchone()
            elif not station_icaos:
                return 0
            else:
                placeholders = ",".join("?" for _ in station_icaos)
                row = conn.execute(
                    f"SELECT COUNT(*) FROM live_order_attempts "
                    f"WHERE kind = ? AND ts >= ? AND station_icao IN ({placeholders})",
                    (kind, since_iso, *station_icaos),
                ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        print(f"[storage] could not count live order attempts: {exc}")
        return None


def load_live_order_attempts(limit: int = 50) -> List[dict]:
    """Most recent submissions first -- the audit trail, for humans."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT ts, kind, station_icao, target_date, bucket_c, side, notional_usd, "
            "       size_shares, limit_price, outcome, order_id, detail "
            "FROM live_order_attempts ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    keys = ("ts", "kind", "station_icao", "target_date", "bucket_c", "side",
            "notional_usd", "size_shares", "limit_price", "outcome", "order_id", "detail")
    return [dict(zip(keys, r)) for r in rows]
