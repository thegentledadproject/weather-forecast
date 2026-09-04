# Remediation plan, revision 4

**Date:** 2026-09-03 (evening)
**Revises:** `2026-09-03-remediation-plan-rev3.md` (revision 3, 30 findings — moved
into this directory 2026-09-03; it was `~/Downloads/polyweather-remediation-plan_2.md`)
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
2. **`P0-0` was a name collision** with an item already deployed and accumulating
   data. Renamed to **P0-5** the same day; see §2.
3. **The `$1,038.82` headline figure looked unreconcilable and is in fact correct** —
   the baseline it reconciles against was missing from `config.py`. Investigated and
   fixed the same day; one $43.74 residual survives and is now named. See §3, which
   also records how the original review got this wrong.
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

## 2. `P0-0` was a name collision — RESOLVED

Revision 3 introduced **P0-0 · Standing hold-vs-actual cohort monitor** as its
headline measurement.

There is already a **P0-0 · Retain dated EV snapshots** — defined in this repo's
committed spec (`2026-09-03-remediation-plan-revised.md` §3), deployed 13:40:58 UTC,
and accumulating rows in the `ev_snapshots` table since. Two different items, the same
identifier, one of them live.

**Fixed 2026-09-03.** Revision 3's cohort monitor is renamed **P0-5** throughout
(`2026-09-03-remediation-plan-rev3.md`, 19 references), with a note at the head of that
document recording the old identifier so anyone holding an earlier copy can map it.
P0-5 was chosen because the number was free and the item is a Phase 0 measurement —
the ID carries no priority, and P0-5 remains the "if you do only one measurement"
item despite sorting last.

The rest of this document calls it **the cohort monitor**, which is unambiguous
regardless of numbering.

---

## 3. The `$1,038.82` figure — RESOLVED, and mostly not a defect

> **CORRECTION (same day, after investigating).** This section originally claimed the
> figure "does not reconcile" and recommended deciding which of three candidate values
> was correct. **That was wrong.** `$1,038.82` is correct. The original finding is left
> visible below rather than deleted, because the way it was wrong is instructive: three
> numbers were reconciled against each other without checking whether a fourth existed
> elsewhere in the repo. It did — in the test file.

**What the original review got wrong.** It derived three values for "what the two
rules cost" — $1,038.82 stated, $1,082.57 as the sum of components, $1,060.48 from the
four-row table — and concluded they contradicted. The reconciliation it never tried
was against a baseline that does not appear in `config.py` at all.

**The actual position:**

- Held to settlement is **+$743.68 (+18.4%)**. That figure lived *only* in
  `tests/test_hold_to_settlement_modes.py`, never in `config.py`.
- `held (+$743.68) − as traded (−$295.15)` = **$1,038.83**, which is the stated
  $1,038.82 to a cent of rounding. **The total was right all along.**
- The four-row table's **"neither" (+$765.33, +18.9%)** is a *different quantity* from
  held to settlement, by **$21.65**. Each is internally consistent with its own
  percentage against the $4,049.93 staked, so this is two measurements, not a typo.
  Reconciling the stated total against "neither" instead is what produces the spurious
  $1,060.48 and the appearance of a contradiction.

**One residual is genuinely unexplained, and survives the correction.** The two
per-rule costs sum to `$600.61 + $481.96 = $1,082.57`, which exceeds the held-based
total by **$43.74**. If every position had exited by a stop, by a take, or by a
resolution close worth exactly its held value, those two figures would be equal. They
are not — so resolution-closed rows must differ from clean settlement value in
aggregate by about −$43.74. Exit fees and closing at the book quote rather than the
settlement reading (**precisely the defect P1-7 addresses**) would both push that way.
That is a hypothesis, not a measurement.

**Fixed in `config.py`, no numbers changed.** A reconciliation note now sits directly
under the measurement block: it states that held and "neither" are different
quantities, gives the held figure that was missing, shows the total reconciling
against it, and names the $43.74 residual as unexplained rather than arguing it away.

**Still true, and still the actionable part:** the cohort monitor must reproduce
$743.68 / $765.33 / −$295.15 *and* account for the $43.74. Its acceptance test demands
"to the cent"; the residual is the part that will resist, and it is now written down
before the module exists rather than discovered during.

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

NOW  P0-5 cohort monitor  ← blocks P2-2 and P3-6 both
     └── §3 resolved: reproduce 743.68 / 765.33 / -295.15 AND the $43.74
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

**§3 is no longer a blocker** — the target is now stated precisely enough to build
against: reproduce $743.68 (held), $765.33 ("neither") and −$295.15 (as traded), and
account for the $43.74 residual between the per-rule sum and the held-based total.
The residual is the part that will resist, and it is the reason the monitor is worth
building rather than a formality.

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

---

## 9. Addendum, 2026-09-04 — the cohort monitor is built, and it found the residual

