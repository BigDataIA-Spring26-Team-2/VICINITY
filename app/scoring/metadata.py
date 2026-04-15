"""Metadata assembly — builds scoring_metadata JSON per listing.

Every number the agent reads comes from here. Full provenance:
config params, raw counts, type breakdowns, historical series,
confidence values, YoY trends, complaint weight formulas,
and lifestyle signal perception overlays.
"""

from datetime import datetime, timezone

from app.scoring.config import ScoringConfig


def resolve_listing_neighborhoods(
    neighborhood: str | None,
    zip_code: str | None,
    zip_to_neighborhood: dict[str, str],
) -> list[str]:
    """Resolve a listing to its neighborhood name(s).

    Handles:
    - Compound names: "West End/Beacon Hill" → ["West End", "Beacon Hill"]
    - Zip fallback: zip_code "02134" → ["Allston"]
    - None: returns []
    """
    if neighborhood:
        return [n.strip() for n in neighborhood.split("/") if n.strip()]

    if zip_code:
        resolved = zip_to_neighborhood.get(str(zip_code).strip()[:5])
        if resolved:
            return [resolved]

    return []


def _match_lifestyle_signals(
    listing_hoods: list[str],
    lifestyle_by_hood: dict[str, dict[str, dict]],
) -> dict[str, dict]:
    """Match lifestyle signals to a listing's neighborhoods.

    A listing in "West End/Beacon Hill" picks up signals from
    both "West End" and "Beacon Hill", merged with deduplication.
    """
    matched: dict[str, dict] = {}

    for hood in listing_hoods:
        hood_signals = lifestyle_by_hood.get(hood, {})
        for tag, data in hood_signals.items():
            if tag not in matched:
                matched[tag] = {
                    "positive": 0, "negative": 0, "mixed": 0,
                    "neutral": 0, "total": 0, "sample_titles": [],
                    "neighborhoods_matched": [],
                }
            m = matched[tag]
            for sent in ("positive", "negative", "mixed", "neutral"):
                m[sent] += data.get(sent, 0)
            m["total"] += data.get("total", 0)
            m["neighborhoods_matched"].append(hood)
            for title in data.get("sample_titles", []):
                if len(m["sample_titles"]) < 5 and title not in m["sample_titles"]:
                    m["sample_titles"].append(title)

    return matched


