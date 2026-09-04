"""
tests/test_calibrated_sizing.py

P3-6 · size on a calibrated probability, with ONE declared buffer.

THE DEFECT. entry_manager sizes with `f* = raw_edge / (1 - price)` on
`model_prob`, then applies KELLY_FRACTION (0.25) AND gap_risk_haircut(). Two
stacked corrections for a bias that can be measured directly is worse than one
correction fitted to the measurement -- and the model's bias IS measured: mean
`model_prob` 0.432 against a 0.344 realised win rate, about 9 points
overconfident.

WHAT THIS BUILDS. An isotonic map from `model_prob` to realised outcome, fitted
OUT OF SAMPLE per day, carried on EVResult as its own field beside the raw
`model_prob`, and fed to the SIZING path only.

PURE PYTHON, DELIBERATELY. numpy and scipy are not installed in the deployed
venv and no module in this repo imports either. Pool-adjacent-violators is
thirty lines and deterministic; adding a numerical dependency to the daemon for
one function is the worse trade. Reaching for sklearn.isotonic would have broken
the deploy rather than the tests.

THE THREE TIERS mirror calibration.estimate_std_dev's chain deliberately, and
for the same reason: "measured for this station", "measured across the book" and
"not measured" are different claims and the caller has to be able to tell them
apart.

    station_isotonic  this station has enough prior rows of its own
    pooled_isotonic   the book does, this station does not
    uncalibrated      neither -- fall back to the raw model_prob AND KEEP the
                      existing double buffer

THAT LAST CLAUSE IS PREREQUISITE B'S ANSWER. gap_risk_haircut() retires on a
stopless book ONLY where the probability is actually calibrated. Where it is
not, the haircut stays, because there it is still standing in for a bias nothing
has measured. See the plan's section 12.
"""
from datetime import date, timedelta

import pytest

import config
import probability_calibration as pc


