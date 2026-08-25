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
clients/official/registry.py (local)
"""

import argparse
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
from clients.official.registry import get_official_client

# Floor on how long the daemon loop may sleep between wake-ups. With 13
# stations across three timezone groups the next-run times interleave, and
# without a floor a group whose boundary is seconds away would spin the
# loop at near-zero intervals. 30s is short enough that no window boundary
# is ever missed by a meaningful margin.
MIN_SLEEP_SECONDS = 30


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
    How long until either this window's own scan interval next fires,
    or the window itself ends -- whichever is sooner. Used by
    run_forever() to decide how long to sleep.
    """
    minute_of_day = hour * 60 + minute
    minutes_left_in_window = window["end_minute"] - minute_of_day

    if window["interval_min"] is None:
        # "closed" windows have no interval -- just sleep until the window ends.
        return max(minutes_left_in_window, 1) * 60

    return min(window["interval_min"], max(minutes_left_in_window, 1)) * 60


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

    if mode == "pre_poll":
        for icao in station_icaos:
            station = config.get_station(icao)
            official = get_official_client(station.official_client_key)
            forecast = official.get_24hr_forecast(station)
            if forecast and forecast.max_temp_c is not None:
                print(f"[scheduler] {icao}: official forecast appears published (max={forecast.max_temp_c}°C) -- primary window can begin.")
            else:
                print(f"[scheduler] {icao}: no forecast detected yet this poll.")
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
    """Forecast -> calibration -> EV -> surfaced opportunities -> exit checks, for one station."""
    try:
        result = pipeline.run(station_icao=station_icao)
        pipeline.print_summary(result)
    except Exception as exc:
        print(f"[scheduler] {station_icao}: pipeline.run() failed this cycle: {exc}")
        return

    try:
        from calibration import calibrate
        from clients import openmeteo_client
        import entry_manager

        station = config.get_station(station_icao)
        # This station's own market day, not the box's UTC+8 notion of one.
        target_date = config.local_today(station)
        estimate = calibrate(
            station=station,
            target_date=target_date,
            # Same assembly as pipeline.run(): seeds + STORED observations
            # (incl. settlement-grade METAR daily maxima), deduped. The
            # trading path previously passed climate-monitor seeds only,
            # calibrating blind to every reading actually collected.
            observations=pipeline.gather_observations(station, target_date),
            forecasts=pipeline.gather_forecasts(station),
            ensemble_members=openmeteo_client.get_ensemble_spread(station),
            # Measured (forecast - settled truth) for THIS station, so a
            # source that habitually runs cool is read as what it has
            # historically meant. The same number gates entry below: if it
            # is too noisy to correct with, decide_portfolio_entries keeps
            # the station collection-only regardless of what the EV says.
            forecast_bias_c=entry_manager.forecast_bias_stats(station_icao)[0] or 0.0,
        )
        # ONE discovery per station-cycle. The EV table, the bucket bounds
        # the model probabilities were computed on, and the token ids entry
        # sizing trades against all come out of the same StationEVRun --
        # re-discovering the map here (as this used to, with the frozen
        # config globals as fallback bounds) is how the prices in the EV
        # table and the tokens being sized end up describing different
        # books, and how a station whose window has drifted gets priced
        # against Singapore's old 25-35 range.
        ev_run = ev_engine.run_for_station_with_map(estimate)
        ev_results = ev_run.ev_results
        # Snapshot every computation -- including empty ones -- so the
        # status dashboard can show the latest EV table and its age.
        ev_engine.save_ev_snapshot(station_icao, ev_results)

        if ev_run.veto_reason:
            print(
                f"[scheduler] {station_icao}: station-day VETOED by discovery "
                f"({ev_run.veto_reason}) -- no entries this cycle, exits still checked below."
            )
        elif ev_results:
            best = ev_engine.best_opportunities(ev_results, min_net_ev=min_net_ev)
            if best:
                print(f"[scheduler] {station_icao}: {len(best)} candidate(s) clearing {min_net_ev:.0%} net EV screen -- running entry_manager sizing/gating:")
                ev_engine.print_ev_table(best)

                entry_decisions = entry_manager.decide_portfolio_entries(
                    best, ev_run.token_map, min_net_ev=min_net_ev,
                )
                entry_manager.print_entry_decisions(entry_decisions)
                for decision in entry_decisions:
                    executor.open_position(decision)
            else:
                print(f"[scheduler] {station_icao}: no opportunities clearing {min_net_ev:.0%} net EV threshold this cycle.")
    except Exception as exc:
        print(f"[scheduler] {station_icao}: EV computation failed this cycle: {exc}")

    # DELIBERATELY None: no exit-path capture inside an entry window.
    #
    # ev_engine.run_for_station_with_map() above already captured both sides of every
    # bucket this cycle, WITH ask and periodic depth, so an exit-path row
    # here would be strictly poorer duplicate data. Worse than useless: the
    # exit check runs seconds AFTER the EV leg, so its ask-less row is the
    # NEWER one, and get_price_at() returns the newest row before an
    # instant. A replay pricing an entry on any later tick would then find
    # ask_price NULL and fall back to the bid -- overstating raw edge by the
    # spread, which is precisely what the 2026-08-10 entry-pricing fix
    # removed (see engine._entry_price and its n_entry_priced_bid_fallback
    # counter). The exact-ts tie-break in get_price_at() does NOT save us;
    # these two rows land a few seconds apart, not on the same second.
    #
    # The gap this whole change exists to close is in monitor_only/risk_only,
    # where nothing captures at all. That is the only place it should write.
    _run_exit_check(station_icao, interval_min=None)


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

    Fresh matters: a cycle over several stations can take minutes (network
    calls to Gamma, the CLOB, and every forecast source), and scheduling
    the next run off the pre-cycle time would hand back an interval that
    has already partly elapsed -- at the primary window's 10-minute
    cadence, a slow cycle would leave near-zero sleep and effectively
    free-run the loop.
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


if __name__ == "__main__":
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
    args = parser.parse_args()

    if args.mode in ("simulation", "live"):
        # A station-by-station decision, never a blanket one. --mode live is
        # a statement about the run, not about the registry: only stations
        # config.py already permits get the real order path, and the other
        # twelve keep paper-trading in the same process.
        if args.mode == "live" and not getattr(args, "i_understand_this_spends_real_money"):
            parser.error(
                "--mode live requires --i-understand-this-spends-real-money. "
                "It also requires POLYMARKET_LIVE_TRADING=true in the environment; "
                "neither switch is sufficient alone."
            )

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
    executor.warn_about_unmanageable_live_positions()

    run_forever()
