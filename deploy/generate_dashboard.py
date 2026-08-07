#!/usr/bin/env python3
"""Render the polyweather status dashboard to /var/www/html/index.html.

Runs on the EC2 instance from a systemd timer (every 5 min). Reads only:
systemd unit state, the journal tail, and the package's own storage layer.
Every data read is fail-soft: a failure shows up ON the page rather than
killing the render.
"""
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

PKG = "/home/ubuntu/weather-forecast/weather-forecast"
OUT = "/var/www/html/index.html"

sys.path.insert(0, PKG)
os.chdir(PKG)

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
pnl_note = "dollar-weighted, closed trades only"
try:
    import config  # noqa: E402
    import storage  # noqa: E402

    open_positions = storage.load_open_positions(is_paper=True)
    closed_positions = []
    for icao in config.STATIONS:
        closed_positions.extend(storage.load_position_history(icao, is_paper=True))
    closed_positions.sort(key=lambda p: p.exit_time or "", reverse=True)
    open_n, closed_n = len(open_positions), len(closed_positions)
except Exception as exc:  # noqa: BLE001
    open_positions, closed_positions = [], []
    warnings.append(f"position read failed: {exc}")

# Registry size, computed once and reused everywhere the page used to say
# "both stations" or hardcode "WSSS & WMKK" -- the count (and every string
# built from it) now tracks config.STATIONS instead of silently going stale
# the next time a station is added or retired from the registry.
station_count = len(config.STATIONS)

