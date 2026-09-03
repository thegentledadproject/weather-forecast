# Remediation plan, revised

**Date:** 2026-09-03
**Status:** revision of `polyweather-remediation-plan.md` (same date)
**Verified against:** `thegentledadproject/weather-forecast@main` at `dcf2870`
**Method:** every factual claim in the source plan checked at file:line; local DB
queried for volume; spread-floor arithmetic recomputed independently.

---

## Status addendum (2026-09-03, post-deploy)

**The six NOW-wave items below (P0-0, P0-4, P1-3, P1-4, P1-5, P1-9) are DONE.**
Committed `807da5b` on `fix/remediation-wave-1`, merged to main at `754888f`,
pushed to origin, and deployed to the EC2 box **2026-09-03 13:40:58 UTC**.
Everything in §0, §2, and §4 below still describes them as pending — read
those sections as the plan that WAS executed, not as an open TODO list.

**File:line citations into `ev_engine.py`, `executor.py`, `risk_manager.py`,
and `position_manager.py` are now STALE** — the wave's own fixes edited
exactly the functions those citations point at. Drift ranges from a couple of
lines to over 150. Two are actively misleading rather than merely off:
`risk_manager.py:299-312` now shows `stop_basis_price()` returning
`Tuple[float, str]`, the opposite of what this document says it shows; and
`executor.py:985-991` no longer contains the P1-5 defect at all — it lands on
an unrelated, correctly-behaving block. Citations into files the wave never
touched (`config.py`, `entry_manager.py`, `probability.py`, `calibration.py`,
`scheduler.py`, `clients/wallet_client.py`) are still exact. **Grep for the
quoted text, don't trust the line number, in any section below.**

**P0-2's cited evidence (§1.4, §4) was drawn from an unrepresentative
database.** "2 rows (2026-08-29, 2026-08-30)" was the local dev checkout,
not the box. The box's real `ensemble_spread` held **198 rows across 35
stations and 6 dates (2026-08-29 through 2026-09-03)** as of this deploy —
~0.94 rows/station/day. The conclusion is unchanged (reaching the plan's own
≥60-per-station bar at that rate lands at **~2026-10-31 to 2026-11-01**), it
is now supported by the right number.

**P0-1's window, dated concretely:** `ev_snapshots` began recording at the
deploy timestamp above. Attempt P0-1 no earlier than **~2026-10-03**, and
check the table's actual row count and date spread on the box before
assuming the 30–60 day estimate has been reached — don't just count calendar
days, since a data gap or a partial deploy would still show a date range
without the row count to back it.

**Unresolved, unchanged by the deploy:** all four items in §6 (fund the EOA,
whether the daemon ever redeems, formal read-only measurement access, P2-2's
rollout station). §6's Q3 has an informal answer now — read-only SSH queries
against the box's live DB worked fine during this deploy — but nothing was
built or packaged from it.

See [[remediation-wave-1]] in project memory for the full deploy record,
including the pre-deploy safety-check bug this session caught.

---

## 0. What this revision changes, and what it does not

**It does not overturn a defect.** Of the source plan's 24 findings, 21 verify
exactly where the plan says they do. The entry-path items, the exit-path items
and the EV-stack items all describe real code doing what the plan says it does.
That work stands.

**What changes is order, cost, and feasibility.** Four structural problems, and
two of them sit in the two items the source plan singles out as most important:
its "if you do only one thing" (P2-1) and its "if you do only one measurement"
(P0-1). One of those cannot be built as specified. The other is not the task the
plan thinks it is.

The net effect is that Phase 0 is not a parallel lane, Phase 3 is roughly two
months further out than the sequencing diagram implies, and the cheapest
unblocking change in the whole system is not on the list at all.

---

## 1. Four structural problems

### 1.1 P0-1 cannot be built as specified — BLOCKED

The plan sources the full-book Brier harness from `ev_engine.save_ev_snapshot`
and instructs: "Do **not** recompute probabilities — a recomputation reads a
calibration that has since seen the outcome."

That instruction is right. The source is not available.

`save_ev_snapshot` writes `data/ev_latest_<ICAO>.json` and **overwrites**
(`ev_engine.py:682`, `os.replace` at `:718`). There is no history: no table in
the DB, one file per station on disk. The local checkout holds exactly one, and
it is empty:

```json
{"station_icao": "WSSS", "generated_at": "2026-08-29T16:26:07+00:00",
 "target_date": null, "results": []}
```

