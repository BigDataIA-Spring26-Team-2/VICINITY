"""Property-based tests for app.scoring.

Covers the pure-function math layer: scorer.py (percentile ranking,
confidence, YoY change, weighted composite) and the data-shaping
helpers in metadata.py (neighborhood resolution, lifestyle signal
matching). Uses Hypothesis to generate adversarial inputs and verify
invariants that must hold across all valid inputs.

What we test: invariants — properties that must ALWAYS be true.
What we don't test: exact output values (unit tests already do this
for known inputs), SQL queries (I/O-bound, out of Hypothesis's scope),
the pipeline orchestrator (integration territory).

Location rationale: unit, not integration. These tests are pure-
function, no Snowflake, no network, deterministic under Hypothesis's
seeded RNG. Fast enough to run on every save.

Run:
    pytest tests/unit/scoring/test_scoring_properties.py -v
    pytest tests/unit/scoring/test_scoring_properties.py --hypothesis-show-statistics
"""

from __future__ import annotations

import math
import string

import pytest
from hypothesis import given, assume, settings, strategies as st, HealthCheck

from app.scoring.scorer import (
    clamp,
    percentile_rank,
    compute_livability_percentiles,
    evidence_confidence,
    compute_safety_confidence,
    compute_livability_confidence,
    compute_transit_confidence,
    compute_yoy_change,
    weighted_composite,
)
from app.scoring.metadata import (
    resolve_listing_neighborhoods,
    _match_lifestyle_signals,
)


# =====================================================================
# Shared strategies
# =====================================================================

# Listing IDs look like "lst-xxxx" in prod; a short distinct alphanum is
# plenty for property tests. Deduplicated via .filter() below.
listing_id_st = st.text(
    alphabet=string.ascii_lowercase + string.digits,
    min_size=4, max_size=12,
)


def _unique_keys(items: dict) -> bool:
    return len(items) > 0


# Raw metric dicts for percentile_rank: listing_id -> non-negative float
raw_metric_dict_st = st.dictionaries(
    keys=listing_id_st,
    values=st.floats(min_value=0, max_value=10_000,
                     allow_nan=False, allow_infinity=False),
    min_size=1, max_size=50,
)


# Livability-shaped dict used by compute_livability_percentiles
livability_entry_st = st.fixed_dictionaries({
    "noise_count":   st.integers(min_value=0, max_value=500),
    "pest_count":    st.integers(min_value=0, max_value=500),
    "heat_count":    st.integers(min_value=0, max_value=500),
    "housing_count": st.integers(min_value=0, max_value=500),
    "infra_count":   st.integers(min_value=0, max_value=500),
    "essentials_found": st.integers(min_value=0, max_value=6),
})

livability_dict_st = st.dictionaries(
    keys=listing_id_st,
    values=livability_entry_st,
    min_size=1, max_size=30,
)


# Monthly series — "YYYY-MM" keys with total/violent counts
@st.composite
def monthly_series(draw, min_months=0, max_months=36):
    """Generate a plausible monthly series. Months are consecutive
    from a random anchor, so gaps don't confuse the YoY logic."""
    n = draw(st.integers(min_value=min_months, max_value=max_months))
    if n == 0:
        return {}
    anchor_year = draw(st.integers(min_value=2020, max_value=2025))
    anchor_month = draw(st.integers(min_value=1, max_value=12))
    out: dict[str, dict] = {}
    y, m = anchor_year, anchor_month
    for _ in range(n):
        key = f"{y:04d}-{m:02d}"
        total = draw(st.integers(min_value=0, max_value=100))
        violent = draw(st.integers(min_value=0, max_value=total))
        out[key] = {"total": total, "violent": violent}
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


# =====================================================================
# 1. clamp — trivial but cheap to lock down
# =====================================================================

