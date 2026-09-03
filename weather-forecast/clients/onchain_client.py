"""
clients/onchain_client.py

Everything that touches the Polygon chain for redemption. No redemption
POLICY and no POSITION concepts live here -- it knows contracts, calldata
and transactions, not which positions are worth redeeming (redemption_client.py)
or how an operator invokes it (redeem.py). Contract ADDRESSES are taken as
parameters everywhere, never hardcoded or imported here: which market you
are calling is policy, and py_clob_client_v2 (the library that resolves the
real addresses) is a dependency this module deliberately does not carry, so
it stays importable and testable without that package installed at all.

FACTS ESTABLISHED BY READ-ONLY PROBE, 2026-09-01 -- see
docs/superpowers/specs/2026-09-01-redemption-design.md for the full record:

  - the funder is a CONTRACT (a deposit wallet, signature type 3 / POLY_1271)
  - redemption routes through NegRiskAdapter.redeemPositions(bytes32,uint256[]),
    NOT ConditionalTokens directly -- the obvious first guess, and wrong,
    because these markets are neg-risk
  - the proxy's entrypoint is
    execute((address,uint256,uint256,(address,uint256,bytes)[]),bytes),
    selector 0xe8c8bf64, authorized by an EIP-712 signed payload
  - the proxy also exposes nonce() and eip712Domain() (EIP-5267)

WHAT IS INFERRED, NOT CONFIRMED. The execute() tuple's exact field
semantics -- which uint256 is the nonce, which is the deadline, what the
leading address means -- are inferred from the type shape plus the
presence of nonce() as a separate getter; there is no published ABI or
source for this proxy. This module encodes the field order the design
settled on: (signer, nonce, deadline, calls[]). eth_call SIMULATION is
what proves this right or wrong, not this comment -- see simulate_call()
below, and NEVER sign or broadcast a payload that has not simulated clean.

No new dependency: eth_abi and eth_account are already transitive
dependencies of py-clob-client-v2, which the repo already requires for the
live-order path. JSON-RPC is a plain requests POST.
"""
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import eth_abi
import requests
from eth_utils import keccak

# Public Polygon RPC gateway. Configurable per call -- every function below
# takes rpc_url explicitly rather than reading a module-level default at
# call time, so a caller with their own node never has to monkeypatch a
# global to use it.
#
# NOT polygon-rpc.com. That was the obvious first choice and it no longer
# works: as of 2026-09-03 it returns 401 "API key disabled, reason: tenant
# disabled" on a bare eth_blockNumber call, with no code change on this
# side -- a public RPC gateway went from free to key-gated sometime after
# this project last checked it. Verified working, unauthenticated, at the
# same date: https://polygon.drpc.org and https://1rpc.io/matic (kept here
# as a fallback if drpc.org ever does the same). Public RPC endpoints are
# not a stable foundation -- re-verify before trusting this constant again
# after any long gap, the same way this comment had to.
DEFAULT_RPC_URL = "https://polygon.drpc.org"

POLYGON_CHAIN_ID = 137

# Error(string) -- the standard revert-reason encoding a require()/revert("...")
# produces. Not every revert carries this (a bare revert() or a custom error
# does not), which is why decoding it is a best-effort fallback to the raw
# message, never the only path.
_ERROR_STRING_SELECTOR = bytes.fromhex("08c379a0")


