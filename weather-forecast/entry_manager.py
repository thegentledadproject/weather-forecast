"""
entry_manager.py

PURPOSE
-------
The gap flagged repeatedly across the last several summaries: nothing
decided WHEN to open a position. ev_engine.py can say a bucket looks
mispriced; risk_manager.py/position_manager.py can manage a position
once it exists; nothing sat between them turning "this looks good" into
"here's exactly how much to trade, or why not to."

Vetoes run BEFORE any sizing, since none of them can be fixed by trading
smaller:
  00. Entry price ceiling -- config.MAX_ENTRY_PRICE. Above it, remaining
      upside (1 - price) is smaller than the risk carried, the edge
      plausibility ceiling below goes structurally blind, and Kelly's
      denominator collapses so every entry sizes to the cap. Nothing
      bounded the top end before; entries this expensive were kept out
      only accidentally, by percentage net EV having a (1-price)/price
      ceiling that a 15% bar happens to make unreachable above 0.87.
  0a. Edge plausibility -- a raw edge past the ceiling is a data error,
      not alpha, and is rejected outright. This is not hypothetical: a
      price-inversion bug produced "edges" of 0.88 and net EVs of +1298%
      that cleared every EV, slippage and sizing gate below and traded
      real money, because nothing asked whether an edge that large was
      believable in the first place. The ceiling is the smaller of
      config.MAX_PLAUSIBLE_RAW_EDGE and a fraction of the headroom that
      actually exists at this price -- a flat ceiling alone could never
      fire above price 0.75, since raw edge <= 1 - price by construction.
  0b. Per-bucket position cap -- config.MAX_OPEN_POSITIONS_PER_BUCKET
      open positions on the same (station, date, bucket, side) already
      means this bet is on. Repeat entries across cycles are not extra
      opportunities, they are one bet accidentally sized up past every
      per-trade cap below.
  0b2. Opposite-side lock -- an open position on the same (station, date,
      bucket) with the OTHER side is the same bet with the sign flipped,
      and 0b's side filter cannot see it. Buying both locks in the spread
      on the overlap and leaves the remainder naked; RPLL held both sides
      of bucket 32 on 2026-08-24 and paid for it.

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

Two further gates are STATION-level rather than candidate-level, and so
live in decide_portfolio_entries() rather than evaluate_entry():
  - the collection-first gate (config.MIN_RESOLUTION_OBS_BEFORE_ENTRY):
    a station without enough settlement-grade observations to have had
    its bias measured opens nothing at all.
  - the portfolio-wide daily budget
    (config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD), applied alongside
    the per-station one inside apply_portfolio_budget().

This module still only RECOMMENDS -- nothing here places an order.
executor.py's manual_review/auto gating still applies downstream.

DEPENDENCIES
------------
config.py, models.py, storage.py, executor.py (local)
clients/market_client.py (local)
"""

from datetime import date
from typing import List, Optional, Tuple

import calibration
import config
import storage
import executor
import risk_manager  # risk_unit() only -- the gap haircut must measure the stop
                     # distance the exit path will actually use, not a copy of it
from models import EVResult, EntryDecision
from clients import market_client

# (station, target_date, bucket_c, side) keys whose per-bucket-cap veto has
# already been logged, so a held position doesn't re-print the same veto on
# every scan cycle all day. In-memory on purpose: keys embed the date, so
# the set stays small and a restart merely re-logs each veto once.
_bucket_cap_vetoes_logged = set()

# Same dedup pattern for the opposite-side lock (0b2).
_opposite_side_vetoes_logged = set()

# Same dedup pattern for the daily stop-out cooldown veto (0c).
_cooldown_vetoes_logged = set()

# And for the collection-first gate, which fires on EVERY cycle of a new
# station's first days -- exactly the situation where a repeated line
# would drown the journal for a week.
_collection_only_logged = set()


# The price-relative edge-plausibility ceiling. DEFINED IN config, not
# here: the live gate below, backtest/entry_sim's replica, and the status
# dashboard's "veto zone" badge all need it, and the dashboard cannot
# import this module cheaply (it would drag in executor and
# clients.market_client onto a page whose whole design rule is "render
# regardless"). Re-exported under the original name so entry_sim's
# `from entry_manager import max_plausible_edge_for` and the tests keep
# reading naturally at the call site.
max_plausible_edge_for = config.max_plausible_edge_for


def _execution_mode(station_icao: str) -> str:
    """This station's executor mode, defaulted the same way executor does."""
    return executor.EXECUTION_MODE.get(station_icao, "manual_review")


def _candidate_is_paper(station_icao: str) -> bool:
    """
    Which track a candidate entry for this station belongs to, matching
    exactly what executor.open_position() stamps onto Position.is_paper.

    MUST stay `mode != "live"`, not `mode == "paper"`. It was the latter
    while there were only two real modes, and the mode ladder grew to four
    underneath it: under "simulation" or "manual_review" the old form
    returns False, i.e. "this is a real-money candidate", so the per-bucket
    cap and the day-exposure total would be counted against the REAL track
    while executor files the position under the PAPER track. The two
    counts then disagree permanently, and the per-bucket cap silently stops
    binding on the track it is supposed to protect.
    """
    return _execution_mode(station_icao) != "live"


def live_size_cap_usd(station_icao: str) -> Optional[float]:
    """
    The fixed notional this station must trade at, or None for normal Kelly
    sizing. Thin wrapper over config.live_size_cap_usd() that supplies the
    mode, so no caller has to remember to.
    """
    return config.live_size_cap_usd(station_icao, _execution_mode(station_icao))


