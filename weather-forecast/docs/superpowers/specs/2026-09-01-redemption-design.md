# Redemption: collecting resolved outcome tokens

**Date:** 2026-09-01
**Status:** design approved, not yet implemented
**Scope decision:** operator-run script. The trading daemon never redeems.

---

## 1. The problem

Nothing in this repo redeems. When a live position resolves,
`position_manager` writes `closed_resolution` to the database and
`executor.close_position()` prints "REDEEM IT ON THE EXCHANGE" for a human.
The outcome tokens stay in the funding wallet forever, and two things follow:

**Money.** A resolved winner is real dollars sitting uncollected. Two have
been collected so far, both by hand.

**A recurring live halt.** Leftover tokens read as unrecorded exposure and
`_live_budget_breach()` fails closed, blocking every live entry. This has
halted the book twice: unredeemed resolution losers on 2026-08-22, and a
short exit fill on 2026-08-30. Both were fixed by teaching reconciliation to
ignore the leftovers — a whitelist and then a value bound. Redemption removes
the leftovers instead, which is the only fix that shrinks the problem rather
than tolerating it.

**What is redeemable right now:** three positions, all losers at `curPrice: 0`
(Taipei 33°C, Taipei 34°C, Singapore 31°C, all 2026-08-28). Redeeming them
collects **$0**. The value of this work is future winners and permanent
removal of the halt mechanism, not today's money. Stating that plainly here
so nobody later reads a $0 result as a failure.

---

## 2. What was verified on-chain, and how

Every fact below came from a read-only probe on 2026-09-01, not from
assumption. They are recorded because re-deriving them is expensive and
guessing any one of them wrong writes a transaction that fails or, worse,
succeeds incorrectly.

| Fact | Value | How established |
|---|---|---|
| Tokens are held by | the funder `0xC669…2A00`, **a contract** (146 bytes) | `eth_getCode` |
| Signing key is | EOA `0x04e6…E516`, no code | `eth_getCode` |
| The EOA's authority over it | `owner()` → the EOA | `eth_call` |
| Proxy resolves to | impl `0xf7f2…3294` (20858 bytes) via registry `0x7a18…fc3a` at storage slot `0xa3f0ad74…33d50` | `eth_getStorageAt` + `eth_call` |
| Execute entrypoint | `execute((address,uint256,uint256,(address,uint256,bytes)[]),bytes)` — selector `0xe8c8bf64` | selector extraction from bytecode + openchain.xyz lookup |
| Authorization model | EIP-712 signed payload; the wallet also exposes `nonce()` and `eip712Domain()` | same |
| Market type | **neg-risk** — every weather market returns `negativeRisk: true` | Polymarket data-api |
| Redemption target | `NegRiskAdapter` `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | `py_clob_client_v2.config.get_contract_config(137)` |
| Gas available | **0.000000 POL in both the EOA and the proxy** | `eth_getBalance` |
| Redemption in py-clob-client-v2 | none — zero matches for "redeem" | grep of the installed package |

**The signature type is 3 (POLY_1271, deposit wallet).** This is why the EOA
cannot simply call `redeemPositions` itself: it holds no tokens. The tokens
belong to the proxy, and the proxy acts only on a signed payload from its
owner.

**Neg-risk is not a detail.** Calling `ConditionalTokens.redeemPositions`
directly — the obvious first guess, and what a generic tutorial shows — is
the wrong contract for these markets. Everything must route through
`NegRiskAdapter`.

### Prerequisite the operator must satisfy

**The EOA needs POL for gas.** A redemption is an on-chain transaction and
there is currently no gas anywhere. Everything up to and including
simulation works without it; only the broadcast does not. The script must
detect this and say so in one clear line rather than failing obscurely.

---

## 3. Requirement: flag the station when manual redemption is needed

Operator-requested, and it fixes a real gap.

**The gap.** `storage.load_settled_live_tokens()` returns
`Dict[token_id, (size_shares, exit_price)]`. The station is in the database —
`station_icao`, `target_date`, `bucket_c`, `side` are all on the row — and the
query throws it away. So every downstream message can only say
`5051499713...`, a token-id prefix that identifies nothing to a human:

```
[!!] 5051499713... won at 1.00 and is still held: 9.181817 shares,
     $9.18 uncollected. REDEEM IT ON THE EXCHANGE
