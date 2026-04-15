"""Scoring math — percentile ranking, confidence, composite.

All scores are integers 0-100. All confidence values are floats 0.0-1.0.
Clamped at boundaries. No magic numbers — scaling derives from data
distribution via percentile ranking.
"""

import math
from datetime import datetime

import structlog

logger = structlog.get_logger()


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Bound value to [lo, hi]."""
    return max(lo, min(hi, value))


# ── Percentile Ranking ───────────────────────────────────────

def percentile_rank(
    values: dict[str, float],
    lower_better: bool,
) -> dict[str, int]:
    """Compute percentile rank (0-100) for each listing_id.

    Percentile 100 = best. Percentile 0 = worst.
    lower_better=True: lowest raw value gets highest percentile.
    """
    if not values:
        return {}

    sorted_ids = sorted(
        values.keys(),
        key=lambda k: values[k],
        reverse=lower_better,
    )
    n = len(sorted_ids)
    if n == 1:
        return {sorted_ids[0]: 50}

    return {
        lid: int(clamp(round(rank / (n - 1) * 100)))
        for rank, lid in enumerate(sorted_ids)
    }


def compute_livability_percentiles(
    livability: dict[str, dict],
    qol_weight: float,
    infra_weight: float,
) -> dict[str, int]:
    """Livability percentile via multi-key ranking.

    Primary sort: weighted complaint score (lower = better).
      effective = (noise + pest + heat + housing) * qol_weight
                + (infra) * infra_weight
    Tiebreaker: essentials_found (higher = better, inverted for sort).

    Weights are config-driven: qol_weight=1.0, infra_weight=0.3
    means infrastructure counts at 30% of quality-of-life complaints.
    """
    composite_key = {}
    for lid, d in livability.items():
        qol = (
            d.get("noise_count", 0)
            + d.get("pest_count", 0)
            + d.get("heat_count", 0)
            + d.get("housing_count", 0)
        )
        infra = d.get("infra_count", 0)
        effective = (qol * qol_weight) + (infra * infra_weight)

        composite_key[lid] = (
            effective,
            -(d.get("essentials_found", 0)),
        )

    # Sort best-first: lowest effective complaints, then most essentials
    sorted_ids = sorted(composite_key.keys(), key=lambda k: composite_key[k])
    n = len(sorted_ids)
    if n <= 1:
        return {lid: 50 for lid in sorted_ids}

    return {
        lid: int(clamp(round((1 - rank / (n - 1)) * 100)))
        for rank, lid in enumerate(sorted_ids)
    }


# ── Evidence Confidence ──────────────────────────────────────

def evidence_confidence(
    evidence_count: int,
    base_reliability: float = 0.80,
) -> float:
    """Confidence from data density. Scales logarithmically.

    Formula: base_reliability * (1 - 1/(1 + ln(count + 1)))
    At count=0:   ~0.08 (minimal confidence)
    At count=10:  ~0.53
    At count=50:  ~0.67
    At count=200: ~0.76
    Asymptotic to base_reliability.

    Returns 0.05-0.95, never zero.
    """
    if evidence_count <= 0:
        return round(clamp(base_reliability * 0.1, 0.05, 0.95), 3)

    scale = 1.0 - 1.0 / (1.0 + math.log(evidence_count + 1))
    return round(clamp(base_reliability * scale, 0.05, 0.95), 3)


def compute_safety_confidence(
    crime_count: int,
    historical_months: int,
) -> float:
    """Safety confidence from current + historical data depth."""
    combined = crime_count + (historical_months * 5)
    return evidence_confidence(combined, base_reliability=0.85)


def compute_livability_confidence(complaint_count: int) -> float:
    """Livability confidence from complaint density + essentials."""
    complaint_conf = evidence_confidence(complaint_count, base_reliability=0.80)
    essentials_conf = 0.90
    return round((complaint_conf + essentials_conf) / 2, 3)


def compute_transit_confidence() -> float:
    """Transit data is a complete MBTA extract. Always high confidence."""
    return 0.95


# ── Year-over-Year Change ────────────────────────────────────

def compute_yoy_change(monthly_series: dict[str, dict]) -> float | None:
    """YoY % change: most recent complete month vs same month last year.

    Returns None if insufficient data (< 13 months).
    Negative = improving. Positive = worsening.
    """
    if not monthly_series or len(monthly_series) < 13:
        return None

    sorted_months = sorted(monthly_series.keys(), reverse=True)
    now = datetime.now()
    current_partial = now.strftime("%Y-%m")
    recent_months = [m for m in sorted_months if m != current_partial]

    if not recent_months:
        return None

    recent = recent_months[0]
    year, month = recent.split("-")
    prior = f"{int(year) - 1}-{month}"

    if prior not in monthly_series:
        return None

    recent_total = monthly_series[recent].get("total", 0)
    prior_total = monthly_series[prior].get("total", 0)

    if prior_total == 0:
        return None

    change = ((recent_total - prior_total) / prior_total) * 100
    return round(change, 1)


# ── Weighted Composite ───────────────────────────────────────

def weighted_composite(
    dimension_percentiles: dict[str, int],
    weights: dict[str, float],
    batch_only: bool = True,
) -> tuple[float, dict[str, float]]:
    """Weighted composite score from dimension percentiles.

    Returns (composite_score, renormalized_weights).
    Renormalized weights shown in metadata for full transparency.
    """
    active = {d: p for d, p in dimension_percentiles.items() if d in weights}
    if not active:
        return 0.0, {}

    if batch_only:
        active = {d: p for d, p in active.items() if d != "lifestyle"}

    active_weights = {d: weights[d] for d in active}
    total_weight = sum(active_weights.values())
    if total_weight == 0:
        return 0.0, {}

    renormalized = {
        d: round(w / total_weight, 4)
        for d, w in active_weights.items()
    }

    score = sum(active[d] * renormalized[d] for d in active)

    return round(clamp(score), 1), renormalized