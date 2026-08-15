"""
tests/test_backtest_stack.py

Two jobs:

  1. IMPORT SMOKE -- every module in the backtest stack imports cleanly.
     Cheap, and it catches the failure mode that makes every other test in
     this directory error out at collection time rather than fail with a
     useful message.

  2. ROUND TRIP -- run the shared synthetic scenario, summarize it, write
     the artifact set, and read every artifact back. The pinned summary
     schema (backtest/report.py) is a contract with the dashboard, so it is
     asserted key by key here rather than eyeballed.
"""

import json
from datetime import date

import pytest


# --------------------------------------------------------------------------
# 1. imports
# --------------------------------------------------------------------------


def test_core_backtest_modules_import():
    import backtest.engine  # noqa: F401
    import backtest.entry_sim  # noqa: F401
    import backtest.fill_model  # noqa: F401
    import backtest.portfolio  # noqa: F401
    import backtest.price_store  # noqa: F401
    import backtest.report  # noqa: F401
    import backtest.resolution  # noqa: F401
    import backtest.settings  # noqa: F401
    import backtest.simclock  # noqa: F401


def test_backtest_engine_module_imports():
    """The pre-existing root-level forecast-accuracy backtest module."""
    import backtest_engine  # noqa: F401


def test_backtest_cli_imports():
    """
    backtest.cli is written by a parallel task. Skip rather than fail while
    it is still landing -- but do NOT delete this test: it is the only thing
    that will notice the CLI failing to import once it exists.
    """
    pytest.importorskip("backtest.cli")


# --------------------------------------------------------------------------
# 2. round trip
# --------------------------------------------------------------------------

PINNED_TOP_LEVEL_KEYS = (
    "run_id",
    "station_icao",
    "start_date",
    "end_date",
    "params",
    "metrics",
    "extras",
    "counters",
    "provenance",
    "generated_at",
)


@pytest.fixture
def round_trip(synthetic_scenario, quiet_run, tmp_path):
    """Run the scenario once, summarize it, and write the artifacts."""
    from backtest import report

    run = quiet_run(synthetic_scenario)
    summary = report.summarize(run)
    out_dir = report.write_artifacts(run, out_dir=tmp_path)
    return run, summary, out_dir


def test_artifact_directory_is_named_for_the_run(round_trip, tmp_path):
    run, _summary, out_dir = round_trip
    assert out_dir == tmp_path / run.run_id
    assert out_dir.is_dir()


def test_summary_json_parses_and_carries_every_pinned_key(round_trip):
    _run, _summary, out_dir = round_trip
    parsed = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    for key in PINNED_TOP_LEVEL_KEYS:
        assert key in parsed, f"pinned key '{key}' missing from summary.json"

    assert set(parsed["params"]) >= {"depth_regime", "fee_rate_pct", "bankroll_mode"}
    assert set(parsed["extras"]) >= {
        "max_drawdown_pct",
        "final_equity_usd",
        "starting_bankroll_usd",
        "brier_model",
        "brier_market",
        "brier_n",
        "n_unresolved",
    }
    assert set(parsed["counters"]) >= {
        "n_cycles",
        "n_entry_cycles",
        "n_entries",
        "n_skipped_no_price",
        "n_skipped_implausible_move",
        "rejections",
    }
    assert set(parsed["provenance"]) >= {
        "n_ticks",
        "pct_live_snapshot",
        "pct_with_depth",
        "median_fidelity_min",
    }
    # Slippage sensitivity is top level, alongside counters/provenance --
    # NOT inside extras. It is a what-if adjustment on top of metrics.
    assert "slippage_sensitivity" in parsed


def test_summary_matches_the_run_it_describes(round_trip):
    run, summary, _out_dir = round_trip

    assert summary["run_id"] == run.run_id
    assert summary["station_icao"] == run.station_icao
    assert summary["start_date"] == run.start_date.isoformat()
    assert summary["end_date"] == run.end_date.isoformat()
    assert summary["extras"]["n_unresolved"] == len(run.unresolved_positions)
    assert summary["extras"]["starting_bankroll_usd"] == run.portfolio.initial_bankroll_usd
    assert summary["extras"]["final_equity_usd"] == run.portfolio.equity_curve[-1][1]
    assert summary["counters"]["n_entries"] == run.counters["n_entries"]

    # The scenario closes positions, so summarize_positions must not be None.
    assert run.closed_positions, "scenario produced no closed positions -- test is vacuous"
    assert summary["metrics"] is not None
    assert summary["metrics"]["n_trades"] == len(run.closed_positions)


def test_summarize_is_deterministic_except_for_generated_at(round_trip):
    from backtest import report

    run, summary, _out_dir = round_trip
    again = report.summarize(run)

    a = dict(summary)
    b = dict(again)
    a.pop("generated_at")
    b.pop("generated_at")
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_brier_scores_are_present_and_side_adjusted(round_trip):
    """
    Every entry in this scenario lands on a day with a recorded observation,
    so both Brier scores must be real numbers in [0, 1]. brier_market is the
    honest baseline -- beating it is the bar, not hitting some absolute
    number.
    """
    _run, summary, _out_dir = round_trip

    brier_model = summary["extras"]["brier_model"]
    brier_market = summary["extras"]["brier_market"]

    assert brier_model is not None and 0.0 <= brier_model <= 1.0
    assert brier_market is not None and 0.0 <= brier_market <= 1.0


def test_brier_n_counts_scored_entries_and_never_exceeds_them(round_trip):
    """
    brier_n is what the maturity gate counts, so it must describe the means
    beside it: > 0 whenever a score exists, and never more than the entries
    the run actually made. In this scenario every entry is observed, so the
    two coincide -- the inequality is what generalises.
    """
    _run, summary, _out_dir = round_trip

    brier_n = summary["extras"]["brier_n"]
    n_entries = summary["counters"]["n_entries"]

    assert brier_n > 0
    assert brier_n <= n_entries
    assert summary["extras"]["brier_model"] is not None


