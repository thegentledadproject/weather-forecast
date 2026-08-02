"""
backtest/portfolio.py

PURPOSE
-------
In-memory book for a replay run: cash, open positions, closed
positions, and a mark-to-market equity curve. This is the piece the
live system does not have and cannot have -- executor.py/storage.py
track positions but nothing anywhere tracks a BALANCE, because there
is no wallet. A backtest has to, or it can't answer the only question
that matters ("would this have made money, and how deep a hole would
it have dug on the way").

Deliberately holds real models.Position instances rather than a local
lookalike dataclass. Two reasons: paper_trading_report.summarize_
positions() then accepts a finished run's closed positions directly,
with no adapter and no chance of the two diverging; and the exit
fields written here are forced to mean exactly what storage.
close_position() means by them.

Every position is created with is_paper=True. A simulated fill is
paper by definition, and letting one look like a real position -- in
this process or in any report handed a mixed list -- would be a
straightforward way to corrupt a real P&L record.

WHAT THIS DOES NOT MODEL
------------------------
Fills are assumed at the price handed in. Depth/slippage gating lives
in the engine (see settings.DEFAULT_DEPTH_REGIME), not here, so this
module stays a pure accounting ledger. It also does not update
high_water_mark -- that is the exit-checking caller's job, exactly as
position_manager.py owns it in the live system.

DEPENDENCIES
------------
typing (standard library)
config.py, models.py (local)
"""

from typing import Callable, Dict, List, Optional, Tuple

import config
from models import Position

# Sizing-bankroll modes:
#   "static"      -- sizing always uses the STARTING bankroll, matching the
#                    live system exactly: config.BANKROLL_USD is a fixed
#                    constant that entry_manager reads every cycle, so a
#                    live run sizes off the same number on day 1 and day 90.
#   "compounding" -- sizing uses current mark-to-market equity. Not what
#                    the live system does today; available so a run can
#                    show what compounding would have changed, without
#                    pretending that is the current behaviour.
BANKROLL_MODES = ("static", "compounding")


