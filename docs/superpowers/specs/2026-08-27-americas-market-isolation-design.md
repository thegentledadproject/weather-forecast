# Americas market isolation framework

Status: draft, pending user review
Date: 2026-08-27

## Purpose

Add American "highest temperature" stations to the trading system as a third
region, isolated from Asia and Europe on capital, real-money blast radius,
and the shared statistical estimators -- the same three axes the Europe
expansion isolated (`docs/superpowers/specs/2026-08-24-europe-market-isolation-design.md`).

That part is a genuine clone. The region mechanism built for Europe is not
Europe-specific: `StationConfig.region`, the five `REGION_*` dicts, the
region-filtered `_live_budget_breach()`, `pooled_error_spread(region=...)`,
`stations_in_region()`, and one dashboard page per region via
`generate_dashboard.py --region` all take a new region name and nothing else.
US daylight saving comes free, because `iana_timezone` already exists.

What is NOT a clone, and is the reason this spec is longer than a registry
patch: **eleven of the fifteen American cities list their markets in
Fahrenheit, in two-degree-wide buckets.**

```
Highest temperature in NYC on August 27
  69°F or below | 70-71°F | 72-73°F | ... | 86-87°F | 88°F or higher
```

Europe was a pure isolation problem precisely because its markets are
Celsius whole-degree -- structurally identical to Asia. The Americas are the
first cohort whose bucket axis differs from the one this codebase was built
around, and the assumption is currently spelled into an identifier:
`bucket_c`, an int, at roughly 197 non-test call sites and in SQLite columns.

This is the same category of latent assumption as
`StationConfig.utc_offset_hours` being a static int -- "an assumption that
happens to hold for every Asian market listed so far, not a general truth"
(`models.py`) -- and it is repaired the same way: leave the field that
carries the market's own datum alone, add a per-station descriptor that
carries the general truth, and route every semantic use through it.

## Scope

**In scope:**
- A per-station bucket AXIS: `bucket_unit` and `bucket_step` on
  `StationConfig`, and a new `bucket_axis.py` that owns every conversion
  between a bucket key and a temperature.
- The six functions that today interpret a bucket key as a temperature,
  made axis-aware without changing behaviour for any existing station.
- Unit- and width-aware market discovery: label parsing, the plausibility
  band, and the contiguity check in `derive_bucket_bounds`.
- The Celsius -> Fahrenheit settlement chain, and the rounding rule that
  keeps it from landing a settled day in the wrong bucket.
- `region="americas"` in all five `REGION_*` dicts, live blast radius at
  `0/0.0/0`, paper pool funded -- Europe's exact pattern.
- Registry entries for the 15 American cities, all collection-only.
- A per-region `SPREAD_CEILING_C`, because the single shared value is
  tuned on tropical stations and clamps in the direction config.py's own
  comment identifies as dangerous.
- Six defects the cohort exposes that fire before any of these stations
  could trade (section 10).

**Out of scope (deferred, not forgotten):**
- Renaming `bucket_c`. Section 5 argues this at length; the short version
  is that the rename is a larger correctness risk than the trap it removes,
  and it would have to be performed against a live book and frozen
  dashboard copies the deploy does not update.
- Funding the Americas paper or live pools above their initial values.
  Raising either is a one-line, auditable operator decision, exactly as
  Europe's is.
- A second wallet or second process. Same reasoning as the Europe spec.
- Live regrouping of scheduler timezone groups across a DST transition.
  Inherited limitation, and it now bites slightly harder -- see section 11.
- Backtest replay of Americas history. There is none yet. The backtest
  path is made axis-correct so it does not silently mis-settle, but no
  Americas backtest is run as part of this work.
- A twelve-entry monthly `long_term_normal_max_c` map for the Southern
  hemisphere. See section 10(f) for what is done instead.

## Design

### 1. What clones, and what does not

Clean clone, no new mechanism:

| Mechanism | Change |
|---|---|
| `StationConfig.region` | 15 entries say `"americas"` |
| Five `REGION_*` dicts (`config.py:1632-1642`, `config.py:2275-2290`) | one new key each |
| `region_bankroll_usd()` / `region_max_daily_exposure_usd()` | none -- they read the dicts |
| `_live_budget_breach()` region filter | none |
| `pooled_error_spread(region=...)` | none |
| `stations_in_region()` | none |
| `generate_dashboard.py --region` | one new page, `americas.html` |
| DST | none -- `iana_timezone` already does this |

