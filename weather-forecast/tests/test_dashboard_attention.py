"""Dashboard Phase 1: attention panel semantics and the $0.03 P&L drift."""
from types import SimpleNamespace

import calibration_panel
import paper_trading_report as ptr

KILL = {"window_days": 30, "level": 0.0, "min_station_days": 30,
        "n": 90, "n_days": 40, "net_price_edge": -0.01, "fired": True}


def test_fired_kill_and_brake_are_labelled_enforced():
    out = calibration_panel.render_attention_html(("kill_criterion", "it fired"), KILL)
    assert "<b>REFUSED</b>" in out and "<b>FIRED</b>" in out
    assert "enforced" in out and "-0.0100" in out


def test_unreadable_inputs_render_unknown_never_ok():
    out = calibration_panel.render_attention_html(("unknown", "boom"), None)
    assert "<b>OK</b>" not in out
    assert out.count("<b>UNKNOWN</b>") == 2


def test_clear_state_and_page_failures():
    clear = dict(KILL, fired=False, net_price_edge=0.02)
    out = calibration_panel.render_attention_html(None, clear, n_warnings=2, n_feeds_bad=1)
    assert out.count("<b>OK</b>") == 2 and out.count("<b>CHECK</b>") == 2


def test_reason_text_is_escaped():
    out = calibration_panel.render_attention_html(("error", "<script>x</script>"), KILL)
    assert "<script>" not in out


def test_summing_exact_pnl_reconciles_where_rounded_drifts():
    # Three one-trade groups of +$0.004 each: rounded per group they sum to
    # $0.00, the true total is $0.012 -> $0.01.
    groups = [[SimpleNamespace(station_icao="WSSS", entry_price=0.5, exit_price=0.5002,
                               size_usd=10.0, status="closed_resolved",
                               entry_fee_per_share=0.0)] for _ in range(3)]
    sums = [ptr.summarize_positions(g) for g in groups]
    assert round(sum(s["total_pnl_usd"] for s in sums), 2) == 0.0
    assert round(sum(s["total_pnl_usd_exact"] for s in sums), 2) == 0.01