The codebase has already reasoned through this exact constraint and reached the
opposite conclusion. `config.py:4062`:

> THE SOURCE IS THE BACKTEST, deliberately. Scoring this needs POINT-IN-TIME
> model probabilities [...] and the live path keeps only the latest EV snapshot
> (ev_engine.save_ev_snapshot overwrites ev_latest_&lt;ICAO&gt;.json). Recomputing
> past probabilities from today's record would be lookahead [...]

And `promotion_dossier.py:50` documents the workaround it adopted instead —
`positions.model_prob`, stored at entry and never recomputed. **That is exactly
why `live_calibration()` scores closed positions only.** The plan diagnosed the
selection bias correctly and then prescribed a cure that needs data nobody kept.

There is a second, smaller problem with P0-1 as written. It proposes a *third*
scoring path (`model_scorecard.py`) alongside `promotion_dossier` and
`calibration_panel`, while P1-9 in the same document insists the audit and
production must not diverge on which stop basis was used. The same rule should
apply here: one scorer, read by three consumers.

**Resolution:** split P0-1 into a data change that lands now and an analysis that
lands in ~30–60 days. See §3 (P0-0).

### 1.2 P2-1's research is already done and committed — RESCOPE

The plan opens P2-1 with:

> **Research first, and record the finding before writing code.** Confirm how
> redemption works for NegRisk weather markets under `py-clob-client-v2` [...]
> If there is no programmatic path, that is a real answer.

That research was completed and committed two days before the plan was written.
Commit `e840dad` (2026-09-01) adds
`docs/superpowers/specs/2026-09-01-redemption-design.md` — 296 lines, status
*design approved, not yet implemented*, every fact established by read-only
on-chain probe rather than assumption:

| Established | Value |
|---|---|
| Redemption target | `NegRiskAdapter` `0xd91E…5296`, **not** `ConditionalTokens` |
| Token holder | the funder, a **contract** (signature type 3, POLY_1271) |
| Authorisation | EIP-712 signed payload to `execute(...)`, selector `0xe8c8bf64` |
| Redemption in `py-clob-client-v2` | none — zero matches for "redeem" |
| Gas available | **0.000000 POL** in both the EOA and the proxy |

The design also names the obvious wrong guess explicitly: routing through
`ConditionalTokens` is "the obvious first guess and it is wrong."

**Two consequences.**

First, P2-1 is not a research task. It is an implementation task against an
approved design, with one non-code prerequisite: **the operator must fund the
EOA with POL.** Nothing broadcasts until that happens; everything up to and
including simulation works without it.

Second, the plan contradicts an approved scope decision. The design's header
reads "operator-run script. The trading daemon never redeems." The plan says
"Scheduler calls it once per day in a `closed` or `collection` window." That is
a reversal, and if it is intended it needs to be argued against the design, not
introduced as a bullet.

**Also worth carrying forward from the design, because the plan's framing invites
a misreading:** what is redeemable today is three positions, all losers at
`curPrice: 0`. Redeeming them collects **$0**. The value is future winners and
the permanent removal of the halt mechanism — not today's money. The design
states this plainly "so nobody later reads a $0 result as a failure."

### 1.3 P2-2 is justified on a stale number and understates its blast radius — RE-ARGUE

**The number.** P2-2 rests on "hold-to-settlement returns +18.4% against the
−7.3% actually realised" over 514 closed positions, and the plan's summary calls
it "the +25-point policy change."

Hold-to-settlement already shipped for the paper book (`2c8db40`,
`HOLD_TO_SETTLEMENT_MODES = ("paper",)` at `config.py:1566`). The most recent
measurement of that live-running policy has the hold edge decayed to **+2.8%
with a confidence interval spanning zero** over the trailing 14 days. The
+18.4% figure describes a window the system is no longer in.

This does not kill P2-1 — the capital lockup and the growing reconciliation
allowlist are costs independent of any edge estimate. It does mean P2-2 should
be argued from the current number, and that the expected gain from flipping the
live book is much smaller than "+25 points" implies.

**The blast radius.** The plan names one guard that must be handled
deliberately:

> `risk_manager.evaluate_exit()`'s carve-out currently requires
> `position.is_paper AND execution_mode in HOLD_TO_SETTLEMENT_MODES` [...] that
> guard inverts its meaning and must be removed deliberately

That is correct and confirmed at `risk_manager.py:376`. But
`HOLD_TO_SETTLEMENT_MODES` has a **second consumer the plan does not mention**.

