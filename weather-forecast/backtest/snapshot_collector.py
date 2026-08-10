"""
backtest/snapshot_collector.py

PURPOSE
-------
Optional standalone price/depth snapshot collector, for building up
backtest/price_store.py history on stations or dates the live trading
daemon (ev_engine.py's per-cycle capture, see its _capture_snapshots())
isn't already covering -- e.g. running a wider set of stations purely
for research, or collecting while the trading daemon itself is down.

Not part of the trading path: this module only reads prices and writes
snapshots, it never evaluates EV or places trades.

DEPENDENCIES
------------
requests (via clients/market_client.py)
config.py, market_discovery.py (local)
backtest/price_store.py, backtest/settings.py (local)
"""

import argparse
import time
from datetime import date, timedelta, datetime, timezone
from typing import List

import config
import market_discovery
from clients import market_client
import backtest.price_store as price_store
import backtest.settings as settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_once(station_icaos: List[str], days_ahead: int = 1, db_path=None) -> int:
    """
    For each station in station_icaos, discover token maps for every
    target date in [today, today + days_ahead] (inclusive), upsert each
    discovered token, and save one live price/depth snapshot per token.

    Fail-soft per token: one token's price/depth fetch failing never
    stops the rest of the run. Returns the total number of snapshots
    saved.
    """
    total_saved = 0

    for station_icao in station_icaos:
        try:
            station = config.get_station(station_icao)
        except KeyError as exc:
            print(f"[snapshot_collector] collect_once: {exc}")
            continue

        # Station-local "today" (C6), not raw date.today(): the registry
        # spans UTC+5..+9, so a box running on UTC (or any single fixed
        # offset) reading date.today() mislabels the target date for the
        # entire first several hours of a station's local day -- exactly
        # the bug config.local_today() exists to close everywhere else on
        # the trading path.
        today = config.local_today(station)

        for offset in range(days_ahead + 1):
            target_date = today + timedelta(days=offset)

            # Station's own live-derived cross-check bounds (B3), not the
            # legacy config.BUCKET_MIN_C/MAX_C globals -- those are
            # WSSS/WMKK-era defaults and would ask discover_token_map for
            # the wrong bucket range on every other station.
            token_map = market_discovery.discover_token_map(
                station, target_date, station.bucket_min_c, station.bucket_max_c
            )
            if not token_map:
                print(f"[snapshot_collector] no token map for {station_icao} on {target_date} -- skipping")
                continue

            discovered_at = _now_iso()
            target_date_iso = target_date.isoformat()

            for bucket_c, ids in token_map.items():
                for side, token_key in (("yes", "yes_token_id"), ("no", "no_token_id")):
                    token_id = ids.get(token_key)
                    if not token_id:
                        continue
                    try:
                        price_store.upsert_token(
                            token_id=token_id,
                            station_icao=station_icao,
                            target_date=target_date_iso,
                            bucket_c=bucket_c,
                            side=side,
                            discovered_at=discovered_at,
                            db_path=db_path,
                        )

                        # THE BID, EXPLICITLY -- and this is a KNOWN,
                        # UNFIXED inconsistency with the live path, not an
                        # oversight. This used to call get_token_price(),
                        # which was the bid under an ambiguous name; the
                        # call is now named, so the stored series has not
                        # changed meaning and old rows stay comparable to
                        # new ones.
                        #
                        # But the live entry path now prices off the ASK
                        # (see market_client.get_entry_price_for_side),
                        # so a backtest replaying these snapshots still
                        # enters at the bid and still overstates edge by
                        # the spread -- the exact bug that was just fixed
                        # live. Fixing it here means capturing BOTH sides,
                        # which needs an ask column on price_snapshots
                        # (price_store.py:78) threaded through
                        # save_snapshot/save_snapshots and the readers.
                        # Deliberately deferred: switching this single
                        # column to the ask instead would silently make
                        # every historical row incomparable to every new
                        # one, which is worse than a documented gap.
                        price = market_client.get_token_bid(token_id)
                        depth_usd = market_client.get_available_depth_usd(token_id)

                        price_store.save_snapshot(
                            token_id=token_id,
                            ts=int(time.time()),
                            price=price,
                            depth_usd=depth_usd,
                            source="live_snapshot",
                            fidelity_min=settings.DEFAULT_SNAPSHOT_FIDELITY_MIN,
                            db_path=db_path,
                        )
                        total_saved += 1
                    except Exception as exc:
                        print(
                            f"[snapshot_collector] collect_once: failed on token {token_id} "
                            f"({station_icao} {target_date_iso} bucket {bucket_c} {side}): {exc}"
                        )
                        continue

    return total_saved


def run_collector(station_icaos: List[str], interval_min: int = 5) -> None:
    """
    Loop calling collect_once() then sleeping interval_min minutes,
    until interrupted. Ctrl-C exits cleanly.
    """
    print(f"[snapshot_collector] starting collector loop for {station_icaos}, every {interval_min}min (Ctrl-C to stop)")
    try:
        while True:
            n_saved = collect_once(station_icaos)
            print(f"[snapshot_collector] cycle complete -- {n_saved} snapshots saved")
            time.sleep(interval_min * 60)
    except KeyboardInterrupt:
        print("[snapshot_collector] stopped by user")
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Polymarket price/depth snapshot collector.")
    parser.add_argument("--stations", type=str, required=True, help="Comma-separated ICAO codes, e.g. WSSS,WMKK")
    parser.add_argument("--interval-min", type=int, default=5, help="Minutes between collection cycles (default 5)")
    parser.add_argument("--once", action="store_true", help="Run a single collection cycle and exit")
    args = parser.parse_args()

    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]

    if args.once:
        n_saved = collect_once(stations)
        print(f"[snapshot_collector] single run complete -- {n_saved} snapshots saved")
    else:
        run_collector(stations, interval_min=args.interval_min)