Genuinely new: the bucket axis, sections 2-8.

### 2. `StationConfig` additions (`models.py`)

```python
bucket_unit: str = "C"    # "C" | "F" -- unit of the MARKET's bucket LABELS,
                          # and therefore of bucket_min_c/bucket_max_c and
                          # every bucket_c key. NOT the unit of any
                          # temperature: forecasts, std_dev, observations,
                          # midpoints and bias stay Celsius everywhere.
bucket_step: int = 1      # width of one listed bucket, in bucket_unit degrees.
```

That is the complete set. Defaults `("C", 1)` reproduce all 20 existing
stations exactly -- zero registry edits, the same property that made
`region: str = "asia"` free.

Deliberately NOT added:

- **No `settlement_unit`.** `clients/metar_client.py` yields Celsius for
  every station on earth; the Fahrenheit the market reads is DERIVED from
  it. A second field would be a second source of truth for one fact.
- **No `bucket_min_f`/`bucket_max_f`.** The existing `bucket_min_c` /
  `bucket_max_c` are reused, now interpreted in `bucket_unit`. Their `_c`
  suffix becomes historical; section 5 says what is owed in exchange.
- **No per-station plausibility band.** Derived from `bucket_unit` inside
  `market_discovery` (section 6).
- **`bucket_edge_mode` stays where it is** and is read INTO the axis by
  `for_station()`. It is arguably an axis property misfiled as a station
  property, but moving it would edit VHHH's registry entry for no
  behavioural gain.
- **`EXPECTED_BUCKET_COUNT = 11` stays 11 and stays global.** It counts
  outcomes, not degrees, and the American events are also 11 outcomes
  (1 + 9 + 1). Its comment must change: "range(min, max+1) must yield
  exactly this many values" is a step-1 statement and becomes false.

### 3. The key convention

**A bucket key is the bucket's LOWER EDGE, in its own unit.** The bottom
catch-all keys to `printed_top + 1 - step`; the top catch-all keys to its
printed number.

Verified against today's markets, where it must be a no-op:

| Label | step | key | matches today? |
|---|---|---|---|
| `27°C or below` | 1 | `27 + 1 - 1` = 27 | yes |
| `28°C` | 1 | 28 | yes |
| `37°C or higher` | 1 | 37 | yes |

And on the American grid: `69°F or below` -> 68, `70-71°F` -> 70, ...,
`88°F or higher` -> 88. Keys `68,70,...,88`: eleven keys, uniform step 2,
`bucket_min_c=68`, `bucket_max_c=88`.

Both edge modes then generalise with one parameter, each reducing to
today's formula verbatim at `step=1`:

```
half_up:  bucket b covers axis-unit [b - 0.5, b - 0.5 + step)
floor:    bucket b covers axis-unit [b,       b + step)
```

**The cost of this convention, stated plainly:** key `68` names a bucket
Polymarket prints as `"69°F or below"`. That number appears nowhere on the
market. It is tolerable ONLY because the axis also owns
`label(key) -> str`, and every human-facing site renders through it. That
makes the display change load-bearing rather than cosmetic -- see section 9.

The alternative (key the bottom bucket by its printed 69, giving the
non-uniform grid `69,70,72,...,88`) keeps every key on-label but forces a
special case into every grid check, including the one guarding against
manufactured trades. A uniform grid that needs one rendering helper beats a
ragged grid that needs a special case in a risk control.

### 4. The governing invariant, and the six functions

State this in `bucket_axis.py`'s module docstring, because it is the whole
design in one line:

> **Every temperature in this codebase is Celsius. Only the bucket key and
> its bounds live in the market's unit.**

One conversion boundary, one module. `bucket_axis.py` imports only stdlib
(`math`, `dataclasses`), so it can be imported by `probability.py`,
`backtest/resolution.py` and `bucket_bias.py` without a cycle.

```python
@dataclass(frozen=True)
class BucketAxis:
    unit: str = "C"
    step: int = 1
    edge_mode: str = "half_up"

    def interval_c(self, key: int) -> Tuple[float, float]   # always Celsius
    def key_for_temp_c(self, t_c: float, lo: int, hi: int) -> int
    def keys(self, lo: int, hi: int) -> List[int]
    def width_c(self) -> float
    def label(self, key: int, lo: int, hi: int) -> str

AXIS_C1 = BucketAxis()                    # today's axis; the default everywhere
def for_station(station) -> BucketAxis    # reads bucket_unit, bucket_step, bucket_edge_mode
```

