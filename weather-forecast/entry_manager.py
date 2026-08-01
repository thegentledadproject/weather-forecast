"""
entry_manager.py

PURPOSE
-------
The gap flagged repeatedly across the last several summaries: nothing
decided WHEN to open a position. ev_engine.py can say a bucket looks
mispriced; risk_manager.py/position_manager.py can manage a position
once it exists; nothing sat between them turning "this looks good" into
"here's exactly how much to trade, or why not to."

Two vetoes run BEFORE any sizing, since neither can be fixed by trading
smaller:
  0a. Edge plausibility -- a raw edge past config.MAX_PLAUSIBLE_RAW_EDGE
      is a data error, not alpha, and is rejected outright. This is not
      hypothetical: a price-inversion bug produced "edges" of 0.88 and
      net EVs of +1298% that cleared every EV, slippage and sizing gate
      below and traded real money, because nothing asked whether an edge
      that large was believable in the first place.
  0b. Per-bucket position cap -- config.MAX_OPEN_POSITIONS_PER_BUCKET
      open positions on the same (station, date, bucket, side) already
      means this bet is on. Repeat entries across cycles are not extra
      opportunities, they are one bet accidentally sized up past every
      per-trade cap below.

Then four checks, in order, for every surviving candidate EVResult:
  1. Kelly-fraction sizing off the real edge (raw_edge / (1 - price)),
     scaled down to fractional Kelly -- see KELLY_FRACTION in config.py
     for why full Kelly is deliberately not used.
  2. Cap by MAX_POSITION_USD -- a hard ceiling independent of how large
     Kelly sizing alone would suggest.
  3. Cap by real order-book depth (MAX_DEPTH_UTILIZATION_PCT of visible
     liquidity within a 10% price-impact band) -- sizing off Kelly alone
     ignores whether the book can actually fill the trade without either
     failing or moving the price against you.
  4. Station-maturity gating -- WMKK gets sized down hard (see
     EXPLORATORY_SIZE_MULTIPLIER) since it has no confirmed bias-correction
     history yet, unlike WSSS.

After sizing, slippage and net EV are RE-CHECKED at the actual
recommended size (not the flat $50 test size ev_engine.py used to
screen candidates) -- a trade that looked good at $50 might not still
clear the bar at $150, or might clear it more easily at a smaller size
if depth is thin. If the re-checked net EV drops below the threshold,
or slippage exceeds MAX_ACCEPTABLE_SLIPPAGE_PCT, the trade is rejected
even though it passed ev_engine's initial screen.

This module still only RECOMMENDS -- nothing here places an order.
executor.py's manual_review/auto gating still applies downstream.

DEPENDENCIES
------------
config.py, models.py, storage.py, executor.py (local)
clients/market_client.py (local)
"""

from datetime import date
from typing import List, Optional

import config
import storage
import executor
from models import EVResult, EntryDecision
from clients import market_client


def compute_kelly_fraction(ev_result: EVResult) -> Optional[float]:
    """
    Full-Kelly fraction for this bet: raw_edge / (1 - price). Derived
    and numerically verified against the standard binary-Kelly formula
    f* = q - (1-q)/b for odds b = (1-p)/p, which simplifies to exactly
    this. Returns None if price/edge data is missing.
    """
    if ev_result.market_price is None or ev_result.raw_edge is None:
        return None
    if ev_result.market_price >= 1.0:
        return None  # degenerate, avoid division by zero
    return ev_result.raw_edge / (1 - ev_result.market_price)


def count_open_positions_for_bucket(
    station_icao: str,
    target_date: date,
    bucket_c: int,
    side: str,
    is_paper: Optional[bool] = None,
) -> Optional[int]:
    """
    How many positions are already open on this exact
    (station, target_date, bucket, side). Returns None if open positions
    could not be loaded at all -- callers must treat that as "unknown,
    therefore do not add another leg," not as zero.

    is_paper scopes the count to one track. It matters: paper and real
    positions are separate books, so a paper position must not block a
    real entry on the same bucket (or the reverse) -- that would silently
    halve real exposure the moment paper mode is used anywhere.
    """
    try:
        open_positions = storage.load_open_positions(station_icao=station_icao, is_paper=is_paper)
    except Exception as exc:
        print(f"[entry_manager] could not load open positions to enforce the per-bucket cap: {exc}")
        return None

    return sum(
        1 for p in open_positions
        if p.target_date == target_date
        and p.bucket_c == bucket_c
        and p.side.upper() == side.upper()
    )


