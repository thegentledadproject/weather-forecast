"""
Gap 4, step 3: admit on P_robust = m + lambda_robust * (p - m).

m is the bucket's YES ask normalised over the station-day's listed buckets, p
the raw model probability, and lambda_robust = max(0, lambda_hat - 1.645 SE)
from ONE pooled fit over settled station-days strictly before the target day
(date-clustered SE). NO = 1 - the YES value. Walk-forward comparison
(scratchpad step1): the ask is the best forecaster and lambda_robust has been 0
since 2026-09-08, so the rule admits ~nothing today. The user accepted that.
"""
import random
import sqlite3
from datetime import date, timedelta

import pytest

import config
import entry_manager
import ev_engine
import probability_calibration as pc
import storage
from backtest import entry_sim
from models import CalibratedEstimate, EVResult, MarketQuote

DAY = date(2026, 9, 25)


@pytest.fixture(autouse=True)
def _fresh_cache():
    pc.clear_cache()
    yield
    pc.clear_cache()


# --- the estimator ------------------------------------------------------------

def _synthetic(lam, n_days=30, noisy=False, seed=7):
    rng = random.Random(seed)
    pts = []
    for k in range(n_days):
        d = date(2026, 8, 1) + timedelta(days=k)
        for _ in range(10):
            m = rng.uniform(0.05, 0.6)
            p = min(max(m + rng.uniform(-0.3, 0.3), 0.0), 1.0)
            q = m + lam * (p - m)
            o = (1.0 if rng.random() < q else 0.0) if noisy else q
            pts.append((d, o, m, p))
    return pts


def test_a_known_lambda_is_recovered_exactly_from_noise_free_outcomes():
    robust, hat, se, days = pc.fit_shrink(_synthetic(0.4))
    assert hat == pytest.approx(0.4, abs=1e-12)
    assert se == pytest.approx(0.0, abs=1e-12)
    assert days == 30
    assert robust == pytest.approx(0.4, abs=1e-12)


def test_a_known_lambda_is_recovered_from_bernoulli_outcomes():
    robust, hat, se, _ = pc.fit_shrink(_synthetic(0.6, n_days=400, noisy=True))
    assert hat == pytest.approx(0.6, abs=0.1)
    assert 0 < se < 0.1
    assert robust == pytest.approx(max(0.0, hat - 1.645 * se))


def test_the_standard_error_is_date_clustered():
    """SE = sqrt(sum_d (A_d - lam B_d)^2) / sum_d B_d, by hand on two dates."""
    d1, d2 = date(2026, 9, 1), date(2026, 9, 2)
    pts = [(d1, 1.0, 0.2, 0.6), (d1, 0.0, 0.5, 0.3), (d2, 0.0, 0.4, 0.8)]
    a1, b1 = (0.8 * 0.4) + (-0.5 * -0.2), 0.4 ** 2 + 0.2 ** 2
    a2, b2 = (-0.4 * 0.4), 0.4 ** 2
    lam = (a1 + a2) / (b1 + b2)
    se = ((a1 - lam * b1) ** 2 + (a2 - lam * b2) ** 2) ** 0.5 / (b1 + b2)
    _, hat, got_se, _ = pc.fit_shrink(pts, min_days=1)
    assert hat == pytest.approx(lam) and got_se == pytest.approx(se)


def test_zero_variance_gives_zero():
    pts = [(date(2026, 9, 1) + timedelta(days=k), 1.0, 0.3, 0.3) for k in range(30)]
    robust, hat, se, days = pc.fit_shrink(pts)
    assert robust == 0.0 and hat is None and se is None and days == 30


def test_too_few_dates_give_zero_but_still_report_the_estimate():
    pts = _synthetic(0.4, n_days=config.SHRINK_MIN_FIT_DAYS - 1)
    robust, hat, se, days = pc.fit_shrink(pts)
    assert robust == 0.0
    assert hat == pytest.approx(0.4)
    assert days == config.SHRINK_MIN_FIT_DAYS - 1


def test_lambda_robust_never_exceeds_one():
    assert pc.fit_shrink(_synthetic(1.5))[0] == 1.0


