# Real-money Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `deploy/generate_realmoney_dashboard.py`, a standalone generator rendering `/var/www/html/realmoney.html`, which answers "could a real order open right now, and if not, what is in the way?"

**Architecture:** A standalone sibling of `deploy/generate_backtest_dashboard.py` — its own page shell and CSS, importing only the package (`config`, `storage`, `scheduler`, `backtest.price_store`). Unlike both existing generators it runs nothing at import time: argument parsing and rendering live in a guarded `main()`, so its pure helpers are unit-testable. Every read is fail-soft; a failure renders as a warning ON the page.

**Tech Stack:** Python 3.12 stdlib only (`argparse`, `html`, `json`, `os`, `subprocess`, `sys`, `datetime`, `pathlib`). pytest for tests. No third-party dependencies.

**Spec:** `docs/superpowers/specs/2026-08-26-realmoney-dashboard-design.md`

## Global Constraints

- **Stage 1 only.** No file under `weather-forecast/` (the package) may be modified. This plan touches `deploy/` and `weather-forecast/tests/` only. Stage 2 (persisting `EntryDecision`s from `entry_manager`) is explicitly out of scope.
- **Fail-soft, always.** Every data read is wrapped. A failure appends to `warnings` and renders on the page. Nothing may raise out of `main()`.
- **Three values are never falsy-defaulted:** an unreadable order count renders `unknown`, never `0`; an unobservable gate 2 renders `cannot observe`, never `off`; no discovered buckets renders `capture has not recorded any`, never a claim that the market does not exist.
- **The gate 2 probe reads NAMES, never values.** No function may return, print, log or store a value read from `/proc/<pid>/environ`.
- **Config is read, never restated.** Thresholds come from `config` via `getattr` with a fallback, never hardcoded. This has gone stale twice in the existing EV card.
- **No cross-region arithmetic.** Stations are grouped by `config.region_of(icao)`; no figure is ever summed across groups.
- **No `os.chdir()`.** `generate_dashboard.py` chdirs into the package dir; this module must not, because it makes `--out` relative paths behave differently under test than in production. `config.DATA_DIR` is absolute, so chdir buys nothing.
- Tests run from the package dir: `cd weather-forecast && python -m pytest tests -q`.

---

## File Structure

| File | Responsibility |
|---|---|
| `deploy/generate_realmoney_dashboard.py` | **Create.** The whole generator: pure helpers at module top, rendering functions, guarded `main()`. |
| `weather-forecast/tests/test_realmoney_dashboard.py` | **Create.** Loads the generator by path; covers the pure helpers and a full-render smoke test. |
| `deploy/setup_dashboard.sh` | **Modify.** Install the third generator so a rebuilt box reproduces the shape. |
| `deploy/deploy_daemon.sh` | **Modify (lines 66-69).** The frozen-copy refresh enumerates generators BY NAME; a third that is not added here is never refreshed by any deploy. |

Everything lives in one generator file because that is the established shape for these scripts and the pieces are read together. It is expected to land around 700-800 lines, comparable to `generate_backtest_dashboard.py` at 736.

---

### Task 1: Module skeleton, page shell, and render smoke test

**Files:**
- Create: `deploy/generate_realmoney_dashboard.py`
- Create: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `PKG` — module-level `str`, the package dir from `DASHBOARD_PKG_DIR`.
  - `render_page(sections: list[tuple[str, str, str]], warnings: list[str]) -> str` — takes `(title, caption, body_html)` triples, returns the complete HTML document.
  - `main(argv: list[str] | None = None) -> int` — parses args, builds the page, writes it, returns an exit status.

- [ ] **Step 1: Write the failing test**

Create `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: FAIL — `FileNotFoundError` / `spec_from_file_location` returns None, because `deploy/generate_realmoney_dashboard.py` does not exist.

- [ ] **Step 3: Write the minimal implementation**

Create `deploy/generate_realmoney_dashboard.py`:

```python
#!/usr/bin/env python3
"""Render the polyweather real-money station dashboard to /var/www/html/realmoney.html.

Sibling to generate_dashboard.py and generate_backtest_dashboard.py, same
fail-soft philosophy: every data read is wrapped so a missing or corrupt
source shows up as a warning ON the page rather than killing the render.

WHAT THIS PAGE IS FOR, AND WHAT IT IS NOT. The region pages report what the
book DID -- P&L, positions, daily grids. This page reports the state of the
machinery that decides whether the book does anything at all: the gate
ladder, the schedule position, whether market discovery has happened, and
what was actually submitted to the exchange. It carries no P&L, deliberately;
a second place where the book is scored is a second place for those numbers
to disagree.

UNLIKE ITS TWO SIBLINGS, THIS MODULE RUNS NOTHING AT IMPORT TIME. Both of
those parse argv at module scope, which makes every function in them
unreachable from a test. Everything here is behind main(), so the pure
helpers can be unit-tested (see tests/test_realmoney_dashboard.py).
"""
import argparse
import html
import os
import sys
from datetime import datetime, timezone

# Mirrors generate_dashboard.py: unset, it's the real EC2 path; set, it
# points at a local checkout so this script can be exercised off the box.
# The SAME variable name on purpose -- one export points both generators at
# a local checkout.
PKG = os.environ.get("DASHBOARD_PKG_DIR", "/home/ubuntu/weather-forecast/weather-forecast")
if PKG not in sys.path:
    sys.path.insert(0, PKG)

# NOTE: no os.chdir(). generate_dashboard.py chdirs into PKG; doing that here
# would make a relative --out resolve differently under test than in
# production. config.DATA_DIR is absolute, so the chdir buys nothing.

