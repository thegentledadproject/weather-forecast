# Station edge check: session notes (2026-09-28)

## Question 1: which stations does the model beat the market on?

**Answer from recorded evidence: none so far.** The live DB is on the box, so this is from notes in the code, not a fresh run.

| Scope | Model Brier | Market Brier | Verdict | Source |
|---|---|---|---|---|
| Whole book (358 rows) | 0.1930 | 0.1842 | market better | `cohort_monitor.py:12` |
| WSSS, 4 windows (Jul 28 - Aug 11) | 0.165-0.205 | 0.111-0.163 | market better every time | `config.py:4680-4683` |
| RCSS (n=9, bar is 20) | 0.145 | 0.062 | market ~2x better | `config.py` MATURITY_OVERRIDE comment |
| All other stations | - | - | not recorded | - |

Lower Brier = more accurate guesses.

**But the book still made money (paper):** mean entry 0.306 vs 0.344 realised win rate. The model is a worse *forecaster* but pointed at *underpriced* tickets. That is a price edge, not a forecasting edge.

## Question 2: which stations find cheap tickets?

"Cheap" = the ticket wins more often than its price says, after the entry fee
(`cohort_monitor.price_edge()`, the same number the kill criterion uses).

Script: `cheap.py` (below). Not yet run on real data.

### Watch item

Recent result: 1 win out of 20 settled positions (target dates 09-24..26), mostly cheap YES tickets at 0.04-0.25.
Known market habit: people **overpay** for long shots. The bot buys long shots. So the first thing to check in the
output is whether **YES tickets under 25c** come out `OVERPRICED`. Could still be bad luck (~12% chance).

## How to run (from the Windows PC, Git Bash)

1. Save the script below as `cheap.py`.
2. Upload:
   ```
   "/c/Program Files/PuTTY/pscp.exe" -batch -i "C:\Users\user\Downloads\multicityweatherbot.ppk" cheap.py ubuntu@43.216.25.99:weather-forecast/weather-forecast/cheap.py
   ```
3. Run (all time, then since 09-03):
   ```
   "/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "cd ~/weather-forecast/weather-forecast && ../.venv/bin/python cheap.py && echo ---- && ../.venv/bin/python cheap.py 2026-09-03"
   ```

Read-only; safe while the daemon runs.

```python
"""Which stations does the model find CHEAP tickets at? Read-only."""
import sys, cohort_monitor as cm
since = None
if len(sys.argv) > 1:
    from datetime import date; since = date.fromisoformat(sys.argv[1])
rows, _ = cm.load_cohort(since=since)
by = {}
for r in rows:
    by.setdefault((r["station_icao"], r["side"]), []).append(r)
    by.setdefault((r["station_icao"], "ALL"), []).append(r)
out = [(k, cm.price_edge(v)) for k, v in by.items()]
out.sort(key=lambda kv: kv[1]["net_price_edge"], reverse=True)
print(f"{'station':7} {'side':4} {'n':>4} {'days':>4} {'paid':>5} {'won':>5} {'edge':>6}  95% range      verdict")
for (st, side), e in out:
    ci = e["ci_net_price_edge"]
    lo, hi = ci if ci else (None, None)
    if lo is not None and lo > 0: v = "CHEAP (clear)"
    elif hi is not None and hi < 0: v = "OVERPRICED (clear)"
    elif e["net_price_edge"] > 0: v = "cheap? (could be luck)"
    else: v = "pricey? (could be luck)"
    rng = f"{lo:+.2f}..{hi:+.2f}" if ci else "n/a"
    print(f"{st:7} {side:4} {e['n']:>4} {e['n_days']:>4} {e['mean_entry_price']:>5.2f} "
          f"{e['win_rate']:>5.2f} {e['net_price_edge']:>+6.2f}  {rng:13}  {v}")
```

How to read it:
- `paid` = average price paid, `won` = how often those tickets won, `edge` = won - paid - fee.
- `CHEAP (clear)`: bargains, and the 95% range is fully above zero.
- `OVERPRICED (clear)`: paying too much, range fully below zero.
- `could be luck`: too few trades to tell. Read `days`, not `n`, as the real sample size.

## Background: where edge comes from in a market full of bots

- The market price is the crowd's average guess and is usually very good. Most casual traders have no edge.
- Winners usually have one of: **speed** (react first to new airport readings), **better/local data**,
  **exploiting habits** (sell overpriced long shots), **market making** (post prices, earn the spread),
  **focus** on few cities.
- As bots multiply, speed and obvious bargains vanish first. What survives: small quiet markets big bots skip,
  unique knowledge, market making, and knowing when to stop.
- The "when to stop" tool exists: `cohort_monitor.kill_criterion` (30-day net price edge < 0). It is report-only
  today; plan-gap-audit gap 6 proposes making it flip to paper and alert.

## Caveats

- Q1 is from August notes, not live data.
- `cheap.py` was tested on made-up rows only.
- The branch `spec/recency-weighted-observed-term` was not opened; its purpose was guessed from its name.

## Next steps

1. Run `cheap.py` on the box and review the output.
2. Add a price-level split (e.g. YES under 25c) to `cheap.py`.
3. Make the kill criterion actually stop trading (gap 6).
