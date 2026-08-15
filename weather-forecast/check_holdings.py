"""
check_holdings.py -- read-only holdings audit, and the decisive test for
whether balances come back in SHARES or in raw 6-decimal base units.

WHY THIS EXISTS
---------------
On 2026-08-14 reconciliation began blocking every live entry with:

    1 held but NOT RECORDED: 3017647041... (8885.00 sh)

8885 shares of a weather bucket would be a four-figure position on a book
that trades in single-digit dollars, and the token appears nowhere in this
daemon's journal. The suspicion is that it is not 8885 shares at all:
wallet_client._held_shares() returns float(resp["balance"]) straight from
get_balance_allowance(), and the CLOB wire format is raw base units --
py_clob_client_v2's own to_token_decimals(x) is int(10**6 * x), and the
client passes the REST response back unconverted. If that is right, 8885
raw units is 0.008885 shares: dust, and a false alarm.

That has a second and worse consequence than a blocked entry. The
db_only direction of reconciliation compares:

    held + RECONCILE_SHARE_TOLERANCE < expected

With `held` in raw units a 5.14-share position reads as ~5_140_000, so
that condition can never be true and the check that catches "the database
thinks we hold shares that are gone" -- the one protecting the exit path
from selling a position that no longer exists -- has never been able to
fire. exchange_only fails loudly; db_only fails silently.

WHAT SETTLES IT
---------------
The USDC collateral balance in section 1. You know roughly what is in the
funding wallet. If a wallet holding about twenty dollars reports ~20000000,
balances are raw units and _held_shares() needs a /1e6. If it reports ~20,
they are already human units and the 8885 is a real position that needs
explaining instead.

Section 2 then TRACES the holding: every fill on this wallet inside
config.RECONCILE_TRADE_LOOKBACK_HOURS, printed raw, so the trade that
created any unrecorded balance is visible rather than inferred.

SAFETY
------
Read-only by construction. It builds the same authenticated client the
executor uses and issues authenticated GETs -- nothing here signs, posts,
or cancels, and no code path reaches wallet_client.submit_order(). No key
material is printed; the funder address is masked.

CREDENTIALS
-----------
Needs POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in the environment, the
same two get_client() always requires. It does NOT need
POLYMARKET_LIVE_TRADING -- that gate guards submission, and nothing here
submits. This codebase reads env vars straight from the environment, so
run it under the same systemd environment the daemon uses.

Usage:
    python check_holdings.py                 # collateral + traded tokens + recorded positions
    python check_holdings.py --token <id>    # also inspect one specific token
    python check_holdings.py --json          # raw responses, for when the table is not the authority

Exit codes:
    0  balances look like human units (the 8885 would be a REAL position)
    1  could not check (no credentials, unreachable API)
    2  balances look like raw base units (_held_shares needs /1e6)
"""

import argparse
import json
import os
import sys
import time

import config
import storage
from clients import wallet_client

SCALE = 10 ** 6  # py_clob_client_v2.order_builder.helpers.to_token_decimals


