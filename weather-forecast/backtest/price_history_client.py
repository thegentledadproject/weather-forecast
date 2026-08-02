"""
backtest/price_history_client.py

PURPOSE
-------
Fetches HISTORICAL price ticks for one Polymarket CLOB outcome token
from the /prices-history endpoint, and backfills them into
backtest/price_store.py's sqlite tables. This is the bulk-history
counterpart to clients/market_client.py's live single-point lookups --
it exists purely to seed backtest/replay data, never called from the
live trading path (see ev_engine.py's own, separate live-snapshot
capture for that).

Endpoint: GET https://clob.polymarket.com/prices-history
  params: market=<token_id>, startTs=<unix sec>, endTs=<unix sec>,
          fidelity=<minutes>
  response (documented shape): {"history": [{"t": <unix>, "p": <0..1>}, ...]}
  Tolerated alternate shape: a bare list [{"t": .., "p": ..}, ...] --
  seen on some CLOB responses, handled defensively below.

KNOWN CAVEAT -- COARSE GRANULARITY ON RESOLVED MARKETS
--------------------------------------------------------
Resolved/closed Polymarket markets have been observed to return only
COARSE (>=12 hour) granularity from this endpoint, REGARDLESS of the
`fidelity` value requested. The API does not report back what
granularity it actually gave you, so this module never trusts the
requested fidelity as fact -- it always MEASURES the observed fidelity
from the gaps between returned timestamps (see observed_fidelity_min())
and records that measured value, not the requested one, into
price_store's fidelity_min column. Anything reading this data back for
a backtest MUST treat fidelity_min as "best known resolution of this
data", not "guaranteed sampling interval" -- a 5-minute-fidelity
request against a long-resolved market may still come back as one tick
every 12+ hours, and the honest thing to do is say so, not repeat the
requested number as if it were achieved.

DEPENDENCIES
------------
requests (pip install requests)
backtest/price_store.py, backtest/settings.py (local -- see their own
module docstrings; both are assumed present per the current project
contract even if not yet imported successfully in every environment)
"""

import time
from datetime import date, datetime, timezone
from statistics import median
from typing import List, Optional, Tuple

import requests

import backtest.price_store as price_store
import backtest.settings as settings

PRICES_HISTORY_ENDPOINT = "https://clob.polymarket.com/prices-history"

# Simple fixed min-interval between outbound requests -- keeps this
# module from hammering CLOB during a multi-token, multi-chunk backfill.
# Plain sleep is deliberately simple; no need for a real token bucket at
# this call volume.
_MIN_REQUEST_INTERVAL_SEC = 0.25
_last_request_ts = 0.0

_CHUNK_SECONDS = 7 * 24 * 60 * 60  # <=7-day chunks per request
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SEC = 1.0


def _rate_limit_sleep() -> None:
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    if elapsed < _MIN_REQUEST_INTERVAL_SEC:
        time.sleep(_MIN_REQUEST_INTERVAL_SEC - elapsed)
    _last_request_ts = time.monotonic()


