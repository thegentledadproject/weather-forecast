# Gap audit: CODEX_IMPLEMENTATION_PLAN.md vs the live repo

**Date:** 2026-09-24 · **Code revision:** `905bacc` (the working tree has uncommitted `cohort_monitor.py` edits from another session; they are not audited) · **Mode:** read-only, nothing changed

**Method:** five independent read-only reviews, one per master gate. A PASS needed a file:line or test citation. Only the backtest determinism and no-lookahead tests were run (9/9 pass), plus the P1 storage and bucket-axis test set (181 pass).

## Headline

**No master gate passes. The system is an evidence-first monolith, not the spec-first system the plan describes.** Most gaps are differences of shape. Eight of them can put money or decisions at risk; they are listed below the scorecard. The plan's central test, P10A (does a bigger predicted edge lead to more realized edge, out of sample?), cannot be run today: the replay tests a different model, and no test set has been kept aside.

## Scorecard

| Phase | Verdict | One-line reason |
|---|---|---|
| P0 Spec freeze | **FAIL** | No schemas, ADRs, error taxonomy or state-machine definitions. The rules live in `config.py` comments |
| P1 Domain + DB | PARTIAL (weak) | Has one migration path and read-only storage. Money is stored as floats, `INSERT OR REPLACE` overwrites history, and there are no foreign keys, content hashes or audit table |
| P2 Contract integrity | PARTIAL | Bucket parsing fails closed and the rounding tests pass. The market's rules text is never read, there is no VALID/UNCERTAIN/INVALID status, and settlement grades itself |
| P3 Point-in-time data | **FAIL** | The known forecast leak is fixed (`storage.py:822`). There is no issued/published/received split, observations are overwritten, and there is no `as_of` interface |
| P4 Feature engine | NOT BUILT | Features are recomputed inline, and the inputs behind a decision are not saved |
| P5 Probability + calibration | PARTIAL → FAIL | No simple baseline has been scored, there is no lock/test split and no P_robust, and the map is fitted only on trades already made |
| P6 Order book | PARTIAL | A buy-side VWAP golden test exists. There are no stale/crossed-book checks, ask and depth come from two separate calls, and books are not saved |
| P7 Edge + execution | PARTIAL → FAIL | There is no edge curve by size and no markouts. Paper and replay fills are booked at the top ask for the full size |
| P8 Portfolio + risk | PARTIAL | Per-trade, station and region caps count money spent per day. There is no cap on money held, no daily-loss limit and no correlation limit, and the bankroll is a fixed number |
| P9 Position + exit | PARTIAL → FAIL | There is no state machine, and a second close silently rewrites the exit price. Entry and exit value positions differently. Closed and settled are the same state |
| P10 Backtester | PARTIAL | Deterministic and free of lookahead, but it replays a different model and one station at a time |
| P10A Edge ledger | NOT BUILT | Nothing traces predicted to realized edge by bucket with confidence intervals |
| P10B Out-of-sample | NOT BUILT | Every sweep and replay scores the whole window |
| P11 Paper + shadow | PARTIAL | Paper pipeline and shadow twin exist. There are no challengers, drills or paper-vs-backtest attribution, and no evidence period was set in advance |
| P12 Controlled production | **FAIL** | A permanent maturity override, no scaling ladder, no automatic de-risk, and redemption unproven |
| Track B Audit | PARTIAL | `entry_decisions` exists, but has no version or snapshot IDs, and recording failures are swallowed |
| Track C Monitoring / kill switch | NOT BUILT | No health state machine, alerts or automatic kill |
| Track D Lineage | PARTIAL | Stores the git SHA only, which ignores uncommitted changes. Backtest datasets are not pinned by hash |

**Gates:** G1 FAIL · G2 FAIL · G3 FAIL · G4 FAIL · G5 FAIL.

## The gaps that actually risk money or wrong decisions (ranked)

1. **Re-arming live has no gate.** Restoring `mode.env` and restarting lets WSSS and RCSS trade through `MATURITY_OVERRIDE` (`config.py:4667-4672`), even though they fail beats_market and redemption is unproven. *Fix:* make the override expire, and require the dossier to pass plus one proven redemption before `live` is allowed.
2. **Live settlement and cash are not reconciled.** Redemption probably reverts: `redeem.py:268-270` sends a one-element amounts array. Resolution is booked from our own station reading with an order-book rounding fallback (`position_manager.py:972-989`), never checked against the oracle's payout. `storage.close_position` (`storage.py:1365`) does not check `status='open'`. *Fix:* cross-check the winner against the exchange's terminal prices, guard the close, and prove one redemption.
3. **The replay tests a different strategy from production.** It has no bias correction, no measured spread (`backtest/engine.py:996`), no bias-quality gate (`:1158-1165`), a raw calibrated gate (`entry_sim.py:294-299`) and no cross-station caps. *Consequence for 10-04:* judge the Wave 2 flags on live and paper rows only, never on a replay.
4. **Calibration is fitted only on trades already made** (`probability_calibration.py:340` → `cohort_monitor.py:714`). The map moves whenever the gate changes and stops learning when approvals fall to zero, which is where NO is now. *Fix:* fit on all settled candidates in `entry_decisions`/`ev_snapshots`, and use a lower confidence bound as P_robust.
5. **No baseline and no untouched test set.** Skill has never been scored against climatology, and K=40, the NO map and the regional map were all chosen on the same window. *Fix:* freeze a test date range now and score climatology beside the model and the market.
6. **Nothing trips on its own and nobody is told.** `cohort_monitor.kill_criterion` is report-only (`cohort_monitor.py:606-621`), and there is no alert channel, daily-loss limit or held-exposure cap. *Fix:* a small watchdog that flips `mode.env` to paper and sends one alert.
7. **Market rules text is never read.** If Polymarket re-points a city to another station or source, nothing notices (`market_discovery.py:369-424`). *Fix:* hash the event's `description`/`resolutionSource` and refuse to trade on a mismatch.
8. **History can be overwritten.** `INSERT OR REPLACE` on observations (`storage.py:703`), settlements (`:1062`) and ensemble spread (`:637-647`), and the inputs behind a decision are not stored (`:529-552`). A bad trade cannot be rebuilt. *Fix:* append-only revision rows, and store the estimate, spread, bias and forecast `fetched_at` list on `entry_decisions`.

## Deliberate differences that are fine

- FOK-only live orders mean there are never resting orders to recover or hold capital for.
- The forecast table is effectively append-only (`fetched_at` is part of the key).
- The weather model imports no market code (`calibration.py`, `probability.py`). It is independent by structure but not pinned by a test.
- Cross-bucket stakes are summed gross, which is conservative rather than wrong.
- Float money is low urgency at a $8 live cap.

## Section 12 acceptance tests

Has a test: **2 of 16** (#4 deterministic replay, #7 executable-price fidelity). Partial: 10. None: 4 (#9 state-machine integrity, #11 idempotent events, #14 model independence, #16 kill-switch drill).

## Decision

All five gates: **BLOCKED / FAIL.** Per plan §2, live capital should stay off until at least gaps 1, 2 and 6 are closed.
