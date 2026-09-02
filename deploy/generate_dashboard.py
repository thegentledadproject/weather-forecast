#!/usr/bin/env python3
"""Render one region's polyweather status dashboard.

Runs on the EC2 instance from a systemd timer (every 5 min), once per
registered region -- see --region below. Reads only: systemd unit state,
the journal tail, and the package's own storage layer. Every data read is
fail-soft: a failure shows up ON the page rather than killing the render.
"""
import argparse
import calendar as calmod
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# DASHBOARD_PKG_DIR mirrors DASHBOARD_OUT_DIR below: unset, it's the real
# EC2 path; set, it lets this script be pointed at a local checkout for
# testing off the instance (this file has no unit tests of its own -- this
# env var and DASHBOARD_OUT_DIR are how it gets exercised end to end).
PKG = os.environ.get("DASHBOARD_PKG_DIR", "/home/ubuntu/weather-forecast/weather-forecast")

sys.path.insert(0, PKG)
os.chdir(PKG)

import config  # noqa: E402 -- needed up front to validate --region and scope every station loop
import bucket_axis  # noqa: E402

# --- region selection ---------------------------------------------------------
# One page per region: Asia and Europe draw on separate capital pools and
# separate live blast radius, with no shared thesis, so a mixed page
# misrepresents both. Defaults to "asia" so an un-updated invocation (the
# pre-existing systemd ExecStart, a stale cron entry, a bare command line)
# keeps rendering exactly the page it always has, at exactly the URL it
# always has.
_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument("--region", default="asia",
                      help="Which registered region to render (default: asia).")
args = _parser.parse_args()
region = args.region
if region not in config.REGION_BANKROLL_USD:
    sys.exit(
        f"generate_dashboard.py: unknown region {region!r} -- known regions: "
        f"{sorted(config.REGION_BANKROLL_USD)} (see config.REGION_BANKROLL_USD)"
    )

# Every config.STATIONS iteration in this file is routed through one of
# these two instead of the raw registry, so nothing belonging to another
# region -- a station, a position, an EV snapshot -- can leak onto this page.
REGION_STATIONS = config.stations_in_region(region)         # ordered list of ICAOs
REGION_STATIONS_SET = set(REGION_STATIONS)                  # O(1) membership for position filtering

# asia keeps the original hardcoded path -- the page's existing public URL --
# so nothing that already links or bookmarks it breaks. Any other region
# gets its own file alongside it. DASHBOARD_OUT_DIR exists purely so this
# script is testable off the EC2 box (the real /var/www/html doesn't exist
# locally); left unset it reproduces production exactly.
OUT_DIR = os.environ.get("DASHBOARD_OUT_DIR", "/var/www/html")
OUT = os.path.join(OUT_DIR, "index.html" if region == "asia" else f"{region}.html")

warnings = []


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip()
    except Exception as exc:  # noqa: BLE001 - dashboard must render regardless
        warnings.append(f"command failed ({cmd.split()[0]}): {exc}")
        return ""


# --- service facts -----------------------------------------------------------
props = {}
for line in sh("systemctl show polyweather -p ActiveState,ActiveEnterTimestamp,NRestarts").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        props[k] = v
active_state = props.get("ActiveState", "unknown")
restarts = props.get("NRestarts", "?")

deploy_epoch_ms = 0
ts = props.get("ActiveEnterTimestamp", "")
if ts:
    try:  # format: "Fri 2026-07-31 11:49:09 UTC"
        dt = datetime.strptime(" ".join(ts.split()[1:3]), "%Y-%m-%d %H:%M:%S")
        deploy_epoch_ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        warnings.append(f"could not parse ActiveEnterTimestamp: {ts}")

journal = sh("journalctl -u polyweather -n 14 --no-pager --output=cat") or "(journal unavailable)"

# --- positions + P&L via the package's own storage layer ---------------------
open_n = closed_n = None
pnl_display = "&mdash;"
pnl_note = "closed trades only"
try:
    import storage  # noqa: E402

    # limit=1000 matches paper_trading_report.load_paper_history(), which is what
    # the headline P&L tile and the station table are computed from. Left at the
    # storage default of 100/station these lists would start silently dropping
    # the oldest closed trades before those aggregates did -- invisible in the
    # "15 most recent" table, but the daily P&L calendar below would lose whole
    # early days off the front of the grid while the tile still counted them.
    #
    # load_open_positions() takes no region -- it reads across the whole
    # registry -- so the open lists are filtered to this region's stations
    # right after the read, before anything downstream aggregates over them.
    open_positions = [p for p in storage.load_open_positions(is_paper=True)
                       if p.station_icao in REGION_STATIONS_SET]
    closed_positions = []
    for icao in REGION_STATIONS:
        closed_positions.extend(storage.load_position_history(icao, limit=1000, is_paper=True))
    closed_positions.sort(key=lambda p: p.exit_time or "", reverse=True)
    open_n, closed_n = len(open_positions), len(closed_positions)

    # REAL-MONEY TRACK, loaded separately and never summed into the paper
    # numbers. is_paper stays True for every execution_mode except "live"
    # (see models.Position), so is_paper=False is exactly the set of
    # positions that spent real capital. Before this, every read on this
    # page was is_paper=True -- a real position could be open on WSSS and
    # the page would show nothing at all, while the header still claimed
    # "paper mode -- no live orders".
    live_open = [p for p in storage.load_open_positions(is_paper=False)
                 if p.station_icao in REGION_STATIONS_SET]
    live_closed = []
    for icao in REGION_STATIONS:
        live_closed.extend(storage.load_position_history(icao, limit=1000, is_paper=False))
    live_closed.sort(key=lambda p: p.exit_time or "", reverse=True)
except Exception as exc:  # noqa: BLE001
    open_positions, closed_positions = [], []
    live_open, live_closed = [], []
    warnings.append(f"position read failed: {exc}")

# Registry size, computed once and reused everywhere the page used to say
# "both stations" or hardcode "WSSS & WMKK" -- the count (and every string
# built from it) now tracks this region's slice of config.STATIONS instead
# of silently going stale the next time a station is added or retired.
station_count = len(REGION_STATIONS)

station_summaries = {}  # icao -> summarize_paper_performance dict; reused by the station table
if closed_n:
    try:
        import paper_trading_report as ptr  # noqa: E402

        pnl_usd = 0.0
        staked_usd = 0.0
        for icao in REGION_STATIONS:
            summary = ptr.summarize_paper_performance(icao)
            if isinstance(summary, dict):
                station_summaries[icao] = summary
                pnl_usd += float(summary.get("total_pnl_usd") or 0.0)
                staked_usd += float(summary.get("total_staked_usd") or 0.0)
        # Still dollar-weighted -- what the bankroll actually experienced, not
        # the per-trade percent sum that overweights $1 lottery tickets. The
        # WORD is gone from the page, not the arithmetic: "+2.1% on $2,224
        # staked" already says a big trade counted for more than a small one,
        # and says it to a reader who doesn't know the term.
        pnl_display = f"{'-' if pnl_usd < 0 else '+'}${abs(pnl_usd):,.2f}"
        if staked_usd:
            pnl_note = f"{pnl_usd / staked_usd:+.1%} on ${staked_usd:,.0f} staked"
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"P&L summary failed: {exc}")

# --- real-money track --------------------------------------------------------
# Which stations would ACTUALLY be permitted to send a real order right now.
# Asked of config.live_mode_is_permitted() rather than re-reading
# LIVE_TRADING_STATIONS here: the allowlist alone is not the gate (live also
# requires maturity), and a dashboard that reproduces half of a safety rule
# is a dashboard that will eventually disagree with the executor.
def _live_armed_stations():
    armed = []
    for icao in REGION_STATIONS:
        try:
            if config.live_mode_is_permitted(icao, "live"):
                armed.append(icao)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"live-permission check failed for {icao}: {exc}")
    return sorted(armed)


try:
    live_stations = _live_armed_stations()
except Exception as exc:  # noqa: BLE001
    live_stations = []
    warnings.append(f"live station check failed: {exc}")

live_at_risk = sum(float(p.size_usd or 0.0) for p in live_open)
# Region-scoped live blast radius. These used to read the flat LIVE_MAX_*
# constants directly, which was fine back when there was only one region --
# now that Europe carries its own (zero) blast radius, the card must read
# THIS region's ceiling or it would show Asia's caps on Europe's page.
# getattr-first, same fail-soft convention as the flat constants used to
# get, so a package checkout that predates the REGION_LIVE_MAX_* dicts still
# renders instead of crashing this card.
_region_live_cap_usd = getattr(config, "REGION_LIVE_MAX_TOTAL_EXPOSURE_USD", {})
_region_live_cap_n = getattr(config, "REGION_LIVE_MAX_CONCURRENT_POSITIONS", {})
_region_live_orders_per_day = getattr(config, "REGION_LIVE_MAX_ORDERS_PER_DAY", {})
live_cap_usd = float(_region_live_cap_usd.get(region, 0.0) or 0.0)
live_cap_n = _region_live_cap_n.get(region)
live_orders_per_day_cap = _region_live_orders_per_day.get(region)
# Per-entry size is still a single flat notional, not region-keyed (see
# config.LIVE_TRADE_SIZE_USD) -- it's what one order asks for, not a ceiling.
live_size_usd = float(getattr(config, "LIVE_TRADE_SIZE_USD", 0.0) or 0.0)
# All three blast-radius backstops at zero (Europe today) means no station in
# this region can ever pass config.live_mode_is_permitted(), independent of
# LIVE_TRADING_STATIONS -- the real-money card says so explicitly below
# rather than just rendering an empty "no station armed" line that reads the
# same as "temporarily paper" instead of "structurally paper".
live_region_caps_all_zero = not live_cap_usd and not live_cap_n and not live_orders_per_day_cap