def _parse_history_payload(payload) -> List[Tuple[int, float]]:
    """
    Accepts either {"history": [{"t":, "p":}, ...]} (documented shape)
    or a bare [{"t":, "p":}, ...] list, and returns [(ts, price), ...].
    Skips any entry missing/unparseable t or p rather than failing the
    whole chunk over one bad point.
    """
    if isinstance(payload, dict):
        rows = payload.get("history", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    points = []
    for entry in rows:
        try:
            ts = int(entry["t"])
            price = float(entry["p"])
            points.append((ts, price))
        except (KeyError, ValueError, TypeError):
            continue
    return points


def _fetch_chunk(
    token_id: str,
    chunk_start: int,
    chunk_end: int,
    fidelity_min: int,
    session,
    timeout: int,
) -> List[Tuple[int, float]]:
    """
    Fetch one <=7-day chunk, with up to _MAX_ATTEMPTS retries on
    429/5xx/connection errors, exponential backoff between attempts.
    Never raises -- returns [] and prints a one-line warning if every
    attempt for this chunk fails.
    """
    requester = session or requests

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _rate_limit_sleep()
        try:
            resp = requester.get(
                PRICES_HISTORY_ENDPOINT,
                params={
                    "market": token_id,
                    "startTs": chunk_start,
                    "endTs": chunk_end,
                    "fidelity": fidelity_min,
                },
                timeout=timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"status {resp.status_code}")
            resp.raise_for_status()
            return _parse_history_payload(resp.json())
        except (requests.RequestException, ValueError) as exc:
            if attempt >= _MAX_ATTEMPTS:
                print(
                    f"[price_history_client] fetch_history: giving up on chunk "
                    f"{chunk_start}-{chunk_end} for token {token_id} after "
                    f"{_MAX_ATTEMPTS} attempts: {exc}"
                )
                return []
            backoff = _BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            print(
                f"[price_history_client] fetch_history: attempt {attempt}/{_MAX_ATTEMPTS} "
                f"failed for token {token_id} chunk {chunk_start}-{chunk_end}: {exc} "
                f"-- retrying in {backoff:.1f}s"
            )
            time.sleep(backoff)

    return []


def fetch_history(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity_min: int = 5,
    session=None,
    timeout: int = 15,
) -> List[Tuple[int, float]]:
    """
    Fetch historical (ts, price) points for one token across
    [start_ts, end_ts], issuing one request per <=7-day chunk and
    merging the results (deduped by ts, sorted ascending).

    Never raises: a chunk that fails all its retries is logged and
    skipped, and whatever other chunks succeeded is still returned.
    """
    if end_ts <= start_ts:
        return []

    points_by_ts = {}
    chunk_start = start_ts
    while chunk_start < end_ts:
        chunk_end = min(chunk_start + _CHUNK_SECONDS, end_ts)
        chunk_points = _fetch_chunk(token_id, chunk_start, chunk_end, fidelity_min, session, timeout)
        for ts, price in chunk_points:
            points_by_ts[ts] = price
        chunk_start = chunk_end

    return sorted(points_by_ts.items(), key=lambda pair: pair[0])


def observed_fidelity_min(points: List[Tuple[int, float]]) -> Optional[int]:
    """
    Median gap (in whole minutes) between consecutive timestamps in
    `points`. Returns None if fewer than 2 points -- no gap to measure.
    Points are assumed sorted by ts (fetch_history guarantees this);
    sorted defensively here too in case a caller passes raw data.
    """
    if len(points) < 2:
        return None
    ordered = sorted(points, key=lambda pair: pair[0])
    gaps_sec = [b[0] - a[0] for a, b in zip(ordered, ordered[1:]) if b[0] > a[0]]
    if not gaps_sec:
        return None
    return round(median(gaps_sec) / 60.0)


def backfill_token(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity_min: int = 5,
    db_path=None,
) -> dict:
    """
    Fetch a token's history over [start_ts, end_ts], persist it via
    price_store.save_snapshots() using the MEASURED (not requested)
    fidelity when available, and log the fetch via price_store.log_fetch().

    Idempotent: relies entirely on price_store's INSERT OR REPLACE
    semantics for save_snapshots/save_snapshot -- this function does not
    dedupe against what's already in the db, it just re-saves.
    """
    points = fetch_history(token_id, start_ts, end_ts, fidelity_min=fidelity_min, timeout=15)
    fidelity_observed = observed_fidelity_min(points)
    n_rows = len(points)
    http_status = 200 if n_rows > 0 else 0

    rows = [(token_id, ts, price, None) for ts, price in points]
    price_store.save_snapshots(
        rows,
        source="clob_history",
        fidelity_min=fidelity_observed if fidelity_observed is not None else fidelity_min,
        db_path=db_path,
    )
    price_store.log_fetch(
        token_id,
        start_ts,
        end_ts,
        fidelity_requested=fidelity_min,
        fidelity_observed=fidelity_observed,
        n_rows=n_rows,
        http_status=http_status,
        db_path=db_path,
    )

    return {
        "token_id": token_id,
        "n_rows": n_rows,
        "fidelity_observed": fidelity_observed,
    }


def _day_start_utc_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def backfill_station(
    station_icao: str,
    start_date: date,
    end_date: date,
    fidelity_min: int = 5,
    db_path=None,
) -> List[dict]:
    """
    Backfill every known token for `station_icao` whose target_date
    falls within [start_date, end_date] (inclusive), pulling a window of
    [target_date - 3 days, target_date + 1 day] (00:00 UTC bounds) per
    token -- wide enough to cover pre-market discovery through
    resolution for a single-day bucket market.
    """
    tokens = price_store.list_tokens(station_icao=station_icao, db_path=db_path)
    results = []

    for token in tokens:
        raw_target_date = token.get("target_date")
        if isinstance(raw_target_date, date):
            token_date = raw_target_date
        else:
            try:
                token_date = date.fromisoformat(str(raw_target_date))
            except ValueError:
                print(f"[price_history_client] backfill_station: unparseable target_date {raw_target_date!r} for token {token.get('token_id')}, skipping")
                continue

        if not (start_date <= token_date <= end_date):
            continue

        window_start = _day_start_utc_ts(date.fromordinal(token_date.toordinal() - 3))
        window_end = _day_start_utc_ts(date.fromordinal(token_date.toordinal() + 1))

        result = backfill_token(
            token["token_id"],
            window_start,
            window_end,
            fidelity_min=fidelity_min,
            db_path=db_path,
        )
        results.append(result)
        print(
            f"[price_history_client] backfill_station: {station_icao} {token_date} "
            f"bucket {token.get('bucket_c')} side {token.get('side')} -- "
            f"{result['n_rows']} rows, observed fidelity {result['fidelity_observed']}min"
        )

    return results
