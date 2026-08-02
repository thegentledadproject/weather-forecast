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

Two entry points:
  - determine_window(): pure function, local-time -> active window.
    No I/O, fully unit-testable without waiting for real clock time.
  - run_cycle(): does the actual work for one scan, dispatched by the
    active window's mode.
  - run_forever(): the actual daemon loop, thin wrapper around the
    two functions above plus time.sleep.

HONEST GAP
----------
run_cycle()'s "primary"/"secondary" modes call ev_engine.run_for_station(),
which depends on market_discovery.py successfully resolving a token
map for that station/date. If discovery fails (event not found, Gamma
API unreachable, station has no Polymarket market that day), the cycle
logs it and continues with position-exit checks only -- it does not
crash the whole scheduler over one station's missing market.

Every station currently trades in "manual_review" mode (executor.py's
EXECUTION_MODE default), so "primary"/"secondary" cycles PRINT
recommended entries for a human to act on -- they do not place orders.
That's consistent with the rest of the codebase: nothing here executes
real trades until a station is deliberately promoted to "auto" and a
real entry-placement path is built (still missing, see project summary).

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
from typing import Optional, Tuple

import config
import pipeline
import ev_engine
import position_manager
import executor
from clients.official.registry import get_official_client


def local_now(tz_offset_hours: int = 8) -> Tuple[int, int]:
    """
    Current local (hour, minute) for SGT/MYT (UTC+8). Same fixed
    offset used by risk_manager.py -- both registered stations share
    this timezone; revisit if a station elsewhere is added.
    """
    utc_now = datetime.now(timezone.utc)
    total_minutes = (utc_now.hour * 60 + utc_now.minute + tz_offset_hours * 60) % (24 * 60)
    return total_minutes // 60, total_minutes % 60


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
            _run_exit_check(icao)
            if mode == "risk_only":
                _check_same_day_signal(icao)
        return

    print(f"[scheduler] unrecognized mode '{mode}' -- skipping this cycle.")


def _run_full_cycle(station_icao: str, min_net_ev: float) -> None:
    """Forecast -> calibration -> EV -> surfaced opportunities -> exit checks, for one station."""
    try:
        result = pipeline.run(station_icao=station_icao)
        pipeline.print_summary(result)
    except Exception as exc:
        print(f"[scheduler] {station_icao}: pipeline.run() failed this cycle: {exc}")
        return

    try:
        from datetime import date
        from calibration import calibrate
        from clients import openmeteo_client, climate_monitor_client
        import market_discovery
        import entry_manager

        station = config.get_station(station_icao)
        estimate = calibrate(
            station=station,
            target_date=config.local_today(),
            forecasts=pipeline.gather_forecasts(station),
            observations=climate_monitor_client.load_recent_observations(station, days=30),
            ensemble_members=openmeteo_client.get_ensemble_spread(station),
        )
        ev_results = ev_engine.run_for_station(estimate)
        # Snapshot every computation -- including empty ones -- so the
        # status dashboard can show the latest EV table and its age.
        ev_engine.save_ev_snapshot(station_icao, ev_results)
        if ev_results:
            best = ev_engine.best_opportunities(ev_results, min_net_ev=min_net_ev)
            if best:
                print(f"[scheduler] {station_icao}: {len(best)} candidate(s) clearing {min_net_ev:.0%} net EV screen -- running entry_manager sizing/gating:")
                ev_engine.print_ev_table(best)

                token_map = market_discovery.discover_token_map(
                    station, estimate.target_date, config.BUCKET_MIN_C, config.BUCKET_MAX_C
                )
                if token_map:
                    entry_decisions = entry_manager.decide_portfolio_entries(best, token_map, min_net_ev=min_net_ev)
                    entry_manager.print_entry_decisions(entry_decisions)
                    for decision in entry_decisions:
                        executor.open_position(decision)
                else:
                    print(f"[scheduler] {station_icao}: no token map available for entry sizing this cycle.")
            else:
                print(f"[scheduler] {station_icao}: no opportunities clearing {min_net_ev:.0%} net EV threshold this cycle.")
    except Exception as exc:
        print(f"[scheduler] {station_icao}: EV computation failed this cycle: {exc}")

    _run_exit_check(station_icao)


def _run_exit_check(station_icao: str) -> None:
    try:
        decisions = position_manager.check_and_exit_positions(station_icao=station_icao)
        position_manager.print_summary(decisions)
    except Exception as exc:
        print(f"[scheduler] {station_icao}: position exit check failed this cycle: {exc}")


def _check_same_day_signal(station_icao: str) -> None:
    try:
        signal = pipeline.gather_same_day_signal(config.get_station(station_icao))
        print(f"[scheduler] {station_icao} same-day signal: {signal}")
    except Exception as exc:
        print(f"[scheduler] {station_icao}: same-day signal check failed: {exc}")


def run_forever(station_icaos: Optional[list] = None) -> None:
    """
    The actual daemon loop. Runs indefinitely, dispatching run_cycle()
    on the schedule defined by config.SCHEDULE_WINDOWS. Intended to be
    the process entry point for continuous operation (e.g. under a
    process supervisor), not something you run inline in a notebook.
    """
    print("[scheduler] starting -- floor is 04:00 local, nothing runs before that by design.")
    while True:
        hour, minute = local_now()
        window = determine_window(hour, minute)

        if window is None:
            print(f"[scheduler] WARNING: no schedule window matched {hour:02d}:{minute:02d} local -- sleeping 5 min and retrying.")
            time.sleep(5 * 60)
            continue

        run_cycle(window, station_icaos=station_icaos)

        sleep_seconds = seconds_until_next_boundary(window, hour, minute)
        print(f"[scheduler] sleeping {sleep_seconds // 60} min until next check.")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the weather-forecast trading scheduler.")
    parser.add_argument(
        "--mode",
        choices=["manual_review", "paper"],
        default="manual_review",
        help="Execution mode for every registered station this run (default: manual_review).",
    )
    args = parser.parse_args()

    if args.mode == "paper":
        for icao in config.STATIONS:
            executor.EXECUTION_MODE[icao] = "paper"
        print("[scheduler] Paper trading mode active -- no live orders will be submitted.")
    else:
        print("[scheduler] Manual review mode active -- recommended actions will be printed for a human to execute.")

    run_forever()