CSS = """
:root { --bg:#0f1115; --card:#171a21; --line:#252a34; --ink:#e6e9ef;
        --ink-2:#a7b0c0; --muted:#6f7a8d; --good:#3fb950; --bad:#f85149;
        --warn:#d29922; --accent:#58a6ff;
        --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
* { box-sizing:border-box; }
body { margin:0; padding:28px 20px 60px; background:var(--bg); color:var(--ink);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1180px; margin:0 auto; }
header h1 { margin:0 0 4px; font-size:22px; letter-spacing:-.01em; }
header .sub { margin:0 0 22px; color:var(--ink-2); font-size:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:18px 20px; margin:0 0 18px; }
.card h2 { margin:0 0 2px; font-size:15px; letter-spacing:.01em; }
.cap { margin:0 0 14px; color:var(--muted); font-size:12px; }
.empty { color:var(--muted); font-size:13px; padding:10px 0; }
.warn { border-color:var(--warn); }
.warn ul { margin:6px 0 0; padding-left:18px; color:var(--ink-2); font-size:12.5px; }
.tablewrap { overflow-x:auto; }
.ptable { border-collapse:collapse; width:100%; font-size:12.5px; }
.ptable th { text-align:left; font-size:10.5px; letter-spacing:.08em;
             text-transform:uppercase; color:var(--muted); padding:0 12px 8px 0;
             border-bottom:1px solid var(--line); white-space:nowrap; }
.ptable td { padding:8px 12px 8px 0; border-bottom:1px solid var(--line);
             white-space:nowrap; vertical-align:top; }
.ptable tr:last-child td { border-bottom:none; }
.ptable .mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
.ptable th.num, .ptable td.num { text-align:right; }
.ptable .pos { color:var(--good); } .ptable .neg { color:var(--bad); }
.ptable .dim2 { color:var(--muted); }
.ptable .sub { font-size:9.5px; color:var(--muted); margin-left:4px; }
.badge { display:inline-block; font-size:9.5px; letter-spacing:.05em;
         text-transform:uppercase; padding:1px 5px; border-radius:4px;
         margin-left:5px; border:1px solid var(--line); color:var(--ink-2); }
.badge.veto { border-color:var(--bad); color:var(--bad); }
.badge.fallback { border-color:var(--warn); color:var(--warn); }
.rung { display:flex; gap:10px; align-items:baseline; padding:6px 0;
        border-bottom:1px solid var(--line); font-size:13px; }
.rung:last-child { border-bottom:none; }
.rung .lab { width:190px; color:var(--ink-2); flex:none; }
.rung .val { font-family:var(--mono); }
.rung.ok .val { color:var(--good); }
.rung.no .val { color:var(--bad); }
.rung.unknown .val { color:var(--warn); }
.rung .why { color:var(--muted); font-size:12px; }
.region { margin:22px 0 10px; font-size:12px; letter-spacing:.1em;
          text-transform:uppercase; color:var(--muted); }
"""