def build_scoring_metadata(
    listing_id: str,
    cfg: ScoringConfig,
    safety: dict,
    livability: dict,
    transit: dict,
    safety_percentile: int,
    livability_percentile: int,
    transit_percentile: int,
    safety_confidence: float,
    livability_confidence: float,
    transit_confidence: float,
    composite: float,
    renormalized_weights: dict[str, float],
    total_listings: int,
    zip_to_neighborhood: dict[str, str],
    lifestyle_by_hood: dict[str, dict[str, dict]] | None = None,
    yoy_change: float | None = None,
    monthly_series: dict[str, dict] | None = None,
    hourly_distribution: dict[int, int] | None = None,
    dow_distribution: dict[str, int] | None = None,
) -> dict:
    """Assemble the complete scoring_metadata VARIANT for one listing."""
    essentials_present = livability.get("essentials_list", [])
    essentials_missing = [
        e for e in cfg.essentials if e not in essentials_present
    ]

    # Weighted complaint score — shown in metadata for transparency
    qol = (
        livability.get("noise_count", 0)
        + livability.get("pest_count", 0)
        + livability.get("heat_count", 0)
        + livability.get("housing_count", 0)
    )
    infra = livability.get("infra_count", 0)
    effective_complaints = (qol * cfg.complaint_qol_weight) + (infra * cfg.complaint_infra_weight)

    # Composite formula string
    formula_parts = []
    for dim, weight in renormalized_weights.items():
        pct = {
            "safety": safety_percentile,
            "livability": livability_percentile,
            "transit": transit_percentile,
        }.get(dim, 0)
        formula_parts.append(f"{dim}({weight})*{pct}")
    formula_str = " + ".join(formula_parts) + f" = {composite}"

    meta = {
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),

        "methodology": {
            "scoring_method": "percentile_rank",
            "percentile_explanation": (
                f"Ranked across all {total_listings} active Boston listings. "
                f"Percentile 100 = best, 0 = worst."
            ),
            "livability_ranking": (
                f"Weighted complaint score: QoL complaints (noise, pest, heat, housing) "
                f"x {cfg.complaint_qol_weight} + infrastructure complaints x "
                f"{cfg.complaint_infra_weight}. Lower = better. "
                f"Essentials coverage as tiebreaker."
            ),
            "confidence_formula": (
                "base_reliability * (1 - 1/(1 + ln(evidence_count + 1))). "
                "Scales 0.05-0.95. Low confidence = possible data coverage gap."
            ),
            "composite_formula": formula_str,
        },

        "config": {
            "safety_radius_m": cfg.safety_radius_m,
            "livability_radius_m": cfg.livability_radius_m,
            "essentials_radius_m": cfg.essentials_radius_m,
            "transit_radius_m": cfg.transit_radius_m,
            "crime_window_days": cfg.crime_window_days,
            "complaint_window_days": cfg.complaint_window_days,
            "citizen_window_hours": cfg.citizen_window_hours,
            "complaint_qol_weight": cfg.complaint_qol_weight,
            "complaint_infra_weight": cfg.complaint_infra_weight,
            "essentials_defined": list(cfg.essentials),
            "original_weights": cfg.composite_defaults,
            "renormalized_weights": renormalized_weights,
        },

        "composite": {
            "score": composite,
            "formula": formula_str,
            "batch_dimensions_only": True,
        },

        "safety": {
            "percentile": safety_percentile,
            "confidence": safety_confidence,
            "crime_count": safety.get("crime_count", 0),
            "violent_count": safety.get("violent_count", 0),
            "shooting_count": safety.get("shooting_count", 0),
            "offense_types": safety.get("offense_types", 0),
            "citizen_48h": safety.get("citizen_total", 0),
            "citizen_nighttime_48h": safety.get("citizen_nighttime", 0),
            "citizen_critical_48h": safety.get("citizen_critical", 0),
            "yoy_change_pct": yoy_change,
            "interpretation": _safety_interpretation(
                safety_percentile, safety_confidence,
                safety.get("crime_count", 0),
                safety.get("violent_count", 0),
                safety.get("citizen_total", 0),
                total_listings,
            ),
        },

        "livability": {
            "percentile": livability_percentile,
            "confidence": livability_confidence,
            "complaint_count_total": livability.get("complaint_count", 0),
            "effective_complaint_score": round(effective_complaints, 1),
            "noise_count": livability.get("noise_count", 0),
            "pest_count": livability.get("pest_count", 0),
            "heat_count": livability.get("heat_count", 0),
            "housing_count": livability.get("housing_count", 0),
            "infra_count": livability.get("infra_count", 0),
            "essentials_found": livability.get("essentials_found", 0),
            "essentials_total": len(cfg.essentials),
            "essentials_present": essentials_present,
            "essentials_missing": essentials_missing,
            "total_amenities": livability.get("total_amenities", 0),
        },

        "transit": {
            "percentile": transit_percentile,
            "confidence": transit_confidence,
            "stop_count": transit.get("stop_count", 0),
            "stop_names": transit.get("stop_names", []),
        },
    }

    # Historical series
    if monthly_series is not None:
        meta["safety"]["monthly_series"] = monthly_series

    if hourly_distribution is not None:
        meta["safety"]["hourly_distribution"] = {
            str(k): v for k, v in sorted(hourly_distribution.items())
        }

    if dow_distribution is not None:
        meta["safety"]["dow_distribution"] = dow_distribution

    # Lifestyle signal overlay — matched by listing neighborhood(s)
    if lifestyle_by_hood:
        listing_hoods = resolve_listing_neighborhoods(
            safety.get("neighborhood"),
            safety.get("zip_code"),
            zip_to_neighborhood,
        )

        if listing_hoods:
            matched = _match_lifestyle_signals(listing_hoods, lifestyle_by_hood)

            if matched:
                meta["lifestyle_overlay"] = matched

                safety_signals = matched.get("safety")
                if safety_signals:
                    meta["safety"]["community_perception"] = {
                        "positive": safety_signals["positive"],
                        "negative": safety_signals["negative"],
                        "mixed": safety_signals["mixed"],
                        "total": safety_signals["total"],
                        "neighborhoods_matched": safety_signals["neighborhoods_matched"],
                        "sample_titles": safety_signals["sample_titles"],
                    }

                noise_signals = matched.get("noise")
                if noise_signals:
                    meta["livability"]["noise_perception"] = {
                        "positive": noise_signals["positive"],
                        "negative": noise_signals["negative"],
                        "mixed": noise_signals["mixed"],
                        "total": noise_signals["total"],
                        "sample_titles": noise_signals["sample_titles"],
                    }

    return meta


def _safety_interpretation(
    percentile: int,
    confidence: float,
    crime_count: int,
    violent_count: int,
    citizen_total: int,
    total_listings: int,
) -> str:
    """Pre-built interpretation the agent can use or override."""
    parts = [
        f"Safety at {percentile}th percentile across "
        f"{total_listings} listings.",
    ]

    if confidence < 0.3:
        parts.append(
            "LOW CONFIDENCE: limited data in this area — "
            "score may reflect a data coverage gap rather than "
            "actual safety conditions."
        )

    if crime_count == 0:
        parts.append("Zero crime incidents in the scoring window.")
    else:
        violent_pct = round(violent_count / crime_count * 100)
        parts.append(
            f"{crime_count} incidents, {violent_count} violent "
            f"({violent_pct}% violent rate)."
        )

    if citizen_total > 0:
        parts.append(
            f"Citizen app shows {citizen_total} recent live reports."
        )

    return " ".join(parts)


def build_summary_fields(
    listing: dict,
    safety_percentile: int,
    livability_percentile: int,
    safety_meta: dict,
    livability_meta: dict,
    transit_stops: list[str],
) -> dict:
    """Denormalized fields for LISTING_SUMMARY update."""
    return {
        "listing_id": listing.get("listing_id"),
        "safety_score": safety_percentile,
        "livability_score": livability_percentile,
        "safety_metadata": safety_meta,
        "livability_metadata": livability_meta,
        "nearest_stops": transit_stops[:5] if transit_stops else [],
        "last_scored_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }