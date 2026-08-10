"""
manual_trigger.py -- one-off manual entry, bypassing the edge/EV gates.

Ported from hermes/manual_trigger.py. Same purpose: place ONE specific
bracket/side on demand, to validate the real order path after a code change,
without waiting for the scheduler's cron ticks and without a signal having
to fire.

WHAT IT BYPASSES, AND WHAT IT DOES NOT
--------------------------------------
Bypassed: the model. No forecast, no probability, no EV, no Kelly, no edge
threshold. You are asserting the trade; nothing here checks whether it is a
good one.

NOT bypassed -- and this is the deliberate difference from the hermes
original, which built its own SimpleNamespace signal and went straight to
the execution engine. This script constructs an EntryDecision and hands it
to executor.open_position(), the same single entry path the scheduler uses.
So every non-model safeguard still runs:

  - executor._validated_mode()   station must be in config.LIVE_TRADING_STATIONS
                                 AND STATION_MATURITY == "mature" (today: WSSS
                                 alone). Raises rather than downgrading.
  - wallet_client.build_entry_order()  tick alignment, share rounding, the
                                 exchange minimum, the FOK limit pad
  - executor._price_drift_ok()   abandons if the resolved limit drifted past
                                 config.MAX_ACCEPTABLE_SLIPPAGE_PCT
  - executor._live_budget_breach()  concurrent-position, total-exposure and
                                 orders-per-day backstops
  - fill-only recording          an unfilled FOK writes NOTHING, because a
                                 stored position with no shares behind it is
                                 the worst thing this codebase can produce

--mode simulation (the default) runs all of that against the real live book
and submits nothing. Use it first, every time. It is not a mock: the only
step it skips is the POST.

--mode live PLACES A REAL ORDER WITH REAL FUNDS. It additionally requires
POLYMARKET_LIVE_TRADING=true in the environment -- the second gate, checked
inside wallet_client.submit_order() itself, not merely here.

Usage:
    python manual_trigger.py --station WSSS --bucket 31 --side YES
    python manual_trigger.py --station WSSS --bucket 31 --side NO --date 2026-08-11
    python manual_trigger.py --station WSSS --bucket 31 --side YES --mode live \\
        --i-understand-this-spends-real-money

side YES -> buys that bucket's YES token (the bucket occurs)
side NO  -> buys that bucket's NO token  (the bucket does not occur)

Both are a real BUY. Polymarket's CLOB has no naked short: a NO position is
opened by buying the NO token, which is independently quoted (NegRisk), so
its price is NOT 1 - yes_price. This script never derives one from the other
-- see market_client.get_entry_price_for_side()'s CONTRACT note.

Size is config.LIVE_TRADE_SIZE_USD and is deliberately not a flag. The live
size is a risk decision that lives in config.py next to its reasoning, not
something to retype at 2am.

Exit codes:
    0  order filled (live), or fully resolved and not submitted (simulation)
    1  refused, killed unfilled, or errored -- nothing was recorded
    2  aborted at the confirmation prompt

DEPENDENCIES
------------
argparse, datetime, sys (standard library)
config.py, executor.py, storage.py, models.py, market_discovery.py,
clients/market_client.py, clients/wallet_client.py (local)
"""

import argparse
import sys
from datetime import date

import config
import executor
import storage
import market_discovery
from models import EntryDecision
from clients import market_client, wallet_client


def _parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--station", default="WSSS",
                   help="Station ICAO (default: WSSS). Must be live-eligible.")
    p.add_argument("--bucket", type=int, required=True,
                   help="Bucket in whole degrees C, e.g. 31.")
    p.add_argument("--side", default="YES", choices=["YES", "NO"],
                   help="YES buys the bucket's YES token, NO its NO token (default: YES).")
    p.add_argument("--date", default=None,
                   help="Market date YYYY-MM-DD (default: the station's LOCAL today).")
    p.add_argument("--mode", default="simulation", choices=["simulation", "live"],
                   help="simulation (default) resolves a real order and submits nothing.")
    p.add_argument("--i-understand-this-spends-real-money", action="store_true",
                   help="Required with --mode live, mirroring scheduler.py.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the typed confirmation. Use only for a scripted re-run.")
    return p.parse_args()