def _pairs(n, model_prob, outcome, day=date(2026, 8, 10), station="WSSS"):
    return [
        {"station_icao": station, "target_date": day,
         "model_prob": model_prob, "outcome": outcome}
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Pool-adjacent-violators, on its own
# ---------------------------------------------------------------------------

def test_an_already_monotone_sequence_is_returned_unchanged():
    assert pc._pava([0.0, 0.0, 1.0, 1.0]) == pytest.approx([0.0, 0.0, 1.0, 1.0])


def test_a_violating_pair_is_pooled_to_their_mean():
    """1.0 before 0.0 is a violation; both become 0.5."""
    assert pc._pava([0.0, 1.0, 0.0, 1.0]) == pytest.approx([0.0, 0.5, 0.5, 1.0])


def test_the_output_is_always_non_decreasing():
    out = pc._pava([1.0, 0.0, 1.0, 0.0, 0.0, 1.0])
    assert all(b >= a - 1e-12 for a, b in zip(out, out[1:]))


def test_pooling_preserves_the_mean():
    """PAVA is a projection: it moves values, never their total."""
    values = [1.0, 0.0, 1.0, 0.0, 0.0, 1.0]
    assert sum(pc._pava(values)) == pytest.approx(sum(values))


def test_an_empty_sequence_is_empty():
    assert pc._pava([]) == []


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def test_a_perfectly_calibrated_book_maps_each_probability_to_itself():
    """
    Ten rows at 0.30 of which three win, ten at 0.70 of which seven win. A map
    fitted on that should return roughly what it was given.
    """
    rows = _pairs(3, 0.30, 1.0) + _pairs(7, 0.30, 0.0) \
        + _pairs(7, 0.70, 1.0) + _pairs(3, 0.70, 0.0)
    fitted = pc.fit_map(rows)

    assert pc.apply_map(fitted, 0.30) == pytest.approx(0.30, abs=0.01)
    assert pc.apply_map(fitted, 0.70) == pytest.approx(0.70, abs=0.01)


def test_an_overconfident_book_is_shrunk_toward_the_realised_rate():
    """
    THE MEASURED CASE. The book says 0.432 and wins 0.344 of the time. The map
    has to pull the stated probability DOWN -- that is the whole point, and it
    is what shrinks raw_edge and therefore position size.
    """
    rows = _pairs(34, 0.432, 1.0) + _pairs(66, 0.432, 0.0)
    fitted = pc.fit_map(rows)

    assert pc.apply_map(fitted, 0.432) == pytest.approx(0.34, abs=0.02)
    assert pc.apply_map(fitted, 0.432) < 0.432


def test_the_map_is_monotone_in_the_input():
    rows = _pairs(1, 0.2, 0.0) + _pairs(9, 0.2, 0.0) \
        + _pairs(5, 0.5, 1.0) + _pairs(5, 0.5, 0.0) \
        + _pairs(9, 0.8, 1.0) + _pairs(1, 0.8, 0.0)
    fitted = pc.fit_map(rows)

    values = [pc.apply_map(fitted, p) for p in (0.2, 0.35, 0.5, 0.65, 0.8)]
    assert all(b >= a - 1e-12 for a, b in zip(values, values[1:]))


def test_a_probability_outside_the_fitted_range_is_clamped_not_extrapolated():
    """
    Extrapolating an isotonic fit past its support invents calibration where
    none was measured. Clamping to the end knots is the honest answer.
    """
    rows = _pairs(5, 0.4, 1.0) + _pairs(5, 0.6, 0.0)
    fitted = pc.fit_map(rows)

    assert pc.apply_map(fitted, 0.01) == pytest.approx(pc.apply_map(fitted, 0.4))
    assert pc.apply_map(fitted, 0.99) == pytest.approx(pc.apply_map(fitted, 0.6))


def test_the_map_never_returns_a_value_outside_zero_to_one():
    rows = _pairs(10, 0.5, 1.0)
    fitted = pc.fit_map(rows)
    assert 0.0 <= pc.apply_map(fitted, 0.5) <= 1.0


# ---------------------------------------------------------------------------
# Out of sample, per day -- the acceptance condition
# ---------------------------------------------------------------------------

DAY_N = date(2026, 8, 20)


def _dated(day, n, model_prob, outcome, station="WSSS"):
    return _pairs(n, model_prob, outcome, day=day, station=station)


def test_the_map_for_day_n_uses_no_data_from_day_n():
    """
    THE ACCEPTANCE CONDITION. A map fitted on the whole record and applied
    retrospectively is the same leak estimate_std_dev(allow_measured=False)
    refuses for the backtest.
    """
    earlier = _dated(DAY_N - timedelta(days=1), 40, 0.60, 0.0)
    same_day = _dated(DAY_N, 400, 0.60, 1.0)

    fitted, source, n = pc.fit_for_day(earlier + same_day, DAY_N, "WSSS")

    assert n == 40, "day N's own 400 rows leaked into its map"
    assert pc.apply_map(fitted, 0.60) == pytest.approx(0.0, abs=0.02)


def test_the_map_for_day_n_uses_no_data_from_after_day_n():
    earlier = _dated(DAY_N - timedelta(days=1), 40, 0.60, 0.0)
    later = _dated(DAY_N + timedelta(days=3), 400, 0.60, 1.0)

    _, _, n = pc.fit_for_day(earlier + later, DAY_N, "WSSS")

    assert n == 40


def test_a_station_with_enough_history_gets_its_own_map():
    rows = _dated(DAY_N - timedelta(days=1), config.MIN_CALIBRATION_SAMPLES, 0.5, 0.0)
    _, source, n = pc.fit_for_day(rows, DAY_N, "WSSS")

    assert source == "station_isotonic"
    assert n == config.MIN_CALIBRATION_SAMPLES


def test_a_thin_station_falls_back_to_the_pooled_map():
    """
    Tier two, mirroring estimate_std_dev's measured -> pooled step. The book
    has evidence even where this station does not.
    """
    thin = _dated(DAY_N - timedelta(days=1), 2, 0.5, 0.0, station="RCSS")
    book = _dated(DAY_N - timedelta(days=1), config.MIN_CALIBRATION_SAMPLES, 0.5, 0.0)

    _, source, n = pc.fit_for_day(thin + book, DAY_N, "RCSS")

    assert source == "pooled_isotonic"


def test_a_thin_book_falls_back_to_no_calibration_at_all():
    """
    Tier three. Below this the map is not estimable, and inventing one is
    worse than declaring that there is none.
    """
    rows = _dated(DAY_N - timedelta(days=1), 3, 0.5, 0.0)

    fitted, source, n = pc.fit_for_day(rows, DAY_N, "WSSS")

    assert fitted is None
    assert source == "uncalibrated"


def test_the_very_first_day_is_uncalibrated():
    """No earlier days exist, so nothing can be fitted. Not an error."""
    fitted, source, _ = pc.fit_for_day(_dated(DAY_N, 500, 0.5, 1.0), DAY_N, "WSSS")

    assert fitted is None
    assert source == "uncalibrated"


def test_rows_without_a_stored_model_prob_are_not_fitted_on():
    """
    NULL model_prob is the honest value on rows written before the column and
    on manual_trigger rows. "No model ran" is not "the model said 0".
    """
    good = _dated(DAY_N - timedelta(days=1), config.MIN_CALIBRATION_SAMPLES, 0.5, 0.0)
    null = _dated(DAY_N - timedelta(days=1), 100, None, 1.0)

    _, _, n = pc.fit_for_day(good + null, DAY_N, "WSSS")

    assert n == config.MIN_CALIBRATION_SAMPLES


# ---------------------------------------------------------------------------
# Prerequisite B: which buffer applies
# ---------------------------------------------------------------------------

def test_a_calibrated_stopless_book_retires_the_haircut():
    """
    PREREQUISITE B, ANSWERED. On a book with no stop there is no trigger to gap
    through and no exit spread to pay, so the arithmetically correct haircut is
    1.0. The only reason it shipped False was that the conservatism was
    standing in for the model's overconfidence -- which is now corrected once,
    at the probability.
    """
    assert pc.haircut_applies(has_stop=False, calibration_source="station_isotonic") is False
    assert pc.haircut_applies(has_stop=False, calibration_source="pooled_isotonic") is False


def test_an_uncalibrated_stopless_book_keeps_the_haircut():
    """
    THE CONDITIONAL HALF, and the reason this is not a flag flip. Where the map
    is not estimable there is no measurement of the bias, so the undeclared
    second buffer is still doing the job it was silently doing before.
    """
    assert pc.haircut_applies(has_stop=False, calibration_source="uncalibrated") is True


def test_a_book_with_a_stop_always_keeps_the_haircut():
    """
    Calibration says nothing about gap risk. A stop that can be gapped through
    still costs more than its trigger, whatever the probability was.
    """
    for source in ("station_isotonic", "pooled_isotonic", "uncalibrated"):
        assert pc.haircut_applies(has_stop=True, calibration_source=source) is True


# ---------------------------------------------------------------------------
# The wiring: sizing moves, the gate and the record do not
# ---------------------------------------------------------------------------

import entry_manager
from models import EVResult


def _ev(model_prob=0.432, price=0.30, calibrated=None, source=pc.NO_TIER):
    return EVResult(
        station_icao="WSSS", target_date=DAY_N, bucket_c=32, side="YES",
        model_prob=model_prob, market_price=price,
        raw_edge=model_prob - price, estimated_slippage_pct=0.0,
        fee_rate_pct=0.0, net_ev_per_dollar=(model_prob - price) / price,
        calibrated_prob=calibrated, calibration_source=source,
    )


def test_kelly_sizes_on_the_calibrated_probability_when_there_is_one():
    """
    The whole point. The book says 0.432 at a price of 0.30; the map says the
    truth is 0.344. Kelly must size the 0.044 edge, not the 0.132 one.
    """
    calibrated = entry_manager.compute_kelly_fraction(
        _ev(calibrated=0.344, source=pc.STATION_TIER)
    )
    raw = entry_manager.compute_kelly_fraction(_ev())

    assert calibrated < raw
    assert calibrated == pytest.approx((0.344 - 0.30) / 0.70, abs=1e-9)


def test_kelly_falls_back_to_the_raw_probability_when_uncalibrated():
    uncalibrated = entry_manager.compute_kelly_fraction(
        _ev(calibrated=0.344, source=pc.NO_TIER)
    )

    assert uncalibrated == pytest.approx((0.432 - 0.30) / 0.70, abs=1e-9)


def test_a_calibrated_probability_below_the_price_sizes_to_nothing():
    """
    Shrinking the probability can take the edge negative, and it should: the
    map is saying the book has no business being on this side at this price.
    """
    assert entry_manager.compute_kelly_fraction(
        _ev(model_prob=0.40, price=0.35, calibrated=0.30, source=pc.POOLED_TIER)
    ) <= 0


def test_the_edge_gate_still_reads_the_raw_edge(monkeypatch):
    """
    SIZING ONLY, deliberately. Whether the edge GATE should move onto the
    calibrated probability is a separate decision with a different risk
    profile, and bundling them makes the result unattributable. An entry whose
    raw edge clears the bar must still reach sizing even when calibration
    shrinks it below the bar.
    """
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)

    # raw edge 0.132 clears MIN_ABS_RAW_EDGE; calibrated edge is 0.001.
    decision = entry_manager.evaluate_entry(
        _ev(model_prob=0.432, price=0.30, calibrated=0.301, source=pc.STATION_TIER),
        token_id="tok", min_net_ev=-9.0,
    )

    assert "below required minimum" not in decision.reason