def test_negative_lambda_floors_at_zero():
    assert pc.fit_shrink(_synthetic(-0.5))[0] == 0.0


# --- the fit set --------------------------------------------------------------

def _row(st, td, b, p, m, settled):
    return {"station_icao": st, "target_date": td, "bucket_c": b,
            "model_prob": p, "market_price": m, "settled_bucket_c": settled}


def test_points_normalise_the_yes_asks_and_drop_partial_books():
    rows = [
        _row("WSSS", "2026-09-10", 30, 0.2, 0.30, 31),
        _row("WSSS", "2026-09-10", 31, 0.8, 0.90, 31),
        _row("RCSS", "2026-09-10", 30, 0.5, 0.40, 30),
        _row("RCSS", "2026-09-10", 31, 0.5, None, 30),   # unpriced bucket: whole day out
    ]
    pts = sorted(pc.shrink_points(rows), key=lambda t: t[2])
    assert pts == [
        (date(2026, 9, 10), 0.0, pytest.approx(0.25), 0.2),
        (date(2026, 9, 10), 1.0, pytest.approx(0.75), 0.8),
    ]


def test_robust_yes_probs():
    q = pc.robust_yes_probs({30: 0.30, 31: 0.90}, {30: 0.2, 31: 0.8}, 0.5)
    assert q == {30: pytest.approx(0.25 + 0.5 * (0.2 - 0.25)), 31: pytest.approx(0.75 + 0.5 * (0.8 - 0.75))}
    assert pc.robust_yes_probs({30: 0.30, 31: None}, {30: 0.2, 31: 0.8}, 0.5) is None


# --- shrink_for_day: causal, cached, fails closed ------------------------------

def _seed_day(con, td, settled, p_by_bucket, m_by_bucket, st="WSSS"):
    start, _ = config.local_day_bounds_utc(st, date.fromisoformat(td))
    gen = (start + timedelta(hours=5)).isoformat()
    for b in p_by_bucket:
        con.execute(
            "INSERT INTO ev_snapshots (station_icao,target_date,bucket_c,side,generated_at,model_prob,market_price) "
            "VALUES (?,?,?,?,?,?,?)", (st, td, b, "YES", gen, p_by_bucket[b], m_by_bucket[b]),
        )
    con.execute(
        "INSERT INTO settled_buckets (station_icao,target_date,bucket_c,bucket_min_c,bucket_max_c,source,recorded_at) "
        "VALUES (?,?,?,?,?,?,?)", (st, td, settled, 30, 31, "t", "t"),
    )


def test_the_target_day_and_later_are_never_in_the_fit(tmp_db):
    con = sqlite3.connect(tmp_db)
    rng = random.Random(3)
    for k in range(20):
        td = (DAY - timedelta(days=20 - k)).isoformat()
        _seed_day(con, td, rng.choice([30, 31]), {30: 0.5, 31: 0.5}, {30: 0.5, 31: 0.5})
    con.commit()
    before = pc.shrink_for_day(DAY)
    assert before[3] == 20
    # From DAY on, the model is perfect and loud: would make lambda ~1 if used.
    for k in range(40):
        td = (DAY + timedelta(days=k)).isoformat()
        _seed_day(con, td, 30, {30: 0.95, 31: 0.05}, {30: 0.5, 31: 0.5})
    con.commit()
    pc.clear_cache()
    assert pc.shrink_for_day(DAY) == before
    assert pc.shrink_for_day(DAY)[0] == 0.0


def test_a_fit_failure_fails_closed_and_is_not_cached(monkeypatch):
    calls = []

    def boom(before):
        calls.append(before)
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(storage, "load_shrink_fit_rows", boom)
    assert pc.shrink_for_day(DAY) == (0.0, None, None, 0)
    pc.shrink_for_day(DAY)
    assert calls == [DAY, DAY]


def test_a_successful_fit_is_cached_per_day(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "load_shrink_fit_rows", lambda before: calls.append(before) or [])
    pc.shrink_for_day(DAY)
    pc.shrink_for_day(DAY)
    pc.shrink_for_day(DAY + timedelta(days=1))
    assert calls == [DAY, DAY + timedelta(days=1)]


# --- ev_engine stamps it --------------------------------------------------------

