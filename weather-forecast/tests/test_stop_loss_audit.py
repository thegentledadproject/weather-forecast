"""
tests/test_stop_loss_audit.py

Covers stop_loss_audit's classification and counterfactual arithmetic --
the pure half, so none of this touches a database.

The distances below are DERIVED from config.STOP_LOSS_PCT /
TIGHTENED_STOP_LOSS_PCT / risk_manager.risk_unit rather than written as
literals. That is the property the script itself claims: change the rule and
the audit re-scores against the new one. A test with 0.30 hardcoded in it
would keep passing while the audit silently measured the wrong threshold.
"""
from datetime import date

import config
import risk_manager
import stop_loss_audit as sla
from models import Position

ENTRY = 0.44
UNIT = risk_manager.risk_unit(ENTRY)
LOOSE_D = config.STOP_LOSS_PCT * UNIT
TIGHT_D = config.TIGHTENED_STOP_LOSS_PCT * UNIT
TIGHTEN_HOUR = config.EDGE_DECAY_TIGHTEN_HOUR_LOCAL


def _utc_hour_for_local(icao: str, local_hour: int) -> int:
    return (local_hour - config.get_station(icao).utc_offset_hours) % 24


def _stop(icao="WSSS", local_hour=11, bid=None, side="NO", bucket_c=32,
          status="closed_stop_loss", size_usd=2.20, note=True):
    """A closed stop-loss whose exit lands at `local_hour` in the station's own
    timezone, quoted at `bid` gross."""
    if bid is None:
        bid = ENTRY - (LOOSE_D + TIGHT_D) / 2  # between the two thresholds
    fee = 0.0117
    reason = (f"stop_loss (live, pnl=-18.6% net; gross {bid:.4f} - exit fee "
              f"{fee:.4f}/share = net {bid - fee:.4f})") if note else "stop_loss"
    return Position(
        position_id=f"{icao}:x:{bucket_c}:{side}",
        station_icao=icao, target_date=date(2026, 8, 17), bucket_c=bucket_c,
        side=side, entry_price=ENTRY, size_usd=size_usd,
        entry_time="2026-08-16T21:00:00+00:00", status=status,
        exit_price=bid - (fee if note else 0.0),
        exit_time=f"2026-08-17T{_utc_hour_for_local(icao, local_hour):02d}:01:00+00:00",
        exit_reason=reason, is_paper=True,
    )


# --- classification ---------------------------------------------------------

def test_stop_between_the_thresholds_after_the_hour_is_tightening_only():
    assert sla.classify(_stop(local_hour=TIGHTEN_HOUR + 1)) == sla.TIGHTENING_ONLY


def test_same_distance_before_the_hour_is_not_attributed_to_the_tightening():
    # Identical price, one hour earlier: the loose threshold was active, so
    # this stop cannot be blamed on (or credited to) the tightening.
    assert sla.classify(_stop(local_hour=TIGHTEN_HOUR - 1)) == sla.BEFORE_TIGHTEN


def test_stop_that_reaches_the_loose_distance_would_have_fired_anyway():
    deep = _stop(local_hour=TIGHTEN_HOUR + 1, bid=ENTRY - LOOSE_D * 1.5)
    assert sla.classify(deep) == sla.WOULD_FIRE_ANYWAY


def test_exactly_at_the_loose_distance_counts_as_would_fire_anyway():
    # evaluate_exit uses >=, so the boundary belongs to the loose rule.
    edge = _stop(local_hour=TIGHTEN_HOUR + 1, bid=ENTRY - LOOSE_D)
    assert sla.classify(edge) == sla.WOULD_FIRE_ANYWAY


def test_non_stop_exits_are_not_scored():
    for status in ("closed_take_profit", "closed_resolution", "closed_trailing_stop", "open"):
        assert sla.classify(_stop(status=status)) is None


# --- the two things most easily got wrong -----------------------------------

def test_gross_price_comes_from_the_fee_note_not_the_stored_net_price():
    p = _stop()
    assert sla.gross_exit_price(p) > p.exit_price
    assert abs(sla.gross_exit_price(p) - (ENTRY - (LOOSE_D + TIGHT_D) / 2)) < 1e-9