def test_the_stored_model_prob_is_untouched_by_calibration():
    """
    ACCEPTANCE. model_prob is what the EV table, the snapshots and every stored
    row MEAN, and P0-1 scores against it. The calibrated value travels beside
    it and never over it.
    """
    row = _ev(model_prob=0.432, calibrated=0.344, source=pc.STATION_TIER)

    assert row.model_prob == 0.432
    assert row.raw_edge == pytest.approx(0.432 - 0.30)
    assert row.calibrated_prob == 0.344


def test_the_provenance_travels_with_the_number():
    row = _ev(calibrated=0.344, source=pc.POOLED_TIER)
    assert row.calibration_source == pc.POOLED_TIER


def test_an_uncalibrated_row_defaults_to_no_calibrated_probability():
    row = EVResult(
        station_icao="WSSS", target_date=DAY_N, bucket_c=32, side="YES",
        model_prob=0.4, market_price=0.3, raw_edge=0.1,
        estimated_slippage_pct=0.0, fee_rate_pct=0.0, net_ev_per_dollar=0.3,
    )
    assert row.calibrated_prob is None
    assert row.calibration_source == pc.NO_TIER


def test_the_decision_reports_which_tier_sized_it():
    """
    A size that rests on a pooled map and one that rests on this station's own
    history are different claims, and estimate_std_dev reports its tier for the
    same reason.
    """
    import inspect

    src = inspect.getsource(entry_manager.evaluate_entry)
    assert "calibration_source" in src


