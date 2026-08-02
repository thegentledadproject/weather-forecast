"""
backtest/entry_sim.py

PURPOSE
-------
entry_manager.evaluate_entry(), with every input injected instead of
fetched. Same gates, same order, same constants, same reason strings --
but no storage query, no market_client call, no wall clock. That makes
it replayable at a simulated instant, and it makes a parity test
possible: feed the same EVResult to both functions with matching stubs
and every EntryDecision field should agree.

WHY A REPLICA AND NOT A CALL
----------------------------
evaluate_entry() reaches out to storage.load_open_positions() and
market_client.get_available_depth_usd()/estimate_slippage() from inside
its own body. Monkeypatching those for a replay would mean the backtest
silently depends on the live module's import-time wiring, and any future
refactor there would break replays in ways that look like strategy
results rather than like bugs. Replicating the decision sequence in a
pure function, and REUSING the parts that are already pure, keeps the
seam explicit and testable.

Reused verbatim from entry_manager (imported, never copied):
  - compute_kelly_fraction()
  - veto_same_bucket_conflicts()
  - apply_portfolio_budget()
and from ev_engine:
  - best_opportunities()   (used by the engine, re-exported here for the
                            parity test's convenience)

THE GATE SEQUENCE (GATE_COUNT distinct decision sites)
------------------------------------------------------
Exactly the order live runs them in. Order matters: an earlier gate's
rejection carries different (mostly empty) sizing fields than a later
one's, so a reordered replica would produce EntryDecisions that differ
from live even when the approve/reject verdict matched.

  1. Veto 0a  raw edge > config.MAX_PLAUSIBLE_RAW_EDGE  -> reject
  2. Veto 0b  open-position count unreadable (None)      -> reject
  3. Veto 0b  open count >= MAX_OPEN_POSITIONS_PER_BUCKET-> reject
  4. Kelly fraction missing or <= 0                      -> reject
  5. depth unknown (None)                                -> reject
  6. depth-capped size < $1                              -> reject
  7. slippage at size > MAX_ACCEPTABLE_SLIPPAGE_PCT      -> reject
  8. net EV at size < min_net_ev                         -> reject
  9. approve

LIVE QUIRKS REPLICATED, NOT FIXED
---------------------------------
  - (FIXED live on 2026-08-02, and mirrored here the same day.)
    apply_portfolio_budget() originally capped only the candidates
    decided in THIS cycle -- blind to earlier cycles' entries, making
    config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD a per-cycle cap in
    practice. Both sides now deduct dollars already deployed into the
    same (station, target date), open AND closed: live reads them via
    entry_manager.station_day_exposure_usd(), the sim passes the same
    figure from PortfolioState through existing_exposure_usd below.
  - The per-bucket cap counts (station, target_date, bucket, SIDE) --
    side-specific. Note backtest/portfolio.py's count_open_for_bucket()
    counts across BOTH sides, which is stricter than live; this module
    counts side-specifically to match entry_manager, and
    decide_portfolio_entries_sim() does its own counting rather than
    using that helper.
  - Gate 7's hard slippage bar is effectively unreachable once depth is
    known, because gate 6 has already capped size at 25% of depth. See
    backtest/fill_model.py's docstring -- this is true of the live path
    too, for its own reasons.

DEPENDENCIES
------------
typing (standard library)
config.py, models.py (local)
entry_manager.py, ev_engine.py (local -- reused, not reimplemented)
"""

from typing import Callable, List, Optional, Sequence, Tuple

import config
import entry_manager
from models import EVResult, EntryDecision

# Reused live logic. Imported by name so a reader can see at a glance
# exactly which parts of entry_manager the replay shares rather than
# reimplements -- and so a rename upstream breaks the import loudly.
from entry_manager import (
    compute_kelly_fraction,
    veto_same_bucket_conflicts,
    apply_portfolio_budget,
)
from ev_engine import best_opportunities  # noqa: F401  (re-exported for tests)