def render_page(sections, warnings):
    """Assemble the full document from (title, caption, body_html) triples.

    `warnings` are exception strings and other machine text -- ESCAPED here,
    never interpolated raw. A warning routinely carries the repr of whatever
    blew up, which can contain markup.
    """
    body = []
    for title, caption, html_body in sections:
        body.append(
            f"<div class='card'><h2>{html.escape(title)}</h2>"
            + (f"<p class='cap'>{caption}</p>" if caption else "")
            + html_body
            + "</div>"
        )
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{html.escape(str(w))}</li>" for w in warnings)
        warn_html = (
            "<div class='card warn'><h2>Render warnings</h2>"
            "<p class='cap'>Sections that could not be built. The page renders anyway.</p>"
            f"<ul>{items}</ul></div>"
        )
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>polyweather -- real money</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>"
        "<header><h1>Real-money stations</h1>"
        "<p class='sub'>Can an order open right now &mdash; and if not, what is in the way? "
        "No P&amp;L here; the region pages own that.</p></header>"
        + "".join(body)
        + warn_html
        + f"<p class='cap'>rendered {stamp}</p>"
        "</div></body></html>"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/var/www/html/realmoney.html",
                        help="path to write the rendered HTML page to")
    args = parser.parse_args(argv)

    warnings = []
    sections = []

    page = render_page(sections, warnings)
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(page)
    except OSError as exc:
        print(f"[realmoney] could not write {args.out}: {exc}", file=sys.stderr)
        return 1
    print(f"real-money dashboard written to {args.out} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Add real-money dashboard skeleton with a testable main()"
```

---

### Task 2: Gate 2 probe — read the NAME, never the value

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the module existing.
- Produces:
  - `environ_blob_has_name(blob: bytes, name: str) -> bool` — pure; parses a NUL-separated `/proc/<pid>/environ` blob.
  - `gate2_state(unit: str = "polyweather") -> str` — returns exactly `"present"`, `"absent"` or `"unknown"`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k "environ or gate2"`
Expected: FAIL with `AttributeError: module has no attribute 'environ_blob_has_name'`.

- [ ] **Step 3: Write the minimal implementation**

Insert into `deploy/generate_realmoney_dashboard.py`, after `CSS` and before `render_page`:

```python
# --- gate 2 probe ------------------------------------------------------------
# GATE 2 IS PROCESS-GLOBAL AND LIVES NOWHERE THIS PROCESS CAN SEE IT.
# POLYMARKET_LIVE_TRADING is set in a systemd drop-in that reaches only the
# daemon's own process; the dashboard runs in a different process on its own
# timer. The only honest way to observe it is to ask the daemon's own
# environment whether the NAME is there.
#
# THAT FILE ALSO HOLDS POLYMARKET_PRIVATE_KEY. This is why the probe is split
# in three: a subprocess call for the pid, a byte read, and a PURE predicate
# that answers a yes/no question about a name. Nothing here returns, prints,
# logs or stores a value, and the tests assert it. Never widen these to
# return the entry, the blob, or a parsed dict "for debugging".
#
# The alternative -- reporting what the repo believes -- is not equivalent.
# On 2026-08-12 the daemon ran active/running with the credentials silently
# dropped by systemd, and an armed-looking daemon with no credentials is
# indistinguishable from a working one until an order is attempted.
GATE2_NAME = "POLYMARKET_LIVE_TRADING"


def environ_blob_has_name(blob, name):
    """True if `name` is a variable name in a NUL-separated environ blob.

    Matches on the full name up to '=' -- a prefix test would report
    POLYMARKET_LIVE as satisfying a probe for POLYMARKET_LIVE_TRADING.
    Returns a bool and nothing else, ever.
    """
    needle = name.encode("utf-8", "replace") + b"="
    for entry in (blob or b"").split(b"\0"):
        if entry.startswith(needle):
            return True
    return False


def _main_pid(unit):
    """The unit's MainPID, or None if it cannot be determined."""
    import subprocess

    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        )
        pid = int((r.stdout or "").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return pid or None  # systemd reports 0 for a stopped unit


def _read_environ(pid):
    """The raw environ blob for `pid`, or None if it cannot be read.

    Unreadable is the NORMAL case off the box and without privilege, and it
    must stay distinguishable from "the name is absent".
    """
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as fh:
            return fh.read()
    except OSError:
        return None


def gate2_state(unit="polyweather"):
    """"present" | "absent" | "unknown" -- never a boolean, never a value.

    "unknown" is not "absent". A probe that cannot run must not be rendered
    as a closed gate: that would report the safest-looking answer for the
    state we are least sure about.
    """
    pid = _main_pid(unit)
    if pid is None:
        return "unknown"
    blob = _read_environ(pid)
    if blob is None:
        return "unknown"
    return "present" if environ_blob_has_name(blob, GATE2_NAME) else "absent"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Probe gate 2 by environment variable NAME, never by value"
```

---

### Task 3: Window countdown

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `active_window(minute_of_day: int, windows: list[tuple]) -> dict | None` — same dict shape `scheduler.determine_window()` returns: keys `start_minute`, `end_minute`, `interval_min`, `mode`, `min_net_ev`, `description`.
  - `next_entry_boundary(minute_of_day: int, windows: list[tuple]) -> tuple[str, int] | None` — `("closes", minutes)` when inside an entry window, `("opens", minutes)` otherwise, `None` when no window in the list accepts entries.

Both take `windows` as a parameter rather than reading `config.SCHEDULE_WINDOWS`, so the tests pin behaviour against a small synthetic table that cannot drift when the real schedule is retuned. The renderer passes the real one.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k "window or boundary"`
Expected: FAIL with `AttributeError: module has no attribute 'active_window'`.

- [ ] **Step 3: Write the minimal implementation**

Insert into `deploy/generate_realmoney_dashboard.py` after the gate 2 block:

```python
# --- schedule windows --------------------------------------------------------
# `windows` is passed in rather than read from config so these stay pure and
# so the tests pin the ARITHMETIC against a fixed synthetic table. Pinning it
# against config.SCHEDULE_WINDOWS would mean every future schedule retune
# breaks tests that are not about the schedule -- and that table has already
# been retuned once (entries closed at 08:00, 2026-08-17).
#
# An ENTRY window is one with a non-None min_net_ev. That is the same
# discriminator scheduler.run_cycle() uses: a window with no EV bar has no
# bar to clear because it does not open positions.
MINUTES_PER_DAY = 24 * 60


def active_window(minute_of_day, windows):
    """The window covering `minute_of_day`, in scheduler.determine_window()'s
    dict shape, or None if the table has a gap there.

    Half-open [start, end) -- exactly as determine_window(), so 08:00 belongs
    to the window that STARTS at 08:00, not the one that ends there.
    """
    for (sh, sm, eh, em, interval, mode, min_ev, desc) in windows:
        start, end = sh * 60 + sm, eh * 60 + em
        if start <= minute_of_day < end:
            return {
                "start_minute": start,
                "end_minute": end,
                "interval_min": interval,
                "mode": mode,
                "min_net_ev": min_ev,
                "description": desc,
            }
    return None


def next_entry_boundary(minute_of_day, windows):
    """("closes", mins) inside an entry window, ("opens", mins) outside one,
    None if no window in the table accepts entries at all.

    The None case is real, not defensive: a region whose caps are all zero,
    or a schedule edited down to monitoring, has no next entry.
    """
    here = active_window(minute_of_day, windows)
    if here and here["min_net_ev"] is not None:
        return ("closes", here["end_minute"] - minute_of_day)

    starts = [sh * 60 + sm for (sh, sm, _eh, _em, _i, _m, min_ev, _d) in windows
              if min_ev is not None]
    if not starts:
        return None
    # Wrap: the next entry window may be tomorrow's.
    return ("opens", min((s - minute_of_day) % MINUTES_PER_DAY for s in starts))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Add pure schedule-window helpers with a wrap-aware entry countdown"
```

---

### Task 4: Bounds drift derivation

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `bounds_drift(config_min: int, config_max: int, discovered: list[int]) -> dict | None` — `None` when config and discovery agree or nothing was discovered; otherwise `{"config": (min, max), "discovered": (min, max), "note": str}`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k drift`
Expected: FAIL with `AttributeError: module has no attribute 'bounds_drift'`.

- [ ] **Step 3: Write the minimal implementation**

Insert into `deploy/generate_realmoney_dashboard.py` after the schedule block:

```python
# --- bounds drift ------------------------------------------------------------
def bounds_drift(config_min, config_max, discovered):
    """The BOUNDS DRIFT check, as page state rather than a journal line.

    ev_engine logs "BOUNDS DRIFT for <ICAO>: live event lists X-Y" when the
    exchange's bucket range disagrees with the registry's. Recovering it
    from the journal means knowing to grep for it; recomputing it from what
    discovery actually recorded puts it on the page.

    NOT AN ERROR, and the page must not present it as one. Bounds are a
    cross-check; the live token map is authoritative at trade time. A
    mismatch is noisy rather than dangerous -- but it is the first
    authoritative signal that a station's registry entry was researched
    wrong, which is exactly the open question on the European stations.

    Nothing discovered is not drift: that is the discovery section's story,
    and reporting it here too would double-count one fact as two problems.
    """
    buckets = [b for b in (discovered or []) if isinstance(b, int)]
    if not buckets:
        return None
    lo, hi = min(buckets), max(buckets)
    if (lo, hi) == (config_min, config_max):
        return None
    return {
        "config": (config_min, config_max),
        "discovered": (lo, hi),
        "note": f"registry lists {config_min}-{config_max}°C, discovery recorded {lo}-{hi}°C",
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Derive BOUNDS DRIFT as page state instead of a journal line"
```

---

### Task 5: Readiness ladder

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: `gate2_state()` (Task 2), `active_window()` / `next_entry_boundary()` (Task 3).
- Produces:
  - `Rung = dict` with keys `label: str`, `value: str`, `state: str` (one of `"ok"`, `"no"`, `"unknown"`), `why: str`.
  - `capacity_rung(icao: str, region: str, counts: dict) -> dict` — pure; `counts` carries `{"orders_today": int | None, "cap": int}`.
  - `readiness_rungs(icao: str, now_utc: datetime) -> list[dict]` — impure; reads `config`, `storage` and the gate 2 probe. Wrapped by its caller.
  - `render_readiness(icaos: list[str], now_utc: datetime, warnings: list) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
    """
    import storage

    monkeypatch.setattr(storage, "count_live_order_attempts",
                        lambda kind, since_iso, station_icaos=None: 0)
    monkeypatch.setattr(storage, "load_live_order_attempts", lambda limit=50: [])
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k "capacity or readiness"`
Expected: FAIL with `AttributeError: module has no attribute 'capacity_rung'`.

- [ ] **Step 3: Write the minimal implementation**

Insert into `deploy/generate_realmoney_dashboard.py` after the bounds-drift block:

```python
# --- readiness ladder --------------------------------------------------------
# THE RUNGS ARE IN THE ORDER THE REAL CODE APPLIES THEM, and each carries its
# actual value rather than a bare tick. A ladder of green ticks tells an
# operator nothing they can act on; "allowlisted, but maturity is an
# OVERRIDE" does.
#
# WHAT THIS LADDER DOES NOT CLAIM. It says an order COULD open, never that a
# given candidate WOULD. The per-bucket cap, the stop-out cooldown and the
# opposite-side lock are per-candidate and live inside evaluate_entry(); none
# of them is observable here because nothing persists an EntryDecision. That
# is stage 2. The page says so rather than letting the reader assume this
# ladder is the whole gate.


def _rung(label, value, state, why=""):
    return {"label": label, "value": value, "state": state, "why": why}


def capacity_rung(icao, region, counts):
    """Today's submitted entries against the cap that actually binds.

    executor.py:238 enforces REGION_LIVE_MAX_ORDERS_PER_DAY[region]. The
    process-global LIVE_MAX_ORDERS_PER_DAY is merely the value the "asia"
    entry aliases today, and would be the wrong number to show for any
    other region.

    UNKNOWN IS NOT ZERO. count_live_order_attempts() returns None when the
    count cannot be read, and the trading path treats that as "cannot
    authorise" -- a rate limit that fails open is not a rate limit. Rendering
    it as 0 would show maximum headroom for the state we know least about.
    """
    used, cap = counts.get("orders_today"), counts.get("cap")
    if used is None:
        return _rung(
            "Capacity", "unknown -- order count unreadable", "unknown",
            f"cap is {cap} entries/day for region {region!r}; the trading path "
            "treats an unreadable count as 'cannot authorise'",
        )
    state = "no" if (cap is not None and used >= cap) else "ok"
    return _rung(
        "Capacity", f"{used} of {cap} entries submitted today", state,
        f"REGION_LIVE_MAX_ORDERS_PER_DAY[{region!r}]",
    )


def readiness_rungs(icao, now_utc):
    """The full ladder for one station. Reads config, storage and /proc."""
    import config
    import storage

    rungs = []

    g2 = gate2_state()
    rungs.append({
        "present": _rung("Gate 2 (process)", f"{GATE2_NAME} set on the daemon", "ok",
                         "read by NAME from the daemon's environ; the value is never read"),
        "absent": _rung("Gate 2 (process)", f"{GATE2_NAME} NOT set", "no",
                        "no real order can be submitted by any station"),
        "unknown": _rung("Gate 2 (process)", "cannot be observed from this process", "unknown",
                         "the daemon's environ is unreadable here -- this is the normal "
                         "answer off the box, and is NOT the same as 'off'"),
    }[g2])

    mode = _read_mode_env().get("POLYWEATHER_MODE")
    rungs.append(
        _rung("Mode", mode or "unknown", "ok" if mode == "live" else
              ("unknown" if mode is None else "no"),
              "/etc/polyweather/mode.env -- HOST state, not repo state")
    )

    # live_mode_is_permitted() folds two independent conditions into one
    # boolean. They have opposite remedies, so both are shown.
    allowlisted = icao in getattr(config, "LIVE_TRADING_STATIONS", set())
    maturity = config.station_maturity(icao)
    permitted = config.live_mode_is_permitted(icao, "live")
    rungs.append(
        _rung("Gate 1 (station)", "permitted" if permitted else "refused",
              "ok" if permitted else "no",
              f"allowlisted: {'yes' if allowlisted else 'NO'} · "
              f"maturity: {maturity}")
    )

    override = getattr(config, "MATURITY_OVERRIDE", {}).get(icao)
    if override:
        forced, why = override
        rungs.append(
            _rung("Maturity provenance", f"{forced} BY OVERRIDE", "unknown",
                  f"config.MATURITY_OVERRIDE bypasses the measured criteria: {why}")
        )
    else:
        rungs.append(
            _rung("Maturity provenance", f"{maturity}, measured", "ok",
                  "derived from stored evidence, not overridden")
        )

    region = config.region_of(icao)
    authorised = config.region_authorises_live_orders(region)
    rungs.append(
        _rung("Region", f"{region}: {'authorised' if authorised else 'all caps zero'}",
              "ok" if authorised else "no",
              "REGION_LIVE_MAX_CONCURRENT_POSITIONS / _TOTAL_EXPOSURE_USD / _ORDERS_PER_DAY")
    )

    offset = config.current_utc_offset_hours(icao)
    local = now_utc.timestamp() + offset * 3600
    local_dt = datetime.fromtimestamp(local, tz=timezone.utc)
    mod = local_dt.hour * 60 + local_dt.minute
    win = active_window(mod, config.SCHEDULE_WINDOWS)
    boundary = next_entry_boundary(mod, config.SCHEDULE_WINDOWS)
    if win is None:
        rungs.append(_rung("Window", "no window covers this minute", "unknown",
                           "a gap in config.SCHEDULE_WINDOWS"))
    else:
        accepts = win["min_net_ev"] is not None
        detail = f"{win['description']} · local {local_dt:%H:%M} (UTC{offset:+d})"
        if accepts:
            detail += f" · EV bar {win['min_net_ev']:.0%} · scan {win['interval_min']}m"
        if boundary:
            what, mins = boundary
            detail += f" · entries {what} in {mins // 60}h{mins % 60:02d}m"
        rungs.append(
            _rung("Window", f"{win['mode']}{'' if accepts else ' (no entries)'}",
                  "ok" if accepts else "no", detail)
        )

    cap = getattr(config, "REGION_LIVE_MAX_ORDERS_PER_DAY", {}).get(region)
    day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    used = storage.count_live_order_attempts(
        "entry", day_start, station_icaos=config.stations_in_region(region)
    )
    rungs.append(capacity_rung(icao, region, {"orders_today": used, "cap": cap}))
    return rungs


def _read_mode_env(path="/etc/polyweather/mode.env"):
    """Parse the host's mode file. Holds no secrets -- values are safe to read.

    Returns {} when absent, which is what an un-deployed box looks like.
    """
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"')
    except OSError:
        return {}
    return out


def render_readiness(icaos, now_utc, warnings):
    """One card body per station, grouped under a region heading.

    Grouped, and never summed across groups. Both live stations are Asia
    today so the grouping is invisible -- that is the point. It is here so
    that arming a European station produces a new group instead of a silent
    cross-region mix.
    """
    import config

    if not icaos:
        return ("<div class='empty'>No station is in config.LIVE_TRADING_STATIONS &mdash; "
                "no real order can be submitted by anything.</div>")

    by_region = {}
    for icao in icaos:
        try:
            by_region.setdefault(config.region_of(icao), []).append(icao)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"region lookup failed for {icao}: {exc}")

    blocks = []
    for region in sorted(by_region):
        blocks.append(f"<p class='region'>{html.escape(region)}</p>")
        for icao in sorted(by_region[region]):
            try:
                rungs = readiness_rungs(icao, now_utc)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"readiness ladder failed for {icao}: {exc}")
                blocks.append(f"<div class='empty'>{html.escape(icao)}: ladder unavailable</div>")
                continue
            rows = "".join(
                f"<div class='rung {r['state']}'>"
                f"<span class='lab'>{html.escape(r['label'])}</span>"
                f"<span class='val'>{html.escape(r['value'])}</span>"
                + (f"<span class='why'>{html.escape(r['why'])}</span>" if r["why"] else "")
                + "</div>"
                for r in rungs
            )
            blocks.append(f"<h3 class='evstation'>{html.escape(icao)}</h3>{rows}")
    return "".join(blocks)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 29 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Add the readiness ladder: seven rungs, each carrying its own value"
```

---

### Task 6: EV detail, rendered unfiltered

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: `active_window()` (Task 3).
- Produces:
  - `ev_row_flags(row: dict, max_entry_price: float, edge_ceiling_for) -> str` — pure; the badge HTML for one EV row.
  - `render_ev(icaos: list[str], bar: float | None, warnings: list) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
    assert "no EV snapshot" in out.lower() or "never" in out.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k "ev_row or render_ev"`
Expected: FAIL with `AttributeError: module has no attribute 'ev_row_flags'`.

- [ ] **Step 3: Write the minimal implementation**

Insert into `deploy/generate_realmoney_dashboard.py` after the readiness block:

```python
# --- EV detail ---------------------------------------------------------------
# RENDERED UNFILTERED, unlike the region pages. Those show only rows clearing
# the entry screen, which is right when the question is "is there anything to
# take" and wrong when it is "how close did we get". On a real-money page the
# near-misses are the signal.
#
# EVERY THRESHOLD IS READ FROM CONFIG, NEVER RESTATED. The existing EV card
# has gone stale this way twice: once when EV_MIN_PRICE_SCREEN was hardcoded
# and phantom "+18,820% EV" rows ranked top of the table, and again when a
# flat MAX_PLAUSIBLE_RAW_EDGE was restated after the real ceiling became
# price-relative. Both are read through getattr with a fallback so this page
# still renders against a package checkout predating either constant.
def _ev_snapshot_path(icao):
    import config

    return config.DATA_DIR / f"ev_latest_{icao}.json"


def ev_row_flags(row, max_entry_price, edge_ceiling_for):
    """Badge HTML for one EV row, in the order evaluate_entry() vetoes:
    the entry-price ceiling first (a property of the market), then the
    price-relative edge ceiling (a property of the signal).
    """
    flags = ""
    price = row.get("market_price")
    if price is not None and price > max_entry_price:
        flags += " <span class='badge veto'>over price cap</span>"
    if abs(row.get("raw_edge") or 0.0) > edge_ceiling_for(price):
        flags += " <span class='badge veto'>veto zone</span>"
    if row.get("spread_source") == "fallback_default":
        flags += " <span class='badge fallback'>fallback est</span>"
    return flags


def render_ev(icaos, bar, warnings):
    import json

    import config

    max_entry_price = getattr(config, "MAX_ENTRY_PRICE", 1.0)
    edge_ceiling_for = getattr(
        config, "max_plausible_edge_for",
        lambda price: getattr(config, "MAX_PLAUSIBLE_RAW_EDGE", 0.25),
    )

    blocks = []
    for icao in sorted(icaos):
        path = _ev_snapshot_path(icao)
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
        except FileNotFoundError:
            blocks.append(
                f"<h3>{html.escape(icao)}</h3><div class='empty'>no EV snapshot yet &mdash; "
                "the engine writes one every time it computes, including when it finds "
                "nothing, so this means it has not run.</div>"
            )
            continue
        except (OSError, ValueError) as exc:
            warnings.append(f"EV snapshot unreadable for {icao}: {exc}")
            continue

        rows = []
        for r in sorted(snap.get("results", []),
                        key=lambda x: (x.get("net_ev_per_dollar") is None,
                                       -(x.get("net_ev_per_dollar") or 0))):
            ev = r.get("net_ev_per_dollar")
            price = r.get("market_price")
            over_bar = bar is not None and ev is not None and ev >= bar
            rows.append(
                "<tr>"
                f"<td class='mono'>{r.get('bucket_c')}&deg;C</td>"
                f"<td class='mono'>{html.escape(str(r.get('side', '')))}</td>"
                f"<td class='mono num'>{'&mdash;' if r.get('model_prob') is None else format(r['model_prob'], '.1%')}</td>"
                f"<td class='mono num'>{'&mdash;' if price is None else format(price, '.3f')}</td>"
                f"<td class='mono num'>{'&mdash;' if r.get('raw_edge') is None else format(r['raw_edge'], '+.1%')}</td>"
                f"<td class='mono num dim2'>{'&mdash;' if r.get('slippage_pct') is None else format(r['slippage_pct'], '.1%')}</td>"
                f"<td class='mono num {'pos' if over_bar else 'dim2'}'>"
                f"{'&mdash;' if ev is None else format(ev, '+.1%')}"
                f"{ev_row_flags(r, max_entry_price, edge_ceiling_for)}</td>"
                "</tr>"
            )
        gen_at = str(snap.get("generated_at", ""))[11:16]
        head = (f"<h3>{html.escape(icao)}</h3><p class='cap'>computed {html.escape(gen_at)} UTC "
                f"&middot; target {html.escape(str(snap.get('target_date')))} "
                f"&middot; {len(rows)} bucket/side row(s), unfiltered</p>")
        if not rows:
            blocks.append(head + "<div class='empty'>the engine computed and produced no rows.</div>")
            continue
        blocks.append(
            head + "<div class='tablewrap'><table class='ptable'>"
            "<thead><tr><th>Bucket</th><th>Side</th><th class='num'>Model p</th>"
            "<th class='num'>Mkt price</th><th class='num'>Raw edge</th>"
            "<th class='num'>Slip</th><th class='num'>Net EV/$</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    return "".join(blocks) or "<div class='empty'>no EV data for any real-money station.</div>"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 36 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Render real-money EV detail unfiltered, against the live EV bar"
```

---

### Task 7: Discovery state and the order audit trail

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: `bounds_drift()` (Task 4).
- Produces:
  - `discovery_state(icao: str, target_date, db_path=None) -> dict` — keys `buckets: list[int]`, `first_seen: str | None`, `with_book: int`, `drift: dict | None`.
  - `render_discovery(icaos: list[str], warnings: list) -> str`.
  - `render_orders(limit: int, warnings: list) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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


def test_render_orders_empty_trail(monkeypatch):
    gen = load_gen()
    import storage

    monkeypatch.setattr(storage, "load_live_order_attempts", lambda limit=50: [])
    monkeypatch.setattr(storage, "count_live_order_attempts",
                        lambda kind, since_iso, station_icaos=None: 0)
    out = gen.render_orders(limit=10, warnings=[])
    assert "no real order" in out.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k "discovery or orders"`
Expected: FAIL with `AttributeError: module has no attribute 'discovery_state'`.

- [ ] **Step 3: Write the minimal implementation**

Insert into `deploy/generate_realmoney_dashboard.py` after the EV block:

```python
# --- discovery ---------------------------------------------------------------
# STACKED UNDER THE SCHEDULE ON PURPOSE. A station sitting inside its primary
# window with zero discovered buckets currently looks exactly like a quiet
# night. Side by side with the window state, it reads as a fault.
#
# `db_path` is threaded through purely so this is testable against a throwaway
# database; production passes None and gets settings.MARKET_DATA_DB.
def discovery_state(icao, target_date, db_path=None):
    """What market discovery has actually recorded for one station/date.

    ABSENCE OF EVIDENCE, NOT EVIDENCE OF ABSENCE. market_tokens is populated
    by snapshot capture, so an empty result means "capture has recorded
    nothing", NOT "the market does not exist". The renderer says so.
    """
    import time

    import config
    import backtest.price_store as price_store

    rows = price_store.list_tokens(station_icao=icao, target_date=target_date, db_path=db_path)
    buckets = sorted({r["bucket_c"] for r in rows if isinstance(r.get("bucket_c"), int)})
    seen = [r["discovered_at"] for r in rows if r.get("discovered_at")]

    now = int(time.time())
    with_book = 0
    for r in rows:
        try:
            if price_store.get_price_at(r["token_id"], now, db_path=db_path):
                with_book += 1
        except Exception:  # noqa: BLE001 - one bad token must not cost the section
            continue

    drift = None
    try:
        station = config.get_station(icao)
        drift = bounds_drift(station.bucket_min_c, station.bucket_max_c, buckets)
    except Exception:  # noqa: BLE001
        drift = None

    return {
        "buckets": buckets,
        "first_seen": min(seen) if seen else None,
        "with_book": with_book,
        "drift": drift,
    }


def render_discovery(icaos, warnings):
    import config

    blocks = []
    for icao in sorted(icaos):
        try:
            target = config.local_today(icao)
            st = discovery_state(icao, str(target))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"discovery state failed for {icao}: {exc}")
            continue
        if not st["buckets"]:
            blocks.append(
                f"<h3>{html.escape(icao)}</h3><div class='empty'>capture has recorded no "
                f"buckets for {html.escape(str(target))}. This means snapshot capture has "
                "not seen this market &mdash; NOT that the market does not exist.</div>"
            )
            continue
        drift_html = ""
        if st["drift"]:
            drift_html = (f"<span class='badge fallback'>bounds drift</span> "
                          f"<span class='why'>{html.escape(st['drift']['note'])}</span>")
        blocks.append(
            f"<h3>{html.escape(icao)}</h3>"
            f"<div class='rung ok'><span class='lab'>Discovered</span>"
            f"<span class='val'>{len(st['buckets'])} bucket(s), "
            f"{min(st['buckets'])}-{max(st['buckets'])}&deg;C</span>"
            f"<span class='why'>first seen {html.escape(str(st['first_seen'])[:16])} UTC</span></div>"
            f"<div class='rung {'ok' if st['with_book'] else 'unknown'}'>"
            f"<span class='lab'>With a live book</span>"
            f"<span class='val'>{st['with_book']} of {len(st['buckets'])}</span>"
            "<span class='why'>a quote fresh enough for price_store's staleness guard</span></div>"
            + (f"<div class='rung unknown'><span class='lab'>Bounds</span>{drift_html}</div>"
               if drift_html else "")
        )
    return "".join(blocks) or "<div class='empty'>no discovery data.</div>"


# --- order audit trail -------------------------------------------------------
# THE ONLY RECORD OF A REFUSED ORDER ANYWHERE. An unfilled FOK deliberately
# writes no position -- a stored position with no shares behind it is the
# worst thing this codebase can produce -- so an order that was built,
# submitted and refused leaves no trace outside the process log and this
# table. Nothing rendered it before this page.
def render_orders(limit, warnings):
    import config
    import storage

    try:
        attempts = storage.load_live_order_attempts(limit=limit)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"order trail unreadable: {exc}")
        return "<div class='empty'>order trail unavailable</div>"

    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    counts = []
    for region in sorted(getattr(config, "REGION_LIVE_MAX_ORDERS_PER_DAY", {})):
        try:
            n = storage.count_live_order_attempts(
                "entry", day_start, station_icaos=config.stations_in_region(region))
        except Exception:  # noqa: BLE001
            n = None
        cap = config.REGION_LIVE_MAX_ORDERS_PER_DAY[region]
        counts.append(f"{html.escape(region)}: "
                      + ("<b>unknown</b>" if n is None else f"{n}")
                      + f" of {cap} today")
    head = f"<p class='cap'>entries submitted today &mdash; {' &middot; '.join(counts)}</p>"

    if not attempts:
        return head + ("<div class='empty'>no real order has been submitted yet &mdash; "
                       "this trail records every submission, including refused and "
                       "unfilled ones.</div>")

    rows = "".join(
        "<tr>"
        f"<td class='mono dim2'>{html.escape(str(a.get('ts', ''))[5:16].replace('T', ' '))}</td>"
        f"<td class='mono'>{html.escape(str(a.get('kind', '')))}</td>"
        f"<td class='mono'>{html.escape(str(a.get('station_icao', '')))}</td>"
        f"<td class='mono'>{a.get('bucket_c')}&deg;C {html.escape(str(a.get('side', '')))}</td>"
        f"<td class='mono num'>{'&mdash;' if a.get('notional_usd') is None else format(a['notional_usd'], '$,.2f')}</td>"
        f"<td class='mono num'>{'&mdash;' if a.get('size_shares') is None else format(a['size_shares'], ',.2f')}</td>"
        f"<td class='mono num'>{'&mdash;' if a.get('limit_price') is None else format(a['limit_price'], '.3f')}</td>"
        f"<td class='mono'>{html.escape(str(a.get('outcome', '')))}</td>"
        f"<td class='mono dim2' title='{html.escape(str(a.get('order_id') or ''))}'>"
        f"{html.escape(str(a.get('order_id') or '')[:10]) or '&mdash;'}</td>"
        f"<td class='mono dim2'>{html.escape(str(a.get('detail') or '')[:60])}</td>"
        "</tr>"
        for a in attempts
    )
    return head + (
        "<div class='tablewrap'><table class='ptable'>"
        "<thead><tr><th>When</th><th>Kind</th><th>Station</th><th>Bucket</th>"
        "<th class='num'>Notional</th><th class='num'>Shares</th><th class='num'>Limit</th>"
        "<th>Outcome</th><th>Order id</th><th>Detail</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q`
Expected: 41 passed.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Add discovery state and the live order audit trail"
```

---

### Task 8: Wire the sections into main() and smoke-test a full render

**Files:**
- Modify: `deploy/generate_realmoney_dashboard.py`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: `render_readiness()`, `render_ev()`, `render_discovery()`, `render_orders()`, `active_window()`, `next_entry_boundary()`.
- Produces: a complete `main()`. No new names.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
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
    assert "Readiness" in page  # the other sections still rendered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k full_render`
Expected: FAIL — `main()` builds no sections, so `assert "Readiness" in page` fails.

- [ ] **Step 3: Write the implementation**

Replace `main()` in `deploy/generate_realmoney_dashboard.py` with:

```python
def _section(sections, warnings, title, caption, builder):
    """Build one card, or record why it could not be built.

    EVERY SECTION DEGRADES INDEPENDENTLY. An unreadable EV snapshot must not
    cost the reader the readiness ladder -- that is the whole fail-soft
    contract this page inherits from its two siblings.
    """
    try:
        sections.append((title, caption, builder()))
    except Exception as exc:  # noqa: BLE001 - the page must render regardless
        warnings.append(f"{title}: {exc}")
        sections.append((title, caption, f"<div class='empty'>{html.escape(title)} unavailable</div>"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/var/www/html/realmoney.html",
                        help="path to write the rendered HTML page to")
    parser.add_argument("--orders", type=int, default=25,
                        help="how many rows of the order audit trail to show")
    args = parser.parse_args(argv)

    warnings = []
    sections = []
    now_utc = datetime.now(timezone.utc)

    try:
        import config

        icaos = sorted(getattr(config, "LIVE_TRADING_STATIONS", set()))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"config unreadable, no station list: {exc}")
        icaos = []

    # The EV bar is whatever the FIRST live station's active window sets. All
    # live stations share config.SCHEDULE_WINDOWS, but not the same local
    # clock, so this is a label for the section rather than a per-row gate --
    # the per-station bar is on that station's Window rung.
    bar = None
    try:
        import config

        if icaos:
            offset = config.current_utc_offset_hours(icaos[0])
            local = datetime.fromtimestamp(now_utc.timestamp() + offset * 3600, tz=timezone.utc)
            win = active_window(local.hour * 60 + local.minute, config.SCHEDULE_WINDOWS)
            bar = win["min_net_ev"] if win else None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"EV bar undetermined: {exc}")

    _section(
        sections, warnings, "Readiness",
        "Could a real order open right now. Rungs are in the order the executor applies them. "
        "This says an order COULD open, never that a given candidate WOULD &mdash; the "
        "per-candidate gates (per-bucket cap, stop-out cooldown, opposite-side lock) are "
        "<b>not recorded anywhere</b> and are not shown here.",
        lambda: render_readiness(icaos, now_utc, warnings),
    )
    _section(
        sections, warnings, "Edge and EV",
        "Every bucket/side the engine computed, unfiltered &mdash; including rows under the bar, "
        + (f"which is {bar:.0%} in the active window." if bar is not None
           else "with no entry window currently open."),
        lambda: render_ev(icaos, bar, warnings),
    )
    _section(
        sections, warnings, "Discovery",
        "What market discovery has recorded for today's target date. An empty result means "
        "capture has recorded nothing, not that the market is absent.",
        lambda: render_discovery(icaos, warnings),
    )
    _section(
        sections, warnings, "Order activity",
        "Every real submission, including refused and unfilled ones. This table is the only "
        "record of a refused order anywhere.",
        lambda: render_orders(args.orders, warnings),
    )

    page = render_page(sections, warnings)
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(page)
    except OSError as exc:
        print(f"[realmoney] could not write {args.out}: {exc}", file=sys.stderr)
        return 1
    print(f"real-money dashboard written to {args.out} ({len(page)} bytes)")
    return 0
```

- [ ] **Step 4: Run the whole suite**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: the full suite passes — 44 new tests plus the existing ones (767 as of 2026-08-25). No existing test may change.

- [ ] **Step 5: Commit**

```bash
git add deploy/generate_realmoney_dashboard.py weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Wire the real-money page sections together behind a fail-soft main()"
```

---

### Task 9: Deploy wiring

**Files:**
- Modify: `deploy/setup_dashboard.sh:12-14`
- Modify: `deploy/deploy_daemon.sh:66-69`
- Test: `weather-forecast/tests/test_realmoney_dashboard.py`

**Interfaces:**
- Consumes: the generator's filename.
- Produces: nothing importable.

- [ ] **Step 1: Write the failing test**

Append to `weather-forecast/tests/test_realmoney_dashboard.py`:

```python
# --- deploy wiring -----------------------------------------------------------
# deploy_daemon.sh refreshes the FROZEN COPIES in /usr/local/bin by name. A
# generator missing from that list is never refreshed by any deploy and rots
# there permanently -- the 2026-08-05 failure that motivated the block.

_REPO = pathlib.Path(__file__).resolve().parents[2]


def test_deploy_daemon_refreshes_the_realmoney_generator():
    script = (_REPO / "deploy" / "deploy_daemon.sh").read_text(encoding="utf-8")
    assert "generate_realmoney_dashboard.py" in script, (
        "deploy_daemon.sh must copy the real-money generator into /usr/local/bin; "
        "a generator it does not name is never refreshed by any deploy"
    )


def test_setup_dashboard_installs_the_realmoney_generator():
    script = (_REPO / "deploy" / "setup_dashboard.sh").read_text(encoding="utf-8")
    assert "generate_realmoney_dashboard.py" in script


def test_setup_dashboard_execstart_renders_the_realmoney_page():
    """A generator installed but never invoked renders nothing. This is the
    2026-08-25 gotcha, where europe.html silently never rendered."""
    script = (_REPO / "deploy" / "setup_dashboard.sh").read_text(encoding="utf-8")
    exec_line = next(l for l in script.splitlines() if l.startswith("ExecStart="))
    assert "generate_realmoney_dashboard.py" in exec_line
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd weather-forecast && python -m pytest tests/test_realmoney_dashboard.py -q -k deploy`
Expected: 3 failed — the string is in neither script.

- [ ] **Step 3: Make the changes**

In `deploy/setup_dashboard.sh`, after the existing `mv` of `generate_backtest_dashboard.py` (line 14):

```bash
sudo mv /home/ubuntu/generate_realmoney_dashboard.py /usr/local/bin/generate_realmoney_dashboard.py
sudo chmod 644 /usr/local/bin/generate_realmoney_dashboard.py
```

And extend the `ExecStart=` line with a third invocation, chained the same way:

```
&& $VENV_PY /usr/local/bin/generate_realmoney_dashboard.py
```

In `deploy/deploy_daemon.sh`, inside the existing `if [ -f /usr/local/bin/generate_dashboard.py ]` block (lines 66-69):

```bash
    sudo cp "$APP_DIR/deploy/generate_realmoney_dashboard.py" /usr/local/bin/generate_realmoney_dashboard.py
    sudo chmod 644 /usr/local/bin/generate_realmoney_dashboard.py
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd weather-forecast && python -m pytest tests -q`
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add deploy/setup_dashboard.sh deploy/deploy_daemon.sh weather-forecast/tests/test_realmoney_dashboard.py
git commit -m "Install and refresh the real-money generator on deploy"
```

---

## Manual verification before deploy

Not a task — run this by hand once Task 9 is committed, against a seeded scratch database, exactly as the column-split change was verified on 2026-08-26:

```bash
DASHBOARD_PKG_DIR=$PWD/weather-forecast python deploy/generate_realmoney_dashboard.py --out /tmp/realmoney.html
```

Confirm: exit 0; all four section headings present; the gate 2 rung reads "cannot be observed from this process" off the box (it must NOT read "not set"); no environment value appears anywhere in the output.

The deploy itself follows the dashboard-only shape recorded in memory `ec2-deployment`: pscp, `sudo install -m 644 -o ubuntu -g ubuntu` into `/usr/local/bin`, hand-edit the dashboard unit's `ExecStart` to add the third invocation, `systemctl start polyweather-dashboard.service`, then verify the md5 pair and the served page over HTTP.

---

## Self-review notes

- **Spec coverage:** shell §1 → Task 1; readiness §2 → Tasks 2, 5; EV §3 → Task 6; windows and discovery §4 → Tasks 3, 4, 7; order activity §5 → Task 7; error handling → Task 8's `_section`; testing → every task; deployment → Task 9. Spec §6 (stage 2) is deliberately unimplemented and out of scope per Global Constraints.
- **Naming consistency:** `gate2_state`, `active_window`, `next_entry_boundary`, `bounds_drift`, `capacity_rung`, `readiness_rungs`, `discovery_state`, `ev_row_flags`, `render_readiness`, `render_ev`, `render_discovery`, `render_orders`, `render_page`, `main` — each defined once and referenced under the same name everywhere.
- **No test may touch the real databases.** `storage._db()` and
  `price_store._connect()` create their sqlite file lazily, so an
  unisolated test writes into the operator's checkout. Every impure test
  takes the `isolated_stores` fixture; `discovery_state` is exercised only
  against an explicit `db_path` under `tmp_path`.
- **The three non-falsy defaults** from Global Constraints each have a dedicated test: `test_capacity_rung_unknown_is_never_zero`, `test_readiness_gate2_unknown_renders_unknown_not_off`, `test_discovery_state_empty_when_nothing_recorded` with its renderer assertion in `render_discovery`.
