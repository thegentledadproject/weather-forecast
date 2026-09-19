"""
regimes.py -- split rows at config.REGIME_BOUNDARIES.

A wave lands on one day (spec Principle 1), and the reports must not pool
rows across it: the pre-registered reads are "did the number move at the
boundary", which a pooled total cannot show. ONE implementation here so
cohort_monitor, calibration_panel and promotion_dossier segment the same
way; each keeps its own arithmetic and only changes WHICH rows it feeds it.

Segments are labelled by their edges -- "pre-2026-09-20", "2026-09-20..
2026-10-04", "from-2026-10-05" -- and the boundary day belongs to the NEW
regime, because the deploy runs before that day's entry window. Every
segment is returned, empty ones included, so a report shows the layout
even where a regime has no rows yet.

Pure: reads config, touches nothing else.
"""
from datetime import date, timedelta
from typing import Callable, List, Optional, Sequence, Tuple

import config


def boundaries() -> Tuple[date, ...]:
    """config.REGIME_BOUNDARIES as dates. Refuses an unsorted or repeated
    list rather than silently producing an empty middle segment."""
    out = tuple(date.fromisoformat(b) for b in config.REGIME_BOUNDARIES)
    if list(out) != sorted(set(out)):
        raise ValueError(
            f"config.REGIME_BOUNDARIES must be strictly increasing ISO dates, got {config.REGIME_BOUNDARIES!r}"
        )
    return out


def segment_labels(bounds: Sequence[date]) -> List[str]:
    if not bounds:
        return ["all"]
    labels = [f"pre-{bounds[0].isoformat()}"]
    for lo, hi in zip(bounds, bounds[1:]):
        labels.append(f"{lo.isoformat()}..{(hi - timedelta(days=1)).isoformat()}")
    labels.append(f"from-{bounds[-1].isoformat()}")
    return labels


def segment_index(day: date, bounds: Sequence[date]) -> int:
    """0 before the first boundary, k after the k-th (boundary day included)."""
    return sum(1 for b in bounds if day >= b)


def regime_segments(
    rows: Sequence,
    key: Optional[Callable] = None,
    bounds: Optional[Sequence[date]] = None,
) -> List[Tuple[str, list]]:
    """[(label, rows_in_segment), ...] in chronological order. `key` reads
    the row's date (default: row["target_date"]); `bounds` defaults to
    boundaries() and is explicit for tests."""
    if key is None:
        key = lambda r: r["target_date"]  # noqa: E731
    if bounds is None:
        bounds = boundaries()
    labels = segment_labels(bounds)
    buckets: List[list] = [[] for _ in labels]
    for row in rows:
        buckets[segment_index(key(row), bounds)].append(row)
    return list(zip(labels, buckets))
