# NO-side model_prob floor (Veto 0c)

2026-09-09. Status: **plan, not implemented.** Read-only analysis only so far.

## The rule

Refuse an entry when `side == "NO"` and the model's **raw** `model_prob` is below
`NO_SIDE_MIN_MODEL_PROB = 0.65`. Nothing else changes: no sizing change, no sort
change, no YES-side change.

## Why

Full-book decomposition, 715 settled rows, 2026-08-06..09-08, $5,053.75 staked.
Scored held-to-settlement via `cohort_monitor` (which reproduces the published
totals to the cent).

The model overstates its own win rate by **+10.7 points** on the traded sample
(mean `model_prob` 0.445, actual win rate 0.338). The market, on the same trades,
is off by -2.7 points and in our favour. The overstatement concentrates on the NO
side at low confidence, where the model claims ~55% on what is closer to a coin
flip and pays spread and fee to take it.

| book (held) | n | staked | return |
|---|---|---|---|
| baseline | 559 | $3,295 | +7.7%  CI [-6.5%, +22.3%] |
| NO floor at 0.70 | 449 | $2,533 | +17.2%  CI [+1.7%, +33.6%] |
| the trades cut at 0.70 | 110 | $762 | -23.9%  CI [-45.3%, -1.7%] |

Station-day clustered bootstrap on the improvement: **median +9.5 pts, 90% CI
[+4.5, +14.7], P(improves) = 1.00.**

**Threshold is 0.65, not the sweep's peak of 0.70.** Peaks of a swept parameter
move when data arrives. 0.65 still improves both disjoint windows (Aug 6-25:
+17.2% -> +19.3%; Aug 26-Sep 8: +3.8% -> +11.8%) and cuts 74 trades instead of
110. The ~3 points given up buys the robustness. This follows the standard this
project set at `ENTRY_PRICE_BLOCK_BAND`: act only where the ordering holds per
station AND across windows.

Worst single cell, for the mechanism: **NO bought at price 0.25-0.40** (n=37,
$234). Model says 52%, market says 34.5%, truth is **19%**. Held -71.5%, CI
[-91%, -42%], negative in both months. Mean `model_prob` there is 0.522, so the
0.65 floor removes nearly all of it. This is the same failure named in the
spread-floor work: the model underprices the winning bucket and therefore sells
the right bucket as NO.

## Why this is NOT redundant with ADMIT_ON_CALIBRATED_EDGE

`ADMIT_ON_CALIBRATED_EDGE = True` shipped and deployed earlier today (d75b269 /
107e54f, box at 77528a4). It targets the same root cause, so redundancy is the
first thing to rule out. **Measured, not assumed:**

- Of the 74 trades this floor would refuse, **66 (89%) are still admitted** by the
  shipped calibrated gate. They are worth **-28.8% held on $432.39.**
- The correction is far too small here: mean `model_prob` 0.553 maps to **0.524**,
  leaving a calibrated edge of **+0.121** against a `MIN_ABS_RAW_EDGE` bar of 0.03.
- On the pooled map (30 of 35 stations) claims of **0.55, 0.60 and 0.65 all map to
  0.489** -- a flat step. The calibrated probability carries no information across
  exactly the range this gate must discriminate in.

The two rules are complementary and each is a single flag, so either can be
reverted without disturbing the other.

## Which probability the gate reads

**Raw `model_prob`, deliberately.** That is the quantity the measurement above was
made on, and per the previous point the calibrated value is a flat constant across
the decision range, so gating on it would be gating on nothing. This is the
opposite choice from `ADMIT_ON_CALIBRATED_EDGE`, and the reason is that this gate
tests the model's *confidence*, not its *edge*.

## Files touched

1. **`config.py`** -- add `NO_SIDE_MIN_MODEL_PROB = 0.65` next to
   `ENTRY_PRICE_BLOCK_BAND` (~line 2043), with the evidence above in the comment
   and an explicit `None` disables. Add helper `no_side_prob_is_blocked(side,
   model_prob)` so the comparison lives in one place, mirroring
   `entry_price_is_blocked()`.
2. **`entry_manager.py`** -- new **Veto 0c** in `evaluate_entry()`, placed after
   Veto 00b and before Veto 0a (a property of the signal's confidence, so it sits
   with the instrument gates rather than the edge-quality ones). Same `print(...)`
   + `_rejected(...)` shape as its neighbours.
3. **`backtest/entry_sim.py`** -- mirror it in `evaluate_entry_sim()`, update the
   gate list in the module docstring (lines 45-50), and bump `GATE_COUNT`
   **16 -> 17** with a dated comment, as 2026-08-27 did.
4. **`tests/test_parity_entry.py`** -- the existing parity test must keep passing;
   it is what catches the two implementations drifting.

**Fail open on a missing `model_prob`**, matching `entry_price_is_blocked()`'s
documented reasoning. Verified this will not silently no-op: `model_prob` is NULL
on **0 of 254** September rows (it was NULL on 171 of 506 August rows, the
pre-backfill era).

## Tests

New `tests/test_no_side_prob_floor.py`:
- NO at `model_prob` 0.64 -> rejected; 0.65 -> admitted (boundary `>=`, matching
  the band gate's half-open convention).
- YES at 0.20 -> unaffected.
- `model_prob is None` -> admitted, with the fail-open reason asserted.
- `NO_SIDE_MIN_MODEL_PROB = None` -> gate fully inert.
- Parity: `entry_manager` and `entry_sim` return the same decision and the same
  reason string on the same input.

## Blast radius, predicted before shipping

On September entries: **49 of 254 refused (19.3%)**, which is **40% of all NO-side
entries**. It reaches live money -- WSSS is live-armed and all 3 of its NO entries
in the last 7 days sit below 0.65. Verify the real refusal count against 19.3%
after one week; a large miss means the gate is not doing what was measured.

## Falsifier and re-score

Re-score at **~2026-09-23**, two weeks of entries under the new gate. The claim is
that the book's held return rises and that refused candidates would have lost. Log
every refusal with its `model_prob` and price so the counterfactual is scoreable.
**If it has not helped by then, set the constant to `None` rather than tuning the
threshold** -- the discipline `ADMIT_ON_CALIBRATED_EDGE` set for itself.

## Explicitly out of scope

The sort (`ev_engine` stays on `raw_edge`), the net-EV bar basis
(`ENTRY_BAR_BASIS`), sizing (P3-6 is working -- dollar-weighted beats
equal-weighted by 8.6 points over the last 14 days), and the YES side. Bundling
any of them would make this unattributable.

## Known limitation

The threshold's axis was chosen after inspecting the data. The two-window split is
the mitigation and it holds in both, but this is not a true out-of-sample result.
Separately, $3,295 of the $5,054 is paper and live is $82, so this is a paper-book
conclusion applied to a live gate.
