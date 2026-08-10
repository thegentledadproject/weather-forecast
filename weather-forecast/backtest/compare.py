"""
backtest/compare.py

PURPOSE
-------
A/B one config change against another over the same replayed history, so a
calibration or sizing decision can be argued from P&L instead of from a
proxy metric. Built after the 2026-08-10 spread-estimator work, where Brier
improved 10.3% and nobody could say whether that was worth anything.

WHY THIS IS NOT JUST "RUN THE BACKTEST TWICE"
----------------------------------------------
Three things went wrong doing it by hand, and each one is fixed here rather
than left for the next person to rediscover.

1. UNRESOLVED POSITIONS WERE DROPPED. Summing P&L over closed_positions
   charges an arm for every loss it realised and credits it for nothing
   still open. On the first attempt one arm ended with 3 closed and 2
   unresolved, and its "-$107.11" was that asymmetry as much as anything
   real. P&L here is portfolio.current_equity() - initial_bankroll_usd,
   which marks open positions to market, and realised/unrealised are
   reported separately so the split is visible rather than implied.

2. A RESULT WITH THREE TRADES WAS PRESENTED AS A RESULT. verdict() refuses
   to call anything at all below MIN_TRADES_FOR_A_VERDICT, and says how
   short it is. A tool that prints a number and lets the reader supply the
   significance is worse than one that prints nothing.

3. THE ARMS TOOK DIFFERENT TRADES ON DIFFERENT DAYS and were compared as
   though paired. They are still not paired -- changing calibration changes
   which trades exist, which is the point -- but the report shows each
   arm's trade count and window breakdown so that is impossible to miss.

WHAT THIS CANNOT TELL YOU
--------------------------
Replays price entries off the BID for any snapshot captured before
2026-08-10, because no ask was stored (see price_store.save_snapshot). That
overstates edge in EVERY arm, so a COMPARISON stays meaningful while the
absolute P&L does not. Runs are labelled with the engine's own
entry_pricing provenance so a bid-priced result cannot be quoted as if it
were realistic.

DEPENDENCIES
------------
contextlib, dataclasses, io, statistics (standard library)
config.py, calibration.py (local)
backtest/{engine,settings,price_store}.py (local)
"""

import contextlib
import io
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import calibration
import config

from backtest import engine, price_store, settings

# Below this many closed trades, summed across every window, the tool
# declines to compare. Chosen as the point where a single trade stops
# deciding the sign: the 2026-08-10 attempt had 3, and one +$80 trade was
# the entire difference between the arms.
MIN_TRADES_FOR_A_VERDICT = 30


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _shipped():
    """Whatever config.py currently says. The control."""
    yield


@contextlib.contextmanager
def _legacy_spread():
    """
    calibration.estimate_std_dev as it stood before 2026-08-10: ensemble,
    then stdev ACROSS THE POINT FORECASTS, then stdev across observed
    history, then a flat 1.2C. Kept verbatim as the historical baseline --
    if the current estimator is ever questioned, this is what it replaced.
    """
    def old_chain(forecasts, observations, ensemble_members=None,
                  station_icao=None, allow_measured=True):
        if ensemble_members and len(ensemble_members) > 1:
            return round(statistics.stdev(ensemble_members), 2), "ensemble"
        valid = [f.max_temp_c for f in forecasts if f.max_temp_c is not None]
        if len(valid) > 1:
            return round(statistics.stdev(valid), 2), "forecast_variance"
        if observations and len(observations) > 1:
            return round(statistics.stdev(o.max_temp_c for o in observations), 2), "observed_variance"
        return 1.2, "fallback_default"

    original = calibration.estimate_std_dev
    calibration.estimate_std_dev = old_chain
    try:
        yield
    finally:
        calibration.estimate_std_dev = original


@contextlib.contextmanager
def _legacy_blend():
    """The 40/60 forecast/observed split, with no per-station override."""
    default = config.FORECAST_BLEND_WEIGHT_DEFAULT
    by_station = dict(config.FORECAST_BLEND_WEIGHT_BY_STATION)
    config.FORECAST_BLEND_WEIGHT_DEFAULT = 0.40
    config.FORECAST_BLEND_WEIGHT_BY_STATION = {}
    try:
        yield
    finally:
        config.FORECAST_BLEND_WEIGHT_DEFAULT = default
        config.FORECAST_BLEND_WEIGHT_BY_STATION = by_station


@contextlib.contextmanager
def _legacy_all():
    with _legacy_spread(), _legacy_blend():
        yield


ARMS: Dict[str, Tuple[str, Callable]] = {
    "shipped": ("current config.py", _shipped),
    "legacy-spread": ("pre-2026-08-10 estimate_std_dev", _legacy_spread),
    "legacy-blend": ("blend weight 0.40 everywhere", _legacy_blend),
    "legacy-all": ("both of the above", _legacy_all),
}


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def discover_windows(market_db_path=None) -> List[Tuple[str, date, date]]:
    """
    (station, first_date, last_date) for every station with market data.

    Read from the store rather than hardcoded, so the comparison widens by
    itself as collection continues -- the 2026-08-10 attempt used a fixed
    list and silently stopped covering new stations.
    """
    with price_store._db(market_db_path) as conn:
        rows = conn.execute(
            "SELECT station_icao, MIN(target_date), MAX(target_date) "
            "FROM market_tokens GROUP BY station_icao ORDER BY station_icao"
        ).fetchall()
    return [
        (icao, date.fromisoformat(lo), date.fromisoformat(hi))
        for icao, lo, hi in rows
        if icao in config.STATIONS
    ]


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