def _table(quotes, shrink):
    estimate = CalibratedEstimate(
        station_icao="WSSS", target_date=DAY, central_estimate_c=30.5,
        std_dev_c=1.0, monsoon_phase="southwest", spread_source="measured_error",
    )
    token_map = {b: {"yes_token_id": f"y{b}", "no_token_id": f"n{b}"} for b in quotes}
    return ev_engine.compute_ev_table(
        estimate, token_map, quotes=quotes, model_probs={30: 0.6, 31: 0.4}, shrink=shrink,
    )


@pytest.fixture
def no_slippage(monkeypatch):
    monkeypatch.setattr(ev_engine.market_client, "estimate_slippage", lambda t, s: 0.0)


def test_the_no_side_is_priced_from_the_yes_fit_without_any_no_rows(no_slippage):
    quotes = {30: MarketQuote(30, 0.50, 0.55), 31: MarketQuote(31, 0.50, 0.55)}
    rows = {(r.bucket_c, r.side): r for r in _table(quotes, (0.5, 0.6, 0.06, 20))}
    q30 = 0.5 + 0.5 * (0.6 - 0.5)
    assert rows[(30, "YES")].p_robust == pytest.approx(q30)
    assert rows[(30, "NO")].p_robust == pytest.approx(1 - q30)
    assert (rows[(30, "NO")].lambda_hat, rows[(30, "NO")].lambda_se, rows[(30, "NO")].lambda_days) == (0.6, 0.06, 20)


def test_a_partial_book_prices_p_robust_at_the_own_ask(no_slippage):
    """Fail closed: nothing to normalise against, so the robust edge is 0."""
    quotes = {30: MarketQuote(30, 0.50, 0.55), 31: MarketQuote(31, None, 0.55)}
    rows = [r for r in _table(quotes, (0.5, 0.6, 0.06, 20)) if r.market_price is not None]
    assert rows and all(r.p_robust == r.market_price for r in rows)


