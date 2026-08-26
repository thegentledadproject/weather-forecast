# Real-money station dashboard

Status: draft, pending user review
Date: 2026-08-26

## Purpose

Give the stations that can spend real money a page of their own, answering
one question the existing dashboards cannot: **could an order open right
now, and if not, what is in the way?**

The Asia and Europe pages are P&L pages. They report what the book did.
Neither reports the state of the machinery that decides whether it does
anything at all -- the gate ladder, the schedule position, whether market
discovery has even happened yet, or what was actually submitted to the
exchange. Today those facts live in four places, none of them a page:
`config` constants, host state under `/etc/polyweather`, the journal, and
a `live_order_attempts` table that nothing renders.

That gap has cost real diagnosis time. `BOUNDS DRIFT` is a journal line you
have to know to grep for. The 2026-08-20 restart armed RCSS for real money
as a side effect of an unrelated deploy, and the only confirmation was a
startup banner. A rejected order leaves no trace outside the process log.
None of this is a defect in the trading code; it is a reporting gap, and
the fix belongs in a dashboard rather than in the daemon.

## Scope

**In scope, stage 1 (dashboard-only, no daemon change):**

- A new standalone generator `deploy/generate_realmoney_dashboard.py`
  rendering `/var/www/html/realmoney.html`.
- One card per station in `config.LIVE_TRADING_STATIONS`, grouped by
  `config.region_of()`, showing the readiness ladder (section 2).
- The full, unfiltered EV/edge table for those stations, compared against
  the EV bar actually in force (section 3).
- The schedule strip in station-local time with market-discovery state
  stacked under it (section 4).
- The `live_order_attempts` audit trail (section 5).

**In scope, stage 2 (requires a daemon deploy, ships separately):**

- `entry_manager` persisting its per-candidate `EntryDecision` list, and
  the page rendering approve/reject with the real reason string
  (section 6).

**Out of scope:**

- P&L of any kind. The region pages own that, and this page must not
  become a second place where the book is scored.
- Any change to trading behaviour. Stage 1 reads; stage 2 adds one
  fail-soft write that no decision depends on.
- Re-deriving `entry_manager`'s decision ladder in the dashboard. See
  "Rejected alternatives".
- Extracting shared page furniture from the three generators. Worth doing
  at four pages; not as part of this.

## Architecture

A standalone sibling of `deploy/generate_backtest_dashboard.py`: its own
page shell, its own CSS, importing only the package (`config`, `storage`,
`scheduler`, `backtest.price_store`). Nothing is shared with
`generate_dashboard.py`.

This is a deliberate choice of isolation over DRY, and it costs a third
copy of the stylesheet. The reasoning: `generate_dashboard.py` renders two
production pages, this page's data sources are half-unproven, and a shared
module would add a file to the frozen-copy set in `/usr/local/bin` that
`deploy_daemon.sh` does not know about. The existing backtest generator
already establishes the standalone shape, so this follows precedent rather
than inventing one.

Fail-soft is inherited whole: every read wrapped, failures rendered as
warnings ON the page, worst case a friendly empty state. Nothing here may
raise out to the caller.

### Region grouping

Stations are grouped under a heading per `config.region_of(icao)`, and no
figure is ever summed across regions. Both live stations are Asia today,
so the grouping is currently invisible -- that is the point. It exists so
that a European station being armed produces a new group rather than a
silent cross-region mix, which is the failure the whole isolation
framework exists to prevent.

## 1. Page shell

Arguments mirror the backtest generator: `--out`, defaulting to
`/var/www/html/realmoney.html`. The package directory is read from
`DASHBOARD_PKG_DIR` -- the same variable `generate_dashboard.py` already
uses, deliberately not a new one, so pointing a local checkout at both
generators is one export rather than two.

Unlike its siblings, the module runs nothing at import time. Argument
parsing and rendering live in a guarded `main()`, so the pure helpers are
importable by tests. See "Testing".

## 2. Readiness ladder

One card per station. The rungs are evaluated and displayed in the order
the real code applies them, each carrying its actual value rather than a
bare tick:

| Rung | Source |
|---|---|
| Gate 2, process-global | `POLYMARKET_LIVE_TRADING` present in the daemon's `/proc/<MainPID>/environ` |
| Execution mode | `/etc/polyweather/mode.env` |
| Gate 1, per station | `config.live_mode_is_permitted(icao, "live")` |
| Maturity provenance | `config.MATURITY_OVERRIDE` |
| Region authorisation | `config.region_authorises_live_orders(region)` |
| Schedule window | `scheduler.determine_window()` at station-local time |
| Capacity | region caps vs `storage` counts |

**The gate 2 probe reads NAMES, never values.** The live-trading drop-in
holds `POLYMARKET_PRIVATE_KEY`. The probe resolves the daemon's MainPID via
`systemctl show`, reads `/proc/<pid>/environ`, and tests for the presence of
the name `POLYMARKET_LIVE_TRADING`. It must never print, log, or store any
value from that file, and the test suite asserts this. Where the probe
cannot run -- off the box, or without the privilege to read another
process's environ -- the rung renders "cannot be observed from this
process", never "off". This mirrors the verification procedure the operator
already follows by hand, and the 2026-08-12 incident it comes from: a
live-armed daemon with no credentials is indistinguishable from a working
one until an order is attempted.

