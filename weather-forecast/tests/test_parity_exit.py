"""
tests/test_parity_exit.py

Covers risk_manager.evaluate_exit()'s local_hour handling:

1. Parity: an explicit local_hour must produce exactly the same
   ExitDecision as the default (wall-clock-derived) path, when the
   wall clock is pinned to that same hour. risk_manager._local_hour()
   derives "now" via `datetime.now(timezone.utc).hour`, converted to
   SGT/MYT (UTC+8) -- see risk_manager.py lines 43-51. We monkeypatch
   the `datetime` name risk_manager imported (`from datetime import
   datetime, timezone`) so `datetime.now(timezone.utc)` returns a
   fixed instant, then check the wall-clock call and the explicit-hour
   call agree for every hour 0-23.

2. Boundary: the tightened thresholds (config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL)
   must flip the decision at exactly that hour, not one hour early/late.
"""
from datetime import datetime, timezone

import config
import risk_manager
from models import Position


def _make_position(entry_price: float, high_water_mark: float = None) -> Position:
    return Position(
        position_id="WSSS:2026-08-02:31:YES:2026-08-02T05:00:00",
        station_icao="WSSS",
        target_date=datetime(2026, 8, 2).date(),
        bucket_c=31,
        side="YES",
        entry_price=entry_price,
        size_usd=50.0,
        entry_time="2026-08-02T05:00:00",
        high_water_mark=high_water_mark,
    )


class _FakeDateTime:
    """Stand-in for the `datetime` name risk_manager imported.

    risk_manager._local_hour() only ever calls `datetime.now(timezone.utc)`
    and reads `.hour` off the result, so this only needs to support that
    one call -- it returns a real `datetime` instance (not a fake), just
    constructed with a caller-controlled hour.
    """

    fixed_utc_hour = 0

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 2, cls.fixed_utc_hour, 0, 0, tzinfo=tz)


def test_explicit_hour_matches_wall_clock_for_every_hour(monkeypatch):
    monkeypatch.setattr(risk_manager, "datetime", _FakeDateTime)

    # Edge-sensitive scenario (a 20% loss) so the decision itself is
    # meaningful evidence the same threshold set was actually used on
    # both paths, not just a coincidental "hold" on both sides.
    #
    # Entry 0.40 / price 0.32 is still exactly -20%, but the entry price
    # now has to be a REAL one: thresholds are fractions of the risk unit
    # min(entry, 1-entry), so the old entry_price=1.0 is a degenerate
    # position with zero upside and a clamped 0.01 risk unit, which stops
    # out at every hour and tests nothing about the tighten boundary.
    position = _make_position(entry_price=0.40)
    current_price = 0.32

    for h in range(24):
        utc_hour = (h - 8) % 24
        _FakeDateTime.fixed_utc_hour = utc_hour

        wall_clock_decision = risk_manager.evaluate_exit(position, current_price)
        explicit_decision = risk_manager.evaluate_exit(
            position, current_price, local_hour=h
        )

        assert explicit_decision == wall_clock_decision, (
            f"local_hour={h} (utc_hour={utc_hour}) diverged: "
            f"explicit={explicit_decision} wall_clock={wall_clock_decision}"
        )


def test_decision_flips_exactly_at_tighten_hour():
    # Stop-loss thresholds: normal 0.30, tightened 0.15 (config.py), both
    # applied to the risk unit min(entry, 1-entry) -- here 0.40, so the
    # stop sits 0.12 below entry before the tighten hour and 0.06 below
    # it after. A pinned -0.20 pnl (0.08 below a 0.40 entry) is a hold
    # under the loose threshold and a stop_loss under the tightened one:
    # exactly straddles the config-driven boundary this test checks.
    assert config.STOP_LOSS_PCT == 0.30
    assert config.TIGHTENED_STOP_LOSS_PCT == 0.15

    position = _make_position(entry_price=0.40)
    current_price = 0.32  # pnl_pct == -0.20

    decisions = {
        h: risk_manager.evaluate_exit(position, current_price, local_hour=h)
        for h in range(24)
    }

    tighten_hour = config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL

    for h in range(24):
        decision = decisions[h]
        if h < tighten_hour:
            assert decision.should_exit is False, (
                f"hour {h} (< tighten hour {tighten_hour}) unexpectedly exited: {decision}"
            )
            assert decision.reason == "hold"
        else:
            assert decision.should_exit is True, (
                f"hour {h} (>= tighten hour {tighten_hour}) unexpectedly held: {decision}"
            )
            assert decision.reason == "stop_loss"

    # And explicitly confirm the transition is at the configured hour,
    # not one before or one after it.
    assert decisions[tighten_hour - 1].should_exit is False
    assert decisions[tighten_hour].should_exit is True