def test_pre_fee_model_rows_fall_back_to_the_stored_price():
    p = _stop(note=False)
    assert sla.gross_exit_price(p) == p.exit_price


def test_measuring_the_distance_on_the_net_price_would_misclassify():
    """The reason gross matters: the fee alone can push a tightening-only stop
    across the loose line and hide it in the wrong bucket."""
    p = _stop(local_hour=TIGHTEN_HOUR + 1, bid=ENTRY - LOOSE_D + 0.005)
    assert sla.classify(p) == sla.TIGHTENING_ONLY
    assert (ENTRY - p.exit_price) > LOOSE_D  # net price says otherwise -- and is wrong


def test_local_hour_uses_the_stations_own_timezone():
    # 02:01 UTC is 10:01 in Singapore (UTC+8) but 11:01 in Tokyo (UTC+9).
    sg = _stop(icao="WSSS", local_hour=10)
    tk = _stop(icao="RJTT", local_hour=10)
    assert sla.exit_local_hour(sg) == 10 and sla.exit_local_hour(tk) == 10
    assert sg.exit_time != tk.exit_time  # same local hour, different instants


# --- the counterfactual -----------------------------------------------------

def _settled(icao="WSSS", temp=32.0):
    return {(icao, "2026-08-17"): temp}


def test_no_side_wins_when_the_settled_bucket_is_a_different_one():
    p = _stop(side="NO", bucket_c=32, size_usd=2.20)
    pnl, won, _ = sla.hold_to_settlement(p, _settled(temp=33.0))
    assert won is True
    # 5 shares paying 1.0 against a $2.20 stake
    assert abs(pnl - (2.20 / ENTRY - 2.20)) < 1e-9


def test_no_side_loses_the_whole_stake_when_its_bucket_settles():
    p = _stop(side="NO", bucket_c=32)
    pnl, won, _ = sla.hold_to_settlement(p, _settled(temp=32.0))
    assert won is False and abs(pnl + p.size_usd) < 1e-9


def test_yes_side_is_the_mirror_of_no():
    p = _stop(side="YES", bucket_c=32)
    assert sla.hold_to_settlement(p, _settled(temp=32.0))[1] is True
    assert sla.hold_to_settlement(p, _settled(temp=33.0))[1] is False


def test_missing_settlement_is_unknown_never_a_loss():
    pnl, won, _ = sla.hold_to_settlement(_stop(), {})
    assert pnl is None and won is None


def test_cost_excludes_stops_with_no_settlement_from_BOTH_sides():
    """An unsettled stop (today's, typically) must not have its realized loss
    charged to the stop rule while contributing no held counterpart -- that is
    what inflated the per-station cost columns before _score tracked a matched
    realized figure separately."""
    settled_one = _stop(local_hour=TIGHTEN_HOUR + 1, bucket_c=32, side="NO")
    unsettled = _stop(local_hour=TIGHTEN_HOUR + 1, bucket_c=31, side="NO")
    unsettled.target_date = date(2026, 8, 18)  # no reading for this date
    s = sla._score([settled_one, unsettled], _settled(temp=33.0))

    assert s["unknown"] == 1 and s["known"] == 1
    # realized covers both; the cost line covers only the settled one.
    assert abs(s["realized"] - 2 * realized_of(settled_one)) < 1e-9
    assert abs(s["matched_realized"] - realized_of(settled_one)) < 1e-9
    assert abs(s["cost"] - (s["held"] - realized_of(settled_one))) < 1e-9


def realized_of(position):
    return sla.realized_pnl(position)


def test_reading_outside_the_configured_window_is_flagged_as_clamped():
    station = config.get_station("WSSS")
    _pnl, _won, clamped = sla.hold_to_settlement(
        _stop(), _settled(temp=station.bucket_max_c + 5.0))
    assert clamped is True
    assert sla.hold_to_settlement(_stop(), _settled(temp=32.0))[2] is False
