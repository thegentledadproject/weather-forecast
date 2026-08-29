# Europe market isolation framework

Status: draft, pending user review
Date: 2026-08-24

## Purpose

Add European "highest temperature" stations to the trading system, isolated
from the existing Asia book on every axis that matters: capital, promotion
to real money, and scheduling correctness. Isolation is required because a
new region carries correlated, unmeasured risk the same way the 11-station
Asia expansion did (see memory `station-performance-divergence`), and
because this codebase has already been burned once by an assumption that
didn't hold outside its original scope: `StationConfig.utc_offset_hours`
is a static int specifically because "NONE of the registered cities
observes DST -- that is an assumption that happens to hold for every Asian
market listed so far, not a general truth" (`models.py`, `StationConfig`
docstring). Every European candidate station observes DST. That comment is
the trigger for this design, not an aside.

## Scope

**In scope:**
- A `region` concept on `StationConfig`, with per-region capital
  (`BANKROLL_USD`) and per-region daily exposure cap
  (`MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD`), so Europe can never size a
  real order against Asia's capital or vice versa.
- A region-scoped version of the LIVE-money blast radius
  (`LIVE_MAX_CONCURRENT_POSITIONS`, `LIVE_MAX_TOTAL_EXPOSURE_USD`,
  `LIVE_MAX_ORDERS_PER_DAY`), which today is a single process-global
  backstop "across all live stations" -- without this, isolation holds
  only until the first European station is ever promoted to real money,
  at which point it would silently start competing with WSSS/RCSS for the
  same slots and the same dollar ceiling.
- A region-scoped pooled forecast-error spread
  (`calibration.pooled_error_spread`), which today pools across every
  registered station and is the tier a new station falls back on -- the
  one coupling that would let merely registering Europe change ASIAN
  trading behaviour, via spread -> probability -> EV -> entries.
- A DST-aware offset mechanism (`iana_timezone` on `StationConfig` +
  `config.current_utc_offset_hours()`), so `local_today()`,
  `local_day_bounds_utc()`, and scheduler grouping stay correct for
  DST-observing stations without touching any Asia station's behavior.
- Registry entries for the European stations confirmed to have a live
  Polymarket "highest temperature" event as of 2026-08-24: London, Paris,
  Madrid, Amsterdam, Milan, Munich, Warsaw.
- All new stations start collection-only (absent from
  `LIVE_TRADING_STATIONS`), same as every Asia station did at launch. No
  new promotion-gate code is needed -- the existing per-station allowlist
  and simulation-mode-is-always-available design already do this for free.

**Out of scope (explicitly deferred, not forgotten):**
- A second Polymarket wallet / second process. The region-scoped capital
  split below achieves the isolation that actually matters (position
  sizing, daily exposure) without doubling operational surface for a
  cohort that will sit in collection-only for a long time. Revisit if/when
  a European station is actually being considered for real-money
  promotion and the single-wallet approach turns out to be insufficient.
- Live regrouping of scheduler timezone groups mid-run across a DST
  transition (see "Known limitation" below).
- Per-station research to confirm exact ICAO code, Wunderground slug,
  lat/lon, and settlement source for each of the 7 candidate cities --
  this spec identifies WHICH cities, not their full `StationConfig`
  payload. That confirmation pass (same shape as the Asia expansion did
  per-station) is implementation work, tracked as an open item below.

## Design

### 1. `StationConfig` additions (`models.py`)

```python
region: str = "asia"          # bankroll/exposure pool this station draws from
iana_timezone: Optional[str] = None   # e.g. "Europe/London"; None => legacy static offset
```

Defaulting `region` to `"asia"` means all 13 existing entries need zero
edits -- they're already implicitly one pool today, this just names it.
`iana_timezone` defaults to `None` so `utc_offset_hours` keeps meaning
exactly what it means today for every station that doesn't set it.

### 2. DST-aware offset (`config.py`)

```python
def current_utc_offset_hours(station: Union[str, StationConfig]) -> int:
    """
    The station's UTC offset RIGHT NOW. Stations with iana_timezone set
    get a live DST-aware offset via zoneinfo; everything else keeps the
    static utc_offset_hours int this codebase has always used. No new
    dependency -- zoneinfo is Python stdlib (3.9+).
    """
```

Every current reader of `station.utc_offset_hours` on the trading path
(`local_today`, `local_day_bounds_utc`, `scheduler.stations_by_utc_offset`)
switches to calling this helper instead of reading the field directly.
Behavior for existing stations is byte-for-byte unchanged: none of them
set `iana_timezone`, so the helper falls straight through to the same int
it already returns.

### 3. Region-scoped capital (`config.py`, `entry_manager.py`)

