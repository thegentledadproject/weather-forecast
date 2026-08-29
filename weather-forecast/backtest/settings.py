"""
backtest/settings.py

PURPOSE
-------
Backtest-only knobs. Everything here describes the SIMULATION -- how
the replay reads history, how strict it is about stale quotes, where
its own database lives. Nothing here redefines a trading constant:
bankroll, bucket edges, risk thresholds and the schedule all come
FROM config.py, so a backtest can never quietly disagree with the live
system about what the strategy actually is. If a number below looks
like a strategy parameter, it is a bug -- move it to config.py.

WHY A SEPARATE DATABASE
-----------------------
MARKET_DATA_DB is deliberately NOT config.DB_PATH. Historical price
history is bulk research data, written by fetchers that may run for
hours and be re-run freely; the live trading db holds positions that
represent real (or paper-tracked) money. Keeping them in separate
files means no backtest fetch, schema change or accidental DELETE can
ever touch the live positions table.

DEPENDENCIES
------------
config.py (local)
"""

import config

# DEFAULT local-time offset only -- no longer the offset every station is
# replayed at. simclock is parameterized per station as of 2026-08-10
# (design C5): engine.run() reads station.utc_offset_hours and threads it
# through SimClock, local_minute_to_ts() and generate_ticks(), and a run's
# manifest records the offset it actually used as sim_utc_offset_hours.
#
# Still matches risk_manager._local_hour()'s tz_offset_hours default and
# scheduler.local_now()'s, both of which hardcode 8 as their own
# station-agnostic fallback. This value is what a caller gets when it names
# no station at all -- the synthetic test scenario, mainly.
LOCAL_UTC_OFFSET_HOURS = 8

# An observation for target date D is not knowable until D+1 local: the
# daily max is only final once the day is over, and the official sources
# publish the confirmed figure the following morning. Encoding it here
# (rather than assuming same-day knowledge) is the look-ahead guard on
# the RESOLUTION side, mirroring price_store.get_price_at()'s guard on
# the quote side.
OBS_PUBLISH_LAG_DAYS = 1

# How the simulator is allowed to assume order-book depth existed at a
# moment we only have a price for:
#   "strict"          -- only trade where real depth was recorded. Any
#                        tick without depth is untradeable. The honest
#                        default: it under-trades rather than inventing
#                        liquidity that may never have been there.
#   "observed_median" -- substitute the station's median observed depth
#                        where depth is missing.
#   "pessimistic"     -- substitute a deliberately thin fixed figure
#                        (PESSIMISTIC_DEPTH_USD) as a stress case.
#   "unconstrained"   -- ignore depth entirely. Useful only for isolating
#                        how much of a result is liquidity-driven; its
#                        P&L is not a claim about achievable fills.
DEFAULT_DEPTH_REGIME = "strict"
DEPTH_REGIMES = ("strict", "observed_median", "pessimistic", "unconstrained")

PESSIMISTIC_DEPTH_USD = 300.0

# Minimum fraction of simulated ticks that must have usable depth data
# before a depth-constrained run's results are worth reporting at all.
#
# MUST stay at or below the structural ceiling set by
# ev_engine.DEPTH_CAPTURE_EVERY_N_PASSES: capture fetches order-book depth
# on every Nth pass, so at N=3 at most ~1/3 of captured rows can ever
# carry depth (observed 28-30%/day since 2026-08-06). The original 0.5 was
# therefore unsatisfiable -- it suppressed the headline metrics of every
# observed-depth run no matter how much data accumulated. 0.25 sits under
# the ceiling while still refusing runs built on almost no depth at all,
# which is what this gate is actually for. Raising it above ~0.30 requires
# lowering N first (and paying 3x the /book traffic for backtest-only data).
MIN_DEPTH_COVERAGE = 0.25

# Backtest fee assumption. 0.0 matches ev_engine's current live default;
# override per-run to test fee sensitivity rather than editing this.
DEFAULT_FEE_RATE_PCT = 0.0

# A price tick older than (factor x its own observed fidelity) is treated
# as NO QUOTE rather than as a usable price. Without this, a gap in the
# history silently becomes "the price never moved", which manufactures
# both fills and hold-throughs that could not have happened.
MAX_STALENESS_FACTOR = 2.0

# Assumed sampling resolution, in minutes, when a source doesn't report one.
DEFAULT_SNAPSHOT_FIDELITY_MIN = 5

MARKET_DATA_DB = config.DATA_DIR / "market_data.sqlite3"
BACKTESTS_OUT_DIR = config.DATA_DIR / "backtests"

# First tick of each simulated LOCAL day. Verified against
# config.SCHEDULE_WINDOWS: the 00:00-04:00 window is mode "closed" with no
# interval, and the first window that does anything ("collection") starts at
# 04:00 -- so 4 is exactly when a live daemon first wakes, not an
# approximation of it.
SIM_DAY_START_HOUR_LOCAL = 4