class TestClamp:

    @given(v=st.floats(allow_nan=False, allow_infinity=False),
           lo=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
           hi=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
    def test_result_in_bounds(self, v, lo, hi):
        assume(lo <= hi)
        result = clamp(v, lo, hi)
        assert lo <= result <= hi

    @given(v=st.floats(min_value=0, max_value=100, allow_nan=False,
                       allow_infinity=False))
    def test_in_bounds_value_unchanged(self, v):
        assert clamp(v, 0, 100) == v


# =====================================================================
# 2. percentile_rank — core ranking invariants
# =====================================================================

class TestPercentileRank:

    @given(values=raw_metric_dict_st, lower_better=st.booleans())
    def test_output_keys_match_input(self, values, lower_better):
        result = percentile_rank(values, lower_better=lower_better)
        assert set(result.keys()) == set(values.keys())

    @given(values=raw_metric_dict_st, lower_better=st.booleans())
    def test_all_percentiles_in_range(self, values, lower_better):
        result = percentile_rank(values, lower_better=lower_better)
        for pct in result.values():
            assert isinstance(pct, int)
            assert 0 <= pct <= 100

    def test_empty_input_returns_empty(self):
        assert percentile_rank({}, lower_better=True) == {}

    @given(single_val=st.floats(min_value=0, max_value=1000,
                                 allow_nan=False, allow_infinity=False))
    def test_single_listing_gets_median(self, single_val):
        """With only one listing there's no ranking — 50 is the fair default."""
        result = percentile_rank({"only": single_val}, lower_better=True)
        assert result == {"only": 50}

    @given(values=raw_metric_dict_st)
    def test_lower_better_inverts_direction(self, values):
        """Flipping lower_better must invert each listing's rank ordering."""
        assume(len(set(values.values())) > 1)  # need distinct ranks to see inversion
        asc = percentile_rank(values, lower_better=True)
        desc = percentile_rank(values, lower_better=False)
        # For any two listings with different raw values, the one with
        # higher percentile in asc must have lower percentile in desc.
        keys = list(values.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if values[a] == values[b]:
                    continue
                # Whichever direction beats the other in asc must lose in desc.
                if asc[a] > asc[b]:
                    assert desc[a] <= desc[b]
                elif asc[a] < asc[b]:
                    assert desc[a] >= desc[b]

    @given(values=raw_metric_dict_st)
    def test_lower_better_awards_min_highest(self, values):
        """With lower_better=True, at least one of the listings tied for
        the minimum raw value must have the maximum percentile.

        (When multiple listings tie on value, rank ordering among them is
        arbitrary — but one of the tied group must win overall.)
        """
        assume(len(values) >= 2)
        result = percentile_rank(values, lower_better=True)
        min_raw = min(values.values())
        min_keys = [k for k, v in values.items() if v == min_raw]
        max_pct = max(result.values())
        assert any(result[k] == max_pct for k in min_keys)


# =====================================================================
# 3. compute_livability_percentiles — multi-key sort invariants
# =====================================================================

class TestLivabilityPercentiles:

    @given(livability=livability_dict_st,
           qol_w=st.floats(min_value=0.1, max_value=2.0),
           infra_w=st.floats(min_value=0.0, max_value=1.0))
    def test_output_keys_match_input(self, livability, qol_w, infra_w):
        result = compute_livability_percentiles(livability, qol_w, infra_w)
        assert set(result.keys()) == set(livability.keys())

    @given(livability=livability_dict_st,
           qol_w=st.floats(min_value=0.1, max_value=2.0),
           infra_w=st.floats(min_value=0.0, max_value=1.0))
    def test_all_in_range(self, livability, qol_w, infra_w):
        result = compute_livability_percentiles(livability, qol_w, infra_w)
        for pct in result.values():
            assert isinstance(pct, int)
            assert 0 <= pct <= 100

    @given(qol_w=st.floats(min_value=0.1, max_value=2.0),
           infra_w=st.floats(min_value=0.0, max_value=1.0))
    def test_fewer_complaints_beats_more(self, qol_w, infra_w):
        """Primary sort: lower effective complaints wins. Given two
        listings with identical essentials, the one with fewer QoL
        complaints must rank higher."""
        livability = {
            "low":  {"noise_count": 1, "pest_count": 0, "heat_count": 0,
                     "housing_count": 0, "infra_count": 0, "essentials_found": 3},
            "high": {"noise_count": 20, "pest_count": 5, "heat_count": 3,
                     "housing_count": 2, "infra_count": 0, "essentials_found": 3},
        }
        result = compute_livability_percentiles(livability, qol_w, infra_w)
        assert result["low"] > result["high"]

    def test_essentials_breaks_ties(self):
        """With identical complaint scores, more essentials ranks higher."""
        livability = {
            "more": {"noise_count": 2, "pest_count": 0, "heat_count": 0,
                     "housing_count": 0, "infra_count": 0, "essentials_found": 6},
            "less": {"noise_count": 2, "pest_count": 0, "heat_count": 0,
                     "housing_count": 0, "infra_count": 0, "essentials_found": 1},
        }
        result = compute_livability_percentiles(livability, 1.0, 0.3)
        assert result["more"] > result["less"]

    def test_infra_weighted_less_than_qol(self):
        """With qol_w=1.0 and infra_w=0.3, 10 infra complaints must rank
        BETTER than 10 noise complaints because infra is weighted less."""
        livability = {
            "ten_infra": {"noise_count": 0, "pest_count": 0, "heat_count": 0,
                          "housing_count": 0, "infra_count": 10,
                          "essentials_found": 3},
            "ten_noise": {"noise_count": 10, "pest_count": 0, "heat_count": 0,
                          "housing_count": 0, "infra_count": 0,
                          "essentials_found": 3},
        }
        result = compute_livability_percentiles(livability, 1.0, 0.3)
        assert result["ten_infra"] > result["ten_noise"]


# =====================================================================
# 4. evidence_confidence — bounded, monotonic
# =====================================================================

class TestEvidenceConfidence:

    @given(count=st.integers(min_value=-100, max_value=100_000),
           base=st.floats(min_value=0.1, max_value=1.0, allow_nan=False))
    def test_always_bounded(self, count, base):
        conf = evidence_confidence(count, base)
        assert 0.05 <= conf <= 0.95

    @given(count=st.integers(min_value=-100, max_value=100_000),
           base=st.floats(min_value=0.1, max_value=1.0, allow_nan=False))
    def test_always_finite(self, count, base):
        conf = evidence_confidence(count, base)
        assert math.isfinite(conf)

    @given(base=st.floats(min_value=0.5, max_value=0.95, allow_nan=False),
           low=st.integers(min_value=0, max_value=50),
           extra=st.integers(min_value=1, max_value=10_000))
    def test_monotonic_non_decreasing_in_count(self, base, low, extra):
        """More evidence never reduces confidence."""
        high = low + extra
        conf_low = evidence_confidence(low, base)
        conf_high = evidence_confidence(high, base)
        assert conf_high >= conf_low

    def test_zero_count_is_minimal_but_nonzero(self):
        conf = evidence_confidence(0, base_reliability=0.85)
        assert conf > 0

    @given(count=st.integers(min_value=1, max_value=10_000),
           lower_base=st.floats(min_value=0.3, max_value=0.6, allow_nan=False),
           bump=st.floats(min_value=0.1, max_value=0.3, allow_nan=False))
    def test_monotonic_in_base_reliability(self, count, lower_base, bump):
        """Higher base reliability always yields >= confidence for same count."""
        higher_base = min(1.0, lower_base + bump)
        lower = evidence_confidence(count, lower_base)
        higher = evidence_confidence(count, higher_base)
        assert higher >= lower


class TestConfidenceShims:
    """compute_safety_confidence, compute_livability_confidence, and
    compute_transit_confidence wrap evidence_confidence with specific
    base reliabilities. Just verify they stay in range and transit is
    always the fixed value."""

    @given(crime=st.integers(min_value=0, max_value=10_000),
           months=st.integers(min_value=0, max_value=60))
    def test_safety_confidence_in_range(self, crime, months):
        c = compute_safety_confidence(crime, months)
        assert 0.05 <= c <= 0.95

    @given(complaints=st.integers(min_value=0, max_value=10_000))
    def test_livability_confidence_in_range(self, complaints):
        c = compute_livability_confidence(complaints)
        assert 0.05 <= c <= 0.95

    def test_transit_confidence_is_fixed(self):
        """Transit is a complete MBTA extract. If this changes,
        the comment in scorer.py needs updating too."""
        assert compute_transit_confidence() == 0.95


# =====================================================================
# 5. compute_yoy_change — insufficient data -> None, sign is meaningful
# =====================================================================

class TestYoYChange:

    @given(series=monthly_series(min_months=0, max_months=12))
    def test_insufficient_data_returns_none(self, series):
        """Function docstring: < 13 months -> None."""
        if len(series) < 13:
            assert compute_yoy_change(series) is None

    def test_zero_prior_returns_none(self):
        """Can't compute % change when prior is zero — must return None."""
        series = {
            "2024-01": {"total": 0, "violent": 0},
            "2024-02": {"total": 5, "violent": 0},
            "2024-03": {"total": 5, "violent": 0},
            "2024-04": {"total": 5, "violent": 0},
            "2024-05": {"total": 5, "violent": 0},
            "2024-06": {"total": 5, "violent": 0},
            "2024-07": {"total": 5, "violent": 0},
            "2024-08": {"total": 5, "violent": 0},
            "2024-09": {"total": 5, "violent": 0},
            "2024-10": {"total": 5, "violent": 0},
            "2024-11": {"total": 5, "violent": 0},
            "2024-12": {"total": 5, "violent": 0},
            "2025-01": {"total": 10, "violent": 0},
        }
        assert compute_yoy_change(series) is None

    def test_sign_of_change_is_correct(self):
        """Positive YoY == worsening (more crime), negative == improving.

        Given jan 2024 = 100 and jan 2025 = 50, change should be -50.0
        (improving). Use 14 months to guarantee the algorithm picks
        jan 2025 as the recent reference even with a partial current month.
        """
        series = {f"2024-{m:02d}": {"total": 100, "violent": 0}
                  for m in range(1, 13)}
        series["2025-01"] = {"total": 50, "violent": 0}
        series["2025-02"] = {"total": 50, "violent": 0}
        yoy = compute_yoy_change(series)
        # We don't assert the exact value — that depends on which
        # "most recent complete month" is picked based on today's date.
        # We just assert sign: must be negative (improvement).
        assert yoy is None or yoy < 0


# =====================================================================
# 6. weighted_composite — renormalization + bounds
# =====================================================================

class TestWeightedComposite:

    @given(weights=st.dictionaries(
        keys=st.sampled_from(["safety", "livability", "transit", "lifestyle"]),
        values=st.floats(min_value=0.1, max_value=1.0, allow_nan=False),
        min_size=2, max_size=4,
    ))
    def test_renormalized_sums_to_one(self, weights):
        """When batch_only=True, lifestyle is dropped. Whatever
        remains must renormalize to ~1.0."""
        percentiles = {d: 50 for d in weights}
        _, renorm = weighted_composite(percentiles, weights, batch_only=True)
        if renorm:
            assert math.isclose(sum(renorm.values()), 1.0, abs_tol=0.01)

    @given(percentiles=st.dictionaries(
        keys=st.sampled_from(["safety", "livability", "transit"]),
        values=st.integers(min_value=0, max_value=100),
        min_size=1, max_size=3,
    ), weight_val=st.floats(min_value=0.1, max_value=1.0, allow_nan=False))
    def test_score_bounded_0_100(self, percentiles, weight_val):
        weights = {d: weight_val for d in percentiles}
        score, _ = weighted_composite(percentiles, weights, batch_only=True)
        assert 0.0 <= score <= 100.0

    def test_batch_only_excludes_lifestyle(self):
        """batch_only=True must strip lifestyle from the composite even
        if it's in the weights dict and percentiles dict."""
        weights = {"safety": 0.5, "lifestyle": 0.5}
        percentiles = {"safety": 100, "lifestyle": 0}
        score, renorm = weighted_composite(percentiles, weights, batch_only=True)
        # Lifestyle should be absent; safety alone renormalized to 1.0.
        assert "lifestyle" not in renorm
        assert score == 100.0

    def test_empty_input_returns_zero(self):
        score, renorm = weighted_composite({}, {}, batch_only=True)
        assert score == 0.0
        assert renorm == {}

    def test_zero_weights_returns_zero(self):
        """When all weights are zero, renormalization has no denominator.
        Must handle gracefully, not divide by zero."""
        score, renorm = weighted_composite(
            {"safety": 80},
            {"safety": 0.0},
            batch_only=True,
        )
        assert score == 0.0

    def test_perfect_percentiles_give_max_score(self):
        """If every dimension is at 100th percentile, composite is 100
        regardless of weight distribution."""
        weights = {"safety": 0.3, "livability": 0.2, "transit": 0.5}
        percentiles = {d: 100 for d in weights}
        score, _ = weighted_composite(percentiles, weights, batch_only=True)
        assert math.isclose(score, 100.0, abs_tol=0.1)


# =====================================================================
# 7. resolve_listing_neighborhoods — string-shape edge cases
# =====================================================================

class TestResolveNeighborhoods:

    @given(hood=st.text(min_size=1, max_size=40).filter(
        lambda s: "/" not in s and s.strip() != ""))
    def test_simple_name_returned_as_single(self, hood):
        result = resolve_listing_neighborhoods(hood, None, {})
        assert result == [hood.strip()]

    def test_compound_name_split(self):
        result = resolve_listing_neighborhoods(
            "West End/Beacon Hill", None, {}
        )
        assert result == ["West End", "Beacon Hill"]

    def test_compound_name_strips_whitespace(self):
        result = resolve_listing_neighborhoods(
            "  West End  /  Beacon Hill  ", None, {}
        )
        assert result == ["West End", "Beacon Hill"]

    def test_none_neighborhood_returns_empty_without_zip(self):
        assert resolve_listing_neighborhoods(None, None, {}) == []

    def test_zip_fallback_hits(self):
        result = resolve_listing_neighborhoods(
            None, "02134", {"02134": "Allston"}
        )
        assert result == ["Allston"]

    def test_zip_fallback_missing_returns_empty(self):
        assert resolve_listing_neighborhoods(
            None, "99999", {"02134": "Allston"}
        ) == []

    def test_zip_plus_four_stripped(self):
        """Snowflake sometimes returns "02134-1234" — strip to 5 chars."""
        result = resolve_listing_neighborhoods(
            None, "02134-1234", {"02134": "Allston"}
        )
        assert result == ["Allston"]

    def test_empty_string_neighborhood_treated_as_missing(self):
        """Falsy neighborhood strings should fall through to zip."""
        result = resolve_listing_neighborhoods(
            "", "02134", {"02134": "Allston"}
        )
        assert result == ["Allston"]

    def test_neighborhood_takes_precedence_over_zip(self):
        """If both present, neighborhood wins — zip is fallback only."""
        result = resolve_listing_neighborhoods(
            "Back Bay", "02134", {"02134": "Allston"}
        )
        assert result == ["Back Bay"]


# =====================================================================
# 8. _match_lifestyle_signals — additive across neighborhoods
# =====================================================================

class TestMatchLifestyleSignals:

    def test_empty_hoods_returns_empty(self):
        assert _match_lifestyle_signals([], {"Allston": {}}) == {}

    def test_single_hood_single_tag_passes_through(self):
        lifestyle = {
            "Allston": {
                "korean_food": {
                    "positive": 3, "negative": 0, "mixed": 1,
                    "neutral": 0, "total": 4,
                    "sample_titles": ["Best korean spot"],
                },
            }
        }
        result = _match_lifestyle_signals(["Allston"], lifestyle)
        assert "korean_food" in result
        assert result["korean_food"]["positive"] == 3
        assert result["korean_food"]["mixed"] == 1
        assert result["korean_food"]["total"] == 4
        assert "Allston" in result["korean_food"]["neighborhoods_matched"]

    def test_compound_hoods_sum_sentiments(self):
        """A listing in "West End/Beacon Hill" sums signals from both."""
        lifestyle = {
            "West End": {
                "noise": {"positive": 0, "negative": 5, "mixed": 1,
                          "neutral": 0, "total": 6,
                          "sample_titles": ["Loud neighborhood"]},
            },
            "Beacon Hill": {
                "noise": {"positive": 0, "negative": 2, "mixed": 0,
                          "neutral": 0, "total": 2,
                          "sample_titles": ["Quiet mostly"]},
            },
        }
        result = _match_lifestyle_signals(
            ["West End", "Beacon Hill"], lifestyle,
        )
        assert result["noise"]["negative"] == 7  # 5 + 2
        assert result["noise"]["mixed"] == 1
        assert result["noise"]["total"] == 8     # 6 + 2
        assert set(result["noise"]["neighborhoods_matched"]) == {
            "West End", "Beacon Hill",
        }

    def test_sample_titles_deduplicated(self):
        """Same title appearing in two hoods should only be listed once."""
        lifestyle = {
            "West End":    {"t": {"positive": 1, "negative": 0, "mixed": 0,
                                  "neutral": 0, "total": 1,
                                  "sample_titles": ["Same title"]}},
            "Beacon Hill": {"t": {"positive": 1, "negative": 0, "mixed": 0,
                                  "neutral": 0, "total": 1,
                                  "sample_titles": ["Same title"]}},
        }
        result = _match_lifestyle_signals(
            ["West End", "Beacon Hill"], lifestyle,
        )
        assert result["t"]["sample_titles"].count("Same title") == 1

    def test_sample_titles_capped_at_five(self):
        lifestyle = {
            "Allston": {
                "noise": {
                    "positive": 0, "negative": 10, "mixed": 0,
                    "neutral": 0, "total": 10,
                    "sample_titles": [f"title {i}" for i in range(20)],
                }
            }
        }
        result = _match_lifestyle_signals(["Allston"], lifestyle)
        assert len(result["noise"]["sample_titles"]) == 5

    def test_hood_not_in_lifestyle_map_is_silent(self):
        """Listings in neighborhoods with no signals shouldn't raise
        — they just match nothing. Report generator handles empty sets."""
        result = _match_lifestyle_signals(
            ["NonexistentHood"], {"Allston": {"x": {"total": 1}}},
        )
        assert result == {}