def gap_risk_haircut(entry_price: Optional[float], station_icao: Optional[str] = None) -> float:
    """
    Fraction to scale a position by so a stop-out costs what the Kelly size
    was actually chosen against.

    Kelly sizes against the loss you expect to take. The stop defines that
    loss as STOP_LOSS_PCT x risk_unit -- but that is the loss you take only
    if you get filled AT the trigger, and on these books you do not. The
    real cost is the stop distance plus config.stop_gap_allowance(), so the
    honest size is the nominal one scaled by the ratio between them:

        nominal / (nominal + gap)

    Worked, at the measured 0.04 gap and the normal-regime 30% stop:

        entry 0.16 -> nominal 0.048 -> haircut 0.55   (gap is most of the risk)
        entry 0.30 -> nominal 0.090 -> haircut 0.69
        entry 0.42 -> nominal 0.126 -> haircut 0.76
        entry 0.70 -> nominal 0.090 -> haircut 0.69   (risk unit is 1-price here)

    The haircut bites hardest on cheap entries, which is correct: four cents
    of slippage is a rounding error against a 0.70 stop distance and most of
    the risk against a 0.16 one.

    Returns 1.0 (no haircut) for LOTTERY-priced entries, which carry no stop
    at all -- their maximum loss is the stake, already accepted at entry, and
    no amount of gapping changes it. Also 1.0 for degenerate prices.

    Uses the NORMAL-regime stop. Entries only happen before 08:00 local (the
    entry window closed two hours earlier on 2026-08-17, which only widens
    the margin this argument already had), so that is the regime a position
    is opened under; after 10:00 the tightened
    stop makes nominal smaller and the true haircut harsher still, so this is
    the conservative-in-the-right-direction choice rather than an exact one.
    """
    if entry_price is None or entry_price <= 0:
        return 1.0
    if entry_price < config.LOTTERY_PRICE_THRESHOLD:
        return 1.0
    nominal = config.STOP_LOSS_PCT * risk_manager.risk_unit(entry_price)
    if nominal <= 0:
        return 1.0
    return nominal / (nominal + config.stop_gap_allowance(station_icao))


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
    # Denominator floored: see config.MIN_KELLY_DENOMINATOR. (1 - price)
    # goes to zero as price -> 1, and the effect is not that sizing gets
    # aggressive so much as that it stops carrying information -- at price
    # 0.95 even the smallest legal edge produces 0.60 full-Kelly, which
    # quarter-Kellys onto MAX_POSITION_USD, so every approved entry
    # emerges at the cap regardless of quality. Binds only above price
    # 0.80, i.e. behind MAX_ENTRY_PRICE as a backstop.
    denominator = max(1 - ev_result.market_price, config.MIN_KELLY_DENOMINATOR)
    return ev_result.raw_edge / denominator


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


def count_stop_outs_for_bucket(
    station_icao: str,
    target_date: date,
    bucket_c: int,
    side: str,
    is_paper: Optional[bool] = None,
) -> Optional[int]:
    """
    How many times this exact (station, target_date, bucket, side) has
    already been closed by a price-noise exit -- feeds the daily re-entry
    cooldown. Counts every status in config.COOLDOWN_COUNTED_EXIT_STATUSES.

    Trailing-stop exits used to be excluded, on the reasoning that "a
    trailing stop is a winner giving back its peak, not the market
    rejecting the entry." True in isolation, but it left the churn loop
    this cooldown exists to break wide open through the other door: a
    bucket could stop -> re-buy -> trail out -> re-buy all day without
    ever tripping it, paying the spread and both legs of taker fees every
    lap. Returns None if history could not be read at all -- callers must
    treat that as "unknown, do not open", not as zero.
    """
    try:
        history = storage.load_position_history(station_icao, is_paper=is_paper)
    except Exception as exc:
        print(f"[entry_manager] could not load position history to enforce the stop-out cooldown: {exc}")
        return None

    return sum(
        1 for p in history
        if p.target_date == target_date
        and p.bucket_c == bucket_c
        and p.side.upper() == side.upper()
        and p.status in config.COOLDOWN_COUNTED_EXIT_STATUSES
    )


def resolution_obs_count(station_icao: str) -> Optional[int]:
    """
    How many stored observations this station has from ITS OWN
    resolution_grade_source -- the count the collection-first gate is
    measured against. Returns None if it could not be read at all
    (unregistered station, storage error), which callers must treat as
    "unknown, therefore not yet graduated", never as a large number.
    """
    try:
        station = config.get_station(station_icao)
        return storage.count_observations_from_source(station_icao, station.resolution_grade_source)
    except Exception as exc:  # noqa: BLE001 - KeyError (unregistered) or any storage failure
        print(f"[entry_manager] could not count settlement-grade observations for {station_icao}: {exc}")
        return None


