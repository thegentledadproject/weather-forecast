"""
redeem.py

Operator-facing script: collect resolved outcome tokens this repo has never
redeemed. See docs/superpowers/specs/2026-09-01-redemption-design.md for
the full design and the on-chain facts it rests on, and
docs/superpowers/specs/2026-09-03-remediation-plan-revised.md for why this
was the highest-value item on the 2026-09-03 remediation plan.

WHAT IT DOES
------------
--list (default): report what CAN be redeemed, what already has been
    (zero on-chain balance -- cleared, by this script or by hand, needs no
    action), and what is held but cannot currently be redeemed via this
    path. No gas, no signing, no network writes beyond the read-only calls
    the report needs.

--execute: for every redeemable item, simulate first (free, no gas). Only
    a clean simulation is ever signed or broadcast. After broadcast, waits
    for the receipt and RE-READS the balance -- a transaction that mined
    successfully but left shares behind is reported as a failure, because
    for this purpose it is one.

TWO GATES GUARD --execute, NEITHER ALONE SUFFICIENT: this script's own
--execute flag, and POLYMARKET_LIVE_TRADING=true in the environment -- the
SAME flag live orders use (the 2026-09-01 design's own section 6: "as with
live orders"), not a separate one. Redeeming moves real assets and inherits
the same bar as spending them.

SCOPE, DELIBERATELY. This script never runs from the scheduler or the
daemon -- it is operator-run, by design (section 9's "out of scope").
Nothing here retries a stuck transaction or escalates gas automatically;
a human watching the run makes that call.

Usage:
    python redeem.py                              # --list, using exported credentials
    sudo .venv/bin/python redeem.py --from-unit    # on the box
    python redeem.py --execute
    python redeem.py --token <id>
    python redeem.py --station WSSS
    python redeem.py --json

Exit codes: 0 nothing to do / everything cleared; 1 could not check (no
credentials, client unavailable, or the data-api is down and --execute was
requested -- refused rather than acting on an incomplete picture); 2
redeemable or unresolvable items remain after this run (the
manual-redemption flag, in exit-code form, for a cron or a wrapper to
detect).
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

import config
from clients import onchain_client, redemption_client, wallet_client
from clients.redemption_client import RedemptionScan


DEFAULT_DEADLINE_SECONDS = 600  # how long a signed payload stays valid before the
                                 # proxy's own deadline check refuses it -- short on
                                 # purpose, since this script is meant to run once
                                 # and be watched, not queued for later.
DEFAULT_GAS_LIMIT = 300_000     # a generous fixed ceiling for one redeemPositions call
                                 # wrapped in one execute() envelope. Not auto-estimated:
                                 # estimate_gas() needs a clean simulation to run against,
                                 # and simulation already gates broadcast on its own --
                                 # a fixed ceiling here just bounds the worst case.


def _mask(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 12 else "(unset)"


def _load_env_from_unit(unit: str) -> int:
    """
    Populate os.environ with the POLYMARKET_* variables systemd already
    hands the daemon. Same mechanism as check_holdings.py's own helper --
    see that module's docstring for why this exists instead of an
    EnvironmentFile shell trick. Needs root. Values are never printed.
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
        if sep and key.startswith("POLYMARKET_"):
            os.environ[key] = value
            loaded.append(key)
    if loaded:
        print(f"  loaded {len(loaded)} variable(s) from {unit}: {', '.join(sorted(loaded))}")
    return len(loaded)


def _resolve_contract_addresses(chain_id: int = onchain_client.POLYGON_CHAIN_ID):
    """
    NegRiskAdapter + ConditionalTokens addresses, from py_clob_client_v2's
    own contract config -- the SAME source the 2026-09-01 design's probe
    used (cross-checked 2026-09-03: its neg_risk_adapter value matches the
    design doc's recorded address exactly), so this script never hardcodes
    an address that could drift from the library's own.

    Local import, deliberately: this is the one place in the redemption
    stack allowed to depend on py_clob_client_v2, matching
    wallet_client.py's own established pattern of importing config/heavy
    dependencies lazily rather than at module load time. onchain_client.py
    and redemption_client.py both stay importable without this package.
    """
    from py_clob_client_v2.config import get_contract_config
    cfg = get_contract_config(chain_id)
    return cfg.neg_risk_adapter, cfg.conditional_tokens