def _mask(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 12 else "(unset)"


def _raw_balance(client, lib, token_id: str, asset_type: str):
    """The balance EXACTLY as the API returns it -- no conversion, because
    the conversion is the thing under test. Returns (value, raw_response)."""
    try:
        if asset_type == "COLLATERAL":
            params = lib.BalanceAllowanceParams(asset_type=lib.AssetType.COLLATERAL)
        else:
            params = lib.BalanceAllowanceParams(
                asset_type=lib.AssetType.CONDITIONAL, token_id=token_id
            )
        resp = client.get_balance_allowance(params)
    except Exception as exc:  # noqa: BLE001 -- report, never raise into an audit
        return None, {"error": str(exc)}

    raw = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", None)
    try:
        return float(raw), resp
    except (TypeError, ValueError):
        return None, resp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--token", help="also inspect this specific token id")
    ap.add_argument("--json", action="store_true", help="print raw API responses")
    args = ap.parse_args()

    funder = os.environ.get("POLYMARKET_FUNDER")
    if not (os.environ.get("POLYMARKET_PRIVATE_KEY") and funder):
        print("Cannot check: POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER are not in the "
              "environment. Run under the same systemd environment as the daemon.")
        return 1

    print(f"funder: {_mask(funder)}   (read-only audit -- nothing here signs or posts)")

    try:
        lib = wallet_client._clob()
        client = wallet_client.get_client()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot check: client unavailable -- {exc}")
        return 1

    # --- 1. THE DECIDING NUMBER ------------------------------------------
    print("\n=== 1. USDC collateral -- the units test ===")
    usdc, resp = _raw_balance(client, lib, None, "COLLATERAL")
    if usdc is None:
        print("  collateral balance unreadable -- cannot run the units test")
        print(f"  response: {resp}")
        verdict_raw = None
    else:
        print(f"  balance as returned : {usdc:,.2f}")
        print(f"  read as raw units   : ${usdc / SCALE:,.6f} USDC")
        print(f"  read as human units : ${usdc:,.2f} USDC")
        verdict_raw = usdc > 100_000  # nobody funds this bot with $100k
        print()
        print("  ^ Compare against what you know is in the wallet. One of those two")
        print("    lines is right; whichever matches your actual balance decides")
        print("    whether _held_shares() is off by 1e6.")
        if args.json:
            print(f"  raw response: {json.dumps(resp, default=str)}")

    # --- 2. TRACE: fills that could have created an unrecorded balance ----
    hours = getattr(config, "RECONCILE_TRADE_LOOKBACK_HOURS", 96)
    print(f"\n=== 2. Fills on this wallet in the last {hours}h (the trace) ===")
    try:
        cutoff = int(time.time()) - hours * 3600
        trades = client.get_trades(lib.TradeParams(after=str(cutoff))) or []
    except Exception as exc:  # noqa: BLE001
        print(f"  trade history unreadable -- {exc}")
        trades = []

    if not trades:
        print("  no fills in the window. An unrecorded balance here predates the")
        print("  lookback, and config.RECONCILE_IGNORE_TRADES_BEFORE may be hiding it.")
    seen = {}
    for t in trades:
        get = t.get if isinstance(t, dict) else (lambda k, d=None: getattr(t, k, d))
        asset = get("asset_id")
        seen.setdefault(asset, []).append(t)
        print(f"  {get('match_time', get('timestamp', '?'))}  {get('side', '?'):4} "
              f"size={get('size', '?')} price={get('price', '?')} "
              f"asset={str(asset)[:14]}... market={str(get('market', ''))[:14]}...")
        if args.json:
            print(f"    {json.dumps(t, default=str)}")

    # --- 3. Balances for every token involved ----------------------------
    print("\n=== 3. Current balance per token ===")
    # Every token this database has ever recorded a LIVE position on, so a
    # returned balance can be held against a known share count. Read per
    # station because that is the only history accessor storage exposes.
    recorded = {}
    for icao in config.STATIONS:
        try:
            for p in storage.load_position_history(icao, is_paper=False):
                if getattr(p, "token_id", None):
                    recorded[p.token_id] = p
        except Exception:  # noqa: BLE001
            continue
    try:
        for p in storage.load_open_positions(is_paper=False):
            if getattr(p, "token_id", None):
                recorded[p.token_id] = p
    except Exception:  # noqa: BLE001
        pass

    tokens = list(seen) + [t for t in recorded if t not in seen]
    if args.token and args.token not in tokens:
        tokens.append(args.token)
    if not tokens:
        print("  no tokens to inspect")

    for token in tokens:
        held, resp = _raw_balance(client, lib, token, "CONDITIONAL")
        pos = recorded.get(token)
        label = (f"{pos.station_icao} {pos.bucket_c}{pos.side} "
                 f"(db: {pos.size_shares} sh, {pos.status})") if pos else "NOT IN DATABASE"
        print(f"\n  {str(token)[:16]}...  {label}")
        if held is None:
            print(f"    balance unreadable -- {resp}")
            continue
        print(f"    balance as returned : {held:,.2f}")
        print(f"    read as raw units   : {held / SCALE:,.6f} shares")
        if pos and getattr(pos, "size_shares", None):
            ratio = held / pos.size_shares if pos.size_shares else 0
            print(f"    db shares           : {pos.size_shares}   -> returned/db = {ratio:,.1f}")
            print("     ^ a ratio near 1,000,000 is the 1e6 scale, proving raw units")
        if args.json:
            print(f"    raw response: {json.dumps(resp, default=str)}")

    # --- verdict ----------------------------------------------------------
    print("\n=== Verdict ===")
    if verdict_raw is None:
        print("  INCONCLUSIVE -- the collateral read failed. Judge from section 3:")
        print("  a returned/db ratio near 1e6 means raw units.")
        return 1
    if verdict_raw:
        print("  Balances look like RAW BASE UNITS (1e6 scale).")
        print("  -> wallet_client._held_shares() must divide by 1e6.")
        print("  -> the 8885 'shares' are 0.008885 shares of dust, and the live-entry")
        print("     block is a false alarm.")
        print("  -> reconciliation's db_only direction has never been able to fire.")
        return 2
    print("  Balances look like HUMAN UNITS -- _held_shares() is correct as written,")
    print("  and the unrecorded holding is a REAL position that needs explaining")
    print("  before live trading is unblocked. Do NOT raise RECONCILE_IGNORE_TRADES_BEFORE")
    print("  to clear it; that hides exposure the live caps cannot see.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
