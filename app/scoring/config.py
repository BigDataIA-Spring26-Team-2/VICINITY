"""Scoring config — loads scoring.yml into a typed, immutable dataclass.

All radii, windows, thresholds, weights, and essentials read from config.
Precomputes bounding box deltas for spatial query pre-filtering.
"""

import math
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"

# Boston center latitude for longitude delta computation
_BOSTON_LAT = 42.35


@dataclass(frozen=True)
class ScoringConfig:
    """All scoring parameters. Immutable after construction."""

    # Radii (meters)
    safety_radius_m: int
    livability_radius_m: int
    essentials_radius_m: int
    transit_radius_m: int
    corridor_buffer_m: int

    # Time windows
    crime_window_days: int
    complaint_window_days: int
    citizen_window_hours: int

    # Complaint scoring weights
    complaint_qol_weight: float
    complaint_infra_weight: float

    # Lifestyle
    lifestyle_top_k: int
    lifestyle_min_relevance: int

    # Essentials list
    essentials: tuple[str, ...]

    # Composite dimension weights
    composite_defaults: dict[str, float]

    # Metadata storage flags
    store_monthly_series: bool
    store_hourly_distribution: bool
    store_dow_distribution: bool

    # Precomputed bbox deltas — set in __post_init__
    safety_dlat: float = field(init=False, repr=False)
    safety_dlon: float = field(init=False, repr=False)
    livability_dlat: float = field(init=False, repr=False)
    livability_dlon: float = field(init=False, repr=False)
    essentials_dlat: float = field(init=False, repr=False)
    essentials_dlon: float = field(init=False, repr=False)
    transit_dlat: float = field(init=False, repr=False)
    transit_dlon: float = field(init=False, repr=False)

    def __post_init__(self):
        cos_lat = math.cos(math.radians(_BOSTON_LAT))
        for prefix in ("safety", "livability", "essentials", "transit"):
            radius = getattr(self, f"{prefix}_radius_m")
            object.__setattr__(self, f"{prefix}_dlat", radius / 111_000)
            object.__setattr__(self, f"{prefix}_dlon", radius / (111_000 * cos_lat))

    def bbox_deltas(self, prefix: str) -> tuple[float, float]:
        """Return (dlat, dlon) for a given radius prefix."""
        return getattr(self, f"{prefix}_dlat"), getattr(self, f"{prefix}_dlon")


@lru_cache(maxsize=1)
def load_scoring_config() -> ScoringConfig:
    """Load scoring.yml once, return cached typed config."""
    with open(CONFIG_DIR / "scoring.yml") as f:
        raw = yaml.safe_load(f)

    return ScoringConfig(
        safety_radius_m=raw["safety_radius_m"],
        livability_radius_m=raw["livability_radius_m"],
        essentials_radius_m=raw["essentials_radius_m"],
        transit_radius_m=raw["transit_radius_m"],
        corridor_buffer_m=raw["corridor_buffer_m"],
        crime_window_days=raw["crime_window_days"],
        complaint_window_days=raw["complaint_window_days"],
        citizen_window_hours=raw["citizen_window_hours"],
        complaint_qol_weight=raw.get("complaint_qol_weight", 1.0),
        complaint_infra_weight=raw.get("complaint_infra_weight", 0.3),
        lifestyle_top_k=raw["lifestyle_top_k"],
        lifestyle_min_relevance=raw["lifestyle_min_relevance"],
        essentials=tuple(raw["essentials"]),
        composite_defaults=raw["composite_defaults"],
        store_monthly_series=raw.get("store_monthly_series", True),
        store_hourly_distribution=raw.get("store_hourly_distribution", True),
        store_dow_distribution=raw.get("store_dow_distribution", True),
    )