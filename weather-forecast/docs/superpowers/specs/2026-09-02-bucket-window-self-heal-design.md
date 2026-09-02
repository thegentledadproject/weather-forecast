# Bucket-window drift self-heal

Status: draft, pending user review
Date: 2026-09-02

## Purpose

Polymarket re-centres each city's temperature-bucket window continuously —
"every few days, not seasonally", as `config.py`'s own resweep note puts it.
`config.STATIONS` keeps a hand-typed copy of each window in
`bucket_min_c`/`bucket_max_c`, and that copy rots the moment it is written.

It has been repaired by hand twice:

| Sweep | Scope | Result |
|---|---|---|
| 2026-08-20 | 13 Asia stations | **13 of 13** had drifted off the 2026-08-06 reading; RKPK and ZBAA by 5 and 4 degrees. ZBAA changed 8 times in 8 days. |
| 2026-09-02 (`b0ba062`) | 15 Americas stations | **13 of 15** had drifted within days of registration; CYYZ by 14 °F-equivalent; SBGR's window fell a full 10 °C. |

Both sweeps were the same manual ritual: read `BOUNDS DRIFT` out of
`journalctl` on the box, retype ~30 numbers into `config.py`, commit. The
commit message for the second one states the obvious conclusion — *"config.py's
copy will drift again and needs the same discovery pass repeated periodically."*

The copy is stale again already. `market_data.sqlite3`'s `market_tokens`
table records what the live events actually listed on 2026-08-27:

```
KLGA  2026-08-27  68..88   (22 token rows)
WSSS  2026-08-27  27..37   <- config.STATIONS["WSSS"] says 28..38
```

WSSS drifted back off the value the 08-20 sweep installed, within a week.

**The mechanism already exists and is thrown away.** `ev_engine.run_for_station()`
discovers the token map every cycle, derives the true bounds through
`market_discovery.derive_bucket_bounds()`, compares them to config, and prints
`BOUNDS DRIFT` — to stdout. Nothing persists it. This design writes it down and
points every consumer of the hand-typed copy at the written-down value.

## What drift reaches, and what it does not

Unchanged from `config.py`'s existing analysis, re-verified against the code:

| Consumer | Effect of a stale window |
|---|---|
| **Trading** | **None.** `derive_bucket_bounds()` on the live token map is authoritative, and `position_manager` settles on the live event's bounds. |
| **Backtest** | **Real.** `engine.py:1000` computes bucket probabilities over the config pair, `engine.py:1321` and `report.py:226` clamp `bucket_for_temp()` to it, `observed_half_life.py:195,199` likewise. A stale bound silently moves where a replay's tail settles and what Brier scores against. |
| **Discovery** | **Latent.** The config pair is `parse_bucket_label()`'s last-resort fallback for an edge label carrying no parseable number. Today the edge buckets parse themselves, so the fallback is never reached. A safety net wants to be right. |
| **Placeholder normals** | **Real, and quiet.** 21 of the 35 stations set `long_term_normal_max_c` to the *midpoint of the config window*, and that value feeds `calibration.py:512`. See "Normal drift report" below. |

## Non-goals

1. **The trading path does not change.** Live derivation stays authoritative on every cycle. Nothing here can move a live price, size or gate.
2. **`long_term_normal_max_c` is not moved automatically.** The self-heal reports on it and changes nothing.
3. **`config.py` is not machine-edited.** No job rewrites tracked source and no bot opens a bounds PR. `config.py`'s pair becomes a first-boot seed whose job is to be reasonable, not current.

## Design

### 1. `observed_bucket_windows` — an append-only change log

New table in `polyweather.sqlite3` (`storage.py`). Both sides can read it:
`backtest/engine.py` already imports `storage` directly, as `settled_buckets`
proves.

```sql
CREATE TABLE IF NOT EXISTS observed_bucket_windows (
    station_icao TEXT NOT NULL,
    target_date  TEXT NOT NULL,
    bucket_min_c INTEGER NOT NULL,
    bucket_max_c INTEGER NOT NULL,
    bucket_unit  TEXT NOT NULL DEFAULT 'C',
    bucket_step  INTEGER NOT NULL DEFAULT 1,
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (station_icao, target_date, observed_at)
)
```

`bucket_unit`/`bucket_step` are stored **per row**, not read from the registry
at query time, for the reason `load_settled_buckets()` already documents: an
observation is immutable history and must keep describing itself if a market's
axis ever changes. Note that `bucket_min_c`/`bucket_max_c` carry the same
historical misnomer as `StationConfig` — they are expressed in `bucket_unit`,
so a KLGA row reads `68..88` with unit `F`.

