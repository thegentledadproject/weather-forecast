"""
manual_trigger.py -- the real-money path with the fewest gates.

It bypasses the MODEL by design (no forecast, probability, EV, Kelly or edge
threshold) and keeps the mechanical safeguards. These tests pin the two
things it got wrong about the gates it does keep:

  - it skipped MAX_OPEN_POSITIONS_PER_BUCKET, because building an approved
    EntryDecision by hand never passes through entry_manager.evaluate_entry()
    where that gate lives. Repeated invocations stacked the same bucket.
  - it claimed "position_manager will now manage its exits" unconditionally,
    which is false whenever the deployed daemon is not in live mode -- its
    normal state -- and is how a real position came to sit unmanaged.
"""

from datetime import date

import pytest

import config
import entry_manager
import manual_trigger


# --------------------------------------------------------------------------
# Reading the DEPLOYED daemon's mode
# --------------------------------------------------------------------------

def test_daemon_mode_is_read_from_the_mode_file(tmp_path, monkeypatch):
    f = tmp_path / "mode.env"
    f.write_text("# a comment\nPOLYWEATHER_MODE=live\nPOLYWEATHER_FALLBACK_MODE=paper\n",
                 encoding="utf-8")
    monkeypatch.setattr(manual_trigger, "MODE_FILE", str(f))

    assert manual_trigger._daemon_mode() == "live"


def test_a_quoted_mode_is_unwrapped(tmp_path, monkeypatch):
    f = tmp_path / "mode.env"
    f.write_text('POLYWEATHER_MODE="simulation"\n', encoding="utf-8")
    monkeypatch.setattr(manual_trigger, "MODE_FILE", str(f))

    assert manual_trigger._daemon_mode() == "simulation"


def test_an_absent_mode_file_reports_unknown_not_live(tmp_path, monkeypatch):
    """
    "unknown" must never be optimistically read as live -- the whole point is
    to stop claiming something will be managed when that cannot be known.
    """
    monkeypatch.setattr(manual_trigger, "MODE_FILE", str(tmp_path / "absent.env"))

    assert manual_trigger._daemon_mode() == "unknown"


def test_a_mode_file_without_the_key_reports_unknown(tmp_path, monkeypatch):
    f = tmp_path / "mode.env"
    f.write_text("POLYWEATHER_FALLBACK_MODE=paper\n", encoding="utf-8")
    monkeypatch.setattr(manual_trigger, "MODE_FILE", str(f))

    assert manual_trigger._daemon_mode() == "unknown"


# --------------------------------------------------------------------------
# The per-bucket cap
# --------------------------------------------------------------------------

def _run(monkeypatch, open_count, mode="simulation"):
    """Drive main() far enough to hit the cap, with all I/O stubbed."""
    monkeypatch.setattr(
        entry_manager, "count_open_positions_for_bucket",
        lambda *a, **kw: open_count,
    )
    monkeypatch.setattr(manual_trigger, "_parse_args", lambda: _Args(mode))
    return manual_trigger.main()


class _Args:
    def __init__(self, mode):
        self.station = "WSSS"
        self.bucket = 33
        self.side = "YES"
        self.date = "2026-08-11"
        self.mode = mode
        self.yes = True
        self.i_understand_this_spends_real_money = True


def test_a_second_leg_on_the_same_bucket_is_refused(monkeypatch, capsys):
    """
    Repeat entries on one bucket are not independent bets, they are the same
    bet sized up by accident -- the failure MAX_OPEN_POSITIONS_PER_BUCKET was
    added for after one bucket was entered four times on day 1.
    """
    rc = _run(monkeypatch, open_count=config.MAX_OPEN_POSITIONS_PER_BUCKET)
    out = capsys.readouterr().out

    assert rc == 1
    assert "MAX_OPEN_POSITIONS_PER_BUCKET" in out
    assert "same bet twice" in out


def test_an_unreadable_position_count_refuses_rather_than_assuming_zero(monkeypatch, capsys):
    rc = _run(monkeypatch, open_count=None)
    out = capsys.readouterr().out

    assert rc == 1
    assert "refusing to open blind" in out


