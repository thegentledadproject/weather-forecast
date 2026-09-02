"""
backtest/fill_model.py

PURPOSE
-------
Everything the replay needs to answer "could this trade actually have
been filled, and at what cost". Two questions, deliberately separated:

  1. DEPTH -- how much liquidity do we believe existed at a tick we only
     have a price for. This is a data-availability question, and the
     honest answers differ enough that the choice is an explicit run
     parameter (settings.DEPTH_REGIMES), recorded in the manifest,
     rather than a hidden default.
  2. SLIPPAGE -- what a fill of a given size costs against that depth.

WHY DEPTH IS A HARD REJECT BY DEFAULT
-------------------------------------
entry_manager.evaluate_entry() treats a None from
market_client.get_available_depth_usd() as a hard reject:

    "Order book depth unavailable -- cannot size safely, skipping."

It does NOT assume zero and it does NOT assume plenty. The "strict"
regime here mirrors that exactly: a tick whose snapshot carries no
depth_usd is untradeable, full stop. Any other regime is INVENTING
liquidity, which is why each one has to be asked for by name. Under
"strict" the backtest under-trades relative to reality (real books had
depth we simply did not record); under the substitution regimes it may
over-trade. Neither is "the" answer -- running both is.

THE SLIPPAGE MODEL, AND WHAT IT ASSUMES
---------------------------------------
The live system walks a real order book (market_client.estimate_slippage).
No historical order books exist -- price_store keeps a price and, at
best, a single depth_usd figure per snapshot -- so the book walk cannot
be reproduced. What is modelled instead:

    slippage = min(BASE_SPREAD_PCT + IMPACT_COEFF * (size_usd / depth_usd),
                   MAX_MODELLED_SLIPPAGE_PCT)

with, as ASSUMPTIONS (not measurements):

  BASE_SPREAD_PCT      = 0.005  -- half a cent on the dollar of
                                   unavoidable spread cost even for a
                                   trade that is tiny against the book.
  IMPACT_COEFF         = 0.05   -- consuming 100% of the visible depth
                                   costs 5% in impact, scaling linearly
                                   in utilisation. Equivalent to the
                                   0.5 * u * 0.10 form: half of a 10%
                                   price-impact band, averaged over the
                                   fill.
  MAX_MODELLED_SLIPPAGE_PCT = 0.25 -- ceiling, so a degenerate
                                   size/depth ratio produces an absurd
                                   number rather than an infinite one.

These are conservative in the sense that they never return zero, and
they are LINEAR in utilisation where a real book is convex -- i.e. this
model is optimistic for orders that eat most of the book. That matters
less than it sounds here, because entry_manager caps every order at
config.MAX_DEPTH_UTILIZATION_PCT (25%) of depth, so the modelled range
that actually gets used is u in [0, 0.25] -> slippage in [0.5%, 1.75%].

KNOWN CONSEQUENCE, FLAGGED NOT FIXED: because depth-capped sizing keeps
u <= MAX_DEPTH_UTILIZATION_PCT, at-size slippage under this model can
never reach config.MAX_ACCEPTABLE_SLIPPAGE_PCT (10%), so entry gate 7
(the hard slippage gate) is unreachable in replay whenever depth is
known. The live system has the same property for a different reason --
market_client.estimate_slippage()'s book walk returns its 0.05 fallback
when an order exceeds visible depth, which is also under 10%. Do not
read "gate 7 never fired" as evidence the gate works.

UNKNOWN DEPTH -> LIVE'S FALLBACK, NOT ZERO
------------------------------------------
When depth is unknown or zero, slippage() returns market_client's own
FALLBACK_SLIPPAGE_PCT rather than anything invented here. That constant
is a function-local in market_client.estimate_slippage(), so it is read
out of the live source with `ast` at import time (see
_read_live_fallback_slippage_pct) and only falls back to a hardcoded
mirror if that read fails. Parity with the live number is the point; a
silent drift between the two would quietly change every backtest built
on thin-book ticks.

ENTRY PRICE PARITY -- READ THIS BEFORE COMPARING P&L
----------------------------------------------------
Live paper mode records the QUOTE as the entry price, not a slipped
fill: executor.open_position() writes Position.entry_price =
decision.entry_price, and entry_manager sets that to
ev_result.market_price. Slippage is used to GATE the trade
(entry_manager's re-check at size) and never to move the recorded fill.

So the replay records the quote too -- entry_fill_price() exists and is
computed, but the engine stores the quote on the Position and carries
the slipped price and the dollar cost separately in
BacktestRun.entry_records. Doing it the other way would make backtest
P&L non-comparable with the live paper track record, which is the one
real benchmark this system has. The cost is real and is reported; it is
simply not smuggled into entry_price.

DEPENDENCIES
------------
ast, os, math, typing (standard library)
backtest/settings.py, backtest/price_store.py (local)
"""