@dataclass
class WindowResult:
    station: str
    start: date
    end: date
    closed: int = 0
    unresolved: int = 0
    realised_usd: float = 0.0
    unrealised_usd: float = 0.0
    entry_pricing: str = ""
    error: str = ""

    @property
    def total_usd(self) -> float:
        return self.realised_usd + self.unrealised_usd


@dataclass
class ArmResult:
    name: str
    description: str
    windows: List[WindowResult] = field(default_factory=list)

    @property
    def closed(self) -> int:
        return sum(w.closed for w in self.windows)

    @property
    def unresolved(self) -> int:
        return sum(w.unresolved for w in self.windows)

    @property
    def realised_usd(self) -> float:
        return sum(w.realised_usd for w in self.windows)

    @property
    def unrealised_usd(self) -> float:
        return sum(w.unrealised_usd for w in self.windows)

    @property
    def total_usd(self) -> float:
        return self.realised_usd + self.unrealised_usd

    @property
    def errors(self) -> List[WindowResult]:
        return [w for w in self.windows if w.error]


def _run_window(station: str, start: date, end: date, market_db_path) -> WindowResult:
    result = WindowResult(station=station, start=start, end=end)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run = engine.run(
                station_icao=station, start_date=start, end_date=end,
                depth_regime="strict", fee_rate_pct=None, bankroll_mode="static",
                market_db_path=str(market_db_path or settings.MARKET_DATA_DB),
            )
    except Exception as exc:  # noqa: BLE001 -- one bad window must not sink the sweep
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    pf = run.portfolio
    # TOTAL P&L, marking open positions to market. Deriving it from
    # closed_positions alone is the accounting bug this tool exists to
    # avoid: it charges an arm for realised losses and credits it nothing
    # for what it still holds.
    total = pf.current_equity() - pf.initial_bankroll_usd
    realised = pf.cash - pf.initial_bankroll_usd

    result.closed = len(run.closed_positions)
    result.unresolved = len(run.unresolved_positions)
    result.realised_usd = realised
    result.unrealised_usd = total - realised
    result.entry_pricing = str(run.provenance.get("entry_pricing", "unknown"))
    return result


def run_arm(name: str, windows: List[Tuple[str, date, date]], market_db_path=None) -> ArmResult:
    description, ctx = ARMS[name]
    arm = ArmResult(name=name, description=description)
    with ctx():
        for station, start, end in windows:
            arm.windows.append(_run_window(station, start, end, market_db_path))
    return arm


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def verdict(arms: List[ArmResult]) -> str:
    """
    What the numbers support, which is usually less than they suggest.

    Refuses to compare below MIN_TRADES_FOR_A_VERDICT. This is the whole
    reason the tool exists rather than a shell loop: the failure mode is
    not producing a wrong number, it is producing a real number and
    letting the reader assume it means something.
    """
    worst = min(a.closed for a in arms) if arms else 0
    if worst < MIN_TRADES_FOR_A_VERDICT:
        return (
            f"NO VERDICT -- the thinnest arm closed {worst} trade(s), against "
            f"{MIN_TRADES_FOR_A_VERDICT} needed before a single trade stops "
            f"deciding the sign. These totals are reported so the run is not "
            f"wasted, and must not be read as a comparison. Collect more days "
            f"and re-run; no code change shortens this."
        )
    best = max(arms, key=lambda a: a.total_usd)
    spread = best.total_usd - min(a.total_usd for a in arms)
    return (
        f"'{best.name}' leads by ${spread:.2f} over the weakest arm on "
        f"{best.closed} closed trades. Still a single unpaired sample -- treat "
        f"as directional, not decisive."
    )


def format_report(arms: List[ArmResult]) -> str:
    out: List[str] = []
    out.append(f"{'arm':<16} {'closed':>7} {'unres':>6} {'realised':>11} "
               f"{'unrealised':>11} {'TOTAL':>11}")
    out.append("-" * 66)
    for arm in arms:
        out.append(
            f"{arm.name:<16} {arm.closed:>7} {arm.unresolved:>6} "
            f"{arm.realised_usd:>10.2f} {arm.unrealised_usd:>11.2f} {arm.total_usd:>11.2f}"
        )

    pricing = {w.entry_pricing for a in arms for w in a.windows if w.entry_pricing}
    if pricing - {"", "unknown"}:
        out.append("")
        out.append(f"entry pricing across windows: {sorted(pricing)}")
        if any(p != "ask" for p in pricing if p and p != "unknown"):
            out.append(
                "  NOTE: any window not priced 'ask' replays entries at the BID, "
                "which overstates edge in every arm. Comparisons survive it; "
                "absolute P&L does not."
            )

    errors = {w.error for a in arms for w in a.windows if w.error}
    if errors:
        out.append("")
        out.append(f"{len(errors)} window(s) failed:")
        for e in sorted(errors):
            out.append(f"  {e}")

    out.append("")
    out.append("per-window closed/total, by arm:")
    keys = [(w.station, w.start) for w in arms[0].windows] if arms else []
    for station, start in keys:
        cells = []
        for arm in arms:
            w = next((x for x in arm.windows if x.station == station and x.start == start), None)
            cells.append("err" if (w and w.error) else
                         (f"{w.closed}/${w.total_usd:+.2f}" if w else "-"))
        out.append(f"  {station} {start.isoformat()}  " + "  ".join(f"{c:<16}" for c in cells))

    out.append("")
    out.append(verdict(arms))
    return "\n".join(out)


def compare(arm_names: List[str], windows=None, market_db_path=None) -> List[ArmResult]:
    windows = windows if windows is not None else discover_windows(market_db_path)
    return [run_arm(name, windows, market_db_path) for name in arm_names]