def test_the_cap_is_counted_on_the_track_the_order_lands_on(monkeypatch):
    """
    is_paper must match what executor.open_position() will stamp, or the cap
    is counted against one track while the position is filed under another.
    """
    seen = {}

    def _count(station, target_date, bucket, side, is_paper=None):
        seen["is_paper"] = is_paper
        return 0

    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", _count)
    monkeypatch.setattr(manual_trigger, "_parse_args", lambda: _Args("live"))
    monkeypatch.setattr(manual_trigger.market_discovery, "discover_token_map",
                        lambda *a, **kw: {})
    manual_trigger.main()

    assert seen["is_paper"] is False, "a live order must be counted on the live track"


def test_simulation_counts_against_the_paper_track(monkeypatch):
    seen = {}

    def _count(station, target_date, bucket, side, is_paper=None):
        seen["is_paper"] = is_paper
        return 0

    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", _count)
    monkeypatch.setattr(manual_trigger, "_parse_args", lambda: _Args("simulation"))
    monkeypatch.setattr(manual_trigger.market_discovery, "discover_token_map",
                        lambda *a, **kw: {})
    manual_trigger.main()

    assert seen["is_paper"] is True


def test_the_cap_runs_before_any_market_call(monkeypatch):
    """Cheap refusals should not cost a discovery round-trip."""
    def _explode(*a, **kw):
        raise AssertionError("discovery ran despite the bucket cap being breached")

    monkeypatch.setattr(manual_trigger.market_discovery, "discover_token_map", _explode)
    assert _run(monkeypatch, open_count=config.MAX_OPEN_POSITIONS_PER_BUCKET) == 1


def test_no_model_means_net_ev_is_none_not_zero(monkeypatch):
    """
    None is "unknown"; 0.0 is "measured as exactly break-even". Conflating
    them made executor._resolved_size_ok() -- which refuses an upsized order
    whose net EV is not positive -- reject EVERY manual order, including on a
    bucket with $216 of visible depth. Found by running the chain end to end.
    """
    import inspect

    src = inspect.getsource(manual_trigger.main)
    assert "net_ev_at_size=None" in src, (
        "manual_trigger must report net EV as None when no model ran; a 0.0 "
        "sentinel is read downstream as a real break-even measurement"
    )


def test_the_size_recheck_skips_the_ev_test_when_there_is_no_model_number(monkeypatch):
    import executor
    from clients import market_client
    from models import EntryDecision
    from datetime import date as _date

    monkeypatch.setattr(market_client, "estimate_slippage", lambda t, s: 0.0)
    # Depth is re-read from the live book at submission time now, not taken
    # from decision.available_depth_usd. Pinned deep enough not to bind, so
    # this test stays about the missing net-EV number.
    monkeypatch.setattr(market_client, "get_available_depth_usd", lambda t: 216.0)
    # The day-budget re-check (P1-1) reads stored exposure, so without this the
    # test's result would depend on whatever the ambient database happens to
    # hold. Pinned to zero spend for the same reason depth is pinned deep: this
    # test is about the missing net-EV number and nothing else.
    import entry_manager
    monkeypatch.setattr(entry_manager, "station_day_exposure_usd",
                        lambda icao, target_date, is_paper=None: 0.0)
    monkeypatch.setattr(entry_manager, "portfolio_day_exposure_usd",
                        lambda is_paper=None, region=None: 0.0)
    decision = EntryDecision(
        station_icao="WSSS", target_date=_date(2026, 8, 12), bucket_c=32, side="YES",
        kelly_fraction_raw=0.0, kelly_fraction_applied=0.0, recommended_size_usd=1.0,
        available_depth_usd=216.0, slippage_at_size_pct=0.0, net_ev_at_size=None,
        approved=True, reason="manual", station_maturity="mature",
        entry_price=0.69, token_id="TOK",
    )
    spec = type("S", (), {"notional_usd": 3.45, "limit_price": 0.70, "size_shares": 5.0})()

    ok, note = executor._resolved_size_ok(spec, decision)

    assert ok, f"an upsized manual order was refused for lack of a model number: {note}"