def forecast_bias_stats(station_icao: str) -> tuple:
    """
    This station's measured forecast bias as (bias_c, n_pairs, stderr_c).

    Returns (None, None, None) if it could not be read at all -- callers
    must treat that as "unknown, therefore not yet graduated", exactly as
    resolution_obs_count()'s None is treated, never as an unbiased zero.

    THE SETTLEMENT-GRADE RECORD IS PREFERRED AND THE FALLBACK IS SECOND ON
    PURPOSE. A temperature from the source the market settles on is a
    strictly better measurement than a 1-degree bucket, so a station with
    both keeps using the former and this fallback changes nothing for it.

    The fallback exists for the case where the preferred record cannot
    arrive in time to be useful. VHHH settles on HKO's CLMMAXT extract,
    which publishes a whole month at a time weeks in arrears: its
    observations stop at 2026-07-31 while its forecasts start 2026-08-06,
    the ranges are disjoint, and forecast_error_samples() returns an EMPTY
    list -- not a small sample, nothing. Before 2026-08-20 that left two
    options, both bad: trade with the bias gate bypassed and the correction
    silently applying 0.0 (which is what BIAS_GATE_OVERRIDE_STATIONS did),
    or do not trade for six weeks.

    bucket_bias.derived_bias_stats() measures the same signed quantity
    against the bucket the market settled into. Validated 2026-08-20 on the
    two stations whose settlement-grade record IS current: WMKK -1.469 vs
    -1.442, WSSS -0.171 vs -0.147, both within 0.03C at opposite ends of
    the bias range.

    ONLY AN EMPTY PREFERRED SAMPLE TRIGGERS IT. A station with three
    settlement-grade pairs keeps those three and stays gated on them,
    rather than having them silently replaced by a larger, coarser sample
    that would graduate it -- mixing the two estimators in one average
    would produce a number that is neither, and switching on "whichever is
    bigger" would let the coarse one quietly win everywhere.
    """
    try:
        station = config.get_station(station_icao)
        dated = storage.forecast_error_samples_dated(
            station_icao, station.resolution_grade_source)
        if dated:
            return calibration.bias_stats_weighted(
                dated, config.local_today(station), config.BIAS_HALF_LIFE_DAYS)
    except Exception as exc:  # noqa: BLE001 - KeyError (unregistered) or any storage failure
        print(f"[entry_manager] could not measure forecast bias for {station_icao}: {exc}")
        return None, None, None

    try:
        import bucket_bias
        bias, n, stderr = bucket_bias.derived_bias_stats(station_icao)
    except Exception as exc:  # noqa: BLE001 - the fallback must never be the thing that breaks
        print(f"[entry_manager] could not derive {station_icao}'s bias from settlements: {exc}")
        return None, None, None

    if n:
        log_key = (station_icao, "derived_bias")
        if log_key not in _collection_only_logged:
            _collection_only_logged.add(log_key)
            print(
                f"[entry_manager] {station_icao} has no settlement-grade forecast/observation "
                f"pairs, so its bias is measured against MARKET SETTLEMENT instead: "
                f"{bias:+.3f}C over n={n} (stderr "
                f"{'unknown' if stderr is None else f'{stderr:.3f}C'}). Bucket-quantized, "
                f"~0.03C from the settlement-grade estimator where both exist."
            )
    return bias, n, stderr


