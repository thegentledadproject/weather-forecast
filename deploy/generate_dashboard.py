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
from datetime import date, datetime, timezone

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

if closed_n:
    try:
        import paper_trading_report as ptr  # noqa: E402

        total = 0.0
        for icao in config.STATIONS:
            summary = ptr.summarize_paper_performance(icao)
            if isinstance(summary, dict):
                total += float(summary.get("total_return_pct_sum") or 0.0)
        # summarize_paper_performance returns FRACTIONS (0.5 = 50%)
        pnl_display = f"{total * 100:+.1f}%"
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


def _run_probes():
    try:
        import market_discovery

        wsss = config.get_station("WSSS")
        slug = market_discovery.build_event_slug(wsss, date.today())
        om = f"?latitude={wsss.lat}&longitude={wsss.lon}&daily=temperature_2m_max&timezone=auto&forecast_days=1"
        return [
            _probe("NEA (data.gov.sg)", "https://api.data.gov.sg/v1/environment/24-hour-weather-forecast"),
            _probe("WWIS / MET Malaysia", "https://worldweather.wmo.int/en/json/82_en.json"),
            _probe("Open-Meteo ECMWF", config.OPEN_METEO_ECMWF_URL + om),
            _probe("Open-Meteo ensemble", config.OPEN_METEO_ENSEMBLE_URL + om + "&models=ecmwf_ifs025"),
            _probe("Wunderground", wsss.wunderground_history_url, timeout=8),
            # Gamma: 200 with a non-empty list means today's event is actually listed;
            # 200-but-empty is degraded (API up, market not found), not ok.
            _probe("Gamma API (today's event)", f"https://gamma-api.polymarket.com/events?slug={slug}",
                   ok_when=lambda r: r.status_code == 200 and r.json()),
            # CLOB: reachability only -- a garbage token id SHOULD 404/400; any
            # HTTP answer < 500 proves the trading API is up and talking.
            _probe("CLOB price API", "https://clob.polymarket.com/price?token_id=1&side=buy",
                   ok_when=lambda r: r.status_code < 500),
        ]
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


def _ev_rows(snap):
    rows = []
    priced = [r for r in snap.get("results", []) if r.get("net_ev_per_dollar") is not None]
    unpriced = len(snap.get("results", [])) - len(priced)
    for r in sorted(priced, key=lambda x: x["net_ev_per_dollar"], reverse=True):
        ev = r["net_ev_per_dollar"]
        cls = "pos" if ev >= EV_ENTRY_SCREEN else ""
        rows.append(
            "<tr>"
            f"<td class='mono'>{html.escape(snap['station_icao'])}</td>"
            f"<td class='mono'>{r['bucket_c']}&deg;C</td>"
            f"<td class='mono'>{html.escape(r['side'])}</td>"
            f"<td class='mono num'>{r['model_prob']:.1%}</td>"
            f"<td class='mono num'>{r['market_price']:.3f}</td>"
            f"<td class='mono num'>{r['raw_edge']:+.1%}</td>"
            f"<td class='mono num {cls}'>{ev:+.1%}</td>"
            "</tr>"
        )
    return rows, unpriced


try:
    ev_snaps = _load_ev_snapshots()
    if ev_snaps:
        all_rows, total_unpriced = [], 0
        ages = []
        for snap in ev_snaps:
            rows, unpriced = _ev_rows(snap)
            all_rows.extend(rows)
            total_unpriced += unpriced
            ages.append(snap.get("generated_at", ""))
        newest = max(ages) if ages else ""
        age_note = f"as of {html.escape(newest[11:16])} UTC" if len(newest) >= 16 else ""
        if all_rows:
            ev_html = (
                "<div class='tablewrap'><table class='ptable'>"
                "<thead><tr><th>Station</th><th>Bucket</th><th>Side</th>"
                "<th class='num'>Model p</th><th class='num'>Mkt price</th>"
                "<th class='num'>Raw edge</th><th class='num'>Net EV/$</th></tr></thead>"
                f"<tbody>{''.join(all_rows)}</tbody></table></div>"
            )
            if total_unpriced:
                ev_html += (f"<p class='cap' style='margin:10px 0 0'>{total_unpriced} bucket/side rows "
                            "had no live quote (unseeded far-tail books).</p>")
        else:
            ev_html = "<div class='empty'>Latest EV computation found no priceable buckets.</div>"
        ev_cap = (f"latest computation {age_note} &middot; rows at or above the "
                  f"{EV_ENTRY_SCREEN:.0%} net-EV entry screen highlighted")
    else:
        ev_html = ("<div class='empty'>No EV snapshot yet &mdash; the engine computes during the morning "
                   "entry windows (05:00&ndash;10:00 SGT) and saves its table here from then on.</div>")
        ev_cap = "model edge vs. market, per bucket and side"
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