# Number of distinct decision/rejection sites replicated from
# entry_manager.evaluate_entry(). Equals the number of decision-returning
# statements in that function (its three `return _rejected(...)` calls
# plus its six direct `return EntryDecision(...)` statements). An AST
# census over the live function must agree with this number; if it does
# not, a gate was added or removed upstream and this replica is stale.
GATE_COUNT = 9


def _maturity_for(station_icao: str, station_maturity: Optional[str]) -> str:
    """
    Live derivation: config.STATION_MATURITY.get(icao, "exploratory") --
    an unregistered station is exploratory, never mature, so a new market
    is sized down by default rather than up.

    station_maturity overrides it, for tests and for what-if runs that ask
    "what would this station have made once it earned mature status".
    """
    if station_maturity is not None:
        return station_maturity
    return config.STATION_MATURITY.get(station_icao, "exploratory")


def evaluate_entry_sim(
    ev: EVResult,
    token_id: str,
    open_count_for_bucket: Optional[int],
    depth_usd: Optional[float],
    slippage_fn: Callable[[float], float],
    min_net_ev: float,
    sizing_bankroll: float,
    station_maturity: Optional[str] = None,
) -> EntryDecision:
    """
    Pure replica of entry_manager.evaluate_entry().

    Injected in place of live I/O:
      open_count_for_bucket  <- storage.load_open_positions() count.
                                None replicates the live "could not read
                                open positions" case, which is a REJECT
                                (refuse to open blind), not a zero.
      depth_usd              <- market_client.get_available_depth_usd().
                                None is a reject, same as live.
      slippage_fn(size_usd)  <- market_client.estimate_slippage(token, size).
      sizing_bankroll        <- config.BANKROLL_USD. Injected so a
                                compounding-bankroll run can vary it;
                                pass config.BANKROLL_USD for live parity.
    """
    station_icao = ev.station_icao
    maturity = _maturity_for(station_icao, station_maturity)

    def _rejected(reason: str) -> EntryDecision:
        """Uniform pre-sizing rejection shape -- mirrors evaluate_entry's nested _rejected()."""
        return EntryDecision(
            station_icao=station_icao, target_date=ev.target_date,
            bucket_c=ev.bucket_c, side=ev.side,
            kelly_fraction_raw=0.0, kelly_fraction_applied=0.0,
            recommended_size_usd=0.0, available_depth_usd=None,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason=reason,
            station_maturity=maturity,
            entry_price=ev.market_price,
            token_id=token_id,
        )

    # --- Gate 1: Veto 0a, edge plausibility ------------------------------
    raw_edge = ev.raw_edge
    if raw_edge is not None and abs(raw_edge) > config.MAX_PLAUSIBLE_RAW_EDGE:
        return _rejected(
            f"VETOED: raw edge {raw_edge:+.1%} exceeds MAX_PLAUSIBLE_RAW_EDGE "
            f"({config.MAX_PLAUSIBLE_RAW_EDGE:.0%}) -- presumed data error, not alpha."
        )

    # --- Gate 2: Veto 0b, open positions unreadable ----------------------
    if open_count_for_bucket is None:
        return _rejected("Open positions unreadable -- per-bucket cap unenforceable, refusing to open blind.")

    # --- Gate 3: Veto 0b, per-bucket cap ---------------------------------
    if open_count_for_bucket >= config.MAX_OPEN_POSITIONS_PER_BUCKET:
        return _rejected(
            f"Per-bucket cap: {open_count_for_bucket} position(s) already open on this bucket/side "
            f"(max {config.MAX_OPEN_POSITIONS_PER_BUCKET})."
        )

    # --- Gate 4: Kelly fraction ------------------------------------------
    kelly_raw = compute_kelly_fraction(ev)
    if kelly_raw is None or kelly_raw <= 0:
        return EntryDecision(
            station_icao=station_icao, target_date=ev.target_date,
            bucket_c=ev.bucket_c, side=ev.side,
            kelly_fraction_raw=kelly_raw or 0.0, kelly_fraction_applied=0.0,
            recommended_size_usd=0.0, available_depth_usd=None,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason="No positive edge (Kelly fraction <= 0).",
            station_maturity=maturity,
            entry_price=ev.market_price,
            token_id=token_id,
        )

    kelly_applied = kelly_raw * config.KELLY_FRACTION
    bankroll_sized_usd = kelly_applied * sizing_bankroll

    # Cap 1: hard per-trade ceiling
    size_usd = min(bankroll_sized_usd, config.MAX_POSITION_USD)

    # Cap 2: station maturity -- exploratory stations sized down hard
    if maturity == "exploratory":
        size_usd *= config.EXPLORATORY_SIZE_MULTIPLIER

    # --- Gate 5: depth unknown -------------------------------------------
    if depth_usd is None:
        return EntryDecision(
            station_icao=station_icao, target_date=ev.target_date,
            bucket_c=ev.bucket_c, side=ev.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=0.0, available_depth_usd=None,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason="Order book depth unavailable -- cannot size safely, skipping.",
            station_maturity=maturity,
            entry_price=ev.market_price,
            token_id=token_id,
        )

    # Cap 3: real order-book depth
    depth_capped_usd = min(size_usd, depth_usd * config.MAX_DEPTH_UTILIZATION_PCT)

    # --- Gate 6: depth-capped size too small ------------------------------
    if depth_capped_usd < 1.0:
        return EntryDecision(
            station_icao=station_icao, target_date=ev.target_date,
            bucket_c=ev.bucket_c, side=ev.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=0.0, available_depth_usd=depth_usd,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason=f"Depth-capped size (${depth_capped_usd:.2f}) too small to trade -- book too thin.",
            station_maturity=maturity,
            entry_price=ev.market_price,
            token_id=token_id,
        )

    # Re-check slippage and net EV at the ACTUAL recommended size, not the
    # flat screening size ev_engine used -- identical arithmetic to live.
    slippage_at_size = slippage_fn(depth_capped_usd)
    net_ev_at_size = (ev.raw_edge / ev.market_price) - slippage_at_size - ev.fee_rate_pct

    # --- Gate 7: hard slippage bar ----------------------------------------
    if slippage_at_size > config.MAX_ACCEPTABLE_SLIPPAGE_PCT:
        return EntryDecision(
            station_icao=station_icao, target_date=ev.target_date,
            bucket_c=ev.bucket_c, side=ev.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=depth_capped_usd, available_depth_usd=depth_usd,
            slippage_at_size_pct=slippage_at_size, net_ev_at_size=net_ev_at_size,
            approved=False,
            reason=f"Slippage at this size ({slippage_at_size:.1%}) exceeds the {config.MAX_ACCEPTABLE_SLIPPAGE_PCT:.0%} hard gate -- book too thin to trust the fill.",
            station_maturity=maturity,
            entry_price=ev.market_price,
            token_id=token_id,
        )

    # --- Gate 8: net EV at actual size ------------------------------------
    if net_ev_at_size < min_net_ev:
        return EntryDecision(
            station_icao=station_icao, target_date=ev.target_date,
            bucket_c=ev.bucket_c, side=ev.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=depth_capped_usd, available_depth_usd=depth_usd,
            slippage_at_size_pct=slippage_at_size, net_ev_at_size=net_ev_at_size,
            approved=False,
            reason=f"Net EV at actual size ({net_ev_at_size:+.1%}) no longer clears the {min_net_ev:.0%} threshold once real slippage is applied.",
            station_maturity=maturity,
            entry_price=ev.market_price,
            token_id=token_id,
        )

    # --- Gate 9: approve ---------------------------------------------------
    return EntryDecision(
        station_icao=station_icao, target_date=ev.target_date,
        bucket_c=ev.bucket_c, side=ev.side,
        kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
        recommended_size_usd=round(depth_capped_usd, 2), available_depth_usd=depth_usd,
        slippage_at_size_pct=slippage_at_size, net_ev_at_size=net_ev_at_size,
        approved=True,
        reason=f"Approved: {net_ev_at_size:+.1%} net EV at ${depth_capped_usd:.2f} ({maturity} station).",
        station_maturity=maturity,
        entry_price=ev.market_price,
        token_id=token_id,
    )