def main():
    args = _parse_args()

    if args.station not in config.STATIONS:
        print(f"ERROR: unknown station '{args.station}'. Registered: "
              f"{', '.join(config.STATIONS)}")
        return 1

    # Same conjunction scheduler.py enforces, checked here rather than left to
    # blow up inside executor: a clearer message, and before any network call.
    if not config.live_mode_is_permitted(args.station, args.mode):
        print(f"ERROR: {args.station} is not eligible for '{args.mode}'. A station "
              f"must be BOTH in config.LIVE_TRADING_STATIONS and have "
              f"STATION_MATURITY == 'mature'. Eligible today: "
              f"{', '.join(i for i in config.STATIONS if config.live_mode_is_permitted(i, args.mode)) or '(none)'}")
        return 1

    if args.mode == "live" and not args.i_understand_this_spends_real_money:
        print("ERROR: --mode live requires --i-understand-this-spends-real-money. "
              "It also requires POLYMARKET_LIVE_TRADING=true in the environment; "
              "neither switch is sufficient alone.")
        return 1

    station = config.STATIONS[args.station]
    target_date = date.fromisoformat(args.date) if args.date else config.local_today(station)

    print(f"Discovering {args.station} markets for {target_date} ...")
    token_map = market_discovery.discover_token_map(station, target_date)
    if not token_map:
        print(f"ERROR: no event/markets discovered for {args.station} on {target_date}.")
        return 1
    if args.bucket not in token_map:
        print(f"ERROR: bucket {args.bucket}C not in the discovered event. "
              f"Found: {sorted(token_map)}")
        return 1

    key = "yes_token_id" if args.side == "YES" else "no_token_id"
    token_id = token_map[args.bucket].get(key)
    if not token_id:
        print(f"ERROR: no {args.side} token id for bucket {args.bucket}C on {target_date}.")
        return 1

    # The ASK on this side's OWN token. Never 1 - the other side's price.
    entry_price = market_client.get_entry_price_for_side(token_id, args.side)
    if entry_price is None:
        print(f"ERROR: no live {args.side} ask for bucket {args.bucket}C -- "
              f"nothing to price the order against. Refusing to order blind.")
        return 1

    size_usd = config.LIVE_TRADE_SIZE_USD
    depth = market_client.get_available_depth_usd(token_id)
    slippage = market_client.estimate_slippage(token_id, size_usd)

    print(f"\n{'REAL ORDER' if args.mode == 'live' else 'SIMULATED ORDER'}: "
          f"BUY {args.side} {args.bucket}C {args.station} {target_date}")
    print(f"  token_id       {token_id}")
    print(f"  ask (entry)    {entry_price:.4f}")
    print(f"  size           ${size_usd:.2f}  (config.LIVE_TRADE_SIZE_USD)")
    print(f"  book depth     {f'${depth:.2f}' if depth is not None else 'unknown'}")
    print(f"  est. slippage  {slippage:+.2%}")

    for line in wallet_client.preflight(token_id):
        print(f"  preflight      {line}")

    if args.mode == "live":
        gate = wallet_client.live_trading_enabled()
        print(f"  env gate       POLYMARKET_LIVE_TRADING="
              f"{'true -- THIS WILL SUBMIT' if gate else 'unset -- submit_order() will refuse'}")

    if not args.yes:
        expected = "spend" if args.mode == "live" else "yes"
        answer = input(f'\nType "{expected}" to proceed: ').strip().lower()
        if answer != expected:
            print("Aborted -- nothing submitted.")
            return 2

    # Everything below is the scheduler's own path. The mode is set on the
    # module the same way scheduler.py's __main__ sets it, because
    # _validated_mode() reads it from there and from nowhere else.
    executor.EXECUTION_MODE[args.station] = args.mode

    decision = EntryDecision(
        station_icao=args.station,
        target_date=target_date,
        bucket_c=args.bucket,
        side=args.side,
        # Zeroed, not faked: no Kelly ran. These feed logging only -- the
        # order path reads size, price and token id, all of which are real.
        kelly_fraction_raw=0.0,
        kelly_fraction_applied=0.0,
        recommended_size_usd=size_usd,
        available_depth_usd=depth,
        slippage_at_size_pct=slippage,
        net_ev_at_size=0.0,
        approved=True,
        reason="manual_trigger -- operator asserted, model gates bypassed",
        station_maturity=config.STATION_MATURITY.get(args.station, "exploratory"),
        entry_price=entry_price,
        token_id=token_id,
    )

    before = {p.position_id for p in storage.load_open_positions(args.station)}
    executor.open_position(decision)
    after = storage.load_open_positions(args.station)
    opened = [p for p in after if p.position_id not in before]

    if not opened:
        print("\nNo position recorded -- the order was refused, drifted out, hit a "
              "risk backstop, or was killed unfilled. The [executor] line above "
              "gives the reason.")
        return 1

    pos = opened[0]
    print(f"\nRecorded: {pos.position_id}")
    print(f"  {pos.size_shares} shares @ {pos.entry_price:.4f} = ${pos.size_usd:.2f}  "
          f"({'REAL MONEY' if not pos.is_paper else pos.execution_mode})")
    if pos.order_id:
        print(f"  order_id {pos.order_id}")
    print("  position_manager will now manage its exits on the normal cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