def collection_only_reason(
    station_icao: str,
    obs_count: Optional[int],
    bias_n: Optional[int] = None,
    bias_stderr: Optional[float] = None,
    enforce_bias_quality: bool = False,
) -> Optional[str]:
    """
    The collection-first gate, as a PURE function: None means this
    station may open positions, any string is the reason it may not.

    A station with fewer than config.MIN_RESOLUTION_OBS_BEFORE_ENTRY
    settlement-grade observations has never had its model bias measured
    against the record it actually settles on. Its calibration is running
    on a placeholder climatological normal and (usually) a
    fallback_default spread -- and a 2°C-wrong placeholder does not
    produce absurd edges that MAX_PLAUSIBLE_RAW_EDGE would catch, it
    produces edges of exactly the tradeable size, on every bucket, every
    cycle, all wrong in the same direction. Eleven stations were added to
    the registry at once; without this gate all eleven would have started
    trading on day one off numbers nobody has checked. Collecting costs
    nothing. Wrong entries cost money.

    Counting observations was only ever a PROXY for the real question, and
    on 2026-08-09 the proxy was measured against the thing it stood for and
    found wanting: ten stations had graduated on 5-7 observations while
    their forecasts ran 1.2-1.8°C cool, which at whole-degree buckets is
    ~2 buckets of misplaced probability mass -- precisely the failure the
    paragraph above predicts. So the gate now also asks whether the bias
    has been measured WELL ENOUGH to correct for (calibration applies the
    correction itself; see blend_central_estimate). bias_stderr, not raw
    |bias|, is the test: a large bias pinned down precisely is correctable,
    a small one measured off two noisy days is not.

    enforce_bias_quality is opt-in so replays that never modelled the bias
    keep their existing meaning instead of silently rejecting everything --
    the same opt-in shape entry_sim's enforce_collection_gate already uses.

    config.BIAS_GATE_OVERRIDE_STATIONS exempts named stations from the
    bias half (never from the observation count, never for a station on
    the real-money allowlist). It lives inside this function rather than
    at the call site precisely BECAUSE this function is shared: an
    override applied in live code and not in backtest/entry_sim.py would
    be the drift this purity was designed to prevent.
    Live always passes True.

    config.FORCE_COLLECTION_ONLY_STATIONS runs in the opposite direction to
    both override sets below and is checked BEFORE either of them: it is the
    operator saying a station must not open, and an exemption further down
    must not be able to answer it. It is also the only branch here that is
    not a measurement, which is why it names itself in the reason string.

    config.COLLECTION_GATE_OVERRIDE_STATIONS is the STRONGER exemption:
    it skips this function's checks outright, observation count included,
    and it applies whether or not enforce_bias_quality was asked for. It
    is refused for anything that can reach the real order path, so what
    it can produce is paper rows and nothing else -- see that constant for
    why that is the entire argument for it. Checked before the obs count
    rather than after, because after is where the bias override sits and
    the obs count is exactly what this one is for.

    Kept pure (registry lookup only, no storage) so backtest/entry_sim.py
    can reuse it verbatim and the two sides cannot drift in wording or in
    the comparison itself.
    """
    try:
        source = config.get_station(station_icao).resolution_grade_source
    except KeyError:
        source = config.RESOLUTION_GRADE_OBSERVATION_SOURCE

    # config.FORCE_COLLECTION_ONLY_STATIONS is the operator STOP, and it is
    # checked before everything else in this function -- before the broken-read
    # branch, before both override sets, and regardless of
    # enforce_bias_quality. It is the one check here that is not a
    # measurement: a station is listed because the operator decided it should
    # not trade, so no gate below it can be the reason it trades anyway. See
    # that constant for why it carries no guard of its own.
    if config.force_collection_only(station_icao):
        return (
            f"Collection-only: {station_icao} is on config.FORCE_COLLECTION_ONLY_STATIONS -- "
            f"stopped by operator decision, not by a gate. Still collecting forecasts, "
            f"observations and settled buckets; open positions are unaffected."
        )

    if obs_count is None:
        return (
            f"Collection-only: {station_icao}'s stored '{source}' observation count could not be read, "
            f"so the collection-first gate cannot be enforced -- refusing to open blind."
        )
    # Operator override of the WHOLE gate, per station. Deliberately below
    # the obs_count-is-None branch above: that branch is a BROKEN READ, not
    # an immature station, and "refusing to open blind" stays true no matter
    # what any allowlist says.
    if config.collection_gate_is_overridden(station_icao):
        return None

    if obs_count < config.MIN_RESOLUTION_OBS_BEFORE_ENTRY:
        return (
            f"Collection-only: {station_icao} has {obs_count} stored '{source}' observation(s), below the "
            f"{config.MIN_RESOLUTION_OBS_BEFORE_ENTRY} required before any entry -- this station's model "
            f"bias is unmeasured, so its 'edge' is a guess. Collecting, not trading."
        )

    if not enforce_bias_quality:
        return None

    # Operator override, per station, for the bias half ONLY -- the
    # observation count above still had to pass. See
    # config.bias_gate_is_overridden for why it refuses real-money
    # stations, and BIAS_GATE_OVERRIDE_STATIONS for what it costs.
    if config.bias_gate_is_overridden(station_icao):
        return None

    if bias_n is None:
        return (
            f"Collection-only: {station_icao}'s forecast bias could not be measured, so there is no way "
            f"to tell whether its edges are real -- refusing to open blind."
        )
    if bias_n < config.MIN_BIAS_PAIRS_BEFORE_ENTRY:
        return (
            f"Collection-only: {station_icao} has {bias_n} forecast/observation pair(s), below the "
            f"{config.MIN_BIAS_PAIRS_BEFORE_ENTRY} needed to measure its forecast bias. Stored observations "
            f"only measure anything on dates a forecast was stored too. Collecting, not trading."
        )
    if bias_stderr is None or bias_stderr > config.MAX_BIAS_STANDARD_ERROR_C:
        shown = "unknown" if bias_stderr is None else f"{bias_stderr:.2f}C"
        return (
            f"Collection-only: {station_icao}'s forecast bias is measured to +/-{shown} (standard error over "
            f"{bias_n} pair(s)), above the {config.MAX_BIAS_STANDARD_ERROR_C:.2f}C needed to correct for it -- "
            f"correcting by a number this noisy is just a different guess. Collecting, not trading."
        )
    return None