# Realized real-money P&L, computed by summarize_positions() -- the same
# arithmetic paper_trading_report uses for the paper track, so the two
# tracks are comparable without ever being added together.
live_pnl_usd = 0.0
live_staked_usd = 0.0
if live_closed:
    try:
        import paper_trading_report as ptr  # noqa: E402,F811

        by_station = {}
        for p in live_closed:
            by_station.setdefault(p.station_icao, []).append(p)
        for _icao, _ps in by_station.items():
            s = ptr.summarize_positions(_ps)
            if isinstance(s, dict):
                live_pnl_usd += float(s.get("total_pnl_usd") or 0.0)
                live_staked_usd += float(s.get("total_staked_usd") or 0.0)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"real-money P&L summary failed: {exc}")

# --- live dependency probes --------------------------------------------------
# Actively probed from this instance on every regeneration (5 min), so the
# page answers "is each upstream feed reachable RIGHT NOW", not "did the
# daemon happen to log an error recently."
def _probe(name, url, ok_when=None, timeout=6):
    import requests  # deferred so a missing requests only breaks probes, not the page

    t0 = time.time()
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "polyweather-status/1.0"})
        ms = int((time.time() - t0) * 1000)
        state = "ok" if (ok_when(r) if ok_when else r.status_code == 200) else "degraded"
        return {"name": name, "state": state, "detail": f"HTTP {r.status_code} &middot; {ms} ms"}
    except Exception as exc:  # noqa: BLE001 - a dead probe is itself the data point
        return {"name": name, "state": "down", "detail": html.escape(type(exc).__name__)}


# WWIS (worldweather.wmo.int) numeric city ids for stations that use the
# generic "wwis" official client. config.py deliberately only stores the
# city NAME (wwis_city_name) -- that's all the live client needs -- so this
# small lookup exists purely to let the live-feed probe below ping one real
# WWIS city endpoint instead of the old hardcoded "city 82" (Kuala Lumpur),
# which isn't even a wwis-client station (WMKK uses "met_malaysia"). Ids
# verified live against worldweather.wmo.int 2026-08-05 (see the Asia
# expansion design doc); a station whose city isn't listed here just means
# the probe below falls through to the next wwis-client station instead.
_WWIS_CITY_ID_BY_NAME = {
    "Tokyo": 183, "Seoul": 231, "Busan": 336, "Hong Kong": 1,
    "Metro Manila": 92, "Shanghai": 240, "Beijing": 237,
    "Guangzhou": 241, "Shenzhen": 1854, "Karachi": 892,
    "Kuala Lumpur": 82, "Singapore": 234,
}


def _run_probes():
    try:
        import market_discovery

        # Cheap representative sample, not all 13 stations: one station per
        # distinct utc_offset_hours group is enough to answer "is each
        # timezone's data path alive right now" without tripling probe
        # latency for stations that share an offset -- and therefore share
        # the same upstream failure modes for the offset-generic checks
        # below (Open-Meteo, Wunderground, CLOB don't care WHICH station in
        # a timezone group you ask, only the Gamma per-event check does).
        offset_reps = {}
        for icao in REGION_STATIONS:
            st = config.STATIONS[icao]
            offset_reps.setdefault(st.utc_offset_hours, st)
        rep_stations = list(offset_reps.values())
        primary = rep_stations[0]  # arbitrary anchor for the station-agnostic probes below

        om = (f"?latitude={primary.lat}&longitude={primary.lon}"
              "&daily=temperature_2m_max&timezone=auto&forecast_days=1")

        probes = [
            _probe("Open-Meteo ECMWF", config.OPEN_METEO_ECMWF_URL + om),
            # GFS is probed for the same reason as ECMWF, and its absence here
            # was a real hole until 2026-08-16: calibration.blend_central_estimate
            # takes a flat mean of whatever forecasts the cycle collected, so a
            # GFS outage does not fail anything -- it silently reweights the
            # central estimate onto the surviving sources and keeps trading.
            # Both halves of that mean need a probe, or the panel can read
            # "all upstream feeds reachable" while half the forecast term is gone.
            _probe("Open-Meteo GFS", config.OPEN_METEO_GFS_URL + om),
            _probe("Open-Meteo ensemble", config.OPEN_METEO_ENSEMBLE_URL + om + "&models=ecmwf_ifs025"),
            _probe("Wunderground", primary.wunderground_history_url, timeout=8),
            # CLOB: reachability only -- a garbage token id SHOULD 404/400; any
            # HTTP answer < 500 proves the trading API is up and talking.
            _probe("CLOB price API", "https://clob.polymarket.com/price?token_id=1&side=buy",
                   ok_when=lambda r: r.status_code < 500),
        ]

        # NEA is Singapore's own government feed, tied to whichever station
        # actually uses the "nea" official client (WSSS today) -- probe it
        # only if such a station is registered IN THIS REGION, instead of
        # assuming WSSS (Europe has no "nea" station at all).
        nea_station = next(
            (config.STATIONS[icao] for icao in REGION_STATIONS
             if config.STATIONS[icao].official_client_key == "nea"),
            None,
        )
        if nea_station is not None:
            probes.append(_probe("NEA (data.gov.sg)", "https://api.data.gov.sg/v1/environment/24-hour-weather-forecast"))

        # At most ONE WWIS probe -- most of the 13-station registry uses the
        # "wwis" client, and pinging all of them on every 5-minute
        # regeneration would be neither cheap nor informative (one WWIS
        # outage looks like every WWIS station going down at once anyway).
        wwis_station = next(
            (config.STATIONS[icao] for icao in REGION_STATIONS
             if config.STATIONS[icao].official_client_key == "wwis"
             and config.STATIONS[icao].wwis_city_name in _WWIS_CITY_ID_BY_NAME),
            None,
        )
        if wwis_station is not None:
            city_id = _WWIS_CITY_ID_BY_NAME[wwis_station.wwis_city_name]
            probes.append(_probe(f"WWIS ({wwis_station.wwis_city_name})",
                                  f"https://worldweather.wmo.int/en/json/{city_id}_en.json"))

        # Gamma event-existence probe per representative station -- this is
        # the one check that's genuinely per-timezone-group: a station's
        # local calendar date (hence its event slug) can already read
        # "tomorrow" in one offset group while another is still "today",
        # which is exactly the bug class config.local_today() exists to
        # prevent (see config.py's local_today docstring).
        # Gamma: 200 with a non-empty list means today's event is actually
        # listed; 200-but-empty is degraded (API up, market not found).
        for st in rep_stations:
            local_date = config.local_today(st)
            slug = market_discovery.build_event_slug(st, local_date)
            probes.append(_probe(
                f"Gamma API ({st.icao} event)",
                f"https://gamma-api.polymarket.com/events?slug={slug}",
                ok_when=lambda r: r.status_code == 200 and r.json(),
            ))

        return probes
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"probes failed to run: {exc}")
        return []


_PROBE_STATE_LABELS = {"ok": "ok", "degraded": "degraded", "down": "DOWN"}
probes = _run_probes()
if probes:
    chips = "".join(
        f"<div class='probe p-{p['state']}'><span class='pdot'></span>"
        f"<span class='pname'>{html.escape(p['name'])}</span>"
        f"<span class='pdetail'>{_PROBE_STATE_LABELS[p['state']]} &middot; {p['detail']}</span></div>"
        for p in probes
    )
    n_down = sum(1 for p in probes if p["state"] != "ok")
    probes_cap = ("all upstream feeds reachable" if n_down == 0
                  else f"{n_down} of {len(probes)} feeds not fully OK")
    probes_html = f"<div class='probes'>{chips}</div>"
else:
    probes_cap = "probe run failed -- see warnings"
    probes_html = "<div class='empty'>probes unavailable</div>"

# --- latest EV snapshot ------------------------------------------------------
EV_ENTRY_SCREEN = 0.15  # matches scheduler's min_net_ev entry screen


def _load_ev_snapshots():
    snaps = []
    for icao in REGION_STATIONS:
        p = config.DATA_DIR / f"ev_latest_{icao}.json"
        try:
            if p.exists():
                with open(p, encoding="utf-8") as fh:
                    snaps.append(json.load(fh))
        except (OSError, ValueError) as exc:
            warnings.append(f"EV snapshot unreadable for {icao}: {exc}")
    return snaps


# Mirrors ev_engine.best_opportunities(): the trading screen excludes
# sub-EV_MIN_PRICE_SCREEN buckets because net EV divides raw_edge by price,
# so a near-zero price turns any stale-model disagreement into a
# "+18,820% EV" artifact. The dashboard used to skip this guard and rank
# exactly those phantoms at the top of the table. getattr fallback keeps
# the page rendering against a package checkout that predates the constant.
try:
    EV_MIN_PRICE = getattr(config, "EV_MIN_PRICE_SCREEN", 0.03)
    # The edge ceiling is PRICE-RELATIVE (config.max_plausible_edge_for) and
    # there is now also a hard entry-price cap. This used to be a single flat
    # constant here, which quietly went stale the moment those gates landed:
    # rows above the price cap, and rows over the headroom ceiling but under
    # the flat 0.25, rendered as clean opportunities the entry path would
    # refuse. Same drift the min-price screen above had (46db643) -- both are
    # now read from config rather than restated.
    MAX_ENTRY_PRICE = getattr(config, "MAX_ENTRY_PRICE", 1.0)
    _edge_ceiling_for = getattr(
        config, "max_plausible_edge_for",
        lambda price: getattr(config, "MAX_PLAUSIBLE_RAW_EDGE", 0.25),
    )
except NameError:  # config import itself failed above; page must still render
    EV_MIN_PRICE, MAX_ENTRY_PRICE = 0.03, 1.0
    _edge_ceiling_for = lambda price: 0.25  # noqa: E731


