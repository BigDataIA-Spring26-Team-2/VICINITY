"""Shared fixtures for MCP module tests.

Provides mock cursor, mock service results, and auth helpers.
No Snowflake, no Redis, no external calls.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional

from app.services.listing_queries import QueryResult


# -- Test data ---------------------------------------------------------

TEST_USER_ID = "u-mcp-test-001"
TEST_EMAIL = "test@vicinity.app"
TEST_PASSWORD = "securepass123"


def make_qr(success=True, data=None, total=None, error=None):
    """Build a QueryResult for mocking service functions."""
    data = data or []
    return QueryResult(
        success=success,
        query_type="test",
        data=data,
        total_count=total if total is not None else len(data),
        error=error,
    )


SAMPLE_LISTING = {
    "listing_id": "lst-mcp-001",
    "street": "100 Test St",
    "city": "Boston",
    "neighborhood": "Allston",
    "price": 2500,
    "beds": 2,
    "baths": 1,
    "sqft": 800,
    "lat": 42.35,
    "lon": -71.13,
    "safety_score": 75,
    "livability_score": 68,
    "source_url": "https://realtor.com/test",
    "primary_photo_url": "https://photos.com/test.jpg",
}

SAMPLE_CRIME = {
    "incident_id": "inc-001",
    "offense_description": "LARCENY",
    "severity": "property",
    "occurred_on_date": "2026-04-15",
    "hour": 14,
    "street": "Main St",
    "distance_m": 150,
}

SAMPLE_NEIGHBORHOOD = {
    "district": "B2",
    "total": 120,
    "violent": 25,
    "property": 65,
    "shootings": 2,
    "streets_affected": 40,
    "most_common_offense": "LARCENY",
}

SAMPLE_AMENITY = {
    "osm_id": 12345,
    "name": "CVS Pharmacy",
    "subcategory": "pharmacy",
    "lat": 42.351,
    "lon": -71.131,
    "address": "200 Main St",
    "distance_m": 200,
    "opening_hours": "8:00-22:00",
}


@pytest.fixture(autouse=True)
def _reset_caches():
    """Reset service config caches between tests."""
    yield
    for mod_path in (
        "app.services.listing_queries",
        "app.services.crime_queries",
        "app.services.complaint_queries",
        "app.services.amenity_lookup",
    ):
        try:
            import importlib
            mod = importlib.import_module(mod_path)
            if hasattr(mod, "reload_config"):
                mod.reload_config()
        except ImportError:
            pass