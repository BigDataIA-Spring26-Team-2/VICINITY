"""Config loader — reads YAML configs into validated typed objects."""

import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
SOURCES_DIR = CONFIG_DIR / "sources"


def _load(filepath: Path) -> dict:
    with open(filepath) as f:
        return yaml.safe_load(f)


# ── Typed config objects ─────────────────────────────────────

@dataclass(frozen=True)
class BBox:
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        return (self.min_lat <= lat <= self.max_lat
                and self.min_lon <= lon <= self.max_lon)


@dataclass(frozen=True)
class RateLimitConfig:
    requests_per_second: float = 5.0
    backoff_base: float = 2.0
    backoff_max: float = 30.0
    delay_between_queries: float = 0.0
    delay_between_requests: float = 0.0


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 30.0


@dataclass(frozen=True)
class LLMConfig:
    model: str = "deepseek-chat"
    temperature: float = 0.0
    max_tokens: int = 2000
    batch_size: int = 20
    version: str = "v1"


@dataclass(frozen=True)
class CostConfig:
    input_per_million: float = 0.14
    output_per_million: float = 0.28


# ── Loaders ──────────────────────────────────────────────────

def load_source_config(source_name: str) -> dict:
    return _load(SOURCES_DIR / f"{source_name}.yml")


def load_spatial() -> dict:
    raw = _load(CONFIG_DIR / "spatial.yml")
    return {
        "bbox": BBox(**raw["boston_bbox"]),
        "radii": raw["scoring_radii"],
        "zip_to_neighborhood": raw["zip_to_neighborhood"],
    }


def load_classification() -> dict:
    return _load(CONFIG_DIR / "classification.yml")


def load_pipeline() -> dict:
    raw = _load(CONFIG_DIR / "pipeline.yml")
    return {
        "retry": RetryConfig(**raw["retry"]),
        "scheduling": raw["scheduling"],
    }


def load_llm_config() -> LLMConfig:
    raw = _load(CONFIG_DIR / "classification.yml")
    return LLMConfig(**raw["llm"])


def load_cost_config(model: str) -> CostConfig:
    raw = _load(CONFIG_DIR / "classification.yml")
    costs = raw.get("cost_per_million_tokens", {}).get(model, {})
    return CostConfig(
        input_per_million=costs.get("input", 0.14),
        output_per_million=costs.get("output", 0.28),
    )