"""
scheduler.py

PURPOSE
-------
The last piece of the holistic framework: runs the Layer 1->5 chain
(forecast -> EV -> risk -> execution-recommendation) on the
time-varying cadence established across the scanning-schedule and
edge-decay analyses, instead of requiring someone to manually re-run
main.py all day.

Design principle: scan frequency should track information arrival,
not be flat across the day. config.SCHEDULE_WINDOWS encodes that
directly -- tight intervals (10 min) during the confirmed-edge
05:00-08:00 window, widening through the day, closed entirely outside
04:00-22:45.

EVERY WINDOW IS LOCAL, AND "LOCAL" IS NOW PLURAL
------------------------------------------------
The registry spans UTC+5 (Karachi) through UTC+9 (Japan/Korea), so there
is no single local clock to evaluate the schedule against. Stations are
grouped by utc_offset_hours and each group gets its own clock, its own
window and its own next-run time: Tokyo's primary window opens an hour
before Singapore's and four hours before Karachi's.

Per-group next_run_ts bookkeeping is what makes that honest. Sleeping
until the earliest group's next run and then re-running EVERY group
would silently promote each group to the shortest interval in play --
a group in a 3-hour monitor_only window would scan every 10 minutes
because some other timezone is in its primary window. Only groups whose
time has actually arrived are dispatched.

Entry points:
  - determine_window(): pure function, local-time -> active window.
    No I/O, fully unit-testable without waiting for real clock time.
  - run_cycle(): does the actual work for one scan, dispatched by the
    active window's mode, over one group's stations.
  - run_forever(): the actual daemon loop -- group the registry, run
    whichever groups are due, sleep until the earliest next run.

HONEST GAP
----------
run_cycle()'s "primary"/"secondary" modes call ev_engine.run_for_station(),
which depends on market_discovery.py successfully resolving a token
map for that station/date. If discovery fails (event not found, Gamma
API unreachable, station has no Polymarket market that day), the cycle
logs it and continues with position-exit checks only -- it does not
crash the whole scheduler over one station's missing market.

EXECUTION MODE IS PER STATION, NOT PER RUN
-------------------------------------------
--mode sets the BASELINE for the run; executor.py's ladder
(manual_review -> paper -> simulation -> live) decides what each station
actually does. --mode simulation/live promotes only the stations
config.live_mode_is_permitted() allows -- today WSSS alone -- and every
other station falls back to --fallback-mode, so one process runs WSSS on
the real order path and the other twelve on paper simultaneously.

Real orders additionally require --mode live, the explicit
--i-understand-this-spends-real-money flag, and POLYMARKET_LIVE_TRADING
=true in the environment. "simulation" builds the identical order and
submits nothing.

DEPENDENCIES
------------
datetime, time (standard library)
config.py (local)
pipeline.py, ev_engine.py, position_manager.py (local)
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfoNotFoundError

import config
import pipeline
import ev_engine
import position_manager
import executor
import storage
from clients import wallet_client

# Floor on how long the daemon loop may sleep between wake-ups. With 13
# stations across three timezone groups the next-run times interleave, and
# without a floor a group whose boundary is seconds away would spin the
# loop at near-zero intervals. 30s is short enough that no window boundary
# is ever missed by a meaningful margin.
MIN_SLEEP_SECONDS = 30

# When each station was last collected, as a unix timestamp. Read by
# _collection_due(), written by the two cycles that record a station:
# _run_collection_cycle() and _run_full_cycle(). In-process only -- see
# _collection_due() for why that is the right direction to be wrong in.
_last_collection_ts: Dict[str, float] = {}

# WAVE 1. The git sha stamped on every entry_decisions row, resolved once per
# process: config._current_git_sha() shells out to git, and a subprocess per
# station-cycle is not a cost the entry leg should carry. Keyed dict rather
# than a bare Optional so "not yet asked" and "asked, no git" stay distinct.
_config_sha_cache: Dict[str, Optional[str]] = {}


def _config_sha() -> Optional[str]:
    if "sha" not in _config_sha_cache:
        try:
            # git sha + dirty flag + hash of the effective mode (resolved by
            # the CLI into executor.EXECUTION_MODE before run_forever primes this).
            _config_sha_cache["sha"] = config.config_fingerprint(dict(executor.EXECUTION_MODE))
        except Exception:  # noqa: BLE001 -- provenance must never break a cycle
            _config_sha_cache["sha"] = None
    return _config_sha_cache["sha"]


def _boot_storage() -> None:
    """
    WAVE 3 (3a). The daemon is THE writer. Apply the schema once, then open
    every later connection writable; every other process on the box opens
    storage read-only (storage._WRITABLE defaults False) and cannot corrupt
    the file the daemon is writing. deploy_daemon.sh runs the same
    migrate() with the daemon stopped, so on a normal boot this is a no-op
    pass over IF NOT EXISTS statements.

    Also primes _config_sha() here, off the entry path: config._current_
    git_sha() shells out to git, and the first entry cycle after a boot
    used to pay for that subprocess (Wave 1 minor).
    """
    storage.migrate()
    storage.set_writable(True)
    sha = _config_sha()
    print(
        f"[scheduler] boot: storage migrated at {config.DB_PATH} "
        f"({storage.schema_summary()}); writable; config sha {sha or 'unknown'}."
    )


def _record_entry_decisions(decisions, station_icao: str, book: str, cycle_ts: str) -> None:
    """
    Persist a cycle's EntryDecisions as entry_decisions rows. BEST-EFFORT:
    a storage failure here is printed and swallowed, because this runs
    between the decision and the executor and must never be the reason a
    trade did not happen (spec 1b).
    """
    try:
        n = storage.record_entry_decisions(
            decisions, book=book, cycle_ts=cycle_ts, config_sha=_config_sha(),
        )
        print(f"[scheduler] {station_icao}: recorded {n} entry decision(s) as book={book!r}.")
    except Exception as exc:  # noqa: BLE001 -- recording is not trading
        print(
            f"[scheduler] {station_icao}: could not record entry decisions "
            f"(book={book!r}): {exc} -- continuing, the trade path is unaffected."
        )


def _run_shadow_pass(station_icao: str, min_net_ev: float, cycle_ts: str,
                     ev_run, forecast_sources) -> list:
    """
    WAVE 1 (spec 1d): the paper twin of a live station's entry cycle.

    Re-prices the PRIMARY pass's own EV table for execution_mode="paper"
    (same quotes, same slippage, no exit fee), screens it the same way, and
    runs decide_portfolio_entries with the explicit execution_mode="paper"
    override -- paper gates, paper Kelly (no $1 clamp), paper cap/budget
    reads -- then records every decision as book='paper_shadow'.

    READ-ONLY, by construction rather than by convention:
      - never calls executor.open_position (tests/test_wave1_shadow_pass.py
        walks the call graph and asserts it);
      - never writes positions; the paper book's cap/budget state is read,
        not consumed;
      - never touches executor.EXECUTION_MODE -- the override is a parameter;
      - restores entry_manager's once-per-day log dedup sets, so a shadow
        veto cannot silence the primary's journal line for the same bucket;
      - its own gate chatter is captured rather than printed, so the journal
        does not show two VETOED lines per bucket per cycle.
    """
    import contextlib
    import io
    import entry_manager

    paper_results = ev_engine.reprice_for_mode(ev_run.ev_results, execution_mode="paper")
    best = ev_engine.best_opportunities(paper_results, min_net_ev=min_net_ev)
    if not best:
        print(f"[scheduler] {station_icao}: paper shadow -- no candidate clears the screen on the paper table; nothing recorded.")
        return []

    dedup_sets = (
        entry_manager._bucket_cap_vetoes_logged,
        entry_manager._opposite_side_vetoes_logged,
        entry_manager._cooldown_vetoes_logged,
        entry_manager._collection_only_logged,
    )
    saved = [set(s) for s in dedup_sets]
    chatter = io.StringIO()
    try:
        with contextlib.redirect_stdout(chatter):
            decisions = entry_manager.decide_portfolio_entries(
                best, ev_run.token_map, min_net_ev=min_net_ev,
                forecast_sources=forecast_sources, execution_mode="paper",
            )
    except Exception as exc:
        # A dying decide_portfolio_entries would otherwise vanish into the
        # redirected `chatter` buffer and leave _run_full_cycle's failure
        # log line with nothing to go on -- append its last couple of lines
        # so the trace survives the redirect.
        tail = "\n".join(chatter.getvalue().splitlines()[-2:])
        if tail:
            raise RuntimeError(f"{exc} -- last shadow output: {tail!r}") from exc
        raise
    finally:
        for live_set, before in zip(dedup_sets, saved):
            live_set.clear()
            live_set.update(before)

    _record_entry_decisions(decisions, station_icao, "paper_shadow", cycle_ts)
    approved = sum(1 for d in decisions if d.approved)
    print(
        f"[scheduler] {station_icao}: paper shadow -- {len(decisions)} decision(s), "
        f"{approved} approved, recorded as book='paper_shadow'; no position opened."
    )
    return decisions


def local_now(tz_offset_hours: int = 8) -> Tuple[int, int]:
    """
    Current local (hour, minute) at a fixed UTC offset. The default is
    UTC+8 (SGT/MYT), which is what a station-agnostic caller means by
    "local" and what config.LOCAL_UTC_OFFSET_HOURS still says -- but the
    daemon loop passes each timezone group's OWN offset, because the
    schedule windows are local to the station, not to the deployment box.
    """
    utc_now = datetime.now(timezone.utc)
    total_minutes = (utc_now.hour * 60 + utc_now.minute + tz_offset_hours * 60) % (24 * 60)
    return total_minutes // 60, total_minutes % 60


def stations_by_utc_offset(station_icaos: Optional[list] = None) -> Dict[int, List[str]]:
    """
    Group stations by their market timezone -- the unit the daemon loop
    schedules on, since every station sharing an offset also shares a
    window, a next-run time and a cycle.

    Returns {utc_offset_hours: [icao, ...]}, ordered by offset so logs read
    east-to-west in the order the trading day actually opens. An
    unregistered ICAO is logged and skipped rather than raising: one bad
    name on the command line must not stop the other twelve stations from
    trading.

    The offset is RESOLVED, not read: a station carrying an iana_timezone
    (every European entry) reports its current DST offset, so it joins the
    group whose local clock it actually shares right now. See the
    known limitation in run_forever() -- grouping happens once at startup.

    Two failure modes are skipped rather than raised, and they are NOT the
    same: an ICAO that is not in the registry at all, and a registered
    station whose iana_timezone the tz database does not know. Both let the
    other stations keep trading; only the second means a station you believe
    is live is silently absent from every cycle.
    """
    groups: Dict[int, List[str]] = {}
    for icao in (station_icaos or list(config.STATIONS.keys())):
        try:
            offset = config.current_utc_offset_hours(icao)
        except ZoneInfoNotFoundError as exc:
            # REGISTERED, but its iana_timezone is not in the tz database --
            # a typo, or a zone name that has been retired. config.current_utc_
            # offset_hours raises rather than falling back to the static int
            # precisely so this cannot trade on a silently wrong clock; the
            # generic handler below would undo that by reporting it as an
            # unknown station and moving on.
            #
            # Still skip rather than raise: this function's existing stance is
            # that one bad name must not stop the other stations from trading,
            # and that is right. But the message has to say what actually
            # happened, because the consequence is that this station does not
            # trade AT ALL until someone fixes the config.
            print(
                f"[scheduler] {icao} is REGISTERED but its UTC offset could not be "
                f"resolved ({exc}) -- check StationConfig.iana_timezone. It will NOT "
                f"be scheduled and will not trade until this is corrected."
            )
            continue
        except KeyError as exc:
            print(f"[scheduler] skipping unknown station: {exc}")
            continue
        groups.setdefault(offset, []).append(icao)
    return dict(sorted(groups.items()))


def determine_window(hour: int, minute: int) -> Optional[dict]:
    """
    Pure function: given a local (hour, minute), return the active
    schedule window as a dict, or None if somehow no window matches
    (shouldn't happen if SCHEDULE_WINDOWS covers the full 24h, but
    fails safe rather than crashing if a gap is ever introduced).

    Testable without touching the real clock -- pass any (hour, minute).
    """
    minute_of_day = hour * 60 + minute

    windows = list(config.SCHEDULE_WINDOWS)
    if config.ENABLE_MARKET_OPEN_WINDOW:
        # Prepend, not append -- must be checked BEFORE the base "closed"
        # window that would otherwise shadow it for the same time range.
        windows = [config.MARKET_OPEN_WINDOW] + windows

    for (sh, sm, eh, em, interval, mode, min_ev, desc) in windows:
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= minute_of_day < end:
            return {
                "start_minute": start,
                "end_minute": end,
                "interval_min": interval,
                "mode": mode,
                "min_net_ev": min_ev,
                "description": desc,
            }
    return None


def seconds_until_next_boundary(window: dict, hour: int, minute: int) -> int:
    """
    How long until this window's next GRID POINT (start + k*interval), or
    the window's own end -- whichever is sooner. Used by run_forever() to
    decide how long to sleep.

    THE GRID, NOT "ONE INTERVAL FROM NOW". This function is called AFTER a
    cycle has run (see _schedule_next_run), so answering "one interval"
    made the real period interval + cycle duration, and every group's
    cadence drifted by its own cycle length. Measured 2026-08-28 on 13
    stations: the 9-station UTC+8 group took ~3.5 min per cycle and got 14
    of its designed 18 entry-window ticks, while one-station groups got
    17-18 -- i.e. the biggest group, which needs the most watching, was
    quietly scanning at 13.5-minute intervals inside a 10-minute window.

    It also settles a live/replay disagreement rather than creating one:
    backtest/simclock.generate_ticks() has always walked the grid from the
    window start, so every replay already assumed the cadence this now
    delivers.

    A cycle that overruns a grid point waits for the NEXT one. Firing
    immediately would convert a slow cycle into a catch-up burst -- the
    one behaviour the old "fresh clock reading" comment in
    _schedule_next_run was right to worry about.
    """
    minute_of_day = hour * 60 + minute
    end_minute = window["end_minute"]
    minutes_left_in_window = end_minute - minute_of_day

    if window["interval_min"] is None:
        # "closed" windows have no interval -- just sleep until the window ends.
        return max(minutes_left_in_window, 1) * 60

    interval = window["interval_min"]
    elapsed = minute_of_day - window["start_minute"]
    next_grid_minute = window["start_minute"] + (elapsed // interval + 1) * interval
    minutes_ahead = min(next_grid_minute, end_minute) - minute_of_day
    return max(minutes_ahead, 1) * 60


def run_cycle(window: dict, station_icaos: Optional[list] = None) -> None:
    """
    Execute one scan cycle for the given window's mode, across the
    requested stations (defaults to every registered station).
    """
    station_icaos = station_icaos or list(config.STATIONS.keys())
    mode = window["mode"]
    timestamp = datetime.now(timezone.utc).isoformat()

    print(f"\n{'='*70}\n[scheduler] {timestamp}  mode={mode}  ({window['description']})\n{'='*70}")

    if mode == "closed":
        print("[scheduler] closed window -- nothing to do.")
        return

    _ingest_resolution_observations(station_icaos)

    if mode == "collection":
        for icao in station_icaos:
            _run_collection_cycle(icao)
        return

    if mode in ("primary", "secondary"):
        for icao in station_icaos:
            _run_full_cycle(icao, min_net_ev=window["min_net_ev"])
        return

    if mode in ("monitor_only", "risk_only"):
        for icao in station_icaos:
            # The window's own interval is what makes these cycles worth
            # recording: monitor_only/risk_only scan at 15-30 min, and
            # until 2026-08-17 they wrote no price history at all, which
            # is why replays could not see any exit after 10:00 local.
            _run_exit_check(icao, interval_min=window["interval_min"])
            if mode == "risk_only":
                _check_same_day_signal(icao)
            # Collection rides along on its OWN throttle rather than the
            # window's interval: the exit intervals are the resolution of
            # every exit level and must not be spent on data gathering,
            # while the model runs collection exists to catch arrive a few
            # times a day. Exits first, deliberately -- a slow collection
            # pass must never be what delays a stop.
            if _collection_due(icao, time.time()):
                _run_collection_cycle(icao)
        return

    print(f"[scheduler] unrecognized mode '{mode}' -- skipping this cycle.")


def _ingest_resolution_observations(station_icaos: list) -> None:
    """
    Pull any missing recent settlement-grade daily maxima into storage,
    from whichever record each station actually settles on. Both ingests
    self-throttle (at most once per station per local day) and BOTH are
    wrapped so a failure can never break a trading cycle -- observation
    ingest feeds calibration and the collection-first gate, it does not
    gate this cycle's exits.

    Separately guarded on purpose: a METAR outage must not also starve
    Hong Kong's HKO ingest (and vice versa), which is exactly what one
    shared try/except would do -- and starving HKO would hold VHHH in
    collection-only indefinitely while looking like a METAR problem.
    """
    try:
        from clients import metar_client
        metar_client.ingest_missing_recent(station_icaos)
    except Exception as exc:  # noqa: BLE001 - ingest is auxiliary to trading
        print(f"[scheduler] METAR observation ingest skipped: {exc}")

    # Hong Kong settles on the HK Observatory's climate extract, not on any
    # airport METAR (VHHH's metar_ingest_mode is "skip" for that reason), so
    # without this call VHHH would never accumulate a single settlement-grade
    # observation. The import is lazy and the whole block fail-soft because
    # clients/official/hko.py is landing alongside this change -- absence
    # must degrade to "no HK observations yet", never to a broken cycle.
    try:
        hko_icaos = [
            icao for icao in station_icaos
            if config.get_station(icao).resolution_grade_source == "hko_daily_max"
        ]
        if hko_icaos:
            from clients.official import hko
            hko.ingest_missing_recent(hko_icaos)
    except Exception as exc:  # noqa: BLE001 - ingest is auxiliary to trading
        print(f"[scheduler] HKO observation ingest skipped: {exc}")

    # Which BUCKET each past station-day's market settled into. Not an
    # observation and not stored as one (see storage.settled_buckets) --
    # this is what the exchange paid out on, and it is the only measurement
    # of VHHH's forecast bias available before HKO publishes the month.
    # Guarded separately for the same reason HKO is: a settlement-fetch
    # outage must not look like, or be caused by, an observation outage.
    try:
        import bucket_bias
        bucket_bias.ingest_settled_buckets(station_icaos)
    except Exception as exc:  # noqa: BLE001 - ingest is auxiliary to trading
        print(f"[scheduler] market settlement ingest skipped: {exc}")


def _run_full_cycle(station_icao: str, min_net_ev: float) -> None:
    """
    Forecast -> calibration -> EV -> surfaced opportunities -> exit checks,
    for one station.

    ONE FETCH, ONE CALIBRATION, printed and traded. This used to run
    pipeline.run() for the printed table and then build a SECOND estimate
    for the EV leg, because only the second one knew the station's
    measured forecast bias. It cost every station-cycle a duplicate fetch
    of every forecast source (18 rows per station-cycle in the live
    `forecasts` table, two identical batches 2.3s apart), and it printed a
    model nobody traded: RCSS 2026-08-28 21:28 printed p(36C) = 0.41%
    while placing a real order on 36 YES at model_prob 0.2691.

    The bias now goes INTO pipeline.run(), and the estimate it returns is
    the one priced below.
    """
    import entry_manager

    # A full cycle records everything a collection pass does, so it counts
    # as one: without this the first monitor cycle after 08:00 would
    # re-collect a station the 07:5x entry cycle had just recorded.
    _last_collection_ts[station_icao] = time.time()

    # WAVE 3 (3c): EXITS FIRST. A position that resolved overnight must be
    # closed before the entry leg counts open positions against the caps
    # (config.EXIT_CHECK_BEFORE_ENTRIES has the 2026-09-02 case). interval_min
    # is None here for the reason given at the bottom of this function.
    if config.EXIT_CHECK_BEFORE_ENTRIES:
        _run_exit_check(station_icao, interval_min=None)

    try:
        # Measured (forecast - settled truth) for THIS station, so a source
        # that habitually runs cool is read as what it has historically
        # meant. The same number gates entry below: if it is too noisy to
        # correct with, decide_portfolio_entries keeps the station
        # collection-only regardless of what the EV says.
        forecast_bias_c = entry_manager.forecast_bias_stats(station_icao)[0] or 0.0
        result = pipeline.run(
            station_icao=station_icao, forecast_bias_c=forecast_bias_c
        )
        pipeline.print_summary(result)
    except Exception as exc:
        print(f"[scheduler] {station_icao}: pipeline.run() failed this cycle: {exc}")
        return

    # WAVE 1: one timestamp per cycle, shared by every row this cycle records
    # (and by the paper shadow rows, so they pair on it); one book label per
    # station, which is its executor mode.
    cycle_ts = datetime.now(timezone.utc).isoformat()
    book = executor.EXECUTION_MODE.get(station_icao, "manual_review")
    # WAVE 1: what the paper shadow pass below needs from the primary pass,
    # hoisted out of the try so a raise leaves them at their sentinels.
    # primary_ok means the EVALUATION succeeded (pipeline -> EV -> decide ->
    # record) -- NOT that every order placed cleanly. It is set as soon as
    # this cycle's decisions exist (or as soon as we know there are none),
    # before executor.open_position() runs, so a CLOB/network failure
    # placing one order cannot retroactively suppress the shadow pass for
    # decisions that were already recorded.
    ev_run = None
    forecast_sources = None
    primary_ok = False

    try:
        estimate = result["estimate"]
        forecast_sources = list(getattr(estimate, "inputs_used", None) or [])
        # ONE discovery per station-cycle. The EV table, the bucket bounds
        # the model probabilities were computed on, and the token ids entry
        # sizing trades against all come out of the same StationEVRun --
        # re-discovering the map here (as this used to, with the frozen
        # config globals as fallback bounds) is how the prices in the EV
        # table and the tokens being sized end up describing different
        # books, and how a station whose window has drifted gets priced
        # against Singapore's old 25-35 range.
        ev_run = ev_engine.run_for_station_with_map(
            estimate, execution_mode=executor.EXECUTION_MODE.get(station_icao),
        )
        ev_results = ev_run.ev_results
        # Snapshot every computation -- including empty ones -- so the
        # status dashboard can show the latest EV table and its age.
        ev_engine.save_ev_snapshot(station_icao, ev_results)

        if ev_run.veto_reason:
            print(
                f"[scheduler] {station_icao}: station-day VETOED by discovery "
                f"({ev_run.veto_reason}) -- no entries this cycle, exits still checked below."
            )
            primary_ok = True
        elif ev_run.contract_status != "VALID":
            # GAP 7: fail closed. Paper is refused too -- it is the evidence book.
            print(
                f"[scheduler] {station_icao}: entries REFUSED -- market rules check "
                f"{ev_run.contract_status} ({', '.join(ev_run.contract_reasons) or 'no detail'}); "
                f"exits unaffected."
            )
            primary_ok = True
        elif ev_results:
            best = ev_engine.best_opportunities(ev_results, min_net_ev=min_net_ev)
            if best:
                print(f"[scheduler] {station_icao}: {len(best)} candidate(s) clearing the {config.entry_bar_label(min_net_ev)} net EV screen -- running entry_manager sizing/gating:")
                ev_engine.print_ev_table(best)

                entry_decisions = entry_manager.decide_portfolio_entries(
                    best, ev_run.token_map, min_net_ev=min_net_ev,
                    # The blend's ACTUAL source list this cycle, so the gate can
                    # refuse when it differs from the mix the bias was fitted on.
                    forecast_sources=estimate.inputs_used,
                )
                entry_manager.print_entry_decisions(entry_decisions)
                # WAVE 1: recorded BEFORE any executor call, best-effort.
                _record_entry_decisions(entry_decisions, station_icao, book, cycle_ts)
                # The EVALUATION is done as of this line -- flip primary_ok
                # BEFORE the executor loop, so an order-placement failure
                # below (a CLOB/network error on one decision) cannot
                # retroactively cancel the shadow pass for decisions that
                # were already recorded.
                primary_ok = True
                for decision in entry_decisions:
                    executor.open_position(decision, cycle_ts=cycle_ts)
            else:
                print(f"[scheduler] {station_icao}: no opportunities clearing the {config.entry_bar_label(min_net_ev)} net EV threshold this cycle.")
                primary_ok = True
        else:
            primary_ok = True
    except Exception as exc:
        print(f"[scheduler] {station_icao}: EV computation failed this cycle: {exc}")

    # WAVE 1: the paper shadow twin, for live stations only, AFTER the
    # primary pass and only if it completed. Its own failure is contained.
    if executor.EXECUTION_MODE.get(station_icao) == "live":
        if not primary_ok:
            print(f"[scheduler] {station_icao}: paper shadow pass skipped -- the primary pass raised.")
        elif ev_run is None or ev_run.veto_reason or ev_run.contract_status != "VALID" or not ev_run.ev_results:
            print(f"[scheduler] {station_icao}: paper shadow pass skipped -- no EV table this cycle.")
        else:
            try:
                _run_shadow_pass(station_icao, min_net_ev, cycle_ts, ev_run, forecast_sources)
            except Exception as exc:  # noqa: BLE001 -- the shadow must never take the cycle down
                print(f"[scheduler] {station_icao}: paper shadow pass failed: {exc} -- primary decisions unaffected.")

    # DELIBERATELY None on the exit check in an entry window: no exit-path
    # capture here, whichever end of the cycle it runs at.
    #
    # ev_engine.run_for_station_with_map() captures both sides of every
    # bucket this cycle, WITH ask and periodic depth, so an exit-path row
    # would be strictly poorer duplicate data. Worse than useless when the
    # exit check ran AFTER the EV leg (every cycle before Wave 3, and still
    # the EXIT_CHECK_BEFORE_ENTRIES=False path below): its ask-less row was
    # the NEWER one, and get_price_at() returns the newest row before an
    # instant, so a replay pricing an entry on any later tick found
    # ask_price NULL and fell back to the bid -- overstating raw edge by the
    # spread, precisely what the 2026-08-10 entry-pricing fix removed (see
    # engine._entry_price and its n_entry_priced_bid_fallback counter). With
    # exits first the row would be the OLDER one and harmless, but there is
    # still nothing to gain from writing it. The gap this capture exists to
    # close is in monitor_only/risk_only, where nothing captures at all.
    if not config.EXIT_CHECK_BEFORE_ENTRIES:
        _run_exit_check(station_icao, interval_min=None)


def _run_collection_cycle(station_icao: str) -> None:
    """
    Record what is available for one station, and decide nothing.

    Fetches and STORES the forecasts, the ensemble members and both sides
    of every bucket's book -- everything a primary cycle records -- then
    stops before entry_manager. No entry is surfaced and no exit is taken.

    WHY IT MUST NOT TAKE EXITS. The 04:00-05:00 hour has never been able to
    fire a stop, and monitor windows already run their own exit check on
    their own interval. Letting collection exit as well would change WHEN
    the book can be closed while claiming to be a data-collection change,
    and would put a slow network call in front of a stop.

    It calibrates on the station's measured bias, exactly as the trading
    cycle does, so the EV snapshot this leaves behind is the table that
    WOULD have been traded -- not a second, uncorrected model. (See
    pipeline.run: printing one model and trading another is the defect
    that motivated collapsing the two.)
    """
    _last_collection_ts[station_icao] = time.time()

    try:
        # Imported inside the guard on purpose: collection rides along with
        # the exit check in monitor windows, and nothing in a data-gathering
        # step may be able to break the cycle that closes positions.
        import entry_manager

        forecast_bias_c = entry_manager.forecast_bias_stats(station_icao)[0] or 0.0
        result = pipeline.run(
            station_icao=station_icao, forecast_bias_c=forecast_bias_c
        )
    except Exception as exc:  # noqa: BLE001 - collection is never a gate
        print(f"[scheduler] {station_icao}: collection forecast leg failed: {exc}")
        return

    try:
        ev_run = ev_engine.run_for_station_with_map(
            result["estimate"],
            # P1-8(a): a book that SELLS pays a second taker fee, and the EV
            # table has to see it. Passed rather than looked up inside
            # ev_engine -- that would be a circular import.
            execution_mode=executor.EXECUTION_MODE.get(station_icao),
        )
        ev_engine.save_ev_snapshot(station_icao, ev_run.ev_results)
        if ev_run.veto_reason:
            print(
                f"[scheduler] {station_icao}: collected forecasts; discovery "
                f"vetoed the station-day ({ev_run.veto_reason}), so no book to price."
            )
        else:
            print(
                f"[scheduler] {station_icao}: collected -- forecasts stored, "
                f"{len(ev_run.ev_results)} bucket(s) priced and captured. "
                f"No entries surfaced."
            )
    except Exception as exc:  # noqa: BLE001 - collection is never a gate
        print(f"[scheduler] {station_icao}: collection price leg failed: {exc}")


def _collection_due(station_icao: str, now_ts: float) -> bool:
    """
    Whether this station is due a collection pass -- a pure read of
    _last_collection_ts against config.COLLECTION_INTERVAL_MIN.

    Per station, not per group: stations in one timezone group share a
    window but are collected one at a time, and a group that grows should
    not start skipping its later members.

    In-process state, deliberately. A restart re-collects every station
    once, which is the harmless direction to be wrong in -- the opposite
    (persisting the stamps) would let a crash loop starve collection
    exactly when something is already going wrong.
    """
    last = _last_collection_ts.get(station_icao)
    if last is None:
        return True
    return (now_ts - last) >= config.COLLECTION_INTERVAL_MIN * 60


def _run_exit_check(station_icao: str, interval_min: Optional[int] = None) -> None:
    try:
        decisions = position_manager.check_and_exit_positions(
            station_icao=station_icao,
            capture_fidelity_min=interval_min,
        )
        position_manager.print_summary(decisions)
    except Exception as exc:
        print(f"[scheduler] {station_icao}: position exit check failed this cycle: {exc}")


def _check_same_day_signal(station_icao: str) -> None:
    try:
        signal = pipeline.gather_same_day_signal(config.get_station(station_icao))
        print(f"[scheduler] {station_icao} same-day signal: {signal}")
    except Exception as exc:
        print(f"[scheduler] {station_icao}: same-day signal check failed: {exc}")


def _schedule_next_run(offset: int) -> float:
    """
    Absolute wall-clock timestamp for one timezone group's next cycle,
    computed from a FRESH reading of the clock.

    Fresh matters, but not for the reason it originally did. A cycle over
    several stations can take minutes (network calls to Gamma, the CLOB,
    and every forecast source), and the clock read here is what tells
    seconds_until_next_boundary() WHERE ON THE GRID that cycle finished --
    which grid point it already ran past, and therefore which one is next.
    The grid itself is fixed to the window, so a slow cycle now costs the
    ticks it ran through and nothing more; it can neither free-run the
    loop nor drag every later tick of the day later, which is what
    "one interval from now" used to do.
    """
    hour, minute = local_now(offset)
    window = determine_window(hour, minute)
    if window is None:
        print(
            f"[scheduler] WARNING: no schedule window matched {hour:02d}:{minute:02d} "
            f"local at UTC+{offset} -- retrying that group in 5 min."
        )
        return time.time() + 5 * 60
    return time.time() + seconds_until_next_boundary(window, hour, minute)


def run_forever(station_icaos: Optional[list] = None) -> None:
    """
    The actual daemon loop. Runs indefinitely, dispatching run_cycle()
    per TIMEZONE GROUP on the schedule defined by config.SCHEDULE_WINDOWS.
    Intended to be the process entry point for continuous operation (e.g.
    under a process supervisor), not something you run inline in a notebook.

    Each group owns a next_run_ts and is dispatched only when that time has
    arrived. The loop then sleeps until the EARLIEST next_run_ts across
    groups -- waking for one group never re-runs the others, which is the
    whole point: a shared sleep with a shared dispatch would run every
    group at whatever the shortest interval in play happens to be.

    GROUPS ARE COMPUTED ONCE, HERE, AND NEVER RECOMPUTED. For a station
    with a static utc_offset_hours that is simply true. For a station
    carrying an iana_timezone it is a known limitation: crossing a DST
    transition while the daemon runs leaves that station on its
    pre-transition offset -- every schedule window an hour off its real
    local clock -- until the process restarts. Restart the daemon on each
    BST/CEST transition date. Deliberately not solved with live
    regrouping: it is a twice-a-year event, and the same operator-action
    stance the bucket-bounds resweep takes.

    GROUPS ARE ISOLATED IN CADENCE, NOT IN CONCURRENCY. The `for offset in
    due:` loop below dispatches each due group's run_cycle() synchronously
    and waits for it to return before considering the next due group -- there
    is no threading or async here. A slow or hanging cycle in one group (for
    example a 7-station European group running the full pipeline over the
    network) delays every OTHER group that comes due while it is still
    running, even though each group's own next_run_ts is computed
    independently. This coupling is pre-existing -- it already applied
    between the Japan and Singapore groups before Europe was added -- and is
    not introduced by adding more groups; adding a 7-station group simply
    raises how long one group's cycle can take, and hence how long the
    delay to others can be. In practice the more exposed side is a delayed
    EXIT check (monitor_only/risk_only groups run all day, and stops are
    roughly half of this book's closed trades) rather than a delayed entry,
    since entries only matter inside a narrow primary window. Fixing this
    would mean dispatching groups concurrently, which is a separate design
    decision with its own risks and is deliberately not made here.
    """
    _boot_storage()

    groups = stations_by_utc_offset(station_icaos)
    if not groups:
        print("[scheduler] no registered stations to run -- nothing to do.")
        return

    print("[scheduler] starting -- floor is 04:00 local, nothing runs before that by design.")
    for offset, icaos in groups.items():
        print(f"[scheduler]   UTC+{offset}: {', '.join(icaos)}")

    # 0.0 = due immediately, so every group runs once at startup.
    next_run_ts: Dict[int, float] = {offset: 0.0 for offset in groups}

    while True:
        now = time.time()
        due = [offset for offset in groups if next_run_ts[offset] <= now]

        for offset in due:
            hour, minute = local_now(offset)
            window = determine_window(hour, minute)
            if window is None:
                print(
                    f"[scheduler] WARNING: no schedule window matched {hour:02d}:{minute:02d} "
                    f"local at UTC+{offset} -- skipping that group, retrying in 5 min."
                )
                next_run_ts[offset] = time.time() + 5 * 60
                continue

            print(
                f"[scheduler] UTC+{offset} group due at {hour:02d}:{minute:02d} local "
                f"({len(groups[offset])} station(s))."
            )
            run_cycle(window, station_icaos=groups[offset])
            next_run_ts[offset] = _schedule_next_run(offset)

        # Recomputed AFTER the cycles ran, from the current wall clock.
        sleep_seconds = max(MIN_SLEEP_SECONDS, min(next_run_ts.values()) - time.time())
        next_offset = min(next_run_ts, key=next_run_ts.get)
        print(
            f"[scheduler] sleeping {sleep_seconds / 60:.1f} min -- next group due is "
            f"UTC+{next_offset}."
        )
        time.sleep(sleep_seconds)


def _build_parser() -> argparse.ArgumentParser:
    """
    The CLI, as a function so its flags can be tested.

    Built here rather than inline under __main__ because a flag that decides
    whether the daemon boots is exactly the kind of thing that should have a
    test, and nothing inside `if __name__ == "__main__"` can have one.
    """
    parser = argparse.ArgumentParser(description="Run the weather-forecast trading scheduler.")
    parser.add_argument(
        "--mode",
        choices=["manual_review", "paper", "simulation", "live"],
        default="manual_review",
        help=(
            "Baseline execution mode for this run (default: manual_review). "
            "'simulation' and 'live' apply ONLY to stations that satisfy "
            "config.live_mode_is_permitted(); every other station falls back to "
            "--fallback-mode. See the mode ladder in executor.py."
        ),
    )
    parser.add_argument(
        "--fallback-mode",
        choices=["manual_review", "paper"],
        default="paper",
        help=(
            "Mode for stations not eligible for --mode simulation/live "
            "(default: paper). Ignored unless --mode is simulation or live."
        ),
    )
    parser.add_argument(
        "--i-understand-this-spends-real-money",
        action="store_true",
        help="Required with --mode live. Without it, live is refused.",
    )
    parser.add_argument(
        "--require-live",
        action="store_true",
        help=(
            "Refuse to start if any OPEN LIVE position belongs to a station this "
            "run is not authorised to close. Without it the mismatch is reported "
            "loudly and the daemon starts anyway (the default). For the systemd "
            "unit once you trust it."
        ),
    )
    return parser


def enforce_live_requirement(require_live: bool) -> int:
    """
    Report every open live position this process cannot close, and decide
    whether that is fatal. Returns the exit code: 0 to continue.

    WHY THIS EXISTS AT ALL. The deployed mode lives in /etc/polyweather/mode.env,
    which deploy_daemon.sh creates once and never rewrites. Lose that file and
    the daemon comes up without live authorisation, at which point
    executor.close_position() refuses to sell every live position. Safe in
    isolation -- and the result is real money sitting on the exchange with no
    working stop-loss behind it, in a process that runs perfectly normally and
    prints nothing unusual until an exit signal fires that cannot be acted on.

    THE WARNING IS THE DEFAULT AND THE REFUSAL IS OPT-IN. Making the refusal
    unconditional would mean one stranded live position also stops the paper
    book -- which risks nothing, is the larger part of the system, and is the
    part still working. An operator who would rather not boot at all than boot
    half-blind sets --require-live in the unit.

    NEVER AUTO-PROMOTES. A live position found while in a non-live mode is not
    authorisation to go live; it is evidence that something is inconsistent.
    Refusing is the safe direction, promoting is the one that spends money on
    a guess.

    An UNREADABLE book is not a refusal. unmanageable_live_positions() already
    swallows a storage failure and returns [], and turning a transient read
    error into a boot loop would be a worse failure than the one this guards.
    The entry path fails closed on its own.
    """
    stranded = executor.warn_about_unmanageable_live_positions()
    if stranded and require_live:
        print(
            f"[scheduler] REFUSING TO START: --require-live was given and {stranded} open "
            f"live position(s) above belong to stations this run cannot close.\n"
            f"  Fix the mode (POLYWEATHER_MODE=live in /etc/polyweather/mode.env) or close\n"
            f"  those positions and their rows, then start again. Drop --require-live to\n"
            f"  boot anyway with the warning above.\n"
        )
        return 1
    return 0


def _check_live_acknowledgement(parser, args) -> None:
    """
    --mode live requires --i-understand-this-spends-real-money. Extracted so
    the two-switch requirement can be tested; unchanged in behaviour.
    """
    if args.mode == "live" and not getattr(args, "i_understand_this_spends_real_money"):
        parser.error(
            "--mode live requires --i-understand-this-spends-real-money. "
            "It also requires POLYMARKET_LIVE_TRADING=true in the environment; "
            "neither switch is sufficient alone."
        )


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if args.mode in ("simulation", "live"):
        # A station-by-station decision, never a blanket one. --mode live is
        # a statement about the run, not about the registry: only stations
        # config.py already permits get the real order path, and the other
        # twelve keep paper-trading in the same process.
        _check_live_acknowledgement(parser, args)

        promoted, held_back = [], []
        for icao in config.STATIONS:
            if config.live_mode_is_permitted(icao, args.mode):
                executor.EXECUTION_MODE[icao] = args.mode
                promoted.append(icao)
            else:
                executor.EXECUTION_MODE[icao] = args.fallback_mode
                held_back.append(icao)

        print(
            f"[scheduler] {args.mode.upper()} mode active for: "
            f"{', '.join(promoted) if promoted else '(no eligible station)'}."
        )
        print(
            f"[scheduler] {len(held_back)} station(s) not eligible -- running "
            f"'{args.fallback_mode}': {', '.join(held_back)}"
        )
        if args.mode == "live":
            env_gate = wallet_client.live_trading_enabled()
            print(
                f"[scheduler] REAL MONEY. Second gate POLYMARKET_LIVE_TRADING="
                f"{'true -- ORDERS WILL BE SUBMITTED' if env_gate else 'unset -- orders will NOT be submitted'}. "
                f"Trade size ${config.LIVE_TRADE_SIZE_USD:.2f}, "
                f"max {config.LIVE_MAX_CONCURRENT_POSITIONS} concurrent, "
                f"${config.LIVE_MAX_TOTAL_EXPOSURE_USD:.2f} total exposure ceiling."
            )
            # Reconcile once at boot so preflight can name any resolved
            # position still sitting in the wallet. Winners there are
            # UNCOLLECTED MONEY -- nothing in this system redeems -- and the
            # exchange balance is the only thing that separates those from
            # the ones already redeemed by hand. Affordable here in a way it
            # is not inside preflight, which runs on every entry.
            settled = []
            try:
                live_open = [
                    p for p in storage.load_open_positions(is_paper=False)
                    if getattr(p, "execution_mode", "paper") == "live"
                ]
                settled = wallet_client.reconcile_cached(
                    live_open,
                    settled_tokens=storage.load_settled_live_tokens(),
                ).settled_unredeemed
            except Exception as exc:  # noqa: BLE001
                # Never let a reporting nicety stop the daemon booting. The
                # entry path reconciles again and fails closed on its own.
                print(f"[scheduler] preflight: settled-holdings scan skipped ({exc})")
            for line in wallet_client.preflight(settled_unredeemed=settled):
                print(f"[scheduler] preflight: {line}")
        else:
            print(
                "[scheduler] Simulation builds real orders against the real book "
                "and submits NOTHING. No credentials are required."
            )
    elif args.mode == "paper":
        for icao in config.STATIONS:
            executor.EXECUTION_MODE[icao] = "paper"
        print("[scheduler] Paper trading mode active -- no live orders will be submitted.")
    else:
        for icao in config.STATIONS:
            executor.EXECUTION_MODE[icao] = "manual_review"
        print("[scheduler] Manual review mode active -- recommended actions will be printed for a human to execute.")

    # Say it AT BOOT, not once the next scan cycle happens to reach one.
    # An open live position this process cannot close is a silent condition
    # by nature: the daemon runs perfectly normally, prints nothing unusual
    # until an exit signal fires, and meanwhile real shares sit with no
    # working stop-loss. The mode chosen above is exactly what determines
    # whether that is the situation, so this is the moment to check.
    exit_code = enforce_live_requirement(args.require_live)
    if exit_code:
        sys.exit(exit_code)

    run_forever()
