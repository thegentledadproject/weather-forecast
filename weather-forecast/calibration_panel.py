"""
calibration_panel.py

PURPOSE
-------
One row per station of the three numbers an operator actually wants while
the book is running: the station's MEASURED forecast bias, the EV the
engine is looking at right now, and how the model has scored against the
market on entries that have already settled.

All three already existed. None of them was visible anywhere during a
trading day:

  * the bias is computed on every entry cycle (entry_manager.
    forecast_bias_stats) and then only ever consumed by calibration;
  * the EV table is snapshotted per station (ev_engine.save_ev_snapshot)
    and shown, but not next to what the model has been worth;
  * model-vs-market lived in promotion_dossier.py, a per-station CLI
    assembled for a promotion decision and run twice, ever.

WHY THE ARITHMETIC IS HERE AND THE HTML IS (MOSTLY) HERE TOO
------------------------------------------------------------
deploy/generate_dashboard.py renders a page at IMPORT time, which is why
it has never had a test. Putting these numbers in the generator would put
them beyond the reach of exactly the checks they need. This module is
importable, has no import-time side effects, and is tested; the generators
wrap render_table_html() in their own card.

Nothing is recomputed here. promotion_dossier owns the scoring, and this
calls it -- so the panel and the promotion decision can never quietly
diverge into two different definitions of "beats the market".

THE REPORTING RULES THIS MODULE EXISTS TO HOLD
-----------------------------------------------
A dashboard cell is where a careful measurement turns into a wrong
impression, so three rules are load-bearing rather than cosmetic:

  1. NO EMPTY BOOK MAY PRINT A NUMBER. A Brier of 0.0 is a PERFECT score.
     live_calibration() returns None rather than zeros for exactly this
     reason, and every None here renders as an em dash.
  2. n AND n_days ALWAYS TRAVEL WITH A GAP. Every entry taken on one
     station-day settles off the same weather, so n overstates the
     evidence and n_days is the honest ceiling. A gap without its n_days
     is the number most likely to be over-read.
  3. NO VERDICT. The dossier deliberately recommends nothing, because a
     printed verdict launders an operator judgement into what looks like
     an arithmetic result. This prints what is measured and marks whether
     the gap clears its own error bar; it does not say "beats the market".

DEPENDENCIES
------------
json, datetime, html, statistics (standard library)
config.py, entry_manager.py, promotion_dossier.py (local)
"""

import html
import json
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import bucket_axis
import calibration
import config
import entry_manager
import promotion_dossier

# Days in the "recent" column. Long enough that a station with a normal
# entry rate has more than a couple of independent days in it, short enough
# that a regime change (a station stopped, a bias correction landing) shows
# up instead of being averaged into weeks of history.
RECENT_DAYS = 14

_EM_DASH = "&mdash;"


def _bias(station_icao: str) -> dict:
    """
    (bias_c, n_pairs, stderr) as forecast_bias_stats reports it, kept as
    None rather than coerced. "No correction measured" and "measured, and
    it is zero" are different facts about a station, and only one of them
    is a reason to trade it.
    """
    bias_c, n, stderr = entry_manager.forecast_bias_stats(station_icao)
    return {"c": bias_c, "n": n, "stderr": stderr}


def _tradeable(row: dict) -> bool:
    """
    Whether an EV row is a candidate the entry path would even look at.

    NET EV DIVIDES RAW EDGE BY PRICE, so a near-zero price turns any stale
    model disagreement into a "+21,517% EV" phantom. The trading screen
    drops those (config.EV_MIN_PRICE_SCREEN) and so does the EV card on
    every page -- a comment in generate_dashboard.py records that the page
    once ranked exactly those phantoms at the top of its table.

    This panel shipped without the screen on 2026-09-02 and put them
    straight back, on three pages, in a column headed "Best net EV now".
    The screens live in config for this reason: the dashboard keeping its
    own idea of what counts as an opportunity is a mistake this codebase
    has now made three times.

    A row with no price recorded is KEPT -- an older snapshot predates the
    field, and screening on a value that was never written would silently
    empty the column rather than report what the engine computed.
    """
    if row.get("net_ev_per_dollar") is None:
        return False

    price = row.get("market_price")
    if price is not None and price < config.EV_MIN_PRICE_SCREEN:
        return False

    edge = row.get("raw_edge")
    if edge is not None and price is not None:
        if abs(edge) > config.max_plausible_edge_for(price):
            return False

    return True


