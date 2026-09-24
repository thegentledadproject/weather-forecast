# Forward lock: pre-registration (gap 5)

**Written:** 2026-09-24, before any VALIDATION or TEST outcome exists. **Enforcement:** HONOR SYSTEM (user decision). **Scorer:** `weather-forecast/lock_score.py` (read-only, stdlib). **No external climatology** (user decision).

## Why

The model, the calibration map, K=40 and the NO map were all chosen on the same window they were scored on (gap audit #5). Nothing has been scored against a no-skill baseline. This locks one untouched period and says in advance what each result means.

## Periods (target dates)

| Period | Dates | Allowed |
|---|---|---|
| TRAIN | < 2026-09-25 | Anything. |
| VALIDATION | 2026-09-25 .. 2026-10-05 | ONE frozen tuning pass. The Wave 2 falsifier (~10-04) runs here. On 2026-10-05, fill `LOCK_SHA` in `lock_score.py` with the revision the box runs from then on, and commit it. |
| TEST | 2026-10-06 .. 2026-11-02 | Nobody reads or tunes on TEST outcomes. No deploys to the box. Restarts are OK (DST: EGLC 10-25, KLGA 11-01). |
| Read | on or after 2026-11-05 | `python lock_score.py --start 2026-10-06 --end 2026-11-02 --locked-read` |

`lock_score.py` refuses any range that overlaps TEST unless `--locked-read` is passed, today is 2026-11-05 or later, and `LOCK_SHA` is filled in.

## Unit

One station-day, if all of these hold:

- It uses the FIRST `ev_snapshots` cycle whose `generated_at` falls on the target LOCAL date.
- Every bucket listed in that cycle has a YES ask and a model probability.
- A `settled_buckets` row exists, and the settled bucket is one of the listed ones.
- Every row of that cycle has `config_sha == LOCK_SHA`.

## Forecasters

Each forecaster gives one probability per listed bucket:

- **P**: the raw model (YES `model_prob`), renormalised.
- **M**: the YES asks, normalised.
- **Q**: P_robust = m + λ_robust(D)·(p − m), renormalised. λ comes from `probability_calibration.shrink_for_day(D)`, which uses dates before D only. When λ = 0, Q is exactly M. Q is a scoring distribution only. Trading ADMISSION uses the same λ but applies it to each side's own raw ask: P_robust_side = ask_side + λ·(p_side − ask_side). So at λ = 0 no side has any edge.
- **U**: uniform.
- **E30**: the station's settled-bucket counts over the 30 target dates before D, plus 0.5 per listed bucket.

## Metrics and comparisons

- **Primary metric:** multi-class Brier, averaged over units.
- **Secondary metrics:** log loss (floor 1e-3), RPS, 10-bin reliability.
- **Comparisons:** P−E30, P−M, Q−M, using a date-cluster bootstrap: 10,000 draws, seed 20261006, 98.33% CI (three comparisons at 5%).
- **Minimum sample:** at least 20 dates and at least 400 station-days. If short, extend ONCE by 14 dates and read whatever is there.

## Decisions (fixed now)

1. **P−E30 CI not entirely below 0:** the model has no skill over climatology. Live stays off, and work goes back to the forecast inputs.
2. **Q−M CI upper bound below 0:** this is the only result that can make a station eligible for re-arm review. Eligibility is per station, using the per-station Q−M CI in the same report.
3. **Anything else:** no re-arm.

## Dry run (TRAIN, snapshot 2026-09-24, target dates 2026-09-03..09-23, no LOCK_SHA filter)

731 station-days, 21 dates, 35 stations. λ_robust = 0 on every date, so Q = M.

| Forecaster | Brier | Log loss | RPS |
|---|---|---|---|
| P | 0.7331 | 1.5360 | 0.0589 |
| M | 0.6122 | 1.1828 | 0.0422 |
| Q | 0.6122 | 1.1828 | 0.0422 |
| U | 0.9091 | 2.3979 | 0.1170 |
| E30 | 0.8744 | 2.2149 | 0.1035 |

| Comparison | Brier diff | 98.33% CI |
|---|---|---|
| P−E30 | −0.1413 | [−0.1689, −0.1120] |
| P−M | +0.1209 | [+0.0822, +0.1669] |
| Q−M | 0.0000 | [0.0000, 0.0000] |

Caveat: `settled_buckets` starts on 2026-08-06, and stations added later have shorter histories. So on TRAIN, E30 has fewer than 30 prior settlements for many station-days, and that makes it weaker than it will be during TEST.
