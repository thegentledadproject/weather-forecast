# Remediation plan, revision 4

**Date:** 2026-09-03 (evening)
**Revises:** `polyweather-remediation-plan_2.md` (revision 3, 30 findings)
**Also supersedes:** `2026-09-03-remediation-plan-revised.md` (this session's earlier revision of revision 1)
**Verified against:** `thegentledadproject/weather-forecast@main` at `584c2c9`
**Method:** every new factual claim in revision 3 checked by content search, not by
cited line number; internal arithmetic recomputed; execution status checked against
the deployed box.

---

## 0. What revision 4 changes

**Revision 3's facts hold up.** Nearly every claim it makes traces to real text in
`config.py` or `risk_manager.py`, verified by searching for the content rather than
trusting the line numbers. Its central reframing — the model is a direction
indicator, not a probability estimator; the P&L is a price edge; no calibration
metric would notice that edge decaying — is supported by the repo's own measurements
and is the most valuable thing in the document. §6 below lists what checked out.

**Four things need correcting, and none of them is an analytical error:**

1. **Revision 3 is stale on execution status.** Seven of its items shipped and
   deployed on 2026-09-03, including the one it names as "if you do only one thing."
2. **`P0-0` is a name collision** with an item that is already deployed and
   accumulating data.
3. **The `$1,038.82` headline figure does not reconcile** with its own components —
   and the defect is in `config.py`, which revision 3 faithfully copied.
4. **P3-6 and P2-2's prerequisite B have a gating gap** that the removal of P2-1's
   blocker just opened.

Plus one reprioritisation on new evidence (§5).

---

## 1. Execution status — seven items are done

Revision 3 was written against a tree that predates the day's work. Deployed state:

| Item | Revision 3 says | Actual |
|---|---|---|
| P1-3, P1-4, P1-5, P1-9 | pending | merged `754888f`, deployed **13:40:58 UTC** |
| P0-4 | pending | same |
| **P2-1** | *"Research first… Do not write a plausible-looking call that has never been executed."* | **built, merged `584c2c9`, deployed ~15:27 UTC** |

Revision 3 still frames P2-1 as gated on research. That research was completed
2026-09-01 (`e840dad`, a 296-line approved design recording on-chain facts from a
read-only probe) and the implementation shipped today.

### 1.1 P2-1 as built differs from revision 3's spec in three deliberate ways

All three follow the approved design doc
(`docs/superpowers/specs/2026-09-01-redemption-design.md`), which post-dates the plan
bullet and is more specific. Recorded here so the divergence is not mistaken for
drift:

| Revision 3 says | As built | Why |
|---|---|---|
| "Gated behind its own environment flag, independent of `POLYMARKET_LIVE_TRADING`" | Uses **the same** `POLYMARKET_LIVE_TRADING` flag | The design doc's §6 is explicit: "Two gates, as with live orders… `POLYMARKET_LIVE_TRADING=true` is the second." The two documents directly contradict each other here. **This one is still worth an operator decision** — it is the only divergence that changes the safety surface. |
| "Scheduler calls it once per day in a `closed` or `collection` window" | Never called by the scheduler or daemon at all | The design's header is a scope decision: "operator-run script. The trading daemon never redeems." Verified: no module in the daemon's import graph references the redemption code. |
| "new module `redemption.py`, single public function `redeem_settled(dry_run=True)`" | `redeem.py` (CLI) + `clients/onchain_client.py` + `clients/redemption_client.py` | The design's §4 specifies exactly this three-component split, separating chain plumbing from eligibility policy from operator interface. |

### 1.2 Revision 3's redemption state figure is out of date

It says "what is redeemable today is three positions, all losers at `curPrice: 0`."
Checked against the real wallet on 2026-09-03 using the newly-built chain reads:
**5 redeemable, all losers, $0 total**, plus **5 already cleared** (zero on-chain
balance — two of them former winners collected by hand outside this system).

The conclusion is unchanged and worth restating in revision 3's own words: the value
of redemption is future winners and the permanent removal of the halt mechanism, not
today's money.

**A finding that generalises beyond redemption:** a database row reading
`closed_resolution` / `exit_price 1.0` is **not** evidence anything is still owed.
Two positions looked like uncollected winners from the database alone and were
already collected. Only the on-chain balance distinguishes them. Any future item that
reasons about "uncollected" value from stored rows needs the same cross-check.

---

## 2. `P0-0` is a name collision

Revision 3 introduces **P0-0 · Standing hold-vs-actual cohort monitor** as its
headline measurement.

There is already a **P0-0 · Retain dated EV snapshots** — defined in this repo's
committed spec (`2026-09-03-remediation-plan-revised.md` §3), deployed 13:40:58 UTC,
and currently accumulating rows in the `ev_snapshots` table.

Two different items, the same identifier, one of them live. Rename revision 3's
cohort monitor before the document circulates further. Suggested: **P0-5**, since it
is a Phase 0 measurement and the number is free.

The rest of this document calls it **the cohort monitor** to avoid compounding the
problem.

---

## 3. The `$1,038.82` figure does not reconcile — and the defect is upstream

Revision 3's headline for P2-1 reads "It unblocks the $1,038.82-per-month policy
change." That number is copied faithfully from `config.py:1522`. The problem is in
`config.py`, not in the plan.

`config.py:1506-1528` records four totals and two per-rule costs. They produce three
different values for the same quantity:

| Value | Source |
|---|---|
| **$1,038.82** | stated directly: "The two rules cost $1,038.82 between them" |
| **$1,082.57** | sum of its own two components ($600.61 + $481.96) |
| **$1,060.48** | derived from its own four totals (`neither − as traded`) |

A ~$44 spread, about 4%.

**The interaction explanation does not cover it.** The natural defence is that the
counterfactuals interact — removing the take changes which positions the stop
catches, so per-rule costs need not sum. But under the consistent reading (each
rule's cost = what removing just that rule from as-traded gains):

- take cost = `stop_only − as_traded` = **$481.96** — matches the stated figure exactly
- stop cost = `take_only − as_traded` = **$578.52** — the stated figure is **$600.61**

The take reconciles to the cent and the stop does not. That points at two rules
costed by different methods, not at a legitimate non-additivity.

**Why this matters concretely, and it is not pedantry:** the cohort monitor's own
acceptance test demands reproducing the four totals *"to the cent"* and states *"if it
does not, the discrepancy is the finding and must be resolved before the module is
trusted."* Whoever builds it will hit this immediately, with three candidate figures
and no way to tell which is authoritative.

**Recommended:** resolve this before building the cohort monitor, not during. Decide
which of the three is correct, correct `config.py`, and make the monitor reproduce
the corrected figure. The plan's headline number for its highest-priority item should
not be one that fails its own acceptance criterion.

---

## 4. P3-6 and prerequisite B have a gating gap that just opened

Revision 3 gates them separately:

- **P2-2** (with prerequisite B: "decide `SIZE_STOPLESS_BOOKS_ON_PURE_KELLY`
  explicitly") is gated on **P2-1**.
- **P3-6** (calibrated-probability sizing) is gated on **the cohort monitor**.

P3-6 then says its approach "is what prerequisite B is really asking, and this is the
version that makes the answer principled rather than a judgement call."

**P2-1 is now done.** So P2-2 is unblocked and the cohort monitor is not built — which
means prerequisite B can now be reached *before* P3-6 exists, forcing exactly the
unprincipled judgement call P3-6 was written to replace. Revision 3's own sequencing
diagram does not show this, because when it was written P2-1 was still blocking.

**Resolve one of two ways, explicitly:**

- gate prerequisite B on P3-6 (and therefore on the cohort monitor), or
- state that answering B early is acceptable, on the grounds that it is one config
  constant and reversible — but say so, rather than letting the ordering decide it.

---

## 5. P1-1 is under-prioritised — new measurement

Revision 3 restates P1-1 unchanged. This session's earlier revision added a "count
first" instruction, on the precedent of the `f383393` resize check that found 0 of 83
real events affected. That count has now been done, and it points the other way.

Over **69 recorded live entry attempts** on the box:

- **38 (55%)** resolved more than 5% above the $1.00 fixed size
- **maximum resolved notional $3.60** against a $1.00 request

The exchange-minimum upsizing P1-1 guards against is **routine, not exceptional**.
Roughly one entry in two is submitted at a notional the day-budget check never saw.
That is a materially stronger case than "a defect on its own terms" and argues for
moving P1-1 up rather than leaving it mid-list.

Note this does not by itself prove a budget was ever breached — that needs the
remaining-budget context at each historical decision, which is not stored. What it
establishes is that the precondition is common, so the unchecked path is exercised
constantly rather than rarely.

---

## 6. What verified exactly

Checked by content search against the deployed tree. All confirmed:

- 358 traded rows; Brier **0.1930** model vs **0.1842** entry ask; mean `model_prob`
  **0.432** against **0.344** realised — ~9 points overconfidence (`config.py:1530-1538`)
- Peak concurrent notional **$432.95** as traded vs **$748.18** held; `BANKROLL_USD`
  $1000; `KELLY_FRACTION` 0.25
- `SIZE_STOPLESS_BOOKS_ON_PURE_KELLY = False`; `MAX_POSITION_USD_EXPENSIVE = 30.0`
  above `EXPENSIVE_ENTRY_PRICE = 0.55`
- Veto 0b / veto 0c naming, and the argument that 0c never fires on a stopless book
- The stop-sweep fill-assumption trap: **+$146** at trigger-fill vs **−$75** at
  provable-quote-fill, same cell
- WMKK 2026-08-07 b35 NO: trigger **0.675**, fill **0.060**; ZGGG 35C YES flat at
  **0.260** for seven reads then **0.110** through a **0.203** trigger
- Cadence 15/15/30; take-profit **21% → 44%**; resolution **21% → 0%**
- The 10:00 tightening vs the 08:00 entry close, flagged as an open question in
  `risk_manager.py:72-77`
- Snapshot coverage: median **25%** of hold window, **365 of 514** under half
- Trailing stop removal: **907** evaluations, armed **7 of 580** non-lottery ticks,
  zero exits across four configurations
- P3-6's full Kelly chain: 3.8 points → **5.5%** → quarter **1.37%** → **$13.69** on
  $1,000, against an actual mean position of **$7.88** ($4,049.93 / 514). The stated
  "about 57%" is 58% on exact arithmetic; immaterial.

**One correction to this reviewer, recorded because the method matters.** The
prerequisite-B multipliers (1.4× at 0.50, 2.0× at 0.20, 2.25× at 0.16) first measured
as 1.27× / 1.67× / 1.83× and were nearly flagged as wrong. `config.py:2194` specifies
"measured at the median 0.020 spread"; the first check omitted the spread. Re-run at
the stated spread they reproduce **exactly**. The claim is correct.

**One claim correctly stated as a gap rather than a fact:** P1-10 requires
`stop_sweep.py`'s output to carry an explicit fill-assumption line. It does not — the
string "fill" does not appear in the file. That is the item identifying real work,
not an error.

---

## 7. Revised sequencing

```
DONE (deployed 2026-09-03)
    P0-0(retention)  P0-4  P1-3  P1-4  P1-5  P1-9        13:40:58 UTC
    P2-1                                                  ~15:27 UTC

NOW  cohort monitor (rename from P0-0)  ← blocks P2-2 and P3-6 both
     └── resolve the $1,038.82 reconciliation FIRST (§3)
     P1-1  ← promoted on the 55% measurement (§5)
     P1-2  P1-6  P1-7  P1-8a  P1-8b  P3-4

NEXT P3-6            ← gated on the cohort monitor
     P2-2            ← gated on cohort monitor + prereqs A/B/C
     └── decide the B-before-P3-6 question first (§4)
     P1-10  P1-11

DEFER P0-1  ~2026-10-03 earliest (ev_snapshots began 13:40:58 today)
      P0-2  ~November (ensemble history)
      P0-3  needs prod DB access
      P3-1  P3-2  P3-3  P3-5
```

**If you do only one thing:** no longer P2-1 — it is done. The cohort monitor, because
it now blocks both P2-2 and P3-6, and because the price edge it watches is the thing
that can decay silently.

**Before that:** resolve §3. It is an hour of arithmetic that stops the monitor being
built against a target that cannot be hit.

---

## 8. Open questions for the operator

Carried forward, with status:

1. ~~Fund the EOA with POL~~ — **decided 2026-09-03: not now.** `redeem.py --execute`
   refuses cleanly on a zero gas balance and names the affected stations. Current
   redemption value is $0, so nothing is lost by parking it.
2. **Does the daemon ever redeem?** Still open, and now concrete rather than
   hypothetical: the design says no and the code as built says no; revision 3 still
   says once daily. If the daemon should redeem, that is a change to shipped
   behaviour, not a gap to fill.
3. **Does redemption share the trading gate?** New, and the one P2-1 divergence worth
   a decision (§1.1). As built it requires `POLYMARKET_LIVE_TRADING=true` — meaning
   arming redemption also requires the trading gate on. Revision 3 wants them
   independent.
4. **Read-only measurement access to the box.** Informally answered — read-only SSH
   queries worked throughout this session — but nothing packaged.
5. **P2-2's rollout station.** Unchanged: the plan says WSSS, which is also the
   station carrying the book and therefore the highest-variance place to test a
   policy change.