`entry_manager.py:144` `_book_has_stop()` reads the same tuple and feeds
`gap_risk_haircut(has_stop=...)` at `entry_manager.py:1009`. On a stopless book
the haircut returns `1.0` — see `tests/test_gap_risk_sizing.py:105`. **Adding a
mode to that tuple therefore increases position sizes on that book.**

Scope of the effect, precisely:

- For live stations that are both allowlisted **and** mature,
  `size_usd = min(size_usd, live_cap)` at `entry_manager.py:1025` clamps to
  `LIVE_TRADE_SIZE_USD`, which swallows the change.
- For `"simulation"`, and for any live station without that clamp, it does not.
  Those books size up.

And `_book_has_stop`'s own docstring states the reason it currently excludes
simulation:

> "simulation" is not in that set and keeps its stop, because it exists to
> rehearse live decisions exactly.

The plan's proposed `("paper", "simulation", "live")` invalidates that stated
reason. It may still be the right tuple — if live holds to settlement, simulation
holding too *is* rehearsing live exactly — but the docstring encodes the old
argument and must be rewritten, not left to read as protection it no longer
provides. That is the same standard the plan applies to `evaluate_exit`.

**Sites that encode this invariant:** `config.py` (`:1566`, plus reasoning at
`:2108`, `:2180`, `:3307`), `entry_manager.py:144`, `risk_manager.py:376`,
`backtest/compare.py:133`. Four non-test modules, plus four test files
(`test_hold_to_settlement_modes.py`, `test_haircut_on_a_stopless_book.py`,
`test_stop_basis.py`, `test_expensive_entry_size_cap.py`). The plan budgets for
one.

### 1.4 Phase 0 is not a parallel lane — RESEQUENCE

The plan presents Phase 0 as "parallel, no risk" and says to land it first so
Phase 3 has something to argue from. Only one of its four items can run today.

| Item | Runnable now | Why not |
|---|---|---|
| P0-1 | **No** | No retained EV history (§1.1) |
| P0-2 | **No** | `ensemble_spread` holds 2 rows (2026-08-29, 2026-08-30). The plan's own ≥60-per-station gate stops it |
| P0-3 | Needs prod | Requires ≥30 settlement-grade readings per station; unverifiable from this checkout |
| P0-4 | **Yes** | Arithmetic reverified below; the smallest item in the phase |

P0-2's blocker is structural, not incidental. `save_ensemble_spread` was added in
`39e2c51` on 2026-08-29 — five days ago. `spread_tier_brier.py:60` already says
so in its own comment. At one row per station-day, ~60 rows per station is a
**November** date, not a Phase 0 date. The plan deserves credit for self-gating
here; the correction is that a self-gated item should not be scheduled as
current work.

Because every Phase 3 item except P3-4 is gated on a Phase 0 output, **Phase 3
is effectively deferred by roughly two months.** The sequencing diagram shows it
as a near-term lane.

**P0-4's arithmetic verifies exactly.** Maximum probability the model can assign
to a single 1 °C bucket, `2Φ(0.5/σ) − 1`:

| σ (°C) | Max bucket probability | Source |
|---|---|---|
| 0.70 | **0.5249** | `SPREAD_FLOOR_C` (`config.py:3628`) |
| 1.00 | **0.3829** | fallback default |
| 1.21 | **0.3206** | typical measured (ZGSZ corrected RMSE) |

The 0.50 line is the one that matters: at the floor the model clears a
coin-flip price by 2.5 points and nothing more.

**Operational note:** this checkout's DB is empty — 0 positions, 0
`settled_buckets`, 0 observations, 441 forecasts, 2 ensemble-spread rows. Every
Phase 0 measurement has to run against the EC2 box, where the test suite writes
to the production DB via `test_no_fd_leak.py`. Whatever measurement harness gets
built needs a read-only path onto that box before it needs anything else.

---

## 2. Revised sequencing

```
NOW      P0-0  EV snapshot retention   ← new, unblocks P0-1
         P0-4  Spread-floor diagnostic
         P1-3  Close the _resolved_size_ok early return
         P1-4  Alarm on an unexitable fill
         P1-5  Escalate repeated exit failures
         P1-9  Stop-basis provenance

NEXT     P2-1  Redemption  (implementation, not research)
         ├── operator prerequisite: fund the EOA with POL
         P1-1  Day budget at resolved size   ← count first
         P1-2  Charge the limit pad to EV
         P1-6  Startup live-authorisation check
         P1-7  Settle from the observation record first
         P1-8a Exit fee in EV
         P1-8b entry_fee_per_share migration
         P3-4  Low-confidence gate gap  (ungated; can move up)

DEFER    P0-1  ~30-60 days after P0-0 lands
         P0-3  needs prod DB access
         P0-2  ~November (ensemble history)
         P3-1, P3-2, P3-3, P3-5  each behind its Phase 0 gate

RE-ARGUE P2-2  against the trailing-14-day number, with the
               gap_risk_haircut side-effect scoped
```

