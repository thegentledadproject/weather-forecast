# Handoff - 2026-09-24

## State
Branch `main`, clean, pushed. Last commit: `9fcff2e` Add Asia NO calibration gap diagnostic.
The EC2 box (`ubuntu@43.216.25.99`) has run `e31363d` since 2026-09-23 15:52 UTC. The two later commits are standalone read-only scripts, and the daemon doesn't need them.
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
4. **Still open from the Europe review (P2):** report the entry held-to-settlement result and the executed exit P&L as separate metrics. Nothing has been built.
   - This matters less now: paper trades have used no stop/take exits since 2026-09-02.
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
