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
Needs POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER, the same two
get_client() always requires. It does NOT need POLYMARKET_LIVE_TRADING --
that gate guards submission, and nothing here submits.

On the deployment box those live in a ROOT-ONLY systemd unit drop-in, in
systemd's config format rather than an env file, so `systemd-run
--property=EnvironmentFile=` cannot read them and neither can an
unprivileged shell. --from-unit asks systemd for the unit's already-
resolved environment and injects only the POLYMARKET_* names into this
process -- no private key on a command line where ps can see it, no
copying secrets to a temp file, and systemd's own quoting rules instead
of a regex guessing at them. It needs root, because reading the drop-in
needs root.

Usage:
    sudo .venv/bin/python check_holdings.py --from-unit    # on the box
    python check_holdings.py                 # if the vars are already exported
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
import shlex
import subprocess
import sys
import time

import config
import storage
from clients import wallet_client

SCALE = 10 ** 6  # py_clob_client_v2.order_builder.helpers.to_token_decimals


def _mask(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 12 else "(unset)"


def _load_env_from_unit(unit: str) -> int:
    """
    Populate os.environ with the POLYMARKET_* variables systemd already
    hands the daemon. Returns how many were loaded.

    WHY NOT A SHELL ONE-LINER. The credentials live in a root-only unit
    drop-in in systemd's own config format, which is NOT an env file --
    systemd-run's EnvironmentFile cannot read it. Every shell workaround
    either puts the private key on a command line (visible in ps) or
    copies it to a temp file. Asking systemd for the resolved environment
    and injecting it in-process avoids both, and lets systemd apply its
    own quoting rules instead of a regex guessing at them.

    Needs root, because that is what reading the drop-in needs. Values are
    never printed -- only the count and the variable NAMES.
    """
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment", "--value"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  could not query systemd for {unit}'s environment: {exc}")
        return 0

    loaded = []
    for token in shlex.split(out):
        key, sep, value = token.partition("=")
        # Only what the client needs. Anything else in that unit is none of
        # this script's business and stays out of the process.
        if sep and key.startswith("POLYMARKET_"):
            os.environ[key] = value
            loaded.append(key)
    if loaded:
        print(f"  loaded {len(loaded)} variable(s) from {unit}: {', '.join(sorted(loaded))}")
    return len(loaded)


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
    ap.add_argument(
        "--from-unit", nargs="?", const="polyweather", metavar="UNIT",
        help="read the POLYMARKET_* credentials from a systemd unit's resolved "
             "environment (default: polyweather). Needs root. Values are never printed.",
    )
    args = ap.parse_args()

    if args.from_unit and not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        print(f"Loading credentials from systemd unit '{args.from_unit}':")
        # geteuid() is POSIX-only; this script is developed on Windows and run
        # on the Linux box, so absence of the call is not "running as root".
        is_root = getattr(os, "geteuid", lambda: -1)() == 0
        if not _load_env_from_unit(args.from_unit) and not is_root:
            print("  nothing loaded, and this is not running as root -- systemd will not "
                  "reveal a unit's Environment to an unprivileged caller. Re-run with sudo.")

    funder = os.environ.get("POLYMARKET_FUNDER")
    if not (os.environ.get("POLYMARKET_PRIVATE_KEY") and funder):
        print("Cannot check: POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER are not in the "
              "environment. Either export them, or re-run as root with --from-unit to "
              "read them from the daemon's own systemd unit.")
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