**Gate 1 reports which half failed.** `live_mode_is_permitted()` returns a
single boolean over two independent conditions -- allowlist membership and
maturity. The card shows both, because they have opposite remedies.

**Maturity provenance is loud when overridden.** Both currently live
stations are live only via `MATURITY_OVERRIDE`, and RCSS fails the measured
`beats_market` criterion (0.145 vs the market's 0.062). A page that renders
"mature" without saying "by override" would misreport the single most
important caveat on the real-money track.

**Capacity distinguishes unknown from zero.** `count_live_order_attempts()`
returns `None` when the count cannot be read, and its callers treat that as
"cannot authorise" precisely because a rate limit that fails open is not a
rate limit. The card renders `None` as "unknown", never as 0.

**The ladder's claim is bounded.** It says an order COULD open, never that
a given candidate WOULD. Per-bucket caps, the stop-out cooldown and the
opposite-side lock are per-candidate, evaluated inside `evaluate_entry()`,
and belong to stage 2. The page states this rather than leaving the reader
to assume the ladder is complete.

## 3. Edge and EV detail

Reads the same `data/ev_latest_<ICAO>.json` snapshots the region pages
read, written by `ev_engine.save_ev_snapshot()`. Columns: bucket, side,
model p, market price, raw edge, slippage, net EV/$, spread source, notes.

**Rendered unfiltered**, unlike the region pages. Those show only rows
clearing the entry screen, which is correct when the question is "is there
anything to take" and wrong when it is "how close did we get". On a
real-money page, a table of near-misses is the signal.

**The EV bar comes from the active window** (`min_net_ev` on the window
`scheduler.determine_window()` returns), not a constant. A row is only
meaningfully "below the bar" relative to the bar in force at that moment,
and the bar differs by window.

**Veto badges read config, never restate it.** The entry-price ceiling is
`config.MAX_ENTRY_PRICE` and the edge ceiling is
`config.max_plausible_edge_for(price)` -- price-relative, not a flat
constant. The existing EV card's own comments record this going stale
twice: once when `EV_MIN_PRICE_SCREEN` was hardcoded and phantom
"+18,820% EV" rows ranked top of the table, and again when the flat
`MAX_PLAUSIBLE_RAW_EDGE` was restated after the ceiling became
price-relative. Both are read through `getattr` with a fallback so the page
still renders against a package checkout predating either constant.

## 4. Schedule strip and market discovery

**The strip** lays `config.SCHEDULE_WINDOWS` across the day in each
station's *current* local offset from `config.current_utc_offset_hours()`,
not the static `utc_offset_hours` field. This matters for any DST-observing
station and is what made London and the continentals split into separate
scheduler groups correctly on 2026-08-25. It marks where "now" sits, names
the active window's mode, EV bar and scan interval, and counts down to the
next entry window opening or closing. The arithmetic comes from
`scheduler.determine_window()` and `seconds_until_next_boundary()`; the
page calls them rather than restating the window table.

**Discovery stacks underneath**, per station, for
`config.local_today(station)`:

- Buckets with tokens in `price_store.market_tokens`, and when each was
  first seen (`discovered_at`).
- How many of those have a book fresh enough that
  `price_store.get_price_at()` returns a quote.
- A derived bounds check: the station's configured `bucket_min_c` /
  `bucket_max_c` against the discovered bucket range. This reproduces the
  `BOUNDS DRIFT` warning as page state instead of a journal line that has
  to be grepped for.

Stacking is the whole point. A station sitting inside its primary window
with zero discovered buckets currently looks exactly like a quiet night.
Here it reads as a fault.

**Caveat rendered on the page:** `market_tokens` is populated by snapshot
capture, so "not discovered" means "capture has not recorded it", not "the
market does not exist". Absence of evidence is labelled as such.

## 5. Order activity

`storage.load_live_order_attempts()` rendered directly: timestamp, station,
bucket and side, notional, shares, limit price, outcome, order id,
truncated detail, with entries and exits distinguished. Above the table,
today's submitted entries against the cap that actually binds:
`config.REGION_LIVE_MAX_ORDERS_PER_DAY[region]`, counted over that
region's stations via `count_live_order_attempts(station_icaos=...)`.
`executor.py:238` enforces the region cap, not the process-global
`LIVE_MAX_ORDERS_PER_DAY` -- which today is merely the value the `asia`
entry aliases, and would be the wrong number to display for any other
region.

This table is the only record anywhere of an order that was built,
submitted and refused -- an unfilled FOK deliberately writes no position --
and nothing currently renders it.

## 6. Stage 2: persisted entry decisions

`entry_manager` gains a snapshot writer on the exact contract
`ev_engine.save_ev_snapshot()` already establishes:

- Writes `data/entry_decisions_<ICAO>.json` after
  `decide_portfolio_entries()`.
- Overwrites; no history, no schema migration.
- Fail-soft: a disk error must never break a cycle. Decisions drive
  trading, the snapshot only drives reporting.
- An empty list still writes a snapshot. "Evaluated and rejected
  everything" and "never ran" are different facts and the page must be able
  to tell them apart.

The page then renders each candidate with `should_enter` and the real
`reason` string, turning the readiness ladder's "could an order open" into
"why this one did not".

This ships as its own change, with a daemon deploy and the timing rules
that implies -- entry windows closed, open live positions accounted for.
Stage 1 ships as a dashboard-only file copy with zero trading impact.

## Rejected alternatives

**Re-deriving the entry ladder in the dashboard.** Rejected. It would let
the page report a reason without a daemon deploy, but it means the
dashboard reimplementing `evaluate_entry()`'s gates against a moving
target. The EV card has already gone stale twice doing exactly this with
two constants; a whole decision ladder is a worse version of the same bug,
and its failure mode is a page that confidently reports the wrong reason.

**A shared `deploy/dashboard_common.py`.** Deferred, not rejected. Correct
at four pages. Today it would edit two generators serving live pages and
add a file to `/usr/local/bin` that `deploy_daemon.sh:66-69` does not
enumerate, so it would go stale on every deploy.

**A `--view` flag on `generate_dashboard.py`.** Rejected. Maximum reuse,
but it grows an already 1,500-line script that renders both production
pages and couples this page's failures to theirs.

## Error handling

Identical contract to both siblings. Every data read wrapped; a failure
appends to `warnings` and renders on the page rather than killing the
render. Every section degrades independently -- an unreadable EV snapshot
must not cost the reader the readiness ladder.

Three cases get explicit non-default handling, because the honest answer
is not the falsy one:

- Unreadable order counts render "unknown", not 0.
- An unobservable gate 2 renders "cannot be observed", not "off".
- No discovered buckets renders "capture has not recorded any", not "the
  market does not exist".

## Testing

Neither existing generator has tests: both run `argparse` at import, so
nothing in them is importable. This one deviates by putting the pure
helpers above a guarded `main()`.

`tests/test_realmoney_dashboard.py` covers the genuinely new arithmetic:

1. **Window countdown** -- boundary cases against `determine_window()` and
   `seconds_until_next_boundary()`, including the window-end boundary and
   a `closed` window with a `None` interval.
2. **Bounds-drift derivation** -- config range vs discovered range: match,
   drift in each direction, and nothing discovered.
3. **The `/proc` probe** -- present, absent, and unreadable. Asserts the
   three-state result, and asserts that no environment VALUE appears in
   anything the probe returns.
4. **Render smoke test** -- full render against a seeded temporary
   database, asserting exit status and that each section rendered. Neither
   existing generator has this.

Stage 2's snapshot writer gets unit tests in the package's existing suite,
where `entry_manager` is already covered.

## Deployment

Stage 1 is a dashboard-only change and takes the shape recorded for
2026-08-17 and 2026-08-26: pscp the file, `sudo install` into
`/usr/local/bin`, kick `polyweather-dashboard.service`. No daemon restart,
no repo touch on the box.

Three files must change together or the new page silently rots:

1. `deploy/generate_realmoney_dashboard.py` -- the generator.
2. `deploy/setup_dashboard.sh` -- installs it, so a rebuilt box reproduces
   the shape.
3. `deploy/deploy_daemon.sh:66-69` -- the frozen-copy refresh enumerates
   the two existing generators BY NAME. A third that is not added there is
   never refreshed by any deploy, which is the 2026-08-05 failure that
   motivated that block in the first place.

The dashboard unit's `ExecStart` also needs the third invocation added by
hand. `deploy_daemon.sh` does not touch
`/etc/systemd/system/polyweather-dashboard.service`, and re-running
`setup_dashboard.sh` is not a substitute -- it `mv`s from `/home/ubuntu`
expecting a pscp'd file, re-installs nginx and rewrites the timer. This is
the gotcha recorded on 2026-08-25, where a generator gaining a new argument
left `europe.html` silently never rendered.

Verification after deploy is the md5 pair between `/usr/local/bin` and
`deploy/`, plus the served page over HTTP -- not "the page looks right".

## Risks

- **The frozen copy goes ahead of the box's repo** until the commit reaches
  main and the box pulls, at which point the next full deploy reverts it.
  Same standing trap as every dashboard-only deploy; closed the same way.
- **The `/proc` probe is a new capability for the dashboard process.** It
  reads another process's environment. Restricted to a name test with no
  value ever leaving the function, asserted by test, and degrading to
  "cannot observe" rather than to a claim.
- **Discovery state is only as good as snapshot capture.** If
  `ENABLE_SNAPSHOT_CAPTURE` is off, the discovery section reports nothing
  discovered for every station. The page must say which of the two it is
  looking at.
