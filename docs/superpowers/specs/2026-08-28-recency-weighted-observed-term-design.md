# Recency-weighted observed term in the central estimate

Status: draft, pending user review
Date: 2026-08-28

## Purpose

`calibration.blend_central_estimate()` reduces the observed side of the
central estimate to `statistics.fmean(o.max_temp_c for o in observations)`
— a plain unweighted mean over every observation `pipeline.gather_observations()`
returns, which is 30 days of climate-monitor seeds plus everything stored
since the 1st of the current month. That term carries **60% of the blend
for WSSS** (`FORECAST_BLEND_WEIGHT_BY_STATION = {"WSSS": 0.40}`), the
station that trades real money.

An unweighted month-to-date mean cannot track a regime change, and on
2026-08-21..27 it did not. Measured on the box for target 2026-08-27:
26 observations, `observed_mean` = **32.538**. WSSS had settled **33.0 on
each of 08-19 through 08-25** — seven consecutive days — but those seven
were diluted by the first half of August's 32s, so the observed term
stayed near 32.5 and pinned the central estimate at ~32.4–32.6. Bucket 32
stayed "most likely" every day of the run.

The book followed the estimate: `32:YES` and `33:NO` on 08-21, 08-22 and
08-25 — six positions betting against 33, all wrong — for **−$5.70 on
$12.61 staked (−45.2%)** across the nine positions in 08-21..27, 8 of 9
losing. The market was on the correct side throughout: on 08-25 it priced
`33:NO` at 0.30 (P(33) = 70%) against the model's 47.6%.

The justification for WSSS's extra observed weight, in
`blend_central_estimate`'s own docstring, is that *"the daily max barely
moves and persistence genuinely is informative."* Persistence is
informative. A month-long unweighted mean is not persistence — it is
climatology wearing persistence's label. This design makes the observed
term mean what that sentence already claims it means.

### Second defect: the window collapses on the 1st of every month

`gather_observations()` is `climate_monitor_client.load_recent_observations(station, days=30)`
plus `storage.load_observations_since(icao, target_date.replace(day=1))`.
Measured on the box:

| as-of | stored since the 1st | blended n | `observed_mean` |
|---|---|---|---|
| 2026-08-27 | 28 | 26 | 32.538 |
| **2026-09-01** | **0** | **0** | **None** |
| **2026-09-02** | **0** | **0** | **None** |

**`load_recent_observations` returns 0 rows for WSSS**, so the seed half
contributes nothing and the whole observed term rests on the
`replace(day=1)` query. That is not a 30-day lookback — it is
*days-since-the-1st*, which is 30 days on the 31st and **zero days on the
1st**.

When `observed_mean` is `None`, `blend_central_estimate` falls through to
`return round(forecast_mean, 1)`. So on 2026-09-01 WSSS's central estimate
silently stops being a 40/60 blend and becomes **100% forecast** — the
term whose sources run −1.24 (ECMWF) and −1.04 (GFS) cold — and then
climbs back over the following week on a sample of 1, 2, 3… readings, each
carrying 60% of the estimate. This repeats every month and it is happening
on a real-money station in four days.

The two defects share one root: **the observed term's sample is chosen by a
calendar accident rather than by a stated lookback.** Fixing only the
weighting would ship a recency-weighted mean over a window that still
empties on the 1st, so both are in scope here.

## Scope

**In scope:**

- `calibration.observed_mean_weighted()`: an exponentially recency-weighted
  mean over dated observations, structurally mirroring the existing
  `calibration.bias_stats_weighted()`.
- `config.OBSERVED_HALF_LIFE_DAYS`, **shipping at the value that reproduces
  today's behaviour exactly** (see "Shipped as a no-op").
