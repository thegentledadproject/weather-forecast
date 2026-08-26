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


def test_main_renders_a_page(tmp_path, monkeypatch, isolated_stores):
    """From Task 8, main() builds all four sections, which read storage and
    price_store -- both create their sqlite file lazily on first use. Needs
    isolated_stores like the full-render tests below it."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")
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


# --- EV detail ---------------------------------------------------------------
# The region pages show only rows clearing the entry screen. This page shows
# ALL of them: when the question is "why didn't it trade", the near-misses are
# the signal. The badges must read config, never restate it -- the existing EV
# card has gone stale that way twice.


def test_ev_row_flags_marks_over_price_cap():
    gen = load_gen()
    row = {"market_price": 0.92, "raw_edge": 0.02}
    flags = gen.ev_row_flags(row, max_entry_price=0.90, edge_ceiling_for=lambda p: 0.25)
    assert "over price cap" in flags


def test_ev_row_flags_marks_veto_zone_using_the_price_relative_ceiling():
    """The edge ceiling is a FUNCTION of price, not a flat constant."""
    gen = load_gen()
    row = {"market_price": 0.10, "raw_edge": 0.30}
    flags = gen.ev_row_flags(row, max_entry_price=1.0, edge_ceiling_for=lambda p: 0.20)
    assert "veto zone" in flags


def test_ev_row_flags_clean_row_has_no_badges():
    gen = load_gen()
    row = {"market_price": 0.40, "raw_edge": 0.05}
    assert gen.ev_row_flags(row, 1.0, lambda p: 0.25) == ""


def test_ev_row_flags_marks_fallback_spread():
    gen = load_gen()
    row = {"market_price": 0.40, "raw_edge": 0.05, "spread_source": "fallback_default"}
    assert "fallback" in gen.ev_row_flags(row, 1.0, lambda p: 0.25)


def test_ev_row_flags_tolerates_a_missing_price():
    """An unpriced far-tail book has market_price None; it must not raise."""
    gen = load_gen()
    assert gen.ev_row_flags({"market_price": None, "raw_edge": 0.05}, 1.0, lambda p: 0.25) == ""


def test_render_ev_shows_rows_below_the_bar(tmp_path, monkeypatch):
    """The whole point of this section: a row under the bar still renders."""
    import json
    gen = load_gen()
    monkeypatch.setattr(gen, "_ev_snapshot_path", lambda icao: tmp_path / f"ev_latest_{icao}.json")
    (tmp_path / "ev_latest_WSSS.json").write_text(json.dumps({
        "station_icao": "WSSS",
        "generated_at": "2026-08-26T05:01:00+00:00",
        "target_date": "2026-08-26",
        "results": [
            {"bucket_c": 32, "side": "YES", "model_prob": 0.30, "market_price": 0.28,
             "raw_edge": 0.02, "slippage_pct": 0.01, "net_ev_per_dollar": 0.02,
             "spread_source": "measured", "notes": ""},
        ],
    }), encoding="utf-8")
    out = gen.render_ev(["WSSS"], bar=0.15, warnings=[])
    assert "32" in out
    assert "2.0%" in out or "+2.0%" in out


def test_render_ev_reports_a_missing_snapshot_as_never_computed(monkeypatch, tmp_path):
    gen = load_gen()
    monkeypatch.setattr(gen, "_ev_snapshot_path", lambda icao: tmp_path / f"nope_{icao}.json")
    out = gen.render_ev(["WSSS"], bar=0.15, warnings=[])
    assert "no ev snapshot" in out.lower() or "never" in out.lower()


def test_render_ev_reports_unreadable_snapshot_as_a_warning_and_skips_station(monkeypatch, tmp_path):
    """Missing vs unreadable are different cases: this pins the unreadable half.

    A snapshot that exists but is not valid JSON must not raise, must be
    reported as a warning (not the 'never computed' message), and must not
    contribute any rows.
    """
    gen = load_gen()
    monkeypatch.setattr(gen, "_ev_snapshot_path", lambda icao: tmp_path / f"ev_latest_{icao}.json")
    (tmp_path / "ev_latest_WSSS.json").write_text("{not valid json", encoding="utf-8")
    warnings = []
    out = gen.render_ev(["WSSS"], bar=0.15, warnings=warnings)
    assert "no EV data for any real-money station" in out
    assert any("WSSS" in w for w in warnings)


def test_render_ev_one_station_failing_does_not_cost_the_others(monkeypatch, tmp_path):
    """render_ev degrades per station, like render_readiness -- not all-or-nothing."""
    import json
    gen = load_gen()
    good_path = tmp_path / "ev_latest_WSSS.json"
    good_path.write_text(json.dumps({
        "station_icao": "WSSS",
        "generated_at": "2026-08-26T05:01:00+00:00",
        "target_date": "2026-08-26",
        "results": [
            {"bucket_c": 32, "side": "YES", "model_prob": 0.30, "market_price": 0.28,
             "raw_edge": 0.02, "slippage_pct": 0.01, "net_ev_per_dollar": 0.02,
             "spread_source": "measured", "notes": ""},
        ],
    }), encoding="utf-8")

    def flaky_path(icao):
        if icao == "RCSS":
            raise RuntimeError("boom")
        return good_path

    monkeypatch.setattr(gen, "_ev_snapshot_path", flaky_path)
    warnings = []
    out = gen.render_ev(["RCSS", "WSSS"], bar=0.15, warnings=warnings)
    assert "WSSS" in out
    assert "32" in out
    assert any("RCSS" in w for w in warnings)


def test_render_ev_tolerates_a_missing_bucket_like_every_other_numeric_field(monkeypatch, tmp_path):
    """bucket_c had no None guard while every sibling numeric field did."""
    import json
    gen = load_gen()
    monkeypatch.setattr(gen, "_ev_snapshot_path", lambda icao: tmp_path / f"ev_latest_{icao}.json")
    (tmp_path / "ev_latest_WSSS.json").write_text(json.dumps({
        "station_icao": "WSSS",
        "generated_at": "2026-08-26T05:01:00+00:00",
        "target_date": "2026-08-26",
        "results": [
            {"bucket_c": None, "side": "YES", "model_prob": 0.30, "market_price": 0.28,
             "raw_edge": 0.02, "slippage_pct": 0.01, "net_ev_per_dollar": 0.02,
             "spread_source": "measured", "notes": ""},
        ],
    }), encoding="utf-8")
    out = gen.render_ev(["WSSS"], bar=0.15, warnings=[])
    assert "None" not in out


# --- discovery + order trail -------------------------------------------------


def _seed_tokens(db_path, icao, target_date, buckets, with_book=()):
    """Seed market_tokens (+ optional fresh snapshots) in a throwaway db."""
    import time

    import backtest.price_store as price_store

    now = int(time.time())
    for b in buckets:
        token = f"tok-{icao}-{b}"
        price_store.upsert_token(
            token_id=token, station_icao=icao, target_date=target_date,
            bucket_c=b, side="yes", discovered_at="2026-08-26T05:01:00+00:00",
            db_path=db_path,
        )
        if b in with_book:
            price_store.save_snapshot(
                token_id=token, ts=now - 60, price=0.30, depth_usd=None,
                source=price_store.EXIT_SNAPSHOT_SOURCE, fidelity_min=5,
                db_path=db_path,
            )


def test_discovery_state_reports_buckets_and_books(tmp_path):
    gen = load_gen()
    db = str(tmp_path / "market.sqlite3")
    _seed_tokens(db, "WSSS", "2026-08-26", [30, 31, 32], with_book=(30, 32))
    st = gen.discovery_state("WSSS", "2026-08-26", db_path=db)
    assert sorted(st["buckets"]) == [30, 31, 32]
    assert st["with_book"] == 2
    assert st["first_seen"] == "2026-08-26T05:01:00+00:00"


def test_discovery_state_empty_when_nothing_recorded(tmp_path):
    gen = load_gen()
    db = str(tmp_path / "market.sqlite3")
    _seed_tokens(db, "WSSS", "2026-08-26", [])
    st = gen.discovery_state("WSSS", "2026-08-26", db_path=db)
    assert st["buckets"] == []
    assert st["with_book"] == 0
    assert st["first_seen"] is None
    assert st["drift"] is None


def test_render_orders_says_unknown_when_the_count_is_unreadable(monkeypatch):
    gen = load_gen()
    import storage

    monkeypatch.setattr(storage, "load_live_order_attempts", lambda limit=50: [])
    monkeypatch.setattr(storage, "count_live_order_attempts",
                        lambda kind, since_iso, station_icaos=None: None)
    out = gen.render_orders(limit=10, warnings=[])
    assert "unknown" in out.lower()


def test_render_orders_lists_a_submission(monkeypatch):
    gen = load_gen()
    import storage

    monkeypatch.setattr(storage, "load_live_order_attempts", lambda limit=50: [{
        "ts": "2026-08-26T05:02:00+00:00", "kind": "entry", "station_icao": "RCSS",
        "target_date": "2026-08-26", "bucket_c": 32, "side": "YES",
        "notional_usd": 1.55, "size_shares": 5.0, "limit_price": 0.31,
        "outcome": "filled", "order_id": "0xa1d4c085deadbeef", "detail": "",
    }])
    monkeypatch.setattr(storage, "count_live_order_attempts",
                        lambda kind, since_iso, station_icaos=None: 1)
    out = gen.render_orders(limit=10, warnings=[])
    assert "RCSS" in out and "filled" in out
    assert "0xa1d4c085" in out
    assert "$1.55" in out


def test_render_orders_empty_trail(monkeypatch):
    gen = load_gen()
    import storage

    monkeypatch.setattr(storage, "load_live_order_attempts", lambda limit=50: [])
    monkeypatch.setattr(storage, "count_live_order_attempts",
                        lambda kind, since_iso, station_icaos=None: 0)
    out = gen.render_orders(limit=10, warnings=[])
    assert "no real order" in out.lower()


# --- render_discovery ---------------------------------------------------------
# discovery_state itself is monkeypatched here, not exercised through a real
# db_path -- render_discovery's interface takes no db_path, so the only way
# to keep these off the real sqlite file is to fake the function it calls,
# the same way the EV tests fake _ev_snapshot_path.


def test_render_discovery_empty_says_capture_not_absence(monkeypatch):
    gen = load_gen()
    import config

    monkeypatch.setattr(config, "local_today", lambda icao: "2026-08-26")
    monkeypatch.setattr(gen, "discovery_state", lambda icao, target, db_path=None: {
        "buckets": [], "first_seen": None, "with_book": 0, "drift": None,
    })
    out = gen.render_discovery(["WSSS"], warnings=[])
    assert "capture has recorded no" in out.lower()
    assert "not that the market does not exist" in out.lower()


def test_render_discovery_reports_buckets_books_and_drift(monkeypatch):
    gen = load_gen()
    import config

    monkeypatch.setattr(config, "local_today", lambda icao: "2026-08-26")
    monkeypatch.setattr(gen, "discovery_state", lambda icao, target, db_path=None: {
        "buckets": [30, 31, 32], "first_seen": "2026-08-26T05:01:00+00:00",
        "with_book": 2,
        "drift": {"config": (28, 30), "discovered": (30, 32),
                  "note": "registry lists 28-30°C, discovery recorded 30-32°C"},
    })
    out = gen.render_discovery(["WSSS"], warnings=[])
    assert "WSSS" in out
    assert "3 bucket" in out
    assert "2 of 3" in out
    assert "bounds drift" in out.lower()


def test_render_discovery_one_station_failing_does_not_cost_the_others(monkeypatch):
    gen = load_gen()
    import config

    monkeypatch.setattr(config, "local_today", lambda icao: "2026-08-26")

    def flaky(icao, target, db_path=None):
        if icao == "RCSS":
            raise RuntimeError("boom")
        return {"buckets": [32], "first_seen": "2026-08-26T05:01:00+00:00",
                "with_book": 1, "drift": None}

    monkeypatch.setattr(gen, "discovery_state", flaky)
    warnings = []
    out = gen.render_discovery(["RCSS", "WSSS"], warnings)
    assert "WSSS" in out
    assert any("RCSS" in w for w in warnings)


# --- full render -------------------------------------------------------------


def test_full_render_has_every_section(tmp_path, monkeypatch, isolated_stores):
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")
    out = tmp_path / "realmoney.html"
    assert gen.main(["--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    for heading in ("Readiness", "Edge and EV", "Discovery", "Order activity"):
        assert heading in page, f"missing section: {heading}"


def test_full_render_states_what_the_ladder_cannot_know(tmp_path, monkeypatch, isolated_stores):
    """Stage 1 has no persisted EntryDecision. The page must say so rather
    than let the ladder read as the whole gate."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")
    out = tmp_path / "realmoney.html"
    gen.main(["--out", str(out)])
    page = out.read_text(encoding="utf-8").lower()
    assert "per-candidate" in page or "not recorded" in page


