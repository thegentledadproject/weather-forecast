# Evidence-first remediation — design

Date: 2026-09-17. Status: approved 2026-09-17; Wave 1 MERGED 0dcc5f6 + DEPLOYED 2026-09-17; Wave 2 MERGED f6f8aac + DEPLOYED 2026-09-19 08:45 UTC (regime boundary 2026-09-20); Wave 3 MERGED 1c32d1f + DEPLOYED 2026-09-23 14:40 UTC (no regime boundary); checkpoint ~2026-10-04.

Source: the six read-only reviews of 2026-09-15 (architecture, trade
logic, edge, stop/take, scheduler timing, paper-vs-live). Their findings
and production measurements are recorded in the session memory files
`*-review-2026-09-15.md`; this document does not restate the evidence,
only what is being built and why.

## Goal

Make the evidence trustworthy before changing what the system trades on.
Over ~4 weeks: (1) record the numbers that actually decide each trade,
(2) correct the statistics those numbers are computed from, on one day,
(3) stop the data from corrupting itself, then (4) re-measure and decide
the big questions (edge redefinition, live hold-to-settlement,
redemption) on clean rows.

Out of scope for every wave here: the lambda-shrunk edge redefinition,
`HOLD_TO_SETTLEMENT_MODES`, redemption execution, and funding the wallet.
Those are checkpoint decisions (section 6), not plan items.

## Principles

1. **One wave = one merge + one deploy**, so each wave has exactly one
   date in the data.
2. **Wave 1 adds rows only.** No branch it touches may change
   `EntryDecision.approved`, `recommended_size_usd`, or any exit
   decision. A test asserts the entry path's decisions are identical
   with recording on and off.
3. **Wave 2 lands on one day.** Its date is stamped in
   `config.REGIME_BOUNDARIES` and every cohort report splits at it.
4. **Nothing here touches live-money semantics.**
5. Deploys use the existing `deploy_daemon.sh`; any wave that adds
   schema uses backup-and-stop (the P1-8(b) shape). Never deploy during
   05:00–08:00 SGT.

## Wave 1 — record what decided the trade

Target: merged and deployed by 2026-09-19 (the ADMIT falsifier read).

### 1a. Persist the deciding numbers on the position

`EntryDecision` and `Position` gain, and `positions` gets columns for:

| field | meaning |
|---|---|
| `calibrated_prob REAL` | the P3-6 map output used for sizing / admission; NULL when uncalibrated |
| `calibration_source TEXT` | the map tier (`station_isotonic`, `pooled_isotonic`, `uncalibrated`, …) exactly as `probability_calibration` names it |
| `admission_edge REAL` | the edge veto 0a2 actually compared (calibrated when `ADMIT_ON_CALIBRATED_EDGE`, else raw) |
| `sizing_edge REAL` | the edge Kelly actually used |
| `kelly_size_preclamp_usd REAL` | `recommended_size_usd` before the live `$1.00` clamp and before the exchange 5-share bump — the paper-equivalent stake for a live ticket |

`net_ev_at_size` on live rows becomes the figure `_resolved_size_ok`
computed at the resolved size (today it stores the `$1.00` figure).

Existing columns keep their meaning: `model_prob` and `raw_edge` stay
raw, because the isotonic map is fitted on `model_prob`.

Migration: append to the column list in `storage._connect()` (the
established idempotent pattern). Pre-existing rows are NULL — not
backfilled; the map that would have applied is not reconstructible
honestly.

### 1b. `entry_decisions` table

One row per `EntryDecision` per cycle, approved or not:

```
cycle_ts TEXT NOT NULL, station_icao TEXT NOT NULL, target_date TEXT NOT NULL,
bucket_c INTEGER NOT NULL, side TEXT NOT NULL,
book TEXT NOT NULL,            -- 'paper' | 'live' | 'simulation' | 'manual_review' | 'paper_shadow'
approved INTEGER NOT NULL, rule_id TEXT NOT NULL, reason TEXT NOT NULL,
entry_price REAL, entry_bid REAL, model_prob REAL, calibrated_prob REAL,
calibration_source TEXT, raw_edge REAL, admission_edge REAL, sizing_edge REAL,
net_ev_at_size REAL, kelly_size_preclamp_usd REAL, recommended_size_usd REAL,
station_maturity TEXT, config_sha TEXT
```