```python
REGION_BANKROLL_USD = {"asia": BANKROLL_USD, "europe": 0.0}
REGION_MAX_DAILY_EXPOSURE_USD = {
    "asia": MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD,
    "europe": 0.0,
}
```

`BANKROLL_USD` and `MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD` remain the
single source of truth for Asia's numbers -- the dict references them
rather than duplicating the literals. Europe is deliberately funded at
$0.00: raising it is a one-line, explicit, auditable operator decision,
not a side effect of adding stations to the registry.

Two call sites change:
- the `bankroll_sized_usd` assignment in `entry_manager.evaluate_entry()` -- `bankroll_sized_usd = kelly_applied *
  config.region_bankroll_usd(station_icao)` (new lookup helper) instead of
  the flat `config.BANKROLL_USD`.
- `entry_manager.portfolio_day_exposure_usd()` (currently sums
  `station_day_exposure_usd` across every entry in `config.STATIONS`
  unconditionally) gains a `region` parameter and sums only stations in
  that region. `apply_portfolio_budget`'s caller passes the acting
  station's own region and `config.region_max_daily_exposure_usd(...)` as
  the cap.

Per-station and per-bucket caps
(`MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD`, per-bucket cooldown) are
already station-scoped and need no change -- only the portfolio-wide cap
was pooling across regions.

### 4. Region-scoped live blast radius (`config.py`, `executor.py`, `storage.py`)

This is a separate mechanism from section 3 -- `LIVE_MAX_CONCURRENT_POSITIONS`,
`LIVE_MAX_TOTAL_EXPOSURE_USD`, and `LIVE_MAX_ORDERS_PER_DAY`
gate real-money order submission specifically, and
are explicitly documented today as counting "across all live stations" --
process-global, not per-station, not per-region. `entry_manager`'s
Kelly-sizing budget being isolated by region does nothing to this path,
because live orders don't go through Kelly sizing at all (`LIVE_TRADE_SIZE_USD`
replaces it entirely, per `entry_manager.py`'s own comment on Cap 2b).

```python
REGION_LIVE_MAX_CONCURRENT_POSITIONS = {"asia": LIVE_MAX_CONCURRENT_POSITIONS, "europe": 0}
REGION_LIVE_MAX_TOTAL_EXPOSURE_USD = {"asia": LIVE_MAX_TOTAL_EXPOSURE_USD, "europe": 0.0}
REGION_LIVE_MAX_ORDERS_PER_DAY = {"asia": LIVE_MAX_ORDERS_PER_DAY, "europe": 0}
```

Same pattern as section 3: Asia's three constants stay the literal source
of truth, referenced not duplicated. Europe is locked at 0/0/0 -- structurally
unable to submit a single live order no matter what `LIVE_TRADING_STATIONS`
or the `POLYMARKET_LIVE_TRADING` env flag say, until an operator explicitly
raises all three.

`executor._live_budget_breach()` (currently `_live_budget_breach(size_usd)`)
gains a `station_icao` parameter -- its one caller, `_open_via_order_path`,
already has `decision.station_icao` in scope. Inside, it:
- resolves `region = config.get_station(station_icao).region`,
- filters `live_positions` to `config.get_station(p.station_icao).region == region`
  before computing the concurrent-position count and summed exposure,
- checks those against `config.REGION_LIVE_MAX_CONCURRENT_POSITIONS[region]`
  / `REGION_LIVE_MAX_TOTAL_EXPOSURE_USD[region]` instead of the flat constants.

`storage.count_live_order_attempts()` gains an optional
`station_icaos: Optional[list[str]] = None` filter (`AND station_icao IN
(...)` alongside the existing `kind`/`ts` filter) so the daily order-rate
cap can be counted per-region; `_live_budget_breach()` passes the region's
station list and compares against `REGION_LIVE_MAX_ORDERS_PER_DAY[region]`.
`None` (no filter) preserves today's global count for any caller that
doesn't pass it, so this is additive, not a behavior change for existing
callers.

**Constraint to carry forward, not just copy a number:** `LIVE_MAX_TOTAL_EXPOSURE_USD`
is not independently chosen -- `config.py`'s own comment derives it as
`LIVE_MAX_CONCURRENT_POSITIONS x worst-case single-entry cost`. Whenever
`REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"]` is ever raised above 0,
`REGION_LIVE_MAX_TOTAL_EXPOSURE_USD["europe"]` must be re-derived from that
same product for Europe's own worst-case entry cost (which will differ --
different `MAX_ENTRY_PRICE`/bucket economics), not copied from Asia's
Asia's current ceiling or guessed.

### 5. Region-scoped pooled forecast-error spread (`calibration.py`)