def _ev_rows(snap):
    rows = []
    priced = [r for r in snap.get("results", []) if r.get("net_ev_per_dollar") is not None]
    unpriced = len(snap.get("results", [])) - len(priced)
    suppressed = 0
    station = config.STATIONS.get(snap.get("station_icao"))
    axis = bucket_axis.for_station(station)
    # Only rows that clear the entry screen are shown -- the sub-screen rows
    # are noise on a page whose question is "is there anything to take", and
    # at 13 stations they dominated the table.
    for r in sorted(priced, key=lambda x: x["net_ev_per_dollar"], reverse=True):
        ev = r["net_ev_per_dollar"]
        if ev < EV_ENTRY_SCREEN:
            continue
        if (r.get("market_price") or 0) < EV_MIN_PRICE:
            suppressed += 1
            continue
        # A row the entry path would refuse is still worth SEEING (it often
        # means the model, not the market, is wrong for that station) -- but
        # it must not read as an opportunity. Badge instead of hide.
        #
        # Both pre-sizing vetoes are reflected, in the order evaluate_entry
        # runs them: the entry-price ceiling first (a property of the market),
        # then the price-relative edge ceiling (a property of the signal).
        flags = ""
        cls = "pos"
        price = r.get("market_price")
        if price is not None and price > MAX_ENTRY_PRICE:
            flags += " <span class='badge veto'>over price cap</span>"
            cls = ""
        if abs(r["raw_edge"]) > _edge_ceiling_for(price):
            flags += " <span class='badge veto'>veto zone</span>"
            cls = ""
        if r.get("spread_source") == "fallback_default":
            flags += " <span class='badge fallback'>fallback est</span>"
        bucket_min = getattr(station, "bucket_min_c", 25)
        bucket_max = getattr(station, "bucket_max_c", 35)
        # Entity form, not the literal degree sign: matches the &deg;C this
        # replaces everywhere else on this page.
        bucket_label = axis.label(r["bucket_c"], bucket_min, bucket_max).replace("°", "&deg;")
        rows.append(
            "<tr>"
            f"<td class='mono'>{html.escape(snap['station_icao'])}</td>"
            f"<td class='mono'>{bucket_label}</td>"
            f"<td class='mono'>{html.escape(r['side'])}</td>"
            f"<td class='mono num'>{r['model_prob']:.1%}</td>"
            f"<td class='mono num'>{r['market_price']:.3f}</td>"
            f"<td class='mono num'>{r['raw_edge']:+.1%}</td>"
            f"<td class='mono num {cls}'>{ev:+.1%}{flags}</td>"
            "</tr>"
        )
    return rows, unpriced, suppressed


try:
    ev_snaps = _load_ev_snapshots()
    if ev_snaps:
        ages = []
        section_blocks = []
        total_unpriced = 0
        total_suppressed = 0
        # Grouped by station, each under its own subheading, instead of one
        # flat table spanning all 13 stations sorted purely by net EV -- at
        # 2-station scale a flat ranking was fine, but at 13 it buries a
        # station's own picture next to whichever other station happened to
        # have a hotter EV that cycle, making "what does Tokyo actually look
        # like right now" a scroll-and-squint exercise. Sorted alphabetically
        # by ICAO for a stable page layout (not by "best EV station first",
        # which would reorder the whole page every regeneration).
        for snap in sorted(ev_snaps, key=lambda s: s.get("station_icao", "")):
            rows, unpriced, suppressed = _ev_rows(snap)
            total_unpriced += unpriced
            total_suppressed += suppressed
            ages.append(snap.get("generated_at", ""))
            if not rows:
                continue
            icao = snap.get("station_icao", "?")
            station = config.STATIONS.get(icao)
            station_label = (f"{html.escape(icao)} &middot; {html.escape(station.display_name)}"
                              if station else html.escape(icao))
            section_blocks.append(
                f"<h3 class='evstation'>{station_label}</h3>"
                "<div class='tablewrap'><table class='ptable'>"
                "<thead><tr><th>Station</th><th>Bucket</th><th>Side</th>"
                "<th class='num'>Model p</th><th class='num'>Mkt price</th>"
                "<th class='num'>Raw edge</th><th class='num'>Net EV/$</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
            )
        newest = max(ages) if ages else ""
        age_note = f"as of {html.escape(newest[11:16])} UTC" if len(newest) >= 16 else ""
        if section_blocks:
            ev_html = "".join(section_blocks)
            if total_unpriced:
                ev_html += (f"<p class='cap' style='margin:14px 0 0'>{total_unpriced} bucket/side rows "
                            "had no live quote (unseeded far-tail books).</p>")
            if total_suppressed:
                ev_html += (f"<p class='cap' style='margin:6px 0 0'>{total_suppressed} row(s) suppressed: "
                            f"market price under {EV_MIN_PRICE:.2f}, where percentage EV explodes on a "
                            "converged market the stale model disagrees with &mdash; the entry screen "
                            "excludes these too (config.EV_MIN_PRICE_SCREEN).</p>")
        else:
            ev_html = ("<div class='empty'>No bucket/side cleared the "
                       f"{EV_ENTRY_SCREEN:.0%} net-EV entry screen in the latest computation.</div>")
        ev_cap = (f"latest computation {age_note} &middot; grouped by station &middot; only rows at or "
                  f"above the {EV_ENTRY_SCREEN:.0%} net-EV entry screen shown")
    else:
        ev_html = ("<div class='empty'>No EV snapshot yet &mdash; the engine computes during the morning "
                   "entry window (05:00&ndash;08:00 local) and saves its table here from then on.</div>")
        ev_cap = "model edge vs. market, per bucket and side, grouped by station"
except Exception as exc:  # noqa: BLE001
    warnings.append(f"EV card failed: {exc}")
    ev_html, ev_cap = "<div class='empty'>EV view unavailable</div>", ""

# --- model vs market panel ---------------------------------------------------
# Measured bias, the EV the engine is looking at right now, and how the model
# has actually scored against the market -- per station, for THIS region only.
#
# The arithmetic belongs to calibration_panel, which calls promotion_dossier:
# the same module the promotion decision itself reads, so this page cannot
# drift into a second definition of "beats the market". This block only asks.
try:
    import calibration_panel

    calib_rows, calib_warnings = calibration_panel.station_rows(REGION_STATIONS)
    warnings.extend(calib_warnings)
    calib_html = calibration_panel.render_table_html(calib_rows)
    calib_cap = (
        "positive gap means the model scored better than the market on those entries "
        "&middot; <b>n_days is the honest ceiling</b> &mdash; entries taken on one "
        "station-day settle off one draw of the weather, so n overstates the evidence "
        "&middot; a gap in bold clears twice its own standard error, which is not a verdict"
    )
except Exception as exc:  # noqa: BLE001
    warnings.append(f"model-vs-market card failed: {exc}")
    calib_html, calib_cap = "<div class='empty'>model vs market unavailable</div>", ""

# --- positions detail table --------------------------------------------------
def status_label(status):
    """Map a position status string to (label, css class), tolerant of the
    exact wording the risk manager writes (e.g. closed_stop vs closed_stop_loss)."""
    s = (status or "").lower()
    if s == "open":
        return "open", "st-open"
    if "profit" in s:
        return "take profit", "st-good"
    if "trailing" in s:
        return "trailing stop", "st-warn"
    if "stop" in s:
        return "stop loss", "st-bad"
    if "resol" in s:
        return "resolved", "st-neutral"
    if "manual" in s:
        return "manual close", "st-neutral"
    return s.replace("_", " "), "st-neutral"


