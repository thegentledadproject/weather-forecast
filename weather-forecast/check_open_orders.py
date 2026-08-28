"""
check_open_orders.py -- read-only inventory of resting CLOB orders.

Cloned from the structure of hermes/manual_trigger.py, with the one thing
that script exists to do removed: this NEVER signs, posts, or cancels
anything. It builds the same authenticated client the executor uses
(clients/wallet_client.get_client()) and issues a single authenticated GET
against /data/orders. No private key material is printed, and no code path
below reaches wallet_client.submit_order().

WHY THIS IS A LEAK DETECTOR, NOT A STATUS PAGE
----------------------------------------------
This bot posts FOK only -- both entries and exits go through
wallet_client.submit_order(), which hardcodes OrderType.FOK (wallet_client.py
:628), and config.LIVE_ENTRY_ORDER_TYPE is "FOK". A fill-or-kill order
either fills completely and immediately or is killed; it never rests on the
book. So in normal operation this script should report ZERO open orders.

Anything it does find is therefore an anomaly worth explaining, not routine
inventory: a manually-placed order from the Polymarket UI, an order left by
some other tool sharing this funder address, or a change that reintroduced
GTC somewhere. That is the reason the exit code distinguishes "none found"
from "found some" -- so a cron line can page on the difference.

FIELD NAMES ARE NOT VERIFIED AGAINST A LIVE RESPONSE
-----------------------------------------------------
py_clob_client_v2 1.0.2's get_open_orders() returns the raw JSON dicts from
/data/orders -- the SDK defines no typed model for them (clob_types.py has
OpenOrderParams for the request and nothing for the response). The column
names below are therefore read defensively with .get() and several
spellings, and every unrecognised key is still surfaced under EXTRA. Use
--json when you need the response exactly as Polymarket sent it, and treat
that, not the table, as the authority.

CREDENTIALS
-----------
Needs POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in the environment (the
same two get_client() always requires). It does NOT need
POLYMARKET_LIVE_TRADING -- that gate guards submission, and nothing here
submits. This codebase reads env vars straight from the environment; there
is no .env loading anywhere in it, so export them or run under the same
systemd EnvironmentFile the daemon uses.

Usage:
    python check_open_orders.py                     # every open order on the account
    python check_open_orders.py --token-id 7134...  # one outcome token
    python check_open_orders.py --market 0xabc...   # one market (condition id)
    python check_open_orders.py --order-id 0x123... # one specific order
    python check_open_orders.py --json              # raw response, for scripting

Exit codes:
    0  no open orders (the expected result for an FOK-only bot)
    1  could not authenticate or the API call failed
    2  open orders found -- inspect them

DEPENDENCIES
------------
argparse, json, sys (standard library)
clients/wallet_client.py, storage.py (local)
py-clob-client-v2 (via wallet_client, lazily imported)
"""

import argparse
import json
import sys


def _parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--token-id", default=None,
                   help="Filter to one outcome token (asset_id / clobTokenId).")
    p.add_argument("--market", default=None,
                   help="Filter to one market (condition id, the 0x... value).")
    p.add_argument("--order-id", default=None,
                   help="Filter to one specific order id.")
    p.add_argument("--json", action="store_true",
                   help="Print the raw response as JSON instead of the table.")
    return p.parse_args()


# Keys we know how to name in the table. Each entry is (heading, candidate
# spellings) -- Polymarket has shipped both camelCase and snake_case on
# adjacent endpoints before, so read for both rather than picking one.
_COLUMNS = [
    ("order_id",   ("id", "orderID", "orderId", "order_id")),
    ("side",       ("side",)),
    ("price",      ("price",)),
    ("size",       ("original_size", "originalSize", "size")),
    ("matched",    ("size_matched", "sizeMatched", "matched_size")),
    ("type",       ("order_type", "orderType", "type")),
    ("status",     ("status",)),
    ("token_id",   ("asset_id", "assetId", "token_id", "tokenId")),
    ("market",     ("market", "condition_id", "conditionId")),
    ("created",    ("created_at", "createdAt", "created")),
    ("expiration", ("expiration", "expiresAt", "expires_at")),
]


def _get(order: dict, candidates) -> str:
    for key in candidates:
        if key in order and order[key] not in (None, ""):
            return str(order[key])
    return ""