Executes §7's "if you do only one thing". New module `cohort_monitor.py`, wired into
`calibration_panel.py` as a book-wide card, kill criterion pre-committed in `config.py`,
`tests/test_cohort_monitor.py` (35 tests) plus 7 added to `tests/test_calibration_panel.py`.
Full suite green.

### 9.1 Acceptance met exactly

Scored against the deployed book over `2026-08-03..09-01`, read-only:

| | published | measured | delta |
|---|---|---|---|
| as traded | −295.15 | −295.15 | 0.00 |
| stop only | +186.81 | +186.81 | 0.00 |
| take only | +283.37 | +283.37 | 0.00 |
| neither | +765.33 | +765.33 | 0.00 |
| held | +743.68 | +743.68 | 0.00 |
| staked | 4,049.93 | 4,049.93 | 0.00 |

514 rows over 252 station-days — both counts also exact. The 0.306 / 0.344 price-edge
pair reproduces on the 358-row `model_prob` subset.

### 9.2 The $43.74 residual is fully accounted for, and it was two things

§3 left this as the one unexplained figure and offered exit fees plus book-quote closes
as a hypothesis. **Neither is the answer, and the hypothesis has the sign backwards.**

The decomposition that closes exactly, by construction:

```
held − as_traded  =  stop cost + take cost + resolution-close gap
```

Measured cost against holding, per exact status (positive = the rule lost money):

| status | rows | cost |
|---|---|---|
| `closed_stop_loss` | 222 | **+600.61** |
| `closed_take_profit` | 197 | +481.96 |
| `closed_trailing_stop` | 15 | **−22.09** |
| `closed_resolution` | 80 | **−21.65** |

- **−$21.65** is the resolution-close gap. Resolution closes booked *above* clean
  settlement value — the **opposite** direction to the exit-fee hypothesis, which
  §3 said "would push that way". The table contradicted it all along.
- **+$22.09** is the trailing stop. `$600.61` is correct and is the **fixed stop alone**;
  the table's "take only" column re-valued the 15 trailing rows too, and the per-rule
  figure did not. Two correct numbers over two different row sets — which is why
  re-reconciling the four totals could never have closed it.

**The finding that generalises:** the two stop rules point in opposite directions. The
fixed stop cost $600.61; the trailing stop *earned* $22.09 on its 15 rows. Any figure
that says "the stop" is averaging a sign change, which is why the monitor reports
`by_status` and not only the three-way class split.

### 9.3 The kill criterion, and a blind spot it has for about two more days

`config.COHORT_KILL_NET_PRICE_EDGE = 0.0` on the **net** price edge (realised win rate
minus mean entry price, less the entry-side taker fee — a held position pays no exit
fee, since redeeming is not a trade), read on `COHORT_KILL_WINDOW_DAYS = 30` with
`COHORT_KILL_MIN_STATION_DAYS = 30`. Zero is the level because below it the book pays
Polymarket for being right about the weather. **No action is encoded** — Phase 0 is
measurement only, and open question 6 below is what firing means.

Current reading (as of 2026-09-04, 572 rows / 284 station-days):

| window | held | as traded | net price edge | 95% CI (station-day clustered) |
|---|---|---|---|---|
| all time | +15.1% | −7.9% | **+0.0335** | [−0.0015, +0.0709] |
| trailing 14d | −0.4% | −12.4% | **−0.0049** | [−0.0536, +0.0433] |
| trailing 30d | +15.1% | −7.9% | +0.0335 | [−0.0015, +0.0709] |

Criterion: **holding** (+0.0335 vs 0.0).

**Read that with the blind spot in mind.** The whole closed book runs 2026-08-06..09-03
— 29 days — so trailing-30 and trailing-60 are *the same rows as all-time* and will be
until ~2026-10-03. The criterion is therefore currently reading the full history under a
30-day label. The only window that can discriminate today is the 14-day one, and it is
at **−0.0049** on 185 station-days: below the level the criterion is set to.

That is not yet a firing — the 14-day CI spans zero, so the honest statement is **"no
measurable price edge in the last fortnight"**, not "the edge is gone". But it is the
same direction as the independently-recorded decay of the hold edge, and it means the
30-day criterion will not be able to see this until roughly 2026-10-03. Whether to key
the criterion to 14 days is a live question and deliberately not decided here: 14 days
crosses zero on noise, and a threshold that trips on noise gets ignored.

### 9.4 What this unblocks and what it does not

- **P3-6** and **P2-2** were both gated on this module. Both are now unblocked on that
  count.
- **§4's gating gap is still open** — the P2-2-prereq-B-before-P3-6 question is
  unchanged by this work and still needs the explicit decision §4 asks for.
- **§5's P1-1** promotion stands; nothing here touches it.
- `risk_manager.py:72-77`'s open question (the 10:00 tightening against an 08:00 entry
  close) asked for "the freed capital modelled". §6 of rev 3 said P0-5 supplies that
  basis. It supplies the *scoring* basis; it does not model freed capital, because
  nothing here reads position concurrency. That item is not unblocked.

