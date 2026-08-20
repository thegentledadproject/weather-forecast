"""
tests/test_bucket_bias_audit.py

The bucket-derived bias estimator (added 2026-08-20).

Background: entry_manager's bias gate needs (forecast - settled truth) in
degrees C, and for VHHH that truth is HKO's CLMMAXT extract, which
publishes a month at a time weeks late. Its observations stop at
2026-07-31 while its forecasts start 2026-08-06, so the two never overlap
and forecast_error_samples() returns nothing. bucket_bias_audit measures
the same quantity against the bucket the MARKET settled into instead.

These pin the three things that decide whether the number is honest:
which price patterns count as a settlement at all, the interval
convention the midpoint is taken under, and the censored edge buckets
being dropped rather than assigned a fabricated midpoint. No test touches
the network -- every one injects its own price function.
"""

import sqlite3
from datetime import date

import pytest

import bucket_bias_audit as bba
import calibration
import config


VHHH_BOUNDS = (27, 37)


def _prices(conn_rows):
    """A price_fn over a {bucket: price} dict, keyed by the fake token id."""
    def price_fn(token_id, target_date):
        return conn_rows.get(token_id)
    return price_fn


def _token_db(tokens_by_date, station="VHHH"):
    """
    A REAL in-memory market_tokens table, not a stub cursor. The lookup
    being tested is SQL -- including the lower(side) match that the live
    store needs because discovery writes 'yes'/'no' in lower case -- and a
    hand-rolled fake would pass regardless of what the query says.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE market_tokens (token_id TEXT PRIMARY KEY, station_icao TEXT, "
        "target_date TEXT, bucket_c INTEGER, side TEXT, event_slug TEXT DEFAULT '', "
        "discovered_at TEXT DEFAULT '')"
    )
    for target_date, buckets in tokens_by_date.items():
        for bucket_c, token_id in buckets.items():
            for side in ("yes", "no"):
                conn.execute(
                    "INSERT INTO market_tokens (token_id, station_icao, target_date, "
                    "bucket_c, side) VALUES (?,?,?,?,?)",
                    (token_id if side == "yes" else f"{token_id}-no",
                     station, target_date, bucket_c, side),
                )
    return conn


def _event(target_date, buckets=range(27, 38)):
    return _token_db({target_date: {b: f"tok-{b}" for b in buckets}})


# --- what counts as a settlement -------------------------------------------

def test_one_winner_and_all_losers_settles():
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": (0.999 if b == 31 else 0.001) for b in range(27, 38)}
    bucket, bounds = bba.settled_bucket("VHHH", date(2026, 8, 15), conn, _prices(prices))
    assert bucket == 31
    assert bounds == VHHH_BOUNDS


def test_two_winners_is_unresolved():
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": (0.99 if b in (31, 32) else 0.001) for b in range(27, 38)}
    with pytest.raises(bba.UnresolvedDate):
        bba.settled_bucket("VHHH", date(2026, 8, 15), conn, _prices(prices))


def test_an_unsettled_favourite_is_not_a_settlement():
    # THE CASE THIS GUARD IS FOR: the day before resolution, one bucket can
    # legitimately trade at 0.93 while its neighbours hold real value. That
    # is a market with an opinion, not a market with an answer.
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": 0.02 for b in range(27, 38)}
    prices["tok-31"] = 0.93
    prices["tok-32"] = 0.30
    with pytest.raises(bba.UnresolvedDate):
        bba.settled_bucket("VHHH", date(2026, 8, 15), conn, _prices(prices))


def test_missing_history_is_unresolved_not_a_loss():
    # A token the API has no history for must not be counted as 0.0 --
    # that would make silence look like a settled loser.
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": (0.999 if b == 31 else 0.001) for b in range(27, 38)}
    del prices["tok-29"]
    with pytest.raises(bba.UnresolvedDate):
        bba.settled_bucket("VHHH", date(2026, 8, 15), conn, _prices(prices))


def test_undiscovered_date_is_unresolved():
    with pytest.raises(bba.UnresolvedDate):
        bba.settled_bucket("VHHH", date(2026, 8, 15), _token_db({}), _prices({}))


# --- the midpoint convention ------------------------------------------------

def test_floor_mode_midpoint_is_half_a_degree_up():
    # VHHH: bucket 31 covers [31.0, 32.0), so 31.5 -- matching
    # backtest.resolution.bucket_for_temp's floor semantics.
    assert bba.bucket_midpoint_c(31, VHHH_BOUNDS, "floor") == 31.5


def test_half_up_mode_midpoint_is_the_bucket_itself():
    # Everywhere else: bucket 31 covers [30.5, 31.5), so 31.0.
    assert bba.bucket_midpoint_c(31, VHHH_BOUNDS, "half_up") == 31.0


def test_edge_buckets_are_censored_in_both_modes():
    for mode in ("floor", "half_up"):
        assert bba.bucket_midpoint_c(27, VHHH_BOUNDS, mode) is None
        assert bba.bucket_midpoint_c(37, VHHH_BOUNDS, mode) is None


def test_bounds_come_from_the_event_not_config():
    # Same bucket, different discovered window: 31 is interior in 27..37
    # and censored in 31..41. config.STATIONS' bounds drift, so the caller
    # passes the event's -- this pins that the function honours them.
    assert bba.bucket_midpoint_c(31, (27, 37), "floor") == 31.5
    assert bba.bucket_midpoint_c(31, (31, 41), "floor") is None


# --- the sample and the statistic ------------------------------------------

def test_error_sign_matches_forecast_error_samples(monkeypatch):
    # storage.forecast_error_samples returns forecast - observed, and
    # calibration.bias_stats reads positive as "forecasts run WARM". A
    # forecast of 30.0 against a settlement of 31.5 is COOL, so negative.
    monkeypatch.setattr(bba.storage, "forecast_means_by_date",
                        lambda icao: {date(2026, 8, 15): 30.0})
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": (0.999 if b == 31 else 0.001) for b in range(27, 38)}
    errors, skipped, detail = bba.bucket_bias_samples("VHHH", conn, _prices(prices))
    assert errors == [pytest.approx(-1.5)]
    assert skipped == []
    assert detail[0][1] == 31


def test_censored_settlement_is_skipped_with_a_reason(monkeypatch):
    monkeypatch.setattr(bba.storage, "forecast_means_by_date",
                        lambda icao: {date(2026, 8, 15): 30.0})
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": (0.999 if b == 37 else 0.001) for b in range(27, 38)}
    errors, skipped, _ = bba.bucket_bias_samples("VHHH", conn, _prices(prices))
    assert errors == []
    assert len(skipped) == 1 and "censored" in skipped[0][1]


def test_a_date_with_no_forecast_is_skipped_not_zeroed(monkeypatch):
    monkeypatch.setattr(bba.storage, "forecast_means_by_date", lambda icao: {})
    conn = _event("2026-08-15")
    prices = {f"tok-{b}": (0.999 if b == 31 else 0.001) for b in range(27, 38)}
    errors, skipped, _ = bba.bucket_bias_samples(
        "VHHH", conn, _prices(prices), dates=[date(2026, 8, 15)])
    assert errors == []
    assert len(skipped) == 1 and "forecast" in skipped[0][1]


def test_statistic_is_calibrations_not_a_local_copy(monkeypatch):
    # bias_stats is documented as shared on purpose so live and backtest
    # cannot compute it differently. This estimator is a third caller and
    # must not become a third implementation.
    monkeypatch.setattr(bba.storage, "forecast_means_by_date",
                        lambda icao: {date(2026, 8, 13): 30.0, date(2026, 8, 15): 32.0})
    conn = _token_db({d: {b: f"{d}-{b}" for b in range(27, 38)}
                      for d in ("2026-08-13", "2026-08-15")})
    prices = {}
    for d in ("2026-08-13", "2026-08-15"):
        for b in range(27, 38):
            prices[f"{d}-{b}"] = 0.999 if b == 31 else 0.001
    errors, _, _ = bba.bucket_bias_samples("VHHH", conn, _prices(prices))
    assert calibration.bias_stats(errors) == calibration.bias_stats([-1.5, 0.5])


# --- the quantization claim -------------------------------------------------

def test_quantization_stderr_is_small_at_a_months_worth():
    # The claim the module's docstring makes: ~0.06 C at n=26, well under
    # the 0.50 C the gate asks for. If this ever stops holding, the whole
    # "close enough" argument for this estimator stops holding with it.
    q = bba.quantization_stderr_c(26)
    assert q == pytest.approx(0.0566, abs=0.001)
    assert q < config.MAX_BIAS_STANDARD_ERROR_C / 5


def test_quantization_stderr_scales_with_bucket_width():
    assert bba.quantization_stderr_c(26, 2.0) == pytest.approx(
        2 * bba.quantization_stderr_c(26, 1.0))
    assert bba.quantization_stderr_c(0) is None
