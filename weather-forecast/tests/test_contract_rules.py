"""
Gap 7: each market's own rules text is checked against the station's
expected contract fingerprint before the station-day may produce ENTRIES.

Fixtures are real Gamma events (2026-09-25), trimmed: tests/fixtures/gamma_rules/.
"""
import copy
import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import contract_rules
from contract_rules import INVALID, UNCERTAIN, VALID

FIXTURES = Path(__file__).parent / "fixtures" / "gamma_rules"
FAMILIES = ["WSSS", "EGLC", "KLGA", "RCSS", "VHHH"]
D = date(2026, 9, 25)


def _event(icao):
    return json.loads((FIXTURES / f"{icao}_2026-09-25.json").read_text(encoding="utf-8"))


def _mutate(event, old, new):
    """Replace text in EVERY rules-bearing field, the way a real re-point would."""
    e = copy.deepcopy(event)
    for obj in [e] + e["markets"]:
        for k in ("description", "resolutionSource"):
            if obj.get(k):
                obj[k] = obj[k].replace(old, new)
    return e


@pytest.fixture(autouse=True)
def _fresh_alerts(monkeypatch):
    monkeypatch.setattr(contract_rules, "_alerted", set())


# ---------------------------------------------------------------- pure check

@pytest.mark.parametrize("icao", FAMILIES)
def test_golden_fixture_is_valid(icao):
    assert contract_rules.check_contract(config.STATIONS[icao], _event(icao)) == (VALID, [])


@pytest.mark.parametrize("icao,old,new", [
    ("WSSS", "site=wsss", "site=wmkk"),
    ("KLGA", "site=klga", "site=kjfk"),
    ("RCSS", "tw/taipei/RCSS", "tw/taipei/RCTP"),
    ("VHHH", "recorded by the Hong Kong Observatory", "recorded at Hong Kong International Airport"),
])
def test_station_swapped_is_invalid(icao, old, new):
    status, reasons = contract_rules.check_contract(config.STATIONS[icao], _mutate(_event(icao), old, new))
    assert status == INVALID
    assert "UNKNOWN_STATION" in reasons


@pytest.mark.parametrize("icao,old,new", [
    ("WSSS", "https://www.weather.gov/wrh/timeseries?site=wsss",
     "https://www.wunderground.com/history/daily/sg/singapore/WSSS"),
    ("RCSS", "https://www.wunderground.com/history/daily/tw/taipei/RCSS",
     "https://www.weather.gov/wrh/timeseries?site=rcss"),
    ("VHHH", "https://www.weather.gov.hk/en/cis/climat.htm", "https://example.org/hk"),
])
def test_source_domain_changed_is_invalid(icao, old, new):
    status, reasons = contract_rules.check_contract(config.STATIONS[icao], _mutate(_event(icao), old, new))
    assert status == INVALID
    assert "UNKNOWN_SOURCE" in reasons


@pytest.mark.parametrize("icao,old,new", [
    ("KLGA", "Fahrenheit", "Celsius"),
    ("WSSS", "Celsius", "Fahrenheit"),
])
def test_unit_flip_is_invalid(icao, old, new):
    status, reasons = contract_rules.check_contract(config.STATIONS[icao], _mutate(_event(icao), old, new))
    assert status == INVALID
    assert "UNIT_MISMATCH" in reasons


def test_precision_change_is_invalid():
    e = _mutate(_event("WSSS"), "to whole degrees Celsius (eg, 9°C)", "in Celsius to one decimal place (eg, 9.1°C)")
    status, reasons = contract_rules.check_contract(config.STATIONS["WSSS"], e)
    assert status == INVALID
    assert "PRECISION_MISMATCH" in reasons


def test_one_bucket_repointed_is_caught():
    """Each bucket market resolves on its OWN description, not the event's."""
    e = _event("WSSS")
    e["markets"][1]["description"] = e["markets"][1]["description"].replace("site=wsss", "site=wmkk")
    status, reasons = contract_rules.check_contract(config.STATIONS["WSSS"], e)
    assert status == INVALID and "UNKNOWN_STATION" in reasons


@pytest.mark.parametrize("event", [None, {}, {"description": "", "markets": [{"description": None}]}])
def test_rules_missing_is_uncertain(event):
    status, reasons = contract_rules.check_contract(config.STATIONS["WSSS"], event)
    assert status == UNCERTAIN
    assert "RULES_MISSING" in reasons


