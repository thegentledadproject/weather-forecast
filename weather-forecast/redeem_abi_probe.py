#!/usr/bin/env python3
"""
redeem_abi_probe.py -- READ-ONLY spike (Wave 3, 3f).

QUESTION. redeem.py builds NegRiskAdapter.redeemPositions(bytes32,uint256[])
with a ONE-element amounts array ([amount]). The 2026-09-15 trade-logic review
read the adapter's source as expecting TWO elements -- [yesAmount, noAmount],
one per outcome -- forwarded as the `amounts` of an ERC1155
safeBatchTransferFrom over the two position ids, which reverts on a length
mismatch. If that is right, redemption has never been able to work. This
script settles it against the DEPLOYED contract, and does nothing else.

WHAT IT DOES (no transaction, no private key, no wallet, no signing code):
  1. prints the selector it is looking for, computed here from the canonical
     signature (0x + keccak256(sig)[:4]) so the reader can compare it with
     clients/onchain_client.REDEEM_POSITIONS_SELECTOR by eye;
  2. with an Etherscan/Polygonscan API key (--api-key or $POLYGONSCAN_API_KEY /
     $ETHERSCAN_API_KEY): fetches the VERIFIED SOURCE of the adapter and
     prints every line of redeemPositions() that touches the amounts
     parameter -- the answer is read off the source;
  3. without a key: fetches the deployed BYTECODE over plain JSON-RPC
     (eth_getCode, no key needed) and lists which of the relevant 4-byte
     selectors appear in it as PUSH4 immediates. The adapter answering to
     redeemPositions(bytes32,uint256[]) AND its bytecode carrying the
     safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)
     selector is consistent with the two-element reading; it is evidence,
     not proof -- say so when appending to memory;
  4. offline (--offline, or no network): prints step 1's numbers and the
     exact commands to run by hand. From the dev box the RPC returns 403;
     run it on the EC2 box, where redeem.py already reaches that endpoint.

WHAT IT NEVER DOES: import clients.onchain_client, clients.wallet_client,
redeem, eth_account or py_clob_client_v2. tests/test_wave3_redeem_abi_probe.py
pins the import allowlist by AST. Nothing here can spend anything.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from eth_utils import keccak

# The deployed NegRiskAdapter on Polygon (chain 137), as recorded in
# docs/superpowers/specs/2026-09-01-redemption-design.md and cross-checked
# against py_clob_client_v2.config.get_contract_config(137).neg_risk_adapter
# on 2026-09-03. Overridable with --adapter; never resolved through the
# client library here, because that library pulls in the account stack.
DEFAULT_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
DEFAULT_RPC_URL = "https://polygon.drpc.org"
POLYGON_CHAIN_ID = 137
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"

REDEEM_SIG = "redeemPositions(bytes32,uint256[])"
# What the two-element reading predicts the adapter calls internally, the
# one-element alternative a reader might expect, and two neighbours.
RELATED_SIGS = (
    "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)",
    "safeTransferFrom(address,address,uint256,uint256,bytes)",
    "getPositionId(bytes32,bool)",
    "redeemPositions(address,bytes32,bytes32,uint256[])",  # ConditionalTokens' own
)


def selector(signature: str) -> str:
    """'0x' + the first four bytes of keccak256(signature), lower-case hex."""
    return "0x" + keccak(signature.encode()).hex()[:8]


def push4_immediates(bytecode_hex: str) -> set:
    """
    Every 4-byte immediate of a PUSH4 (0x63) opcode in EVM bytecode, as
    '0x' + 8 hex chars. Solidity's function dispatcher compares the calldata
    selector against PUSH4 immediates, so the set of selectors a contract
    answers to appears here. Walks the bytecode opcode by opcode (PUSH1..
    PUSH32 carry 1..32 immediate bytes, everything else carries none) so a
    selector-looking byte pattern INSIDE another PUSH's data is not
    mistaken for a dispatcher entry.
    """
    raw = bytes.fromhex(bytecode_hex[2:] if bytecode_hex.startswith("0x") else bytecode_hex)
    found = set()
    i = 0
    while i < len(raw):
        op = raw[i]
        if 0x60 <= op <= 0x7F:              # PUSH1 (0x60) .. PUSH32 (0x7F)
            width = op - 0x5F
            if op == 0x63 and i + 4 < len(raw):
                found.add("0x" + raw[i + 1:i + 5].hex())
            i += 1 + width
        else:
            i += 1
    return found


def _rpc(method: str, params: list, rpc_url: str, timeout: int = 20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if "error" in payload:
        raise RuntimeError(f"{method}: {payload['error']}")
    return payload["result"]


def fetch_bytecode(adapter: str, rpc_url: str) -> str:
    return _rpc("eth_getCode", [adapter, "latest"], rpc_url)


def fetch_verified_source(adapter: str, api_key: str, timeout: int = 20) -> str:
    """The verified source text (all files concatenated) from Etherscan V2 for chain 137."""
    url = (f"{ETHERSCAN_V2}?chainid={POLYGON_CHAIN_ID}&module=contract&action=getsourcecode"
           f"&address={adapter}&apikey={api_key}")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("status") != "1" or not payload.get("result"):
        raise RuntimeError(f"explorer said: {payload.get('message')} {payload.get('result')}")
    src = payload["result"][0].get("SourceCode", "") or ""
    # Multi-file verifications arrive as a JSON blob wrapped in one extra
    # pair of braces; flatten to text so line reading works either way.
    if src.startswith("{{"):
        try:
            files = json.loads(src[1:-1]).get("sources", {})
            src = "\n".join(f"// ---- {name}\n{f.get('content', '')}" for name, f in files.items())
        except ValueError:
            pass
    return src


def redeem_positions_lines(source: str) -> list:
    """The redeemPositions() body's lines that mention its amounts argument
    (plus the signature line), so the answer can be read off the source."""
    out = []
    inside = False
    depth = 0
    opened = False
    for line in source.splitlines():
        if not inside:
            if "function redeemPositions" not in line:
                continue
            inside, depth, opened = True, 0, False
        if "function redeemPositions" in line or "amount" in line.lower():
            out.append(line.rstrip())
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            inside = False
    return out


def report_selectors() -> str:
    lines = [f"selector wanted  : {selector(REDEEM_SIG)}  <- {REDEEM_SIG}", "related selectors:"]
    for sig in RELATED_SIGS:
        lines.append(f"  {selector(sig)}  <- {sig}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", default=DEFAULT_ADAPTER)
    ap.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    ap.add_argument("--api-key", default=os.environ.get("POLYGONSCAN_API_KEY") or os.environ.get("ETHERSCAN_API_KEY"))
    ap.add_argument("--offline", action="store_true", help="print the selectors and the manual steps only")
    args = ap.parse_args(argv)

    print(report_selectors())
    want = selector(REDEEM_SIG)

    if args.offline:
        print("\nOFFLINE. Run by hand on the box:\n"
              "  python redeem_abi_probe.py --api-key <etherscan/polygonscan key>   # reads the verified source\n"
              "  python redeem_abi_probe.py                                        # bytecode selectors only\n"
              f"and look for {want} and, in the source, whether redeemPositions indexes\n"
              "_amounts[0] and _amounts[1] (two elements) or forwards a single amount.")
        return 0

    verdict = []
    if args.api_key:
        try:
            lines = redeem_positions_lines(fetch_verified_source(args.adapter, args.api_key))
            print("\nVERIFIED SOURCE, redeemPositions lines mentioning the amounts argument:")
            for line in lines:
                print("  " + line)
            two = any("[1]" in line for line in lines)
            verdict.append("source: TWO-element [yesAmount, noAmount]" if two
                           else "source: no [1] index seen -- READ THE LINES ABOVE BY HAND")
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            print(f"\nverified source unavailable: {exc}")

    try:
        code = fetch_bytecode(args.adapter, args.rpc_url)
        sels = push4_immediates(code)
        print(f"\nDEPLOYED BYTECODE: {len(code) // 2 - 1} bytes, {len(sels)} PUSH4 immediates")
        print(f"  {want} {REDEEM_SIG:<44}: {'PRESENT' if want in sels else 'ABSENT'}")
        for sig in RELATED_SIGS:
            s = selector(sig)
            print(f"  {s} {sig[:44]:<44}: {'present' if s in sels else 'absent'}")
        batch = selector(RELATED_SIGS[0])
        if want in sels and batch in sels:
            verdict.append("bytecode: adapter answers redeemPositions(bytes32,uint256[]) and carries the "
                           "safeBatchTransferFrom selector -- CONSISTENT with a two-element amounts array")
        elif want in sels:
            verdict.append("bytecode: redeemPositions present, safeBatchTransferFrom absent -- inconclusive")
        else:
            verdict.append("bytecode: redeemPositions(bytes32,uint256[]) NOT in the dispatcher -- wrong address?")
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"\nbytecode unavailable ({exc}) -- re-run on a box with network, or with --offline")

    print("\nVERDICT (append one paragraph to memory; the redeem.py fix is a checkpoint decision):")
    for v in verdict or ["nothing fetched"]:
        print("  - " + v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
