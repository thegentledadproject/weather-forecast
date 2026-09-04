# polyweather — remediation plan

Source: session analysis of the entry path, the exit path, and the EV/probability/Brier stack against `thegentledadproject/weather-forecast@main` (fetched 2026-09-03).

30 findings, grouped into 4 phases. Phase order is a dependency order, not a priority order — Phase 0 produces the measurements that decide whether several Phase 3 items are worth doing at all.

**Revision 3.** One new item (P3-6, calibrated-probability sizing — the highest-value item in Phase 3), one new prerequisite (P2-2 prereq C, sizing headroom), a kill criterion folded into P0-5, a retained-control statement folded into P2-2, and a new "not rejected — untested or deferred" section so a partial take and price-gated entry are not read as decided. Phase 3 also gained a reframing preamble: the model is a direction indicator, not a probability estimator, and that governs the phase's priorities.

**Revision 2.** Five items added (P0-5, P1-10, P1-11, and two prerequisites folded into P2-2) after a closer read of the stop/take measurement block at `config.py:1502-1566` and the sizing block at `config.py:2100-2210`. Two existing items were corrected: P0-1's premise was wrong, and P3-1's direction was unstated. Both corrections are marked in place rather than silently rewritten — the original reasoning is worth seeing next to what replaced it.

**Renamed 2026-09-03: the cohort monitor was `P0-0`, and is now `P0-5`.** The old
identifier collided with a different item that is already deployed — `P0-0 · Retain
dated EV snapshots`, defined in the repo's own committed spec
(`docs/superpowers/specs/2026-09-03-remediation-plan-revised.md` §3) and accumulating
rows in the `ev_snapshots` table since 13:40:58 UTC that day. Two items, one
identifier, one of them live. The rename is applied throughout this document; if you
are holding an earlier copy, every `P0-0` in it means the cohort monitor described in
Phase 0 below, not the deployed snapshot-retention item.

**Ground rules for every item below.** No fabricated results. Where a change needs a number that has not been measured, add the measurement first or leave an explicit `NotImplementedError` stub — never a plausible default. Where a change touches a stored field's meaning, it is a migration, not a side effect. Every behaviour change lands with a test that fails before it and passes after.

---

## Phase 0 — measurement only, zero behaviour change

These produce evidence. None of them may alter a live decision path. Land them first so Phase 3 has something to argue from.

### P0-5 · Standing hold-vs-actual cohort monitor
**Finding:** the edge is a price edge, not a forecasting edge — and no current metric would notice it decaying

This is the load-bearing measurement and it was missing from revision 1. `config.py:1533-1538` already establishes what P0-1 was written to go and find out: over 358 traded rows with a stored prediction, **Brier 0.1930 for the model against 0.1842 for the entry ask** — the model is worse calibrated than the market it trades against — with mean model_prob 0.432 against a 0.344 realised win rate, about 9 points of overconfidence.

What produces the P&L is a **price** edge: mean entry 0.306 against that 0.344 realised win rate. That is why a month of calibration work moved Brier and did not move P&L. The repo states the consequence explicitly and nothing acts on it yet: *this edge can decay without any calibration metric noticing, so the cohort needs re-scoring on the same hold-vs-actual basis, not on Brier.*

- New module `cohort_monitor.py`. Reproduce exactly the 2026-09-02 measurement: every closed position carrying a recorded settlement, actual P&L against the same row held to settlement at the same entry price, split four ways (as traded / stop only / take only / neither).
- Report the day-clustered bootstrap over station-days, not a naive mean — the original used 252 station-days for 514 rows and the clustering is what makes the interval honest.
- Report the price edge as its own line: mean entry price against realised win rate, with its own interval. **That number, not Brier, is the decay alarm.**
- Run it on a rolling window (trailing 30 and 60 days) as well as all-time, so decay shows up as a trend rather than only as a level.
- Wire the output into `calibration_panel.py` alongside the existing bias / EV / model-vs-market columns.
- **Set a kill criterion, in config, before the P2-2 flip.** A monitor without a pre-committed threshold is a dashboard you will rationalise during a drawdown. This is a bias exploit — agency outlook language runs warm, the market follows the language, the station reads cooler — and bias exploits close, either because the market learns or because the agencies fix their bias. Neither event would move any calibration metric. Pick the threshold on the price-edge line, not on P&L (P&L is too noisy at these sizes), express it as a rolling-window level with a minimum sample, and decide what firing means: halt the station, halt the book, or drop to paper. Write it down while there is no money on the line. `config.py`'s existing gate constants are the model for how to state it.

**Acceptance:** `tests/test_cohort_monitor.py` — re-running against the 2026-08-03..09-01 window reproduces the four published totals (−$295.15 / +$186.81 / +$283.37 / +$765.33) to the cent. If it does not, the discrepancy is the finding and must be resolved before the module is trusted.

**This supersedes P0-1 as the headline measurement.** P0-1 is still worth building, for the reasons stated below, but it is no longer the thing that answers the most important open question.

### P0-1 · Full-book Brier harness
**Finding:** EV-4 (Brier only grades trades you chose to take)

`promotion_dossier.live_calibration()` scores model vs. market on *closed positions*, i.e. the subset where the two disagreed enough to trade. That answers "does the model win where it thinks it wins," not "is the model calibrated."