`key_for_temp_c` must SHORT-CIRCUIT the `("C", 1)` case to the existing
literal expressions (`math.floor(t + 0.5)` / `math.floor(t)`) rather than
route it through the general grid formula. The general formula is
algebraically identical for integer bounds, but the short-circuit makes
"byte-for-byte identical for Asia and Europe" a property of the code rather
than of an algebra argument someone has to re-derive during a review.

Six functions gain an optional `axis`, each defaulting to `AXIS_C1`:

| File · function | Change |
|---|---|
| `probability._bucket_interval(bucket_c, edge_mode)` | -> `(bucket, axis)`; private; returns **Celsius** |
| `probability.bucket_probabilities(...)` | `*, axis=None`; loop becomes `range(lo, hi+1, axis.step)` |
| `backtest/resolution.bucket_for_temp(t, ...)` | `*, axis=None`; `t` stays **Celsius**, conversion is internal |
| `market_discovery.parse_bucket_label(...)` | `*, axis=AXIS_C1`; the `"C"` branch is the existing code untouched |
| `market_discovery.derive_bucket_bounds(token_map)` | `step: int = 1` |
| `bucket_bias.bucket_midpoint_c(...)` | `*, axis=None`; return value stays **Celsius** |

Roughly 14 production call sites pass an axis. Nothing in `entry_manager.py`,
`executor.py`, `risk_manager.py`, `backtest/entry_sim.py` or
`backtest/portfolio.py` is touched, so the entry and exit parity harnesses
(`tests/test_parity_entry.py`, `tests/test_parity_exit.py`) are structurally
unaffected.

**`bucket_probabilities` must FAIL CLOSED, not default.** See section 12 for
why this is the highest-risk failure in the design. It receives a
`CalibratedEstimate` carrying `station_icao`, so when `axis is None` and the
station is not `("C", 1)`, it raises rather than silently pricing a
Fahrenheit market on a Celsius grid.

### 5. Why `bucket_c` keeps its name

At roughly 190 of its 197 sites, `bucket_c` is an OPAQUE KEY: a `token_map`
dict key, a `position_id` component, the per-bucket cap and cooldown key in
`entry_manager`, a DB join key. Only six functions ever treat it as a
temperature, and section 4 enumerates all six.

Three concrete reasons the rename loses:

1. **The deploy generators are frozen copies.** `git pull` does not update
   `/usr/local/bin` (memory: EC2 deployment). They read `r["bucket_c"]` by
   string key. Renaming the dict key silently breaks the real-money
   dashboard at the next deploy.
2. **The SQLite columns hold the name**, and the test suite already writes
   to the production DB on the box.
3. A rename that stops at Python identifiers and leaves the column, the
   dict key and the `position_id` format alone makes the codebase LOOK
   repaired at 197 sites while the persisted name still says `_c`.

Against that: the trap is real and it is not cosmetic. `bucket_c = 74` on a
Fahrenheit market makes the suffix an outright false statement, and the
falsehood is load-bearing at the highest-stakes point in the system --
`executor.py` prints `[ACTION NEEDED] ... 74°C (YES) -- OPEN ENTRY` for a
human to act on. A human told to buy the wrong contract is the trap
materialising.

**Verdict: keep the name, conditional on four things, all cheap.**

1. Functions whose `_c` suffix is a PROMISE ABOUT A RETURN VALUE honour it
   by converting: `bucket_midpoint_c` and `quantization_stderr_c` return
   Celsius; `bucket_for_temp`'s `t` parameter takes Celsius. After that, no
   Fahrenheit number ever reaches `calibration.py`.
2. `settled_buckets` gains `bucket_unit TEXT NOT NULL DEFAULT 'C'` and
   `bucket_step INTEGER NOT NULL DEFAULT 1`, via the idempotent
   `PRAGMA table_info` / `ALTER TABLE ADD COLUMN` pattern already in
   `storage.py`. Persisted rows become self-describing, so a future
   cross-station query cannot silently mix units. `load_settled_buckets`
   widens its tuple 3 -> 5, touching two unpack sites (`bucket_bias.py`,
   `promotion_dossier.py`).
3. `derived_bias_stats` asserts its result is inside a plausible Celsius
   band, so a regression fails loudly instead of graduating a station
   through the bias gate.
4. The docstring on `StationConfig.bucket_min_c`/`bucket_max_c` states, in
   the `iana_timezone` idiom, that these are in `bucket_unit`, that the
   `_c` suffix is historical, and that **the axis is authoritative for what
   a key means -- never the field name.**

