"""LLM-backed classification with Snowflake cache and provider fallback.

Distinct values are classified once and cached. Subsequent runs hit cache.
Provider fallback: tries each provider in config order until one succeeds.
Every LLM call is cost-tracked to RAW.LLM_USAGE_LOG.
"""

import json
import time
import uuid
import os
from typing import Optional

import structlog
from openai import OpenAI

from app.core.config_loader import load_classification

logger = structlog.get_logger()


class LLMProvider:
    """Single LLM provider wrapping the OpenAI SDK."""

    def __init__(self, name: str, model: str, base_url: str, api_key: str):
        self.name = name
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, system: str, user: str,
                 temperature: float, max_tokens: int) -> dict:
        """Call the LLM. Returns parsed response with usage metadata."""
        start = time.perf_counter()

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

        duration_ms = int((time.perf_counter() - start) * 1000)
        content = response.choices[0].message.content.strip()
        usage = response.usage

        return {
            "content": content,
            "model": self.model,
            "provider": self.name,
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "duration_ms": duration_ms,
        }


class ProviderChain:
    """Fallback chain of LLM providers. Tries each in order."""

    def __init__(self):
        config = load_classification()
        llm_config = config["llm"]
        self._temperature = llm_config["temperature"]
        self._max_tokens = llm_config["max_tokens"]
        self._providers: list[LLMProvider] = []
        self._log = logger.bind(component="provider_chain")

        for p in llm_config["providers"]:
            api_key = os.getenv(p["env_key"], "")
            if not api_key:
                self._log.warning("provider_skipped_no_key", provider=p["name"])
                continue
            self._providers.append(
                LLMProvider(
                    name=p["name"],
                    model=p["model"],
                    base_url=p["base_url"],
                    api_key=api_key,
                )
            )

        if not self._providers:
            raise RuntimeError("No LLM providers configured with valid API keys")

        self._log.info("providers_initialized",
                       count=len(self._providers),
                       names=[p.name for p in self._providers])

    def complete(self, system: str, user: str) -> dict:
        """Try each provider in order. First success wins."""
        last_error = None

        for provider in self._providers:
            try:
                result = provider.complete(
                    system=system,
                    user=user,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                self._log.debug("llm_call_success",
                                provider=provider.name,
                                duration_ms=result["duration_ms"])
                return result

            except Exception as e:
                last_error = e
                self._log.warning("provider_failed",
                                  provider=provider.name,
                                  error=str(e))
                continue

        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


class CostTracker:
    """Logs LLM token usage and cost to RAW.LLM_USAGE_LOG."""

    def __init__(self, cursor, pipeline_run_id: str, source: str):
        self._cursor = cursor
        self._pipeline_run_id = pipeline_run_id
        self._source = source
        self._config = load_classification()
        self._costs = self._config.get("cost_per_million_tokens", {})
        self._log = logger.bind(component="cost_tracker", source=source)

    def log_usage(self, llm_result: dict, operation: str, batch_size: int):
        """Write one usage record to Snowflake."""
        model = llm_result["model"]
        model_costs = self._costs.get(model, {"input": 0.0, "output": 0.0})

        input_cost = llm_result["input_tokens"] * model_costs["input"] / 1_000_000
        output_cost = llm_result["output_tokens"] * model_costs["output"] / 1_000_000
        total_cost = input_cost + output_cost

        self._cursor.execute(
            "INSERT INTO RAW.LLM_USAGE_LOG "
            "(id, pipeline_run_id, source, operation, model, "
            " input_tokens, output_tokens, total_tokens, "
            " input_cost_usd, output_cost_usd, total_cost_usd, "
            " batch_size, duration_ms) "
            "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s",
            (
                str(uuid.uuid4()),
                self._pipeline_run_id,
                self._source,
                operation,
                model,
                llm_result["input_tokens"],
                llm_result["output_tokens"],
                llm_result["total_tokens"],
                round(input_cost, 6),
                round(output_cost, 6),
                round(total_cost, 6),
                batch_size,
                llm_result["duration_ms"],
            ),
        )

        self._log.info("usage_logged",
                        model=model,
                        tokens=llm_result["total_tokens"],
                        cost_usd=round(total_cost, 6),
                        operation=operation)


class ClassificationCache:
    """LLM-backed classification with Snowflake cache.

    Classifies distinct field values once, caches in RAW.CLASSIFICATION_CACHE.
    Subsequent runs return cached results. New values trigger LLM calls.

    Usage:
        cache = ClassificationCache(cursor, pipeline_run_id, "crime", "offense_description")
        mappings = cache.classify(["ASSAULT - AGGRAVATED", "SICK ASSIST", ...])
        # Returns: {"ASSAULT - AGGRAVATED": {"severity": "violent", ...}, ...}
    """

    def __init__(self, cursor, pipeline_run_id: str, source: str, field_name: str):
        self._cursor = cursor
        self._source = source
        self._field_name = field_name
        self._chain = ProviderChain()
        self._cost = CostTracker(cursor, pipeline_run_id, source)
        self._log = logger.bind(component="classifier",
                                source=source,
                                field=field_name)

        config = load_classification()
        llm_config = config["llm"]
        self._batch_size = llm_config["batch_size"]
        self._version = llm_config["version"]

        prompt_key = source
        prompts = config.get("prompts", {})
        if prompt_key not in prompts:
            raise ValueError(f"No prompt configured for source '{prompt_key}'")
        self._system_prompt = prompts[prompt_key]["system"]

        self._cache: dict = {}
        self._load_cache()

    def _load_cache(self):
        """Load existing classifications from Snowflake."""
        self._cursor.execute(
            "SELECT raw_value, severity, category, narrative "
            "FROM RAW.CLASSIFICATION_CACHE "
            "WHERE source = %s AND field_name = %s",
            (self._source, self._field_name),
        )
        rows = self._cursor.fetchall()
        for raw_value, severity, category, narrative in rows:
            self._cache[raw_value] = {
                "severity": severity,
                "category": category,
                "narrative": narrative,
            }

        self._log.info("cache_loaded", cached_count=len(self._cache))

    def classify(self, raw_values: list[str]) -> dict:
        """Classify a list of values. Cache hits skip LLM.

        Args:
            raw_values: list of raw field values to classify.

        Returns:
            dict mapping raw_value -> {severity, category, narrative}
        """
        distinct = list(set(v for v in raw_values if v))
        uncached = [v for v in distinct if v not in self._cache]

        if uncached:
            self._log.info("classifying_new_values", count=len(uncached))
            self._classify_batch(uncached)

        return {v: self._cache.get(v, self._fallback(v)) for v in raw_values if v}

    def _classify_batch(self, values: list[str]):
        """Send uncached values to LLM in batches."""
        for i in range(0, len(values), self._batch_size):
            batch = values[i:i + self._batch_size]
            user_prompt = f"Classify these values:\n{json.dumps(batch)}"

            try:
                result = self._chain.complete(
                    system=self._system_prompt,
                    user=user_prompt,
                )

                self._cost.log_usage(result, "classify", len(batch))
                parsed = self._parse_response(result["content"], batch)

                for item in parsed:
                    raw = item["raw_value"]
                    classification = {
                        "severity": item.get("severity", "unknown"),
                        "category": item.get("category", "other"),
                        "narrative": item.get("narrative", ""),
                    }
                    self._cache[raw] = classification
                    self._persist(raw, classification, result["model"])

            except Exception as e:
                self._log.error("batch_classification_failed",
                                batch_size=len(batch),
                                error=str(e))
                for v in batch:
                    self._cache[v] = self._fallback(v)

    def _parse_response(self, content: str, expected_values: list[str]) -> list[dict]:
        """Parse LLM JSON response with error tolerance."""
        # Strip markdown fences if present
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()

        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError as e:
            self._log.error("json_parse_failed",
                            error=str(e),
                            content_preview=clean[:200])
            return [{"raw_value": v, "severity": "unknown",
                     "category": "parse_error", "narrative": ""}
                    for v in expected_values]

        if isinstance(parsed, dict):
            parsed = [parsed]

        if not isinstance(parsed, list):
            self._log.error("unexpected_response_type", type=type(parsed).__name__)
            return [{"raw_value": v, "severity": "unknown",
                     "category": "parse_error", "narrative": ""}
                    for v in expected_values]

        return parsed

    def _persist(self, raw_value: str, classification: dict, model: str):
        """Write one classification to Snowflake cache."""
        self._cursor.execute(
            "INSERT INTO RAW.CLASSIFICATION_CACHE "
            "(source, field_name, raw_value, severity, category, "
            " narrative, classified_by, classification_version) "
            "SELECT %s, %s, %s, %s, %s, %s, %s, %s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM RAW.CLASSIFICATION_CACHE "
            "  WHERE source = %s AND field_name = %s AND raw_value = %s"
            ")",
            (
                self._source, self._field_name, raw_value,
                classification["severity"],
                classification["category"],
                classification["narrative"],
                model, self._version,
                self._source, self._field_name, raw_value,
            ),
        )

    @staticmethod
    def _fallback(value: str) -> dict:
        return {
            "severity": "unknown",
            "category": "unclassified",
            "narrative": f"Unclassified: {value}",
        }