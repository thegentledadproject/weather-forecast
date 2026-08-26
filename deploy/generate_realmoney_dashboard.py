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
