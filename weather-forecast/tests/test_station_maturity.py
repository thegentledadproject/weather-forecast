"""
config.station_maturity() -- the gate that authorises real money.

It used to be a hand-typed dict, which made it the ONLY unmeasured gate in
the system: every gate asking "is this trade sound?" read data, while the
one asking "may this station risk real money?" read a literal. It is also
the master key -- manual_trigger.py honours it and nothing else.

These tests pin the criteria and, more importantly, the failure direction:
an unmeasurable criterion is a FAILED criterion, never a satisfied one.
"""

from datetime import date, timedelta

import pytest

import config
import storage
from models import ObservedReading, PointForecast, Position


@pytest.fixture(autouse=True)
def measured_db(tmp_path, monkeypatch):
    """
    A real empty database, with the deterministic-maturity pin from conftest
    REMOVED -- these tests are about the criterion computing for real.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "trading.sqlite3"))
    monkeypatch.setattr(config, "_maturity_cache", {})
    monkeypatch.setattr(config, "MATURITY_OVERRIDE", {})
    # The beats-market criterion reads persisted backtest artifacts, which a
    # tmp database has none of. Tests about the OTHER criteria pass it; the
    # ones about this criterion patch it back and exercise it for real.
    monkeypatch.setattr(config, "calibration_vs_market",
                        lambda icao: (True, "stubbed for this test"))
    storage._connect().close()


def seed(icao="WSSS", pairs=10, errors=None, sims=0, start=date(2026, 7, 1)):
    """Give a station enough history to clear (or miss) each criterion."""
    station = config.get_station(icao)
    errors = errors if errors is not None else [0.1, -0.1] * (pairs // 2 + 1)
    for i in range(pairs):
        target = start + timedelta(days=i)
        storage.save_observation(ObservedReading(
            station_icao=icao, target_date=target, max_temp_c=32.0,
            source=station.resolution_grade_source,
        ))
        storage.save_forecast(PointForecast(
            station_icao=icao, source="open_meteo_ecmwf", target_date=target,
            max_temp_c=32.0 + errors[i % len(errors)],
            fetched_at=f"{target.isoformat()}T00:00:00+00:00",
        ))
    for i in range(sims):
        storage.open_position(Position(
            position_id=f"{icao}:sim:{i}", station_icao=icao, target_date=start,
            bucket_c=32, side="YES", entry_price=0.3, size_usd=1.0,
            entry_time=f"2026-07-0{(i % 9) + 1}T00:00:00+00:00", status="open",
            is_paper=True, execution_mode="simulation", size_shares=3.3,
        ))
        storage.close_position(position_id=f"{icao}:sim:{i}", exit_price=0.3,
                               exit_time="2026-07-10T00:00:00+00:00",
                               status="closed_resolution", reason="test")


# --------------------------------------------------------------------------
# The default is exploratory
# --------------------------------------------------------------------------

def test_an_unknown_station_is_never_mature():
    assert config.station_maturity("NOT_A_STATION") == "exploratory"


def test_an_empty_database_makes_nothing_mature():
    """
    The safe direction. A fresh deployment must not inherit real-money
    authorisation from a literal that shipped in the repo.
    """
    for icao in config.STATIONS:
        assert config.station_maturity(icao) == "exploratory"


def test_a_fully_evidenced_station_is_mature():
    seed(pairs=12, sims=3)
    report = config.maturity_report("WSSS")

    assert report["mature"], report["criteria"]
    assert config.station_maturity("WSSS") == "mature"


# --------------------------------------------------------------------------
# Each criterion, failing alone
# --------------------------------------------------------------------------

def test_too_few_observations_blocks():
    seed(pairs=config.MATURITY_MIN_OBSERVATIONS - 1, sims=3)
    assert not config.maturity_report("WSSS")["criteria"]["observations"][0]
    assert config.station_maturity("WSSS") == "exploratory"


def test_too_few_bias_pairs_blocks():
    """
    The maturity bar is HIGHER than the entry bar: five pairs is enough to
    start correcting a bias, not enough to certify a station for real money.
    """
    assert config.MATURITY_MIN_BIAS_PAIRS > config.MIN_BIAS_PAIRS_BEFORE_ENTRY
    seed(pairs=config.MATURITY_MIN_BIAS_PAIRS - 1, sims=3)
    assert not config.maturity_report("WSSS")["criteria"]["bias_pairs"][0]


def test_an_imprecise_bias_blocks():
    seed(pairs=12, errors=[-4.0, 4.0, -3.0, 3.5, -4.5, 4.2], sims=3)
    assert not config.maturity_report("WSSS")["criteria"]["bias_precision"][0]


def test_a_drifting_bias_blocks_even_when_precise():
    """
    THE criterion stderr cannot express. A bias that is tightly pinned but
    MOVING has a correction fitted to the past rather than measured -- and
    the fit looks excellent right up until it stops working.
    """
    drifting = [0.0] * 6 + [3.0] * 6          # tight in each half, 3C apart
    seed(pairs=12, errors=None, sims=3)
    storage._connect().close()
    # re-seed with the drifting series
    for i, err in enumerate(drifting):
        target = date(2026, 7, 1) + timedelta(days=i)
        storage.save_forecast(PointForecast(
            station_icao="WSSS", source="open_meteo_ecmwf", target_date=target,
            max_temp_c=32.0 + err, fetched_at=f"{target.isoformat()}T00:00:00+00:00",
        ))
    config._maturity_cache.clear()
    stable, detail = config.maturity_report("WSSS")["criteria"]["bias_stability"]

    assert not stable, f"a 3C drift was called stable: {detail}"


def test_no_simulated_orders_blocks():
    """
    The graduation criterion simulation was built to supply and never had:
    proof the ORDER path works for this station's markets.
    """
    seed(pairs=12, sims=0)
    passed, detail = config.maturity_report("WSSS")["criteria"]["order_path"]

    assert not passed
    assert "simulated order" in detail
    assert config.station_maturity("WSSS") == "exploratory"


def test_paper_positions_do_not_count_as_order_path_evidence():
    """Paper never touches wallet_client, so it proves nothing about orders."""
    seed(pairs=12, sims=0)
    for i in range(5):
        storage.open_position(Position(
            position_id=f"paper{i}", station_icao="WSSS", target_date=date(2026, 7, 1),
            bucket_c=32, side="YES", entry_price=0.3, size_usd=1.0,
            entry_time="2026-07-01T00:00:00+00:00", status="open",
            is_paper=True, execution_mode="paper",
        ))
        storage.close_position(position_id=f"paper{i}", exit_price=0.3,
                               exit_time="2026-07-10T00:00:00+00:00",
                               status="closed_resolution", reason="t")
    config._maturity_cache.clear()

    assert not config.maturity_report("WSSS")["criteria"]["order_path"][0]


def test_an_unreadable_criterion_fails_rather_than_passes(monkeypatch):
    """"I could not check" must never read as "it passed"."""
    seed(pairs=12, sims=3)
    config._maturity_cache.clear()

    def _broken(*a, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(storage, "count_observations_from_source", _broken)

    report = config.maturity_report("WSSS")
    assert not report["mature"]
    assert "unreadable" in report["criteria"]["observations"][1]


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------

def test_an_override_wins_and_announces_itself(monkeypatch, capsys):
    monkeypatch.setattr(config, "MATURITY_OVERRIDE",
                        {"WMKK": ("mature", "operator decision for a smoke test")})

    assert config.station_maturity("WMKK") == "mature"
    out = capsys.readouterr().out
    assert "MATURITY OVERRIDE" in out
    assert "operator decision for a smoke test" in out


def test_an_override_can_also_demote(monkeypatch):
    seed(pairs=12, sims=3)
    config._maturity_cache.clear()
    assert config.station_maturity("WSSS") == "mature"

    config._maturity_cache.clear()
    monkeypatch.setattr(config, "MATURITY_OVERRIDE",
                        {"WSSS": ("exploratory", "suspended pending investigation")})
    assert config.station_maturity("WSSS") == "exploratory"


# --------------------------------------------------------------------------
# What it gates
# --------------------------------------------------------------------------

def test_real_money_permission_follows_the_measured_answer():
    seed(pairs=12, sims=3)
    config._maturity_cache.clear()
    assert config.live_mode_is_permitted("WSSS", "live") is True

    config._maturity_cache.clear()
    config._maturity_cache["WSSS"] = "exploratory"
    assert config.live_mode_is_permitted("WSSS", "live") is False, (
        "a station that fails the measured criteria must not be live-eligible"
    )


def test_the_allowlist_is_still_required_as_well():
    """Measured maturity is necessary, not sufficient -- the AND still holds."""
    seed(icao="WMKK", pairs=12, sims=3)
    config._maturity_cache.clear()

    assert config.station_maturity("WMKK") == "mature"
    assert config.live_mode_is_permitted("WMKK", "live") is False, (
        "WMKK is not in LIVE_TRADING_STATIONS and must not be live-eligible "
        "however good its evidence"
    )


# --------------------------------------------------------------------------
# Does the model beat the MARKET?
# --------------------------------------------------------------------------

def _write_run(tmp_path, monkeypatch, station="WSSS", model=0.10, market=0.20,
               n_entries=30, end="2026-08-09", sha=None, generated="2026-08-11T00:00:00+00:00"):
    """A persisted backtest artifact set, the way report.write_artifacts lays it out."""
    import json

    from backtest import settings as bt_settings

    base = tmp_path / "backtests"
    run_dir = base / f"bt_{station}_{n_entries}_{model}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": run_dir.name, "station_icao": station,
        "start_date": "2026-07-28", "end_date": end,
        "generated_at": generated,
        "counters": {"n_entries": n_entries},
        "extras": {"brier_model": model, "brier_market": market},
    }), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({
        "git_sha": sha if sha is not None else (config._current_git_sha() or "deadbeef"),
    }), encoding="utf-8")
    monkeypatch.setattr(bt_settings, "BACKTESTS_OUT_DIR", str(base))
    return run_dir


@pytest.fixture
def real_market_criterion(monkeypatch):
    """Undo the autouse stub so the criterion computes for real."""
    monkeypatch.setattr(config, "calibration_vs_market",
                        config.__dict__["calibration_vs_market"].__wrapped__
                        if hasattr(config.calibration_vs_market, "__wrapped__")
                        else _real_calibration_vs_market)


import config as _cfg_module
_real_calibration_vs_market = _cfg_module.calibration_vs_market


def test_a_model_that_beats_the_market_passes(tmp_path, monkeypatch, real_market_criterion):
    _write_run(tmp_path, monkeypatch, model=0.10, market=0.20, n_entries=30)
    passed, detail = config.calibration_vs_market("WSSS")

    assert passed
    assert "beats market" in detail


def test_a_model_that_loses_to_the_market_fails(tmp_path, monkeypatch, real_market_criterion):
    """
    The real WSSS result under current code on 2026-08-11: model 0.2046,
    market 0.1318. Beating a uniform guess is not the test -- the market is
    the counterparty, and if its prices are better calibrated then every
    "edge" the EV table reports is the model being wrong in a direction the
    book already knows about.
    """
    _write_run(tmp_path, monkeypatch, model=0.2046, market=0.1318, n_entries=30)
    passed, detail = config.calibration_vs_market("WSSS")

    assert not passed
    assert "LOSES to market" in detail


def test_too_few_scored_entries_fails(tmp_path, monkeypatch, real_market_criterion):
    """A Brier difference over five entries is noise, whichever way it points."""
    _write_run(tmp_path, monkeypatch, model=0.10, market=0.20,
               n_entries=config.MATURITY_MIN_BRIER_ENTRIES - 1)
    passed, detail = config.calibration_vs_market("WSSS")

    assert not passed
    assert "need" in detail


def test_a_run_from_superseded_code_fails(tmp_path, monkeypatch, real_market_criterion):
    """
    The code is what produced the number. This session alone changed the
    blend weight, the spread estimator and entry pricing -- each moves
    brier_model, and every run on the box predated them.
    """
    _write_run(tmp_path, monkeypatch, model=0.10, market=0.20, n_entries=30, sha="0" * 40)
    passed, detail = config.calibration_vs_market("WSSS")

    assert not passed
    assert "code that made this number has changed" in detail


def test_a_stale_window_fails(tmp_path, monkeypatch, real_market_criterion):
    """A model certified on July's weather is not certified on today's."""
    _write_run(tmp_path, monkeypatch, model=0.10, market=0.20, n_entries=30, end="2020-01-01")
    passed, detail = config.calibration_vs_market("WSSS")

    assert not passed
    assert "stale" in detail


def test_no_backtest_at_all_fails_with_the_command_to_fix_it(tmp_path, monkeypatch,
                                                             real_market_criterion):
    from backtest import settings as bt_settings

    monkeypatch.setattr(bt_settings, "BACKTESTS_OUT_DIR", str(tmp_path / "empty"))
    passed, detail = config.calibration_vs_market("WSSS")

    assert not passed
    assert "backtest.cli run" in detail


def test_another_stations_run_does_not_certify_this_one(tmp_path, monkeypatch,
                                                        real_market_criterion):
    _write_run(tmp_path, monkeypatch, station="WMKK", model=0.10, market=0.20, n_entries=30)
    passed, _ = config.calibration_vs_market("WSSS")

    assert not passed


# --------------------------------------------------------------------------
# Simulation must not be gated on maturity
# --------------------------------------------------------------------------

def test_a_demoted_station_may_still_simulate():
    """
    THE TRAP this avoids: maturity needs order_path evidence, order_path
    evidence comes from resolved SIMULATED orders. Gating simulation behind
    maturity would mean a demoted station is barred from the only activity
    that could restore it.
    """
    config._maturity_cache["WSSS"] = "exploratory"

    assert config.live_mode_is_permitted("WSSS", "simulation") is True
    assert config.live_mode_is_permitted("WSSS", "live") is False


def test_simulation_still_requires_the_allowlist():
    """Zero-risk is not the same as ungated."""
    assert config.live_mode_is_permitted("WMKK", "simulation") is False