def evaluate_entry(
    ev_result: EVResult,
    token_id: str,
    min_net_ev: float = 0.15,
) -> EntryDecision:
    """
    Core entry point: turn one EVResult into a sized, gated
    EntryDecision. Runs the two pre-sizing vetoes (edge plausibility,
    per-bucket position cap), then fetches live order-book depth for this
    specific bucket/side and re-validates the trade at its actual
    recommended size, rather than trusting the flat-size screen
    ev_engine.py used.
    """
    station_icao = ev_result.station_icao
    maturity = config.STATION_MATURITY.get(station_icao, "exploratory")

    def _rejected(reason: str) -> EntryDecision:
        """Uniform shape for a pre-sizing rejection -- nothing was sized, so every sizing field is empty."""
        return EntryDecision(
            station_icao=station_icao, target_date=ev_result.target_date,
            bucket_c=ev_result.bucket_c, side=ev_result.side,
            kelly_fraction_raw=0.0, kelly_fraction_applied=0.0,
            recommended_size_usd=0.0, available_depth_usd=None,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason=reason,
            station_maturity=maturity,
            entry_price=ev_result.market_price,
            token_id=token_id,
        )

    # Veto 0a: edge plausibility. An edge this large on a liquid weather
    # market is bad data, not alpha -- reject before it can be sized.
    raw_edge = ev_result.raw_edge
    if raw_edge is not None and abs(raw_edge) > config.MAX_PLAUSIBLE_RAW_EDGE:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side}: raw edge "
            f"{raw_edge:+.1%} exceeds the {config.MAX_PLAUSIBLE_RAW_EDGE:.0%} plausibility ceiling. An edge "
            f"this large is a PRESUMED DATA ERROR (bad or inverted quote, stale calibration, wrong token id) "
            f"-- not a real trading signal. Not trading it; investigate the price feed and the calibration."
        )
        return _rejected(
            f"VETOED: raw edge {raw_edge:+.1%} exceeds MAX_PLAUSIBLE_RAW_EDGE "
            f"({config.MAX_PLAUSIBLE_RAW_EDGE:.0%}) -- presumed data error, not alpha."
        )

    # Veto 0b: per-bucket position cap. This bet is either already on, or
    # we can't tell -- either way, don't stack another leg onto it. Counted
    # within this candidate's own track (paper vs. real), matching how
    # executor.open_position() stamps Position.is_paper from the same mode.
    candidate_is_paper = executor.EXECUTION_MODE.get(station_icao, "manual_review") == "paper"
    open_count = count_open_positions_for_bucket(
        station_icao, ev_result.target_date, ev_result.bucket_c, ev_result.side,
        is_paper=candidate_is_paper,
    )
    if open_count is None:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side}: could not read "
            f"open positions, so the per-bucket cap cannot be enforced -- refusing to open blind."
        )
        return _rejected("Open positions unreadable -- per-bucket cap unenforceable, refusing to open blind.")

    if open_count >= config.MAX_OPEN_POSITIONS_PER_BUCKET:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side} on "
            f"{ev_result.target_date}: {open_count} position(s) already open on this exact bucket/side, cap is "
            f"{config.MAX_OPEN_POSITIONS_PER_BUCKET}. Re-entering the same bucket across cycles is one bet "
            f"sized up by accident, not a second opportunity -- skipping."
        )
        return _rejected(
            f"Per-bucket cap: {open_count} position(s) already open on this bucket/side "
            f"(max {config.MAX_OPEN_POSITIONS_PER_BUCKET})."
        )

    kelly_raw = compute_kelly_fraction(ev_result)
    if kelly_raw is None or kelly_raw <= 0:
        return EntryDecision(
            station_icao=station_icao, target_date=ev_result.target_date,
            bucket_c=ev_result.bucket_c, side=ev_result.side,
            kelly_fraction_raw=kelly_raw or 0.0, kelly_fraction_applied=0.0,
            recommended_size_usd=0.0, available_depth_usd=None,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason="No positive edge (Kelly fraction <= 0).",
            station_maturity=maturity,
            entry_price=ev_result.market_price,
            token_id=token_id,
        )

    kelly_applied = kelly_raw * config.KELLY_FRACTION
    bankroll_sized_usd = kelly_applied * config.BANKROLL_USD

    # Cap 1: hard per-trade ceiling
    size_usd = min(bankroll_sized_usd, config.MAX_POSITION_USD)

    # Cap 2: station maturity -- exploratory stations sized down hard
    if maturity == "exploratory":
        size_usd *= config.EXPLORATORY_SIZE_MULTIPLIER

    # Cap 3: real order-book depth
    depth_usd = market_client.get_available_depth_usd(token_id)
    if depth_usd is None:
        return EntryDecision(
            station_icao=station_icao, target_date=ev_result.target_date,
            bucket_c=ev_result.bucket_c, side=ev_result.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=0.0, available_depth_usd=None,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason="Order book depth unavailable -- cannot size safely, skipping.",
            station_maturity=maturity,
            entry_price=ev_result.market_price,
            token_id=token_id,
        )

    depth_capped_usd = min(size_usd, depth_usd * config.MAX_DEPTH_UTILIZATION_PCT)

    if depth_capped_usd < 1.0:
        return EntryDecision(
            station_icao=station_icao, target_date=ev_result.target_date,
            bucket_c=ev_result.bucket_c, side=ev_result.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=0.0, available_depth_usd=depth_usd,
            slippage_at_size_pct=None, net_ev_at_size=None,
            approved=False, reason=f"Depth-capped size (${depth_capped_usd:.2f}) too small to trade -- book too thin.",
            station_maturity=maturity,
            entry_price=ev_result.market_price,
            token_id=token_id,
        )

    # Re-check slippage and net EV at the ACTUAL recommended size, not the
    # flat test size ev_engine.py used to screen this candidate initially.
    slippage_at_size = market_client.estimate_slippage(token_id, depth_capped_usd)
    net_ev_at_size = (ev_result.raw_edge / ev_result.market_price) - slippage_at_size - ev_result.fee_rate_pct

    if slippage_at_size > config.MAX_ACCEPTABLE_SLIPPAGE_PCT:
        return EntryDecision(
            station_icao=station_icao, target_date=ev_result.target_date,
            bucket_c=ev_result.bucket_c, side=ev_result.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=depth_capped_usd, available_depth_usd=depth_usd,
            slippage_at_size_pct=slippage_at_size, net_ev_at_size=net_ev_at_size,
            approved=False,
            reason=f"Slippage at this size ({slippage_at_size:.1%}) exceeds the {config.MAX_ACCEPTABLE_SLIPPAGE_PCT:.0%} hard gate -- book too thin to trust the fill.",
            station_maturity=maturity,
            entry_price=ev_result.market_price,
            token_id=token_id,
        )

    if net_ev_at_size < min_net_ev:
        return EntryDecision(
            station_icao=station_icao, target_date=ev_result.target_date,
            bucket_c=ev_result.bucket_c, side=ev_result.side,
            kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
            recommended_size_usd=depth_capped_usd, available_depth_usd=depth_usd,
            slippage_at_size_pct=slippage_at_size, net_ev_at_size=net_ev_at_size,
            approved=False,
            reason=f"Net EV at actual size ({net_ev_at_size:+.1%}) no longer clears the {min_net_ev:.0%} threshold once real slippage is applied.",
            station_maturity=maturity,
            entry_price=ev_result.market_price,
            token_id=token_id,
        )

    return EntryDecision(
        station_icao=station_icao, target_date=ev_result.target_date,
        bucket_c=ev_result.bucket_c, side=ev_result.side,
        kelly_fraction_raw=kelly_raw, kelly_fraction_applied=kelly_applied,
        recommended_size_usd=round(depth_capped_usd, 2), available_depth_usd=depth_usd,
        slippage_at_size_pct=slippage_at_size, net_ev_at_size=net_ev_at_size,
        approved=True,
        reason=f"Approved: {net_ev_at_size:+.1%} net EV at ${depth_capped_usd:.2f} ({maturity} station).",
        station_maturity=maturity,
        entry_price=ev_result.market_price,
        token_id=token_id,
    )


