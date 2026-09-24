# Handoff - 2026-09-24

## State
Branch `main`, clean, pushed. Last code commit: `da3a5a1` cohort_monitor --by.
The EC2 box (`ubuntu@43.216.25.99`) has run daemon code `e31363d` since 2026-09-23 15:52 UTC. Its checkout was pulled to `da3a5a1` on 2026-09-24 without a restart. The later commits are read-only scripts and docs, so the daemon doesn't need them.
- **The daemon runs in PAPER mode** (`--mode paper --fallback-mode paper`) since 2026-09-23 15:33 UTC.
  - This was set in `/etc/polyweather/mode.env`, not in config. The backup is `mode.env.bak-20260923`.
  - To re-arm live: restore the backup, then restart the daemon.
  - It had 0 live open positions when it was switched. The wallet holds ~$0.26.

## Done this session (2026-09-15 → 2026-09-24)
- Six read-only reviews on 2026-09-15. Findings are in memory `*-review-2026-09-15.md`.
- Spec: `weather-forecast/docs/superpowers/specs/2026-09-17-evidence-first-remediation-design.md`.
- Wave 1 (record the deciding numbers): merged `0dcc5f6`, deployed 2026-09-17. Falsifier passed on 2026-09-18.
- Wave 2 (correct the inputs): merged `f6f8aac`, deployed 2026-09-19 08:45 UTC. Regime boundary is 2026-09-20.
- Wave 3 (stop the corruption): merged `1c32d1f`, deployed 2026-09-23 14:40 UTC.
- Region edge reviews on 2026-09-23 (Asia, Europe, America): the ask beat the raw model on Brier almost everywhere. That led to:
  - Live disarmed (see State).
  - `17463dc`: the final net-EV bar now uses the calibrated edge, not the raw edge. The calibration map is clamped to [0.05, 0.95].
  - `d2e1a88`: each station's calibration map is blended with the pooled map.
    - The station's weight is days / (days + 40), where days = distinct target dates (`CALIBRATION_SHRINK_DAYS`).
    - Re-score with `calibration_shrink_replay.py`.
  - `e31363d`: NO tickets are calibrated on a NO-only map (`CALIBRATE_NO_SIDE_SEPARATELY`). YES keeps the combined map.
    - Re-score with `calibration_side_replay.py`.
  - `4181370`: `calibration_region_replay.py` tested a regional map. The answer is NO in all three regions, so no regional tier gets built.
  - `9fcff2e`: `asia_no_gap.py` looked at Asia NO. The gap is historical: early maps were small, and we bought NO where the ask was also too high. No fix is needed.

## Open / next step
1. **Entry volume.** In the first day after `e31363d` there were 0 NO approvals (out of 205 NO decisions) and only 2 YES approvals in total.
   - This is expected: the NO map maps raw 0.70 to 0.50, which is below the typical NO ask.
   - Watch the first NO approval. Check it against the current map before trusting it.
   - If volume stays near zero, the book stops generating evidence. Decide whether that is acceptable before ~10-04.
2. **Redemption ABI question (still OPEN):** does `redeem.py:269`'s one-element amounts array match what the contract expects? The RPC returns 403 on both the box and the dev machine. Re-run the probe with a Polygonscan key:
   `cd ~/weather-forecast/weather-forecast && ~/weather-forecast/.venv/bin/python redeem_abi_probe.py --api-key <key>`
   Live can't be re-armed safely until this is answered.
3. **~2026-10-04 checkpoint.** Run `wave2_falsifier.py` on the box. It defaults to boundary 2026-09-20.
   - Stop rule: if the CI upper bound of (after − before) is below 0, set `ERROR_SAMPLE_FETCH_WINDOW_ENABLED` and `SPREAD_FLOOR_MEASURED_TIERS_EXEMPT` to False.
   - Then run the checkpoint reads: λ refit on `ev_snapshots`, the calibration gap on stored `calibrated_prob`, and shadow-twin pairing.
   - Re-run the shrink and side replays at the same time.