def _extra_keys(order: dict) -> dict:
    """Everything the table above does not already show."""
    known = {k for _, candidates in _COLUMNS for k in candidates}
    return {k: v for k, v in order.items() if k not in known}


def _position_labels() -> dict:
    """
    Map token_id -> "ICAO bucket side" from our own open positions, so a
    resting order on a token we hold can be named instead of read as a bare
    id. Purely a local sqlite read; failing it must not cost us the order
    listing, which is the point of the script.
    """
    try:
        import bucket_axis
        import config
        import storage

        def _bucket_label(pos) -> str:
            station = config.STATIONS.get(pos.station_icao)
            if station is None:
                # Unregistered/test station -- no bounds to build a real
                # axis from. Keep the old bare rendering rather than guess.
                return f"{pos.bucket_c}C"
            axis = bucket_axis.for_station(station)
            return axis.label(pos.bucket_c, station.bucket_min_c, station.bucket_max_c)

        return {
            pos.token_id: (
                f"{pos.station_icao} {_bucket_label(pos)} {pos.side} "
                f"({'paper' if pos.is_paper else 'real'})"
            )
            for pos in storage.load_open_positions()
            if pos.token_id
        }
    except Exception as exc:  # noqa: BLE001 -- annotation is a nicety, not the job
        print(f"  (could not read local positions for labelling: "
              f"{type(exc).__name__}: {exc})")
        return {}


def main():
    args = _parse_args()

    from clients import wallet_client

    print("Authenticating CLOB client (read-only -- nothing will be signed or posted)...")
    try:
        client = wallet_client.get_client()
    except Exception as exc:  # noqa: BLE001 -- one message, no traceback with creds in frames
        print(f"ERROR: could not build/authenticate CLOB client: "
              f"{type(exc).__name__}: {exc}")
        sys.exit(1)

    # Safe to import at this point and not before: get_client() has already
    # returned, so the library imported successfully. Keeping it out of module
    # scope preserves wallet_client's rule that a paper-only box (which does
    # not install py-clob-client-v2) can still import and --help this file.
    import py_clob_client_v2 as lib

    params = None
    if args.token_id or args.market or args.order_id:
        params = lib.OpenOrderParams(
            id=args.order_id, market=args.market, asset_id=args.token_id
        )
        shown = {k: v for k, v in {
            "order_id": args.order_id, "market": args.market, "token_id": args.token_id
        }.items() if v}
        print(f"Filtering by {shown}")

    try:
        orders = client.get_open_orders(params)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: get_open_orders() failed: {type(exc).__name__}: {exc}")
        sys.exit(1)

    if args.json:
        print(json.dumps(orders, indent=2, default=str))
        sys.exit(2 if orders else 0)

    if not orders:
        # ASCII only, deliberately: this codebase's own scripts run under a
        # Windows console (cp1252) as well as on the Linux box, and a "*" or
        # a check mark that raises UnicodeEncodeError turns a clean result
        # into a traceback.
        print("\nOK -- no open orders on this account.")
        print("  Expected: entries and exits both post FOK, which never rests on the book.")
        sys.exit(0)

    labels = _position_labels()

    print(f"\nWARNING -- {len(orders)} OPEN ORDER(S) FOUND. This bot posts FOK only, "
          f"so none of these were placed by its normal path.")
    for i, order in enumerate(orders, 1):
        if not isinstance(order, dict):
            print(f"\n[{i}] (unrecognised response element) {order!r}")
            continue
        print(f"\n[{i}]")
        for heading, candidates in _COLUMNS:
            value = _get(order, candidates)
            if value:
                print(f"  {heading:<11} {value}")
                if heading == "token_id" and value in labels:
                    print(f"  {'position':<11} {labels[value]}")
        extra = _extra_keys(order)
        if extra:
            print(f"  {'EXTRA':<11} {json.dumps(extra, default=str)}")

    print("\nThis script does not cancel anything. To cancel, do it from the "
          "Polymarket UI, or write the call deliberately -- client.cancel_order(id) "
          "/ client.cancel_orders([ids]) -- after confirming which of these are yours.")
    sys.exit(2)


if __name__ == "__main__":
    main()