def _hm(iso_ts, utc_offset_hours=8):
    """ISO timestamp -> compact 'dd MMM HH:MM' LOCAL string, in the given
    utc_offset_hours (defaults to +8 -- the legacy SGT behaviour -- for any
    caller that can't name a specific station's offset)."""
    if not iso_ts:
        return "&mdash;"
    try:
        dt = datetime.fromisoformat(str(iso_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = dt.timestamp() + utc_offset_hours * 3600
        d = datetime.fromtimestamp(secs, tz=timezone.utc)
        return d.strftime("%d %b %H:%M")
    except ValueError:
        return html.escape(str(iso_ts)[:16])


def _usd(amount):
    """'+$12.34' / '-$12.34', with the sign DERIVED from the value.

    Every signed dollar figure in the calendar used to rebuild this inline, and
    two of the four sites asserted the sign they expected instead of reading
    it: "Best" hardcoded a '+' and "Worst" a '-'. A grid always has a best and
    a worst day and NEITHER has a guaranteed sign -- on an all-winning grid the
    worst day is still a profit, and it rendered as "Worst -$5.00" in red; on
    an all-losing grid the best day is still a loss, and it rendered as the
    garbled "Best +$-12.34". Both are reachable on a young real-money track
    with a handful of closes.
    """
    return f"{'+' if amount >= 0 else '-'}${abs(amount):,.2f}"


def _pn(amount):
    """CSS class for a signed figure -- derived, for the same reason as _usd."""
    return "pos" if amount >= 0 else "neg"


def _age(epoch_s):
    """'4m ago' / '2h ago' for a captured-quote timestamp.

    A mark is only worth reading next to how old it is: the exit checker only
    quotes a position while its station is in an active scan window, so a
    perfectly valid row can be hours stale outside one.
    """
    secs = max(0, int(time.time()) - int(epoch_s))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


_mark_lookup_failed = []


def _open_mark(p):
    """The last bid ACTUALLY CAPTURED for this open position's token, or None.

    Reads backtest.price_store -- the series position_manager writes on every
    exit check (price_store.EXIT_SNAPSHOT_SOURCE) -- through get_price_at(), so
    the staleness guard is the same one the replay honours: a series that has
    gone quiet for longer than its own recorded fidelity allows returns None
    rather than pretending the last print is still the market.

    DELIBERATELY NOT position.high_water_mark, which is what this column used
    to show. The hwm is a monotone non-decreasing peak (risk_manager.
    update_high_water_mark), so an unrealized P&L computed off it could never
    print a loss -- every open row would read flat-or-up regardless of where
    the market actually is, which is the overhang trap the P&L calendar caption
    below already warns about, dressed up as a number.

    Fail-soft like every other read on this page: no token id, no rows, or an
    unreachable price store all mean "no mark", never a fabricated one.
    """
    if not p.token_id:
        return None
    try:
        import backtest.price_store as price_store  # noqa: E402

        return price_store.get_price_at(p.token_id, int(time.time()))
    except Exception as exc:  # noqa: BLE001 - a missing mark must not kill the page
        if not _mark_lookup_failed:
            _mark_lookup_failed.append(1)
            warnings.append(f"open-position marks unavailable: {exc}")
        return None


def _ret_and_pnl(p, price):
    """(return string, css, P&L string, css) for this position marked at `price`.

    ONE arithmetic for the open row and the closed row, and the same one
    paper_trading_report uses: the percentage is _position_return_pct()'s
    (price - entry) / entry, and the dollar figure is that fraction times the
    stake, which is exactly how summarize_positions() accumulates
    total_pnl_usd. A row and the tile above it cannot drift apart because
    neither re-derives P&L its own way.
    """
    if price is None or not p.entry_price:
        return "&mdash;", "", "&mdash;", ""
    r = (price - p.entry_price) / p.entry_price
    return f"{r * 100:+.1f}%", _pn(r), _usd(p.size_usd * r), _pn(r)


# Entry and exit get their OWN columns, and so do the entered and exited
# timestamps. The merged "Last/exit" and "Entered/exited" cells they replace
# printed an open position's running mark under a heading that says exit, which
# reads as a trade already closed at that price -- the one thing an operator
# scanning this table must never be misled about. Paper and real money share
# these columns; the only difference between the two tables is stake precision.
POS_COLS = ("<thead><tr><th>Station</th><th>Market date</th><th>Bucket</th><th>Side</th>"
            "<th class='num'>Size</th><th class='num'>Entry</th><th class='num'>Mark</th>"
            "<th class='num'>Exit</th><th class='num'>Return</th><th class='num'>P&amp;L</th>"
            "<th>Status</th><th>Entered</th><th>Exited</th></tr></thead>")


def _pos_row(p, live=False):
    is_open = p.status == "open"
    label, cls = status_label(p.status)
    # Entered/exited times render in THIS position's own station's local
    # time, not a blanket SGT -- a Tokyo (UTC+9) position timestamped in
    # Singapore (UTC+8) time would print an hour off, which is exactly the
    # kind of cross-timezone mislabeling the rest of this expansion (see
    # config.local_today) exists to avoid. Falls back to the legacy +8 if
    # the station isn't (or is no longer) registered.
    try:
        pos_station = config.get_station(p.station_icao)
        station_offset = pos_station.utc_offset_hours
    except KeyError:
        pos_station = None
        station_offset = 8
    bucket_label = bucket_axis.for_station(pos_station).label(
        p.bucket_c,
        getattr(pos_station, "bucket_min_c", 25),
        getattr(pos_station, "bucket_max_c", 35),
    ).replace("°", "&deg;")
    entered = _hm(p.entry_time, station_offset)
    exited = _hm(p.exit_time, station_offset) if not is_open else "&mdash;"

    if is_open:
        # UNREALIZED. An open row's P&L is a mark-to-market against a quote
        # nobody has traded on, and the position can still round-trip to
        # anything between 0 and 1 before it settles. No mark means no number:
        # an em-dash is honest, a stale print dressed as a P&L is not.
        mark = _open_mark(p)
        exit_cell = "&mdash;"
        if mark is None:
            mark_cell = "&mdash;<span class='sub'>no fresh quote</span>"
            ret, ret_cls, pnl, pnl_cls = "&mdash;", "", "&mdash;", ""
        else:
            mark_cell = f"{mark['price']:.2f}<span class='sub'>{_age(mark['ts'])}</span>"
            ret, ret_cls, pnl, pnl_cls = _ret_and_pnl(p, mark["price"])
    else:
        mark_cell = "&mdash;"
        exit_cell = f"{p.exit_price:.2f}" if p.exit_price is not None else "&mdash;"
        ret, ret_cls, pnl, pnl_cls = _ret_and_pnl(p, p.exit_price)

    # Real money is sized in dollars and cents ($1.00 an entry), so the paper
    # table's whole-dollar format would render every live stake as "$1".
    size = f"${p.size_usd:,.2f}" if live else f"${p.size_usd:,.0f}"
    return (
        "<tr>"
        f"<td class='mono'>{html.escape(p.station_icao)}</td>"
        f"<td class='mono'>{html.escape(str(p.target_date))}</td>"
        f"<td class='mono'>{bucket_label}</td>"
        f"<td class='mono'>{html.escape(p.side)}</td>"
        f"<td class='mono num'>{size}</td>"
        f"<td class='mono num'>{p.entry_price:.2f}</td>"
        f"<td class='mono num'>{mark_cell}</td>"
        f"<td class='mono num'>{exit_cell}</td>"
        f"<td class='mono num {ret_cls}'>{ret}</td>"
        f"<td class='mono num {pnl_cls}'>{pnl}</td>"
        f"<td><span class='st {cls}'>{html.escape(label)}</span></td>"
        f"<td class='mono dim2'>{entered}</td>"
        f"<td class='mono dim2'>{exited}</td>"
        "</tr>"
    )


MAX_CLOSED_SHOWN = 15
try:
    shown = open_positions + closed_positions[:MAX_CLOSED_SHOWN]
    if shown:
        omitted = len(closed_positions) - min(len(closed_positions), MAX_CLOSED_SHOWN)
        rows = "".join(_pos_row(p) for p in shown)
        foot = (
            f"<p class='cap' style='margin:10px 0 0'>{omitted} older closed positions not shown.</p>"
            if omitted > 0 else ""
        )
        positions_html = (
            "<div class='tablewrap'><table class='ptable'>"
            + POS_COLS +
            f"<tbody>{rows}</tbody></table></div>{foot}"
        )
        positions_cap = (
            f"{len(open_positions)} open &middot; {min(len(closed_positions), MAX_CLOSED_SHOWN)} most recent closed"
            " &middot; open rows are marked to the last captured bid and their P&amp;L is"
            " UNREALIZED &mdash; nothing has traded against it"
        )
    else:
        positions_html = (
            "<div class='empty'>No paper positions yet &mdash; the first candidates appear in the"
            " 05:00&ndash;08:00 local entry window, and only if the EV engine finds edge worth taking.</div>"
        )
        positions_cap = f"paper positions, {station_count} stations"
except Exception as exc:  # noqa: BLE001
    warnings.append(f"positions table failed: {exc}")
    positions_html = "<div class='empty'>positions table unavailable</div>"
    positions_cap = ""

# --- daily P&L calendar ------------------------------------------------------
# One cell per calendar day, coloured by that day's REALIZED P&L. The rest of
# the page reports the book as a single running total, which answers "are we up"
# but not "when did that happen" -- a run that is +$40 because of one huge day
# and eleven flat ones is a different system from one that grinds +$3.50 a day,
# and those two are indistinguishable in every other number on this page.
#
# THREE THINGS THIS GRID DELIBERATELY DOES NOT DO:
#
# 1. It never mixes tracks. Paper and real-money each get their own grid, built
#    by the same function and rendered in their own card -- same rule as every
#    other number here.
# 2. It counts REALIZED P&L only: a day's cell moves when a position CLOSES, and
#    an open position contributes nothing to any cell until it does. That is the
#    overhang trap the 2026-08-15 cohort review ran into -- a book that looks
#    green because its losers are still open and only its winners have settled.
#    The caption carries the open-position count for exactly that reason.
# 3. It does not re-derive P&L. Every cell is summed by the same
#    paper_trading_report.summarize_positions() the headline tile and the station
#    table use, so a cell and the total above it cannot drift apart.
CAL_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _exit_local_date(p):
    """The calendar date this trade's P&L landed on, in the position's OWN
    station-local time -- not UTC, and not a blanket SGT.

    A Tokyo (UTC+9) position that closes at 22:30 UTC closed on the NEXT day
    in Tokyo, and filing it under the UTC date would put it in the wrong cell
    of this grid. Same reasoning as _hm() above and config.local_today().

    Attribution is by EXIT time, not target_date: this grid answers "what did
    the book earn or lose that day", and a position closed at resolution is
    routinely settled the morning after its market date, once the day's
    settlement-grade observation is actually published.
    """
    if not p.exit_time:
        return None
    try:
        station_offset = config.get_station(p.station_icao).utc_offset_hours
    except KeyError:
        station_offset = 8
    try:
        dt = datetime.fromisoformat(str(p.exit_time))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(dt.timestamp() + station_offset * 3600, tz=timezone.utc).date()


def _daily_pnl(positions):
    """date -> {pnl, n, staked, tip}, plus a count of positions with no usable
    exit timestamp (which belong in no cell and are reported, not dropped silently)."""
    import paper_trading_report as ptr  # noqa: E402,F811 - same arithmetic as the P&L tile

    buckets = {}
    undated = 0
    for p in positions:
        d = _exit_local_date(p)
        if d is None:
            undated += 1
            continue
        buckets.setdefault(d, []).append(p)

    days = {}
    for d, ps in buckets.items():
        summary = ptr.summarize_positions(ps)
        if not summary:  # no position that day had a usable realized return
            continue
        # Per-station split, one summarize_positions() call per station -- that
        # is the function's documented contract (single station in, stats out),
        # and it makes the hover text reconcile exactly with the station table.
        by_station = {}
        for p in ps:
            by_station.setdefault(p.station_icao, []).append(p)

        parts = []
        for icao in sorted(by_station):
            s = ptr.summarize_positions(by_station[icao])
            if s:
                parts.append(f"{icao} {s['total_pnl_usd']:+.2f} ({s['n_trades']} trade"
                             f"{'' if s['n_trades'] == 1 else 's'})")
        days[d] = {
            "pnl": float(summary["total_pnl_usd"]),
            "n": int(summary["n_trades"]),
            "staked": float(summary["total_staked_usd"]),
            "tip": "  ".join(parts),
        }
    return days, undated


def _pnl_calendar(positions, empty_msg):
    """Month grids + a summary strip for one track's closed positions."""
    days, undated = _daily_pnl(positions)
    if not days:
        return f"<div class='empty'>{empty_msg}</div>"

    first, last = min(days), max(days)
    # Heat is scaled to the biggest single day IN THIS GRID, so the shading is
    # readable on a $3/day paper book and on a $0.40/day real-money one without
    # either being tuned by hand. It is therefore a WITHIN-track ranking, never
    # a cross-track one -- another reason the two grids stay in separate cards.
    peak = max(abs(v["pnl"]) for v in days.values()) or 1.0
    today_sgt = (datetime.now(timezone.utc) + timedelta(hours=8)).date()

    months = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    blocks = []
    for y, m in months:
        lead, ndays = calmod.monthrange(y, m)  # lead = weekday of the 1st, Mon=0
        month_pnl = sum(v["pnl"] for d, v in days.items() if (d.year, d.month) == (y, m))
        month_n = sum(v["n"] for d, v in days.items() if (d.year, d.month) == (y, m))
        cells = [f"<div class='calhead'>{d}</div>" for d in CAL_DOW]
        cells += ["<div class='cell blank'></div>"] * lead
        for dom in range(1, ndays + 1):
            d = datetime(y, m, dom, tzinfo=timezone.utc).date()
            today_cls = " today" if d == today_sgt else ""
            v = days.get(d)
            if v is None:
                # A day inside the run with no closes reads differently from a
                # day before the book existed: "nothing closed" vs "not yet
                # trading". Only the first is a fact about the strategy.
                if first <= d <= last:
                    cells.append(f"<div class='cell flat{today_cls}'><div class='d'>{dom}</div>"
                                 "<div class='amt'>&middot;</div><div class='n'>no closes</div></div>")
                else:
                    cells.append(f"<div class='cell void{today_cls}'><div class='d'>{dom}</div></div>")
                continue
            gain = v["pnl"] >= 0
            cls = "gain" if gain else "loss"
            # 14-64% of the accent colour mixed into the card background: enough
            # for the shape of the month to read at a glance, never so much that
            # the number on top of it stops being legible in either theme.
            mix = 14 + 50 * min(1.0, abs(v["pnl"]) / peak)
            var = "--good" if gain else "--bad"
            amt = _usd(v["pnl"])
            tip = f"{d.isoformat()}  {amt} on ${v['staked']:,.2f} staked\n{v['tip']}"
            cells.append(
                f"<div class='cell {cls}{today_cls}' title='{html.escape(tip)}' "
                f"style='background:color-mix(in srgb, var({var}) {mix:.0f}%, var(--card))'>"
                f"<div class='d'>{dom}</div>"
                f"<div class='amt'>{amt}</div>"
                f"<div class='n'>{v['n']} trade{'' if v['n'] == 1 else 's'}</div></div>"
            )
        blocks.append(
            f"<div class='calmonth'><b>{calmod.month_name[m]} {y}</b>"
            f"<span class='mn'>{month_n} closed</span>"
            f"<span class='mtot {_pn(month_pnl)}'>{_usd(month_pnl)}</span></div>"
            f"<div class='tablewrap'><div class='cal'>{''.join(cells)}</div></div>"
        )

    total = sum(v["pnl"] for v in days.values())
    green = sum(1 for v in days.values() if v["pnl"] > 0)
    red = sum(1 for v in days.values() if v["pnl"] < 0)
    best = max(days.items(), key=lambda kv: kv[1]["pnl"])
    worst = min(days.items(), key=lambda kv: kv[1]["pnl"])
    span = (last - first).days + 1
    quiet = span - len(days)
    stats = [
        f"Realized <b class='{_pn(total)}'>{_usd(total)}</b> over {span} day(s)",
        f"<b class='pos'>{green}</b> up / <b class='neg'>{red}</b> down"
        + (f" / <b>{quiet}</b> with no closes" if quiet else ""),
        f"Best <b class='{_pn(best[1]['pnl'])}'>{_usd(best[1]['pnl'])}</b> ({best[0].isoformat()})",
        f"Worst <b class='{_pn(worst[1]['pnl'])}'>{_usd(worst[1]['pnl'])}</b> ({worst[0].isoformat()})",
    ]
    if undated:
        stats.append(f"<b>{undated}</b> closed position(s) carry no exit timestamp and sit in no cell")
    return "".join(blocks) + f"<div class='calstats'>{'&middot;'.join(f'<span>{s}</span>' for s in stats)}</div>"


try:
    pnl_calendar_html = _pnl_calendar(
        closed_positions,
        "No closed paper trades yet &mdash; days fill in as positions close.",
    )
    pnl_cal_cap = (
        "PAPER track &middot; each cell is that day&rsquo;s <b>realized</b> P&amp;L, summed by the same "
        "code as the tile above &middot; a trade lands on the day it CLOSED, in its own station&rsquo;s "
        "local time &middot; shading is relative to this grid&rsquo;s biggest day; the orange outline is "
        "today (SGT)"
    )
    if open_positions:
        pnl_cal_cap += (f" &middot; <b>{len(open_positions)} position(s) still open and counted nowhere "
                        "here</b> &mdash; an unresolved overhang can hold a green month up on its own")
except Exception as exc:  # noqa: BLE001
    warnings.append(f"P&L calendar failed: {exc}")
    pnl_calendar_html = "<div class='empty'>daily P&amp;L calendar unavailable</div>"
    pnl_cal_cap = ""

# --- real-money card ---------------------------------------------------------
# Deliberately the FIRST card on the page and visually the loudest. Everything
# below it is paper; this is the only section where a number moving means real
# capital moved. Open real positions are the headline -- an operator glancing
# at this page should never have to work out whether money is currently at
# risk, which was impossible when the page read is_paper=True everywhere.
MAX_LIVE_CLOSED_SHOWN = 10
try:
    blocks = []
    if live_open:
        cap_bits = []
        if live_cap_usd:
            pct_of_cap = live_at_risk / live_cap_usd if live_cap_usd else 0
            cap_bits.append(f"{pct_of_cap:.0%} of the ${live_cap_usd:,.2f} exposure cap")
        if live_cap_n:
            cap_bits.append(f"{len(live_open)} of {live_cap_n} concurrent slots")
        blocks.append(
            "<div class='moneybar'>"
            f"<div class='mb-amt'>${live_at_risk:,.2f}</div>"
            f"<div class='mb-lab'>real capital at risk right now<br>"
            f"<span class='mb-sub'>{' &middot; '.join(cap_bits)}</span></div>"
            "</div>"
        )
        blocks.append(
            "<div class='tablewrap'><table class='ptable'>" + POS_COLS +
            f"<tbody>{''.join(_pos_row(p, live=True) for p in live_open)}</tbody>"
            "</table></div>"
        )
    elif live_region_caps_all_zero:
        blocks.append(
            "<div class='moneybar flat'>"
            "<div class='mb-amt'>$0.00</div>"
            "<div class='mb-lab'>no real capital at risk right now<br>"
            f"<span class='mb-sub'>no station in the {html.escape(region)} region can submit a real "
            "order &mdash; config.REGION_LIVE_MAX_CONCURRENT_POSITIONS, _TOTAL_EXPOSURE_USD and "
            f"_ORDERS_PER_DAY are all 0 for {html.escape(region)!r}. Promotion means raising those "
            "three entries, not just adding a station to LIVE_TRADING_STATIONS.</span></div>"
            "</div>"
        )
    else:
        blocks.append(
            "<div class='moneybar flat'>"
            "<div class='mb-amt'>$0.00</div>"
            "<div class='mb-lab'>no real capital at risk right now<br>"
            f"<span class='mb-sub'>{html.escape(', '.join(live_stations)) if live_stations else 'no station'} "
            f"armed for live orders at ${live_size_usd:,.2f} per entry</span></div>"
            "</div>"
        )

    if live_closed:
        shown_live = live_closed[:MAX_LIVE_CLOSED_SHOWN]
        realized = f"{'-' if live_pnl_usd < 0 else '+'}${abs(live_pnl_usd):,.2f}"
        realized_cls = "neg" if live_pnl_usd < 0 else "pos"
        pct = f" ({live_pnl_usd / live_staked_usd:+.1%})" if live_staked_usd else ""
        blocks.append(
            f"<p class='cap' style='margin:16px 0 6px'>Closed real-money trades &mdash; realized "
            f"<span class='{realized_cls}'><b>{realized}</b></span> on ${live_staked_usd:,.2f} staked"
            f"{pct}, {len(live_closed)} trade(s)</p>"
            "<div class='tablewrap'><table class='ptable'>" + POS_COLS +
            f"<tbody>{''.join(_pos_row(p, live=True) for p in shown_live)}</tbody>"
            "</table></div>"
        )
        # Real money gets its own daily grid, inside its own card. Same builder
        # as the paper calendar further down the page, never the same grid: the
        # heat scale is per-grid, so a $0.40 real day and a $40 paper day would
        # otherwise be shaded against each other.
        blocks.append(
            "<p class='cap' style='margin:18px 0 6px'>Real-money daily realized P&amp;L</p>"
            + _pnl_calendar(live_closed, "No closed real-money trades yet.")
        )

    realmoney_html = "".join(blocks)
    if live_stations:
        realmoney_cap = (f"live-armed: {html.escape(', '.join(live_stations))} &middot; "
                         f"${live_size_usd:,.2f} per entry &middot; separate from every paper number "
                         "on this page, never summed with them")
    elif live_region_caps_all_zero:
        realmoney_cap = (f"no station in the {html.escape(region)} region can submit a real order "
                         "&mdash; REGION_LIVE_MAX_CONCURRENT_POSITIONS / _TOTAL_EXPOSURE_USD / "
                         f"_ORDERS_PER_DAY are all 0 for {html.escape(region)!r} &middot; the whole "
                         "book below is paper")
    else:
        realmoney_cap = ("no station passes config.live_mode_is_permitted() &mdash; "
                         "the whole book below is paper")
except Exception as exc:  # noqa: BLE001
    warnings.append(f"real-money card failed: {exc}")
    realmoney_html = "<div class='empty'>real-money view unavailable</div>"
    realmoney_cap = ""

now_utc = datetime.now(timezone.utc)
snap_sgt = now_utc.astimezone(timezone.utc).strftime("%d %b %Y, ")
snap_sgt += f"{(now_utc.hour + 8) % 24:02d}:{now_utc.minute:02d} SGT"
run_days = max(0, (now_utc.timestamp() * 1000 - deploy_epoch_ms) / 86400000) if deploy_epoch_ms else 0

service_ok = active_state == "active"
pill_cls = "active" if service_ok else "down"
pill_txt = "service active" if service_ok else f"service {html.escape(active_state)}"

# The mode pill used to be the hardcoded string "paper mode -- no live orders".
# That is a claim about where real money can go, and it went stale the moment
# a station was armed for live trading -- the worst possible way for this
# particular sentence to be wrong. Derived from the live gate now.
if live_stations:
    mode_pill = ("<span class='pill livem'><span class='dot'></span>LIVE &mdash; real money: "
                 f"{html.escape(', '.join(live_stations))}</span>")
else:
    mode_pill = "<span class='pill paperm'><span class='dot'></span>paper mode &mdash; no live orders</span>"

region_label = region.capitalize()

# Cross-link every registered region's page. Built from
# config.REGION_BANKROLL_USD's keys rather than a hardcoded "Asia"/"Europe"
# pair, so a third region gets its own nav pill for free the day it's
# registered. Filenames mirror the OUT computation above: asia is
# index.html (the pre-existing URL), everything else is "<region>.html".
region_nav_html = "".join(
    (f"<span class='pill regioncur'>{html.escape(_r.capitalize())}</span>"
     if _r == region else
     f"<a class='pill' href='{html.escape('index.html' if _r == 'asia' else f'{_r}.html')}'>"
     f"{html.escape(_r.capitalize())}</a>")
    for _r in sorted(config.REGION_BANKROLL_USD)
)

live_at_risk_display = f"${live_at_risk:,.2f}"
if live_open:
    live_note = f"{len(live_open)} open"
    if live_cap_usd:
        live_note += f" &middot; cap ${live_cap_usd:,.2f}"
elif live_stations:
    live_note = f"{html.escape(', '.join(live_stations))} armed &middot; ${live_size_usd:,.2f}/entry"
else:
    live_note = "no live stations armed"

warn_html = ""
if warnings:
    items = "".join(f"<div>&bull; {html.escape(w)}</div>" for w in warnings)
    warn_html = (
        '<div class="callout"><b>Data collection warnings</b>' + items + "</div>"
    )


def tile_val(v):
    return "&mdash;" if v is None else str(v)


# --- station table ----------------------------------------------------------
# One row per registered station instead of a grid of descriptive cards. The
# cards' prose (observed history / calibration / sizing) was entirely
# derivable from the maturity badge, while the numbers worth scanning per
# station -- closed-trade stats, open exposure, realized P&L -- weren't on
# the page at all in per-station form. Stats come from the same
# summarize_paper_performance() dicts the headline P&L tile sums over, so
# the table rows and the tile can never disagree.
def _station_table():
    open_count = {}
    open_staked = {}
    for p in open_positions:
        open_count[p.station_icao] = open_count.get(p.station_icao, 0) + 1
        open_staked[p.station_icao] = open_staked.get(p.station_icao, 0.0) + float(p.size_usd or 0.0)
    rows = []
    for icao in sorted(REGION_STATIONS):
        st = config.STATIONS[icao]
        maturity = config.STATION_MATURITY.get(icao, "exploratory")
        badge_cls = "mature" if maturity == "mature" else "immature"
        s = station_summaries.get(icao)
        if s:
            n_trades = str(s["n_trades"])
            win = f"{s['win_rate']:.0%}"
            pnl = float(s["total_pnl_usd"])
            pnl_cell = f"{'-' if pnl < 0 else '+'}${abs(pnl):,.2f}"
            pnl_cls = "neg" if pnl < 0 else "pos"
            dwr = s.get("dollar_weighted_return_pct")
            ret = f"{dwr:+.1%}" if dwr is not None else "&mdash;"
            ret_cls = ("neg" if dwr < 0 else "pos") if dwr is not None else ""
        else:
            n_trades, win = "0", "&mdash;"
            pnl_cell, pnl_cls, ret, ret_cls = "&mdash;", "", "&mdash;", ""
        n_open = open_count.get(icao, 0)
        at_risk = f"${open_staked.get(icao, 0.0):,.0f}" if n_open else "&mdash;"
        # Which track this station's orders actually go to. Without it the
        # table's paper-only stats silently imply WSSS is paper like the rest.
        track = ("<span class='badge live'>real money</span>" if icao in live_stations
                 else "<span class='badge paper'>paper</span>")
        rows.append(
            "<tr>"
            f"<td class='mono'>{html.escape(icao)}</td>"
            f"<td>{html.escape(st.display_name)}</td>"
            f"<td>{track}</td>"
            f"<td><span class='badge {badge_cls}'>{html.escape(maturity)}</span></td>"
            f"<td class='mono num'>{n_trades}</td>"
            f"<td class='mono num'>{win}</td>"
            f"<td class='mono num'>{n_open}</td>"
            f"<td class='mono num'>{at_risk}</td>"
            f"<td class='mono num {pnl_cls}'>{pnl_cell}</td>"
            f"<td class='mono num {ret_cls}'>{ret}</td>"
            "</tr>"
        )
    return (
        "<div class='tablewrap'><table class='ptable'>"
        "<thead><tr><th>Station</th><th>Name</th><th>Track</th><th>Maturity</th>"
        "<th class='num'>Trades</th><th class='num'>Win rate</th>"
        "<th class='num'>Open</th><th class='num'>At risk</th>"
        "<th class='num'>Realized P&amp;L</th><th class='num'>Per $1 staked</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


try:
    stations_table_html = _station_table()
    stations_cap = ("PAPER track only &mdash; real-money trades are in the card at the top, never "
                    "mixed in here &middot; &ldquo;Per $1 staked&rdquo; is realized P&amp;L divided by "
                    "everything staked, so a big trade counts for more than a small one &middot; "
                    "At risk sums open paper stakes")
except Exception as exc:  # noqa: BLE001
    warnings.append(f"station table failed: {exc}")
    stations_table_html = "<div class='empty'>station table unavailable</div>"
    stations_cap = ""

# --- per-timezone-group time strips -----------------------------------------
# The registry spans UTC+5 (Karachi) through UTC+9 (Tokyo/Seoul/Busan) --
# scheduler.py already runs each offset group's primary/secondary/monitor
# cycle against that group's OWN local clock (config.SCHEDULE_WINDOWS), so a
# single "Singapore time" strip stopped being an honest picture of the whole
# system's trading day the moment a station outside UTC+8 was registered.
# Group stations by utc_offset_hours -- derived from this region's slice of
# the live registry, never hardcoded, so a future station on a not-yet-seen
# offset gets its own strip automatically -- and render one strip per group.
_offset_groups = {}
for _icao in REGION_STATIONS:
    _st = config.STATIONS[_icao]
    _offset_groups.setdefault(_st.utc_offset_hours, []).append(_icao)

_now_for_strips = datetime.now(timezone.utc)


def _next_local_5am_utc_ms(offset_hours, now_utc):
    """UTC epoch ms of this offset's next upcoming 05:00 local -- today's
    05:00 if that hasn't passed yet local-side, otherwise tomorrow's."""
    local_now = now_utc + timedelta(hours=offset_hours)
    target_local = local_now.replace(hour=5, minute=0, second=0, microsecond=0)
    if local_now >= target_local:
        target_local += timedelta(days=1)
    target_utc = target_local - timedelta(hours=offset_hours)
    return int(target_utc.timestamp() * 1000)


_strip_blocks = []
_nowmark_groups = []  # -> JS as JSON: [{offset, markId, clockId}, ...] so tick()
                       # can position + label each group's own "now" client-side
_group_next5am_ms = []
for _idx, _offset in enumerate(sorted(_offset_groups)):
    _stations_in_group = _offset_groups[_offset]
    _mark_id = f"nowmark-{_idx}"
    _clock_id = f"localclock-{_idx}"
    _nowmark_groups.append({"offset": _offset, "markId": _mark_id, "clockId": _clock_id})
    _group_next5am_ms.append(_next_local_5am_utc_ms(_offset, _now_for_strips))
    _sign = "+" if _offset >= 0 else ""
    _strip_blocks.append(f"""
    <div class="strip">
      <div class="striphead"><b>UTC{_sign}{_offset}</b>
        <span class="stripstations">{html.escape(", ".join(_stations_in_group))}</span>
        <span class="stripclock mono" id="{_clock_id}">&mdash;</span></div>
      <div class="segments">
        <div class="seg closed" style="flex:4"></div>
        <div class="seg closed" style="flex:1;opacity:.55"></div>
        <div class="seg primary" style="flex:3"></div>
        <div class="seg monitor" style="flex:2"></div>
        <div class="seg monitor" style="flex:14"></div>
      </div>
      <div class="nowmark" id="{_mark_id}"></div>
      <div class="seglabels">
        <div class="seglabel" style="flex:4"><b>00&ndash;04</b>closed &mdash; hard floor</div>
        <div class="seglabel" style="flex:1"><b>04</b>warm-up</div>
        <div class="seglabel" style="flex:3"><b>05&ndash;08</b>entries &mdash; the only ones</div>
        <div class="seglabel" style="flex:2"><b>08&ndash;10</b>monitoring, loose stop</div>
        <div class="seglabel" style="flex:14"><b>10&ndash;24</b>monitoring, tightened stop</div>
      </div>
      <div class="hours"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    </div>""")

time_strips_html = "".join(_strip_blocks)
# Countdown target = the SOONEST upcoming 05:00 local across every group, not
# just one -- on any given UTC instant, Tokyo's (UTC+9) primary window opens
# before Singapore's (UTC+8), which opens before Karachi's (UTC+5).
countdown_target_ms = min(_group_next5am_ms) if _group_next5am_ms else 0
nowmark_groups_json = json.dumps(_nowmark_groups)

page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>polyweather &mdash; trading monitor</title>
<style>
  :root {
    --paper:#F2F4F6; --card:#FBFCFD; --ink:#182230; --ink-2:#4A5866;
    --muted:#7B8794; --line:#D8DEE4; --teal:#147D8C; --teal-soft:#63AEB8;
    --teal-faint:#D5E6E9; --heat:#C65D1E; --good:#2E7D4F; --good-bg:#E3F0E8;
    --warn:#9A6B15; --warn-bg:#F5ECD9; --bad:#A33B2E; --bad-bg:#F3E1DE;
    --mono:"Cascadia Code",Consolas,"SF Mono",Menlo,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper:#10161D; --card:#171F28; --ink:#E4E9EE; --ink-2:#AEB9C4;
      --muted:#76838F; --line:#2A343F; --teal:#2BB3C4; --teal-soft:#1A6E7C;
      --teal-faint:#1C333A; --heat:#E58A4B; --good:#57B380; --good-bg:#16281E;
      --warn:#D3A34E; --warn-bg:#2C2413; --bad:#E07A6B; --bad-bg:#33201C;
    }
  }
  * { box-sizing:border-box; }
  body { background:var(--paper); color:var(--ink);
    font:15px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif;
    margin:0; padding:28px 20px 48px; }
  .wrap { max-width:980px; margin:0 auto; display:flex; flex-direction:column; gap:18px; }
  header .eyebrow { font-family:var(--mono); font-size:11px; letter-spacing:.14em;
    text-transform:uppercase; color:var(--teal); margin-bottom:6px; }
  header h1 { font-size:26px; margin:0 0 4px; letter-spacing:-.01em; }
  header .sub { color:var(--ink-2); font-size:14px; margin:0; }
  .headrow { display:flex; flex-wrap:wrap; align-items:flex-end; justify-content:space-between; gap:12px; }
  .pills { display:flex; gap:8px; flex-wrap:wrap; }
  .pill { display:inline-flex; align-items:center; gap:7px; font-family:var(--mono);
    font-size:12px; padding:5px 12px; border-radius:999px; border:1px solid var(--line);
    background:var(--card); color:var(--ink-2); white-space:nowrap; text-decoration:none; }
  .pill .dot { width:8px; height:8px; border-radius:50%; }
  .pill.active { border-color:var(--good); color:var(--good); background:var(--good-bg); }
  .pill.active .dot { background:var(--good); }
  .pill.down { border-color:var(--bad); color:var(--bad); background:var(--bad-bg); }
  .pill.down .dot { background:var(--bad); }
  .pill.paperm { border-color:var(--teal); color:var(--teal); background:var(--teal-faint); }
  .pill.paperm .dot { background:var(--teal); }
  /* Real-money accent -- deliberately the only heat-coloured, heavier-bordered
     surface on the page, so "money is at stake here" is legible at a glance
     rather than something you read your way into. */
  .pill.livem { border-color:var(--heat); color:var(--heat); background:transparent; font-weight:700; }
  .pill.livem .dot { background:var(--heat); }
  /* Region nav: the current region renders as an inert, muted pill (no href,
     nothing to click into) so it never reads as a live link back to itself;
     the other regions stay ordinary clickable pills. */
  .pill.regioncur { background:var(--paper); color:var(--muted); cursor:default; }
  .card.money, .tile.money { border-color:var(--heat); border-width:2px; }
  .card.money h2 { color:var(--heat); }
  .moneybar { display:flex; align-items:center; gap:16px; padding:14px 16px; border-radius:8px;
    background:var(--paper); border:1px solid var(--heat); }
  .moneybar.flat { border-style:dashed; border-color:var(--line); }
  .mb-amt { font-family:var(--mono); font-size:30px; font-variant-numeric:tabular-nums;
    color:var(--heat); line-height:1; }
  .moneybar.flat .mb-amt { color:var(--muted); }
  .mb-lab { font-size:13px; color:var(--ink-2); }
  .mb-sub { font-size:12px; color:var(--muted); }
  .badge.live { background:var(--heat); color:var(--card); }
  .badge.paper { background:var(--paper); color:var(--muted); border:1px solid var(--line); }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px 12px; }
  .tile .label { font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin-bottom:6px; }
  .tile .value { font-family:var(--mono); font-size:26px; font-variant-numeric:tabular-nums; line-height:1.1; }
  .tile .value.dim { color:var(--muted); }
  .tile .note { font-size:12px; color:var(--muted); margin-top:4px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px 20px; }
  .card h2 { font-size:15px; margin:0 0 2px; }
  .card .cap { font-size:12.5px; color:var(--muted); margin:0 0 14px; }
  .strip { position:relative; margin-top:26px; }
  .striphead { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:8px; }
  .striphead b { font-family:var(--mono); color:var(--teal); font-size:12.5px; }
  .stripstations { font-size:12px; color:var(--ink-2); }
  .stripclock { margin-left:auto; font-size:13px; color:var(--ink); font-variant-numeric:tabular-nums; }
  .segments { display:flex; height:44px; border-radius:6px; overflow:hidden; gap:2px; background:var(--paper); }
  .seg.closed { background:var(--line); } .seg.primary { background:var(--teal); }
  .seg.decay { background:var(--teal-soft); } .seg.monitor { background:var(--teal-faint); }
  .seglabels { display:flex; gap:2px; margin-top:8px; }
  .seglabel { font-size:11.5px; color:var(--ink-2); line-height:1.3; min-width:0; }
  .seglabel b { display:block; font-family:var(--mono); font-size:11px; color:var(--ink); }
  .hours { display:flex; justify-content:space-between; font-family:var(--mono); font-size:10.5px; color:var(--muted); margin-top:10px; }
  /* .nowmark was a single #nowmark id when there was one Singapore-time
     strip; now there's one marker per timezone group, so it's a class and
     each strip's marker gets its own generated id (see _nowmark_groups). */
  .nowmark { position:absolute; top:-22px; height:78px; width:2px; background:var(--heat); border-radius:1px; }
  .nowmark::after { content:"now"; position:absolute; top:-2px; left:6px; font-family:var(--mono);
    font-size:10.5px; color:var(--heat); white-space:nowrap; }
  .countdown { margin-top:16px; font-size:13.5px; color:var(--ink-2); }
  .countdown b { font-family:var(--mono); color:var(--heat); font-variant-numeric:tabular-nums; }
  .badge { font-family:var(--mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase;
    padding:2px 8px; border-radius:4px; }
  .badge.mature { background:var(--good-bg); color:var(--good); }
  .badge.immature { background:var(--warn-bg); color:var(--warn); }
  .badge.veto { background:var(--bad-bg); color:var(--bad); }
  .badge.fallback { background:var(--warn-bg); color:var(--warn); }
  .probes { display:grid; grid-template-columns:repeat(auto-fit,minmax(215px,1fr)); gap:8px; }
  .probe { display:flex; align-items:center; gap:8px; padding:8px 12px; border-radius:6px;
    border:1px solid var(--line); background:var(--paper); font-size:12px; min-width:0; }
  .pdot { width:9px; height:9px; border-radius:50%; flex:none; }
  .p-ok .pdot { background:var(--good); }
  .p-degraded .pdot { background:var(--warn); }
  .p-down .pdot { background:var(--bad); }
  .p-down { border-color:var(--bad); }
  .pname { color:var(--ink); font-weight:600; white-space:nowrap; }
  .pdetail { font-family:var(--mono); font-size:10.5px; color:var(--muted); margin-left:auto;
    white-space:nowrap; }
  .tablewrap { overflow-x:auto; }
  /* Daily P&L calendar. min-width keeps a 7-column grid readable on a phone by
     letting .tablewrap scroll it, rather than crushing seven cells into 320px. */
  .cal { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; min-width:560px; }
  .calhead { font-family:var(--mono); font-size:10px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); text-align:center; padding-bottom:2px; }
  .cell { border:1px solid var(--line); border-radius:5px; padding:6px 7px 5px; min-height:64px;
    background:var(--card); display:flex; flex-direction:column; gap:1px; }
  .cell.blank { border:none; background:transparent; min-height:0; }
  .cell.void { border-style:dashed; opacity:.4; }
  .cell .d { font-family:var(--mono); font-size:10.5px; color:var(--muted); }
  .cell .amt { font-family:var(--mono); font-size:13.5px; font-variant-numeric:tabular-nums;
    line-height:1.2; letter-spacing:-.02em; }
  .cell .n { font-size:10.5px; color:var(--muted); margin-top:auto; }
  /* The class carries a flat fallback colour and the inline style mixes the
     real intensity on top -- a browser without color-mix() still shows every
     day's SIGN, just not its magnitude. */
  .cell.gain { background:var(--good-bg); border-color:var(--good); }
  .cell.loss { background:var(--bad-bg); border-color:var(--bad); }
  .cell.gain .amt { color:var(--good); }
  .cell.loss .amt { color:var(--bad); }
  .cell.gain .n, .cell.loss .n { color:var(--ink-2); }
  .cell.flat .amt { color:var(--muted); }
  .cell.today { outline:2px solid var(--heat); outline-offset:1px; }
  .calmonth { display:flex; align-items:baseline; gap:12px; margin:20px 0 8px;
    font-family:var(--mono); font-size:12px; color:var(--ink-2); }
  .calmonth b { color:var(--ink); font-size:12.5px; }
  .calmonth .mn { color:var(--muted); font-size:11px; }
  .calmonth .mtot { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:700; }
  .calmonth .mtot.pos { color:var(--good); } .calmonth .mtot.neg { color:var(--bad); }
  .calmonth:first-child { margin-top:0; }
  .calstats { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:14px; font-size:12.5px;
    color:var(--ink-2); }
  .calstats b { font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--ink); }
  .calstats b.pos { color:var(--good); } .calstats b.neg { color:var(--bad); }
  .evstation { font-family:var(--mono); font-size:12px; letter-spacing:.03em; color:var(--ink-2);
    margin:16px 0 6px; }
  .evstation:first-child { margin-top:0; }
  .ptable { border-collapse:collapse; width:100%; font-size:12.5px; }
  .ptable th { text-align:left; font-size:10.5px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); font-weight:600; padding:6px 12px 6px 0; border-bottom:1px solid var(--line);
    white-space:nowrap; }
  .ptable td { padding:8px 12px 8px 0; border-bottom:1px solid var(--line); white-space:nowrap;
    color:var(--ink-2); }
  .ptable tr:last-child td { border-bottom:none; }
  .ptable .mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .ptable th.num, .ptable td.num { text-align:right; }
  .ptable .pos { color:var(--good); } .ptable .neg { color:var(--bad); }
  .ptable .dim2 { color:var(--muted); }
  .ptable .sub { font-size:9.5px; color:var(--muted); margin-left:4px; }
  .st { font-family:var(--mono); font-size:10.5px; letter-spacing:.05em; text-transform:uppercase;
    padding:2px 8px; border-radius:4px; white-space:nowrap; }
  .st-open { background:var(--teal-faint); color:var(--teal); }
  .st-good { background:var(--good-bg); color:var(--good); }
  .st-bad { background:var(--bad-bg); color:var(--bad); }
  .st-warn { background:var(--warn-bg); color:var(--warn); }
  .st-neutral { background:var(--paper); color:var(--muted); border:1px solid var(--line); }
  .empty { font-size:13.5px; color:var(--muted); background:var(--paper);
    border:1px dashed var(--line); border-radius:6px; padding:16px 18px; }
  .callout { border:1px solid var(--warn); background:var(--warn-bg); border-radius:8px;
    padding:14px 18px; font-size:13.5px; color:var(--ink-2); }
  .callout b { color:var(--warn); font-family:var(--mono); font-size:12px; letter-spacing:.08em;
    text-transform:uppercase; display:block; margin-bottom:4px; }
  .log { font-family:var(--mono); font-size:12px; line-height:1.7; background:var(--paper);
    border:1px solid var(--line); border-radius:6px; padding:12px 14px; overflow-x:auto;
    white-space:pre; color:var(--ink-2); }
  footer { font-size:12.5px; color:var(--muted); text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="headrow">
      <div>
        <div class="eyebrow">polyweather &middot; EC2 ap-southeast-5</div>
        <h1>Trading monitor</h1>
        <p class="sub">Polymarket temperature brackets &middot; @@STATIONCOUNT@@ stations across @@REGIONLABEL@@ &middot; generated @@SNAP@@</p>
      </div>
      <div class="pills">
        <span class="pill @@PILLCLS@@"><span class="dot"></span>@@PILLTXT@@</span>
        @@MODEPILL@@
        @@REGIONNAV@@
        <a class="pill" href="backtest.html">backtest lab &rarr;</a>
      </div>
    </div>
  </header>

  <div class="tiles">
    <div class="tile"><div class="label">Uptime</div><div class="value" id="uptime">&mdash;</div>
      <div class="note">day @@RUNDAY@@ of the 14&ndash;28 day run</div></div>
    <div class="tile"><div class="label">Restarts</div><div class="value">@@RESTARTS@@</div>
      <div class="note">systemd, auto-restart armed</div></div>
    <div class="tile"><div class="label">Open positions</div><div class="value">@@OPEN@@</div>
      <div class="note">paper, @@STATIONCOUNT@@ stations</div></div>
    <div class="tile"><div class="label">Closed positions</div><div class="value">@@CLOSED@@</div>
      <div class="note">what the report scores</div></div>
    <div class="tile"><div class="label">Paper P&amp;L</div><div class="value @@PNLDIM@@">@@PNL@@</div>
      <div class="note">@@PNLNOTE@@</div></div>
    <div class="tile money"><div class="label">Real money at risk</div>
      <div class="value @@LIVEDIM@@">@@LIVEATRISK@@</div>
      <div class="note">@@LIVENOTE@@</div></div>
  </div>

  <div class="card money">
    <h2>Real money</h2>
    <p class="cap">@@REALMONEYCAP@@</p>
    @@REALMONEY@@
  </div>

  <div class="card">
    <h2>Live feeds</h2>
    <p class="cap">@@PROBESCAP@@ &middot; probed from this instance at generation time</p>
    @@PROBES@@
  </div>

  <div class="card">
    <h2>Trading day &mdash; by timezone group</h2>
    <p class="cap">Simplified view of the scheduler&rsquo;s windows, one strip per distinct
      utc_offset_hours in the registry (currently spans UTC+5 through UTC+9). Same local
      pattern in every group: entries open 05:00&ndash;08:00 and nothing new is taken after
      that; the stop-loss tightens at 10:00 for whatever is still open.</p>
    @@TIMESTRIPS@@
    <p class="countdown">Next entry window opens in <b id="countdown">&mdash;</b> (soonest 05:00 across all groups).</p>
  </div>

  <div class="card">
    <h2>Stations</h2>
    <p class="cap">@@STATIONSCAP@@</p>
    @@STATIONTABLE@@
  </div>

  <div class="card">
    <h2>Daily P&amp;L</h2>
    <p class="cap">@@PNLCALCAP@@</p>
    @@PNLCALENDAR@@
  </div>

  <div class="card">
    <h2>Edge &mdash; latest EV table</h2>
    <p class="cap">@@EVCAP@@</p>
    @@EVCARD@@
  </div>

  <div class="card">
    <h2>Model vs market</h2>
    <p class="cap">@@CALIBCAP@@</p>
    @@CALIBCARD@@
  </div>

  <div class="card">
    <h2>Paper positions</h2>
    <p class="cap">@@POSCAP@@</p>
    @@POSTABLE@@
  </div>

  @@WARNINGS@@

  <div class="card">
    <h2>Recent activity</h2>
    <p class="cap">journalctl -u polyweather &middot; last 14 lines</p>
    <div class="log">@@JOURNAL@@</div>
  </div>

  <footer>Regenerated on the instance every 5 minutes; page reloads itself every 5 minutes.
    Clock, &ldquo;now&rdquo; marker, and countdown are live.</footer>
</div>

<script>
  var DEPLOY_UTC = @@DEPLOYMS@@;
  // One entry per distinct utc_offset_hours group in config.STATIONS (see
  // the Python _nowmark_groups build above) -- replaces the single
  // hardcoded "+8 hours" assumption that only ever matched Singapore/KL.
  var NOWMARK_GROUPS = @@NOWMARKGROUPS@@;
  // Soonest upcoming 05:00 local across ALL groups, computed once
  // server-side at generation time (Tokyo's UTC+9 window opens before
  // Singapore's UTC+8, which opens before Karachi's UTC+5) -- the client
  // just counts down to this fixed instant instead of re-deriving "today's
  // 5am" from a single assumed offset.
  var COUNTDOWN_TARGET_MS = @@COUNTDOWNMS@@;
  function fmt(n) { return String(n).padStart(2, "0"); }
  function tick() {
    var nowMs = Date.now();
    NOWMARK_GROUPS.forEach(function (g) {
      var local = new Date(nowMs + g.offset * 3600e3);
      var frac = (local.getUTCHours() + local.getUTCMinutes() / 60 + local.getUTCSeconds() / 3600) / 24;
      var markEl = document.getElementById(g.markId);
      if (markEl) markEl.style.left = "calc(" + (frac * 100).toFixed(3) + "% - 1px)";
      var clockEl = document.getElementById(g.clockId);
      if (clockEl) clockEl.textContent = fmt(local.getUTCHours()) + ":" + fmt(local.getUTCMinutes());
    });
    if (DEPLOY_UTC > 0) {
      var up = Math.max(0, nowMs - DEPLOY_UTC);
      var uh = Math.floor(up / 3600e3), um = Math.floor(up / 60e3) % 60;
      document.getElementById("uptime").textContent =
        uh >= 48 ? Math.floor(uh / 24) + "d " + (uh % 24) + "h" : uh + "h " + fmt(um) + "m";
    }
    var dt = Math.max(0, COUNTDOWN_TARGET_MS - nowMs);
    document.getElementById("countdown").textContent =
      Math.floor(dt / 3600e3) + "h " + fmt(Math.floor(dt / 60e3) % 60) + "m";
  }
  tick();
  setInterval(tick, 30e3);
</script>
</body>
</html>
"""

page = (
    page.replace("@@SNAP@@", html.escape(snap_sgt))
    .replace("@@PILLCLS@@", pill_cls)
    .replace("@@PILLTXT@@", pill_txt)
    .replace("@@RUNDAY@@", str(int(run_days)))
    .replace("@@RESTARTS@@", html.escape(restarts))
    .replace("@@OPEN@@", tile_val(open_n))
    .replace("@@CLOSED@@", tile_val(closed_n))
    .replace("@@PNL@@", pnl_display)
    .replace("@@PNLNOTE@@", pnl_note)
    .replace("@@PNLDIM@@", "dim" if pnl_display == "&mdash;" else "")
    .replace("@@PROBESCAP@@", probes_cap)
    .replace("@@PROBES@@", probes_html)
    .replace("@@EVCAP@@", ev_cap)
    .replace("@@EVCARD@@", ev_html)
    .replace("@@CALIBCAP@@", calib_cap)
    .replace("@@CALIBCARD@@", calib_html)
    .replace("@@POSCAP@@", positions_cap)
    .replace("@@POSTABLE@@", positions_html)
    .replace("@@MODEPILL@@", mode_pill)
    .replace("@@REGIONNAV@@", region_nav_html)
    .replace("@@REGIONLABEL@@", html.escape(region_label))
    .replace("@@LIVEATRISK@@", live_at_risk_display)
    .replace("@@LIVEDIM@@", "" if live_open else "dim")
    .replace("@@LIVENOTE@@", live_note)
    .replace("@@REALMONEYCAP@@", realmoney_cap)
    .replace("@@REALMONEY@@", realmoney_html)
    .replace("@@WARNINGS@@", warn_html)
    .replace("@@JOURNAL@@", html.escape(journal))
    .replace("@@DEPLOYMS@@", str(deploy_epoch_ms))
    .replace("@@STATIONCOUNT@@", str(station_count))
    .replace("@@STATIONSCAP@@", stations_cap)
    .replace("@@STATIONTABLE@@", stations_table_html)
    .replace("@@PNLCALCAP@@", pnl_cal_cap)
    .replace("@@PNLCALENDAR@@", pnl_calendar_html)
    .replace("@@TIMESTRIPS@@", time_strips_html)
    .replace("@@NOWMARKGROUPS@@", nowmark_groups_json)
    .replace("@@COUNTDOWNMS@@", str(countdown_target_ms))
)

tmp = OUT + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(page)
os.replace(tmp, OUT)
print(f"dashboard written to {OUT} ({len(page)} bytes)")
