"""
backtest/compare.py -- the A/B tool.

Two of these pin mistakes made by hand on 2026-08-10 that the tool exists
to prevent: dropping unresolved positions from the P&L, and reporting a
three-trade result as though it compared anything.
"""

from datetime import date

import pytest

import calibration
import config
from backtest import compare, engine


# --------------------------------------------------------------------------
# The accounting bug
# --------------------------------------------------------------------------

class _FakePortfolio:
    def __init__(self, initial, cash, open_value):
        self.initial_bankroll_usd = initial
        self.cash = cash
        self._open_value = open_value

    def current_equity(self):
        return self.cash + self._open_value


class _FakeRun:
    def __init__(self, portfolio, closed, unresolved, entry_pricing="ask"):
        self.portfolio = portfolio
        self.closed_positions = [object()] * closed
        self.unresolved_positions = [object()] * unresolved
        self.provenance = {"entry_pricing": entry_pricing}


def test_unresolved_positions_are_marked_to_market_not_dropped(monkeypatch):
    """
    THE bug. An arm that realised -$50 but still holds $80 of open
    positions is up $30, not down $50. Summing closed_positions alone
    charges it for the loss and credits it nothing for what it holds --
    which is exactly how the first hand-run made the shipped config look
    $185 worse than it was.
    """
    pf = _FakePortfolio(initial=1000.0, cash=950.0, open_value=80.0)
    monkeypatch.setattr(engine, "run", lambda **kw: _FakeRun(pf, closed=3, unresolved=2))

    w = compare._run_window("WSSS", date(2026, 8, 6), date(2026, 8, 9), None)

    assert w.realised_usd == pytest.approx(-50.0)
    assert w.unrealised_usd == pytest.approx(80.0)
    assert w.total_usd == pytest.approx(30.0)
    assert w.unresolved == 2


def test_realised_and_unrealised_are_reported_separately(monkeypatch):
    """A total that hides its composition invites the same mistake again."""
    pf = _FakePortfolio(initial=1000.0, cash=1100.0, open_value=0.0)
    monkeypatch.setattr(engine, "run", lambda **kw: _FakeRun(pf, closed=4, unresolved=0))

    w = compare._run_window("WSSS", date(2026, 8, 6), date(2026, 8, 9), None)

    assert w.realised_usd == pytest.approx(100.0)
    assert w.unrealised_usd == pytest.approx(0.0)


def test_a_failed_window_does_not_sink_the_sweep(monkeypatch):
    def boom(**kw):
        raise ValueError("no market data")

    monkeypatch.setattr(engine, "run", boom)
    w = compare._run_window("RJTT", date(2026, 8, 6), date(2026, 8, 9), None)

    assert w.error.startswith("ValueError")
    assert w.total_usd == 0.0


# --------------------------------------------------------------------------
# The verdict guard
# --------------------------------------------------------------------------

def _arm(name, closed, total):
    a = compare.ArmResult(name=name, description="")
    a.windows.append(compare.WindowResult(
        station="WSSS", start=date(2026, 8, 6), end=date(2026, 8, 9),
        closed=closed, realised_usd=total,
    ))
    return a


def test_thin_samples_get_no_verdict():
    """
    The 2026-08-10 numbers exactly: 3 trades, a $185 gap, and one trade
    carrying all of it. The tool must refuse to call that.
    """
    arms = [_arm("shipped", 3, -107.11), _arm("legacy-all", 3, 78.25)]

    v = compare.verdict(arms)

    assert v.startswith("NO VERDICT")
    assert "3 trade" in v
    assert str(compare.MIN_TRADES_FOR_A_VERDICT) in v


def test_a_verdict_appears_once_the_sample_is_big_enough():
    n = compare.MIN_TRADES_FOR_A_VERDICT
    arms = [_arm("shipped", n, 400.0), _arm("legacy-all", n, 100.0)]

    v = compare.verdict(arms)

    assert not v.startswith("NO VERDICT")
    assert "shipped" in v
    assert "300.00" in v