def test_brier_terms_are_paired(monkeypatch):
    """
    An entry missing either probability is scored into NEITHER mean. Scoring
    model over one subset and market over another would make
    `brier_model < brier_market` -- the entire maturity bar -- a comparison
    between two different sets of days.
    """
    from backtest import report

    class _Pos:
        def __init__(self, pid, side, bucket, target_date):
            self.position_id, self.side = pid, side
            self.bucket_c, self.target_date = bucket, target_date

    day = date(2026, 8, 10)
    positions = [_Pos("a", "YES", 31, day), _Pos("b", "YES", 31, day)]

    class _Run:
        station_icao = "WSSS"
        closed_positions = positions
        unresolved_positions = []
        entry_records = {
            "a": {"model_prob": 0.6, "market_price": 0.5},
            "b": {"model_prob": 0.9},  # no market price -- unscorable, both sides
        }

    monkeypatch.setattr(report, "_observations_by_date", lambda run: {day: 31.0})

    scores = report._brier_scores(_Run())

    assert scores["brier_n"] == 1
    # Entry "b" contributed to neither mean: 0.6 -> (0.6-1)^2 = 0.16 alone.
    assert scores["brier_model"] == pytest.approx(0.16)
    assert scores["brier_market"] == pytest.approx(0.25)


def test_equity_csv_row_count(round_trip):
    run, _summary, out_dir = round_trip
    lines = (out_dir / "equity.csv").read_text(encoding="utf-8").splitlines()

    assert lines[0] == "ts,equity"
    assert len(lines) == len(run.portfolio.equity_curve) + 1  # +1 for the header

    first_ts, first_equity = run.portfolio.equity_curve[0]
    assert lines[1].split(",")[0] == str(int(first_ts))
    assert float(lines[1].split(",")[1]) == pytest.approx(first_equity)


def test_positions_jsonl_one_line_per_closed_position(round_trip):
    run, _summary, out_dir = round_trip
    lines = [
        line for line in
        (out_dir / "positions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(lines) == len(run.closed_positions)

    ids = set()
    for line in lines:
        row = json.loads(line)
        # Every models.Position field survives the round trip...
        for field in ("position_id", "station_icao", "target_date", "bucket_c",
                      "side", "entry_price", "size_usd", "entry_time", "status",
                      "exit_price", "exit_time", "exit_reason", "token_id", "is_paper"):
            assert field in row, f"Position field '{field}' missing from positions.jsonl"
        # ...with the date coerced to an ISO string, not left as a date object.
        assert isinstance(row["target_date"], str)
        # ...merged with the entry-record extras, joined on position_id.
        for field in ("model_prob", "market_price", "net_ev", "slippage_cost_usd"):
            assert field in row, f"entry_record extra '{field}' missing from positions.jsonl"
        ids.add(row["position_id"])

    assert ids == {p.position_id for p in run.closed_positions}


def test_decisions_jsonl_one_line_per_decision_cycle(round_trip):
    run, _summary, out_dir = round_trip
    lines = [
        line for line in
        (out_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(lines) == len(run.decisions_log)
    for line in lines:
        json.loads(line)


def test_manifest_json_parses(round_trip):
    run, _summary, out_dir = round_trip
    parsed = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert parsed["run_id"] == run.run_id
    assert "config_constants" in parsed


def test_write_artifacts_is_idempotent_and_leaves_no_tmp_files(round_trip):
    """Re-running the same experiment overwrites in place, atomically."""
    from backtest import report

    run, _summary, out_dir = round_trip
    again = report.write_artifacts(run, out_dir=out_dir.parent)

    assert again == out_dir
    assert not list(out_dir.glob("*.tmp"))
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "decisions.jsonl",
        "equity.csv",
        "manifest.json",
        "positions.jsonl",
        "summary.json",
    ]


def test_print_report_runs_on_a_real_summary(round_trip, capsys):
    from backtest import report

    _run, summary, _out_dir = round_trip
    report.print_report(summary)

    out = capsys.readouterr().out
    # Data quality must come before the headline -- the ordering is the point.
    assert out.index("Data quality") < out.index("Headline")
    assert "Brier" in out
    assert "Slippage sensitivity" in out
    assert "Unresolved" in out


def test_print_report_handles_no_closed_positions(capsys):
    """summarize_positions() returns None for an empty run; print must cope."""
    from backtest import report

    summary = {
        "run_id": "bt_empty",
        "station_icao": "WSSS",
        "start_date": "2026-08-10",
        "end_date": "2026-08-12",
        "params": {"depth_regime": "strict", "fee_rate_pct": 0.0, "bankroll_mode": "static"},
        "metrics": None,
        "extras": {
            "max_drawdown_pct": 0.0,
            "final_equity_usd": 1000.0,
            "starting_bankroll_usd": 1000.0,
            "brier_model": None,
            "brier_market": None,
            "n_unresolved": 0,
        },
        "counters": {"n_cycles": 0, "n_entry_cycles": 0, "n_entries": 0,
                     "n_skipped_no_price": 0, "n_skipped_implausible_move": 0,
                     "rejections": {}},
        "provenance": {"n_ticks": 0, "pct_live_snapshot": 0.0,
                       "pct_with_depth": 0.0, "median_fidelity_min": None},
        "slippage_sensitivity": 0.0,
        "generated_at": "2026-08-02T00:00:00+00:00",
    }

    report.print_report(summary)
    out = capsys.readouterr().out
    assert "No closed positions" in out