def test_every_station_has_a_consistent_fingerprint():
    for icao, st in config.STATIONS.items():
        assert st.contract_source in contract_rules.SOURCES, icao
        # Hong Kong settles on the Observatory's 0.1C extract; nothing else may claim it.
        assert (st.contract_source == "hko") == (st.resolution_grade_source == "hko_daily_max"), icao
        assert (st.contract_source == "hko") == (st.bucket_edge_mode == "floor"), icao


# --------------------------------------------------------------- hashing

def test_hash_ignores_the_date_but_nothing_else():
    e = _event("KLGA")
    other_day = _mutate(e, "25 Sep '26", "26 Sep '26")
    assert contract_rules.rules_hash(e) == contract_rules.rules_hash(other_day)
    reworded = _mutate(e, "resolve to the lowest bracket", "resolve 50-50")
    assert contract_rules.rules_hash(e) != contract_rules.rules_hash(reworded)


# ------------------------------------------------ evaluate: history + alerts

def _rows(db):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT station_icao, target_date, status, reasons, rules_hash FROM contract_checks ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def test_evaluate_persists_only_on_change(tmp_db):
    st = config.STATIONS["WSSS"]
    for _ in range(3):
        assert contract_rules.evaluate(st, D, _event("WSSS")) == (VALID, [])
    rows = _rows(tmp_db)
    assert len(rows) == 1
    assert rows[0][:4] == ("WSSS", "2026-09-25", VALID, "")


def test_wording_change_blocks_one_station_day_then_heals(tmp_db, monkeypatch):
    sent = []
    monkeypatch.setattr(contract_rules.alerts, "send", lambda *a, **kw: sent.append(a) or True)
    st = config.STATIONS["WSSS"]
    e = _event("WSSS")
    assert contract_rules.evaluate(st, date(2026, 9, 24), _mutate(e, "25 Sep", "24 Sep"))[0] == VALID

    changed = _mutate(e, "resolve to the lowest bracket", "resolve 50-50")
    status, reasons = contract_rules.evaluate(st, D, changed)
    assert (status, reasons) == (UNCERTAIN, ["RULES_CHANGED"])
    # Every cycle that day agrees -- the reference is the PREVIOUS day, not the last row.
    assert contract_rules.evaluate(st, D, changed)[0] == UNCERTAIN
    # The next day compares against the new text and is clean again.
    assert contract_rules.evaluate(st, date(2026, 9, 26), _mutate(changed, "25 Sep", "26 Sep"))[0] == VALID
    assert len(sent) == 1  # one alert per station per UTC day


def test_mid_event_edit_is_rules_changed(tmp_db):
    st = config.STATIONS["WSSS"]
    e = _event("WSSS")
    assert contract_rules.evaluate(st, D, e)[0] == VALID
    status, reasons = contract_rules.evaluate(st, D, _mutate(e, "lowest bracket", "highest bracket"))
    assert status == UNCERTAIN and "RULES_CHANGED" in reasons


def test_fetch_error_is_uncertain_and_alerts_once(tmp_db, monkeypatch):
    sent = []
    monkeypatch.setattr(contract_rules.alerts, "send", lambda *a, **kw: sent.append(a) or True)
    for _ in range(3):
        assert contract_rules.evaluate(config.STATIONS["WSSS"], D, None) == (UNCERTAIN, ["RULES_MISSING"])
    contract_rules.evaluate(config.STATIONS["WMKK"], D, None)
    assert [a[0].split()[-1] for a in sent] == ["WSSS", "WMKK"]