def collection_only_decision(ev_result: EVResult, token_id: Optional[str], reason: str) -> EntryDecision:
    """
    The EntryDecision a collection-only station produces for one
    candidate: rejected, nothing sized, reason intact so the funnel shows
    WHY rather than silently dropping the candidate. Shared with
    backtest/entry_sim.py so live and replay emit identical objects.
    """
    return EntryDecision(
        station_icao=ev_result.station_icao, target_date=ev_result.target_date,
        bucket_c=ev_result.bucket_c, side=ev_result.side,
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0,
        recommended_size_usd=0.0, available_depth_usd=None,
        slippage_at_size_pct=None, net_ev_at_size=None,
        approved=False, reason=reason,
        station_maturity=config.STATION_MATURITY.get(ev_result.station_icao, "exploratory"),
        entry_price=ev_result.market_price,
        token_id=token_id,
        model_prob=ev_result.model_prob,
        raw_edge=ev_result.raw_edge,
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
            model_prob=ev_result.model_prob,
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
        )

    # Veto 00: entry price ceiling. Above MAX_ENTRY_PRICE the instrument
    # itself is wrong for this system regardless of how good the signal
    # looks -- remaining upside is (1 - price) against a full stake at
    # risk, the plausibility ceiling below goes blind, and Kelly sizes
    # everything to the cap. Checked first because it is a property of
    # the market, not of the edge.
    if ev_result.market_price is not None and ev_result.market_price > config.MAX_ENTRY_PRICE:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side}: entry price "
            f"{ev_result.market_price:.3f} is above the {config.MAX_ENTRY_PRICE:.2f} ceiling. Only "
            f"{1 - ev_result.market_price:.3f} of upside remains against the whole stake at risk, and the "
            f"edge-plausibility and Kelly-sizing gates both stop discriminating this close to 1.00."
        )
        return _rejected(
            f"Entry price {ev_result.market_price:.3f} above MAX_ENTRY_PRICE "
            f"({config.MAX_ENTRY_PRICE:.2f}) -- too little upside left to justify the stake."
        )

    # Veto 0a: edge plausibility. An edge this large on a liquid weather
    # market is bad data, not alpha -- reject before it can be sized.
    raw_edge = ev_result.raw_edge
    max_plausible_edge = max_plausible_edge_for(ev_result.market_price)
    if raw_edge is not None and abs(raw_edge) > max_plausible_edge:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side}: raw edge "
            f"{raw_edge:+.1%} exceeds the {max_plausible_edge:.1%} plausibility ceiling at price "
            f"{ev_result.market_price}. An edge this large is a PRESUMED DATA ERROR (bad or inverted quote, "
            f"stale calibration, wrong token id) -- not a real trading signal. Not trading it; investigate "
            f"the price feed and the calibration."
        )
        return _rejected(
            f"VETOED: raw edge {raw_edge:+.1%} exceeds the plausibility ceiling "
            f"({max_plausible_edge:.1%} at price {ev_result.market_price}) -- presumed data error, not alpha."
        )

    # Veto 0a2: edge MATERIALITY -- the mirror of 0a. Percentage EV explodes
    # mechanically as price -> 0, floating claimed edges of a few cents to
    # the top of the ranking even though they sit inside the bid-ask noise
    # of a near-empty book. An edge must be worth at least MIN_ABS_RAW_EDGE
    # in absolute cents to be a tradeable disagreement rather than noise.
    #
    # The bar itself scales with how the edge was computed: an estimate
    # whose spread came from calibration's flat fallback default (no real
    # ensemble/forecast/observed spread behind it -- see
    # CalibratedEstimate.spread_source) has an unmeasured probability
    # underneath its edge, not just an unmeasured price. Requiring a
    # bigger edge there isn't extra caution for its own sake -- it's the
    # same "is this real or is this noise" question MIN_ABS_RAW_EDGE
    # already asks, applied to a case where the noise floor is higher.
    # Membership in a SET, not equality with one string: the spread chain
    # now has two tiers that mean "not measured for this station"
    # (fallback_default, and the pooled cross-station estimate). Under the
    # old chain every station got "forecast_variance", which never tripped
    # this multiplier despite being the least trustworthy number in the
    # model -- so this reads as a tightening, and is one.
    spread_source = getattr(ev_result, "spread_source", None)
    min_abs_edge = config.MIN_ABS_RAW_EDGE
    if spread_source in config.LOW_CONFIDENCE_SPREAD_SOURCES:
        min_abs_edge *= config.LOW_CONFIDENCE_EDGE_MULTIPLIER

    if raw_edge is not None and abs(raw_edge) < min_abs_edge:
        low_conf_note = f" (raised: spread_source={spread_source})" if min_abs_edge != config.MIN_ABS_RAW_EDGE else ""
        return _rejected(
            f"Absolute edge {raw_edge:+.3f} below required minimum {min_abs_edge:.3f}{low_conf_note} "
            f"-- inside book noise, not a tradeable disagreement."
        )

    # Veto 0b: per-bucket position cap. This bet is either already on, or
    # we can't tell -- either way, don't stack another leg onto it. Counted
    # within this candidate's own track (paper vs. real), matching how
    # executor.open_position() stamps Position.is_paper from the same mode.
    candidate_is_paper = _candidate_is_paper(station_icao)
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
        # Log the full explanation ONCE per bucket/side/day: a held position
        # re-triggers this veto on every scan cycle for hours, and the repeat
        # lines bury real events in the journal. The veto itself still fires
        # every time -- only the logging is deduplicated.
        veto_key = (station_icao, ev_result.target_date, ev_result.bucket_c, ev_result.side.upper())
        if veto_key not in _bucket_cap_vetoes_logged:
            _bucket_cap_vetoes_logged.add(veto_key)
            print(
                f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side} on "
                f"{ev_result.target_date}: {open_count} position(s) already open on this exact bucket/side, cap is "
                f"{config.MAX_OPEN_POSITIONS_PER_BUCKET}. Re-entering the same bucket across cycles is one bet "
                f"sized up by accident, not a second opportunity -- skipping. "
                f"(Further cap vetoes for this bucket today will not be logged.)"
            )
        return _rejected(
            f"Per-bucket cap: {open_count} position(s) already open on this bucket/side "
            f"(max {config.MAX_OPEN_POSITIONS_PER_BUCKET})."
        )

    # Veto 0b2: the OPPOSITE side of the same bucket is already open.
    #
    # Veto 0b above counts only the side being bought, so it cannot see the
    # bet it is really guarding against: the same bucket with the sign
    # flipped. RPLL's book on 2026-08-24 held both at once --
    #
    #     21:41Z  RPLL 32 YES @0.34  $4.54   model P(32) = 0.4234
    #     22:20Z  RPLL 32 NO  @0.64  $14.04  model P(32) = 0.2215
    #
    # -- 39 minutes apart, with the model's P(32) halved in between. The
    # overlapping 13.35 shares cost 0.34 + 0.64 = $0.98 for a guaranteed
    # $1.00, which the taker fee then eats; the remainder is a naked NO
    # opened against a YES the same book still held. RPLL settled 32, so
    # the YES was right and closed early for +$1.44 while the NO rode to
    # -$14.04.
    #
    # BLOCKING THE NEW ENTRY, not closing the old one, is deliberate. It is
    # the conservative direction -- refusing an entry can only make trading
    # less likely -- and a model that has changed its mind should be
    # exiting the position it no longer believes in through the exit path,
    # where the stop, the take and resolution detection all apply. Opening
    # its own hedge through the entry path pays the spread twice to reach a
    # position it could have reached by selling.
    opposite_side = "NO" if ev_result.side.upper() == "YES" else "YES"
    opposite_count = count_open_positions_for_bucket(
        station_icao, ev_result.target_date, ev_result.bucket_c, opposite_side,
        is_paper=candidate_is_paper,
    )
    if opposite_count is None:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side}: could not read "
            f"open positions, so the opposite-side lock cannot be enforced -- refusing to open blind."
        )
        return _rejected(
            "Open positions unreadable -- opposite-side lock unenforceable, refusing to open blind."
        )

    if opposite_count > 0:
        veto_key = (station_icao, ev_result.target_date, ev_result.bucket_c, ev_result.side.upper())
        if veto_key not in _opposite_side_vetoes_logged:
            _opposite_side_vetoes_logged.add(veto_key)
            print(
                f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side} on "
                f"{ev_result.target_date}: {opposite_count} {opposite_side} position(s) already open on this "
                f"bucket. Buying both sides is not two bets -- it locks in the spread on the overlap and "
                f"leaves the remainder naked. If the model has changed its mind, the old leg should be SOLD, "
                f"not hedged. (Further opposite-side vetoes for this bucket today will not be logged.)"
            )
        return _rejected(
            f"Opposite side already open: {opposite_count} {opposite_side} position(s) on this bucket."
        )

    # Veto 0c: stop-out cooldown. The open-position cap (0b) stops STACKING
    # but has no memory of exits, so a bucket could run "stop -> re-buy ->
    # stop" all day, paying the spread on every lap (it did, on 2026-08-03).
    # One stop-loss on a bucket/side is the market's answer for the day.
    stop_outs = count_stop_outs_for_bucket(
        station_icao, ev_result.target_date, ev_result.bucket_c, ev_result.side,
        is_paper=candidate_is_paper,
    )
    if stop_outs is None:
        print(
            f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side}: could not read "
            f"position history, so the stop-out cooldown cannot be enforced -- refusing to open blind."
        )
        return _rejected("Position history unreadable -- stop-out cooldown unenforceable, refusing to open blind.")

    if stop_outs >= config.MAX_STOP_OUTS_PER_BUCKET_PER_DAY:
        veto_key = (station_icao, ev_result.target_date, ev_result.bucket_c, ev_result.side.upper())
        if veto_key not in _cooldown_vetoes_logged:
            _cooldown_vetoes_logged.add(veto_key)
            print(
                f"[entry_manager] VETOED {station_icao} {ev_result.bucket_c}°{ev_result.side} on "
                f"{ev_result.target_date}: already stop-lossed {stop_outs}x on this bucket/side today "
                f"(cooldown limit {config.MAX_STOP_OUTS_PER_BUCKET_PER_DAY}). Re-buying a bucket the market "
                f"just stopped us out of is churn, not conviction -- no more entries here today. "
                f"(Further cooldown vetoes for this bucket today will not be logged.)"
            )
        return _rejected(
            f"Stop-out cooldown: {stop_outs} stop-loss exit(s) on this bucket/side today "
            f"(max {config.MAX_STOP_OUTS_PER_BUCKET_PER_DAY})."
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
            model_prob=ev_result.model_prob,
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
        )

    kelly_applied = kelly_raw * config.KELLY_FRACTION
    # THIS STATION'S REGION'S bankroll, not one global number. A station in
    # a zero-funded region sizes to $0 here and every cap below is a no-op
    # -- which is the intended state for a newly registered region.
    bankroll_sized_usd = kelly_applied * config.region_bankroll_usd(station_icao)

    # Cap 1: hard per-trade ceiling
    size_usd = min(bankroll_sized_usd, config.MAX_POSITION_USD)

    # Cap 2: station maturity -- exploratory stations sized down hard
    if maturity == "exploratory":
        size_usd *= config.EXPLORATORY_SIZE_MULTIPLIER

    # Gap-risk haircut: the stop does not fill at the stop. Applied AFTER the
    # hard ceiling and the maturity multiplier so it always reduces real risk
    # rather than being swallowed by a cap above it, and BEFORE the live fixed
    # size below, which is a deliberate override rather than a risk estimate.
    size_usd *= gap_risk_haircut(ev_result.market_price, station_icao)

    # Cap 2b: fixed live/simulation trade size. REPLACES Kelly sizing rather
    # than capping it -- see config.LIVE_TRADE_SIZE_USD.
    #
    # Applied HERE, above the depth/slippage/net-EV re-checks below, and not
    # in executor. Those three checks all re-evaluate the trade at "the
    # ACTUAL recommended size"; clamping after them would file a $1 trade
    # under a $150 trade's slippage estimate and net-EV figure, and
    # net_ev_at_size -- the number the approval reason quotes, the dashboard
    # prints and the P&L attribution reads -- would describe a trade that
    # never happened. Clamping first costs nothing and keeps every
    # downstream number about the order that actually gets built.
    live_cap = live_size_cap_usd(station_icao)
    if live_cap is not None:
        size_usd = min(size_usd, live_cap)

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
            model_prob=ev_result.model_prob,
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
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
            model_prob=ev_result.model_prob,
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
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
            model_prob=ev_result.model_prob,
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
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
            model_prob=ev_result.model_prob,
            raw_edge=ev_result.raw_edge,
            min_net_ev=min_net_ev,
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
        model_prob=ev_result.model_prob,
        raw_edge=ev_result.raw_edge,
        min_net_ev=min_net_ev,
    )