A THIRD kind of coupling, found while reviewing the implementation plan
against the code and not identified when sections 3 and 4 were written.
It is neither capital nor blast radius but a shared STATISTICAL
ESTIMATOR, and it is the only one of the three that can change an
existing region's trading behaviour without touching that region's code.

`calibration.pooled_error_spread()` pools forecast errors across every
station in `config.STATIONS`, and is the spread tier a station falls back
to when it has fewer than `config.MIN_SPREAD_PAIRS` (5) pairs of its own.
Every newly registered station starts on exactly that tier. So:

  - European stations would derive their spread from Asian tropical
    forecast errors. Wrong, but wrong only for Europe.
  - Once European errors accumulate, they enter the pool ASIAN stations
    fall back on. Spread feeds `probability.py`, which feeds EV, which
    feeds entry decisions -- so merely registering Europe would move
    Asian trading behaviour. That is the isolation premise failing at its
    quietest point.

Temperate and tropical stations have genuinely different error
distributions; a pool spanning both describes neither.

`pooled_error_spread(region: Optional[str] = None)` filters the stations
it pools, and `region` joins `config.DB_PATH` in the `_pooled_spread_cache`
key -- without that, the first region to compute would serve its spread to
every other one, reintroducing the leak the filter removes. `None` keeps
the all-stations pool for callers that predate regions. Callers that know
their station pass its region; `estimate_std_dev()` is the one that
matters, since it is on the live entry path.

### 6. Promotion gate

No new code. `LIVE_TRADING_STATIONS` already gates real-money submission
per station-ICAO, and simulation mode is already available to any
registered station regardless of maturity (`live_mode_is_permitted`'s own
design: simulation is deliberately never gated on maturity, "so a station
is never barred from the activity that produces its own evidence"). The 7
European stations are added to `config.STATIONS` and simply never added
to `LIVE_TRADING_STATIONS` -- identical to how all 11 non-WSSS Asia
stations started.

### 7. Scheduling isolation (`scheduler.py`)

`stations_by_utc_offset()` switches its lookup from
`station.utc_offset_hours` to `config.current_utc_offset_hours(station)`.
No other scheduler change is needed: European stations' offsets (UK
0/+1, CET +1/+2) don't collide with any Asia offset (+5/+8/+9), so they
fall into their own timezone group(s) automatically under the daemon's
existing per-offset-group dispatch.

CORRECTED 2026-08-25, after the final branch review. An earlier draft of
this section claimed that "a bug or slow cycle in one region's group
cannot block or delay another region's group". That is FALSE.
`run_forever()` dispatches groups synchronously inside a `for offset in
due:` loop, so group isolation is CADENCE, not CONCURRENCY: a slow or
hanging cycle in one group delays any other group that comes due while it
runs. The coupling is pre-existing -- it already applied to Japan vs.
Singapore -- and this design does not introduce it, but registering seven
more stations multiplies the load, and Europe's primary window
(~03:00-06:00 UTC) interleaves with Asia's all-day monitor_only/risk_only
exit checks. Since stops are roughly 47% of closed trades in this book,
the practical exposure is a DELAYED EXIT CHECK, not a delayed entry.
Making the dispatch concurrent is a separate decision with its own risks
and is not part of this design.

**Known limitation, documented rather than solved:** `run_forever()`
computes `groups = stations_by_utc_offset(...)` once at startup. A station
using `iana_timezone` that crosses a DST transition while the daemon is
running keeps its pre-transition offset (and therefore a schedule window
shifted by an hour) until the process restarts. This matches how this
codebase already handles other slow-moving drift (e.g. the bucket-bounds
resweep is a manual, log-driven operator action, not automatic) rather
than adding live-regrouping machinery for an event that happens twice a
year. The operational note: restart the daemon on or shortly after each
BST/CEST transition date. Building automatic mid-run regrouping is
explicitly deferred -- flag it if it ever causes a real missed window.

### 8. Station registry entries

Confirmed via the Gamma API (`https://gamma-api.polymarket.com/events?slug=...`,
same lookup `market_discovery.py` uses at trade time) to have a live
"highest temperature" event as of 2026-08-24:

| City | Event slug fragment | Region |
|---|---|---|
| London | `highest-temperature-in-london` | europe |
| Paris | `highest-temperature-in-paris` | europe |
| Madrid | `highest-temperature-in-madrid` | europe |
| Amsterdam | `highest-temperature-in-amsterdam` | europe |
| Milan | `highest-temperature-in-milan` | europe |
| Munich | `highest-temperature-in-munich` | europe |
| Warsaw | `highest-temperature-in-warsaw` | europe |

Checked and NOT currently live: Berlin, Rome, Dublin, Barcelona, Vienna,
Zurich, Brussels, Copenhagen, Athens, Lisbon. This is not necessarily an
exhaustive sweep of every European city Polymarket might list -- it's the
same one-pass check the Asia expansion used, not a claim that no other
city has a market.