> **CORRECTION (rev 2).** Revision 1 sold this item as answering "the question none of the current instrumentation can: is the model actually calibrated." That premise was wrong — `config.py:1533-1538` already answers it, and the answer is no: the model loses to the market on Brier and is ~9 points overconfident. Build this anyway, but for the *breakouts*, not the headline: the price-decile reliability table is the input P3-2 needs, the `spread_source` breakout is what P3-1 and P3-4 need, and the above-mode / below-mode split is what P3-3 needs. Do not treat a good aggregate Brier here as evidence the model is fine, and do not treat this as the decay alarm — that is P0-5.

- New module `model_scorecard.py` (importable, no import-time side effects, mirroring `calibration_panel.py`'s constraints).
- For every station-day with a settlement-grade reading **and** a stored EV snapshot: score every listed bucket, both sides, whether or not it was traded.
- Reuse `ev_engine.save_ev_snapshot` output as the source of `model_prob` and `market_price`. Do **not** recompute probabilities — a recomputation reads a calibration that has since seen the outcome.
- Report: mean Brier model, mean Brier market, paired gap, `n`, `n_days`, and a reliability table (predicted-probability decile → observed frequency).
- Carry `promotion_dossier`'s two existing honesty rules verbatim: `None` rather than zeros on an empty sample, and `n_days` reported alongside `n` because buckets on one day are one draw of the weather.
- Break the reliability table out by `spread_source` and by price decile — that is what tells you whether EV-1 and EV-2 are real.

**Acceptance:** `tests/test_model_scorecard.py` — empty input returns `None`; a synthetic perfectly-calibrated book returns a gap of ~0 with the reliability table on the diagonal; a synthetic overconfident book shows the characteristic S-curve.

### P0-2 · Re-run the spread-tier comparison against recorded ensemble history
**Finding:** EV-5 (the ensemble tier was demoted on a test that couldn't see it move)

`spread_tier_brier.py` scored the ensemble tier at one standing width per station, because the value was fetched and discarded until `pipeline.ensemble_spread_for()` started recording it. The entire argument for an ensemble is its day-to-day movement. The comparison as run cannot see that.

- Confirm how many station-days of recorded ensemble spread now exist. If under ~60 per station, stop here and record that fact — do not re-run on a sample that cannot separate the tiers.
- Extend `spread_tier_brier.py` to read the *dated* ensemble spread per station-day instead of a standing value.
- Report the same shape as the original comparison (mean Brier gap, t-stat, per-station table, leave-one-station-out range) so the two runs are directly comparable.
- Add a row for `SPREAD_FLOOR_C` as its own tier. Raw ensemble dispersion ran 0.24–1.20 °C across the registry and the 0.70 floor clamps 24 of 35 stations upward — for those, "ensemble" and "the floor" are the same number, and the comparison must say so.

**Acceptance:** the report distinguishes floor-clamped from unclamped stations, and states the sample size per station rather than pooling silently.

### P0-3 · Test the Normal assumption
**Finding:** EV-7 (symmetric Normal on a left-skewed tropical maximum)

- New script `distribution_audit.py`: for each station with ≥30 settlement-grade readings, compute the residual distribution `observed_max − corrected_central_estimate` and report skew, excess kurtosis, and an Anderson–Darling statistic against the fitted Normal.
- Also report empirical bucket-hit frequency vs. the Normal-implied frequency, bucket by bucket relative to the mode — that is the number that translates directly into mispricing.
- No code change to `probability.py` in this phase. If skew is material (|skew| > ~0.5 on WSSS/WMKK), it becomes P3-3.

**Acceptance:** report runs on the live DB and prints per-station rows with `n`; stations under the threshold print "insufficient" rather than a number.

### P0-4 · Quantify the spread floor's effect on the tradeable set
**Finding:** EV-1 (the floor caps model confidence at ~52.5% on a 1 °C bucket)

Arithmetic, already done: at `SPREAD_FLOOR_C = 0.70` the maximum probability the model can assign to any single 1 °C bucket is 0.525; at the 1.0 fallback it is 0.383; at a typical measured 1.21 it is 0.321.

- Add this as a computed diagnostic to `calibration_panel.py` (a per-station "max attainable bucket prob" column), so the constraint is visible on the dashboard rather than derivable only by hand.
- Count, over stored EV snapshots, how many YES candidates were rejected *solely* because `model_prob` could not exceed the price — i.e. how much of the book the floor removes.

**Acceptance:** panel renders the column; the count is reported with its date range.

---

## Phase 1 — correctness and safety, independent of any measurement

Land these regardless of what Phase 0 says. Each is a defect on its own terms.

### P1-1 · Re-check the day budget at the resolved order size
**Finding:** ENTRY-2

`apply_portfolio_budget()` scales legs to fit the remaining station/day and portfolio/day budget. `wallet_client.build_entry_order()` then raises the share count to the exchange minimum — up to the `LIVE_SIZE_OVERSHOOT_CEILING_USD` of $5 on a $1 request. `executor._resolved_size_ok()` re-runs depth, slippage and net-EV at the resolved notional but **not** the day budgets, and `_live_budget_breach()` (which does re-check at resolved size) covers only the region caps and only in live mode.

- In `executor._resolved_size_ok()`, add a day-budget re-check at `spec.notional_usd` using `entry_manager.station_day_exposure_usd()` and `portfolio_day_exposure_usd()`, scoped to the decision's track (`is_paper` per `_candidate_is_paper`).
- Fail closed on an unreadable exposure, matching the existing convention in `decide_portfolio_entries`.
- Run it in **every** order-path mode, not just live — simulation writes rows at the resolved notional and those rows feed the next cycle's exposure total.

**Acceptance:** `tests/test_resolved_size_budget.py` — a decision scaled to $0.30 that resolves to $2.25 against a budget with $1.00 remaining is refused with a reason naming the binding cap.

### P1-2 · Charge the limit pad against expected value
**Finding:** ENTRY-3

Three things consume the 10% slippage budget — tick alignment, `_pad_limit`'s up-to-3% widening, and book-walk slippage — measured at two different layers and never summed. `_resolved_size_ok` re-derives net EV as `net_ev − (slippage_new − slippage_old)`, so the pad never appears in the number the approval is tested against.

- Add `pad_cost_pct = (spec.limit_price − spec.expected_price) / spec.expected_price` to `OrderSpec`.
- Subtract it in `_resolved_size_ok`'s net-EV re-derivation, and test the result against `decision.min_net_ev` exactly as now.
- Log the three components separately in the resize note so a journal reader can see which one ate the budget.

**Note:** the pad is a *worst case*, not an expectation, so subtracting it in full is conservative. That is the correct direction, but say so in the docstring rather than implying it is the expected cost.

**Acceptance:** `tests/test_pad_in_ev.py` — a decision at the net-EV bar with a 2-tick pad on a $0.30 share is refused; the same decision with a sub-tick pad is approved.

### P1-3 · Close the `_resolved_size_ok` early return
**Finding:** ENTRY-4

The function returns `True, ""` immediately when `resolved <= requested`, validating nothing. The depth ceiling also tests against `decision.available_depth_usd`, which is the stale fetch from `evaluate_entry`; only slippage is re-read live.

- Always re-fetch depth and slippage at submission time, regardless of whether the size moved.
- Keep the "size did not move" note distinct from "size moved" so the log still distinguishes them.
- If the extra `/book` call per entry is a latency concern, cache the book for the duration of one `open_position()` call rather than skipping the check.

**Acceptance:** `tests/test_resolved_size_stale_depth.py` — an entry whose book thinned between sizing and submission is refused even though the resolved notional equals the requested one.

### P1-4 · Alarm at fill time when a position becomes unexitable
**Finding:** EXIT-3

`build_exit_order()` refuses below the market share minimum — loudly, and correctly. But the trap springs at *entry*: WSSS 2026-08-20 requested 5.00 shares, received 4.891, and had no working stop for its whole life. `_shares_at_worst_fill()` prevents new occurrences at order-construction time; nothing inspects the actual fill.

- In `executor._open_via_order_path()`, after a live fill, compare `result.fill_shares` against the `min_order_size` already on the spec.
- If below, emit an `[ACTION NEEDED]` block naming the position, and set a persisted flag on the row (new column `exit_blocked_reason` on `positions`, nullable).
- `position_manager._check_one_position()` reads the flag and reports it in the cycle log instead of discovering it on the first stop attempt.
- Do **not** refuse to record the position — the shares exist, and an unrecorded real position is strictly worse.

**Acceptance:** `tests/test_unexitable_fill.py` — a fill of 4.891 against a 5-share minimum sets the flag and prints the alert; a fill of 5.00 does not.

### P1-5 · Escalate repeated exit failures
**Finding:** EXIT-4

An unfilled FOK sell prints one line and returns; next cycle retries. No counter, no escalation — unlike `_note_price_failure` and `_note_live_close_refused`, which both escalate. Exit failure correlates with a thin book, so failures will cluster, and clusters currently produce silence.

- Add `_consecutive_exit_failures: Dict[str, int]` in `executor.py`, keyed by `position_id`, in-memory and per-process (same reasoning as `_consecutive_live_close_refusals`).
- Threshold: escalate to the full `[ACTION NEEDED]` block after 3 consecutive failures. A single killed FOK on a momentarily thin book is routine; three in a row is not.
- Reset on a successful fill or on a resolution close.
- Include the last three attempted limit prices in the escalation so an operator can see whether the book is moving away or simply empty.

**Acceptance:** `tests/test_exit_failure_escalation.py` — three consecutive unfilled exits produce exactly one escalation block, not three; a fill in between resets the counter.

### P1-6 · Make the live-authorisation state legible and durable
**Finding:** EXIT-5

Deployed mode lives in `/etc/polyweather/mode.env`, written once by `deploy_daemon.sh` and never rewritten. If it goes missing, `scheduler.py` comes up without live authorisation and `close_position()` refuses to sell every live position — safe in isolation, but the result is real money with no working stop and only a log escalation.

- On startup, `scheduler.py` compares the resolved `EXECUTION_MODE` against the set of stations that currently hold **open live positions** (`storage.load_open_positions(is_paper=False)`).
- Any station with an open live position but a non-live mode gets a startup `[ACTION NEEDED]` block, before the first cycle runs, naming the positions and the dollar amount stranded.
- Add a `--require-live` CLI flag that makes the daemon **refuse to start** in that state, for use in the systemd unit once you trust it.
- Do not auto-promote to live. The refusal is the safe direction; the fix is that it must be impossible to miss.

**Acceptance:** `tests/test_startup_live_mismatch.py` — an open live position plus a `manual_review` mode produces the block; `--require-live` exits non-zero.

### P1-7 · Settle from the observation record first, the book second
**Finding:** EXIT-6

`_close_as_resolved()` decides the winner by rounding the confirmed *book quote* at 0.5. The market settles on the airport thermometer. `_close_from_settlement_source()` — the honest path — runs only when the price feed is down.

- Invert the order in `position_manager._check_one_position()`: when Gamma reports the market closed, call `_settlement_grade_reading()` first.
- If a settlement-grade reading exists for the target date, close from it, passing `basis="<source> reading N.N C"` into `_close_as_resolved()`.
- Fall back to the quote only when no reading exists, and say so in the `basis` string.
- Keep the existing refusal to guess when neither is available.

**Risk:** stations whose settlement source publishes in arrears (VHHH / HKO CLMMAXT) will now fall through to the quote path more often than they do today — which is the current behaviour, so this is not a regression, but the log must make the distinction visible.

**Acceptance:** `tests/test_resolution_source_precedence.py` — with both a reading and a quote available and *disagreeing*, the close uses the reading and the log names it.

### P1-8 · Symmetrise the entry fee (migration)
**Finding:** EXIT-7, EV-3

Exit prices are stored net of the taker fee; entry prices are stored gross. Every recorded return is flattered by roughly 0.5–1.25% of stake per round trip — and this paper record is the only forward validation you have, since backtesting is blocked.

Two separate pieces, do not conflate them:

**(a) EV side — live, no migration.** `ev_engine.compute_ev_table()` subtracts only the entry fee. On any book that *sells* (live, simulation — anything not in `HOLD_TO_SETTLEMENT_MODES`) a second taker fee is due on exit. Add an expected exit-fee term to `net_ev`, gated on whether the candidate's mode holds to settlement. A position held to par pays no exit fee; one exited by selling pays `0.05 × (1 − p_exit)`. Use the take-profit target price as the estimator for `p_exit` and say in the docstring that it is an estimator.

**(b) Storage side — migration, separate PR.** Add a nullable `entry_fee_per_share` column, backfill it from `risk_manager.taker_fee_per_share(entry_price)` for rows with a known entry price, and have reporting compute net P&L from `(exit_price − entry_price − entry_fee_per_share)`. Do **not** rewrite `entry_price` itself — that changes the meaning of every historical row.

**Acceptance:** (a) `tests/test_exit_fee_in_ev.py` — a candidate on a selling book gets a lower net EV than the identical candidate on a hold-to-settlement book. (b) the migration is idempotent and every backfilled row satisfies `size_usd == entry_price * size_shares` unchanged.

### P1-9 · Make the stop-basis fallback visible
**Finding:** EXIT-8

`stop_basis_price()` falls back to `entry_price` when `entry_bid` is absent (pre-field rows, `manual_trigger`), silently running the old tighter stop. Your own measurement: 117 of 207 fires would not fire on the bid basis, 46 of those were eventual winners, −$401.74 across 13 of 17 stations.

- Have `stop_basis_price()` return `(price, basis)` where basis is `"entry_bid"` or `"entry_ask_fallback"`.
- Thread the basis into `ExitDecision.reason` so a stop fired on the fallback says so in the stored row.
- `stop_loss_audit.py` reads the same pair, so the audit and production cannot diverge on which basis was used.
- Consider — but do not implement without measuring — refusing the stop entirely on the fallback basis, i.e. treating "we never recorded the entry-side book" as a reason to hold rather than to use a knowingly-too-tight threshold.

**Acceptance:** `tests/test_stop_basis_provenance.py` — a position with no `entry_bid` produces a stored reason containing `entry_ask_fallback`.

### P1-10 · Record what a stop actually cost, not what it was set to
**Finding:** stops gap straight through their trigger, so the stated risk is not the realised risk

WMKK 2026-08-07 b35 NO @0.750: stop triggered at 0.675, filled at **0.060**. A 92% loss on a "30% stop." Reconstructed snapshots show the same pattern elsewhere — ZGGG 35C YES flat at 0.260 for seven consecutive reads then 0.110 on the eighth, straight through a 0.203 trigger. These are single jumps, not sampling misses, and `config.py` already concludes that no scan interval catches a price that never trades in between.

Mostly mooted by P2-2 on the books that stop trading — but `"simulation"` stays armed deliberately, so it survives there, and the historical record needs the distinction regardless.

- Store `trigger_price` alongside `exit_price` on every stop-loss close. Today the row records where it sold; nothing records where the rule said to sell.
- Add `stop_slippage_c = trigger_price − exit_price` to reporting, and report the distribution — median, 90th percentile, worst — not a mean. The distribution is the point: a mean hides that most stops fill near their trigger and a few fill 60 cents away.
- Any analysis that scores alternative stop thresholds (`stop_sweep.py`) must state its fill assumption in its own output. `config.py:2113-2125` documents the trap precisely: assume a cap always fills at its trigger and the best cell is +$146; assume it fills at the lowest quote the record can prove existed and the same cell is −$75. Same data, opposite sign.
- Do **not** attempt to backfill this from `price_snapshots`. Coverage is a median 25% of each position's hold window, with 365 of 514 positions under half. The record cannot support it and a backfilled number would be worse than no number.

**Acceptance:** `tests/test_stop_slippage_record.py` — a stop triggered at 0.675 and filled at 0.060 stores both prices and reports 0.615 of slippage; `stop_sweep.py` output contains an explicit fill-assumption line.

### P1-11 · Re-examine the post-decision exit cadence
**Finding:** the tighter cadence converted the best exit type into a worse one

When post-decision windows moved to 15/15/30, take-profit went from 21% to **44%** of all exits and resolution from 21% to **0%**. That was recorded at the time as the change paying for itself. Against the 2026-09-02 measurement it reads the other way: resolution is the highest-returning exit type (+18.9% held vs −7.3% as traded), and the cadence change is what stopped positions reaching it.

- No change until P2-2 lands — on a hold-to-settlement book the cadence question mostly dissolves, because there is no price level to catch.
- After P2-2, re-derive the exit-type mix on whatever books still run price rules and check whether 15/15/30 is still justified there. The original justification is now known to have been measuring the wrong thing.
- Separately: the threshold tightening at 10:00 is still keyed to an entry close that moved to 08:00 on 2026-08-17. Two hours a day run with entries shut and thresholds on their loose setting. `risk_manager.py`'s docstring flags this as an open question needing the freed capital modelled; P0-5 supplies exactly that basis, so it becomes answerable once P0-5 exists.

**Acceptance:** an analysis note, not a code change. If the answer is "leave it," that is a valid outcome and should be recorded in the `SCHEDULE_WINDOWS` comment block next to the original reasoning.

---

## Phase 2 — capital mechanics

The single highest-leverage change in the system. Findings EXIT-1 and EXIT-2 are one problem: you cannot hold to settlement because you cannot collect at settlement.

### P2-1 · Build the redemption path
**Finding:** EXIT-2

Nothing in the repo redeems. `executor.close_position()` prints `REDEEM IT ON THE EXCHANGE` and writes the row. Winning tokens sit in the funding wallet. `storage.load_settled_live_tokens()` exists to stop those holdings tripping reconciliation — a correct patch, but it is an allowlist of tokens permanently excluded from the backstop, and it grows with every resolved live position.

- **Research first, and record the finding before writing code.** Confirm how redemption works for NegRisk weather markets under `py-clob-client-v2` — whether it is a CTF `redeemPositions` call, a NegRisk adapter call, or a UI-only operation. If there is no programmatic path, that is a real answer: record it in the module docstring and stop. Do not write a plausible-looking call that has never been executed.
- Assuming a path exists: new module `redemption.py`, single public function `redeem_settled(dry_run: bool = True) -> list[RedemptionResult]`.
- Gated behind its own environment flag, independent of `POLYMARKET_LIVE_TRADING` — redeeming is not trading and should not require the trading gate, but it does move real assets.
- Reads `storage.load_settled_live_tokens()`, redeems each, and on success clears the token from the handover map (new column or status transition — `closed_resolution` → `closed_redeemed`).
- Scheduler calls it once per day in a `closed` or `collection` window, never inside an entry or exit window.
- `wallet_client.reconcile_live_positions()` then narrows: only genuinely-pending redemptions stay in the handover, so the backstop stops widening.

**Acceptance:** `tests/test_redemption.py` — `dry_run=True` performs no writes and lists exactly the tokens in the handover map; a successful redemption transitions the status and removes the token from the next `load_settled_live_tokens()` call.

### P2-2 · Extend hold-to-settlement to the real-money book
**Finding:** EXIT-1

**Strictly gated on P2-1 landing and being exercised at least once against a real settled position.**

Your measurement over 514 closed positions with a recorded settlement: hold-to-settlement returns +18.4% [+5.2, +31.5] against the −7.4% [−13.1, −1.8] actually realised, day-clustered bootstrap over 252 station-days, both intervals excluding zero. Stop-only +4.6%, take-only +7.0%, neither +18.9% — both price rules individually negative. The two cost **$1,038.82** between them: the stop 222 fires (mean 0.380 → 0.240, and 36% of what it killed would have won at settlement, −$600.61), the take 197 fires (mean exit 0.468 against a 0.543 settlement win rate on the same rows, ~7.5¢/share given away, −$481.96). `HOLD_TO_SETTLEMENT_MODES = ("paper",)` applies the best-measured policy only to the book with no money in it, because a held live winner would strand capital.

- Once redemption works, change `HOLD_TO_SETTLEMENT_MODES` to `("paper", "simulation", "live")`.
- `risk_manager.evaluate_exit()`'s carve-out currently requires `position.is_paper AND execution_mode in HOLD_TO_SETTLEMENT_MODES` — the `is_paper` conjunction exists to stop a mislabelled row disarming a real stop. With live in the set, that guard inverts its meaning and must be removed deliberately, with the reasoning rewritten, not left to read as protection it no longer provides.
- Land it station by station, WSSS first, via a per-station override rather than a global flip.
- Instrument before and after: realised return per dollar staked, on the same station, over a comparable window. P0-5 is the instrument.

**PREREQUISITE A — the bankroll has less room than the measurement assumed.** Peak concurrent open notional over the measured month was $432.95 as traded against **$748.18 held**, inside `BANKROLL_USD` ($1000) but not by much. `config.py:1560-1563` states the consequence: the portfolio caps bind sooner and the entry set will not be identical to the measured one. So the +18.4% was measured on a book that never actually carried $748 at once. Before flipping any station, replay the same cohort *with the portfolio and region caps applied at held exposure* and report how many of the measured entries would have been refused. If a large fraction would have been, the measured return does not transfer and either `BANKROLL_USD` or the caps have to move first — deliberately, as their own decision.

**PREREQUISITE B — decide `SIZE_STOPLESS_BOOKS_ON_PURE_KELLY` explicitly.** It ships `False`. The arithmetic says `True`: `gap_risk_haircut()` scales a position so a stop-out costs what Kelly was sized against, and on a book with no stop there is no trigger to gap through and no exit spread to pay, so the correct haircut is 1.0. But setting it `True` multiplies positions 1.4× at entry 0.50, 2.0× at 0.20 and 2.25× at 0.16, and `config.py:2193-2200` is explicit that the conservatism being removed is doing real work for a reason *not* stated in the haircut's own docstring: Kelly takes the model's probability at face value and this model is measurably overconfident (0.432 mean model_prob against 0.344 realised). Quarter-Kelly is the declared buffer; the haircut has been an undeclared second one. Extending hold-to-settlement to more books without settling this leaves the sizing regime ambiguous on exactly the books being changed. Decide it in the same PR, either way, and write down which.

**PREREQUISITE C — compute the sizing headroom before, not after.** Prerequisite A replays the cohort at *current* sizing. This asks the forward question. The measured price edge is 3.8 points (0.344 realised win rate against 0.306 mean entry). Kelly on that is `0.038 / (1 − 0.306) = 5.5%`, quarter-Kelly `1.37%` of bankroll = **$13.69** on $1,000. Actual mean position over the 514-row cohort is **$7.88** ($4,049.93 / 514), i.e. about 57% of quarter-Kelly-on-measured-edge. So there is nominal headroom — and taking it would push peak held exposure well past the $748.18 already measured, against a $1,000 bankroll and caps that would start refusing entries. Report the three numbers together (current, quarter-Kelly-on-measured-edge, peak exposure implied by each) and state explicitly whether `BANKROLL_USD` or the portfolio caps have to move to use the headroom. Do not size up as a side effect of prerequisite B; that is a second, separate decision.

**RETAINED, AND STATE IT: `MAX_POSITION_USD_EXPENSIVE` is now the only downside control.** Under hold-to-settlement maximum loss is the whole stake — `config.py:2108` says so directly. The $30 ceiling above entry 0.55 stops being one control among several and becomes the sole one, which is exactly why the 80-cell measurement rejected a price-triggered loss cap in its favour: a ceiling acts at entry, where gapping cannot reach it. Do not treat it as vestigial when the stop comes off, and do not relax it in the same PR that removes the stop.

**Also verify, do not assume:** veto 0c (`MAX_STOP_OUTS_PER_BUCKET_PER_DAY`) counts stop-outs and never fires on a stopless book. `config.py` argues veto 0b (open positions per bucket/side) covers it, because a position that is never price-exited holds the bucket until resolution. That argument is sound but it has only been reasoned, not measured on a live stopless book. Check it on paper before extending.

**Do not do this before P2-1.** A live book that holds to settlement and cannot redeem locks its own capital and the region exposure caps will progressively refuse every new entry.

---

## Phase 3 — model changes, gated on Phase 0

Each of these should be argued from a P0 output, not from the analysis alone.

> **REFRAMING (rev 3) — read this before prioritising anything in this phase.** Revision 1 wrote Phase 3 as though the model were a probability estimator with fixable defects. The evidence supports something narrower. Two facts hold simultaneously and both are measured: the model is a **worse forecaster than the market** (Brier 0.1930 against the entry ask's 0.1842 on 358 rows), and it nonetheless **points reliably at underpriced buckets** (mean entry 0.306 against a 0.344 realised win rate).
>
> The reconciliation is that `model_prob` is a **direction indicator with a number attached**, not a probability. It successfully detects the founding bias — agency outlook language runs warm, the market follows the language, the airport station reads cooler — while being badly calibrated in absolute terms, by about 9 points.
>
> Three consequences govern this phase:
>
> 1. **Improving calibration has no demonstrated P&L path.** A month of it moved Brier and did not move return. P3-3 and P3-5 are calibration work. They are worth doing for correctness and they may be worth less in dollars than their placement implies — do not let them displace P3-6 or Phase 2.
> 2. **Anything that consumes `model_prob` as a probability is consuming a number known to be wrong.** Kelly sizing is the main offender; that is P3-6, and it is the highest-value item in this phase.
> 3. **The thing to protect is the price edge, not the forecast.** P0-5, not P0-1.

### P3-6 · Size on a calibrated probability, with one declared buffer
**Gated on:** P0-5 (needs the realised-outcome record it assembles) · **New in rev 3 · Highest-value item in this phase**

`entry_manager` sizes with `f* = raw_edge / (1 − price)` on `model_prob`, then applies `KELLY_FRACTION` (0.25) *and* `gap_risk_haircut()`. `config.py:2193-2200` already identifies the problem: quarter-Kelly is the declared buffer for the model's overconfidence, and the haircut has been an undeclared second one. Two stacked corrections for a bias you can measure directly is worse than one correction fitted to the measurement.

- Fit a calibration map — isotonic regression, or Platt scaling if the sample is too thin for isotonic — from `model_prob` to realised outcome, over the same cohort P0-5 assembles.
- Fit it **out-of-sample per day**, on strictly earlier days only, exactly as `corrected_error_rmse()` already does for the bias correction. A map fitted on the whole record and applied retrospectively is the same leak `estimate_std_dev(allow_measured=False)` refuses for the backtest.
- Carry the calibrated value on `EVResult` as its own field with its own provenance, alongside the raw `model_prob`. **Do not overwrite `model_prob`** — it is what the EV table, the snapshots and every stored row mean today, and P0-1 scores against it.
- Feed the calibrated value to the **sizing** path only, at first. Whether the *edge gate* should also move to it is a separate decision with a different risk profile, and bundling them makes the result unattributable.
- Then apply quarter-Kelly once, with `gap_risk_haircut()` retired on any book covered by `HOLD_TO_SETTLEMENT_MODES` — which is what prerequisite B is really asking, and this is the version that makes the answer principled rather than a judgement call.
- Below `MIN_BIAS_PAIRS_BEFORE_ENTRY`-scale samples the map is not estimable. Fall back to the raw `model_prob` with the existing double buffer and say so in the note, exactly as `estimate_std_dev` reports `fallback_default`.

**Expected effect, stated so it can be checked rather than assumed:** a map fitted on 0.432 → 0.344 shrinks probabilities toward the realised rate, which shrinks `raw_edge`, which shrinks positions — offset by removing the haircut, which grows them 1.4× at 0.50 and 2.0× at 0.20. The net could go either way and the point is that it would go there *for a measured reason*.

**Acceptance:** `tests/test_calibrated_sizing.py` — the map fitted on day N uses no data from day N or later; a station below the sample threshold falls back to raw `model_prob` and reports the fallback; `model_prob` on every stored row is unchanged by the feature.

### P3-1 · Revisit the spread floor
**Gated on:** P0-1 (reliability table), P0-4 (tradeable-set count) · **Finding:** EV-1

If the reliability table shows the model is *under*confident at the floor — predicted 0.50 buckets hitting materially more than 50% of the time — the floor is costing edge and should come down or become per-station. If it shows overconfidence, the floor is doing its job and stays.

> **CORRECTION (rev 2) — the direction is already known.** Revision 1 left this open. It should not have: mean model_prob 0.432 against a 0.344 realised win rate is ~9 points of overconfidence, aggregate, already measured. **Lowering `SPREAD_FLOOR_C` is therefore the wrong way to move**, and the floor is currently compensating for a real defect rather than merely capping the tradeable set. Rewrite this item as "should the floor come *up*, or become per-station where a station's own reliability row justifies it." P0-4's tradeable-set count is then a cost, not an argument — it tells you what the compensation is buying, not that it should stop.

- Make `SPREAD_FLOOR_C` resolvable per station (`config.spread_floor_c(station_icao)`), defaulting to the current global 0.70, mirroring how `REGION_SPREAD_CEILING_C` already works.
- Do not change any station's value without its own reliability row.
- Any downward move on any station needs that station to be shown *under*confident on its own row, against the aggregate finding, not merely absent from it.

### P3-2 · Rank on absolute expected dollars, not percentage EV
**Gated on:** P0-1 reliability by price decile · **Finding:** EV-2

`best_opportunities()` sorts by `net_ev_per_dollar`, which is `raw_edge / price` — mechanically largest at low prices. Model error is roughly constant in absolute degrees, so relative error is worst exactly where this ranking is most favourable. `EV_MIN_PRICE_SCREEN` (0.03) and `MIN_ABS_RAW_EDGE` (0.03) are blunt patches on this.

- Add `expected_dollars = net_ev_per_dollar * recommended_size_usd` as a secondary sort key, or replace the primary sort outright if the price-decile reliability table shows cheap buckets are systematically overpredicted.
- This is a *ranking* change, not a gating change — it changes which candidate wins when several clear the bar, and the per-cycle candidate count is usually small. Measure the actual effect on selection before assuming it matters.

### P3-3 · Skew-aware bucket probabilities
**Gated on:** P0-3 · **Finding:** EV-7

Only if `distribution_audit.py` shows material left skew on the stations that trade.

- Replace the Normal CDF in `probability.py` with a skew-normal, keeping `_bucket_interval()` and the tail-folding logic untouched — those are correct and independently tested.
- The shape parameter is a *measured* per-station quantity from P0-3, carried on `CalibratedEstimate` with its own provenance field, and a station without enough data gets the Normal and says so.
- `bucket_probabilities()` already raises rather than defaulting an axis; apply the same fail-closed stance to the shape parameter.

### P3-4 · Close the low-confidence gate gap
**Finding:** EV-8 · *No P0 dependency — could move to Phase 1 if you want it sooner*

`LOW_CONFIDENCE_SPREAD_SOURCES = {"fallback_default", "pooled_error"}` doubles the required edge. `"ensemble"` is not in the set — but the ensemble tier is exactly what fires for a station with too few error pairs to measure its own spread, and it is usually clamped straight to the floor (maximum permitted confidence) at the *normal* edge bar.

- Either add `"ensemble"` to the set, or better: make the multiplier a function of whether the spread is *station-specific*, which is the property the gate is actually about.
- `"replay_constant"` must stay out of the set for the reason already documented — putting it in creates a live/replay divergence in the one direction the backtest exists to rule out.

### P3-5 · Feed the honest spread to the model, not the flattering one
**Finding:** EV-6

`measured_error_spread()` takes the standard deviation about the sample's own mean — what would remain if the bias correction were instant and perfect. `corrected_error_rmse()` replays the correction the entry path really had and comes out wider (ZGSZ 1.07 → 1.21 on 2026-09-03). The wider, honest number feeds only the entry gate; the narrower one feeds the probability model.

> **STRENGTHENED (rev 2).** This item now has independent support beyond the argument from consistency. Widening the spread lowers every model probability, and the model is measurably overconfident by ~9 points (0.432 mean model_prob against 0.344 realised). So the direction of this change is corroborated by the P&L record, not just by which estimator is more honest. That raises its priority relative to the rest of Phase 3 — but it does not remove the gate, because the *size* of the correction still has to be measured.

- Change `estimate_std_dev()`'s `measured_error` tier to read `corrected_error_rmse()` instead of `measured_error_spread()`.
- This widens every station's distribution slightly, which lowers every model probability and therefore every edge — expect fewer trades, and measure how many before landing it.
- Score both with P0-1's harness on the same station-days before choosing. This is a two-line change with a large downstream effect and it should not be made on the argument alone.
- Note that this and P3-1 pull the same lever from opposite ends — both widen the effective distribution. Land one, re-measure, then decide on the other. Landing both blind would double-count a single correction.

---

## Sequencing summary

```
Phase 0 (parallel, no risk)      P0-5 ★  P0-1  P0-2  P0-3  P0-4
Phase 1 (parallel, independent)  P1-1 … P1-9   P1-10
                                 P1-11 ← deferred until after P2-2
Phase 2 (strictly serial)        P0-5 ──► P2-1 ──► [prereq A, prereq B] ──► P2-2
Phase 3 (each gated)             P3-6←P0-5 ★      P3-1←P0-1,P0-4
                                 P3-2←P0-1        P3-3←P0-3
                                 P3-4 (ungated)   P3-5←P0-1
                                 (P3-1 and P3-5 mutually gated)
```

P0-5 moved onto Phase 2's critical path in rev 2: it is the instrument P2-2 needs to tell whether the flip worked, so it has to exist before the flip, not after. Rev 3 adds a second consumer — P3-6 needs the realised-outcome record P0-5 assembles in order to fit its calibration map, which makes P0-5 the input to the two highest-value items on the list.

**If you do only one thing:** P2-1. It unblocks the $1,038.82-per-month policy change, stops the reconciliation allowlist growing, and ends the capital lockup — and it is the only item on this list whose absence is currently costing measured money.

**If you do only one measurement:** ~~P0-1~~ **P0-5**. Corrected in rev 2. The question P0-1 was meant to answer is already answered — the model is worse calibrated than the market and ~9 points overconfident, and the P&L comes from a price edge (mean entry 0.306 against a 0.344 realised win rate), not a forecasting one. The consequence, in the repo's own words, is that this edge can decay without any calibration metric noticing. P0-5 is the only thing on this list that would catch that. P0-1 remains the input to four Phase 3 items and is worth building second.

---

## Not rejected — untested or deferred

Distinct from the section below. These have not been measured, so they are not decided. Recording them here stops them being read as rejected, and stops them being adopted on an argument.

- **A partial take** — sell half at the target, hold the remainder to settlement. It is **not** in the four-row table and nothing in the record speaks to it. The table compares all-or-nothing configurations; its rows cannot be interpolated. If it is worth testing, it needs its own labelled cohort, and the label must be applied at open.
- **Price-gated entry** (e.g. YES below 20¢ only). Deferred pending P0-1's price-decile reliability table, which is the instrument that decides it. Two things are already known and neither settles it: the ranking already tilts cheap because `net_ev_per_dollar` is `raw_edge / price`, and at a 1.2 °C spread a 0.2 °C central-estimate error moves a 20¢ bucket's fair value by ~3.1¢ — the whole of `MIN_ABS_RAW_EDGE` — against ~0.4¢ at the mode. If tested, tag the cohort at open; splitting after the fact by price cannot work, because price is the variable under test.
- **Moving the edge gate onto the calibrated probability** (P3-6 moves only the sizing path). Different risk profile, and it changes which trades exist rather than how big they are.

---

## Items deliberately not on this list

- **Reintroducing maker/GTC orders.** Unresolved from a prior session and orthogonal to everything here. The FOK design is what makes the "nothing is recorded unless it filled" invariant hold, and P1-4 and P1-5 both assume it.
- **Rewriting `entry_price` to be net of fees.** P1-8(b) adds a column instead. Rewriting the field changes the meaning of every historical row and would invalidate the very record P0-1 scores against.
- **The $1 clamp / $5 exchange-floor bracket itself.** P1-1 makes the caps in that range enforceable, which is the defect. Whether `LIVE_TRADE_SIZE_USD` should be $1 at all is a sizing decision that needs the live track record, not a code change.
- **Reintroducing a trailing stop.** Built, instrumented and removed 2026-08-17. 907 evaluations, armed on 7 of 580 non-lottery ticks, never once observed a give-back, zero exits across four configurations. The diagnosis was sampling frequency, not thresholds — a trailing stop needs two observations, one past activation and a later one showing retreat, and the book crosses the whole band in a single step. Retuning cannot fix it. Under P2-2 the question disappears entirely.
- **Backfilling stop slippage from `price_snapshots`.** See P1-10. Coverage is a median 25% of each position's hold window, 365 of 514 positions under half. The record cannot support the number and a backfilled one would be worse than none.
- **A price-triggered per-position loss cap.** Already measured over 80 (cap distance, min size, min price) cells and rejected: its sign is decided by the fill assumption, not the data. `MAX_POSITION_USD_EXPENSIVE` ($30 above entry 0.55) is the ceiling that replaced it, and it acts at entry where gapping cannot reach it.