def decide_entries(
    ev_results: List[EVResult],
    token_map: dict,
    min_net_ev: float = 0.15,
) -> List[EntryDecision]:
    """
    Batch entry point: evaluate every candidate EVResult (typically
    ev_engine.best_opportunities()'s output) against real depth/sizing
    constraints. Returns one EntryDecision per candidate, approved and
    rejected alike, so callers (scheduler.py) can log WHY something
    was skipped, not just silently drop it.
    """
    decisions = []
    for result in ev_results:
        bucket_ids = token_map.get(result.bucket_c)
        if not bucket_ids:
            continue
        token_id = bucket_ids["yes_token_id"] if result.side == "YES" else bucket_ids["no_token_id"]
        decisions.append(evaluate_entry(result, token_id, min_net_ev=min_net_ev))
    return decisions


def veto_same_bucket_conflicts(decisions: List[EntryDecision]) -> List[EntryDecision]:
    """
    Detects and vetoes any bucket where BOTH YES and NO were approved
    in the same cycle. Per the NegRisk mechanism (confirmed earlier):
    1 YES + 1 NO of the SAME market merges back into exactly $1 --
    holding both simultaneously isn't a hedge, it's paying the bid-ask
    spread for a position worth exactly $1 with zero directional
    exposure. This is never a legitimate entry (as opposed to closing
    an existing position via Merge, which is a different, valid use of
    the same underlying mechanism -- see executor.py's docstring).

    Both legs of any same-bucket conflict are vetoed (not just one),
    since approving either side alone at this point would be arbitrary
    -- the conflict itself signals something upstream (e.g. a stale
    calibration disagreeing with itself across the YES/NO screen)
    is worth investigating, not silently resolving.
    """
    by_bucket = {}
    for d in decisions:
        by_bucket.setdefault(d.bucket_c, []).append(d)

    result = []
    for bucket_c, bucket_decisions in by_bucket.items():
        approved_sides = {d.side for d in bucket_decisions if d.approved}
        if approved_sides == {"YES", "NO"}:
            print(
                f"[entry_manager] VETOED bucket {bucket_c}: both YES and NO were approved "
                f"simultaneously -- this is not a real position (NegRisk Merge means holding "
                f"both nets to exactly $1, so this would just pay the spread for nothing). "
                f"Vetoing both legs."
            )
            for d in bucket_decisions:
                if d.approved:
                    result.append(EntryDecision(
                        **{**d.__dict__, "approved": False,
                           "reason": "VETOED: same-bucket YES+NO conflict -- see entry_manager logs."}
                    ))
                else:
                    result.append(d)
        else:
            result.extend(bucket_decisions)

    return result