**Open item, tracked not guessed:** each station's exact ICAO code,
`wunderground_slug`, lat/lon, `official_client_key` (does an
official-forecast adapter already exist for these countries, e.g. a
Met Office / Météo-France / DWD equivalent to `clients/official/nea.py`,
or do they start with no official client the way some Asia stations
did?), `resolution_grade_source`, and initial `bucket_min_c`/`bucket_max_c`
still need the same per-station confirmation the Asia expansion did (see
memory `asia-station-expansion` for the shape of that work -- e.g. VHHH's
settlement-source override and OPKC's station-identity ambiguity were
both caught exactly this way). This spec fixes WHICH 7 cities and HOW the
isolation mechanism works; the implementation plan should include a
research step per station before writing its `StationConfig` entry, not
assume Wunderground-everywhere.

**Risk gates that apply automatically, unchanged:**
`MIN_RESOLUTION_OBS_BEFORE_ENTRY` (collection-only until 5+ resolution
observations accumulate) and the bias-quality gate both already read
generically off any registered station -- no region-specific version
needed, same as Asia.

## Testing

- `current_utc_offset_hours()`: a station with `iana_timezone` set returns
  the correct offset on both sides of a real DST boundary (e.g.
  `Europe/London` around 2026-10-25, `Europe/Berlin`/`Europe/Warsaw`
  around 2026-03-29 and 2026-10-25); a station without it returns the
  unchanged static int.
- Region-scoped sizing: `bankroll_sized_usd` for a `region="europe"`
  station with `REGION_BANKROLL_USD["europe"] == 0.0` is always 0,
  regardless of Kelly fraction -- structurally cannot size a real order.
- Region-scoped portfolio exposure: `portfolio_day_exposure_usd(region=...)`
  sums only same-region stations; a large Asia-side exposure does not
  reduce Europe's remaining budget or vice versa.
- Region-scoped live blast radius: with `REGION_LIVE_MAX_CONCURRENT_POSITIONS["europe"] == 0`,
  `_live_budget_breach()` refuses every live entry for a `region="europe"`
  station regardless of how empty the live book is. Separately, 3 open
  Asia live positions at the (hypothetical, test-only) Asia cap do not
  block a Europe entry once Europe has its own non-zero budget, and vice
  versa -- the two regions' concurrent-position counts, exposure sums, and
  daily order counts must be computed independently.
- Region-scoped pooled spread: with a European station registered and
  carrying wild forecast errors, `pooled_error_spread(region="asia")` is
  unchanged from the pre-Europe value, and the two regions' cached values
  do not overwrite one another.
- Regression: full existing suite green, plus an explicit check that
  `current_utc_offset_hours()` for all 13 existing stations returns
  exactly `station.utc_offset_hours` (i.e. the new helper is a true
  superset, not a behavior change), that `REGION_BANKROLL_USD["asia"]`
  /`REGION_MAX_DAILY_EXPOSURE_USD["asia"]` equal the pre-existing flat
  constants, and that `_live_budget_breach()` for any existing Asia
  station produces the identical accept/reject decision it did before
  the region filter was added (Asia is the only region with real
  positions today, so its filtered and unfiltered counts are identical).
- `stations_by_utc_offset()` places the 7 European stations into groups
  distinct from every Asia station's group, and does not change any Asia
  station's group membership.

## Files touched

- `models.py` -- `StationConfig.region`, `StationConfig.iana_timezone`
- `config.py` -- `current_utc_offset_hours()`, `REGION_BANKROLL_USD`,
  `REGION_MAX_DAILY_EXPOSURE_USD`, `region_bankroll_usd()`,
  `region_max_daily_exposure_usd()`, `REGION_LIVE_MAX_CONCURRENT_POSITIONS`,
  `REGION_LIVE_MAX_TOTAL_EXPOSURE_USD`, `REGION_LIVE_MAX_ORDERS_PER_DAY`,
  7 new `STATIONS` entries, updated `local_today()`/`local_day_bounds_utc()`
  to use the new helper
- `entry_manager.py` -- region-scoped bankroll sizing,
  `portfolio_day_exposure_usd(region=...)`, `apply_portfolio_budget` caller
- `executor.py` -- `_live_budget_breach(size_usd, station_icao)` filters
  live positions and rate-limit counts to the station's own region
- `storage.py` -- `count_live_order_attempts(kind, since_iso, station_icaos=None)`
- `calibration.py` -- `pooled_error_spread(region=None)`, region in the
  `_pooled_spread_cache` key, and its callers on the entry path
- `scheduler.py` -- `stations_by_utc_offset()` uses the new helper
- `tests/` -- new test file(s) for the above, plus regression coverage