def _hm(iso_ts):
    """ISO timestamp -> compact 'dd MMM HH:MM' SGT string."""
    if not iso_ts:
        return "&mdash;"
    try:
        dt = datetime.fromisoformat(str(iso_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = dt.timestamp() + 8 * 3600
        d = datetime.fromtimestamp(secs, tz=timezone.utc)
        return d.strftime("%d %b %H:%M")
    except ValueError:
        return html.escape(str(iso_ts)[:16])


def _pos_row(p):
    is_open = p.status == "open"
    label, cls = status_label(p.status)
    if is_open:
        mark = p.high_water_mark if p.high_water_mark is not None else p.entry_price
        last_price = f"{mark:.2f}<span class='sub'>hwm</span>"
        ret = "&mdash;"
        ret_cls = ""
        when = _hm(p.entry_time)
    else:
        last_price = f"{p.exit_price:.2f}" if p.exit_price is not None else "&mdash;"
        if p.exit_price is not None and p.entry_price:
            r = (p.exit_price - p.entry_price) / p.entry_price * 100
            ret = f"{r:+.1f}%"
            ret_cls = "pos" if r >= 0 else "neg"
        else:
            ret, ret_cls = "&mdash;", ""
        when = _hm(p.exit_time)
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
            " 05:00&ndash;08:00 SGT entry window, and only if the EV engine finds edge worth taking.</div>"
        )
        positions_cap = "paper positions, both stations"
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
  .segments { display:flex; height:44px; border-radius:6px; overflow:hidden; gap:2px; background:var(--paper); }
  .seg.closed { background:var(--line); } .seg.primary { background:var(--teal); }
  .seg.decay { background:var(--teal-soft); } .seg.monitor { background:var(--teal-faint); }
  .seglabels { display:flex; gap:2px; margin-top:8px; }
  .seglabel { font-size:11.5px; color:var(--ink-2); line-height:1.3; min-width:0; }
  .seglabel b { display:block; font-family:var(--mono); font-size:11px; color:var(--ink); }
  .hours { display:flex; justify-content:space-between; font-family:var(--mono); font-size:10.5px; color:var(--muted); margin-top:10px; }
  #nowmark { position:absolute; top:-22px; height:78px; width:2px; background:var(--heat); border-radius:1px; }
  #nowmark::after { content:"now"; position:absolute; top:-2px; left:6px; font-family:var(--mono);
    font-size:10.5px; color:var(--heat); white-space:nowrap; }
  .countdown { margin-top:16px; font-size:13.5px; color:var(--ink-2); }
  .countdown b { font-family:var(--mono); color:var(--heat); font-variant-numeric:tabular-nums; }
  .stations { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px; }
  .station h3 { font-size:15px; margin:0; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .station .icao { font-family:var(--mono); color:var(--teal); }
  .badge { font-family:var(--mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase;
    padding:2px 8px; border-radius:4px; }
  .badge.mature { background:var(--good-bg); color:var(--good); }
  .badge.immature { background:var(--warn-bg); color:var(--warn); }
  .station dl { display:grid; grid-template-columns:auto 1fr; gap:4px 14px; margin:12px 0 0; font-size:13px; }
  .station dt { color:var(--muted); } .station dd { margin:0; color:var(--ink-2); }
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
        <p class="sub">Polymarket temperature brackets &middot; WSSS Changi &amp; WMKK KLIA &middot; generated @@SNAP@@</p>
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
      <div class="note">paper, both stations</div></div>
    <div class="tile"><div class="label">Closed positions</div><div class="value">@@CLOSED@@</div>
      <div class="note">what the report scores</div></div>
    <div class="tile"><div class="label">Paper P&amp;L</div><div class="value @@PNLDIM@@">@@PNL@@</div>
      <div class="note">summed return, uncompounded</div></div>
  </div>

  <div class="card">
    <h2>Live feeds</h2>
    <p class="cap">@@PROBESCAP@@ &middot; probed from this instance at generation time</p>
    @@PROBES@@
  </div>

  <div class="card">
    <h2>Trading day &mdash; Singapore time (UTC+8)</h2>
    <p class="cap">Simplified view of the scheduler&rsquo;s windows. The edge lives in the morning:
      entries 05:00&ndash;08:00, decaying to nothing by 10:00.</p>
    <div class="strip">
      <div class="segments">
        <div class="seg closed" style="flex:4"></div>
        <div class="seg closed" style="flex:1;opacity:.55"></div>
        <div class="seg primary" style="flex:3"></div>
        <div class="seg decay" style="flex:2"></div>
        <div class="seg monitor" style="flex:14"></div>
      </div>
      <div id="nowmark"></div>
      <div class="seglabels">
        <div class="seglabel" style="flex:4"><b>00&ndash;04</b>closed &mdash; hard floor</div>
        <div class="seglabel" style="flex:1"><b>04</b>warm-up</div>
        <div class="seglabel" style="flex:3"><b>05&ndash;08</b>primary entries</div>
        <div class="seglabel" style="flex:2"><b>08&ndash;10</b>edge decay</div>
        <div class="seglabel" style="flex:14"><b>10&ndash;24</b>position monitoring only</div>
      </div>
      <div class="hours"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    </div>
    <p class="countdown">Next entry window opens in <b id="countdown">&mdash;</b> (05:00 SGT).</p>
  </div>

  <div class="stations">
    <div class="card station">
      <h3><span class="icao">WSSS</span> Singapore Changi <span class="badge mature">mature</span></h3>
      <dl>
        <dt>Observed history</dt><dd>14+ days, real station readings</dd>
        <dt>Calibration</dt><dd>NEA hot-bias confirmed, 60/40 blend</dd>
        <dt>Sizing</dt><dd>full Kelly path (capped)</dd>
        <dt>Resolution source</dt><dd>Wunderground WSSS history</dd>
      </dl>
    </div>
    <div class="card station">
      <h3><span class="icao">WMKK</span> Kuala Lumpur Intl <span class="badge immature">immature</span></h3>
      <dl>
        <dt>Observed history</dt><dd>accumulating since 31 Jul 2026</dd>
        <dt>Calibration</dt><dd>unverified; MET Malaysia partial stub</dd>
        <dt>Sizing</dt><dd>maturity-gated, reduced</dd>
        <dt>Resolution source</dt><dd>Wunderground WMKK history</dd>
      </dl>
    </div>
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
  function fmt(n) { return String(n).padStart(2, "0"); }
  function tick() {
    var now = new Date(Date.now() + 8 * 3600e3);
    var frac = (now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600) / 24;
    document.getElementById("nowmark").style.left = "calc(" + (frac * 100).toFixed(3) + "% - 1px)";
    if (DEPLOY_UTC > 0) {
      var up = Math.max(0, Date.now() - DEPLOY_UTC);
      var uh = Math.floor(up / 3600e3), um = Math.floor(up / 60e3) % 60;
      document.getElementById("uptime").textContent =
        uh >= 48 ? Math.floor(uh / 24) + "d " + (uh % 24) + "h" : uh + "h " + fmt(um) + "m";
    }
    var target = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 5, 0, 0);
    if (now.getTime() >= target) target += 24 * 3600e3;
    var dt = target - now.getTime();
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
)

tmp = OUT + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(page)
os.replace(tmp, OUT)
print(f"dashboard written to {OUT} ({len(page)} bytes)")