- Wiring it into `blend_central_estimate()`, for the live path *and* the
  replay path identically (see "Why this one is not pinned in the
  backtest").
- **`config.OBSERVATION_LOOKBACK_DAYS`, replacing `target_date.replace(day=1)`
  in `pipeline.gather_observations()` with a fixed lookback.** This half is
  a bug fix, not a tuning question, and it is NOT gated on the measurement
  below: a term that empties on the 1st of the month is wrong at every
  half-life including the current one. Ship it first and separately.
  `backtest/engine.py` already uses a fixed `OBSERVATION_WINDOW_DAYS = 30`
  whose comment claims to mirror the live call sites — it does not, and
  this change is what makes that comment true.
- The measurement that chooses the half-life, and the bar it must clear
  before the constant moves.

**Out of scope:**

- Changing `FORECAST_BLEND_WEIGHT_BY_STATION` or
  `FORECAST_BLEND_WEIGHT_DEFAULT`. The weight and the estimator are
  separate questions and moving both at once makes neither measurable.
  See memory `blend-weight-and-spread`.
- Per-source bias correction. WSSS's sources carry large, individually
  well-measured, opposite-signed biases (nea_24hr **+1.533**,
  open_meteo_ecmwf **−1.235**, open_meteo_gfs **−1.039**, n = 26–27, sd
  0.65–0.75) which the single pooled scalar `forecast_bias_stats()`
  returns collapses to **−0.224**. That is a real defect and it is
  measured — bucket hit-rate over 27 days goes raw 13/27 → pooled 15/27 →
  per-source 17/27 — but it is a *second* change to a *different* term,
  and it is the smaller of the two. It gets its own spec.
- `estimate_std_dev` and the spread chain.
- Anything about exits.

## Design

### The estimator

```
observed_mean_weighted(dated_observations, as_of, half_life_days) -> float | None
```

`dated_observations` is `[(target_date, max_temp_c), ...]`; `as_of` is the
date ages are measured from. Weight for a sample `d` days old is
`0.5 ** (d / half_life_days)`; the return is `sum(w*t)/sum(w)`.

Three properties are load-bearing, and all three are inherited deliberately
from `bias_stats_weighted`, whose docstring already argues for them:

1. **Decay, not a rolling window.** A hard window drops samples. Dropping
   observations can push a station under
   `MIN_RESOLUTION_OBS_BEFORE_ENTRY`, so a change meant to make the
   estimate more honest would instead stop stations trading. Decay keeps
   every sample and discounts it.
2. **A separate function, not a flag on the existing path.** The unweighted
   mean stays exactly where it is and keeps meaning what it meant.
3. **`half_life_days = None` means no decay**, returning the unweighted
   mean bit-for-bit — the mechanism by which this ships inert.

Unlike `bias_stats_weighted`, this returns a single float rather than a
`(value, n, stderr)` triple: there is no gate on the observed term's
precision today, and inventing one here would be a second change smuggled
in beside the first. If such a gate is ever wanted, Kish effective-n is the
precedent to copy.

### Shipped as a no-op

`OBSERVED_HALF_LIFE_DAYS = None` on the first commit. `blend_central_estimate`
calls the new function, the new function returns the unweighted mean, and
every stored estimate is unchanged. This is the same pattern as
`LOTTERY_PROFIT_TAKE_PCT` (commit 0661765): ship the knob, measure it,
then decide the value in a separate commit that changes exactly one number
and cites the measurement.

The knob is not the fix. The measured half-life is the fix, and it does not
exist yet.

### Why this one is NOT pinned in the backtest

`backtest/engine.py` pins `forecast_bias_c=0.0` and passes
`allow_measured_spread=False`, because both of those estimators read the
*whole stored record* and would price a simulated Aug-3 tick using Aug-10
information. An AST test fails if the engine drops the spread opt-out.

**This estimator is different and must NOT be pinned.** Its inputs are the
observations the engine already hands it, which `engine._visible_observations()`
has filtered through `backtest/resolution.py`'s `observation_visible()`
(publish lag) and a `day - OBSERVATION_WINDOW_DAYS` cutoff. The weights
depend only on each sample's `target_date` and on `as_of` — both of which
the replay knows at the simulated instant. Nothing from the future enters.
Re-weighting already-visible samples leaks nothing.

**Parity caveat, and why the lookback fix must land first.** Today the
replay applies a fixed 30-day observation cutoff while live applies
`replace(day=1)`, so the two are already computing the observed term over
different samples — most extremely on the 1st, where the replay has 30 days
and live has none. Any half-life scored in the replay before the lookback
fix ships would be scored against a window live does not use. Landing
`OBSERVATION_LOOKBACK_DAYS = 30` first collapses that difference to zero
and is a precondition of the measurement, not merely adjacent to it.

This matters beyond correctness: because there is no leak, the replay can
score candidate half-lives, which is the *only* reason the measurement
below is possible at all. Pinning it "for consistency with bias and
spread" would silently destroy the ability to choose the constant.

A test asserts the engine does **not** opt out — the mirror image of the
existing test that asserts it *does* opt out for spread, and there so that
a future reader pattern-matching on "replays pin the calibration inputs"
cannot quietly break the measurement.

### Blast radius

`FORECAST_BLEND_WEIGHT_BY_STATION` puts WSSS at 0.40 (60% observed) and
every other station at the 0.85 default (15% observed). So the effect is
concentrated almost entirely on WSSS — which is one of the two real-money
stations (`LIVE_TRADING_STATIONS = {"WSSS"}`, plus RCSS in the working
tree). A half-life change is therefore a real-money behaviour change and
takes the measurement bar below, not a judgement call.

## Risks

**Whipsaw is the real one.** A short half-life tracks a regime change fast
in both directions. WSSS settled 31.0 on 08-26 immediately after seven 33s;
under a 2-day half-life that single reading would dominate the observed
term the next morning and could produce the mirror-image error to the one
this design fixes. The sweep must score this rather than assume it away —
it is precisely what a Brier score over the full record captures, and it
is the reason the candidate set below extends to half-lives long enough to
be nearly inert.

**Sharper estimates size up.** Memory `blend-weight-and-spread` records
that tightening the spread produced more and bigger positions (5 entries
vs 3; $20.93/$85.13 vs $1.05/$1.05/$106.51), because a narrower or
better-centred distribution raises `model_prob`, hence `raw_edge`, hence
Kelly size. Any improvement here arrives together with increased exposure.
That is a risk consequence, not a free win, and the measurement must
report position count and staked total alongside Brier.

**Seeds and stored rows are mixed.** `gather_observations` returns
climate-monitor seeds *and* stored rows, deduped by
`config.observation_source_rank`. Both carry a `target_date`, so both
weight correctly — but a seed source with systematically stale dates would
be down-weighted relative to today's behaviour. The implementation must
confirm the dedup happens *before* weighting, so a duplicated day cannot
enter the weighted sum twice.

## Measurement — the bar for moving the constant

The half-life is chosen by replay, never by hand.

- **Metric: multi-class Brier.** Not RMSE. Memory `blend-weight-and-spread`
  records that RMSE overstated the blend-weight gain roughly fourfold
  (−39.2% RMSE against −10.9% Brier); "do not tune this system on RMSE" is
  a standing conclusion from that measurement.
- **Candidates:** `None` (today), 14, 7, 5, 3, 2 days.
- **Procedure:** score the real production chain
  (`blend_central_estimate` → `estimate_std_dev` →
  `probability.bucket_probabilities` → `resolution.bucket_for_temp`) over
  the stored record, bias applied leave-one-out, exactly as the 2026-08-10
  blend-weight measurement did.
- **Report per candidate:** Brier, modal-bucket hit rate, and — for the
  sizing risk above — entries and total staked.
- **Bar:** a paired bootstrap `P(candidate better than None)` ≥ 0.95, *and*
  the ordering holding per-station rather than being carried by WSSS alone,
  *and* the 08-26-style reversal days not being where the gain comes from.
  Below that bar the constant stays `None` and the tool is the deliverable
  — the outcome `backtest/stop_sweep.py` and `backtest/take_sweep.py` both
  reached, and the honest one.

Note the P&L question is separately *not* answerable yet: `verdict()` in
`backtest/compare.py` refuses a comparison below
`MIN_TRADES_FOR_A_VERDICT = 30`. Brier over the stored record is the
measurement this spec commits to; a P&L A/B is a later question with its
own data bar.

## Testing

- **The month boundary.** `gather_observations()` on the 1st of a month
  returns a full lookback rather than an empty list, and
  `blend_central_estimate` still produces a blended estimate rather than
  falling through to forecast-only. Written against the real 2026-09-01
  case, which is the failure this half of the spec exists to prevent.
- **Live/replay parity on the window**, asserting the live lookback and
  `engine.OBSERVATION_WINDOW_DAYS` agree, so the engine comment claiming it
  "mirrors the live call sites" is enforced rather than merely stated.
- `half_life_days=None` reproduces `statistics.fmean` bit-for-bit over a
  randomised sample — the no-op proof this commit rests on.
- A recent regime shift moves the weighted mean materially more than the
  unweighted one, using the real 2026-08-01..27 WSSS series (observed mean
  32.538; the last seven readings 33/33/33/33/33/33/31).
- Weights depend on age only: shifting every date and `as_of` by the same
  offset leaves the result unchanged.
- Degenerate inputs — empty (returns `None`, so the existing
  `observed_mean is None` fallbacks still fire), a single observation, and
  every observation on the same date (equals the unweighted mean at any
  half-life).
- Dedup precedes weighting: a day present in both seeds and stored rows
  contributes once.
- `backtest/engine.py` does **not** pin this estimator (see above).
- The existing forecast-bias and spread tests keep passing unchanged,
  demonstrating the three estimators stayed independent.