def count_open_for_bucket_side(portfolio, station_icao: str, target_date, bucket_c: int, side: str) -> int:
    """
    Open positions on this exact (station, target_date, bucket, SIDE) in a
    replay portfolio -- the injected stand-in for
    entry_manager.count_open_positions_for_bucket()'s storage query.

    Side-specific ON PURPOSE. backtest/portfolio.py's
    count_open_for_bucket() deliberately counts both sides together, which
    is a stricter rule than the live system enforces; using it here would
    make the replay refuse entries the live system takes. Live's paper/real
    scoping needs no equivalent because every replay position is paper.
    """
    side_upper = side.upper()
    return sum(
        1 for p in portfolio.open.values()
        if p.station_icao == station_icao
        and p.target_date == target_date
        and p.bucket_c == bucket_c
        and p.side.upper() == side_upper
    )


def decide_portfolio_entries_sim(
    candidates: Sequence[Tuple[EVResult, str]],
    portfolio,
    fill_model,
    price_lookup: Callable[[str], Optional[dict]],
    min_net_ev: float,
    sizing_bankroll: float,
    station_maturity: Optional[str] = None,
    existing_exposure_usd: float = 0.0,
) -> List[EntryDecision]:
    """
    Replica of entry_manager.decide_portfolio_entries(), in the same three
    stages and the same order:

      1. per-leg evaluate_entry_sim()   (replicated)
      2. veto_same_bucket_conflicts()   (live function, reused)
      3. apply_portfolio_budget()       (live function, reused, fed the
         dollars this station/day already spent -- the engine computes it
         from PortfolioState exactly as live computes it from storage via
         entry_manager.station_day_exposure_usd())

    candidates are (EVResult, token_id) pairs, already screened by
    ev_engine.best_opportunities() exactly as the live scheduler does.
    Callers should pass them in a deterministic order -- the engine sorts
    by (bucket_c, side) -- because stage 2 groups by bucket and stage 3
    scales proportionally, and both preserve input order.

    IMPORTANT (and identical to live): the per-bucket open count is read
    ONCE per candidate, from the portfolio as it stands BEFORE any of this
    cycle's approvals are booked. Live has the same property, because
    executor.open_position() only runs after every decision is made. Two
    legs on the same bucket/side in one cycle therefore both see count 0 --
    which is precisely why stage 2's same-bucket veto exists.

    Returns EVERY decision, approved and rejected, with .approved/.reason
    intact so the engine can build a rejection funnel instead of silently
    dropping candidates.
    """
    decisions: List[EntryDecision] = []

    for ev, token_id in candidates:
        snapshot = price_lookup(token_id)
        depth_usd = fill_model.depth_at(snapshot)
        open_count = count_open_for_bucket_side(
            portfolio, ev.station_icao, ev.target_date, ev.bucket_c, ev.side
        )

        def _slippage_fn(size_usd: float, _depth=depth_usd) -> float:
            # _depth bound at definition time: a late-binding closure over
            # the loop variable would price every leg off the LAST leg's
            # depth, which is the kind of bug that shows up as an
            # inexplicably consistent backtest.
            return fill_model.slippage(size_usd, _depth)

        decisions.append(
            evaluate_entry_sim(
                ev=ev,
                token_id=token_id,
                open_count_for_bucket=open_count,
                depth_usd=depth_usd,
                slippage_fn=_slippage_fn,
                min_net_ev=min_net_ev,
                sizing_bankroll=sizing_bankroll,
                station_maturity=station_maturity,
            )
        )

    decisions = veto_same_bucket_conflicts(decisions)
    decisions = apply_portfolio_budget(decisions, existing_exposure_usd=existing_exposure_usd)
    return decisions
