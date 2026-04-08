"""Data extractors — paginated fetch with retry and rate limiting.

Yields pages as they arrive. Caller processes incrementally.
No full-dataset accumulation in memory.
"""

import time
from typing import Optional, Generator
from dataclasses import dataclass

import httpx
import structlog

from app.core.config_loader import load_source_config

logger = structlog.get_logger()


@dataclass
class PageResult:
    records: list[dict]
    page_number: int
    total_available: int


@dataclass
class ExtractionSummary:
    total_records: int
    total_available: int
    pages_fetched: int
    duration_ms: int


class CKANExtractor:
    """Paginated CKAN datastore_search with retry and rate limiting.

    Yields one page at a time via extract_pages(). Caller decides
    what to do with each page — no full accumulation in memory.

    Usage:
        extractor = CKANExtractor("crime")
        for page in extractor.extract_pages(since="2026-04-01", max_records=100):
            process(page.records)
    """

    def __init__(self, source_name: str):
        config = load_source_config(source_name)
        self._base_url = config["connection"]["base_url"]
        self._resource_id = config["connection"]["resource_id"]
        self._page_size = config["connection"]["page_size"]
        self._max_pages = config["connection"]["max_pages"]
        self._date_field = config["keys"]["date_field"]
        self._source_name = source_name

        rate = config.get("rate_limit", {})
        rps = rate.get("requests_per_second", 5.0)
        self._backoff_base = rate.get("backoff_base", 2.0)
        self._backoff_max = rate.get("backoff_max", 30.0)
        self._min_interval = 1.0 / rps if rps > 0 else 0.2

        self._log = logger.bind(extractor="ckan", source=source_name)

    def extract_pages(self, since: Optional[str] = None,
                      until: Optional[str] = None,
                      max_records: Optional[int] = None
                      ) -> Generator[PageResult, None, ExtractionSummary]:
        """Yield pages of records. Stops at max_records, max_pages, or data exhaustion.

        Args:
            since: ISO date string. Only records after this date.
            until: ISO date string. Only records before this date.
            max_records: Stop after yielding this many records total.

        Yields:
            PageResult with records for that page.

        Returns:
            ExtractionSummary after generator exhaustion.
        """
        start = time.perf_counter()
        offset = 0
        pages = 0
        total_yielded = 0
        total_available = 0
        sort = f"{self._date_field} desc"

        while pages < self._max_pages:
            if max_records and total_yielded >= max_records:
                self._log.info("max_records_reached", total=total_yielded)
                break

            params = {
                "resource_id": self._resource_id,
                "limit": self._page_size,
                "offset": offset,
                "sort": sort,
            }

            data = self._fetch_page(params)
            if data is None:
                break

            records = data.get("result", {}).get("records", [])
            total_available = data.get("result", {}).get("total", 0)

            if not records:
                break

            # Trim to max_records
            if max_records:
                remaining = max_records - total_yielded
                if len(records) > remaining:
                    records = records[:remaining]

            pages += 1
            total_yielded += len(records)

            self._log.debug("page_fetched",
                            page=pages,
                            records=len(records),
                            total_so_far=total_yielded)

            yield PageResult(
                records=records,
                page_number=pages,
                total_available=total_available,
            )

            # Early termination: fewer records than page size
            if len(records) < self._page_size:
                break

            # Early termination: passed the watermark date
            if since and self._past_watermark(records, since):
                break

            offset += self._page_size
            time.sleep(self._min_interval)

        duration_ms = int((time.perf_counter() - start) * 1000)

        self._log.info("extraction_complete",
                       total_records=total_yielded,
                       total_available=total_available,
                       pages=pages,
                       duration_ms=duration_ms)

    def _fetch_page(self, params: dict) -> Optional[dict]:
        """Single page fetch with retry."""
        for attempt in range(1, 4):
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.get(self._base_url, params=params)

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 429:
                    wait = min(self._backoff_base ** attempt, self._backoff_max)
                    self._log.warning("rate_limited", attempt=attempt, wait_s=wait)
                    time.sleep(wait)
                    continue

                self._log.error("http_error", status=resp.status_code, attempt=attempt)
                time.sleep(self._backoff_base ** attempt)

            except httpx.TimeoutException:
                wait = min(self._backoff_base ** attempt, self._backoff_max)
                self._log.warning("timeout", attempt=attempt, wait_s=wait)
                time.sleep(wait)

            except httpx.RequestError as e:
                self._log.error("request_error", error=str(e), attempt=attempt)
                time.sleep(self._backoff_base ** attempt)

        self._log.error("page_fetch_exhausted", params=params)
        return None

    def _past_watermark(self, records: list[dict], since: str) -> bool:
        """Check if the last record is older than the watermark."""
        last_date = str(records[-1].get(self._date_field, ""))[:10]
        if last_date and last_date < since[:10]:
            self._log.info("early_termination",
                           last_date=last_date,
                           watermark=since[:10])
            return True
        return False