If a rename is ever warranted the safe order is: axis first, then a new
`bucket_key` field written alongside, backfill, switch readers, drop the old
field, rename the column last, after the frozen dashboards are rebuilt. Not
now.

### 6. Discovery: parsing, the plausibility band, contiguity

The Celsius branch of `parse_bucket_label` is the existing code VERBATIM --
same `_BUCKET_NUM_RE`, same take-the-LAST-match rule, same band -- with one
correction that is not about Fahrenheit at all (section 10(b), sign capture).

The Fahrenheit branch is new and is deliberately stricter:

- The regex REQUIRES the unit letter: `r"(-?\d+)\s*°\s*F"`, with
  `r"(-?\d+)\s*-\s*(\d+)\s*°\s*F"` for the interior range form. This
  matters because the Celsius plausibility band's real job is throwing out
  calendar days and years, and **in Fahrenheit it cannot do that job**:
  plausible winter buckets (`"9°F or below"`) overlap the day-of-month
  range 1-31. Requiring the `°F` letter is what replaces the band as the
  guard. The band becomes a sanity floor (roughly `-20..130`), not a
  control.
- An interior label must yield exactly `step` consecutive integers
  (`hi - lo == step - 1`); the key is `lo`. A label yielding a
  non-consecutive pair is REJECTED, not guessed. This is what makes an
  off-grid key structurally unconstructible.
- `"... or below"` -> `n + 1 - step`. `"... or higher"` -> `n`.

`derive_bucket_bounds(token_map, step=1)` replaces its contiguity test with
one comparison:

```python
keys = sorted(token_map)
if len(keys) != config.EXPECTED_BUCKET_COUNT:
    return None
lo, hi = keys[0], keys[-1]
if keys != list(range(lo, hi + step, step)):
    return None
return lo, hi
```

At `step=1` this is provably the same predicate as today's. What it must
still reject, and does:

- **Short map** (discovery parsed 9 of 11) -- the length test, unchanged.
  This is the one that matters: every absent bucket gets `model_prob 0.0`,
  so its NO side shows a phantom ~0.20 raw edge that clears
  `MAX_PLAUSIBLE_RAW_EDGE` and both EV windows.
- **Gap in the middle** -- range equality.
- **Step-1 map at a step-2 station** -- `hi - lo = 10 != 20`. This is
  exactly what today's regex produces on a Fahrenheit event (`"70-71°F"`
  yields only 71, because `70` carries no degree sign), so it is the case
  most likely to actually occur.
- **Off-grid / wrong-parity key** -- caught by range equality, but the real
  guard is the parser's consecutive-pair rule above, because a uniformly
  shifted odd grid `69,71,...,89` has both the right count and the right
  span.

Nothing it rejects today stops being rejected.

### 7. Settlement: Celsius observation, Fahrenheit market

American markets resolve on NOAA's whole-degree Fahrenheit `Temp` column
(`weather.gov/wrh/timeseries?site=klga`). The system ingests
`resolution_grade_source="metar_daily_max"` in Celsius. US ASOS METARs carry
0.1°C precision in the remarks T-group -- verified against
`aviationweather.gov/api/data/metar`, which returns `"temp": 26.1` for a
`RMK ... T02610133` group. The Asia and Europe stations return whole degrees
because their METARs carry no T-group. `clients/metar_client.py` passes the
decoded value through unmodified, so no change is needed there.

The chain, for a `("F", 2, half_up)` station:

1. `daily_max_temp_c` -- max over the local-day window, Celsius to tenths.
2. `F = t_c * 9/5 + 32` -- real-valued.
3. `displayed = math.floor(F + 0.5)` -- half-up, **never `round()`**.
4. `key = lo + step * math.floor((displayed - lo) / step)`, clamped to
   `[lo, hi]`.

Converting AFTER taking the Celsius max is equivalent to converting each
observation first, because both C->F and half-up rounding are monotonic
non-decreasing. Worth a comment so nobody "fixes" it later.

Where this can silently land in the wrong bucket:

- **`round()` vs half-up.** The exact `.5°F` values reachable from
  tenths-Celsius are `F in {63.5, 72.5, 81.5, 90.5, 99.5}` at
  `t = 17.5, 22.5, 27.5, 32.5, 37.5°C`. Python's banker's `round`
  disagrees with half-up at **22.5°C (72 vs 73)** and **32.5°C (90 vs 91)**.
  On a `68..88` window both flips land inside one bucket, so the bug hides.
  Shift the window two degrees -- a routine Polymarket re-centre -- and
  32.5°C settles in `88-89` under `round()` and `90-91` under half-up. Two
  adjacent buckets, one of them a position.
