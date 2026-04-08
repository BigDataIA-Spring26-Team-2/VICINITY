"""Record validator — spatial, null, and type checks before loading.

Config-driven from spatial.yml. Reusable across all geo-spatial pipelines.
Failed records are returned, not discarded — caller decides to log or skip.
"""

from dataclasses import dataclass
from typing import Optional
from app.core.config_loader import load_spatial, BBox


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]


class RecordValidator:
    """Validates raw records against spatial and completeness rules.

    Usage:
        validator = RecordValidator()
        result = validator.validate(record, lat_field="Lat", lon_field="Long",
                                    required=["INCIDENT_NUMBER", "OFFENSE_DESCRIPTION"])
        if not result.valid:
            pipeline.record_error(...)
    """

    def __init__(self):
        spatial = load_spatial()
        self._bbox: BBox = spatial["bbox"]
        self._zip_to_neighborhood: dict = spatial["zip_to_neighborhood"]

    def validate(self, record: dict, lat_field: str, lon_field: str,
                 required: Optional[list[str]] = None) -> ValidationResult:
        """Run all checks. Returns ValidationResult with error list."""
        errors = []

        if required:
            errors.extend(self._check_required(record, required))

        coord_errors = self._check_coordinates(record, lat_field, lon_field)
        errors.extend(coord_errors)

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def _check_required(self, record: dict, fields: list[str]) -> list[str]:
        errors = []
        for f in fields:
            val = record.get(f)
            if val is None or str(val).strip() == "":
                errors.append(f"missing_field:{f}")
        return errors

    def _check_coordinates(self, record: dict, lat_field: str, lon_field: str) -> list[str]:
        errors = []
        raw_lat = record.get(lat_field)
        raw_lon = record.get(lon_field)

        if raw_lat is None or raw_lon is None:
            errors.append("null_coordinates")
            return errors

        try:
            lat = float(raw_lat)
            lon = float(raw_lon)
        except (ValueError, TypeError):
            errors.append(f"unparseable_coordinates:lat={raw_lat},lon={raw_lon}")
            return errors

        if lat == 0.0 or lon == 0.0:
            errors.append("zero_coordinates")
            return errors

        if not self._bbox.contains(lat, lon):
            errors.append(f"outside_bbox:lat={lat},lon={lon}")

        return errors

    # ── Type casting helpers ─────────────────────────────────

    @staticmethod
    def to_float(val, default: Optional[float] = None) -> Optional[float]:
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def to_int(val, default: Optional[int] = None) -> Optional[int]:
        if val is None:
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def to_bool(val, true_values: tuple = ("1", "true", "yes", "Y")) -> bool:
        if val is None:
            return False
        return str(val).strip().lower() in true_values

    @staticmethod
    def to_str(val, max_len: Optional[int] = None) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        if not s:
            return None
        if max_len:
            s = s[:max_len]
        return s

    def resolve_neighborhood(self, zip_code: Optional[str]) -> Optional[str]:
        """Map zip code to Boston neighborhood from spatial config."""
        if not zip_code:
            return None
        return self._zip_to_neighborhood.get(str(zip_code).strip()[:5])