def test_the_approval_note_names_the_calibration_tier(monkeypatch):
    """
    "Fall back to the raw model_prob with the existing double buffer and SAY
    SO in the note, exactly as estimate_std_dev reports fallback_default."
    A size that rests on a pooled map, on this station's own history, or on no
    map at all are three different claims about the same dollar figure.
    """
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)

    # Edge kept under config.max_plausible_edge_for(price) -- a 30-point edge
    # is vetoed as a presumed data error long before sizing.
    approved = entry_manager.evaluate_entry(
        _ev(model_prob=0.42, price=0.30, calibrated=0.38, source=pc.STATION_TIER),
        token_id="tok", min_net_ev=-9.0,
    )
    assert approved.approved
    assert pc.STATION_TIER in approved.reason

    uncalibrated = entry_manager.evaluate_entry(
        _ev(model_prob=0.42, price=0.30), token_id="tok", min_net_ev=-9.0,
    )
    assert uncalibrated.approved
    assert pc.NO_TIER in uncalibrated.reason


# ---------------------------------------------------------------------------
# Fitting from the real cohort, and the cache
# ---------------------------------------------------------------------------

def test_the_cohort_is_read_through_the_monitor_not_re_derived(monkeypatch):
    """
    ONE definition of "a scorable closed row with an outcome". cohort_monitor
    already assembles it and reproduces the published totals to the cent; a
    second loader here would be a second definition of the record this
    correction is fitted on.
    """
    import inspect

    src = inspect.getsource(pc.calibration_for)
    assert "cohort_monitor" in src