```

The operator cannot tell which station, which day, or which bucket that is
without a manual database query. `executor.close_position()` already names
the station correctly at the moment of closing, but that line scrolls past
once; the *standing* reports — the ones read when deciding what to do — do not.

**The requirement.** Every place the system says redemption is needed must
name the position in human terms: `WSSS 2026-08-20 32°NO`, not a token
prefix. The token id stays in the line, because it is what the operator pastes
into a block explorer, but it is no longer the only identifier.

**Where this surfaces (all four must be covered):**

1. `wallet_client.preflight()` — the uncollected-winner line above.
2. `Reconciliation.describe_settled()` — the "settled but unredeemed" summary.
3. `Reconciliation.describe_dust()` — dust that can never be sold or redeemed.
4. `redeem.py --list` — the new script's own report.

**What "manual redemption is needed" means, precisely.** The flag is raised
whenever the system holds a resolved token it cannot clear itself:

- gas is unavailable, so nothing can be broadcast;
- the simulation step fails, so the mechanism is not working;
- a winner is held and no redemption has been run.

Dust is reported but is **not** flagged as needing manual action: it is
unsellable (far below the 5-share exchange minimum) and worth fractions of a
cent, so a "do something" flag against it would be noise the operator learns
to ignore. It appears in the listing, described as what it is.

**Design constraint on the fix.** `load_settled_live_tokens()` is consumed by
`executor.py:181`, `scheduler.py:720` and three test files, and its shape is
asserted directly in `test_settled_token_wiring.py`. Widening it to carry
position identity is the right change — the data is already on the row — but
it must not change the meaning of the map or the `closed_resolution`-only
filter, which is load-bearing against the 2026-08-22 halt. The value becomes a
small record carrying station, target date, bucket, side, shares and exit
price; the key stays the token id.

---

## 4. Components

Three units, each independently testable.

### `clients/onchain_client.py` (new)

Everything that touches the chain. No redemption policy, no position
concepts — it knows contracts, calldata and transactions.

- Resolve the proxy's `nonce()` and EIP-712 domain.
- Encode `NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] amounts)`.
- Wrap calls in the proxy's `execute(payload, signature)` envelope.
- Sign the payload (EIP-712) and the transaction, both with `eth_account`.
- `eth_call` simulation, broadcast, receipt polling.
- Read ERC-1155 balances directly, for the post-redemption check.

**No new dependency.** `eth_abi` 5.2.0 and `eth_account` 0.13.7 are already
installed as transitive dependencies of `py-clob-client-v2`, and JSON-RPC is
a `requests` POST. This matters: the repo's declared dependencies are
`requests`, `beautifulsoup4` and `py-clob-client-v2`, and adding `web3.py` to
redeem three worthless tokens would be out of proportion. The RPC endpoint is
configurable, defaulting to a public Polygon node.

### `clients/redemption_client.py` (new)

Discovery and eligibility — what *can* be redeemed, from three sources that
must agree.

- Polymarket data-api: `positions?user=<funder>` yields `conditionId`,
  `redeemable`, `negativeRisk`, `size`, `curPrice`, `title`.
- The database: which of those are ours, and which station each belongs to.
- The chain: the actual ERC-1155 balance.

Returns a list of redeemable items carrying both the on-chain facts and the
station identity from requirement 3.

### `redeem.py` (new, top level)

The operator-facing script, in the same shape as `check_holdings.py`: a module
docstring that explains what it is for and what it found, `--from-unit` for
credentials on the box, read-only by default.

- `--list` (default): the report. No gas, no signing, no network writes.
- `--execute`: simulate, then broadcast, then verify.
- `--token <id>` / `--station <ICAO>`: narrow to one position.
- `--json`: raw output for when the table is not the authority.

---

## 5. Data flow

```
data-api positions ─┐
storage (DB rows) ──┼─→ redemption_client.find_redeemable()
on-chain balances ──┘        │
                             │  [three sources cross-checked]
                             ▼
                    RedeemableItem(station, target_date, bucket, side,
                                   token_id, condition_id, shares,
                                   is_winner, value_usd)
                             │
              ┌──────────────┴───────────────┐
              ▼                              ▼
        --list: report              --execute: onchain_client
        (+ station flags)             encode → simulate → sign
                                      → broadcast → receipt
                                             │
                                             ▼
                                    re-read balance, confirm zero