**If you do only one thing:** P2-1 still, but as implementation from the existing
design, and the first blocking step is funding gas, not writing code.

**If you do only one measurement:** P0-0. Not P0-1 — P0-1 has no input until P0-0
has been running for a month.

---

## 3. New item · P0-0 · Retain dated EV snapshots

**Status:** new; not in the source plan. Prerequisite for P0-1.

**Why this is the cheapest change on the list.** The snapshot payload already
carries every field P0-1 needs — `model_prob`, `market_price`, `raw_edge`,
`slippage_pct`, `net_ev_per_dollar`, `spread_source`, `notes`, per bucket per
side (`ev_engine.py:700-711`). It is thrown away on the next cycle for one
reason: the file is a dashboard handoff, and a dashboard only wants the latest.
Nothing about the decision to overwrite was about the data being unwanted.

- Add table `ev_snapshots`, keyed `(station_icao, target_date, bucket_c, side,
  generated_at)`, carrying the payload fields above verbatim.
- `save_ev_snapshot()` keeps writing `ev_latest_<ICAO>.json` unchanged — the
  dashboard handoff is not touched — and additionally appends to the table.
- Fails soft exactly as the JSON write does. `ev_engine.py:691` already sets the
  rule: "the EV table drives trading, the snapshot only drives reporting, so a
  disk error here must never break the cycle." A DB error here inherits it.
- Record nothing derived. No probabilities recomputed, no bias applied at write
  time — the point of the row is that it is what the model believed at
  `generated_at`.

**Volume:** ~35 stations × ~10 buckets × 2 sides × cycles-per-day. Order of a few
hundred KB per day uncompressed. Not a retention concern at any horizon that
matters.

**Acceptance:** `tests/test_ev_snapshot_retention.py` — two snapshots for the same
station-day at different `generated_at` both persist and are both readable; the
JSON file still contains only the later one; a DB failure prints and does not
raise.

**Then, ~30-60 days later:** build P0-1 against `ev_snapshots`, with the one
amendment from §1.1 — extend `promotion_dossier` or `calibration_panel` rather
than adding a third scorer, so the dashboard, the promotion decision and the
scorecard cannot diverge.

---

## 4. Per-item corrections

Every item below verifies as a real defect. These are corrections to the *stated
detail*, which matter because several change the cost of the fix.