class SimulationReverted(Exception):
    """An eth_call simulation reverted. .reason is the decoded revert string
    when the node returned one, else the raw JSON-RPC error message."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _selector(signature: str) -> bytes:
    """The 4-byte Solidity function selector for a canonical signature string."""
    return keccak(signature.encode())[:4]


REDEEM_POSITIONS_SELECTOR = _selector("redeemPositions(bytes32,uint256[])")
EXECUTE_SELECTOR = _selector("execute((address,uint256,uint256,(address,uint256,bytes)[]),bytes)")


def _as_bytes32(value: Union[bytes, str]) -> bytes:
    if isinstance(value, str):
        value = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    if len(value) != 32:
        raise ValueError(f"condition id must be 32 bytes, got {len(value)}")
    return value


def encode_redeem_positions(condition_id: Union[bytes, str], amounts: List[int]) -> bytes:
    """
    Calldata for NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] amounts).

    condition_id accepts either raw bytes or a 0x-prefixed hex string -- API
    responses hand this back as hex, on-chain reads as bytes, and a caller
    should not have to convert before calling in.
    """
    condition_id = _as_bytes32(condition_id)
    return REDEEM_POSITIONS_SELECTOR + eth_abi.encode(
        ["bytes32", "uint256[]"], [condition_id, amounts],
    )


def encode_execute(
    signer: str,
    nonce: int,
    deadline: int,
    calls: List[Tuple[str, int, bytes]],
    signature: bytes,
) -> bytes:
    """
    Calldata for the proxy's
    execute((address,uint256,uint256,(address,uint256,bytes)[]),bytes).

    `calls` is a list of (to, value, data) -- the same shape simulate_call()
    and the transaction builder both expect, so a caller builds the call
    list once and passes it through unchanged.

    signature must be the 65-byte r||s||v form eth_account produces --
    checked here because a truncated or malformed signature would
    otherwise surface only as an opaque revert deep in the proxy.
    """
    if len(signature) != 65:
        raise ValueError(f"signature must be 65 bytes (r||s||v), got {len(signature)}")
    payload = (signer, nonce, deadline, calls)
    return EXECUTE_SELECTOR + eth_abi.encode(
        ["(address,uint256,uint256,(address,uint256,bytes)[])", "bytes"],
        [payload, signature],
    )


# --------------------------------------------------------------------------
# RPC primitives
# --------------------------------------------------------------------------

class RpcError(Exception):
    """A JSON-RPC error response, before any endpoint-specific interpretation."""

    def __init__(self, message: str, data: Optional[str] = None, code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.data = data
        self.code = code


def _rpc(method: str, params: list, rpc_url: str, timeout: int = 15) -> Any:
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        err = body["error"]
        raise RpcError(err.get("message", "unknown RPC error"), data=err.get("data"),
                        code=err.get("code"))
    return body["result"]


def _decode_revert_reason(data: Optional[str]) -> Optional[str]:
    """
    Best-effort decode of a require()/revert("...") reason from the node's
    error.data. Returns None (never raises) when there is nothing to decode
    -- a bare revert() or a custom error carries no string, and the caller
    falls back to the raw JSON-RPC message in that case.
    """
    if not data:
        return None
    try:
        raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        if raw[:4] != _ERROR_STRING_SELECTOR:
            return None
        (reason,) = eth_abi.decode(["string"], raw[4:])
        return reason
    except Exception:
        return None


def simulate_call(to: str, calldata: bytes, rpc_url: str = DEFAULT_RPC_URL,
                   from_address: Optional[str] = None) -> bytes:
    """
    eth_call against current chain state -- free, needs no gas, and is the
    mandatory step before signing anything (see the module docstring on why
    the execute() field order is inferred, not confirmed). A revert raises
    SimulationReverted with the decoded reason where the node provided one.
    """
    call_obj = {"to": to, "data": "0x" + calldata.hex()}
    if from_address:
        call_obj["from"] = from_address
    try:
        result_hex = _rpc("eth_call", [call_obj, "latest"], rpc_url)
    except RpcError as exc:
        reason = _decode_revert_reason(exc.data) or exc.message
        raise SimulationReverted(reason) from exc
    return bytes.fromhex(result_hex[2:] if result_hex.startswith("0x") else result_hex)


def get_nonce(proxy_address: str, rpc_url: str = DEFAULT_RPC_URL) -> int:
    """
    The PROXY's own EIP-712 replay-protection nonce -- NOT the EOA's
    account-level transaction nonce used to pay gas. Two different counters
    with the same name in casual conversation; conflating them signs a
    payload that simulates fine today and reverts on broadcast against a
    nonce someone else already consumed.
    """
    raw = simulate_call(to=proxy_address, calldata=_selector("nonce()"), rpc_url=rpc_url)
    return int.from_bytes(raw, "big")


def get_erc1155_balance(contract_address: str, owner: str, token_id: int,
                         rpc_url: str = DEFAULT_RPC_URL) -> int:
    """
    The real on-chain outcome-token balance -- the third of the three
    sources redemption_client.py cross-checks (data-api, database, chain).
    Reads directly rather than through py-clob-client-v2's balance API,
    which talks to Polymarket's own service and is not itself the chain.
    """
    calldata = _selector("balanceOf(address,uint256)") + eth_abi.encode(
        ["address", "uint256"], [owner, token_id],
    )
    raw = simulate_call(to=contract_address, calldata=calldata, rpc_url=rpc_url)
    return int.from_bytes(raw, "big")


def get_pol_balance(address: str, rpc_url: str = DEFAULT_RPC_URL) -> int:
    """Native POL balance in wei. eth_getBalance, not a contract call --
    this is what tells the redemption script gas is or is not available."""
    result_hex = _rpc("eth_getBalance", [address, "latest"], rpc_url)
    return int(result_hex, 16)


def get_eip712_domain(proxy_address: str, rpc_url: str = DEFAULT_RPC_URL) -> Dict[str, Any]:
    """
    EIP-5267's eip712Domain(), read directly from the proxy rather than
    assumed. The domain is what makes a signature valid for THIS contract on
    THIS chain -- hardcoding it risks signing a payload whose
    verifyingContract silently does not match, which fails as an
    unrecoverable signature-mismatch revert with no useful message.
    """
    raw = simulate_call(to=proxy_address, calldata=_selector("eip712Domain()"), rpc_url=rpc_url)
    _fields, name, version, chain_id, verifying_contract, _salt, _extensions = eth_abi.decode(
        ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"], raw,
    )
    return {
        "name": name,
        "version": version,
        "chainId": chain_id,
        "verifyingContract": verifying_contract,
    }


# --------------------------------------------------------------------------
# EIP-712 signing
# --------------------------------------------------------------------------

# UNVERIFIED. Unlike the execute() SHAPE above (confirmed via a selector
# recovered from real bytecode) or the field ORDER (inferred from the type
# shape plus nonce() existing as a getter -- see the module docstring),
# there is no published ABI or source for this proxy, so the struct and
# field NAMES below are a best-effort guess. A WRONG name here does not
# fail quietly: it changes the EIP-712 type hash, which makes the signature
# invalid, which the contract rejects -- but it is still caught by the same
# safety net as a wrong field order, because eth_call simulation reverts on
# an invalid signature before anything is ever broadcast. Never skip
# simulation on the theory that "the signing code looks right."
#
# If a real signature attempt reverts with something that reads like a
# signature-verification failure rather than a revert from inside
# redeemPositions itself, THIS is the first place to look -- not the field
# order in encode_execute().
EXECUTE_PRIMARY_TYPE = "Execute"

EXECUTE_TYPED_DATA_TYPES = {
    "Call": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
    ],
    "Execute": [
        {"name": "signer", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "calls", "type": "Call[]"},
    ],
}


def sign_execute_payload(
    private_key: str,
    domain: Dict[str, Any],
    signer: str,
    nonce: int,
    deadline: int,
    calls: List[Tuple[str, int, bytes]],
) -> bytes:
    """
    EIP-712-sign the execute() payload with eth_account, returning the
    65-byte r||s||v signature encode_execute() expects.

    `domain` is get_eip712_domain()'s return value, passed straight
    through -- read from the chain, never hardcoded, because the domain is
    what binds a signature to THIS contract on THIS chain (see
    get_eip712_domain's docstring).

    See the UNVERIFIED note above EXECUTE_TYPED_DATA_TYPES before treating
    a signature-verification revert as a code bug rather than a wrong guess
    at the struct name.
    """
    from eth_account import Account

    message = {
        "signer": signer,
        "nonce": nonce,
        "deadline": deadline,
        "calls": [{"to": to, "value": value, "data": data} for to, value, data in calls],
    }
    signed = Account.sign_typed_data(
        private_key, domain_data=domain, message_types=EXECUTE_TYPED_DATA_TYPES,
        message_data=message,
    )
    return bytes(signed.signature)


# --------------------------------------------------------------------------
# The EOA's transaction: build, sign, broadcast, wait for the receipt.
#
# THE TWO NONCES, NAMED SO THEY CANNOT BE CONFUSED IN A CALL SITE:
#   get_nonce()              the PROXY's EIP-712 replay-protection nonce
#   get_transaction_count()  the EOA's ACCOUNT-level nonce, for gas payment
# A redemption transaction needs both, for two different fields, and they
# do not move together.
# --------------------------------------------------------------------------

def get_transaction_count(address: str, rpc_url: str = DEFAULT_RPC_URL) -> int:
    """
    The EOA's account-level transaction nonce -- what pays for gas and
    orders transactions on the account, NOT the proxy's replay-protection
    nonce (get_nonce()). "pending" so a transaction already in the mempool
    is accounted for; broadcasting at a nonce already in flight is rejected
    by the node rather than silently queued.
    """
    result_hex = _rpc("eth_getTransactionCount", [address, "pending"], rpc_url)
    return int(result_hex, 16)


def estimate_gas(to: str, calldata: bytes, from_address: str,
                  rpc_url: str = DEFAULT_RPC_URL) -> int:
    """eth_estimateGas -- free, no gas needed itself, informational only.
    Deliberately not used to auto-set gas_limit anywhere in this module:
    the design's safety model puts gas decisions in the operator's hands,
    not on an automatic estimate that could be wrong under load."""
    call_obj = {"to": to, "data": "0x" + calldata.hex(), "from": from_address}
    result_hex = _rpc("eth_estimateGas", [call_obj], rpc_url)
    return int(result_hex, 16)


def build_and_sign_transaction(
    private_key: str,
    to: str,
    calldata: bytes,
    chain_id: int,
    nonce: int,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
    value: int = 0,
) -> Tuple[bytes, str]:
    """
    Build and sign an EIP-1559 transaction. Returns (raw_tx_bytes, tx_hash)
    -- no network call, purely local signing, so this can be inspected
    before broadcast_transaction() ever sends anything.
    """
    from eth_account import Account

    tx = {
        "type": 2,
        "chainId": chain_id,
        "nonce": nonce,
        "to": to,
        "value": value,
        "data": calldata,
        "gas": gas_limit,
        "maxFeePerGas": max_fee_per_gas,
        "maxPriorityFeePerGas": max_priority_fee_per_gas,
    }
    signed = Account.from_key(private_key).sign_transaction(tx)
    return bytes(signed.raw_transaction), "0x" + signed.hash.hex()


def broadcast_transaction(raw_tx: bytes, rpc_url: str = DEFAULT_RPC_URL) -> str:
    """
    eth_sendRawTransaction. THE ONE CALL IN THIS MODULE THAT MOVES REAL
    ASSETS WHEN INVOKED AGAINST A REAL ENDPOINT -- see the module docstring.
    Returns the transaction hash; raises RpcError if the node rejects it
    outright (e.g. a stale nonce or insufficient gas), which happens before
    the transaction ever enters the mempool and costs nothing.
    """
    result = _rpc("eth_sendRawTransaction", ["0x" + raw_tx.hex()], rpc_url)
    return result


def wait_for_receipt(tx_hash: str, rpc_url: str = DEFAULT_RPC_URL,
                      timeout_s: float = 120, poll_interval_s: float = 3) -> Optional[dict]:
    """
    Poll eth_getTransactionReceipt until mined or timeout_s elapses.
    Returns None on timeout -- NOT an exception, because "not yet mined" is
    not a failure. Deliberately no retry loop, no gas-price escalation, no
    automatic re-broadcast on timeout: per the design's safety model, a
    stuck transaction is the operator's call, made by a human watching the
    script run, not something this function decides on its own.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_url)
        if receipt is not None:
            return receipt
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval_s)


def get_gas_price_suggestion(rpc_url: str = DEFAULT_RPC_URL) -> Dict[str, int]:
    """
    ONE reading of current network gas conditions, in wei. Deliberately not
    a loop and not an escalation policy -- per the design's safety model,
    gas decisions belong to the operator watching the script run, not to
    automatic retry logic this module would own. A caller is free to
    override either value; this is a starting point, not an authority.

    max_fee_per_gas = 2 * current base fee + priority fee -- a conventional
    margin that tolerates the base fee moving for a couple of blocks
    without needing a resubmission, not a claim that it always suffices.
    """
    priority_fee = int(_rpc("eth_maxPriorityFeePerGas", [], rpc_url), 16)
    block = _rpc("eth_getBlockByNumber", ["latest", False], rpc_url)
    base_fee = int(block["baseFeePerGas"], 16)
    return {
        "max_fee_per_gas": 2 * base_fee + priority_fee,
        "max_priority_fee_per_gas": priority_fee,
    }