def gates_open() -> "tuple[bool, str]":
    """
    Gate 2 of 2 (gate 1 is the --execute flag itself, checked by main()
    before this is ever called). SAME flag live orders use -- see the
    module docstring on why this is not a separate flag.
    """
    if not wallet_client.live_trading_enabled():
        return False, (
            "POLYMARKET_LIVE_TRADING is not set to true -- redemption uses the "
            "same gate live orders do, and neither --execute alone nor the "
            "gate alone broadcasts anything."
        )
    return True, ""


# --------------------------------------------------------------------------
# --list
# --------------------------------------------------------------------------

def _bucket_label(item) -> str:
    return f"{item.station_icao} {item.target_date} {item.bucket_c}°{item.side}"


def list_exit_code(scan: RedemptionScan) -> int:
    """0 nothing to do; 2 something remains that this run did not clear --
    the manual-redemption flag, in exit-code form. already_cleared items
    never affect this: they need no action, by definition."""
    return 2 if (scan.redeemable or scan.unresolvable) else 0


def render_list(scan: RedemptionScan, gas_balance_wei: int, json_output: bool = False) -> str:
    """
    The --list report. Pure function of a scan and a gas balance reading,
    so it is testable without argparse, stdout, or a real chain.
    """
    if json_output:
        return json.dumps({
            "redeemable": [
                {"token_id": i.token_id, "station": i.station_icao,
                 "target_date": str(i.target_date), "bucket_c": i.bucket_c,
                 "side": i.side, "shares": i.size_shares, "is_winner": i.is_winner,
                 "value_usd": i.value_usd}
                for i in scan.redeemable
            ],
            "already_cleared": [
                {"token_id": c.token_id, "station": c.station_icao,
                 "target_date": str(c.target_date), "bucket_c": c.bucket_c, "side": c.side}
                for c in scan.already_cleared
            ],
            "unresolvable": [
                {"token_id": u.token_id, "station": u.station_icao,
                 "target_date": str(u.target_date), "bucket_c": u.bucket_c,
                 "side": u.side, "shares": u.size_shares, "reason": u.reason}
                for u in scan.unresolvable
            ],
            "data_api_reachable": scan.data_api_reachable,
            "gas_balance_pol": gas_balance_wei / 10**18,
        }, indent=2)

    lines = []
    total_value = sum(i.value_usd for i in scan.redeemable)
    lines.append(f"REDEEMABLE: {len(scan.redeemable)} item(s), ${total_value:.2f} total")
    for item in scan.redeemable:
        lines.append(f"  [!!] {_bucket_label(item)} -- {item.size_shares:g} shares, "
                      f"${item.value_usd:.2f}{' (WINNER)' if item.is_winner else ' (worthless)'}")

    if scan.unresolvable:
        lines.append(f"\nUNRESOLVABLE: {len(scan.unresolvable)} item(s) -- held, but this "
                      f"script cannot redeem them")
        for u in scan.unresolvable:
            lines.append(f"  [!!] {_bucket_label(u)} -- {u.size_shares:g} shares -- {u.reason}")

    if scan.already_cleared:
        lines.append(f"\nALREADY CLEARED: {len(scan.already_cleared)} item(s), no action needed")
        for c in scan.already_cleared:
            lines.append(f"  [ok] {_bucket_label(c)}")

    if not scan.data_api_reachable:
        lines.append(f"\n[!!] data-api unreachable ({scan.data_api_error}) -- this report has "
                      f"REDUCED CONFIDENCE; --execute will refuse to run against it")

    if gas_balance_wei == 0 and scan.redeemable:
        stations = ", ".join(sorted({_bucket_label(i) for i in scan.redeemable}))
        lines.append(f"\n[!!] the EOA has 0 POL for gas -- nothing can be broadcast until it "
                      f"is funded. Affected: {stations}")

    if not scan.redeemable and not scan.unresolvable:
        lines.append("\nNothing to do.")

    return "\n".join(lines)


def filter_scan(scan: RedemptionScan, token: Optional[str] = None,
                 station: Optional[str] = None) -> RedemptionScan:
    """Narrow a scan to one token or one station, across all three buckets."""
    def keep(x):
        if token is not None and x.token_id != token:
            return False
        if station is not None and x.station_icao != station:
            return False
        return True

    return RedemptionScan(
        redeemable=[i for i in scan.redeemable if keep(i)],
        already_cleared=[c for c in scan.already_cleared if keep(c)],
        unresolvable=[u for u in scan.unresolvable if keep(u)],
        data_api_reachable=scan.data_api_reachable,
        data_api_error=scan.data_api_error,
    )


