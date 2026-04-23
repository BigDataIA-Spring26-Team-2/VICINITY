"""Shared fixtures for service unit tests.

MockCursor simulates Snowflake cursor behavior:
  - Tracks all executed SQL and params
  - Returns configurable rows via set_results()
  - Provides column descriptions matching real schema
  - No Snowflake connection, no network, no side effects
"""

import pytest


class MockCursor:
    """Simulates Snowflake cursor for unit tests."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self._columns: list[str] = []
        self._rows: list[tuple] = []
        self.rowcount: int = 0
        self._error = None

    def set_results(self, columns: list[str], rows: list[tuple]):
        self._columns = columns
        self._rows = rows
        self.rowcount = len(rows)

    def set_error(self, error: Exception):
        self._error = error

    def execute(self, sql: str, params=None):
        if self._error:
            err = self._error
            self._error = None
            raise err
        self.executed.append((sql, params))
        self.rowcount = len(self._rows)

    def executemany(self, sql: str, params_list):
        for p in params_list:
            self.executed.append((sql, p))
        self.rowcount = len(params_list)

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def description(self):
        return [(col.upper(), None, None, None, None, None, None)
                for col in self._columns]

    def close(self):
        pass

    @property
    def last_sql(self) -> str:
        return self.executed[-1][0] if self.executed else ""

    @property
    def last_params(self):
        return self.executed[-1][1] if self.executed else None


@pytest.fixture
def cursor():
    return MockCursor()


@pytest.fixture(autouse=True)
def _reset_service_caches():
    """Clear config caches before and after each test."""
    from app.services import (
        listing_queries, crime_queries, complaint_queries,
        user_data, url_health, sql_freeform, amenity_lookup,
    )
    modules = (listing_queries, crime_queries, complaint_queries,
               user_data, url_health, sql_freeform, amenity_lookup)
    for mod in modules:
        mod.reload_config()
    yield
    for mod in modules:
        mod.reload_config()