- **Whole-degree body group vs T-group.** If any path ever reads the METAR
  body (`26/13` -> 26.0) instead of the decoded `temp` (26.1), the
  Fahrenheit differs whenever the tenths digit crosses an integer: 26.4°C
  -> 79.52 -> 80°F (bucket `80-81`), while 26.0°C -> 78.8 -> 79°F (bucket
  `78-79`). A whole bucket, from a tenths digit.
- **Float formulation.** `c*9/5+32` and `c*1.8+32` disagree in the last ULP
  at 111 of 1100 tenths-points across -50..+59.9°C, but **zero** of those
  disagreements cross the `floor(F + 0.5)` boundary. Float is safe for
  tenths inputs; pin it with an exact-arithmetic test rather than trusting
  the argument.
- **`MIN_REPORTS_PER_DAY = 24`** (`clients/metar_client.py`) was tuned for a
  half-hourly tropical airport. Check it against real US ASOS volumes.

**Open item, to be pinned before any Fahrenheit station is armed, not
guessed:** whether Polymarket reads NOAA's hourly-observation `Temp` column
or the ASOS 6-hour maximum group / CLI daily climate report. The latter two
carry peaks reached BETWEEN hourly observations, in whole Fahrenheit
computed by the ASOS itself, and max-over-obs can understate the daily max
by 1-2°F. If it resolves on the timeseries column, max-over-obs is right and
`maxT` is wrong; if it resolves on the CLI, the reverse. This must be
confirmed against one real settled American day. The Europe registration
already carries an unclosed NOAA-vs-METAR item; do not stack a second
unclosed one on top of it.

### 8. Registry: 15 cities

Sourced from `polydata.pro/weather`, a third-party mirror, because
`gamma-api.polymarket.com` and `polymarket.com` are network-blocked from
this environment -- the same block the Europe research doc records, and the
same substitution it made. **Every city below still needs the per-station
confirmation pass the Europe expansion did** (ICAO, Wunderground slug,
lat/lon, official client, resolution source, bucket bounds, and above all
the unit and step read off the live event) before its `StationConfig` is
written. This spec fixes WHICH cities and HOW the axis works.

| City | Unit | Step | Standard offset | IANA | DST? |
|---|---|---|---|---|---|
| New York City | F | 2 | -5 | `America/New_York` | yes |
| Atlanta | F | 2 | -5 | `America/New_York` | yes |
| Miami | F | 2 | -5 | `America/New_York` | yes |
| Chicago | F | 2 | -6 | `America/Chicago` | yes |
| Houston | F | 2 | -6 | `America/Chicago` | yes |
| Dallas | F | 2 | -6 | `America/Chicago` | yes |
| Austin | F | 2 | -6 | `America/Chicago` | yes |
| Denver | F | 2 | -7 | `America/Denver` | yes |
| Los Angeles | F | 2 | -8 | `America/Los_Angeles` | yes |
| San Francisco | F | 2 | -8 | `America/Los_Angeles` | yes |
| Seattle | F | 2 | -8 | `America/Los_Angeles` | yes |
| Toronto | C | 1 | -5 | `America/Toronto` | yes |
| Mexico City | C | 1 | -6 | `America/Mexico_City` | no (abolished 2022) |
| Sao Paulo | C | 1 | -3 | `America/Sao_Paulo` | no (abolished 2019) |
| Buenos Aires | C | 1 | -3 | `America/Argentina/Buenos_Aires` | no (never) |

Every offset and DST claim above is to be re-verified against the tz
database during the research pass, not taken from this table.

All 15 start collection-only -- absent from `LIVE_TRADING_STATIONS`,
`REGION_LIVE_MAX_*` at `0/0.0/0` -- so no promotion-gate code is needed,
exactly as with Europe. `MIN_RESOLUTION_OBS_BEFORE_ENTRY` means at least ten
days of collection before anything can trade regardless.

Note that the four Celsius cities need NO new axis code. They are what makes
the mixed-unit reality visible inside one region on day one, which is the
whole reason the axis is per-station rather than per-region.

### 9. Display: the unit suffix is a correctness bug, not a cosmetic one