# --------------------------------------------------------------------------
# --execute
# --------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    item: "redemption_client.RedeemableItem"
    outcome: str   # "redeemed" | "simulation_reverted" | "receipt_reverted" |
                   # "balance_still_nonzero" | "broadcast_rejected" | "pending"
    detail: str
    tx_hash: Optional[str] = None


def execute_one(
    item,
    eoa_address: str,
    private_key: str,
    funder: str,
    neg_risk_adapter: str,
    conditional_tokens_address: str,
    rpc_url: str,
    chain_id: int,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
    deadline: int,
) -> ExecutionResult:
    """
    Simulate, then (only on a clean simulation) sign, broadcast, and verify
    ONE redeemable item. See the module docstring's outcome list and
    section 6/7 of the design for what each one means and why.
    """
    amount_base_units = round(item.size_shares * 1_000_000)
    redeem_calldata = onchain_client.encode_redeem_positions(
        item.condition_id, [amount_base_units],
    )
    calls = [(neg_risk_adapter, 0, redeem_calldata)]

    proxy_nonce = onchain_client.get_nonce(funder, rpc_url)
    domain = onchain_client.get_eip712_domain(funder, rpc_url)
    signature = onchain_client.sign_execute_payload(
        private_key=private_key, domain=domain, signer=eoa_address,
        nonce=proxy_nonce, deadline=deadline, calls=calls,
    )
    execute_calldata = onchain_client.encode_execute(
        signer=eoa_address, nonce=proxy_nonce, deadline=deadline,
        calls=calls, signature=signature,
    )

    print(f"  {_bucket_label(item)}: {item.size_shares:g} shares, ${item.value_usd:.2f} -- "
          f"redeemPositions via {neg_risk_adapter}, execute() to {funder}")

    try:
        onchain_client.simulate_call(
            to=funder, calldata=execute_calldata, rpc_url=rpc_url, from_address=eoa_address,
        )
    except onchain_client.SimulationReverted as exc:
        print(f"    SIMULATION REVERTED: {exc.reason} -- aborting before signing anything")
        return ExecutionResult(item=item, outcome="simulation_reverted", detail=exc.reason)

    eoa_nonce = onchain_client.get_transaction_count(eoa_address, rpc_url)
    raw_tx, _local_tx_hash = onchain_client.build_and_sign_transaction(
        private_key=private_key, to=funder, calldata=execute_calldata,
        chain_id=chain_id, nonce=eoa_nonce, gas_limit=gas_limit,
        max_fee_per_gas=max_fee_per_gas, max_priority_fee_per_gas=max_priority_fee_per_gas,
    )

    try:
        tx_hash = onchain_client.broadcast_transaction(raw_tx, rpc_url)
    except onchain_client.RpcError as exc:
        print(f"    BROADCAST REJECTED: {exc}")
        return ExecutionResult(item=item, outcome="broadcast_rejected", detail=str(exc))

    print(f"    broadcast: {tx_hash} -- waiting for the receipt")
    receipt = onchain_client.wait_for_receipt(tx_hash, rpc_url)
    if receipt is None:
        print(f"    not mined within the timeout -- check {tx_hash} by hand")
        return ExecutionResult(item=item, outcome="pending",
                                detail="not mined within timeout", tx_hash=tx_hash)

    if receipt.get("status") != "0x1":
        print(f"    RECEIPT REVERTED -- position left alone, nothing collected")
        return ExecutionResult(item=item, outcome="receipt_reverted",
                                detail="transaction mined but reverted", tx_hash=tx_hash)

    remaining = onchain_client.get_erc1155_balance(
        conditional_tokens_address, funder, int(item.token_id), rpc_url,
    )
    if remaining != 0:
        detail = f"receipt succeeded but {remaining / 1_000_000:g} shares still held"
        print(f"    {detail.upper()} -- treated as a failure")
        return ExecutionResult(item=item, outcome="balance_still_nonzero",
                                detail=detail, tx_hash=tx_hash)

    print(f"    redeemed -- balance confirmed zero")
    return ExecutionResult(item=item, outcome="redeemed", detail="balance confirmed zero",
                            tx_hash=tx_hash)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                     help="report only -- no gas, no signing, no network writes (this is "
                          "the default; the flag exists so an invocation can say so explicitly)")
    ap.add_argument("--execute", action="store_true",
                     help="simulate, broadcast, and verify -- default is --list only")
    ap.add_argument("--token", help="narrow to one token id")
    ap.add_argument("--station", help="narrow to one station ICAO")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--rpc-url", default=onchain_client.DEFAULT_RPC_URL,
                     help="Polygon JSON-RPC endpoint (default: %(default)s)")
    ap.add_argument(
        "--from-unit", nargs="?", const="polyweather", metavar="UNIT",
        help="read the POLYMARKET_* credentials from a systemd unit's resolved "
             "environment (default: polyweather). Needs root. Values are never printed.",
    )
    args = ap.parse_args(argv)

    if args.list and args.execute:
        print("--list and --execute together is a contradiction -- pick one. "
              "(--list is the default; you do not need it alongside --execute.)")
        return 1

    if args.from_unit and not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        print(f"Loading credentials from systemd unit '{args.from_unit}':")
        is_root = getattr(os, "geteuid", lambda: -1)() == 0
        if not _load_env_from_unit(args.from_unit) and not is_root:
            print("  nothing loaded, and this is not running as root -- systemd will not "
                  "reveal a unit's Environment to an unprivileged caller. Re-run with sudo.")

    funder = os.environ.get("POLYMARKET_FUNDER")
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not (funder and private_key):
        print("Cannot check: POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER are not in the "
              "environment. Either export them, or re-run as root with --from-unit.")
        return 1

    print(f"funder: {_mask(funder)}")

    try:
        neg_risk_adapter, conditional_tokens = _resolve_contract_addresses()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot check: contract addresses unavailable -- {exc}")
        return 1

    scan = redemption_client.find_redeemable(
        funder=funder, conditional_tokens_address=conditional_tokens, rpc_url=args.rpc_url,
    )
    scan = filter_scan(scan, token=args.token, station=args.station)

    if args.execute and not scan.data_api_reachable:
        print(f"Refusing --execute: the data-api is unreachable "
              f"({scan.data_api_error}), so conditionId cannot be trusted for any item. "
              f"Run --list to see the reduced-confidence report instead.")
        return 1

    if not args.execute:
        try:
            from eth_account import Account
            eoa_address = Account.from_key(private_key).address
            gas_balance = onchain_client.get_pol_balance(eoa_address, args.rpc_url)
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not read gas balance: {exc})")
            gas_balance = 0
        print(render_list(scan, gas_balance_wei=gas_balance, json_output=args.json))
        return list_exit_code(scan)

    ok, reason = gates_open()
    if not ok:
        print(f"Refusing --execute: {reason}")
        return 1

    from eth_account import Account
    eoa_address = Account.from_key(private_key).address
    gas_balance = onchain_client.get_pol_balance(eoa_address, args.rpc_url)
    if gas_balance == 0 and scan.redeemable:
        stations = ", ".join(sorted({_bucket_label(i) for i in scan.redeemable}))
        print(f"Refusing --execute: the EOA ({_mask(eoa_address)}) has 0 POL for gas. "
              f"Fund it before retrying. Affected: {stations}")
        return 2

    gas_prices = onchain_client.get_gas_price_suggestion(args.rpc_url)
    print(f"Gas price (one-time reading, not escalated): "
          f"max_fee={gas_prices['max_fee_per_gas'] / 1e9:.1f} gwei, "
          f"priority={gas_prices['max_priority_fee_per_gas'] / 1e9:.1f} gwei")

    import time as _time
    deadline = int(_time.time()) + DEFAULT_DEADLINE_SECONDS

    results = []
    for item in scan.redeemable:
        result = execute_one(
            item=item, eoa_address=eoa_address, private_key=private_key,
            funder=funder, neg_risk_adapter=neg_risk_adapter,
            conditional_tokens_address=conditional_tokens, rpc_url=args.rpc_url,
            chain_id=137, gas_limit=DEFAULT_GAS_LIMIT,
            max_fee_per_gas=gas_prices["max_fee_per_gas"],
            max_priority_fee_per_gas=gas_prices["max_priority_fee_per_gas"],
            deadline=deadline,
        )
        results.append(result)

    cleared = [r for r in results if r.outcome == "redeemed"]
    remaining = [r for r in results if r.outcome != "redeemed"]
    print(f"\n{len(cleared)}/{len(results)} redeemed.")
    if scan.unresolvable:
        print(f"{len(scan.unresolvable)} item(s) unresolvable -- see the --list report.")

    return 0 if not remaining and not scan.unresolvable else 2


if __name__ == "__main__":
    sys.exit(main())
