"""
tests/test_bucket_bounds_live.py

Regression tests for the live-bounds derivation fix (design doc B1-B4):
a station's real Polymarket bucket window is a live fact that drifts
seasonally, and every piece of code that turns a raw bucket-label string
or a discovered token map into bucket bounds must get this right, or a
missing/mis-parsed bucket hands its NO side a phantom ~20c edge that
clears every risk gate (see market_discovery.py's module docstring).

Covers:
  - market_discovery.parse_bucket_label: edge labels, the date-in-prose
    trap, implausible numbers.
  - market_discovery.derive_bucket_bounds: contiguity + exact-11 vetoes.
  - probability.bucket_probabilities: "floor" vs "half_up" edge modes
    put a near-boundary point estimate's modal mass in different buckets.
  - backtest.resolution.bucket_for_temp: same floor/half_up distinction,
    edge clamping, and legacy no-bounds-passed behavior.
"""

from datetime import date

import config
import market_discovery
import probability
from backtest import resolution
from models import CalibratedEstimate


# --- market_discovery.parse_bucket_label -----------------------------------

class TestParseBucketLabel:
    def test_question_text_with_date_does_not_parse_the_date(self):
        market = {
            "question": "Will the highest temperature in Tokyo on August 6, 2026 be 27°C or below?"
        }
        assert market_discovery.parse_bucket_label(market) == 27

    def test_group_item_title_or_higher(self):
        assert market_discovery.parse_bucket_label({"groupItemTitle": "36°C or higher"}) == 36

    def test_group_item_title_plain_degree(self):
        assert market_discovery.parse_bucket_label({"groupItemTitle": "31°C"}) == 31

    def test_implausible_number_falls_back_to_none(self):
        # "2026°C" is outside the 5..50 plausibility band -- must not be
        # accepted as a real bucket, and there's no edge-label fallback
        # here (no "or below"/"or above", no bounds passed).
        assert market_discovery.parse_bucket_label({"groupItemTitle": "2026°C"}) is None

    def test_implausible_number_with_no_fallback_bounds_is_none(self):
        market = {"question": "Will the highest temperature be 2026°C or below?"}
        assert market_discovery.parse_bucket_label(market) is None


# --- market_discovery.derive_bucket_bounds ----------------------------------

class TestDeriveBucketBounds:
    def test_contiguous_eleven_keys_returns_bounds(self):
        token_map = {b: {} for b in range(27, 38)}  # 27..37 inclusive == 11 keys
        assert market_discovery.derive_bucket_bounds(token_map) == (27, 37)

    def test_ten_keys_returns_none(self):
        token_map = {b: {} for b in range(27, 37)}  # 27..36 == 10 keys, right shape wrong size
        assert market_discovery.derive_bucket_bounds(token_map) is None

    def test_eleven_span_with_a_gap_returns_none(self):
        # 27..37 spans 11 values but bucket 32 is missing -- only 10 keys
        # present, non-contiguous over the implied span.
        token_map = {b: {} for b in range(27, 38) if b != 32}
        assert market_discovery.derive_bucket_bounds(token_map) is None

    def test_empty_map_returns_none(self):
        assert market_discovery.derive_bucket_bounds({}) is None


# --- probability.bucket_probabilities edge modes ----------------------------

class TestBucketProbabilitiesEdgeModes:
    def _estimate(self, central_c: float, std_c: float = 0.05) -> CalibratedEstimate:
        return CalibratedEstimate(
            station_icao="VHHH", target_date=date(2026, 8, 6),
            central_estimate_c=central_c, std_dev_c=std_c, monsoon_phase="unknown",
        )

    def test_legacy_call_unchanged(self):
        # No edge_mode passed at all -- must match an explicit half_up call
        # over the module-level default bounds, preserving old-test behavior.
        est = self._estimate(30.0, std_c=2.0)
        legacy = probability.bucket_probabilities(est)
        explicit = probability.bucket_probabilities(est, config.BUCKET_MIN_C, config.BUCKET_MAX_C, edge_mode="half_up")
        assert legacy == explicit

    def test_floor_mode_point_mass_below_half_up_mode(self):
        # 33.9C with a tiny std: half_up's [33.5, 34.5) interval claims it
        # for bucket 34; floor's [33, 34) interval claims it for bucket 33.
        # Getting this backwards is a half-degree systematic mispricing of
        # the two buckets straddling the mode, on every cycle.
        est = self._estimate(33.9)
        floor_probs = probability.bucket_probabilities(est, 27, 37, edge_mode="floor")
        half_up_probs = probability.bucket_probabilities(est, 27, 37, edge_mode="half_up")

        floor_modal = max(floor_probs, key=lambda b: b.probability).bucket_c
        half_up_modal = max(half_up_probs, key=lambda b: b.probability).bucket_c

        assert floor_modal == 33
        assert half_up_modal == 34


# --- backtest.resolution.bucket_for_temp ------------------------------------

class TestBucketForTemp:
    def test_floor_mode(self):
        assert resolution.bucket_for_temp(33.9, 27, 37, "floor") == 33

    def test_half_up_mode(self):
        assert resolution.bucket_for_temp(33.9, 27, 37, "half_up") == 34

    def test_clamps_at_low_edge(self):
        assert resolution.bucket_for_temp(-50.0, 27, 37, "half_up") == 27

    def test_clamps_at_high_edge(self):
        assert resolution.bucket_for_temp(500.0, 27, 37, "half_up") == 37

    def test_legacy_no_bounds_call_matches_globals(self):
        # No bucket_min/bucket_max passed -- falls back to the module-level
        # config.BUCKET_MIN_C/MAX_C globals, exactly the pre-multi-station
        # behavior old callers/tests still rely on.
        t = 30.5
        assert resolution.bucket_for_temp(t) == resolution.bucket_for_temp(
            t, config.BUCKET_MIN_C, config.BUCKET_MAX_C, "half_up"
        )