These sites hardcode `°C` / `&deg;C` and must render `axis.label(...)`
instead: `executor.py` (the `[ACTION NEEDED]` order instruction, and two
others), `ev_engine.py`, `pipeline.py`, `deploy/generate_dashboard.py`, and
`deploy/generate_realmoney_dashboard.py` (including its `bounds_drift()`,
which renders `min(buckets)-max(buckets)` with a `°C` suffix and compares
against `station.bucket_min_c`/`bucket_max_c`).

Two of these carry real cost rather than confusion: the executor line is
what a human reads before placing a real order, and the real-money dashboard
is a frozen copy on the box, so it will render Fahrenheit keys under a `°C`
heading until it is explicitly rebuilt and reinstalled.

### 10. Defects this cohort exposes

In scope because each fires before any Americas station could trade, and
three of them fire on the four Celsius cities even if the Fahrenheit work
were dropped entirely.

**a. `SPREAD_CEILING_C = 2.0` becomes per-region** (`config.py:2413`).
The band is tuned on tropical stations whose day-to-day maximum barely
moves. Continental North America and Southern-hemisphere winter routinely
produce forecast-error spreads above 2.0°C, and config.py's own comment
states that "a too-NARROW spread is the dangerous direction: it makes the
model look certain, which inflates the gap between model probability and
market price, which is an edge the entry gates will happily size into." The
clamp would run in that direction, on every cycle, for every affected
station.

Split it into a per-region dict with `asia` and `europe` holding 2.0
verbatim -- provably a no-op for them, the same reference-don't-duplicate
pattern the `REGION_*` dicts use.

**`americas` is registered as `None`, not as a number.** `None` means NO
CLAMP: the measured spread is used as-is. That is deliberately the
conservative direction -- an unclamped spread is wider, which makes the
model less certain, which SHRINKS the gap the entry gates size into. A
guessed ceiling would run the other way, which is the failure this defect
is about. The Americas value stops being `None` only when it is derived
from that region's own measured spread, which cannot happen before the
region has observations. Do not guess it, and do not copy 2.0 across.

**b. `_BUCKET_NUM_RE` captures no sign.** `r"(\d+)\s*°"` parses `"-2°C"` as
`2`. This is not a Fahrenheit issue -- it is a Toronto and Buenos Aires
issue. An interior sub-zero bucket with no `or below`/`or above` text parses
to a positive key, producing a gappy map (fail-closed, but silently and
permanently) or, if the plausibility band is ever widened to admit
negatives, a collision with a real key.

**c. `MIN_PLAUSIBLE_BUCKET_C = 5`.** Toronto and Buenos Aires both have
daily maxima below 5°C. Combined with (b), the outcome is a total trading
blackout for those stations across their cold season -- exactly the volatile
days. The band becomes per-unit and its floor drops; its own justification
("every registered city's live window sits inside 25..40") is already false
for Europe.

**d. `metar_client` day window uses the static offset.**
`_local_day_window_utc` is called with `station.utc_offset_hours`, not
`config.current_utc_offset_hours(station)`, so for a DST-observing station
the window is shifted an hour for most of the year and observations from
23:00-00:00 local on day D-1 are attributed to day D. This is a
PRE-EXISTING defect that Europe already carries; the Americas inherit it at
the same magnitude, on twelve more stations. Fixed here because this is the
change that is already in the file. The arithmetic itself handles negative
offsets correctly.

**e. Two registry tests encode the old assumptions.**
`test_bucket_span_is_eleven_for_every_station` counts VALUES
(`max - min + 1`), which is 21 for a Fahrenheit station; it becomes
`(max - min) // step + 1`. `test_utc_offset_hours_in_registered_timezones`
asserts membership in `(0, 1, 5, 8, 9)` and must admit the negative
Americas offsets.

**f. `long_term_normal_max_c` placeholders and the Southern hemisphere.**
The Europe precedent is "midpoint of the live bucket window at registration
time". Registering Sao Paulo and Buenos Aires in August freezes a WINTER
midpoint as a year-round constant, and their annual swing is larger than any
registered city's. Blast radius is low on the live path -- it is read only
when a station has neither a forecast nor an observation -- but it is not
rare in the backtest walk-forward. Mitigation: label each placeholder with
the month it was taken in. A twelve-entry monthly map is deferred; flag it
if it ever moves a real decision.

### 11. Scheduling

