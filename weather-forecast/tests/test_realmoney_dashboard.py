"""Tests for deploy/generate_realmoney_dashboard.py.

The generator lives in deploy/, which is not on sys.path and is not a
package. It is loaded by file path -- which only works because, unlike its
two sibling generators, this module runs nothing at import time.
"""
import importlib.util
import pathlib
import sys

import pytest

_GEN_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy" / "generate_realmoney_dashboard.py"
)


def load_gen():
    """Import the generator module fresh. Must have NO import-time side effects."""
    spec = importlib.util.spec_from_file_location("generate_realmoney_dashboard", _GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_import_writes_nothing(tmp_path, monkeypatch):
    """Importing must not parse argv or write a page -- both siblings do, which
    is exactly why neither of them has a single test."""
    monkeypatch.setattr(sys, "argv", ["generate_realmoney_dashboard.py", "--out", str(tmp_path / "x.html")])
    load_gen()
    assert not (tmp_path / "x.html").exists()


def test_main_renders_a_page(tmp_path):
    """Safe without store isolation: at Task 1 main() builds no sections and
    so touches no database. From Task 8 the full-render tests take the
    isolated_stores fixture instead."""
    gen = load_gen()
    out = tmp_path / "realmoney.html"
    status = gen.main(["--out", str(out)])
    assert status == 0
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Real-money stations" in page


def test_render_page_escapes_warnings():
    """A warning is data, not markup -- it can carry an exception string."""
    gen = load_gen()
    page = gen.render_page([], ["boom <script>alert(1)</script>"])
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- gate 2 probe ------------------------------------------------------------
# The drop-in this probe reads also holds POLYMARKET_PRIVATE_KEY. Every test
# here exists to pin one property: the probe answers a yes/no question about
# a NAME and never surfaces a VALUE.

_BLOB = (
    b"PATH=/usr/bin\x00"
    b"POLYMARKET_PRIVATE_KEY=0xdeadbeefcafe\x00"
    b"POLYMARKET_LIVE_TRADING=true\x00"
    b"HOME=/root\x00"
)


def test_environ_blob_has_name_finds_the_name():
    gen = load_gen()
    assert gen.environ_blob_has_name(_BLOB, "POLYMARKET_LIVE_TRADING") is True


def test_environ_blob_has_name_missing_name():
    gen = load_gen()
    assert gen.environ_blob_has_name(_BLOB, "POLYMARKET_NOT_SET") is False


def test_environ_blob_has_name_returns_a_bool_not_a_value():
    """The only thing that may leave this function is True or False. A prefix
    match that returned the entry would leak the private key."""
    gen = load_gen()
    result = gen.environ_blob_has_name(_BLOB, "POLYMARKET_PRIVATE_KEY")
    assert result is True
    assert isinstance(result, bool)
    assert "0xdeadbeefcafe" not in repr(result)


def test_environ_blob_has_name_does_not_match_a_prefix():
    """POLYMARKET_LIVE must not satisfy a probe for POLYMARKET_LIVE_TRADING,
    and vice versa -- the match is on the full name up to '='."""
    gen = load_gen()
    assert gen.environ_blob_has_name(_BLOB, "POLYMARKET_LIVE") is False


def test_gate2_state_unknown_when_pid_unavailable(monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "_main_pid", lambda unit: None)
    assert gen.gate2_state() == "unknown"


def test_gate2_state_unknown_when_environ_unreadable(monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "_main_pid", lambda unit: 4242)
    monkeypatch.setattr(gen, "_read_environ", lambda pid: None)
    assert gen.gate2_state() == "unknown"


def test_gate2_state_present_and_absent(monkeypatch):
    gen = load_gen()
    monkeypatch.setattr(gen, "_main_pid", lambda unit: 4242)
    monkeypatch.setattr(gen, "_read_environ", lambda pid: _BLOB)
    assert gen.gate2_state() == "present"
    monkeypatch.setattr(gen, "_read_environ", lambda pid: b"PATH=/usr/bin\x00")
    assert gen.gate2_state() == "absent"


# --- schedule windows --------------------------------------------------------
# A synthetic table in config.SCHEDULE_WINDOWS' shape:
#   (start_h, start_m, end_h, end_m, interval_min, mode, min_net_ev, description)
# Entry windows are the ones with a non-None min_net_ev. Deliberately NOT the
# real table -- these assertions must not move when the schedule is retuned.
_WINDOWS = [
    (0, 0, 4, 0, None, "closed", None, "overnight"),
    (4, 0, 5, 0, 15, "pre_poll", None, "early watch"),
    (5, 0, 8, 0, 10, "primary", 0.15, "primary edge window"),
    (8, 0, 24, 0, 30, "monitor_only", None, "exits only"),
]


def test_active_window_inside_primary():
    gen = load_gen()
    w = gen.active_window(6 * 60, _WINDOWS)
    assert w["mode"] == "primary"
    assert w["min_net_ev"] == 0.15
    assert w["interval_min"] == 10


def test_active_window_is_half_open_at_the_boundary():
    """08:00 belongs to monitor_only, not primary -- entries close AT 08:00."""
    gen = load_gen()
    assert gen.active_window(8 * 60 - 1, _WINDOWS)["mode"] == "primary"
    assert gen.active_window(8 * 60, _WINDOWS)["mode"] == "monitor_only"


def test_active_window_returns_none_on_a_gap():
    gen = load_gen()
    assert gen.active_window(6 * 60, [(0, 0, 1, 0, None, "closed", None, "x")]) is None


def test_next_entry_boundary_inside_an_entry_window_counts_to_close():
    gen = load_gen()
    assert gen.next_entry_boundary(6 * 60, _WINDOWS) == ("closes", 120)


def test_next_entry_boundary_before_the_window_counts_to_open():
    gen = load_gen()
    assert gen.next_entry_boundary(4 * 60 + 30, _WINDOWS) == ("opens", 30)


def test_next_entry_boundary_wraps_past_midnight():
    """22:00 -> the next entry window is 05:00 tomorrow: 7h."""
    gen = load_gen()
    assert gen.next_entry_boundary(22 * 60, _WINDOWS) == ("opens", 420)


def test_next_entry_boundary_none_when_nothing_accepts_entries():
    gen = load_gen()
    closed_only = [(0, 0, 24, 0, None, "closed", None, "nothing runs")]
    assert gen.next_entry_boundary(6 * 60, closed_only) is None


# --- bounds drift ------------------------------------------------------------
# Reproduces ev_engine's BOUNDS DRIFT warning as page state. That warning is
# currently a journal line you have to know to grep for.


def test_bounds_drift_none_when_ranges_agree():
    gen = load_gen()
    assert gen.bounds_drift(28, 38, [30, 31, 32, 38, 28]) is None


def test_bounds_drift_none_when_nothing_discovered():
    """No discovery is not drift -- it is the discovery section's business."""
    gen = load_gen()
    assert gen.bounds_drift(28, 38, []) is None


def test_bounds_drift_reports_a_wider_live_event():
    gen = load_gen()
    d = gen.bounds_drift(28, 38, [26, 30, 40])
    assert d["config"] == (28, 38)
    assert d["discovered"] == (26, 40)


def test_bounds_drift_reports_a_narrower_live_event():
    gen = load_gen()
    d = gen.bounds_drift(28, 38, [30, 31, 32])
    assert d["config"] == (28, 38)
    assert d["discovered"] == (30, 32)


def test_bounds_drift_ignores_non_integer_buckets():
    """list_tokens() can hand back a NULL bucket_c on a malformed row."""
    gen = load_gen()
    assert gen.bounds_drift(28, 38, [None, 30, 38, 28]) is None


# --- readiness ladder --------------------------------------------------------
# The rung the ladder gets WRONG by default is capacity: count_live_order_attempts
# returns None when it cannot read, and its callers in the trading path treat
# that as "cannot authorise" precisely because a rate limit that fails open is
# not a rate limit. The page must not render that None as 0.


@pytest.fixture
def isolated_stores(monkeypatch):
    """Keep the suite from touching the real databases.

    storage._db() and price_store._connect() both CREATE their sqlite file
    lazily. Left alone, merely running these tests would write
    data/polyweather.sqlite3 and data/market_data.sqlite3 into the checkout
    -- a test suite with a side effect on the operator's own box. Every test
    that exercises an impure path takes this fixture.

    count_observations_from_source, forecast_error_samples and
    load_position_history are patched too, and NOT because any test today
    calls them directly: they are the storage reads on
    config.station_maturity()'s non-override path (maturity_report(), which
    checks observations, bias pairs/precision/stability via the first two,
    and the order_path criterion via the third). WSSS and RCSS both happen
    to be in config.MATURITY_OVERRIDE today, so station_maturity()
    short-circuits before ever reaching any of them -- but that is an
    unstated invariant, not isolation. This page exists partly to show
    readiness for stations that are NOT yet live, so a future test naming a
    non-overridden station would otherwise fall through to maturity_report()
    and lazily create the real sqlite file on the strength of an assumption
    this fixture never actually enforced. Verified directly: with only the
    first two of these three patched, calling maturity_report() for a
    non-overridden station (e.g. WMKK) still creates the sqlite file --
    order_path's storage.load_position_history() call is unguarded. All
    three must be patched for the claim in this docstring to be true rather
    than lucky.
    """
    import storage

    monkeypatch.setattr(storage, "count_live_order_attempts",
                        lambda kind, since_iso, station_icaos=None: 0)
    monkeypatch.setattr(storage, "load_live_order_attempts", lambda limit=50: [])
    monkeypatch.setattr(storage, "count_observations_from_source",
                        lambda station_icao, source: 0)
    monkeypatch.setattr(storage, "forecast_error_samples",
                        lambda station_icao, source: [])
    monkeypatch.setattr(storage, "load_position_history",
                        lambda station_icao, limit=100, is_paper=None: [])
    return monkeypatch


def test_capacity_rung_reports_headroom():
    gen = load_gen()
    r = gen.capacity_rung("WSSS", "asia", {"orders_today": 3, "cap": 10})
    assert r["state"] == "ok"
    assert "3" in r["value"] and "10" in r["value"]


def test_capacity_rung_at_the_cap_is_not_ok():
    gen = load_gen()
    r = gen.capacity_rung("WSSS", "asia", {"orders_today": 10, "cap": 10})
    assert r["state"] == "no"


def test_capacity_rung_unknown_is_never_zero():
    """An unreadable count is 'unknown', not 'plenty of headroom'."""
    gen = load_gen()
    r = gen.capacity_rung("WSSS", "asia", {"orders_today": None, "cap": 10})
    assert r["state"] == "unknown"
    assert "unknown" in r["value"].lower()
    assert not r["value"].strip().startswith("0")


def test_readiness_rungs_covers_every_gate(monkeypatch, isolated_stores):
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "present")
    from datetime import datetime, timezone

    rungs = gen.readiness_rungs("WSSS", datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc))
    labels = [r["label"] for r in rungs]
    for expected in ("Gate 2", "Mode", "Gate 1", "Maturity", "Region", "Window", "Capacity"):
        assert any(expected in lab for lab in labels), f"missing rung: {expected}"


def test_readiness_gate2_unknown_renders_unknown_not_off(monkeypatch, isolated_stores):
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    from datetime import datetime, timezone

    rungs = gen.readiness_rungs("WSSS", datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc))
    gate2 = next(r for r in rungs if "Gate 2" in r["label"])
    assert gate2["state"] == "unknown"
    assert "cannot" in gate2["value"].lower()


def test_readiness_maturity_says_when_it_is_an_override(monkeypatch, isolated_stores):
    """Both live stations are mature only by MATURITY_OVERRIDE, and RCSS fails
    the measured beats_market criterion. A bare 'mature' would misreport the
    single most important caveat on the real-money track."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "present")
    from datetime import datetime, timezone

    rungs = gen.readiness_rungs("WSSS", datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc))
    maturity = next(r for r in rungs if "Maturity" in r["label"])
    assert "override" in (maturity["value"] + maturity["why"]).lower()


def test_render_readiness_groups_by_region(monkeypatch, isolated_stores):
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "present")
    from datetime import datetime, timezone

    warnings = []
    out = gen.render_readiness(
        ["WSSS", "RCSS"], datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc), warnings
    )
    assert "asia" in out.lower()
    assert "WSSS" in out and "RCSS" in out