def test_evaluate_survives_a_read_only_or_broken_store(monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(contract_rules.storage, "record_contract_check", boom)
    monkeypatch.setattr(contract_rules.storage, "contract_reference_hashes", boom)
    # The fingerprint still decides; history is best-effort.
    assert contract_rules.evaluate(config.STATIONS["WSSS"], D, _event("WSSS")) == (VALID, [])


# ------------------------------------------------------------ ev_engine seam

def test_ev_engine_carries_the_contract_status(monkeypatch, tmp_db):
    import ev_engine
    import market_discovery

    st = config.STATIONS["WSSS"]
    token_map = {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"}
                 for b in range(st.bucket_min_c, st.bucket_max_c + 1)}
    bad = _mutate(_event("WSSS"), "site=wsss", "site=wmkk")

    def fake_discover(station, target_date, *a, **kw):
        market_discovery._remember(market_discovery.build_event_slug(station, target_date), bad)
        return token_map

    monkeypatch.setattr(market_discovery, "discover_token_map", fake_discover)
    monkeypatch.setattr(ev_engine, "fetch_market_quotes", lambda tm: {})
    monkeypatch.setattr(ev_engine, "_capture_snapshots", lambda *a: None)
    monkeypatch.setattr(ev_engine, "compute_ev_table", lambda *a, **kw: [])
    monkeypatch.setattr(ev_engine, "_calibration_for", lambda *a: None)
    est = SimpleNamespace(station_icao="WSSS", target_date=D, central_estimate_c=32.0,
                          std_dev_c=1.0, monsoon_phase="", inputs_used=[])
    monkeypatch.setattr(ev_engine, "bucket_probabilities", lambda *a, **kw: [])
    run = ev_engine.run_for_station_with_map(est)
    assert run.contract_status == INVALID
    assert "UNKNOWN_STATION" in run.contract_reasons
    assert run.veto_reason == ""  # prices still captured: collection is not blocked


def test_station_ev_run_defaults_to_unchecked_not_valid():
    import ev_engine

    assert ev_engine.StationEVRun(station_icao="WSSS", target_date=D).contract_status != VALID


# ------------------------------------------------------------ scheduler wiring

from test_wave3_exit_check_first import (  # noqa: E402  -- reuse the stubbed cycle
    STATION as CYCLE_STATION, TARGET as CYCLE_TARGET, _ev as _cycle_ev, cycle,  # noqa: F401
)


def _run_with_contract(monkeypatch, cycle, status, reasons=()):
    import ev_engine
    import scheduler

    ev_run = ev_engine.StationEVRun(
        station_icao=CYCLE_STATION, target_date=CYCLE_TARGET,
        token_map={b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in (31, 32, 33)},
        bucket_min_c=31, bucket_max_c=33, ev_results=[_cycle_ev(32)],
        contract_status=status, contract_reasons=list(reasons),
    )
    monkeypatch.setattr(ev_engine, "run_for_station_with_map", lambda estimate, **kw: ev_run)
    monkeypatch.setattr(scheduler, "_run_exit_check",
                        lambda station_icao, interval_min=None: cycle["order"].append("exit"))
    scheduler._run_full_cycle(CYCLE_STATION, min_net_ev=0.15)


def test_valid_contract_enters(cycle, monkeypatch):
    _run_with_contract(monkeypatch, cycle, VALID)
    assert cycle["order"] == ["exit", "record", "open"]


@pytest.mark.parametrize("status,reasons", [
    (INVALID, ["UNKNOWN_STATION"]), (UNCERTAIN, ["RULES_CHANGED"]), ("UNCHECKED", []),
])
def test_non_valid_contract_refuses_entries_but_exits_still_run(cycle, monkeypatch, status, reasons):
    _run_with_contract(monkeypatch, cycle, status, reasons)
    assert cycle["order"] == ["exit"]          # no record, no open -- the exit check still ran


def test_non_valid_contract_refuses_entries_with_exits_after(cycle, monkeypatch):
    monkeypatch.setattr(config, "EXIT_CHECK_BEFORE_ENTRIES", False)
    _run_with_contract(monkeypatch, cycle, INVALID, ["UNKNOWN_SOURCE"])
    assert cycle["order"] == ["exit"]


def test_live_station_shadow_pass_is_refused_too(cycle, monkeypatch):
    import executor
    import scheduler

    monkeypatch.setattr(executor, "EXECUTION_MODE", {**executor.EXECUTION_MODE, CYCLE_STATION: "live"})
    shadow = []
    monkeypatch.setattr(scheduler, "_run_shadow_pass", lambda *a, **kw: shadow.append(a))
    _run_with_contract(monkeypatch, cycle, INVALID, ["UNKNOWN_STATION"])
    assert shadow == [] and cycle["order"] == ["exit"]


def test_manual_trigger_refuses_a_non_valid_contract(monkeypatch, capsys, tmp_db):
    import entry_manager
    import manual_trigger
    import market_discovery
    from test_manual_trigger import _Args

    bad = _mutate(_event("WSSS"), "site=wsss", "site=wmkk")

    def fake_discover(station, target_date, *a, **kw):
        market_discovery._remember(market_discovery.build_event_slug(station, target_date), bad)
        return {33: {"yes_token_id": "y33", "no_token_id": "n33"}}

    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **kw: 0)
    monkeypatch.setattr(manual_trigger, "_parse_args", lambda: _Args("simulation"))
    monkeypatch.setattr(manual_trigger.market_discovery, "discover_token_map", fake_discover)
    monkeypatch.setattr(manual_trigger.market_client, "get_entry_price_for_side",
                        lambda *a: (_ for _ in ()).throw(AssertionError("priced a re-pointed contract")))
    assert manual_trigger.main() == 1
    assert "UNKNOWN_STATION" in capsys.readouterr().out