```

---

## 6. Safety model

Redemption moves real assets irreversibly, so it inherits the two-gate
pattern `wallet_client` already uses for orders, and adds simulation.

1. **Dry run is the default.** Running `redeem.py` with no arguments reports
   and exits. Broadcasting requires `--execute` explicitly.
2. **Two gates, as with live orders.** `--execute` is the first;
   `POLYMARKET_LIVE_TRADING=true` is the second. Neither alone broadcasts.
3. **Simulate before signing.** Every call is `eth_call`-simulated against the
   current chain state. A revert aborts before anything is signed. This is
   free and needs no gas, so there is no reason to skip it.
4. **Three sources must agree.** If the data-api says a position is redeemable
   but the chain shows a zero balance, or the database has no record of it,
   the script refuses that item and says why. Fail closed, mirroring
   reconciliation.
5. **Verify after.** Re-read the balance after the receipt. A transaction that
   succeeded but did not clear the balance is reported as a failure, because
   for this purpose it is one.
6. **Never sign anything the operator has not seen.** `--execute` prints the
   exact calls and the total value first.

**Deliberately absent:** no retry loop, no gas-price escalation, no automatic
re-broadcast. A stuck transaction is the operator's call. This script is run
by a human who is watching it.

---

## 7. Error handling

| Situation | Behaviour |
|---|---|
| No POL for gas | Report every redeemable item, then one clear line naming the EOA and what it needs. Exit non-zero. **Stations flagged.** |
| data-api unreachable | Fall back to the database plus on-chain balances; report reduced confidence; refuse `--execute`. |
| Simulation reverts | Abort before signing. Print the revert reason and the decoded call. **Station flagged.** |
| Sources disagree | Refuse that item, list all three readings, continue with the others. |
| Receipt reverts | Report the transaction hash and leave the position alone. |
| Balance still non-zero after success | Treat as failure and say so explicitly. |

Exit codes: `0` nothing to do or all redeemed; `1` could not check;
`2` redeemable items exist that this run could not clear (the manual-redemption
flag, in exit-code form, so a cron or a wrapper can detect it).

---

## 8. Testing

No test touches the network or the chain. The RPC layer is faked at the
transport boundary.

- **Calldata encoding pinned byte-for-byte** against known-good ABI encoding
  for both `redeemPositions` and the `execute` envelope. A silent change in
  either is a wrong transaction, so this is the load-bearing test.
- **EIP-712 payload pinned** against a fixed vector, including the domain.
- **Neg-risk routing:** a `negativeRisk: true` market must target the
  `NegRiskAdapter`; a false one must not. This is the mistake most likely to
  be made later.
- **Station flagging:** every one of the four call sites in §3 names the
  station. Asserted per site, not once.
- **Three-source disagreement** in each direction produces a refusal.
- **Gas absent** produces exit code 2 with the stations named.
- **The `closed_resolution`-only filter survives** the widening of
  `load_settled_live_tokens()` — the existing guarantee from the 2026-08-22
  fix, re-asserted against the new return shape.

---

## 9. Out of scope

- The scheduler and executor do not call this. No automatic redemption.
- No changes to the trading, entry, or exit paths.
- No sweeping or consolidation of dust; it is reported, not acted on.
- No allowance management — already the operator's job, deliberately.
- No relayer integration. The wallet's `execute` + EIP-712 shape suggests a
  relayer could pay gas instead of the EOA, which would remove the POL
  prerequisite entirely, but the endpoint is undocumented and reverse-engineering
  a private API into a money path is not justified for the amounts here.
  Recorded as a future option, not built.

---

## 10. Open risks

- **The `execute` ABI came from a selector lookup, not a published ABI.** The
  signature is unambiguous and the field order is conventional, but the first
  real simulation is what proves it. Simulation is free and mandatory before
  any broadcast, so the risk is caught before it costs anything — which is
  precisely why step 3 of the safety model is not optional.
- **Field semantics inside the payload** (which `uint256` is the nonce and
  which the deadline) are inferred from the type shape plus the presence of
  `nonce()`. A failed simulation is the expected way to find this wrong, and
  it costs nothing.
- **The data-api is not a contract.** It is a convenience for discovery. Every
  value that determines a transaction — balance, condition id — is
  independently confirmed on-chain before use.