def _ev(station_icao: str, now: datetime) -> Optional[dict]:
    """
    The best net-EV row in this station's latest EV snapshot, and how old
    that snapshot is.

    Returns None when NO snapshot exists, and a row with net_ev=None when
    one exists but is empty. ev_engine writes a snapshot even for an empty
    computation precisely so "computed at 05:01 and found nothing" stays
    distinguishable from "never computed", and collapsing the two here
    would throw that away at the last step.
    """
    path = config.DATA_DIR / f"ev_latest_{station_icao}.json"
    if not path.exists():
        return None

    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    age_s = None
    generated_at = payload.get("generated_at")
    if generated_at:
        try:
            stamped = datetime.fromisoformat(generated_at)
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=timezone.utc)
            age_s = (now - stamped).total_seconds()
        except ValueError:
            age_s = None

    results = payload.get("results") or []
    priced = [r for r in results if _tradeable(r)]
    best = max(priced, key=lambda r: r["net_ev_per_dollar"]) if priced else None

    return {
        "net_ev": best["net_ev_per_dollar"] if best else None,
        "bucket_c": best.get("bucket_c") if best else None,
        "side": best.get("side") if best else None,
        "target_date": payload.get("target_date"),
        "age_s": age_s,
    }


def _max_attainable_prob(station_icao: str) -> Optional[dict]:
    """
    The most probability the model could EVER place in one bucket, given
    this station's real bucket width and its resolved spread:

        p_max = 2 * Phi(half_width_c / sigma) - 1

    sigma is calibration.estimate_std_dev's own answer for this station,
    clamp/floor included -- SPREAD_FLOOR_C is exactly what this column
    exists to make visible, so using anything else would hide it again.
    half_width_c comes from bucket_axis, not a hardcoded 1C: eleven
    Americas stations list 2F buckets (1.111C wide), and assuming 1C would
    silently mis-report every one of them.

    Returns None -- never a fabricated number -- when the station is not
    registered or its spread cannot otherwise be resolved.

    WHERE THIS CAN DISAGREE WITH THE TRADING PATH, and why that is accepted
    here. estimate_std_dev reaches its "ensemble" tier only when handed
    ensemble_members, and this column does not fetch them -- one book call
    per station on every dashboard render, for a number the entry path has
    already computed. So for a station that falls THROUGH the measured tier,
    live resolves "ensemble" while this column reports the next tier down
    (pooled_error, then the flat constant).

    That is a real divergence and the `source` returned alongside p_max is
    what exposes it: a row whose source reads "pooled_error" while the
    station trades on an ensemble spread is reporting a p_max the entry path
    does not use. After the 2026-08-29 tier reorder the measured tier sits
    ABOVE the ensemble, so the common case agrees; this is the exception,
    and it is written down rather than left for someone to rediscover.
    """
    try:
        station = config.get_station(station_icao)
        axis = bucket_axis.for_station(station)
        half_width_c = axis.width_c() / 2
        sigma, source = calibration.estimate_std_dev([], [], station_icao=station_icao)
    except Exception:  # noqa: BLE001 - one column, not the row
        return None

    if not sigma or sigma <= 0:
        return None

    p_max = 2 * (0.5 * (1 + math.erf(half_width_c / (sigma * math.sqrt(2))))) - 1
    return {"p_max": p_max, "sigma": sigma, "source": source, "half_width_c": half_width_c}


def station_row(station_icao: str, now: datetime, recent_days: int = RECENT_DAYS) -> dict:
    """
    One station's row. Scores twice -- all time, then the recent window --
    because the two answer different questions and a station whose recent
    behaviour has changed looks fine on the first alone.
    """
    alltime_entries, _ = promotion_dossier.scorable_entries(station_icao)
    recent_entries, _ = promotion_dossier.scorable_entries(
        station_icao, since=now.date() - timedelta(days=recent_days)
    )
    return {
        "icao": station_icao,
        "bias": _bias(station_icao),
        "ev": _ev(station_icao, now),
        "alltime": promotion_dossier.live_calibration(alltime_entries),
        "recent": promotion_dossier.live_calibration(recent_entries),
        "max_attainable_prob": _max_attainable_prob(station_icao),
        "error": None,
    }


def station_rows(
    station_icaos: List[str],
    now: Optional[datetime] = None,
    recent_days: int = RECENT_DAYS,
) -> Tuple[List[dict], List[str]]:
    """
    (rows, warnings) for a list of stations, in the order given.

    ONE STATION'S FAILURE COSTS ONE ROW. A station whose history cannot be
    read still gets a row saying so, and the panel still renders for
    everything else -- the same fail-soft contract the dashboard sections
    already hold each other to. A panel that vanishes because one station
    has a bad row is worse than a panel with a bad row in it.
    """
    now = now or datetime.now(timezone.utc)
    rows, warnings = [], []
    for icao in station_icaos:
        try:
            rows.append(station_row(icao, now=now, recent_days=recent_days))
        except Exception as exc:  # noqa: BLE001 - one row, not the panel
            warnings.append(f"calibration panel: {icao} unreadable: {exc}")
            rows.append({
                "icao": icao, "bias": {"c": None, "n": None, "stderr": None},
                "ev": None, "alltime": None, "recent": None,
                "max_attainable_prob": None, "error": str(exc),
            })
    return rows, warnings