def apply_portfolio_budget(
    decisions: List[EntryDecision],
    max_total_usd: float = None,
) -> List[EntryDecision]:
    """
    Caps the COMBINED size of all approved decisions (e.g. a YES leg on
    the most-likely bucket plus NO legs hedging tail buckets -- the
    real, legitimate version of "both YES and NO" discussed in the
    reanalysis) against a shared per-station-per-day budget. Without
    this, entry_manager.evaluate_entry sizes each leg independently and
    unaware of the others -- five approved legs could each hit
    MAX_POSITION_USD independently and blow well past any sane total
    exposure for one station on one day.

    Scales every approved leg down PROPORTIONALLY if the combined total
    exceeds the budget, preserving the relative sizing the Kelly/depth
    logic already decided rather than arbitrarily keeping some legs
    at full size and zeroing others.
    """
    max_total_usd = max_total_usd if max_total_usd is not None else config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD
    approved = [d for d in decisions if d.approved]
    total_requested = sum(d.recommended_size_usd for d in approved)

    if total_requested <= max_total_usd or total_requested == 0:
        return decisions

    scale = max_total_usd / total_requested
    print(
        f"[entry_manager] portfolio budget exceeded: {len(approved)} approved leg(s) "
        f"requesting ${total_requested:.2f} total, budget is ${max_total_usd:.2f} -- "
        f"scaling all approved legs by {scale:.2%}."
    )

    result = []
    for d in decisions:
        if d.approved:
            result.append(EntryDecision(
                **{**d.__dict__,
                   "recommended_size_usd": round(d.recommended_size_usd * scale, 2),
                   "reason": d.reason + f" [scaled {scale:.0%} for shared portfolio budget]"}
            ))
        else:
            result.append(d)
    return result


def decide_portfolio_entries(
    ev_results: List[EVResult],
    token_map: dict,
    min_net_ev: float = 0.15,
) -> List[EntryDecision]:
    """
    The full, portfolio-aware entry point -- use this instead of calling
    decide_entries() directly when evaluating multiple buckets/sides
    together in one cycle (which is the normal case, since ev_engine
    typically surfaces several candidates at once). Runs, in order:
      1. decide_entries() -- per-leg Kelly/depth/maturity sizing (unchanged)
      2. veto_same_bucket_conflicts() -- kill any same-bucket YES+NO pair
      3. apply_portfolio_budget() -- scale down if the combined total
         exceeds the shared per-station-per-day budget
    """
    decisions = decide_entries(ev_results, token_map, min_net_ev=min_net_ev)
    decisions = veto_same_bucket_conflicts(decisions)
    decisions = apply_portfolio_budget(decisions)
    return decisions


def print_entry_decisions(decisions: List[EntryDecision]) -> None:
    """Human-readable console output."""
    if not decisions:
        print("[entry_manager] no candidate entries to evaluate this cycle.")
        return

    print(f"\n{'Bucket':>7} {'Side':>4} {'Approved':>9} {'Size':>8} {'Depth':>9} {'Slip@size':>10} {'NetEV@size':>11}  Reason")
    for d in decisions:
        depth_str = f"${d.available_depth_usd:.0f}" if d.available_depth_usd is not None else "n/a"
        slip_str = f"{d.slippage_at_size_pct:.1%}" if d.slippage_at_size_pct is not None else "--"
        ev_str = f"{d.net_ev_at_size:+.1%}" if d.net_ev_at_size is not None else "--"
        flag = "YES" if d.approved else "no"
        print(f"{d.bucket_c:>6}° {d.side:>4} {flag:>9} ${d.recommended_size_usd:>6.2f} {depth_str:>9} {slip_str:>10} {ev_str:>11}  {d.reason}")
    print()