### 9.5 One more open question for the operator

6. **What does firing mean?** Recorded in `config.COHORT_KILL_*` as deliberately not
   encoded. The three candidates — halt the station, halt the book, drop to paper —
   differ in what they cost if the firing is a false alarm, which makes it a call for
   whoever is carrying the money. Worth deciding *before* ~2026-10-03, when the 30-day
   window starts being able to fire.

---

## 10. Addendum, 2026-09-04 — Phase 1's remaining NOW-row items

Branch `fix/remediation-wave-2`, six commits, **not merged and not deployed**.
1535 tests green. Every premise was re-verified against the deployed tree first,
because §1 had already found seven plan items shipped — and doing so changed two
of the six.

| Item | Premise held? | What shipped |
|---|---|---|
| **P1-1** day budget at resolved size | yes | `executor._day_budget_breach()`, called before the mode branch |
| **P1-2** limit pad in EV | yes | `OrderSpec.pad_cost_pct`, charged in the at-size re-derivation |
| **P1-6** live-auth legibility | **mostly already shipped** | only `--require-live` was missing |
| **P1-7** settlement before book | yes | `position_manager._close_resolved_market()` |
| **P1-8(a)** exit fee in EV | yes | `ev_engine.expected_exit_fee_pct_of_notional()` |
| **P3-4** low-confidence gate | yes, and **strengthened** by the 08-29 reorder | `"ensemble"` added to the set |

**P1-8(b)** (the `entry_fee_per_share` migration) is deliberately excluded — the
plan calls for a separate PR, and it changes the meaning of stored rows.

### 10.1 Two items were not what the plan said

**P1-6 was four-fifths done already.** `executor.warn_about_unmanageable_live_
positions()` already prints the `[ACTION NEEDED]` block at boot, before the first
cycle, naming each position and the dollar total, and never auto-promotes. Only
the `--require-live` refusal was missing. The existing behaviour had no test at
all; it does now.

**P3-4 got stronger, not weaker, from a change made after the plan was written.**
Before the 2026-08-29 tier reordering the ensemble sat at the *top* of the chain
and fired for every station, so adding it to the set would have doubled the edge
bar for the whole book — and an existing test pinned it *out* for exactly that
reason. After the reorder it fires only where the measured tier could not, which
is precisely the "no spread measured for this station" population the gate is
about. The old pin was right when written and is now obsolete; it was updated in
place with that history rather than silently flipped.

### 10.2 What the measurements said

- **P1-1's 55% (§5) is confirmed as the reason it moved up.** The unchecked path
  is the common path, not an edge case.
- **P1-7's size is now known, and it is not a windfall.** The cohort monitor
  measured 80 resolution-closed rows booking **$21.65 more** than clean
  settlement value over 2026-08-03..09-01. The old behaviour *flattered* the
  book, so this makes the record correct rather than more profitable.
  `cohort_monitor`'s `other_gap` should trend toward zero afterwards — that is
  the verification hook.
- **P3-4's blast radius today is one collection-only station.** 34 of 35 stations
  resolve to `measured_error`; only OPKC does not, and it is not
  live-allowlisted. It also removes a real inconsistency: OPKC's confidence class
  flipped on whether an ensemble *fetch succeeded* — `"ensemble"` (normal bar)
  when it worked, `"pooled_error"` (doubled bar) when it did not.

### 10.3 What changes live trading behaviour

Unlike P0-5, which was measurement-only, **four of these six change what the
daemon does**, all in the conservative direction:

- **P1-1** refuses entries that would breach a day budget at the resolved size.
  On every order path, including simulation.
- **P1-2** and **P1-8(a)** both *lower* the net-EV number the entry gate tests
  against an unchanged bar, so fewer entries clear it. P1-8(a) affects live and
  simulation only; the paper book holds to settlement and correctly pays one fee.
- **P1-7** changes which source decides a resolved position's payout.
- **P3-4** doubles the edge bar for one collection-only station.

P1-6 changes nothing unless `--require-live` is added to the systemd unit, which
this does not do.

### 10.4 Sequencing after this wave

```
DONE   P0-0(retention) P0-4 P1-3 P1-4 P1-5 P1-9   deployed 09-03
       P2-1                                        deployed 09-03
       P0-5 cohort monitor                         deployed 09-04
       P1-1 P1-2 P1-6 P1-7 P1-8a P3-4              built 09-04, NOT deployed

NOW    P1-8b  ← the storage migration, its own PR by design

NEXT   P3-6   ← unblocked: the cohort monitor exists
       P2-2   ← unblocked on P0-5; still needs prereqs A/B/C
       └── §4's B-before-P3-6 question is STILL UNDECIDED
       P1-10  P1-11

DEFER  P0-1 ~2026-10-03   P0-2 ~November   P0-3 needs prod DB access
       P3-1 P3-2 P3-3 P3-5
```

§4's gating gap and §9.5's "what does firing mean" are both still open, and
neither is affected by this wave.