class PortfolioState:
    """
    The book for one backtest run. Not thread-safe and not persisted --
    a run builds one of these, feeds it ticks, and reads the results off
    it at the end.
    """

    def __init__(self, bankroll_usd: float, bankroll_mode: str = "static"):
        if bankroll_mode not in BANKROLL_MODES:
            raise ValueError(
                f"Unknown bankroll_mode '{bankroll_mode}' -- expected one of {BANKROLL_MODES}."
            )
        self.initial_bankroll_usd: float = float(bankroll_usd)
        self.bankroll_mode: str = bankroll_mode
        self.cash: float = float(bankroll_usd)
        self.open: Dict[str, Position] = {}
        self.closed: List[Position] = []
        self.equity_curve: List[Tuple[int, float]] = []
        self.peak_equity: float = float(bankroll_usd)
        self.max_drawdown_pct: float = 0.0
        # Last price actually seen for each open position, keyed by
        # position_id. A position the feed has gone quiet on marks at its
        # last observed price rather than vanishing from equity -- and at
        # entry_price if it has never been priced at all.
        self._last_price: Dict[str, float] = {}

    # --- sizing -----------------------------------------------------------

    def sizing_bankroll(self) -> float:
        """
        The bankroll figure entry sizing should use this cycle. In
        "static" mode this is the starting bankroll (parity with
        config.BANKROLL_USD, which the live system treats as a constant);
        in "compounding" mode it is current mark-to-market equity.
        """
        if self.bankroll_mode == "compounding":
            return self.current_equity()
        return self.initial_bankroll_usd

    def current_equity(self) -> float:
        """
        Mark-to-market equity at the last known prices, WITHOUT recording
        a point on the equity curve. mark() is the function that records;
        this one is for callers that just need the number.
        """
        equity = self.cash
        for position in self.open.values():
            equity += self._position_value(position, self._last_price.get(position.position_id))
        return equity

    # --- position lifecycle ------------------------------------------------

    def open_position(self, pos: Position) -> None:
        """
        Record a simulated fill: debit cash by the staked size and add the
        position to the open book. The Position is forced to is_paper=True
        (a simulated fill is paper by definition) and its high_water_mark
        seeded from entry_price if the caller left it unset.
        """
        if pos.position_id in self.open:
            raise ValueError(
                f"position_id '{pos.position_id}' is already open -- "
                f"position ids must be unique within a run."
            )
        pos.is_paper = True
        if pos.high_water_mark is None:
            pos.high_water_mark = pos.entry_price

        self.cash -= pos.size_usd
        self.open[pos.position_id] = pos
        self._last_price[pos.position_id] = pos.entry_price

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_time_iso: str,
        status: str,
        exit_reason: str,
    ) -> Position:
        """
        Close an open position and credit the proceeds back to cash.

        Field semantics mirror storage.close_position() exactly: `status`
        is the terminal status string written to the position ("closed_
        take_profit", "closed_stop_loss", "closed_trailing_stop",
        "closed_resolution" -- see models.Position.status for the full,
        exact set), and `exit_reason` is the free-text reason column.
        Callers must not pass a resolution close as a stop-loss status,
        for the same reason position_manager._close_as_resolved() has its
        own code path: it misattributes the exit AND fabricates the price.

        Proceeds are size_usd * exit_price / entry_price -- the staked
        dollars scaled by the price move, consistent with how
        risk_manager.compute_pnl_pct() defines return on a position.
        """
        if position_id not in self.open:
            raise KeyError(f"position_id '{position_id}' is not open -- cannot close it.")

        position = self.open.pop(position_id)
        self._last_price.pop(position_id, None)

        self.cash += self._position_value(position, exit_price)

        position.status = status
        position.exit_price = exit_price
        position.exit_time = exit_time_iso
        position.exit_reason = exit_reason
        self.closed.append(position)
        return position

    # --- queries -----------------------------------------------------------

    def count_open_for_bucket(self, station_icao: str, target_date, bucket_c: int) -> int:
        """
        Open positions on this exact (station, target_date, bucket),
        across BOTH sides -- the backtest equivalent of
        entry_manager.count_open_positions_for_bucket()'s storage query,
        used to enforce config.MAX_OPEN_POSITIONS_PER_BUCKET in replay.
        """
        return sum(
            1 for p in self.open.values()
            if p.station_icao == station_icao
            and p.target_date == target_date
            and p.bucket_c == bucket_c
        )

    def total_open_exposure(self, station_icao=None, target_date=None) -> float:
        """
        Staked dollars currently at risk, optionally scoped to one station
        and/or one target date -- the figure
        config.MAX_TOTAL_EXPOSURE_PER_STATION_PER_DAY_USD is checked against.
        """
        total = 0.0
        for p in self.open.values():
            if station_icao is not None and p.station_icao != station_icao:
                continue
            if target_date is not None and p.target_date != target_date:
                continue
            total += p.size_usd
        return total

    # --- marking -----------------------------------------------------------

    def mark(self, ts: int, price_of: Callable[[Position], Optional[float]]) -> float:
        """
        Mark the book to market at simulated time ts and append the point
        to the equity curve.

        equity = cash + SUM over open positions of
                 size_usd * (current_price / entry_price)

        price_of() returning None means "no usable quote right now" (a
        genuine gap in the history, or a quote too stale to trust -- see
        settings.MAX_STALENESS_FACTOR). Such a position marks at its last
        observed price, or at entry_price if it has never been priced.
        That is deliberately not the same as dropping it from equity: an
        unquotable position is still money at risk, and excluding it would
        flatter both the equity curve and the drawdown figure.

        Returns the equity value recorded.
        """
        equity = self.cash
        for position in self.open.values():
            price = price_of(position)
            if price is None:
                price = self._last_price.get(position.position_id, position.entry_price)
            else:
                self._last_price[position.position_id] = price
            equity += self._position_value(position, price)

        self.equity_curve.append((int(ts), equity))

        if equity > self.peak_equity:
            self.peak_equity = equity
        elif self.peak_equity > 0:
            drawdown = (self.peak_equity - equity) / self.peak_equity
            if drawdown > self.max_drawdown_pct:
                self.max_drawdown_pct = drawdown

        return equity

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _position_value(position: Position, price: Optional[float]) -> float:
        """
        Current dollar value of a position's stake at `price`. Falls back
        to entry_price (i.e. valued at cost) when there is no price at
        all, and guards the degenerate entry_price <= 0 case the same way
        risk_manager.compute_pnl_pct() does rather than dividing by zero.
        """
        if price is None:
            price = position.entry_price
        if position.entry_price is None or position.entry_price <= 0:
            return position.size_usd
        return position.size_usd * (price / position.entry_price)


def new_portfolio(bankroll_mode: str = "static") -> PortfolioState:
    """Convenience constructor at config.BANKROLL_USD parity -- the live system's sizing bankroll."""
    return PortfolioState(bankroll_usd=config.BANKROLL_USD, bankroll_mode=bankroll_mode)