**Write rule — append only on change.** A row is written only when the observed
window differs from the most recent row for that `(station, target_date)`. A
stable station-day costs exactly one row for its whole life; a day whose window
moves records the move.

This is deliberately not `INSERT OR REPLACE`. Nobody currently knows whether a
*single day's* window moves intraday, because the only record is a log line
nobody aggregates. Last-write-wins would answer the question by destroying the
evidence. The change log answers it: if intraday movement is real, a backtest
replaying an entry made at 06:00 must price it on the window listed at 06:00,
and `as_of` (below) exists for exactly that.

**Write site.** `ev_engine.run_for_station()`, at the point that logs
`BOUNDS DRIFT` today — which is *after* the malformed-map veto. That ordering is
the table's integrity guarantee: a short or gappy map returns early, so nothing
malformed can ever be recorded. The window is *evaluated* on every cycle for
every station — including cycles where it matches config, because "the window
did not drift today" is also an observation — and *written* only when it
differs from the last row, per the rule above.

`bucket_unit`/`bucket_step` at write time are the axis the map was parsed on,
`bucket_axis.for_station(station)` — the same axis `derive_bucket_bounds()` was
given as `step`. Storing them alongside the bounds is what lets a row be read
back without consulting today's registry.

Writing is wrapped so a storage failure cannot break a trading cycle: the
record is telemetry, and a cycle that trades correctly but fails to journal its
window is strictly better than one that refuses to trade.

### 2. Read API

```python
@dataclass(frozen=True)
class ObservedWindow:
    bucket_min_c: int
    bucket_max_c: int
    bucket_unit: str
    bucket_step: int
    source: str          # "observed_exact" | "observed_recent" | "config_seed"
    observed_at: Optional[str]

storage.bucket_window_for(station, target_date, as_of=None) -> ObservedWindow
```

`station` is a `StationConfig`, not an ICAO string: the third resolution step
needs the seed, and taking the object keeps the fallback inside the resolver
rather than duplicated at every call site. `as_of` is an ISO-8601 UTC string,
matching `observed_at`'s storage format.

Resolution order:

1. **`observed_exact`** — a row for this exact `(station, target_date)`. With
   `as_of` set, the row in force at that instant (latest `observed_at <= as_of`);
   without it, the latest row for the day.
2. **`observed_recent`** — no row for this day: the most recent observed window
   for this station, from any day.
3. **`config_seed`** — no observation at all for this station:
   `station.bucket_min_c`/`bucket_max_c`.

No staleness horizon and no modal smoothing. Per-day keying already absorbs most
of what smoothing existed to suppress — much of the "oscillation" in the
resweep notes (RKPK listing four windows in six days) is plausibly not one
quantity oscillating but each day's event being centred on that day's forecast.
An `observed_recent` value is by construction at least as fresh as the last
human resweep, so ageing it out in favour of `config_seed` would trade a stale
value for a staler one. The `source` field is how a caller stays honest about
which of the three it got, and the change log lets the oscillation question be
settled with data later rather than pre-empted with a knob now.

### 3. Call sites

**Correctness — a replay resolves on the window that day actually listed:**

| Site | Today |
|---|---|
| `backtest/engine.py:1000` | bucket probabilities over `station.bucket_min_c/max_c` |
| `backtest/engine.py:1321` | `resolution.bucket_for_temp()` clamp |
| `backtest/report.py:226` | Brier scoring clamp |
| `backtest/observed_half_life.py:195,199` | both clamps |

Each takes the window for that row's own `target_date`. Where the replay has a
simulated clock (`simclock`), it passes `as_of`.

**Freshness — discovery hints and `parse_bucket_label`'s last-resort fallback:**
`ev_engine.py:411`, `backtest/snapshot_collector.py:73`,
`position_manager.py:503`. These filter nothing today; the change makes the
safety net current rather than months old.

**Display only — bucket labels, no trade effect:** `pipeline.py:213,260,269`,
`executor.py:476,623,899`, `check_open_orders.py:133`, `bucket_bias.py:529`.
Switched for consistency, so no site is left reading the stale pair.

**Untouched:** `position_manager.py:641-658` already calls
`discover_token_map` + `derive_bucket_bounds` and is authoritative.
`executor.py:544` and `position_manager.py:192` are `getattr(..., 25)`
defaults inside error-path label rendering; they stay as they are.

**Manifest.** `backtest/engine.py:787`'s `station_config` block currently
records the config pair. It records instead the windows the replay actually
resolved on and a count of each `source`, so a replay stays self-describing —
a run that healed 40 of 60 station-days and fell back to config for 20 says so
on its face.

### 4. Backfill