class CKANMultiExtractor:
    """Extracts from multiple CKAN resource IDs with field normalization.

    Used for 311 complaints where 3 resource IDs have different schemas.
    Yields pages across all variants sequentially.
    """

    def __init__(self, source_name: str):
        self._config = load_source_config(source_name)
        self._source_name = source_name
        self._log = logger.bind(extractor="ckan_multi", source=source_name)

    def extract_pages(self, since: Optional[str] = None,
                      until: Optional[str] = None,
                      max_records: Optional[int] = None
                      ) -> Generator[PageResult, None, None]:
        """Yield normalized pages from all variants."""
        total_yielded = 0

        for variant_name, variant in self._config["variants"].items():
            if max_records and total_yielded >= max_records:
                break

            self._log.info("extracting_variant", variant=variant_name)
            remaining = (max_records - total_yielded) if max_records else None
            field_mapping = variant.get("field_mapping", {})

            extractor = _VariantExtractor(
                base_url=self._config["connection"]["base_url"],
                resource_id=variant["resource_id"],
                page_size=self._config["connection"]["page_size"],
                max_pages=self._config["connection"]["max_pages"],
                date_field=variant["date_field"],
                field_mapping=field_mapping,
                source_name=self._source_name,
                variant_name=variant_name,
            )

            for page in extractor.extract_pages(since=since, until=until,
                                                max_records=remaining):
                total_yielded += len(page.records)
                yield page

        self._log.info("multi_extraction_complete", total_records=total_yielded)


class _VariantExtractor:
    """Internal. Fetches one CKAN variant and normalizes field names."""

    def __init__(self, base_url: str, resource_id: str, page_size: int,
                 max_pages: int, date_field: str, field_mapping: dict,
                 source_name: str, variant_name: str):
        self._base_url = base_url
        self._resource_id = resource_id
        self._page_size = page_size
        self._max_pages = max_pages
        self._date_field = date_field
        self._field_mapping = field_mapping
        self._reverse_map = {v: k for k, v in field_mapping.items()}
        self._log = logger.bind(extractor="ckan_variant",
                                source=source_name,
                                variant=variant_name)

    def extract_pages(self, since: Optional[str] = None,
                      until: Optional[str] = None,
                      max_records: Optional[int] = None
                      ) -> Generator[PageResult, None, None]:
        offset = 0
        pages = 0
        total_yielded = 0

        while pages < self._max_pages:
            if max_records and total_yielded >= max_records:
                break

            params = {
                "resource_id": self._resource_id,
                "limit": self._page_size,
                "offset": offset,
                "sort": f"{self._date_field} desc",
            }

            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.get(self._base_url, params=params)

                if resp.status_code != 200:
                    self._log.error("http_error", status=resp.status_code)
                    break

                data = resp.json()
                records = data.get("result", {}).get("records", [])
                total_available = data.get("result", {}).get("total", 0)

                if not records:
                    break

                # Trim to max_records
                if max_records:
                    remaining = max_records - total_yielded
                    if len(records) > remaining:
                        records = records[:remaining]

                # Normalize field names
                if self._reverse_map:
                    records = [self._normalize(r) for r in records]

                pages += 1
                total_yielded += len(records)

                yield PageResult(
                    records=records,
                    page_number=pages,
                    total_available=total_available,
                )

                if len(records) < self._page_size:
                    break

                if since:
                    last_date = str(records[-1].get(self._date_field, ""))[:10]
                    if last_date and last_date < since[:10]:
                        break

                offset += self._page_size
                time.sleep(0.2)

            except Exception as e:
                self._log.error("fetch_error", error=str(e))
                break

    def _normalize(self, record: dict) -> dict:
        """Apply field mapping to normalize variant schema to canonical."""
        normalized = {}
        for key, value in record.items():
            canonical = self._reverse_map.get(key, key)
            normalized[canonical] = value
        return normalized