station_summaries = {}  # icao -> summarize_paper_performance dict; reused by the station table
if closed_n:
    try:
        import paper_trading_report as ptr  # noqa: E402

        pnl_usd = 0.0
        staked_usd = 0.0
        for icao in config.STATIONS:
            summary = ptr.summarize_paper_performance(icao)
            if isinstance(summary, dict):
                station_summaries[icao] = summary
                pnl_usd += float(summary.get("total_pnl_usd") or 0.0)
                staked_usd += float(summary.get("total_staked_usd") or 0.0)
        # Dollar-weighted: what the bankroll actually experienced, not the
        # per-trade percent sum that overweights $1 lottery tickets.
        pnl_display = f"{'-' if pnl_usd < 0 else '+'}${abs(pnl_usd):,.2f}"
        if staked_usd:
            pnl_note = f"{pnl_usd / staked_usd:+.1%} dollar-weighted on ${staked_usd:,.0f} staked"
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"P&L summary failed: {exc}")

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
        for icao, st in config.STATIONS.items():
            offset_reps.setdefault(st.utc_offset_hours, st)
        rep_stations = list(offset_reps.values())
        primary = rep_stations[0]  # arbitrary anchor for the station-agnostic probes below

        om = (f"?latitude={primary.lat}&longitude={primary.lon}"
              "&daily=temperature_2m_max&timezone=auto&forecast_days=1")

        probes = [
            _probe("Open-Meteo ECMWF", config.OPEN_METEO_ECMWF_URL + om),
            _probe("Open-Meteo ensemble", config.OPEN_METEO_ENSEMBLE_URL + om + "&models=ecmwf_ifs025"),
            _probe("Wunderground", primary.wunderground_history_url, timeout=8),
            # CLOB: reachability only -- a garbage token id SHOULD 404/400; any
            # HTTP answer < 500 proves the trading API is up and talking.
            _probe("CLOB price API", "https://clob.polymarket.com/price?token_id=1&side=buy",
                   ok_when=lambda r: r.status_code < 500),
        ]

        # NEA is Singapore's own government feed, tied to whichever station
        # actually uses the "nea" official client (WSSS today) -- probe it
        # only if such a station is registered, instead of assuming WSSS.
        nea_station = next((s for s in config.STATIONS.values() if s.official_client_key == "nea"), None)
        if nea_station is not None:
            probes.append(_probe("NEA (data.gov.sg)", "https://api.data.gov.sg/v1/environment/24-hour-weather-forecast"))

        # At most ONE WWIS probe -- most of the 13-station registry uses the
        # "wwis" client, and pinging all of them on every 5-minute
        # regeneration would be neither cheap nor informative (one WWIS
        # outage looks like every WWIS station going down at once anyway).
        wwis_station = next(
            (s for s in config.STATIONS.values()
             if s.official_client_key == "wwis" and s.wwis_city_name in _WWIS_CITY_ID_BY_NAME),
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
    for icao in config.STATIONS:
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
    EV_VETO_EDGE = getattr(config, "MAX_PLAUSIBLE_RAW_EDGE", 0.25)
except NameError:  # config import itself failed above; page must still render
    EV_MIN_PRICE, EV_VETO_EDGE = 0.03, 0.25


def _ev_rows(snap):
    rows = []
    priced = [r for r in snap.get("results", []) if r.get("net_ev_per_dollar") is not None]
    unpriced = len(snap.get("results", [])) - len(priced)
    suppressed = 0
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
        flags = ""
        cls = "pos"
        if abs(r["raw_edge"]) > EV_VETO_EDGE:
            flags += " <span class='badge veto'>veto zone</span>"
            cls = ""
        if r.get("spread_source") == "fallback_default":
            flags += " <span class='badge fallback'>fallback est</span>"
        rows.append(
            "<tr>"
            f"<td class='mono'>{html.escape(snap['station_icao'])}</td>"
            f"<td class='mono'>{r['bucket_c']}&deg;C</td>"
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
                   "entry windows (05:00&ndash;10:00 local) and saves its table here from then on.</div>")
        ev_cap = "model edge vs. market, per bucket and side, grouped by station"
except Exception as exc:  # noqa: BLE001
    warnings.append(f"EV card failed: {exc}")
    ev_html, ev_cap = "<div class='empty'>EV view unavailable</div>", ""

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


def _pos_row(p):
    is_open = p.status == "open"
    label, cls = status_label(p.status)
    # Entered/exited times render in THIS position's own station's local
    # time, not a blanket SGT -- a Tokyo (UTC+9) position timestamped in
    # Singapore (UTC+8) time would print an hour off, which is exactly the
    # kind of cross-timezone mislabeling the rest of this expansion (see
    # config.local_today) exists to avoid. Falls back to the legacy +8 if
    # the station isn't (or is no longer) registered.
    try:
        station_offset = config.get_station(p.station_icao).utc_offset_hours
    except KeyError:
        station_offset = 8
    if is_open:
        mark = p.high_water_mark if p.high_water_mark is not None else p.entry_price
        last_price = f"{mark:.2f}<span class='sub'>hwm</span>"
        ret = "&mdash;"
        ret_cls = ""
        when = _hm(p.entry_time, station_offset)
    else:
        last_price = f"{p.exit_price:.2f}" if p.exit_price is not None else "&mdash;"
        if p.exit_price is not None and p.entry_price:
            r = (p.exit_price - p.entry_price) / p.entry_price * 100
            ret = f"{r:+.1f}%"
            ret_cls = "pos" if r >= 0 else "neg"
        else:
            ret, ret_cls = "&mdash;", ""
        when = _hm(p.exit_time, station_offset)
    return (
        "<tr>"
        f"<td class='mono'>{html.escape(p.station_icao)}</td>"
        f"<td class='mono'>{html.escape(str(p.target_date))}</td>"
        f"<td class='mono'>{p.bucket_c}&deg;C</td>"
        f"<td class='mono'>{html.escape(p.side)}</td>"
        f"<td class='mono num'>${p.size_usd:,.0f}</td>"
        f"<td class='mono num'>{p.entry_price:.2f}</td>"
        f"<td class='mono num'>{last_price}</td>"
        f"<td class='mono num {ret_cls}'>{ret}</td>"
        f"<td><span class='st {cls}'>{html.escape(label)}</span></td>"
        f"<td class='mono dim2'>{when}</td>"
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
            "<thead><tr><th>Station</th><th>Market date</th><th>Bucket</th><th>Side</th>"
            "<th class='num'>Size</th><th class='num'>Entry</th><th class='num'>Last/exit</th>"
            "<th class='num'>Return</th><th>Status</th><th>Entered/exited</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>{foot}"
        )
        positions_cap = (
            f"{len(open_positions)} open &middot; {min(len(closed_positions), MAX_CLOSED_SHOWN)} most recent closed"
            " &middot; open rows show high-water mark, not a live quote"
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

now_utc = datetime.now(timezone.utc)
snap_sgt = now_utc.astimezone(timezone.utc).strftime("%d %b %Y, ")
snap_sgt += f"{(now_utc.hour + 8) % 24:02d}:{now_utc.minute:02d} SGT"
run_days = max(0, (now_utc.timestamp() * 1000 - deploy_epoch_ms) / 86400000) if deploy_epoch_ms else 0

service_ok = active_state == "active"
pill_cls = "active" if service_ok else "down"
pill_txt = "service active" if service_ok else f"service {html.escape(active_state)}"

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
    for icao in sorted(config.STATIONS):
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
        rows.append(
            "<tr>"
            f"<td class='mono'>{html.escape(icao)}</td>"
            f"<td>{html.escape(st.display_name)}</td>"
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
        "<thead><tr><th>Station</th><th>Name</th><th>Maturity</th>"
        "<th class='num'>Trades</th><th class='num'>Win rate</th>"
        "<th class='num'>Open</th><th class='num'>At risk</th>"
        "<th class='num'>Realized P&amp;L</th><th class='num'>Return</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


try:
    stations_table_html = _station_table()
    stations_cap = ("closed paper trades per station &middot; Return is dollar-weighted realized P&amp;L "
                    "per dollar staked &middot; At risk sums open paper stakes")
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
# Group stations by utc_offset_hours -- derived from the live registry, never
# hardcoded, so a future station on a not-yet-seen offset gets its own strip
# automatically -- and render one strip per group.
_offset_groups = {}
for _icao, _st in config.STATIONS.items():
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
        <div class="seg decay" style="flex:2"></div>
        <div class="seg monitor" style="flex:14"></div>
      </div>
      <div class="nowmark" id="{_mark_id}"></div>
      <div class="seglabels">
        <div class="seglabel" style="flex:4"><b>00&ndash;04</b>closed &mdash; hard floor</div>
        <div class="seglabel" style="flex:1"><b>04</b>warm-up</div>
        <div class="seglabel" style="flex:3"><b>05&ndash;08</b>primary entries</div>
        <div class="seglabel" style="flex:2"><b>08&ndash;10</b>edge decay</div>
        <div class="seglabel" style="flex:14"><b>10&ndash;24</b>position monitoring only</div>
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
<title>polyweather &mdash; paper trading monitor</title>
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
        <h1>Paper trading monitor</h1>
        <p class="sub">Polymarket temperature brackets &middot; @@STATIONCOUNT@@ stations across Asia &middot; generated @@SNAP@@</p>
      </div>
      <div class="pills">
        <span class="pill @@PILLCLS@@"><span class="dot"></span>@@PILLTXT@@</span>
        <span class="pill paperm"><span class="dot"></span>paper mode &mdash; no live orders</span>
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
      pattern in every group: entries 05:00&ndash;08:00, decaying to nothing by 10:00.</p>
    @@TIMESTRIPS@@
    <p class="countdown">Next entry window opens in <b id="countdown">&mdash;</b> (soonest 05:00 across all groups).</p>
  </div>

  <div class="card">
    <h2>Stations</h2>
    <p class="cap">@@STATIONSCAP@@</p>
    @@STATIONTABLE@@
  </div>

  <div class="card">
    <h2>Edge &mdash; latest EV table</h2>
    <p class="cap">@@EVCAP@@</p>
    @@EVCARD@@
  </div>

  <div class="card">
    <h2>Positions</h2>
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
    .replace("@@POSCAP@@", positions_cap)
    .replace("@@POSTABLE@@", positions_html)
    .replace("@@WARNINGS@@", warn_html)
    .replace("@@JOURNAL@@", html.escape(journal))
    .replace("@@DEPLOYMS@@", str(deploy_epoch_ms))
    .replace("@@STATIONCOUNT@@", str(station_count))
    .replace("@@STATIONSCAP@@", stations_cap)
    .replace("@@STATIONTABLE@@", stations_table_html)
    .replace("@@TIMESTRIPS@@", time_strips_html)
    .replace("@@NOWMARKGROUPS@@", nowmark_groups_json)
    .replace("@@COUNTDOWNMS@@", str(countdown_target_ms))
)

tmp = OUT + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(page)
os.replace(tmp, OUT)
print(f"dashboard written to {OUT} ({len(page)} bytes)")