def candidates_with_token_ids(
    ev_results: List[EVResult],
    token_map: dict,
) -> List[Tuple[EVResult, str]]:
    """
    Pair each candidate with the token id for its own side, dropping any
    candidate whose bucket isn't in the map (it cannot be traded without
    a token id). Factored out so the collection-first gate and the sizing
    path agree exactly on which candidates exist -- and so the live
    candidate list has the same (EVResult, token_id) shape
    backtest/entry_sim.decide_portfolio_entries_sim() consumes.
    """
    candidates = []
    for result in ev_results:
        bucket_ids = token_map.get(result.bucket_c)
        if not bucket_ids:
            continue
        token_id = bucket_ids["yes_token_id"] if result.side == "YES" else bucket_ids["no_token_id"]
        candidates.append((result, token_id))
    return candidates


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
    return [
        evaluate_entry(result, token_id, min_net_ev=min_net_ev)
        for result, token_id in candidates_with_token_ids(ev_results, token_map)
    ]


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


def station_day_exposure_usd(
    station_icao: str,
    target_date: date,
    is_paper: Optional[bool] = None,
) -> Optional[float]:
    """
    Dollars ALREADY deployed into this station's market for this target
    date, across earlier cycles today: open positions plus positions
    already closed (a leg entered at 05:10 and stopped out at 06:40 still
    spent budget -- "per day" means cumulative dollars put at risk, not
    concurrent exposure, or re-entering after every stop-out would let a
    bad morning burn through the cap several times over).

    Returns None if either read fails -- callers must treat that as
    "unknown, therefore no budget remains," the same fail-closed rule as
    count_open_positions_for_bucket(). is_paper scopes to one track,
    matching how the per-bucket cap and executor stamp positions.
    """
    try:
        positions = storage.load_open_positions(station_icao=station_icao, is_paper=is_paper)
        positions += storage.load_position_history(station_icao, limit=1000, is_paper=is_paper)
    except Exception as exc:
        print(f"[entry_manager] could not load positions to enforce the station/day budget: {exc}")
        return None

    return sum(p.size_usd for p in positions if p.target_date == target_date)