`rule_id` is a stable code set at each `return EntryDecision(...)` site
in `entry_manager.evaluate_entry` and `apply_portfolio_budget` (veto ids
`00`, `00b`, `00c`, `0a`, `0a2`, `0b`, `0b2`, `0c`, `collection_gate`,
`depth`, `slippage`, `kelly_nonpositive`, `size_floor`, `net_ev_bar`,
`budget_scaled`, `approved`). The prose `reason` is kept beside it;
nothing parses prose any more. `backtest/entry_sim.py` sets the same
ids so the replay funnel and the live funnel share a key.

Written by `scheduler._run_full_cycle` immediately after
`decide_portfolio_entries` returns, before any executor call. The write
is best-effort: a storage failure logs and continues, it never blocks a
trade.

Scope limit, stated: candidates refused *before* `evaluate_entry` (the
`best_opportunities` screen) do not get rows. The universe at that level
is already captured per cycle in `ev_snapshots`, from which the screen
is reconstructible.

### 1c. Executor refusals become rows

Every executor-level refusal that today is only a `print` — exchange
reconciliation, region/live concurrent and exposure caps, orders-per-day
cap, day-budget breach at resolved size, resolved-size net-EV/depth/
slippage re-checks, drift — is recorded through the existing
`storage.record_live_order_attempt` with `outcome='refused'` and
`detail=<reason code>: <message>`. Journal lines are unchanged.

### 1d. Paper shadow twin for live stations

For a station whose `executor.EXECUTION_MODE` is `live`,
`_run_full_cycle` runs a second, read-only evaluation of the same cycle
with `execution_mode='paper'` (paper EV table, paper gates, paper
sizing, paper budget view) and records every resulting `EntryDecision`
in `entry_decisions` with `book='paper_shadow'`. The shadow pass:

- never calls `executor.open_position` (an AST test asserts it);
- never writes `positions`;
- reads the paper book's budget/cap state but does not consume it;
- is skipped, with a log line, if the primary pass raised.

Pairing for analysis: `entry_decisions(book='paper_shadow',
approved=1)` ⋈ `positions(execution_mode='live')` on
(station, target_date, bucket_c, side). Hold-to-settlement value of a
shadow decision = `entry_price`, `recommended_size_usd`, and the winner
in `settled_buckets`; no Position row is needed because paper holds.

### 1e. Tests

- Decision-identity test: `decide_portfolio_entries` on a fixture
  returns the same `(approved, recommended_size_usd, reason)` tuple set
  with `entry_decisions` recording enabled and disabled.
- Field-parity test: every field on `EntryDecision` that names a
  deciding number is present on `entry_decisions`.
- `rule_id` census: every `return EntryDecision(` in `evaluate_entry`
  and `apply_portfolio_budget` passes a `rule_id` (extends
  `tests/test_gate_census.py`); `entry_sim.py` uses the same id set.
- Shadow guard: AST test that the shadow pass's call graph does not
  reach `executor.open_position` or `storage.open_position`.
- Migration test: `_connect()` on a pre-Wave-1 schema adds the five
  columns and the table, idempotently.
- Refusal-row test: each executor refusal site records one
  `live_order_attempts` row with `outcome='refused'`.

### 1f. Falsifier (run the day of deploy, appended to memory)

- `SELECT COUNT(*) FROM entry_decisions WHERE approved=0` grows every
  entry cycle.
- `SELECT COUNT(*) FROM positions WHERE calibrated_prob IS NULL AND
  calibration_source != 'uncalibrated' AND entry_time > <deploy_ts>`
  is 0.
- `SELECT COUNT(*) FROM entry_decisions WHERE book='paper_shadow'` is
  > 0 once a live station has run one entry cycle (WSSS/RCSS at
  05:00 SGT), even with the wallet unfunded.