def test_run_for_station_threads_the_days_fit(monkeypatch):
    seen = {}

    def fake_table(*a, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(ev_engine.market_discovery, "discover_token_map",
                        lambda *a: {30: {"yes_token_id": "y", "no_token_id": "n"},
                                    31: {"yes_token_id": "y", "no_token_id": "n"}})
    monkeypatch.setattr(ev_engine.market_discovery, "derive_bucket_bounds", lambda *a, **k: (30, 31))
    monkeypatch.setattr(ev_engine, "fetch_market_quotes", lambda tm: {})
    monkeypatch.setattr(ev_engine, "_capture_snapshots", lambda *a: None)
    monkeypatch.setattr(ev_engine, "_calibration_for", lambda *a: None)
    monkeypatch.setattr(ev_engine, "compute_ev_table", fake_table)
    monkeypatch.setattr(pc, "shrink_for_day", lambda d: (0.25, 0.4, 0.1, 15))
    estimate = CalibratedEstimate(
        station_icao="WSSS", target_date=DAY, central_estimate_c=30.5,
        std_dev_c=1.0, monsoon_phase="southwest", spread_source="measured_error",
    )
    ev_engine.run_for_station_with_map(estimate)
    assert seen["shrink"] == (0.25, 0.4, 0.1, 15)

    monkeypatch.setattr(pc, "shrink_for_day", lambda d: 1 / 0)
    ev_engine.run_for_station_with_map(estimate)
    assert seen["shrink"] == (0.0, None, None, 0)


# --- admission / sizing -------------------------------------------------------

def _ev(p=0.50, price=0.30, calibrated=0.45, p_robust=0.31, side="YES"):
    return EVResult(
        station_icao="WSSS", target_date=DAY, bucket_c=32, side=side,
        model_prob=p, market_price=price, raw_edge=p - price, estimated_slippage_pct=0.0,
        fee_rate_pct=0.0, net_ev_per_dollar=(p - price) / price,
        calibrated_prob=calibrated, calibration_source=pc.STATION_TIER, p_robust=p_robust,
        lambda_hat=0.1, lambda_se=0.05, lambda_days=20,
    )


@pytest.fixture
def no_live_io(monkeypatch):
    monkeypatch.setattr(entry_manager.market_client, "estimate_slippage", lambda t, s: 0.0)
    monkeypatch.setattr(entry_manager.market_client, "get_available_depth_usd", lambda t: 100_000.0)
    monkeypatch.setattr(entry_manager, "count_open_positions_for_bucket", lambda *a, **k: 0)


def test_the_flag_ships_on():
    assert config.ADMIT_ON_ROBUST_EDGE is True


def test_precedence_robust_over_calibrated_over_raw(monkeypatch):
    ev = _ev()
    assert entry_manager.admission_edge(ev) == pytest.approx(0.01)        # robust
    monkeypatch.setattr(config, "ADMIT_ON_ROBUST_EDGE", False)
    assert entry_manager.admission_edge(ev) == pytest.approx(0.15)        # calibrated
    monkeypatch.setattr(config, "ADMIT_ON_CALIBRATED_EDGE", False)
    assert entry_manager.admission_edge(ev) == pytest.approx(0.20)        # raw
    monkeypatch.setattr(config, "ADMIT_ON_ROBUST_EDGE", True)
    assert entry_manager.admission_edge(ev) == pytest.approx(0.01)        # robust wins either way


def test_no_p_robust_on_the_row_falls_back_to_the_calibrated_rule():
    """None = this caller did not compute it (tests, the replay). Production
    always stamps it -- a failed fit stamps lambda 0, i.e. p_robust = m."""
    assert entry_manager.admission_edge(_ev(p_robust=None)) == pytest.approx(0.15)


def test_the_edge_is_signed_in_the_sides_own_direction():
    assert entry_manager.admission_edge(_ev(price=0.40, p_robust=0.35, side="NO")) == pytest.approx(-0.05)


def test_a_robust_edge_below_the_bar_is_refused_at_0a2(no_live_io):
    d = entry_manager.evaluate_entry(_ev(), token_id="tok", min_net_ev=-9.0)
    assert not d.approved and d.rule_id == "0a2"
    assert d.p_robust == pytest.approx(0.31) and d.lambda_days == 20
    assert "robust" in d.reason


def test_final_net_ev_bar_and_sizing_use_p_robust(no_live_io):
    d = entry_manager.evaluate_entry(_ev(p_robust=0.40), token_id="tok", min_net_ev=-9.0)
    assert d.approved
    assert d.net_ev_at_size == pytest.approx(0.10 / 0.30)
    assert d.sizing_edge == pytest.approx(0.10)               # min(calibrated 0.15, robust 0.10)
    assert d.calibrated_prob == pytest.approx(0.45)           # the old map still travels


def test_sizing_never_exceeds_the_calibrated_size():
    assert entry_manager.sizing_edge(_ev(p_robust=0.50)) == pytest.approx(0.15)


def test_flag_off_restores_the_calibrated_rule_exactly(no_live_io, monkeypatch):
    monkeypatch.setattr(config, "ADMIT_ON_ROBUST_EDGE", False)
    d = entry_manager.evaluate_entry(_ev(), token_id="tok", min_net_ev=-9.0)
    assert d.approved and d.sizing_edge == pytest.approx(0.15)


def test_the_replay_mirrors_the_robust_gate():
    """Same helper, same answer -- but the replay's own rows carry no
    p_robust (backtest/engine.py builds no fit and no normalised ask), so a
    sweep still admits on the calibrated/raw edge and cannot score this."""
    d = entry_sim.evaluate_entry_sim(
        _ev(), token_id="tok", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=100_000.0, slippage_fn=lambda s: 0.0,
        min_net_ev=-9.0, sizing_bankroll=1_000.0,
    )
    assert not d.approved and d.rule_id == "0a2"
    d = entry_sim.evaluate_entry_sim(
        _ev(p_robust=0.40), token_id="tok", open_count_for_bucket=0, opposite_count_for_bucket=0,
        stop_outs_for_bucket=0, depth_usd=100_000.0, slippage_fn=lambda s: 0.0,
        min_net_ev=-9.0, sizing_bankroll=1_000.0,
    )
    assert d.approved and d.net_ev_at_size == pytest.approx(0.10 / 0.30)