def test_the_map_is_cached_per_station_day(monkeypatch):
    """
    compute_ev_table runs every cycle -- as often as every 10 minutes in the
    primary window -- and the cohort read is two storage queries per station.
    Refitting per cycle would be pure waste for a map that cannot change until
    the next day's rows land.
    """
    calls = {"n": 0}

    def _fake_cohort(**kwargs):
        calls["n"] += 1
        return [], {}

    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", _fake_cohort)
    pc.clear_cache()

    pc.calibration_for("WSSS", DAY_N)
    pc.calibration_for("WSSS", DAY_N)
    pc.calibration_for("WSSS", DAY_N)

    assert calls["n"] == 1


def test_a_different_day_refits(monkeypatch):
    calls = {"n": 0}

    def _fake_cohort(**kwargs):
        calls["n"] += 1
        return [], {}

    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", _fake_cohort)
    pc.clear_cache()

    pc.calibration_for("WSSS", DAY_N)
    pc.calibration_for("WSSS", DAY_N + timedelta(days=1))

    assert calls["n"] == 2


def test_an_unreadable_cohort_falls_back_to_uncalibrated_rather_than_raising(monkeypatch):
    """
    This runs on the entry path. A storage failure must degrade to today's
    sizing behaviour, not take the cycle down -- and "uncalibrated" is exactly
    today's behaviour, double buffer included.
    """
    def _boom(**kwargs):
        raise RuntimeError("storage is gone")

    monkeypatch.setattr(pc.cohort_monitor, "load_cohort", _boom)
    pc.clear_cache()

    fitted, source, n = pc.calibration_for("WSSS", DAY_N)

    assert fitted is None
    assert source == pc.NO_TIER
    assert n == 0


# ---------------------------------------------------------------------------
# ev_engine attaches it
# ---------------------------------------------------------------------------

import ev_engine
from models import CalibratedEstimate, MarketQuote


def _ev_table(calibration=None):
    estimate = CalibratedEstimate(
        station_icao="WSSS", target_date=DAY_N, central_estimate_c=32.0,
        std_dev_c=1.0, monsoon_phase="southwest", spread_source="measured_error",
    )
    token_map = {32: {"yes_token_id": "y", "no_token_id": "n"}}
    quotes = {32: MarketQuote(bucket_c=32, yes_price=0.30, no_price=0.70)}
    return ev_engine.compute_ev_table(
        estimate, token_map, quotes=quotes, calibration=calibration,
    )


def test_the_ev_table_carries_the_calibrated_probability(monkeypatch):
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.0)
    rows = _ev_table(calibration=(pc.fit_map(
        _pairs(34, 0.5, 1.0) + _pairs(66, 0.5, 0.0)
    ), pc.STATION_TIER, 100))

    row = next(r for r in rows if r.side == "YES")
    assert row.calibration_source == pc.STATION_TIER
    assert row.calibrated_prob is not None
    assert row.model_prob != row.calibrated_prob or row.model_prob == pytest.approx(0.34, abs=0.02)


def test_no_calibration_leaves_the_row_uncalibrated(monkeypatch):
    """
    The backtest and any caller that does not fit a map get exactly today's
    behaviour: raw model_prob, double buffer, and a provenance that says so.
    """
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.0)
    row = next(r for r in _ev_table() if r.side == "YES")

    assert row.calibrated_prob is None
    assert row.calibration_source == pc.NO_TIER


def test_the_calibrated_value_is_side_adjusted(monkeypatch):
    """
    model_prob on an EVResult is ALREADY P(this side wins) -- ev_engine stores
    side_model_prob, not the bucket probability. The map is fitted on exactly
    that quantity (cohort_monitor scores the stored side-adjusted value against
    a 0/1 outcome for the same side), so applying it per row is correct and no
    second flip is needed. Pinned because flipping twice is the obvious bug.
    """
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.0)
    fitted = pc.fit_map(_pairs(1, 0.0, 0.0) + _pairs(1, 1.0, 1.0))
    rows = _ev_table(calibration=(fitted, pc.POOLED_TIER, 2))

    yes = next(r for r in rows if r.side == "YES")
    no = next(r for r in rows if r.side == "NO")
    assert yes.calibrated_prob == pytest.approx(pc.apply_map(fitted, yes.model_prob))
    assert no.calibrated_prob == pytest.approx(pc.apply_map(fitted, no.model_prob))
