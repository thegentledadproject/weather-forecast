"""
paper_trading_report.py

PURPOSE
-------
Turns accumulated paper-trading history (Position rows with
is_paper=True, closed over time via the normal position_manager ->
risk_manager -> executor loop, just with executor.EXECUTION_MODE set
to "paper" instead of "manual_review"/"auto") into a real performance
record.

This is the practical answer to backtest_engine.run_trading_strategy_
backtest()'s deliberate refusal to run: that function is blocked
because no HISTORICAL Polymarket price data exists anywhere in this
codebase. Paper trading sidesteps that entirely by generating REAL
FORWARD data instead -- every paper position is priced against
genuinely live prices as time actually passes, so there's no
look-ahead bias question to even ask (unlike a retrospective backtest,
which would need historical price data this codebase doesn't have and
hasn't confirmed is even available).

The tradeoff: paper trading takes real calendar time to accumulate a
useful sample (weeks, not a batch script run), and it validates
sizing/exit LOGIC, not real fill quality -- paper fills assume the
decision price is achievable, which real slippage/partial-fill
mechanics (see wallet_client.py's noted gaps) may not match exactly.

DEPENDENCIES
------------
statistics (standard library)
storage.py, models.py (local)
"""

import argparse
import statistics
from typing import List, Optional

import config
import storage
from models import Position


def load_paper_history(station_icao: str, limit: int = 1000) -> List[Position]:
    """All closed paper positions for one station, most recent first."""
    return storage.load_position_history(station_icao, limit=limit, is_paper=True)


def _position_return_pct(position: Position) -> Optional[float]:
    """Realized return on this closed position, as a fraction of entry price."""
    if position.exit_price is None or position.entry_price is None or position.entry_price <= 0:
        return None
    return (position.exit_price - position.entry_price) / position.entry_price


def summarize_positions(positions: List[Position]) -> Optional[dict]:
    """
    Aggregate performance stats across a list of CLOSED positions held
    in memory: win rate, mean/total return, breakdown by exit reason
    (take_profit / stop_loss / trailing_stop -- lets you see whether
    the trailing stop is actually earning its complexity vs. a plain
    fixed take-profit would have). Returns None for an empty list, or
    for a list where no position has a usable realized return.

    Pure -- no I/O of any kind. That is the whole point of it being
    separate from summarize_paper_performance() below: a simulated run
    holds its closed positions in memory and never writes them to the
    live positions table, so it needs these metrics computed off a list.
    Both paths therefore report the SAME numbers computed by the SAME
    code, which is the only way a backtest figure and a paper-trading
    figure can honestly be compared side by side.

    station_icao is read off the positions themselves; callers are
    expected to pass a single station's positions, as
    summarize_paper_performance() does.
    """
    history = positions
    if not history:
        return None

    returns = [_position_return_pct(p) for p in history]
    returns = [r for r in returns if r is not None]
    if not returns:
        return None

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    by_reason = {}
    for p in history:
        reason = p.status.replace("closed_", "")
        by_reason.setdefault(reason, []).append(_position_return_pct(p))

    reason_summary = {}
    for reason, rets in by_reason.items():
        valid = [r for r in rets if r is not None]
        if valid:
            reason_summary[reason] = {
                "n": len(valid),
                "mean_return_pct": round(statistics.fmean(valid), 4),
            }

    return {
        "station_icao": history[0].station_icao,
        "n_trades": len(returns),
        "win_rate": round(len(wins) / len(returns), 4),
        "mean_return_pct": round(statistics.fmean(returns), 4),
        "total_return_pct_sum": round(sum(returns), 4),  # naive sum, not compounded -- see print_report note
        "std_return_pct": round(statistics.stdev(returns), 4) if len(returns) > 1 else 0.0,
        "mean_win_pct": round(statistics.fmean(wins), 4) if wins else None,
        "mean_loss_pct": round(statistics.fmean(losses), 4) if losses else None,
        "by_exit_reason": reason_summary,
    }


def summarize_paper_performance(station_icao: str) -> Optional[dict]:
    """
    Aggregate performance stats across all closed paper positions for
    one station -- the I/O half: load this station's paper history from
    storage, then hand it to summarize_positions() for the arithmetic.
    Returns None if there's no paper history yet for this station.

    Metric semantics are unchanged; every calculation, rounding and
    field name lives in summarize_positions() now, verbatim.
    """
    return summarize_positions(load_paper_history(station_icao))


def print_report(station_icao: str) -> None:
    """Human-readable console summary of one station's paper-trading track record."""
    summary = summarize_paper_performance(station_icao)
    if summary is None:
        print(f"[paper_trading_report] no closed paper positions yet for {station_icao} -- nothing to report.")
        return

    print(f"\n=== Paper Trading Report — {summary['station_icao']} ===")
    print(f"Trades closed:     {summary['n_trades']}")
    print(f"Win rate:          {summary['win_rate']:.1%}")
    print(f"Mean return/trade: {summary['mean_return_pct']:+.1%}  (std: {summary['std_return_pct']:.1%})")
    if summary["mean_win_pct"] is not None:
        print(f"Mean winning trade:  {summary['mean_win_pct']:+.1%}")
    if summary["mean_loss_pct"] is not None:
        print(f"Mean losing trade:   {summary['mean_loss_pct']:+.1%}")
    print(f"Sum of returns (naive, NOT compounded -- see note below): {summary['total_return_pct_sum']:+.1%}")
    print("\nBy exit reason:")
    for reason, stats in summary["by_exit_reason"].items():
        print(f"  {reason:15s} n={stats['n']:3d}  mean_return={stats['mean_return_pct']:+.1%}")
    print(
        "\nNote: 'sum of returns' adds each trade's % return independently -- it does NOT\n"
        "model compounding, position sizing interaction, or capital reuse across trades.\n"
        "Treat it as a rough per-trade-quality signal, not a portfolio-return figure."
    )
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print paper-trading performance reports.")
    parser.add_argument(
        "--station",
        default=None,
        help="ICAO code to report on (default: every station in config.STATIONS).",
    )
    args = parser.parse_args()

    if args.station:
        print_report(args.station)
    else:
        for icao in config.STATIONS:
            print_report(icao)