| Item | Verdict | Correction |
|---|---|---|
| P0-1 | **Blocked** | No retained EV history; split into P0-0 + deferred analysis (§1.1) |
| P0-2 | **Blocked** | 2 rows of ensemble history; ~November, not now |
| P0-3 | Verified | Needs prod DB; no local sample |
| P0-4 | **Verified exactly** | Arithmetic reproduced: 0.5249 / 0.3829 / 0.3206 |
| P1-1 | Verified | `_candidate_is_paper` **does not exist**. Executor uses `is_paper=(mode != "live")` (`executor.py:604`). Also: the plan demands measurement everywhere else but asserts this breach without counting it — precedent is the `f383393` resize check, which found 0 of 83 real events affected. Count first |
| P1-2 | Verified | `_pad_limit` is in `clients/wallet_client.py:450`, not executor. The pad is `min(LIVE_LIMIT_PAD_TICKS=2, LIVE_LIMIT_PAD_MAX_PCT=0.03)`, not a flat 3%. The net-EV re-derivation is **exact**, not "roughly": `executor.py:328` |
| P1-3 | Verified | Early return at `executor.py:298-301` confirmed verbatim |
| P1-4 | **Cheaper than described** | `_open_via_order_path` **already reads** `result.fill_shares` (`executor.py:749`). `min_order_size` has zero occurrences in `executor.py` — thread it from the spec. The `_shares_at_worst_fill` docstring already cites the WSSS incident |
| P1-5 | Verified | `executor.py:985-991` prints and returns with no counter. Escalation precedents exist at `position_manager.py:883` and `executor.py:433/444` |
| P1-6 | **Cheaper, and misattributed** | `scheduler.py` never reads `/etc/polyweather/mode.env` — `executor.py:82/490/554` and `manual_trigger.py:110` do. But `scheduler.py:715` **already calls** `load_open_positions(is_paper=False)` for preflight reconciliation, so the mismatch check is a few lines at an existing call site |
| P1-7 | Verified | `_close_as_resolved` rounds the quote at `position_manager.py:554`; `_close_from_settlement_source` is reached only on a price-feed failure |
| P1-8 | Verified | Asymmetry confirmed and **already documented** at `executor.py:796` as a KNOWN ASYMMETRY. `ev_engine.py:57` states exit fees are not modelled. No `entry_fee_per_share` column exists |
| P1-9 | Verified | `stop_basis_price` returns a bare float and falls back to `entry_price` (`risk_manager.py:299-312`) |
| P2-1 | **Rescope** | Research already committed (`e840dad`); daemon-scheduling contradicts the approved scope decision (§1.2) |
| P2-2 | **Re-argue** | Stale justification; second consumer via `gap_risk_haircut` (§1.3) |
| P3-1 | Verified | `SPREAD_FLOOR_C = 0.7` (`config.py:3628`); `region_spread_ceiling_c()` at `config.py:3522` is the pattern to mirror |
| P3-2 | Verified, formula wrong | `net_ev_per_dollar` is **not** `raw_edge/price` — it is `raw_edge/price − slippage − fee_pct` (`ev_engine.py:281`). The low-price bias argument survives; the stated formula does not |
| P3-3 | Verified | Symmetric normal CDF at `probability.py:34`; `bucket_probabilities()` raises rather than defaulting the axis at `:116` |
| P3-4 | Verified | `LOW_CONFIDENCE_SPREAD_SOURCES = {"fallback_default", "pooled_error"}` (`config.py:3670`); `replay_constant`'s exclusion is deliberate per `calibration.py:516` |
| P3-5 | Verified | `estimate_std_dev()`'s measured tier calls `measured_error_spread()` (`calibration.py:524`). **Note:** the tier order changed on 2026-08-29 so `measured_error` now fires *before* `ensemble`; `spread_tier_brier.py:11` still describes the old order and is stale |

### 4.1 One interaction the source plan treats as independent

P3-1 (lower the spread floor) and P3-5 (feed the wider, honest spread to the
model) both move the same number in `estimate_std_dev()`, in opposite
directions, and the floor decides which one binds. `SPREAD_FLOOR_C` clamps 24 of
35 stations upward today; switching the measured tier to `corrected_error_rmse`
makes those numbers larger, which moves stations *off* the clamp — so P3-5
partly does P3-1's job by a different route.

They should be scored jointly on the same station-days, not gated separately on
the same P0-1 output and landed independently.

---

## 5. What the source plan gets right, and should be kept verbatim

- **P1-8's refusal to rewrite `entry_price`.** Adding a nullable
  `entry_fee_per_share` column instead is the correct call, and the stated reason
  — that rewriting the field invalidates the record P0-1 scores against — is
  exactly right.
- **The "items deliberately not on this list" section.** Naming what was
  considered and rejected, with reasons, is what makes the rest of the document
  trustworthy.
- **Carrying `promotion_dossier`'s honesty rules verbatim** into any new
  reporting: `None` rather than zeros on an empty sample, and `n_days` alongside
  `n` because buckets on one day are one draw of the weather.
- **P1-6's refusal to auto-promote to live.** "The refusal is the safe direction;
  the fix is that it must be impossible to miss."
- **P1-9's rule that the audit and production must read the same basis.** It is
  the right rule; §1.1 only asks that it be applied to P0-1 as well.
- **The ground rules in the header.** No fabricated results; a stub over a
  plausible default; a stored field's meaning changing is a migration, not a side
  effect. Those held up under checking — the plan does not invent a single number.

---

## 6. Open questions for the operator

1. **Fund the EOA with POL?** Nothing in P2-1 broadcasts until this happens. It
   is a manual, off-repo action and it gates the plan's highest-value item.
2. **Does the daemon ever redeem?** The approved design says no. The source plan
   says once daily in a closed window. One of them has to give.
3. **Read-only access onto the EC2 DB for measurement?** Every Phase 0 item needs
   it, and the current path runs a test suite that writes to production.
4. **P2-2's per-station rollout — which station first?** The plan says WSSS. WSSS
   is also the station carrying the book, which makes it the highest-variance
   place to test a policy change. Worth a sentence either way.