One-time, idempotent pass over `market_tokens` in `market_data.sqlite3`:
`min(bucket_c)`/`max(bucket_c)` per `(station_icao, target_date)`, with
`discovered_at` as `observed_at`. That table has been populated by
`ev_engine._capture_snapshots()` on every live cycle since capture was enabled,
so it is a real history, not a synthetic one — and because capture also runs
after the veto, its maps are well-formed by the same argument.

Backfilled rows are subject to the same on-change rule, so a station-day that
never moved contributes one row. Rows are only written where none already
exists for that `(station, target_date, observed_at)`, making re-runs safe.

The volume is not measurable from the dev checkout — the local
`market_data.sqlite3` holds 2 station-days. It must be counted on the box before
the backfill is judged sufficient, and the count reported.

### 5. Normal drift report

21 of 35 stations carry `long_term_normal_max_c` set to the midpoint of the
config window, marked only by a comment reading
`PLACEHOLDER -- bucket-window midpoint`. SBGR is the worked example of the
failure: its window fell 10 °C between August and September, so its "climate
normal" — a live input to `calibration.py:512` — was 5 °C wrong until a human
noticed months later.

Freezing `config.py`'s bounds makes that staleness *invisible* rather than
merely quiet, because the midpoints stop being refreshed as a side effect of
the bounds resweep. The report is the compensating control.

**Turn the comment into data.** Add `StationConfig.normal_is_bucket_midpoint:
bool = False`, set `True` on the 21 placeholder stations. A report cannot key
off a comment, and a field states in the registry what the comment states in
prose.

**The report** — a standalone read-only script in the established
`spread_audit.py` / `stop_loss_audit.py` mould. For each flagged station it
prints the registered normal, the midpoint of the healed current window, and
the gap, sorted worst first.

The midpoint must be **converted to Celsius when `bucket_unit` is `F`**.
`long_term_normal_max_c` is Celsius for every station, while 11 of the 15
Americas cities carry a °F axis in 2 °F buckets — KLGA's registered 25.6 is
78 °F, the midpoint of its 68..88 window. Comparing 78 to 25.6 directly would
report every US station as catastrophically drifted.

**It changes nothing.** No write, no config edit, no gate. Its output is a
table for a human to act on.

## Consequences and risks

**Past backtest numbers move.** Every replay figure recorded to date — P&L,
Brier, the take-profit and stop sweeps — was computed with tails clamped to
config's stale pair. After this, historical days resolve on the window they
actually had. That is the point of the change, and it means pre- and post-
figures are not comparable. Mitigation: re-run one known replay both ways and
report the delta explicitly rather than silently rebasing the numbers. If the
delta is large, that is a finding about how much the stale clamp has been
distorting every conclusion drawn from the backtest, and it belongs in the
memory files.

**Healing is partial for old days.** Days before snapshot capture began have no
observation and fall back to `config_seed`. A replay spanning that boundary is
half-healed; the manifest's `source` counts are what make that legible.

**Graceful degradation.** If the live path stops writing (daemon down, capture
disabled), the resolver falls through `observed_recent` to `config_seed` — the
current behaviour. The mechanism cannot fail closed in a way that blocks
trading, because the trading path never reads it.

**Unknown, and deliberately so.** Whether a single day's window moves intraday.
The change log measures it. Until there is an answer, `as_of` is plumbed but
its effect is expected to be nil.

## Testing

TDD, per layer:

1. **Write dedup** — an unchanged window writes no second row; a changed one appends; the first observation of a station-day always writes.
2. **Write-site ordering** — a malformed map (short, gappy, off-grid) records nothing, because the veto returns before the write.
3. **Write-site isolation** — a storage exception inside the record call does not fail the cycle.
4. **Resolution order** — exact, then recent, then seed; each returns the right `source`.
5. **`as_of`** — with two rows for one station-day, the earlier `as_of` gets the earlier window.
6. **Unit/step fidelity** — an F-axis row round-trips `unit='F'`, `step=2` regardless of what the registry says at read time.
7. **Backfill** — derives the right per-day window from `market_tokens`, is idempotent across two runs, and honours the on-change rule.
8. **Replay fixture** — a station-day whose observed window differs from config resolves and Brier-scores on the observed one; the manifest reports the source counts.
9. **Normal drift report** — flags only `normal_is_bucket_midpoint` stations; converts an F-axis midpoint to Celsius before comparing (KLGA 68..88 → 25.6, gap 0.0, not 52.4); writes nothing.

## Follow-ups, not in scope

- Whether the observed record should eventually *replace* `config.py`'s pair rather than shadow it. Keeping the seed costs one number per station and keeps a fresh checkout able to trade before it has observed anything.
- Whether `long_term_normal_max_c` should track the healed midpoint automatically. Deliberately deferred: it is a live calibration input, and the report is the cheap way to learn how often it would have moved before granting it the right to move itself.