import ast
import math
import os
from typing import Optional

from backtest import settings
from backtest import price_store

# Mirror of the FALLBACK_SLIPPAGE_PCT local inside
# clients/market_client.py::estimate_slippage. Used only if the live
# value cannot be read from source (see _read_live_fallback_slippage_pct).
_HARDCODED_FALLBACK_SLIPPAGE_PCT = 0.05

# Slippage model coefficients -- ASSUMPTIONS, documented in the module
# docstring above. Changing these changes every backtest's cost basis,
# so they live here as named constants and go into the run manifest.
BASE_SPREAD_PCT = 0.005
IMPACT_COEFF = 0.05
MAX_MODELLED_SLIPPAGE_PCT = 0.25


def _read_live_fallback_slippage_pct() -> float:
    """
    Read FALLBACK_SLIPPAGE_PCT straight out of
    clients/market_client.py::estimate_slippage's source, so the replay's
    unknown-book cost can never silently drift from the live one.

    Parsed with `ast`, never exec'd -- this reads a constant, it does not
    run market_client. Returns the hardcoded mirror on any failure
    (file moved, constant renamed, non-literal value), because a backtest
    that refuses to start over this would be worse than one that uses a
    documented stand-in.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "clients",
        "market_client.py",
    )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "estimate_slippage":
                for stmt in ast.walk(node):
                    if not isinstance(stmt, ast.Assign):
                        continue
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "FALLBACK_SLIPPAGE_PCT":
                            value = ast.literal_eval(stmt.value)
                            return float(value)
    except (OSError, SyntaxError, ValueError, TypeError):
        pass
    return _HARDCODED_FALLBACK_SLIPPAGE_PCT


FALLBACK_SLIPPAGE_PCT = _read_live_fallback_slippage_pct()


class FillModel:
    """
    Depth resolution + slippage for one backtest run of one station.

    Stateless apart from a single cached median depth lookup, so the same
    instance can be reused across every tick of a run without any tick
    influencing the next one -- an instance that accumulated state would
    make results depend on evaluation order.
    """

    def __init__(
        self,
        depth_regime: str,
        fee_rate_pct: float,
        station_icao: str,
        market_db_path=None,
    ):
        if depth_regime not in settings.DEPTH_REGIMES:
            raise ValueError(
                f"Unknown depth_regime '{depth_regime}' -- expected one of "
                f"{settings.DEPTH_REGIMES}. See backtest/settings.py for what each means."
            )
        self.depth_regime = depth_regime
        # None = price-dependent weather taker fee (the live default since
        # ca42555); a float is a flat override. Stored for provenance only.
        self.fee_rate_pct = None if fee_rate_pct is None else float(fee_rate_pct)
        self.station_icao = station_icao
        self.market_db_path = market_db_path

        # Computed at most once, on first use, and only in the
        # "observed_median" regime -- the query scans every snapshot for
        # the station, so it must not run per tick.
        self._median_depth_cached = False
        self._median_depth: Optional[float] = None

    # --- depth ---------------------------------------------------------

    def observed_median_depth(self) -> Optional[float]:
        """The station's median recorded depth, computed once and cached for the run."""
        if not self._median_depth_cached:
            self._median_depth = price_store.observed_depth_median(
                self.station_icao, db_path=self.market_db_path
            )
            self._median_depth_cached = True
        return self._median_depth

    def depth_at(self, snapshot_row: Optional[dict]) -> Optional[float]:
        """
        Believed order-book depth in USD at the tick this snapshot came
        from, per the active regime. `snapshot_row` is exactly what
        price_store.get_price_at() returns (or None when there was no
        usable quote at all).

        None means UNKNOWN, and callers must treat it the way
        entry_manager.evaluate_entry() treats a None from
        market_client.get_available_depth_usd(): reject the entry. It does
        not mean zero.
        """
        if self.depth_regime == "unconstrained":
            # Depth ignored entirely -- the isolation regime. Note this
            # returns infinity even for a tick with no quote at all;
            # the caller still has no PRICE there, so no trade results.
            return float("inf")

        recorded = None if snapshot_row is None else snapshot_row.get("depth_usd")
        if recorded is not None:
            return float(recorded)

        if self.depth_regime == "strict":
            return None
        if self.depth_regime == "observed_median":
            return self.observed_median_depth()
        if self.depth_regime == "pessimistic":
            return float(settings.PESSIMISTIC_DEPTH_USD)

        raise ValueError(f"Unhandled depth_regime '{self.depth_regime}'.")

    # --- slippage ------------------------------------------------------

    def slippage(self, size_usd: float, depth_usd: Optional[float]) -> float:
        """
        Modelled cost of filling `size_usd` against `depth_usd`, as a
        fraction of trade value -- the replay's stand-in for
        market_client.estimate_slippage().

        Unknown or non-positive depth returns market_client's own
        FALLBACK_SLIPPAGE_PCT, mirroring its semantics exactly: that
        function returns the fallback both when the book is unavailable
        and when the order exceeds visible depth, i.e. "we cannot price
        this fill, assume it is expensive."

        Infinite depth (the "unconstrained" regime) collapses to
        BASE_SPREAD_PCT, since utilisation is zero -- not to zero cost,
        because even an infinitely deep book has a spread.
        """
        if depth_usd is None:
            return FALLBACK_SLIPPAGE_PCT
        depth = float(depth_usd)
        if not math.isfinite(depth):
            return BASE_SPREAD_PCT
        if depth <= 0:
            return FALLBACK_SLIPPAGE_PCT

        size = max(float(size_usd), 0.0)
        utilisation = size / depth
        return min(BASE_SPREAD_PCT + IMPACT_COEFF * utilisation, MAX_MODELLED_SLIPPAGE_PCT)

    # --- fills ---------------------------------------------------------

    def entry_fill_price(
        self,
        quote_price: float,
        size_usd: float,
        depth_usd: Optional[float],
    ) -> float:
        """
        What the buy would ACTUALLY have filled at: the quote pushed up by
        modelled slippage, clamped to 1.0 (a prediction-market outcome
        token cannot cost more than par -- past that you would simply not
        trade).

        NOT what the engine records as Position.entry_price. See the
        module docstring: live paper mode records the quote, so replay
        parity demands the replay record the quote too. This value and
        slippage_cost_usd() below are carried alongside, in
        BacktestRun.entry_records, so the report layer can show the gap
        between the decision price and the achievable price without that
        gap contaminating a like-for-like P&L comparison.
        """
        slip = self.slippage(size_usd, depth_usd)
        return min(quote_price * (1.0 + slip), 1.0)

    def exit_fill_price(
        self,
        quote_price: float,
        size_usd: float,
        bid_depth_usd: Optional[float],
    ) -> float:
        """
        What a SALE of `size_usd` would actually have realised: the bid
        pushed DOWN by modelled slippage, floored at 0.0 (an outcome token
        cannot be sold for less than nothing).

        THE SIGN IS THE WHOLE DIFFERENCE from entry_fill_price(), which
        pushes the quote UP. Both share slippage(), so an inverted sign here
        would silently make every modelled exit better than reality -- it is
        pinned by test.

        `bid_depth_usd` is BID-side depth (market_client.get_bid_depth_usd),
        NOT the `depth_usd` column, which is ask-side and belongs to entries.
        Passing the ask figure would price a sale against liquidity sitting on
        the other side of the spread. None means "not captured" and costs
        FALLBACK_SLIPPAGE_PCT, the same treatment slippage() gives an unknown
        book anywhere else.

        WHAT THIS IS FOR. The per-position loss cap question (2026-09-02)
        could not be decided because every simulation of it assumed the whole
        order cleared at one quote. Filling at the first bid to cross the
        trigger is the honest quote-level answer; this is the size-level one,
        and it only becomes usable once bid depth has actually been collected
        -- no historical row carries it.
        """
        cost = self.slippage(size_usd, bid_depth_usd)
        return max(float(quote_price) * (1.0 - cost), 0.0)

    def slippage_cost_usd(
        self,
        quote_price: float,
        size_usd: float,
        depth_usd: Optional[float],
    ) -> float:
        """
        Dollar cost of the entry slippage that live paper mode does not
        record -- (fill_price - quote) / quote * size_usd. Zero-safe on a
        degenerate quote of 0.
        """
        if quote_price is None or quote_price <= 0:
            return 0.0
        fill = self.entry_fill_price(quote_price, size_usd, depth_usd)
        return size_usd * ((fill - quote_price) / quote_price)

    # --- provenance ----------------------------------------------------

    def describe(self) -> dict:
        """Every assumption this instance embodies, for the run manifest."""
        return {
            "depth_regime": self.depth_regime,
            "fee_rate_pct": self.fee_rate_pct,
            "fallback_slippage_pct": FALLBACK_SLIPPAGE_PCT,
            "fallback_slippage_source": (
                "clients/market_client.py::estimate_slippage"
                if FALLBACK_SLIPPAGE_PCT == _read_live_fallback_slippage_pct()
                else "hardcoded mirror (live read failed)"
            ),
            "base_spread_pct": BASE_SPREAD_PCT,
            "impact_coeff": IMPACT_COEFF,
            "max_modelled_slippage_pct": MAX_MODELLED_SLIPPAGE_PCT,
            "pessimistic_depth_usd": settings.PESSIMISTIC_DEPTH_USD,
            "entry_price_recorded": "quote (live paper parity -- slippage tracked separately)",
        }
