"""
tests/test_no_lookahead.py

The engine must not be able to see past the end of its own window.

The test is a poisoning experiment rather than an inspection of the code:
build the scenario twice, once clean and once with EVERY observation and
forecast that sits outside the simulated window shifted by +10C, then
assert the two runs are identical. +10C is far outside anything the
calibration would produce from the in-window data, so any leak at all --
one forecast read one tick too early, one future observation used to
settle a position -- moves a central estimate, which moves an EV row,
which moves a fill. There is no way for the poison to be read and the
result to survive unchanged.

Poisoned in the fixture (see tests/conftest.py):
  * observations for target dates AFTER end_date
  * a forecast targeting an IN-WINDOW day but fetched_at AFTER the window
    closed -- the direct test of engine._entry_pass's fetched_at filter,
    since that row would otherwise be a perfectly eligible input

The manifest is excluded from the comparison because the two variants
live in different tmp directories and so record different db paths. That
is environment, not experiment; every field that is a function of the
data is compared.
"""

from conftest import canonical_run


def test_future_poisoning_does_not_change_the_result(scenario_builder, quiet_run):
    clean = scenario_builder(poison_future=False, name="clean")
    run_clean = quiet_run(clean)

    poisoned = scenario_builder(poison_future=True, name="poisoned")
    run_poisoned = quiet_run(poisoned)

    # Guard against a vacuous pass: if the scenario never traded, identical
    # empty results would prove nothing.
    assert run_clean.closed_positions, "scenario produced no closed positions -- test is vacuous"
    assert run_clean.entry_records, "scenario produced no entries -- test is vacuous"

    assert canonical_run(run_clean) == canonical_run(run_poisoned)


def test_no_entry_precedes_the_first_price_snapshot(synthetic_scenario, quiet_run):
    """
    Nothing may be opened at a tick with no quote at or before it. The
    fixture starts its price series at 05:30 local while the schedule's
    first trading tick is at 05:00, so there are genuine entry cycles with
    no price -- and they must produce no fills.
    """
    run = quiet_run(synthetic_scenario)
    first_ts = synthetic_scenario.first_snapshot_ts

    assert run.entry_records, "scenario produced no entries -- test is vacuous"

    for position_id, record in run.entry_records.items():
        assert record["entry_ts"] >= first_ts, (
            f"{position_id} entered at ts={record['entry_ts']}, before the first "
            f"price snapshot at ts={first_ts}"
        )

    for position in list(run.closed_positions) + list(run.unresolved_positions):
        record = run.entry_records.get(position.position_id)
        assert record is not None, f"{position.position_id} has no entry record"
        assert record["entry_ts"] >= first_ts

    # And the pre-snapshot entry cycles must exist, or the assertion above
    # never had anything to catch.
    pre_snapshot_cycles = [d for d in run.decisions_log if d["ts"] < first_ts]
    assert pre_snapshot_cycles, "expected entry cycles before the first snapshot"
    assert all(d["n_opened"] == 0 for d in pre_snapshot_cycles)


def test_recorded_entry_price_matches_the_stored_quote(synthetic_scenario, quiet_run):
    """
    Every fill's recorded market_price must equal the quote price_store
    returns for that token at or before the entry instant -- i.e. the fill
    was priced off a real, already-published snapshot, not a nearby one.
    """
    from backtest import price_store

    run = quiet_run(synthetic_scenario)

    for position_id, record in run.entry_records.items():
        snapshot = price_store.get_price_at(
            record["token_id"],
            record["entry_ts"],
            db_path=synthetic_scenario.market_db_path,
        )
        assert snapshot is not None, f"{position_id} filled with no snapshot at or before entry"
        assert abs(snapshot["price"] - record["market_price"]) < 1e-12


def test_in_window_late_forecast_does_not_leak_backwards(synthetic_scenario, quiet_run):
    """
    The fixture publishes an absurd 40C forecast for D1 at 06:30 local. It
    is legitimately visible to ticks from 06:30 onward, and must be
    invisible to every earlier tick.
    """
    run = quiet_run(synthetic_scenario)
    tripwire_ts = synthetic_scenario.tripwire_ts
    day_one = synthetic_scenario.start_date.isoformat()

    before = {
        d["central_estimate_c"]
        for d in run.decisions_log
        if d["local_date"] == day_one and d["ts"] < tripwire_ts
    }
    after = {
        d["central_estimate_c"]
        for d in run.decisions_log
        if d["local_date"] == day_one and d["ts"] >= tripwire_ts
    }

    assert before, "expected entry cycles before the tripwire"
    assert len(before) == 1, f"pre-tripwire estimates should be constant, got {before}"
    assert max(before) < 33.0, f"pre-tripwire estimate leaked the 40C forecast: {before}"
    assert after and max(after) > max(before), (
        f"tripwire never took effect (before={before}, after={after}) -- "
        "the test would pass vacuously"
    )
