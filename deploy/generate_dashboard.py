#!/usr/bin/env python3
"""Render the polyweather status dashboard to /var/www/html/index.html.

Runs on the EC2 instance from a systemd timer (every 5 min). Reads only:
systemd unit state, the journal tail, and the package's own storage layer.
Every data read is fail-soft: a failure shows up ON the page rather than
killing the render.
"""
import html
import os
import subprocess
import sys
from datetime import datetime, timezone

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

    total_open = total_closed = 0
    for icao in config.STATIONS:
        try:
            total_open += len(storage.load_open_positions(icao, is_paper=True))
        except TypeError:
            total_open += len(storage.load_open_positions(icao))
        try:
            total_closed += len(storage.load_position_history(icao, is_paper=True))
        except TypeError:
            total_closed += len(storage.load_position_history(icao))
    open_n, closed_n = total_open, total_closed
except Exception as exc:  # noqa: BLE001
    warnings.append(f"position read failed: {exc}")

if closed_n:
    try:
        import paper_trading_report as ptr  # noqa: E402

        total = 0.0
        for icao in config.STATIONS:
            summary = ptr.summarize_paper_performance(icao)
            if isinstance(summary, dict):
                total += float(summary.get("total_return_pct_sum") or 0.0)
        pnl_display = f"{total:+.1f}%"
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"P&L summary failed: {exc}")

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
    background:var(--card); color:var(--ink-2); white-space:nowrap; }
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
    .replace("@@WARNINGS@@", warn_html)
    .replace("@@JOURNAL@@", html.escape(journal))
    .replace("@@DEPLOYMS@@", str(deploy_epoch_ms))
)

tmp = OUT + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(page)
os.replace(tmp, OUT)
print(f"dashboard written to {OUT} ({len(page)} bytes)")
