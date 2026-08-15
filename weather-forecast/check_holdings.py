"""
check_holdings.py -- read-only holdings audit: what the funding wallet
actually holds, which fills put it there, and whether balance readings are
still being converted out of raw base units.

WHAT IT WAS WRITTEN FOR, AND WHAT IT FOUND (2026-08-14/15)
----------------------------------------------------------
Reconciliation began blocking every live entry with:

    1 held but NOT RECORDED: 3017647041... (8885.00 sh)

8885 shares of a weather bucket would have been a four-figure position on
a book trading in single-digit dollars, and the token appeared nowhere in
the daemon's journal. It was not 8885 shares. wallet_client._held_shares()
returned float(resp["balance"]) verbatim, and the CLOB wire format is raw
6-decimal base units -- py_clob_client_v2's own to_token_decimals(x) is
int(10**6 * x), and the REST response comes back unconverted. So the
reading was 1e6 too large.

This script settled it and traced it in one run: USDC collateral returned
6,669,795.00 against a wallet holding about six dollars, and section 2
showed the token's only two fills -- BUY 5.138885 @ 0.72, SELL 5.13 @
0.86. The residue is 5.138885 - 5.13 = 0.008885 shares, i.e. exactly 8885
base units. Dust left by build_exit_order() flooring the sell size onto
the share grid, which it does on purpose because selling more than is held
fails outright.

FIXED in commit 6f2c8d9: _held_shares() now divides by
wallet_client.BALANCE_BASE_UNITS. Note the bug broke reconciliation in
BOTH directions -- exchange_only loudly (the block above), and db_only
silently, because `held + RECONCILE_SHARE_TOLERANCE < expected` can never
be true when held is 1e6 too large, so the check that catches "the
database thinks we hold shares that are gone" had never fired.

WHY IT IS STILL WORTH KEEPING
-----------------------------
Sections 2 and 3 are a general holdings audit and trace, independent of
that incident: what is held, and which fills produced it.

Section 1 is now a REGRESSION CHECK. The API still returns base units --
that is simply the wire format, and correct. What matters is that
_held_shares() keeps converting. So the script reads the balance raw,
deliberately bypassing the conversion, and compares it against what
_held_shares() reports. If they ever agree, the /1e6 has been lost again
and the 2026-08-14 outage is back.

The USDC collateral line remains the human-checkable anchor: you know
roughly what is in the wallet, so a reading of ~6,669,795 against about
six dollars tells you the scale without trusting any code in this file.

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
    0  healthy -- the API returns base units and _held_shares() converts them
    1  could not check (no credentials, unreachable API)
    2  REGRESSION -- _held_shares() is returning base units as if they were
       shares, which is the 2026-08-14 outage returning
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

    # --- 1. SCALE CHECK ---------------------------------------------------
    print("\n=== 1. USDC collateral -- the scale anchor ===")
    usdc, resp = _raw_balance(client, lib, None, "COLLATERAL")
    if usdc is None:
        print("  collateral balance unreadable -- cannot anchor the scale")
        print(f"  response: {resp}")
    else:
        print(f"  balance as returned : {usdc:,.2f}")
        print(f"  read as base units  : ${usdc / SCALE:,.6f} USDC")
        print(f"  read as whole USDC  : ${usdc:,.2f} USDC")
        print()
        print("  ^ The human-checkable anchor: you know roughly what is in the")
        print("    wallet, so whichever line matches tells you the API's scale")
        print("    without trusting any code in this file. It has been base units")
        print("    every time it has been run.")
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

    # Whether _held_shares() still converts. None = never established,
    # because every token read failed or every balance was zero (zero is
    # the one value that looks identical under both scales).
    conversion_applied = None

    for token in tokens:
        held, resp = _raw_balance(client, lib, token, "CONDITIONAL")
        pos = recorded.get(token)
        label = (f"{pos.station_icao} {pos.bucket_c}{pos.side} "
                 f"(db: {pos.size_shares} sh, {pos.status})") if pos else "NOT IN DATABASE"
        print(f"\n  {str(token)[:16]}...  {label}")
        if held is None:
            print(f"    balance unreadable -- {resp}")
            continue
        print(f"    balance as returned : {held:,.2f}   (raw, conversion bypassed)")
        print(f"    as shares (raw/1e6) : {held / SCALE:,.6f}")

        # The regression check. This script reads raw on purpose; production
        # goes through _held_shares(). They must differ by exactly the scale.
        via_production = wallet_client._held_shares(client, token)
        if via_production is not None:
            print(f"    _held_shares() says : {via_production:,.6f} shares")
            if held == 0:
                pass  # zero is scale-invariant and proves nothing either way
            elif abs(via_production - held / SCALE) >= 1e-9:
                conversion_applied = False          # one mismatch is decisive
            elif conversion_applied is None:
                conversion_applied = True           # ... and never un-decides it
        if args.json:
            print(f"    raw response: {json.dumps(resp, default=str)}")

    # --- verdict ----------------------------------------------------------
    print("\n=== Verdict ===")
    if conversion_applied is None:
        print("  INCONCLUSIVE -- no token had a readable, nonzero balance, and zero")
        print("  reads the same under either scale. Judge from section 1: a collateral")
        print("  figure ~1e6 times the wallet's real dollars means base units, which")
        print("  is expected and correct. Re-run when a live position is open.")
        return 1
    if not conversion_applied:
        print("  REGRESSION: _held_shares() is returning the balance unconverted, so")
        print("  reconciliation is seeing share counts 1e6 too large. This is the")
        print("  2026-08-14 outage returning -- exchange_only will block every live")
        print("  entry on exit dust, and db_only will silently never fire again.")
        print("  -> restore the /BALANCE_BASE_UNITS in wallet_client._held_shares().")
        return 2
    print("  Healthy: the API returns base units and _held_shares() converts them to")
    print("  shares, so reconciliation is comparing like with like in both directions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