`stations_by_utc_offset()` already resolves DST through
`config.current_utc_offset_hours()`, so the Americas need no scheduler
change. Their standard offsets (-3 to -8) collide with no Asia (+5/+8/+9) or
Europe (0/+1) group, and their primary windows land at roughly 09:00-15:00
UTC -- an empty slot between Europe's 02:00-07:00Z and Asia's evening block.

Two inherited limitations, worth restating because this cohort makes both
larger:

- **Group isolation is cadence, not concurrency.** `run_forever()`
  dispatches groups synchronously in a `for offset in due:` loop, so a slow
  or hanging cycle in one group delays any other group that comes due while
  it runs. Pre-existing, not introduced here, but fifteen more stations in
  five new groups multiplies the load. The practical exposure is a delayed
  EXIT check, since stops are roughly 47% of closed trades in this book.
- **Groups are computed once at startup.** A station crossing a DST
  transition mid-run keeps its pre-transition offset until the process
  restarts. The Americas make this slightly worse than Europe did, because
  American and European transition dates differ by two weeks in spring and
  one in autumn, and because Mexico City (fixed -6) shares a group with
  Chicago in winter but not in summer -- so group MEMBERSHIP, not just
  timing, changes across a transition. The operational note is unchanged:
  restart the daemon on or shortly after each transition date.

### 12. The highest-risk failure mode

**A defaulted axis, not a wrong one.** Every signature change above defaults
to the Celsius step-1 axis, and that default is precisely what buys "zero
edits to Asia and Europe". If any one of the ~14 call sites is missed, a
Fahrenheit station gets `bucket_probabilities` integrating a Celsius Normal
(mean around 27) over intervals `[67.5, 68.5), [69.5, 70.5), ...` -- all
eleven buckets sit some 40 degrees above the distribution. The tail fold at
`b == bucket_min` puts `model_prob ~ 1.0` on the lowest bucket and ~0.0 on
the other ten. Ten buckets at `model_prob 0.0` is ten NO sides showing
`raw_edge ~ 1 - 0 - 0.80 ~ 0.20` -- under `MAX_PLAUSIBLE_RAW_EDGE` (0.25),
over `MIN_ABS_RAW_EDGE`, through both EV windows.

This is exactly the manufactured-trade mechanism `parse_bucket_label`'s own
docstring warns about, except that discovery SUCCEEDED and every risk gate
passes. It would size and place roughly ten trades per cycle per station,
across eleven stations, silently.

Hence the fail-closed rule in section 4: `bucket_probabilities` raises when
handed no axis for a non-`("C", 1)` station. `ev_engine.py` calls it with
neither bounds nor edge mode today and is the highest-probability instance.

## Testing

**The property sweep carries most of the weight.** For every station in
`config.STATIONS`, sweep `t_c` in 0.1°C steps across
`[bucket_min - 3°C, bucket_max + 3°C]` and assert three things, calling the
PRODUCTION functions rather than `bucket_axis` directly:

1. The interval of the key returned by `bucket_for_temp(t_c, ..., axis=...)`
   CONTAINS `t_c` -- one-sided in the two clamped catch-alls.
2. The eleven intervals TILE the line: no gap, no overlap.
3. `sum(p.probability for p in bucket_probabilities(...)) == 1.0 +/- 1e-6`
   for an estimate centred in the window, and the modal bucket's interval
   contains `central_estimate_c`.

One sweep catches an omitted or defaulted axis (a Fahrenheit station tested
against Celsius intervals fails containment on the first sample), a wrong
step (gaps or overlaps), a wrong edge mode (half-degree shift, fails at the
edges), a wrong key convention (off by `step`), and -- because the sweep is
on tenths -- the banker's-vs-half-up flip at 22.5 and 32.5°C. Parametrising
over `config.STATIONS` covers every future station by construction and
re-asserts today's exact behaviour for the existing twenty.

Beyond it:

- **No-op proof:** for all 20 existing stations, `for_station()` returns
  `("C", 1, <their edge_mode>)`, and `_bucket_interval`,
  `bucket_probabilities`, `bucket_for_temp`, `parse_bucket_label`,
  `derive_bucket_bounds` and `bucket_midpoint_c` return values identical to
  the pre-change implementations across their full input ranges.
- **Fail-closed:** `bucket_probabilities` RAISES when given no axis for a
  Fahrenheit station, rather than returning a Celsius-grid answer.
- **Parsing:** all eleven real NYC labels parse to `68,70,...,88`; a label
  yielding a non-consecutive pair is rejected; a Celsius label is never
  parsed by the Fahrenheit branch; `"-2°C"` parses to `-2`, not `2`.