def test_the_verdict_is_gated_on_the_THINNEST_arm():
    """One well-populated arm must not license a verdict over a starved one."""
    arms = [_arm("shipped", compare.MIN_TRADES_FOR_A_VERDICT * 2, 500.0),
            _arm("legacy-all", 2, -10.0)]

    assert compare.verdict(arms).startswith("NO VERDICT")


def test_report_warns_when_entries_were_priced_off_the_bid(monkeypatch):
    pf = _FakePortfolio(initial=1000.0, cash=1000.0, open_value=0.0)
    monkeypatch.setattr(
        engine, "run",
        lambda **kw: _FakeRun(pf, closed=1, unresolved=0, entry_pricing="bid_fallback"),
    )
    arm = compare.run_arm("shipped", [("WSSS", date(2026, 8, 6), date(2026, 8, 9))])

    text = compare.format_report([arm])

    assert "bid_fallback" in text
    assert "overstates edge" in text


# --------------------------------------------------------------------------
# Arms restore what they patch
# --------------------------------------------------------------------------

def test_legacy_spread_arm_restores_the_estimator():
    original = calibration.estimate_std_dev
    with compare.ARMS["legacy-spread"][1]():
        assert calibration.estimate_std_dev is not original
    assert calibration.estimate_std_dev is original


def test_legacy_blend_arm_restores_config():
    default = config.FORECAST_BLEND_WEIGHT_DEFAULT
    by_station = dict(config.FORECAST_BLEND_WEIGHT_BY_STATION)

    with compare.ARMS["legacy-blend"][1]():
        assert config.FORECAST_BLEND_WEIGHT_DEFAULT == 0.40
        assert config.FORECAST_BLEND_WEIGHT_BY_STATION == {}

    assert config.FORECAST_BLEND_WEIGHT_DEFAULT == default
    assert config.FORECAST_BLEND_WEIGHT_BY_STATION == by_station


def test_arms_restore_even_if_a_run_raises():
    """A sweep that dies mid-arm must not leave the process misconfigured."""
    default = config.FORECAST_BLEND_WEIGHT_DEFAULT
    with pytest.raises(RuntimeError):
        with compare.ARMS["legacy-all"][1]():
            raise RuntimeError("boom")
    assert config.FORECAST_BLEND_WEIGHT_DEFAULT == default


def test_legacy_spread_arm_reproduces_the_old_behaviour():
    """
    The historical baseline has to actually be the old chain, or the
    comparison is against a straw man. Three disagreeing point forecasts
    used to produce a wide spread; that is the signature of the old tier.
    """
    from models import PointForecast

    fc = [PointForecast(station_icao="WSSS", source=s, target_date=date(2026, 8, 10),
                        max_temp_c=t, fetched_at="")
          for s, t in (("a", 28.0), ("b", 32.0), ("c", 36.0))]

    with compare.ARMS["legacy-spread"][1]():
        sd, src = calibration.estimate_std_dev(fc, [], station_icao="WSSS")

    assert src == "forecast_variance"
    assert sd == pytest.approx(4.0)


# --------------------------------------------------------------------------
# Window discovery
# --------------------------------------------------------------------------

def test_discover_windows_reads_the_store(tmp_path):
    from backtest import price_store

    db = str(tmp_path / "market.sqlite3")
    for day in ("2026-08-06", "2026-08-09"):
        price_store.upsert_token(
            token_id=f"t{day}", station_icao="WSSS", target_date=day, bucket_c=32,
            side="yes", event_slug="e", discovered_at="2026-08-01T00:00:00+00:00",
            db_path=db,
        )

    windows = compare.discover_windows(db)

    assert windows == [("WSSS", date(2026, 8, 6), date(2026, 8, 9))]


def test_discover_windows_ignores_unregistered_stations(tmp_path):
    from backtest import price_store

    db = str(tmp_path / "market.sqlite3")
    price_store.upsert_token(
        token_id="x", station_icao="ZZZZ", target_date="2026-08-06", bucket_c=32,
        side="yes", event_slug="e", discovered_at="2026-08-01T00:00:00+00:00", db_path=db,
    )

    assert compare.discover_windows(db) == []