def portfolio_day_exposure_usd(
    is_paper: Optional[bool] = None,
    region: Optional[str] = None,
) -> Optional[float]:
    """
    Dollars deployed across EVERY registered station for its own current
    local trading day -- the figure the portfolio-wide cap is measured
    against.

    Each station is summed against ITS OWN config.local_today(): the
    registry spans UTC+5 to UTC+9, so there is no single "today" to sum
    over, and using one station's date would either miss a group that has
    already rolled over or double-count one that hasn't.

    Returns None if ANY station's exposure could not be read. Same
    fail-closed rule as station_day_exposure_usd(): an unknown portion of
    the portfolio's spend means the total is unknown, and an unknown
    total must not be treated as a small one.

    Cost note: this is 2 storage reads per registered station, but it
    only runs on cycles that actually produced candidates clearing the EV
    screen -- not on every scan.

    `region` scopes the sum to one capital pool. None keeps the original
    all-stations meaning for callers that predate regions. Passing it is
    what stops an Asian drawdown from consuming a European station's
    budget, and vice versa -- the two cohorts share no thesis, so they
    must not share a denominator.
    """
    total = 0.0
    for icao in config.STATIONS:
        if region is not None and config.region_of(icao) != region:
            continue
        station_total = station_day_exposure_usd(
            icao, config.local_today(icao), is_paper=is_paper,
        )
        if station_total is None:
            return None
        total += station_total
    return total