- Entry count per day and per-station approval rate are unchanged
  versus the 7 days before deploy (record-only wave).

## Wave 2 — correct the inputs, one day

Target: ~2026-09-21; DEPLOYED 2026-09-19 (in the 15:00-19:00Z gap when no region's entry window is open); regime boundary 2026-09-20. All items ship in one merge. Each has an on/off
constant defaulting to on, so a revert is a config flip.

| id | change | can only refuse? |
|---|---|---|
| 2a | Bias / RMSE / spread error sample restricted to forecast rows fetched inside `ERROR_SAMPLE_FETCH_WINDOW_LOCAL = (4, 8)`; `storage.py` comment at the day filter corrected | no |
| 2b | `SPREAD_FLOOR_C` applies only to the `corrected_error` tier (n>=15, visible to the `MAX_ERROR_RMSE_PER_BUCKET` upper gate), with a numerical-sanity minimum `MEASURED_SPREAD_MIN_C = 0.30`; the naive `measured_error` tier (5-14 pairs) keeps the floor because the gate fails open below 15 residuals | no |
| 2c | Veto 0a2 signed compare on the calibrated basis (`gate_edge < min_abs_edge`); mirrored in `entry_sim.py` | yes |
| 2d | `probability_calibration` does not cache a failed fit; retries next cycle | no — a retry restores the calibrated path mid-day (stricter admission, but the gap haircut retires on calibrated books, so sizing can grow); this is the already-shipped P3-6 design, not a new loosening |
| 2e | Total forecast outage sets `today_source_mix = frozenset()` so the mix guard refuses | yes |
| 2f | `entry_sim.py` haircut and exit-fee parity with live (`_book_has_stop` threaded through) | backtest only |

`config.REGIME_BOUNDARIES = ("2026-09-20",)` (the first target date every station decides on the new code — Asia's 09-19 window ran 20:00-00:00Z before the deploy; the boundary day belongs to the new regime);
`cohort_monitor`, `calibration_panel` and `promotion_dossier` report
each side of a boundary separately by default.

Pre-registered reads at Wave 2 + 14 days: morning-only and all-day sd
converge (2a); EDDM / RKPK / WSSS priced sd ≠ 0.700 (2b); c / lambda
refit on post-boundary `ev_snapshots`; bought-side calibration gap on
the stored `calibrated_prob`.

Stop condition: if the 14-day held-to-settlement return on
post-boundary paper rows is worse than the pre-boundary 14 days by more
than the day-clustered CI, flip 2a and 2b off together.

## Wave 3 — stop the data from corrupting itself

Target: ~2026-09-25.

- 3a `storage.migrate()` is explicit, run by the daemon at boot and by
  `deploy_daemon.sh`; `_connect()` issues no DDL; non-daemon processes
  open `mode=ro` unless they pass `writable=True`.
- 3b `User=ubuntu` on `polyweather-dashboard.service`; dashboards open
  read-only.
- 3c `_run_full_cycle` runs the exit check before the entry leg; a
  past-dated position with no price goes straight to
  `_market_reported_closed`.
- 3d `position_manager._local_hour_for` and `backtest/simclock.py` use
  `config.current_utc_offset_hours`.
- 3e Stale-docs sweep: "both tighten", "skip BOTH", trailing-stop
  references, the `storage.py` day-filter comment.
- 3f Read-only spike: confirm the `NegRiskAdapter.redeemPositions`
  amounts-array shape against the deployed contract ABI. No
  transaction. One paragraph, gates any future redemption work.

## Checkpoint — Wave 2 + 14 days (~2026-10-05)

One run each of `cohort_monitor`, `calibration_panel` and the four
pre-registered reads on post-boundary rows only, plus the shadow-twin
pairing. Decisions taken only here: lambda (yes / no / per-band),
`"live"` in `HOLD_TO_SETTLEMENT_MODES`, redemption fix and funding.

## Rollout, per wave

branch → TDD → full test suite locally (never on the box) → merge to
main → `deploy_daemon.sh` (backup-and-stop when schema changes) → box
`git log -1` == main → falsifier queries the same day → memory note.
