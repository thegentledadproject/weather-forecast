"""
tests/test_exit_loop_isolation.py

Two 2026-08-09 fixes that are about CONTAINMENT rather than arithmetic:

1. position_manager.check_and_exit_positions() must survive one position
   raising. The scheduler already wraps the whole call in a try/except,
   which keeps the daemon alive but abandons the rest of the cycle -- so
   before this fix, one bad row meant every position after it in the
   list went unchecked, on every cycle, indefinitely. The two known
   raisers are permanent per-row conditions: _token_id_for() on any
   position opened before token_id was threaded through, and executor's
   auto-mode sell, which raises by design.

2. The re-entry cooldown must count trailing-stop exits, not just
   stop-losses. Excluding them left the stop -> re-buy -> trail out ->
   re-buy churn loop open through the other door.
"""
from datetime import date, timedelta

import config
import entry_manager
import position_manager
import storage
from clients import market_client
from models import Position

# Deliberately in the future rather than a fixed calendar date: a
# past-dated bucket routes position_manager through the resolution
# branch, which calls out to Gamma. A hardcoded date would quietly turn
# these into network tests the day after it passed.
TARGET_DATE = date.today() + timedelta(days=2)


def _pos(position_id: str, token_id="tok", entry_price=0.30, status="open") -> Position:
    return Position(
        position_id=position_id,
        station_icao="WSSS",
        target_date=TARGET_DATE,
        bucket_c=31,
        side="YES",
        entry_price=entry_price,
        size_usd=50.0,
        entry_time="2026-08-09T05:00:00",
        status=status,
        token_id=token_id,
    )


class TestOneBadPositionDoesNotStrandTheRest:
    def test_position_with_no_token_id_does_not_abort_the_cycle(self, monkeypatch):
        """
        The legacy-row case. The first position has no token_id, so
        _token_id_for() raises -- the second must still be evaluated.
        """
        poisoned = _pos("legacy-no-token", token_id=None)
        healthy = _pos("healthy")

        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [poisoned, healthy])
        monkeypatch.setattr(
            market_client, "get_current_price_for_side", lambda token_id, side: 0.31,
        )

        decisions = position_manager.check_and_exit_positions()

        assert [d.position_id for d in decisions] == ["healthy"], (
            "the healthy position after the poisoned one was never evaluated"
        )

    def test_a_raising_exit_does_not_abort_the_cycle(self, monkeypatch):
        """
        The auto-mode case: closing the first position raises, and the
        second must still be checked. Ordered so the raiser comes first.
        """
        stopping = _pos("stops-out")
        healthy = _pos("healthy")

        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [stopping, healthy])
        monkeypatch.setattr(storage, "update_high_water_mark", lambda *a, **kw: None)

        # Price collapses for the first position only -> it decides to
        # exit. Both fixtures share a token id, so which position is
        # being checked has to be threaded through explicitly.
        prices = {"stops-out": 0.10, "healthy": 0.31}
        current_id = ["stops-out"]
        original = position_manager._check_one_position

        def _tracking(position):
            current_id[0] = position.position_id
            return original(position)

        monkeypatch.setattr(position_manager, "_check_one_position", _tracking)
        monkeypatch.setattr(
            market_client,
            "get_current_price_for_side",
            lambda token_id, side: prices[current_id[0]],
        )

        def _boom(position, decision, status=None, exit_reason=None):
            raise NotImplementedError("auto-mode order placement is not implemented")

        monkeypatch.setattr(position_manager.executor, "close_position", _boom)

        decisions = position_manager.check_and_exit_positions()

        assert [d.position_id for d in decisions] == ["healthy"], (
            "a position whose close raised took the rest of the cycle down with it"
        )

    def test_a_healthy_cycle_still_returns_every_decision(self, monkeypatch):
        """Guard against the isolation swallowing normal results."""
        positions = [_pos("a"), _pos("b"), _pos("c")]
        monkeypatch.setattr(storage, "load_open_positions", lambda **kw: positions)
        monkeypatch.setattr(storage, "update_high_water_mark", lambda *a, **kw: None)
        monkeypatch.setattr(
            market_client, "get_current_price_for_side", lambda token_id, side: 0.31,
        )

        decisions = position_manager.check_and_exit_positions()

        assert [d.position_id for d in decisions] == ["a", "b", "c"]
        assert all(d.should_exit is False for d in decisions)


class TestCooldownCountsTrailingStops:
    def test_trailing_stop_exit_trips_the_re_entry_cooldown(self, monkeypatch):
        history = [_pos("t", status="closed_trailing_stop")]
        monkeypatch.setattr(
            storage, "load_position_history", lambda *a, **kw: list(history),
        )

        count = entry_manager.count_stop_outs_for_bucket(
            "WSSS", TARGET_DATE, 31, "YES", is_paper=True,
        )
        assert count == 1
        assert count >= config.MAX_STOP_OUTS_PER_BUCKET_PER_DAY, (
            "a trailing-stop exit must be enough to block re-entry for the day"
        )

    def test_stop_loss_still_counts(self, monkeypatch):
        history = [_pos("s", status="closed_stop_loss")]
        monkeypatch.setattr(
            storage, "load_position_history", lambda *a, **kw: list(history),
        )
        assert entry_manager.count_stop_outs_for_bucket(
            "WSSS", TARGET_DATE, 31, "YES", is_paper=True,
        ) == 1

    def test_resolution_close_does_not_count(self, monkeypatch):
        """
        Only price-noise exits trip the cooldown. A market that simply
        resolved says nothing about whether the entry was rejected.
        """
        history = [_pos("r", status="closed_resolution")]
        monkeypatch.setattr(
            storage, "load_position_history", lambda *a, **kw: list(history),
        )
        assert entry_manager.count_stop_outs_for_bucket(
            "WSSS", TARGET_DATE, 31, "YES", is_paper=True,
        ) == 0
