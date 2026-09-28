# WSSS (Singapore Changi) — state and improvement plan, 2026-09-28

WSSS is our best station, and the only candidate for real money, so its plan
runs on two tracks:

- **Model track:** does the model have an edge?
- **Plumbing track:** if it does, can we collect the winnings?

Think of a strong player who hasn't won lately and whose bank account can't
cash cheques. You check both: are they still good, and can they get paid?

## Current state (measured on the box, 2026-09-28)

- **Status: locked two ways.**
  - The bot on the box runs with `--mode paper`.
  - `REDEMPTION_PROVEN_TX` is still None. That's the proof that a winning live
    position has been redeemed on-chain, and live can't be re-armed without it.
  - WSSS is still in `LIVE_TRADING_STATIONS`. Nothing is open, and no real
    money is at risk.
- **Maturity: passes 5 of 6 checks.**
  - Bias stability passes (−0.31 then −0.37, so steady), and 7 test orders have
    gone through.
  - "Beats the market" fails only on a technicality: that score came from older
    code (`16a0949f`) and has to be re-run. Past runs have always shown the
    market winning at WSSS, so it's not expected to pass.
- **Aug 6 to Sep 7 was good** (43 entries, $187 staked):
  - Held to settlement: **+62%**
  - As traded: **+30%**
  - Take-profits cost $55; stops barely mattered ($5).
- **The last 30 days are bad:** 11 entries, net price edge −13.6 cents per
  dollar. The automatic warning check has only 8 of the 30 trading days it
  needs, so it can't give a verdict yet.
- **Entries have stopped:** 1 paper entry since 09-07 (09-24, lost $10.25) and
  none since 09-25. Two filters do the blocking: the NO-side floor (the model
  must be at least 65% sure to bet NO, rule `00c`) and the calibration map,
  which shrinks the model's edge (rule `0a2`).
- **History, not a current issue:** on 09-21, before live was disarmed, the
  live orders failed with "not enough balance". The wallet held about $0.26.
- **NEA check passed:** the old `data.gov.sg/v1` address still works. There are
  32 `nea_24hr` rows a day through 09-28, and no failure lines in the logs.

### How good each forecast source is (vs the airport's real daily highs, ~56 days)

"Skill" is the correlation with the real highs: how well the forecast moves up
and down with them, where 1 is perfect and 0 means no connection. "Average
miss" is measured after correcting each source's usual bias.

| Source | Usual bias | Skill | Average miss |
|---|---|---|---|
| ECMWF | −1.35°C | 0.49 | 0.59°C |
| NEA (official) | +1.45°C | 0.44 | 0.66°C |
| GFS | −1.15°C | −0.02 (none) | 0.66°C |
| *Always guess the average* | — | — | 0.68°C |

Singapore's weather is so steady that "guess the usual" nearly matches the best
forecast. It helps explain why the market keeps winning on Brier (the accuracy
score for probabilities), and why 60% of the estimate comes from a flat 30-day
average of real highs (forecast blend weight 0.40).

## Phase 0: now to 10-05 (before the freeze)

1. **Recency-weighted observed term: don't ship it before the freeze.** It's
   untested, and it would change WSSS's biggest input in the middle of the
   ~10-04 checkpoint. The seasonal drift into November is likely only a few
   tenths of a degree, small next to a typical 0.68°C miss. Replay it in step 4.
2. **Run the ~10-04 checkpoint** for WSSS (`cohort_monitor.py --by station`),
   and re-run the beats-market backtest on current code so the maturity check
   stops showing a stale result.

## Phase 1: 10-06 to 11-02 (freeze, so study only)

Everything runs offline on a copy of the database, except step 7, which is an
operator task.

3. **Replay source selection.** Compare the current mix, the mix without GFS,
   and ECMWF plus NEA.
   - **Pass:** it beats the current mix on the average miss *and* on Brier, in
     both halves of the data.
4. **Replay the observed term.** Compare the flat 30-day mean with the
   recency-weighted version (a few half-lives). Score it through November as
   the monsoon shifts.
   - **Pass:** it clearly beats flat in both halves. Otherwise flat stays.
5. **Replay take-profit vs hold.** Take-profit cost $55. The 09-07 re-run found
   the modes disagree, which is why `LOTTERY_PROFIT_TAKE_PCT` stays untouched
   for now. Re-test it on fresh data.
6. **Read the recorder data:**
   - **Day-ahead (~10-12):** is WSSS priced wrong the evening before?
   - **Fast recorder (~10-13):** can a market maker (posting both buy and sell
     prices) earn on WSSS? The late-day METAR idea already answered "no": the
     market prices new airport readings within the hour, so don't re-test it.
7. **Plumbing: prove redemption.** This is the biggest blocker to live, and
   it's independent of the model.
   - Fix the `redeem.py` ABI issue from the 09-15 review. The ABI is the
     contract interface the script uses to call the chain.
   - Fund the wallet with a small amount, and redeem one real winning position
     by hand.
   - Record the transaction hash as `REDEMPTION_PROVEN_TX`.
   - This is an operator-run script, not a scheduler change, so it can happen
     during the freeze, **with operator approval** (it moves money).

## Phase 2: from 11-03, one change at a time, two weeks each

8. **The input fixes that passed (steps 3 and 4) go first.** They correct the
   model's inputs, so they come before any tuning.
9. **Take-profit change next, only if step 5 passed.** For paper that's easy.
   For live, the rule is already "hold to settlement once redemption is proven".
10. **Two small fixes, bundled with whichever deploy comes first:**
    - **NEA date stamp:** take the day from `valid_period` so an evening
      forecast can't be stored as today's.
    - **Stale bucket window:** re-sweep it from the drift log (currently 28–38;
      the live markets use 25–35).

## Phase 3: live decision point, around mid-December

11. **Re-arm live only if every one of these holds:**
    - redemption is proven;
    - the other gates from the 09-24 audit are closed: settlement
      reconciliation (checking our books against how markets actually settled)
      and the watchdog;
    - maturity passes with no override, including beats-market (model Brier at
      or below the market's on at least 20 scored entries);
    - the net price edge over the trailing 30 days is above zero, with at least
      30 trading days of data;
    - the operator gives the go-ahead.
12. **If those gates close but the model still loses to the market on Brier,**
    WSSS stays on paper, and the recorders decide whether market-making is the
    better use of the station.
13. **If the edge stays below zero for 30 trading days after the Phase 2
    changes,** move WSSS to collection-only, the same stop rule as WMKK.

## Deliberately left out

- Loosening the NO-side floor or the calibration shrink. Both were measured,
  and both are what stop the model's known optimism about its own picks from
  costing money.
- New data sources.
- Entry-price filters. Those already answered "no" twice.