def _fmt_bias(bias: dict) -> str:
    if bias.get("c") is None:
        return _EM_DASH
    out = f"{bias['c']:+.2f}&deg;C"
    if bias.get("stderr") is not None:
        out += f" &plusmn;{bias['stderr']:.2f}"
    if bias.get("n") is not None:
        out += f" <span class='sub'>(n={bias['n']})</span>"
    return out


def _fmt_age(age_s: Optional[float]) -> str:
    if age_s is None:
        return ""
    minutes = int(age_s // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    return f"{hours:.1f}h" if hours < 24 else f"{hours / 24:.1f}d"


def _fmt_ev(ev: Optional[dict]) -> str:
    if ev is None:
        return f"{_EM_DASH} <span class='sub'>never computed</span>"
    if ev.get("net_ev") is None:
        return f"{_EM_DASH} <span class='sub'>computed, nothing priced</span>"
    label = f"{ev['net_ev']:+.0%}"
    where = f"{ev['bucket_c']}&deg; {html.escape(str(ev.get('side') or ''))}".strip()
    age = _fmt_age(ev.get("age_s"))
    tail = f" <span class='sub'>({age})</span>" if age else ""
    return f"{label} on {where}{tail}"


def _fmt_brier(cal: Optional[dict]) -> str:
    if not cal:
        return _EM_DASH
    return f"{cal['brier_model']:.3f} / {cal['brier_market']:.3f}"


def _fmt_gap(cal: Optional[dict]) -> str:
    """
    The paired gap, its error bar, and the sample it rests on.

    POSITIVE MEANS THE MODEL SCORED BETTER on those entries -- the sign
    convention is live_calibration's, not this module's. `separable` is
    marked, never translated into a verdict: it says the gap is bigger than
    its own error bar, which is not the same as the model being better, and
    the difference matters most exactly where the sample is smallest.
    """
    if not cal:
        return _EM_DASH
    out = f"{cal['mean_gap']:+.3f}"
    if cal.get("gap_stderr") is not None:
        out += f" &plusmn;{cal['gap_stderr']:.3f}"
    else:
        out += " <span class='sub'>(no stderr, n=1)</span>"
    out += f" <span class='sub'>(n={cal['n']}, {cal['n_days']}d)</span>"
    if cal.get("separable"):
        out = f"<strong>{out}</strong>"
    else:
        out = f"<span class='sub'>{out}</span>"
    return out


def _fmt_max_prob(mabp: Optional[dict]) -> str:
    """
    The spread floor's cap on model confidence, made visible: the most
    probability the model could EVER place in one bucket at this station's
    resolved sigma. Source travels with the number -- a cap computed on
    "fallback_default" or "pooled_error" (config.LOW_CONFIDENCE_SPREAD_
    SOURCES) is a station-agnostic guess, not a measurement of this station.
    """
    if not mabp:
        return _EM_DASH
    out = f"{mabp['p_max']:.1%}"
    out += f" <span class='sub'>(&sigma;={mabp['sigma']:.2f} {html.escape(mabp['source'])})</span>"
    return out


def render_table_html(rows: List[dict]) -> str:
    """
    The panel, as one table both dashboards can drop into a card. Uses only
    the .mono/.num/.empty/.sub classes both pages already define -- .dim
    exists on only one of them, and a class the page never styles renders
    as undifferentiated body text on the other.
    """
    if not rows:
        return "<div class='empty'>no stations to score</div>"

    body = []
    for row in rows:
        if row.get("error"):
            body.append(
                f"<tr><td class='mono'>{html.escape(row['icao'])}</td>"
                f"<td colspan='6' class='sub'>unreadable: {html.escape(row['error'])}</td></tr>"
            )
            continue
        body.append(
            "<tr>"
            f"<td class='mono'>{html.escape(row['icao'])}</td>"
            f"<td class='mono num'>{_fmt_bias(row['bias'])}</td>"
            f"<td class='mono num'>{_fmt_ev(row['ev'])}</td>"
            f"<td class='mono num'>{_fmt_brier(row['alltime'])}</td>"
            f"<td class='mono num'>{_fmt_gap(row['alltime'])}</td>"
            f"<td class='mono num'>{_fmt_gap(row['recent'])}</td>"
            f"<td class='mono num'>{_fmt_max_prob(row['max_attainable_prob'])}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>Station</th><th>Bias</th><th>Best net EV now</th>"
        "<th>Brier model / market</th><th>Gap &plusmn;se (all time)</th>"
        f"<th>Gap &plusmn;se (last {RECENT_DAYS}d)</th>"
        "<th>Max attainable bucket p</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )
