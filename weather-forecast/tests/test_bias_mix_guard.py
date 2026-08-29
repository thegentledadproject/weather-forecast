"""
tests/test_bias_mix_guard.py

The forecast-source mix guard on the bias correction.

WHAT IT PROTECTS. entry_manager.forecast_bias_stats() returns ONE scalar,
fitted per storage.forecast_error_samples() on "the per-date forecast mean
[that] mirrors blend_central_estimate's own forecast term". That is exactly
right while the source mix is stable -- and measured over the live database
it is: 17 of 19 stations ran a single mix on 100% of their scored days, WSSS
28/29 (97%), WMKK 26/28 (93%).

It is WRONG on a day whose mix differs, because the scalar was fitted across
a different blend. Measured on WSSS, per-source correction moves the central
estimate more than 0.05C on exactly one of 29 days -- 2026-07-31, the single
day with one source present -- and there it moves it by 1.820C, which is two
whole buckets.

So this is a tail-risk guard, not an edge improvement: rare, and large when
it bites. The realistic trigger is a source outage during a trading cycle.

NOT a per-source bias correction. That was investigated on 2026-08-28 and
rejected: the pooled bias is fitted on the blend's own error rather than
averaged from source biases, forecast disagreement no longer feeds the
spread chain (ensemble -> measured_error -> pooled_error -> fallback), and
on a constant mix the two schemes agree to within 0.02C.
"""

import pytest

import config
import entry_manager


FITTED = frozenset({"open_meteo_ecmwf", "open_meteo_gfs", "nea_24hr"})


def _reason(today_mix, fitted_mix=FITTED, enforce=True, obs=999, n=99, stderr=0.1):
    return entry_manager.collection_only_reason(
        "WSSS", obs, bias_n=n, bias_stderr=stderr,
        enforce_bias_quality=enforce,
        bias_source_mix=fitted_mix,
        today_source_mix=today_mix,
    )


def test_the_matching_mix_passes():
    assert _reason(FITTED) is None


def test_a_missing_source_is_refused():
    """
    THE 2026-07-31 CASE. One source instead of three, and the scalar fitted
    on the three-source blend is off by 1.820C there -- two buckets.
    """
    reason = _reason(frozenset({"nea_24hr"}))

    assert reason is not None
    assert "open_meteo_ecmwf" in reason and "open_meteo_gfs" in reason


def test_an_unexpected_extra_source_is_also_refused():
    """
    A mismatch in either direction invalidates the correction: the scalar
    was fitted on a blend that did not contain the extra source, so its
    error says nothing about today's mean.
    """
    assert _reason(FITTED | {"some_new_model"}) is not None


def test_the_reason_names_the_station_and_reads_as_collection_only():
    """Matches the other gate strings so the rejection funnel classifies it."""
    reason = _reason(frozenset({"nea_24hr"}))

    assert reason.startswith("Collection-only:")
    assert "WSSS" in reason


def test_the_guard_is_off_when_bias_quality_is_not_enforced():
    """
    The replay runs enforce_bias_quality=False because it applies NO bias
    correction (engine.py pins forecast_bias_c=0.0). A correction that does
    not exist cannot be mis-specified, so the guard must inherit that
    documented divergence rather than invent a new one.
    """
    assert _reason(frozenset({"nea_24hr"}), enforce=False) is None


def test_unknown_mixes_do_not_block_trading():
    """
    Fails OPEN, unlike the rest of this gate, and deliberately. Every other
    branch here refuses on a broken read because it cannot tell whether the
    station is calibrated. This one is different: not knowing the mix is the
    NORMAL state for every caller that has not been taught to pass it --
    entry_sim, and any operator script -- and failing closed would silently
    stop those paths trading on a check they never opted into.
    """
    assert _reason(None) is None
    assert _reason(FITTED, fitted_mix=None) is None


def test_it_does_not_fire_before_the_earlier_bias_gates():
    """
    Ordering. A station that has not earned a bias at all must report THAT,
    not a mix mismatch -- the mix is meaningless when the correction it
    guards was never trustworthy.
    """
    reason = _reason(frozenset({"nea_24hr"}), n=1, stderr=None)

    assert reason is not None
    assert "mix" not in reason.lower()


def test_the_bias_gate_override_skips_the_mix_guard_too(monkeypatch):
    """
    BIAS_GATE_OVERRIDE_STATIONS exists to let an operator trade a station
    whose bias cannot be measured the normal way (VHHH's HKO extract). That
    override sits above every bias-quality branch, and this guard is one --
    so it must be skipped as well, or the override would half-work in a way
    nobody declared. No new escape hatch is added for the mix check; this is
    the existing one.
    """
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"WMKK"})

    assert entry_manager.collection_only_reason(
        "WMKK", 999, bias_n=99, bias_stderr=0.1, enforce_bias_quality=True,
        bias_source_mix=FITTED, today_source_mix=frozenset({"nea_24hr"}),
    ) is None


def test_the_override_cannot_disarm_the_guard_on_a_real_money_station(monkeypatch):
    """
    config.bias_gate_is_overridden REFUSES to apply to anything in
    LIVE_TRADING_STATIONS -- "trade this station on an unmeasured bias" is a
    sentence that must never be true of money. So the mix guard is
    UNCONDITIONAL wherever real orders are placed, which is exactly where a
    mis-specified correction would cost something.
    """
    monkeypatch.setattr(config, "BIAS_GATE_OVERRIDE_STATIONS", {"WSSS"})
    monkeypatch.setattr(config, "LIVE_TRADING_STATIONS", {"WSSS"})

    assert _reason(frozenset({"nea_24hr"})) is not None