def apply_portfolio_budget(
    decisions: List[EntryDecision],
    max_total_usd: float = None,
    existing_exposure_usd: float = 0.0,
    max_portfolio_usd: float = None,
    portfolio_exposure_usd: float = 0.0,
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

    existing_exposure_usd is what earlier cycles ALREADY spent against
    the same budget (see station_day_exposure_usd). Until 2026-08-02
    this was implicitly zero, which made the "per day" cap a per-CYCLE
    cap: ~18 entry cycles a day could each spend the full budget afresh.
    Found by the backtest build's gate audit. Callers evaluating a
    single cycle in isolation (unit tests) keep the old behavior via the
    0.0 default.

    portfolio_exposure_usd / max_portfolio_usd are the SAME arithmetic one
    level up: dollars already deployed across every station today against
    config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD (see
    portfolio_day_exposure_usd). The per-station cap stopped meaning
    anything the day the registry went from 2 stations to 13 -- 13 x $250
    quietly authorizes $3,250/day against a $1,000 bankroll, and these
    stations' errors are CORRELATED (shared placeholder normals, shared
    fallback spreads), so the "diversification" that might excuse it does
    not exist. Whichever budget binds tighter wins; the defaults leave
    single-station behaviour unchanged.

    Scales every approved leg down PROPORTIONALLY if the combined total
    exceeds the remaining budget, preserving the relative sizing the
    Kelly/depth logic already decided rather than arbitrarily keeping
    some legs at full size and zeroing others. If nothing remains, every
    approved leg is rejected outright.
    """
    max_total_usd = max_total_usd if max_total_usd is not None else config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD
    max_portfolio_usd = (
        max_portfolio_usd if max_portfolio_usd is not None
        else config.MAX_TOTAL_EXPOSURE_PORTFOLIO_PER_DAY_USD
    )

    station_remaining_usd = max_total_usd - existing_exposure_usd
    portfolio_remaining_usd = max_portfolio_usd - portfolio_exposure_usd

    # The tighter of the two caps is the real budget this cycle. Reported
    # by name so a log line says which ceiling actually stopped the trade
    # -- "the station is full" and "the book is full" call for different
    # responses from whoever reads the journal.
    if station_remaining_usd <= portfolio_remaining_usd:
        remaining_usd = station_remaining_usd
        binding_label, binding_spent, binding_cap = "station/day", existing_exposure_usd, max_total_usd
    else:
        remaining_usd = portfolio_remaining_usd
        binding_label, binding_spent, binding_cap = "portfolio/day", portfolio_exposure_usd, max_portfolio_usd

    approved = [d for d in decisions if d.approved]
    total_requested = sum(d.recommended_size_usd for d in approved)

    if approved and remaining_usd <= 0:
        print(
            f"[entry_manager] {binding_label} budget exhausted: ${binding_spent:.2f} already deployed "
            f"against a ${binding_cap:.2f} budget -- rejecting all {len(approved)} approved leg(s) this cycle."
        )
        result = []
        for d in decisions:
            if d.approved:
                result.append(EntryDecision(
                    **{**d.__dict__,
                       "approved": False,
                       "recommended_size_usd": 0.0,
                       "reason": d.reason + f" [rejected: {binding_label} budget exhausted "
                                            f"(${binding_spent:.2f} of ${binding_cap:.2f} already deployed)]"}
                ))
            else:
                result.append(d)
        return result

    if total_requested <= remaining_usd or total_requested == 0:
        return decisions

    scale = remaining_usd / total_requested
    print(
        f"[entry_manager] {binding_label} budget exceeded: {len(approved)} approved leg(s) "
        f"requesting ${total_requested:.2f} total, remaining budget is ${remaining_usd:.2f} "
        f"(${binding_spent:.2f} of ${binding_cap:.2f} already deployed) -- "
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
      0. the collection-first gate -- a station without
         config.MIN_RESOLUTION_OBS_BEFORE_ENTRY settlement-grade
         observations opens NOTHING, whatever the EV table says (see
         collection_only_reason). Placed before sizing because it is a
         property of the STATION, not of any one candidate: no amount of
         trading smaller fixes an unmeasured bias.
      1. decide_entries() -- per-leg Kelly/depth/maturity sizing (unchanged)
      2. veto_same_bucket_conflicts() -- kill any same-bucket YES+NO pair
      3. apply_portfolio_budget() -- scale down if the combined total
         exceeds what REMAINS of the shared per-station-per-day budget
         after earlier cycles' entries, or of the portfolio-wide daily
         budget across all 13 stations (fail-closed if either can't be read)
    """
    if not ev_results:
        return []

    station_icao = ev_results[0].station_icao
    candidate_is_paper = _candidate_is_paper(station_icao)

    # --- Stage 0: collection-first gate ----------------------------------
    bias_c, bias_n, bias_stderr = forecast_bias_stats(station_icao)
    gate_reason = collection_only_reason(
        station_icao,
        resolution_obs_count(station_icao),
        bias_n=bias_n,
        bias_stderr=bias_stderr,
        enforce_bias_quality=True,
    )
    if gate_reason is not None:
        # Logged once per station/day: a brand-new station trips this on
        # every cycle for days, and the repeat lines would bury real events.
        log_key = (station_icao, ev_results[0].target_date)
        if log_key not in _collection_only_logged:
            _collection_only_logged.add(log_key)
            print(
                f"[entry_manager] {gate_reason} "
                f"({len(ev_results)} candidate(s) this cycle get no entry. Further collection-only "
                f"notices for this station today will not be logged.)"
            )
        return [
            collection_only_decision(result, token_id, gate_reason)
            for result, token_id in candidates_with_token_ids(ev_results, token_map)
        ]

    # An overridden station passes the gate SILENTLY otherwise, and a
    # silent pass is indistinguishable from a station that earned it.
    # Same once-per-station-per-day budget as the block notice above.
    if config.bias_gate_is_overridden(station_icao):
        warn_key = (station_icao, ev_results[0].target_date, "override")
        if warn_key not in _collection_only_logged:
            _collection_only_logged.add(warn_key)
            print(
                f"[entry_manager] {station_icao} is entering on an UNMEASURED forecast bias: "
                f"config.BIAS_GATE_OVERRIDE_STATIONS exempts it from the bias-quality gate "
                f"(measured pairs={bias_n}, stderr="
                f"{'unknown' if bias_stderr is None else f'{bias_stderr:.2f}C'}). "
                f"Positions opened for this station are not evidence of edge until its bias "
                f"is measured. Paper track only -- {station_icao} is not in LIVE_TRADING_STATIONS."
            )

    decisions = decide_entries(ev_results, token_map, min_net_ev=min_net_ev)
    decisions = veto_same_bucket_conflicts(decisions)

    existing_usd = station_day_exposure_usd(
        station_icao, ev_results[0].target_date, is_paper=candidate_is_paper,
    )
    if existing_usd is None:
        # Same fail-closed stance as the per-bucket cap: unknown spend
        # means unknown remaining budget -- treat it as exhausted
        # rather than as a fresh budget.
        existing_usd = config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD

    station_region = config.region_of(station_icao)
    portfolio_usd = portfolio_day_exposure_usd(
        is_paper=candidate_is_paper, region=station_region,
    )
    if portfolio_usd is None:
        # Fail closed, same rule as the station cap above: an unknown
        # portion of the region's spend means the total is unknown, and an
        # unknown total must not be treated as a small one.
        portfolio_usd = config.region_max_daily_exposure_usd(station_icao)

    decisions = apply_portfolio_budget(
        decisions,
        existing_exposure_usd=existing_usd,
        portfolio_exposure_usd=portfolio_usd,
        max_portfolio_usd=config.region_max_daily_exposure_usd(station_icao),
    )
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