def test_full_render_survives_a_broken_section(tmp_path, monkeypatch, isolated_stores):
    """Fail-soft is the contract: one section blowing up costs the reader that
    section and nothing else."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(gen, "render_ev", boom)
    out = tmp_path / "realmoney.html"
    assert gen.main(["--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "Render warnings" in page
    assert "synthetic failure" in page
    # "Readiness" alone proves nothing -- _section's OWN failure branch
    # renders "<title> unavailable", so the heading is in the page whether
    # the builder succeeded or failed. Assert on a rung only a successful
    # render produces, and rule out the failure placeholder by name.
    assert "Gate 2 (process)" in page  # a real readiness rung actually rendered
    assert "Readiness unavailable" not in page


def test_full_render_survives_a_broken_render_page(tmp_path, monkeypatch, isolated_stores):
    """render_page() itself is called bare in main() -- nothing may raise out
    of main(), including from the final assembly step, not just the four
    section builders."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")

    def boom(sections, warnings):
        raise RuntimeError("render_page synthetic failure")

    monkeypatch.setattr(gen, "render_page", boom)
    out = tmp_path / "realmoney.html"
    assert gen.main(["--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "render_page synthetic failure" in page


def test_full_render_survives_a_non_numeric_ev_bar(tmp_path, monkeypatch, isolated_stores):
    """The EV caption's f-string is built as a call argument, BEFORE
    _section's try is ever entered -- a non-numeric min_net_ev must not
    escape through the very mechanism meant to guard sections."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")
    monkeypatch.setattr(gen, "active_window", lambda minute, windows: {
        "start_minute": 0, "end_minute": 1440, "interval_min": 5,
        "mode": "primary", "min_net_ev": "not-a-number", "description": "test window",
    })
    out = tmp_path / "realmoney.html"
    assert gen.main(["--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "Edge and EV" in page
    assert "no entry window currently open" in page
    assert "EV bar caption formatting failed" in page  # surfaced via Render warnings


def test_full_render_ev_caption_names_the_station_the_bar_came_from(tmp_path, monkeypatch, isolated_stores):
    """The bar is computed from ONE station's local window (icaos[0]) but
    captions a table covering every live station. Overclaiming by omission:
    the caption must name which station's window the number came from."""
    gen = load_gen()
    monkeypatch.setattr(gen, "gate2_state", lambda unit="polyweather": "unknown")
    monkeypatch.setattr(gen, "render_discovery", lambda icaos, warnings: "<div>stub</div>")
    monkeypatch.setattr(gen, "active_window", lambda minute, windows: {
        "start_minute": 0, "end_minute": 1440, "interval_min": 5,
        "mode": "primary", "min_net_ev": 0.08, "description": "test window",
    })
    out = tmp_path / "realmoney.html"
    assert gen.main(["--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "8%" in page
    # sorted(config.LIVE_TRADING_STATIONS) == ["RCSS", "WSSS"], so icaos[0] is RCSS.
    # Captions are inserted into render_page raw (they already carry HTML
    # entities like &mdash;), so the apostrophe is not re-escaped.
    assert "RCSS's active-window bar" in page
    # The caption must not presuppose a timezone spread among live stations
    # that does not exist -- it points to the per-station rung instead.
    assert "different timezone" not in page
    assert "Window rung" in page
