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
import json
import os
import subprocess
import sys
import time
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
    """The unit's MainPID as a real int (0 included), or None if the probe
    itself could not be run (no systemctl, no privilege, timeout, malformed
    output).

    0 and None are NOT the same case. systemd reports MainPID=0 for a unit
    that is stopped or crash-looping -- that is an ANSWER ("not running"),
    not an inability to observe. Collapsing them cost gate2_state() its
    fourth state: a dead daemon used to render identically to "cannot be
    observed from this process".
    """
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = (r.stdout or "").strip()
    if not stdout:
        return None
    try:
        return int(stdout)
    except ValueError:
        return None


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
    """"present" | "absent" | "not_running" | "unknown" -- never a boolean,
    never a value.

    "unknown" is not "absent". A probe that cannot run must not be rendered
    as a closed gate: that would report the safest-looking answer for the
    state we are least sure about.

    "not_running" is not "unknown" either, in the other direction: MainPID=0
    is systemctl actually answering "this unit is not running", which on the
    production box means no real order can be submitted by any station --
    the same practical meaning as "absent", not a shrug.
    """
    pid = _main_pid(unit)
    if pid is None:
        return "unknown"
    if pid == 0:
        return "not_running"
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


def effective_windows(cfg):
    """The window table active_window()/next_entry_boundary() must be handed
    -- `cfg.SCHEDULE_WINDOWS` with `cfg.MARKET_OPEN_WINDOW` prepended when
    `cfg.ENABLE_MARKET_OPEN_WINDOW` is set.

    Mirrors scheduler.determine_window() (scheduler.py:172-177) exactly.
    Without this adapter these two pure helpers see the base table only, so
    flipping the flag opens entries in the scheduler while the page keeps
    reporting "closed (no entries)" at that time -- a false negative on the
    page's single central claim.
    """
    w = list(cfg.SCHEDULE_WINDOWS)
    if getattr(cfg, "ENABLE_MARKET_OPEN_WINDOW", False):
        w = [cfg.MARKET_OPEN_WINDOW] + w   # PREPEND -- must not be shadowed by the base window
    return w


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
        "not_running": _rung("Gate 2 (process)", "daemon not running", "no",
                             "systemctl reports MainPID=0 for this unit -- "
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
    windows = effective_windows(config)
    win = active_window(mod, windows)
    boundary = next_entry_boundary(mod, windows)
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
            verb = {"opens": "open", "closes": "close"}[what]
            detail += f" · entries {verb} in {mins // 60}h{mins % 60:02d}m"
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

    if not by_region:
        return ("<div class='empty'>region lookup failed for every station &mdash; "
                "see Render warnings.</div>")

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


# Rows below this net EV are dropped. The section was deliberately UNFILTERED
# at first, on the argument that near-misses are the signal when the question
# is "why didn't it trade" -- and that argument still holds for a row a few
# points under the bar. It does not hold for a row at -6299%.
#
# Those extremes are not opinions about the market, they are an artifact of
# estimating slippage against an unseeded far-tail book: net EV divides by
# price, so a 0.001 quote turns any model disagreement into a four-figure
# percentage. Twenty-two rows per station, half of them arithmetic noise,
# buries the handful a human should actually read.
#
# -10% is the floor because it is comfortably below any bar the schedule
# sets (the tightest is 15%) while still admitting genuine near-misses. The
# suppressed count is always reported -- a filtered table that does not say
# it is filtered is the same overclaim this page exists to avoid.
EV_DISPLAY_FLOOR = -0.10


def render_ev(icaos, bar, warnings):
    import config

    max_entry_price = getattr(config, "MAX_ENTRY_PRICE", 1.0)
    edge_ceiling_for = getattr(
        config, "max_plausible_edge_for",
        lambda price: getattr(config, "MAX_PLAUSIBLE_RAW_EDGE", 0.25),
    )

    blocks = []
    for icao in sorted(icaos):
        # Everything for this station -- including resolving its snapshot path --
        # is inside this guard, matching render_readiness: one bad station costs
        # you that station, not the whole section. FileNotFoundError and
        # (OSError, ValueError) below are more specific, expected cases and
        # `continue` out before ever reaching this outer handler.
        try:
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
            suppressed = 0
            for r in sorted(snap.get("results", []),
                            key=lambda x: (x.get("net_ev_per_dollar") is None,
                                           -(x.get("net_ev_per_dollar") or 0))):
                ev = r.get("net_ev_per_dollar")
                # An unpriced row (ev is None) is NOT suppressed: "no quote" is
                # a different fact from "deeply negative", and it is one worth
                # seeing on a page about why nothing traded.
                if ev is not None and ev < EV_DISPLAY_FLOOR:
                    suppressed += 1
                    continue
                price = r.get("market_price")
                bucket_c = r.get("bucket_c")
                # `bar` is icaos[0]'s own local-window bar (see main()), applied
                # uniformly to every station's rows here. Correct today because
                # every live station shares one clock; it stops being correct
                # the moment a live station sits in a different timezone with a
                # different active window at render time.
                over_bar = bar is not None and ev is not None and ev >= bar
                rows.append(
                    "<tr>"
                    f"<td class='mono'>{'&mdash;' if bucket_c is None else str(bucket_c) + '&deg;C'}</td>"
                    f"<td class='mono'>{html.escape(str(r.get('side', '')))}</td>"
                    f"<td class='mono num'>{'&mdash;' if r.get('model_prob') is None else format(r['model_prob'], '.1%')}</td>"
                    f"<td class='mono num'>{'&mdash;' if price is None else format(price, '.3f')}</td>"
                    f"<td class='mono num'>{'&mdash;' if r.get('raw_edge') is None else format(r['raw_edge'], '+.1%')}</td>"
                    f"<td class='mono num {'pos' if over_bar else 'dim2'}'>"
                    f"{'&mdash;' if ev is None else format(ev, '+.1%')}"
                    f"{ev_row_flags(r, max_entry_price, edge_ceiling_for)}</td>"
                    "</tr>"
                )
            gen_at = str(snap.get("generated_at", ""))[11:16]
            target_date = snap.get("target_date")
            target_html = "&mdash;" if target_date is None else html.escape(str(target_date))
            head = (f"<h3>{html.escape(icao)}</h3><p class='cap'>computed {html.escape(gen_at)} UTC "
                    f"&middot; target {target_html} "
                    f"&middot; {len(rows)} bucket/side row(s)"
                    + (f" &middot; {suppressed} below {EV_DISPLAY_FLOOR:.0%} net EV suppressed"
                       if suppressed else "")
                    + "</p>")
            if not rows:
                blocks.append(head + "<div class='empty'>the engine computed and produced no rows.</div>")
                continue
            blocks.append(
                head + "<div class='tablewrap'><table class='ptable'>"
                "<thead><tr><th>Bucket</th><th>Side</th><th class='num'>Model p</th>"
                "<th class='num'>Mkt price</th><th class='num'>Raw edge</th>"
                "<th class='num'>Net EV/$</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"EV section failed for {icao}: {exc}")
            continue
    return "".join(blocks) or "<div class='empty'>no EV data for any real-money station.</div>"


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

    `discovered_at` IS A LAST-SEEN TIMESTAMP, NOT A FIRST-SEEN ONE.
    price_store.upsert_token is INSERT OR REPLACE and both writers stamp a
    fresh timestamp on every pass, so the column always holds the most
    recent capture. max(seen) -- not min(seen) -- is therefore the only
    reading that lets a FROZEN value mean what it should: capture has
    stopped updating this station, the one signal that capture has died.
    """
    import config
    import backtest.price_store as price_store

    rows = price_store.list_tokens(station_icao=icao, target_date=target_date, db_path=db_path)
    buckets = sorted({r["bucket_c"] for r in rows if isinstance(r.get("bucket_c"), int)})
    seen = [r["discovered_at"] for r in rows if r.get("discovered_at")]

    # market_tokens holds one row per (bucket, side) -- both ev_engine.py and
    # snapshot_collector.py write yes AND no. Count DISTINCT BUCKETS with a
    # quote, not rows with a quote, or two quoted sides of one bucket double
    # it (reproduced directly: 3 buckets x 2 sides, all quoted, rendered
    # "6 of 3").
    now = int(time.time())
    books = set()
    for r in rows:
        try:
            if price_store.get_price_at(r["token_id"], now, db_path=db_path):
                books.add(r["bucket_c"])
        except Exception:  # noqa: BLE001 - one bad token must not cost the section
            continue
    with_book = len(books & set(buckets))

    drift = None
    try:
        station = config.get_station(icao)
        drift = bounds_drift(station.bucket_min_c, station.bucket_max_c, buckets)
    except Exception:  # noqa: BLE001
        drift = None

    return {
        "buckets": buckets,
        "last_seen": max(seen) if seen else None,
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
            f"<span class='why'>last recorded by capture {html.escape(str(st['last_seen'])[:16])} UTC</span></div>"
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
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"order count unreadable for region {region}: {exc}")
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
        f"<td class='mono'>{'&mdash;' if a.get('bucket_c') is None else str(a['bucket_c']) + '&deg;C'} {html.escape(str(a.get('side', '')))}</td>"
        f"<td class='mono num'>{'&mdash;' if a.get('notional_usd') is None else '$' + format(a['notional_usd'], ',.2f')}</td>"
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
            win = active_window(local.hour * 60 + local.minute, effective_windows(config))
            bar = win["min_net_ev"] if win else None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"EV bar undetermined: {exc}")

    # The dynamic half of this caption is built here, OUTSIDE _section's try,
    # because it is evaluated as a call argument before _section is ever
    # entered -- a format failure on `bar` would otherwise kill main() through
    # the very mechanism meant to prevent that. Guarded and named to the one
    # station it actually describes: `bar` comes from icaos[0]'s own local
    # window, not a threshold shared by every live station.
    ev_caption = ("Every bucket/side the engine computed, unfiltered &mdash; including rows "
                  "under the bar, with no entry window currently open.")
    if bar is not None:
        try:
            ev_caption = (
                "Every bucket/side the engine computed, unfiltered &mdash; including rows "
                f"under the bar, which is {bar:.0%} &mdash; {html.escape(icaos[0])}'s active-window "
                "bar. Each station's own bar is on its Window rung, on the Readiness card above."
            )
        except (TypeError, ValueError) as exc:
            warnings.append(f"EV bar caption formatting failed: {exc}")

    _section(
        sections, warnings, "Readiness",
        "Could a real order open right now. Rungs are in the order the executor applies them. "
        "This says an order COULD open, never that a given candidate WOULD &mdash; the "
        "per-candidate gates (per-bucket cap, stop-out cooldown, opposite-side lock) are "
        "<b>not recorded anywhere</b> and are not shown here.",
        lambda: render_readiness(icaos, now_utc, warnings),
    )
    # Discovery is rendered right after Readiness, and BEFORE Edge and EV, so
    # it sits beside the Window rung it is meant to be read against. Spec §4:
    # "Stacking is the whole point. A station sitting inside its primary
    # window with zero discovered buckets currently looks exactly like a
    # quiet night. Here it reads as a fault."
    _section(
        sections, warnings, "Discovery",
        "What market discovery has recorded for today's target date. An empty result means "
        "capture has recorded nothing, not that the market is absent.",
        lambda: render_discovery(icaos, warnings),
    )
    _section(
        sections, warnings, "Edge and EV",
        ev_caption,
        lambda: render_ev(icaos, bar, warnings),
    )
    _section(
        sections, warnings, "Order activity",
        "Every real submission, including refused and unfilled ones. This table is the only "
        "record of a refused order anywhere.",
        lambda: render_orders(args.orders, warnings),
    )

    try:
        page = render_page(sections, warnings)
    except Exception as exc:  # noqa: BLE001 - nothing may raise out of main()
        warnings.append(f"render_page failed: {exc}")
        items = "".join(f"<li>{html.escape(str(w))}</li>" for w in warnings)
        page = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<title>polyweather -- real money</title></head><body>"
            "<h1>Real-money stations</h1>"
            "<p>The page renderer itself failed; only the warnings below are known.</p>"
            f"<ul>{items}</ul></body></html>"
        )

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