- **Contiguity:** a step-1 map at a step-2 station is rejected; a short map
  is rejected; the uniformly shifted odd grid `69,71,...,89` is rejected.
- **Settlement:** exact-arithmetic boundary test at every reachable `.5°F`
  edge; `round()` and half-up are asserted to differ at 22.5 and 32.5°C and
  the production path is asserted to use half-up.
- **Bias unit safety:** `bucket_midpoint_c` for a Fahrenheit station returns
  a Celsius value inside the station's window, and `derived_bias_stats`
  raises rather than returning an order-60 "bias".
- **Region isolation, mirroring `tests/test_region_isolation.py`:**
  `REGION_*` entries exist for `americas` in all five dicts;
  `REGION_LIVE_MAX_*` are `0/0.0/0`; a large Asia exposure does not reduce
  Americas' budget or vice versa; `pooled_error_spread(region="asia")` is
  unchanged with Americas registered and carrying wild errors; the three
  regions' cached spreads do not overwrite one another.
- **Spread ceiling:** `asia` and `europe` resolve to exactly 2.0;
  `americas` resolves to `None` and the resolver applies no clamp for it,
  returning the measured spread unchanged.
- **The exact-set assertion breaks on purpose.**
  `test_region_isolation.py` asserts
  `set(config.REGION_BANKROLL_USD) == {"asia", "europe"}`. It becomes
  `{"asia", "europe", "americas"}`. Flagged here because it is an exact-set
  test that WILL fail on the region shell commit, and a reviewer should see
  that as expected rather than as a regression.
- **Registry:** the two updated tests pass for all 35 stations; every
  Americas station is absent from `LIVE_TRADING_STATIONS`; `americas` has an
  entry in all five `REGION_*` dicts.
- **Regression:** full existing suite green.

## Build order

Three commits, in this order, because the axis must be provably a no-op
before anything depends on it:

1. **`bucket_axis.py` + the six signature changes + the property sweep.**
   All 20 existing stations declare the default axis; every test above under
   "no-op proof" passes. No new station, no new region. Individually
   revertible, and the only suspect if Asia or Europe regress.
2. **The `americas` region shell + the 4 Celsius cities** (Toronto, Mexico
   City, Sao Paulo, Buenos Aires) + all six defects (a) through (f). These
   need no Fahrenheit code at all, so the region's isolation can be verified
   on its own.
3. **The 11 Fahrenheit cities**, with the settlement source confirmed
   against one real settled American day first.

## Files touched

- `models.py` -- `StationConfig.bucket_unit`, `bucket_step`; docstring on
  `bucket_min_c`/`bucket_max_c`
- `bucket_axis.py` -- NEW: `BucketAxis`, `AXIS_C1`, `for_station()`
- `probability.py` -- `_bucket_interval`, `bucket_probabilities` (axis-aware,
  fail-closed)
- `market_discovery.py` -- `parse_bucket_label` (F branch, sign capture,
  per-unit band), `derive_bucket_bounds(step=)`, two call sites
- `bucket_bias.py` -- `bucket_midpoint_c` (returns Celsius),
  `quantization_stderr_c` caller, `derived_bias_stats` assertion,
  `load_settled_buckets` unpack
- `backtest/resolution.py` -- `bucket_for_temp(axis=)`
- `storage.py` -- `settled_buckets.bucket_unit` / `.bucket_step`,
  `save_settled_bucket`, `load_settled_buckets`
- `clients/metar_client.py` -- DST-correct day window; `MIN_REPORTS_PER_DAY`
  review
- `config.py` -- `REGION_*` x5 gain `americas`, per-region
  `SPREAD_CEILING_C`, 15 new `STATIONS` entries,
  `EXPECTED_BUCKET_COUNT` comment
- `ev_engine.py`, `pipeline.py`, `position_manager.py`, `executor.py`,
  `stop_loss_audit.py`, `promotion_dossier.py`, `backtest/engine.py`,
  `backtest/report.py` -- pass an axis; render `axis.label(...)`
- `deploy/generate_dashboard.py`, `deploy/generate_realmoney_dashboard.py`
  -- render the axis unit; `bounds_drift()`; `americas.html`;
  `deploy/setup_dashboard.sh` gains `--region americas`
- `tests/` -- `test_bucket_axis.py` (new), `test_region_isolation.py`
  (americas), `test_station_registry.py` (two assertions), plus the
  settlement and parsing tests above
