"""Production guardrails for the Vicinity agent graph.

Standard layers:
  1. PII sanitization — scrub SSNs, credit cards, phone numbers from
     input and output. Regex-based, deterministic, zero latency.
  2. Tool health     — detect systemic tool failures before synthesis.
  3. Response quality — empty detection, length enforcement.

Grounding is handled architecturally: tool results in context +
specific system prompts. Post-hoc regex grounding checks are unreliable
(number formatting mismatches, abbreviation differences) and give false
confidence. The system prompts ARE the grounding mechanism.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from langchain_core.messages import ToolMessage

logger = structlog.get_logger()


# -- PII Patterns (standard regexes) ----------------------------------

_PII_PATTERNS = {
    "ssn": re.compile(r"\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "phone_us": re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ),
    "email": re.compile(
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
    ),
}

_PII_REPLACEMENT = "[REDACTED]"

# Whitelist patterns that look like PII but aren't (listing IDs, zip codes, etc.)
_PII_WHITELIST = re.compile(
    r"\b\d{5}(?:-\d{4})?\b"  # zip codes (02134, 02134-1234)
    r"|\blst-[a-z0-9]+\b"     # listing IDs
    r"|\bsig-[a-z0-9]+\b"     # signal IDs
)


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Remove PII patterns from text. Returns (cleaned, list of types found).

    Skips patterns that match the whitelist (zip codes, internal IDs).
    Phone numbers in business listings (from OSM amenity data) are public
    and intentionally NOT scrubbed — they appear in tool results, not user input.
    """
    found = []
    result = text

    for pii_type, pattern in _PII_PATTERNS.items():
        matches = pattern.findall(result)
        for match in matches:
            # Skip whitelisted patterns
            if _PII_WHITELIST.match(match):
                continue
            # Skip short matches that are likely not PII (e.g. "100" matching phone)
            if pii_type == "phone_us" and len(match.replace("-", "").replace(" ", "")) < 10:
                continue
            if pii_type == "credit_card" and len(match.replace("-", "").replace(" ", "")) < 13:
                continue

            result = result.replace(match, _PII_REPLACEMENT)
            if pii_type not in found:
                found.append(pii_type)

    if found:
        logger.warning("pii_scrubbed", types=found, original_length=len(text))

    return result, found


# -- Tool Health -------------------------------------------------------

def check_tool_health(messages: list) -> dict:
    """Analyze ToolMessages for systemic failures.

    Returns:
        total:      number of ToolMessages
        errors:     number that contain error indicators
        all_failed: True if every tool call returned an error
    """
    total = 0
    errors = 0

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        total += 1
        content = (msg.content or "").lower()
        if '"success": false' in content or '"success":false' in content:
            errors += 1

    return {
        "total": total,
        "errors": errors,
        "all_failed": total > 0 and errors == total,
    }


# -- Fallback messages -------------------------------------------------

TOOL_FAILURE_MSG = (
    "I wasn't able to retrieve the data needed to answer your question. "
    "All data sources returned errors. This might be a temporary issue "
    "with our database connection. Please try again in a moment."
)

EMPTY_RETRY_NUDGE = (
    "Your previous response was empty. The user is waiting for an answer. "
    "Review the tool results in this conversation and synthesize them into "
    "a clear, specific response. If tools returned errors, say so honestly."
)

EMPTY_EXHAUSTED_MSG = (
    "I'm sorry, I wasn't able to generate a response. "
    "Please try rephrasing your question."
)