4. **Europe review P2 is DONE (`da3a5a1`).** `python cohort_monitor.py --by station|side|region` (read-only) prints held-to-settlement and as-traded P&L per group, net of entry fees. Results from the box on 2026-09-24:
   - All time: held +$467 vs as traded −$531 on $6,017 staked, so exits cost $998 (Asia $742, Europe $256, Americas $0). All of that exit cost predates 2026-09-02.
   - Since 2026-09-03 (no exits, so held == as traded): Americas +$220, Asia −$120, Europe −$191; NO −$116, YES +$24.
   - So removing exits fixed the exit loss, but the entries are now losing outside the Americas.
   - Per station since 09-03, profit and loss both come from a handful of stations:
     - KATL +$159 and KLGA +$63.
     - EDDM −$76, ZSPD −$71 and RKPK −$29.
     - The other 26 stations net about −$140.
   - Americas without KATL and KLGA is about −$3. Europe: 6 of 7 stations lost; only LFPB is positive (+$31). Asia traded far less than before (69 trades vs Europe's 124).
   - Every station has at most 15 days since 09-03, so none of this is separable yet. KATL is also the one station where raw beats calibrated (see memory `regional-calibration-replay`). Don't act on one station.
   - **Collection-only for EDDM, ZSPD and RKPK: checked 2026-09-24, answer NO.** Nothing was changed.
     - All three pass the structural gate (error ÷ bucket, stop above 1.00): EDDM 0.65, ZSPD 0.78, RKPK 0.95.
     - Model-minus-ask Brier gap since 09-03 (negative = the ask wins):
       - EDDM −0.140 ± 0.024
       - RKPK −0.080 ± 0.031
       - ZSPD −0.050 ± 0.037 (not separable)
       - These are per-entry SEs over 9–11 days, so they are optimistic.
     - Since `e31363d` the calibrated gate already refuses all 110 of their candidates (EDDM 0/45, ZSPD 0/37, RKPK 0/28), so a named stop changes nothing today.
     - Selecting stations on past P&L doesn't persist (split-half about −0.1; see config's `MAX_ERROR_RMSE_PER_BUCKET` note).
   - **EDDM's real defect is probably the spread floor, not the station.** It has the best forecast in Europe yet the worst Brier gap. That matches memory `spread-floor-underconfidence`:
     - Its corrected RMSE is 0.65C, but `SPREAD_FLOOR_C` 0.70 is a `max()`, so Wave 2's floor exemption is inert for it.
     - The too-wide spread underprices the winning bucket, and the model then sells it as NO.
     - A fix would lower or exempt the floor where measured RMSE is small. The replay can't test it, because `backtest/engine.py` sets `allow_measured_spread=False`, so it needs its own safety argument. Not scoped.
   - **The Task 5 re-check of the 2026-09-09 named stops is now runnable** (their ratios exist):
     - CYYZ 0.98 and KHOU 0.95 now PASS. The config note says to drop any that pass, but CYYZ's model loses to the ask (Brier 0.202 vs 0.129). Operator decision, pending.
     - SBGR 2.00, KSEA 1.19, KMIA 1.05 and MMMX 1.02 still fail. Keep them stopped.
     - Re-run with the scratch script's logic: `calibration.error_width_ratio` plus `promotion_dossier.live_calibration` per station.
   - Re-run `--by region --since 2026-09-03` and `--by station` at the ~10-04 checkpoint.
   - To split by station within one region, build the `--station` list from `config.region_of`:
     `PY=~/weather-forecast/.venv/bin/python; $PY cohort_monitor.py --by station --since 2026-09-03 $($PY -c 'import config; print(" ".join("--station "+s for s in sorted(config.STATIONS) if config.region_of(s)=="europe"))')`
5. **DST end:** restart the daemon after 2026-10-25 01:00 UTC (EGLC) and after 2026-11-01 06:00 UTC (KLGA). The scheduler's offset groups are computed once, at boot.

## Gotchas
- SSH is PuTTY plink, not OpenSSH:
  `"/c/Program Files/PuTTY/plink.exe" -batch -ssh -i "C:\Users\user\Downloads\multicityweatherbot.ppk" ubuntu@43.216.25.99 "<cmd>"`
- On the box the code is at `~/weather-forecast/weather-forecast/` and the venv one level up (`~/weather-forecast/.venv/bin/python`).
- Deploy with `~/deploy.sh`. It stops the daemon, backs up the DB, migrates and restarts on its own.
  - Deploy only 16:00–19:00 UTC, when no region's entry window is open.
  - If it prints `!! DEPLOY ABORTED`, the daemon is STOPPED. Recovery is in the Wave 3 plan, Task 8 step 4.
- Don't empty `config.LIVE_TRADING_STATIONS` to disarm live: 51 tests use WSSS as their live fixture. Switch the mode in `mode.env` instead.
- Storage is read-only by default. A new script that writes must call `storage.set_writable(True)`, and must also be added to the allowlist in `tests/test_wave3_writers_and_readers.py`.
- When grepping the journal for the boot line, use `--since` with the full date and a minute earlier than the restart; the line lands seconds after it.
- Run pytest in the foreground (`timeout 420 python -m pytest -q` from `weather-forecast/`). Subagents that backgrounded the suite repeatedly stalled.
