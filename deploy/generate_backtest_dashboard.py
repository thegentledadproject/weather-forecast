#!/usr/bin/env python3
"""Render the polyweather backtest lab dashboard to /var/www/html/backtest.html.

Sibling to generate_dashboard.py, same fail-soft philosophy: this script reads
run artifacts written by `python -m backtest.cli run` under --backtests-dir.
Every data read is wrapped so a missing/corrupt file shows up as a warning ON
the page rather than killing the render. Nothing here should ever raise out
to the caller -- worst case is the friendly empty state.
"""
import argparse
import csv
import html
import io
import json
import os
import sys
from datetime import datetime, timezone

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--backtests-dir",
    default="/home/ubuntu/weather-forecast/weather-forecast/data/backtests",
    help="directory containing one subdirectory per backtest run",
)
parser.add_argument(
    "--out",
    default="/var/www/html/backtest.html",
    help="path to write the rendered HTML page to",
)
args = parser.parse_args()

BACKTESTS_DIR = args.backtests_dir
OUT = args.out

warnings = []

MAX_CLOSED_SHOWN = 20
MAX_EQUITY_POINTS = 300


def tile_val(v):
    return "&mdash;" if v is None else str(v)


def _hm(iso_ts):
    """ISO timestamp -> compact 'dd MMM HH:MM' string. Never raises."""
    if not iso_ts:
        return "&mdash;"
    try:
        dt = datetime.fromisoformat(str(iso_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d %b %H:%M")
    except ValueError:
        return html.escape(str(iso_ts)[:16])


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


# --- discover runs ------------------------------------------------------------
def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _discover_runs(backtests_dir):
    """Return a list of dicts: {run_id, dir, summary, mtime}. Tolerant of a
    missing backtests dir, missing summary.json, or corrupt summary.json --
    each failure is logged and either the run is skipped (unreadable) or kept
    with summary=None (present but corrupt/missing, still shown for its other
    artifacts)."""
    runs = []
    try:
        entries = os.listdir(backtests_dir)
    except OSError as exc:
        warnings.append(f"backtests dir unreadable ({backtests_dir}): {exc}")
        return runs

    for name in sorted(entries):
        run_dir = os.path.join(backtests_dir, name)
        if not os.path.isdir(run_dir):
            continue
        summary = None
        summary_path = os.path.join(run_dir, "summary.json")
        if os.path.exists(summary_path):
            try:
                summary = _load_json(summary_path)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"summary.json corrupt for run {name}: {exc}")
        else:
            warnings.append(f"summary.json missing for run {name}")
        try:
            mtime = os.path.getmtime(run_dir)
        except OSError:
            mtime = 0
        runs.append({"run_id": name, "dir": run_dir, "summary": summary, "mtime": mtime})
    return runs


def _run_sort_key(run):
    summary = run["summary"] or {}
    gen = summary.get("generated_at")
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            warnings.append(f"generated_at unparseable for run {run['run_id']}: {gen}")
    return run["mtime"]


runs = _discover_runs(BACKTESTS_DIR)
runs.sort(key=_run_sort_key, reverse=True)
latest = runs[0] if runs else None

# --- per-run artifact loaders ---------------------------------------------------
def _load_equity(run_dir):
    path = os.path.join(run_dir, "equity.csv")
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    rows.append((float(r["ts"]), float(r["equity"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"equity.csv unreadable: {exc}")
        return []
    return rows


def _load_positions(run_dir):
    path = os.path.join(run_dir, "positions.jsonl")
    if not os.path.exists(path):
        warnings.append(f"positions.jsonl missing for run {os.path.basename(run_dir)}")
        return []
    positions = []
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    positions.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    warnings.append(f"positions.jsonl line {lineno} corrupt: {exc}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"positions.jsonl unreadable: {exc}")
        return []
    return positions


def _load_manifest(run_dir):
    path = os.path.join(run_dir, "manifest.json")
    if not os.path.exists(path):
        return None
    try:
        return _load_json(path)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"manifest.json corrupt for run {os.path.basename(run_dir)}: {exc}")
        return None


# Validate the secondary artifacts (positions.jsonl, manifest.json) of every
# discovered run, not just the latest -- a run with a missing/corrupt file
# still shows up in the "All runs" table, and its data-quality problems still
# belong in the warnings callout. Full contents are only kept for `latest`
# (loaded further down); for the rest this is a validate-and-discard pass.
for _run in runs:
    if _run is latest:
        continue
    _load_positions(_run["dir"])
    _load_manifest(_run["dir"])


# --- SVG equity curve ------------------------------------------------------------
def _downsample(rows, n):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = []
    for i in range(n):
        out.append(rows[int(i * step)])
    if out[-1] != rows[-1]:
        out.append(rows[-1])
    return out


def _equity_svg(rows, starting_bankroll):
    if not rows:
        return "<div class='empty'>No equity curve for this run &mdash; equity.csv missing or empty.</div>"
    try:
        rows = sorted(rows, key=lambda r: r[0])
        rows = _downsample(rows, MAX_EQUITY_POINTS)
        values = [v for _, v in rows]
        vmin, vmax = min(values), max(values)
        if vmax == vmin:
            vmax = vmin + 1.0
        W, H, PAD = 900, 220, 24
        n = len(rows)
        pts = []
        for i, (_, v) in enumerate(rows):
            x = PAD + (W - 2 * PAD) * (i / max(1, n - 1))
            y = H - PAD - (H - 2 * PAD) * ((v - vmin) / (vmax - vmin))
            pts.append(f"{x:.1f},{y:.1f}")
        polyline = " ".join(pts)

        gridlines = "".join(
            f"<line x1='{PAD}' y1='{H - PAD - (H - 2*PAD)*frac:.1f}' "
            f"x2='{W - PAD}' y2='{H - PAD - (H - 2*PAD)*frac:.1f}' "
            "stroke='var(--line)' stroke-width='1' />"
            for frac in (0, 0.25, 0.5, 0.75, 1.0)
        )

        ref_line = ""
        if starting_bankroll is not None and vmin <= starting_bankroll <= vmax:
            ry = H - PAD - (H - 2 * PAD) * ((starting_bankroll - vmin) / (vmax - vmin))
            ref_line = (
                f"<line x1='{PAD}' y1='{ry:.1f}' x2='{W - PAD}' y2='{ry:.1f}' "
                "stroke='var(--muted)' stroke-width='1' stroke-dasharray='4,4' />"
            )

        svg = (
            f"<svg viewBox='0 0 {W} {H}' xmlns='http://www.w3.org/2000/svg' "
            "style='width:100%;height:auto;' role='img' aria-label='equity curve'>"
            f"{gridlines}{ref_line}"
            f"<polyline points='{polyline}' fill='none' stroke='var(--teal)' "
            "stroke-width='2' stroke-linejoin='round' stroke-linecap='round' />"
            f"<text x='{PAD}' y='14' font-family='var(--mono)' font-size='11' fill='var(--muted)'>"
            f"${vmax:,.0f}</text>"
            f"<text x='{PAD}' y='{H - 8}' font-family='var(--mono)' font-size='11' fill='var(--muted)'>"
            f"${vmin:,.0f}</text>"
            "</svg>"
        )
        return svg
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"equity curve render failed: {exc}")
        return "<div class='empty'>equity curve unavailable</div>"


# --- render one run's full page --------------------------------------------------
def _fmt_pct(fraction, signed=True):
    """FRACTIONS where 0.5 means 50% -- multiply by 100 before display.
    See commit 6efea80 on the sibling dashboard for the bug this guards."""
    if fraction is None:
        return "&mdash;"
    try:
        v = float(fraction) * 100
    except (TypeError, ValueError):
        return "&mdash;"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _closed_position_row(p):
    label, cls = status_label(p.get("status"))
    entry_price = p.get("entry_price")
    exit_price = p.get("exit_price")
    if exit_price is not None and entry_price:
        try:
            r = (float(exit_price) - float(entry_price)) / float(entry_price) * 100
            ret = f"{r:+.1f}%"
            ret_cls = "pos" if r >= 0 else "neg"
        except (TypeError, ValueError, ZeroDivisionError):
            ret, ret_cls = "&mdash;", ""
    else:
        ret, ret_cls = "&mdash;", ""
    size_usd = p.get("size_usd")
    size_disp = f"${float(size_usd):,.0f}" if size_usd is not None else "&mdash;"
    entry_disp = f"{float(entry_price):.2f}" if entry_price is not None else "&mdash;"
    exit_disp = f"{float(exit_price):.2f}" if exit_price is not None else "&mdash;"
    bucket = p.get("bucket_c")
    bucket_disp = f"{bucket}&deg;C" if bucket is not None else "&mdash;"
    return (
        "<tr>"
        f"<td class='mono'>{html.escape(str(p.get('station_icao') or '&mdash;'))}</td>"
        f"<td class='mono'>{html.escape(str(p.get('target_date') or '&mdash;'))}</td>"
        f"<td class='mono'>{bucket_disp}</td>"
        f"<td class='mono'>{html.escape(str(p.get('side') or '&mdash;'))}</td>"
        f"<td class='mono num'>{size_disp}</td>"
        f"<td class='mono num'>{entry_disp}</td>"
        f"<td class='mono num'>{exit_disp}</td>"
        f"<td class='mono num {ret_cls}'>{ret}</td>"
        f"<td><span class='st {cls}'>{html.escape(label)}</span></td>"
        f"<td class='mono dim2'>{_hm(p.get('exit_time'))}</td>"
        "</tr>"
    )


def _run_row(run, is_latest):
    summary = run["summary"] or {}
    metrics = summary.get("metrics") or {}
    params = summary.get("params") or {}
    row_style = " style='background:var(--teal-faint)'" if is_latest else ""
    date_range = f"{summary.get('start_date') or '&mdash;'} &ndash; {summary.get('end_date') or '&mdash;'}"
    return (
        f"<tr{row_style}>"
        f"<td class='mono'>{html.escape(run['run_id'])}</td>"
        f"<td class='mono'>{html.escape(str(summary.get('station_icao') or '&mdash;'))}</td>"
        f"<td class='mono'>{html.escape(date_range)}</td>"
        f"<td class='mono'>{html.escape(str(params.get('depth_regime') or '&mdash;'))}</td>"
        f"<td class='mono num'>{tile_val(metrics.get('n_trades'))}</td>"
        f"<td class='mono num'>{_fmt_pct(metrics.get('total_return_pct_sum'))}</td>"
        f"<td class='mono dim2'>{_hm(summary.get('generated_at'))}</td>"
        "</tr>"
    )


try:
    if not runs:
        empty_reason = (
            f"backtests directory not found: {html.escape(BACKTESTS_DIR)}"
            if not os.path.isdir(BACKTESTS_DIR)
            else "backtests directory has no run subdirectories yet"
        )
        body_html = (
            "<div class='card'><h2>No runs yet</h2>"
            "<p class='cap'>No backtest runs yet &mdash; runs appear here once "
            "<code>python -m backtest.cli run</code> has been executed.</p>"
            f"<div class='empty'>{empty_reason}</div></div>"
        )
        header_pills = "<a class=\"pill\" href=\"index.html\">&larr; live monitor</a>"
        header_sub = "No backtest runs found yet."
        snap_generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    else:
        run = latest
        summary = run["summary"] or {}
        metrics = summary.get("metrics") or {}
        extras = summary.get("extras") or {}
        params = summary.get("params") or {}
        counters = summary.get("counters") or {}
        provenance = summary.get("provenance") or {}
        rejections = counters.get("rejections") or {}
        by_exit_reason = metrics.get("by_exit_reason") or {}

        manifest = _load_manifest(run["dir"])  # noqa: F841 - existence/validity check only
        equity_rows = _load_equity(run["dir"])
        positions = _load_positions(run["dir"])

        depth_regime = html.escape(str(params.get("depth_regime") or "&mdash;"))
        fee_rate = params.get("fee_rate_pct")
        fee_disp = f"{float(fee_rate):.2f}%" if fee_rate is not None else "&mdash;"
        bankroll_mode = html.escape(str(params.get("bankroll_mode") or "&mdash;"))

        pct_live = provenance.get("pct_live_snapshot")
        hifi = isinstance(pct_live, (int, float)) and pct_live >= 0.5
        data_quality_pill = (
            f"<span class='pill hifi'><span class='dot'></span>high-fidelity data</span>"
            if hifi else
            f"<span class='pill coarse'><span class='dot'></span>coarse history data</span>"
        )

        header_pills = (
            f"<span class='pill'><span class='dot'></span>{depth_regime} depth</span>"
            f"<span class='pill'><span class='dot'></span>{html.escape(fee_disp)} fee</span>"
            f"<span class='pill'><span class='dot'></span>{bankroll_mode} bankroll</span>"
            f"{data_quality_pill}"
            "<a class=\"pill\" href=\"index.html\">&larr; live monitor</a>"
        )

        gen_disp = html.escape(_hm(summary.get("generated_at")))
        station_disp = html.escape(str(summary.get("station_icao") or "&mdash;"))
        date_range = (
            f"{html.escape(str(summary.get('start_date') or '&mdash;'))} &ndash; "
            f"{html.escape(str(summary.get('end_date') or '&mdash;'))}"
        )
        header_sub = (
            f"{html.escape(run['run_id'])} &middot; {station_disp} &middot; {date_range} "
            f"&middot; generated {gen_disp}"
        )
        snap_generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

        # -- tiles --
        n_trades_disp = tile_val(metrics.get("n_trades"))
        win_rate_disp = _fmt_pct(metrics.get("win_rate"), signed=False)
        total_return_disp = _fmt_pct(metrics.get("total_return_pct_sum"))
        total_return_dim = "" if metrics.get("total_return_pct_sum") is not None else "dim"
        # extras.max_drawdown_pct: treated as a FRACTION for consistency with every
        # other *_pct field in this payload (win_rate, mean_return_pct, etc all are
        # fractions where 0.5 == 50%); multiply by 100 the same way.
        max_dd_disp = _fmt_pct(extras.get("max_drawdown_pct"), signed=False)
        final_equity = extras.get("final_equity_usd")
        starting_bankroll = extras.get("starting_bankroll_usd")
        final_equity_disp = f"${float(final_equity):,.0f}" if final_equity is not None else "&mdash;"
        starting_bankroll_disp = (
            f"started ${float(starting_bankroll):,.0f}" if starting_bankroll is not None else ""
        )

        brier_model = extras.get("brier_model")
        brier_market = extras.get("brier_market")
        if brier_model is not None and brier_market is not None:
            brier_disp = f"{float(brier_model):.3f} vs {float(brier_market):.3f}"
            beats_market = float(brier_model) < float(brier_market)
            brier_note = "lower is better; beats market" if beats_market else "lower is better; trails market"
            brier_dim = ""
        else:
            brier_disp = "&mdash;"
            brier_note = "lower is better"
            brier_dim = "dim"

        tiles_html = f"""
    <div class="tile"><div class="label">Trades</div><div class="value">{n_trades_disp}</div>
      <div class="note">closed positions</div></div>
    <div class="tile"><div class="label">Win rate</div><div class="value">{win_rate_disp}</div>
      <div class="note">share of trades in profit</div></div>
    <div class="tile"><div class="label">Total return</div><div class="value {total_return_dim}">{total_return_disp}</div>
      <div class="note">summed return, uncompounded</div></div>
    <div class="tile"><div class="label">Max drawdown</div><div class="value">{max_dd_disp}</div>
      <div class="note">peak-to-trough equity</div></div>
    <div class="tile"><div class="label">Final equity</div><div class="value">{final_equity_disp}</div>
      <div class="note">{starting_bankroll_disp}</div></div>
    <div class="tile"><div class="label">Brier (model vs mkt)</div><div class="value {brier_dim}">{brier_disp}</div>
      <div class="note">{brier_note}</div></div>
"""

        # -- equity curve card --
        equity_svg = _equity_svg(equity_rows, starting_bankroll)

        # -- provenance card --
        pct_live_disp = _fmt_pct(pct_live, signed=False)
        pct_depth = provenance.get("pct_with_depth")
        pct_depth_disp = _fmt_pct(pct_depth, signed=False)
        median_fid = provenance.get("median_fidelity_min")
        median_fid_disp = f"{float(median_fid):.1f} min" if median_fid is not None else "&mdash;"
        n_unresolved_disp = tile_val(extras.get("n_unresolved"))
        n_skip_price_disp = tile_val(counters.get("n_skipped_no_price"))
        n_skip_move_disp = tile_val(counters.get("n_skipped_implausible_move"))

        depth_warning = ""
        if isinstance(pct_depth, (int, float)) and pct_depth < 0.5:
            depth_warning = (
                "<div class='callout' style='margin-top:12px'><b>Data quality</b>"
                "headline metrics rest on modeled depth, not observed books.</div>"
            )

        provenance_html = f"""
    <div class="tablewrap"><table class="ptable">
      <tbody>
        <tr><td>Live snapshot vs CLOB history</td><td class="mono num">{pct_live_disp}</td></tr>
        <tr><td>Ticks with real depth</td><td class="mono num">{pct_depth_disp}</td></tr>
        <tr><td>Median fidelity</td><td class="mono num">{median_fid_disp}</td></tr>
        <tr><td>Unresolved positions</td><td class="mono num">{n_unresolved_disp}</td></tr>
        <tr><td>Skipped &mdash; no price</td><td class="mono num">{n_skip_price_disp}</td></tr>
        <tr><td>Skipped &mdash; implausible move</td><td class="mono num">{n_skip_move_disp}</td></tr>
      </tbody>
    </table></div>{depth_warning}
"""

        # -- exit breakdown card --
        if by_exit_reason:
            rows = []
            for reason, stats in sorted(
                by_exit_reason.items(), key=lambda kv: (kv[1] or {}).get("n", 0), reverse=True
            ):
                stats = stats or {}
                n = stats.get("n")
                mean_ret = stats.get("mean_return_pct")
                mean_disp = _fmt_pct(mean_ret)
                mean_cls = ""
                if mean_ret is not None:
                    mean_cls = "pos" if mean_ret >= 0 else "neg"
                rows.append(
                    "<tr>"
                    f"<td class='mono'>{html.escape(str(reason).replace('_', ' '))}</td>"
                    f"<td class='mono num'>{tile_val(n)}</td>"
                    f"<td class='mono num {mean_cls}'>{mean_disp}</td>"
                    "</tr>"
                )
            exit_breakdown_html = (
                "<div class='tablewrap'><table class='ptable'>"
                "<thead><tr><th>Reason</th><th class='num'>N</th><th class='num'>Mean return</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
            )
        else:
            exit_breakdown_html = "<div class='empty'>No exit-reason breakdown for this run.</div>"

        # -- decision funnel card --
        funnel_html = f"""
    <div class="tablewrap"><table class="ptable">
      <tbody>
        <tr><td>Cycles</td><td class="mono num">{tile_val(counters.get('n_cycles'))}</td></tr>
        <tr><td>Entry cycles</td><td class="mono num">{tile_val(counters.get('n_entry_cycles'))}</td></tr>
        <tr><td>Entries</td><td class="mono num">{tile_val(counters.get('n_entries'))}</td></tr>
        <tr><td>Skipped &mdash; no price</td><td class="mono num">{n_skip_price_disp}</td></tr>
        <tr><td>Skipped &mdash; implausible move</td><td class="mono num">{n_skip_move_disp}</td></tr>
      </tbody>
    </table></div>
"""
        if rejections:
            rej_rows = "".join(
                f"<tr><td class='mono'>{html.escape(str(reason).replace('_', ' '))}</td>"
                f"<td class='mono num'>{count}</td></tr>"
                for reason, count in sorted(rejections.items(), key=lambda kv: kv[1], reverse=True)
            )
            funnel_html += (
                "<div class='tablewrap' style='margin-top:12px'><table class='ptable'>"
                "<thead><tr><th>Rejection reason</th><th class='num'>Count</th></tr></thead>"
                f"<tbody>{rej_rows}</tbody></table></div>"
            )

        # -- closed positions card --
        closed = [p for p in positions if (p.get("status") or "") != "open"]
        closed.sort(key=lambda p: p.get("exit_time") or "", reverse=True)
        shown = closed[:MAX_CLOSED_SHOWN]
        omitted = len(closed) - len(shown)
        if shown:
            rows = "".join(_closed_position_row(p) for p in shown)
            foot = (
                f"<p class='cap' style='margin:10px 0 0'>{omitted} older not shown.</p>"
                if omitted > 0 else ""
            )
            positions_html = (
                "<div class='tablewrap'><table class='ptable'>"
                "<thead><tr><th>Station</th><th>Market date</th><th>Bucket</th><th>Side</th>"
                "<th class='num'>Size</th><th class='num'>Entry</th><th class='num'>Exit</th>"
                "<th class='num'>Return</th><th>Status</th><th>Exited</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>{foot}"
            )
        else:
            positions_html = "<div class='empty'>No closed positions for this run.</div>"

        # -- all runs card --
        run_rows = "".join(_run_row(r, r is latest) for r in runs)
        all_runs_html = (
            "<div class='tablewrap'><table class='ptable'>"
            "<thead><tr><th>Run</th><th>Station</th><th>Date range</th><th>Depth</th>"
            "<th class='num'>Trades</th><th class='num'>Total return</th><th>Generated</th></tr></thead>"
            f"<tbody>{run_rows}</tbody></table></div>"
        )

        body_html = f"""
  <div class="tiles">{tiles_html}
  </div>

  <div class="card">
    <h2>Equity curve</h2>
    <p class="cap">Simulated portfolio equity over the run.</p>
    {equity_svg}
  </div>

  <div class="card">
    <h2>Data provenance</h2>
    <p class="cap">How much of this result rests on real market data vs. modeled fallbacks.</p>
    {provenance_html}
  </div>

  <div class="card">
    <h2>Exit breakdown</h2>
    <p class="cap">Closed positions grouped by exit reason.</p>
    {exit_breakdown_html}
  </div>

  <div class="card">
    <h2>Decision funnel</h2>
    <p class="cap">How simulated cycles became entries, and why candidates were rejected.</p>
    {funnel_html}
  </div>

  <div class="card">
    <h2>Closed positions</h2>
    <p class="cap">{len(closed)} closed &middot; {len(shown)} most recent shown</p>
    {positions_html}
  </div>

  <div class="card">
    <h2>All runs</h2>
    <p class="cap">Every discovered run under {html.escape(BACKTESTS_DIR)}, latest first.</p>
    {all_runs_html}
  </div>
"""
except Exception as exc:  # noqa: BLE001 - the dashboard must render regardless
    warnings.append(f"run render failed: {exc}")
    body_html = "<div class='card'><h2>Render error</h2><div class='empty'>backtest lab view unavailable</div></div>"
    header_pills = "<a class=\"pill\" href=\"index.html\">&larr; live monitor</a>"
    header_sub = "Render error &mdash; see warnings below."
    snap_generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

warn_html = ""
if warnings:
    items = "".join(f"<div>&bull; {html.escape(w)}</div>" for w in warnings)
    warn_html = '<div class="callout"><b>Data collection warnings</b>' + items + "</div>"

page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>polyweather &mdash; backtest lab</title>
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
  .pill.hifi { border-color:var(--good); color:var(--good); background:var(--good-bg); }
  .pill.hifi .dot { background:var(--good); }
  .pill.coarse { border-color:var(--warn); color:var(--warn); background:var(--warn-bg); }
  .pill.coarse .dot { background:var(--warn); }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px 12px; }
  .tile .label { font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin-bottom:6px; }
  .tile .value { font-family:var(--mono); font-size:26px; font-variant-numeric:tabular-nums; line-height:1.1; }
  .tile .value.dim { color:var(--muted); }
  .tile .note { font-size:12px; color:var(--muted); margin-top:4px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px 20px; }
  .card h2 { font-size:15px; margin:0 0 2px; }
  .card .cap { font-size:12.5px; color:var(--muted); margin:0 0 14px; }
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
  footer { font-size:12.5px; color:var(--muted); text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="headrow">
      <div>
        <div class="eyebrow">polyweather &middot; backtest lab</div>
        <h1>Backtest results</h1>
        <p class="sub">@@HEADSUB@@</p>
      </div>
      <div class="pills">
        @@HEADPILLS@@
      </div>
    </div>
  </header>

  @@BODY@@

  @@WARNINGS@@

  <footer>Regenerated on the instance every 5 minutes; page reloads itself every 5 minutes.</footer>
</div>
</body>
</html>
"""

page = (
    page.replace("@@HEADSUB@@", header_sub)
    .replace("@@HEADPILLS@@", header_pills)
    .replace("@@BODY@@", body_html)
    .replace("@@WARNINGS@@", warn_html)
)

tmp = OUT + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(page)
os.replace(tmp, OUT)
print(f"backtest dashboard written to {OUT} ({len(page)} bytes)")
