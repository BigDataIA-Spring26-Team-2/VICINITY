"""Tests for app.agents.guardrails — PII scrubbing, tool health."""

import pytest
from langchain_core.messages import ToolMessage

from app.agents.guardrails import (
    scrub_pii,
    check_tool_health,
    TOOL_FAILURE_MSG,
    EMPTY_RETRY_NUDGE,
    EMPTY_EXHAUSTED_MSG,
)


class TestScrubPii:

    def test_ssn_dashes(self):
        text, found = scrub_pii("My SSN is 123-45-6789 ok?")
        assert "123-45-6789" not in text
        assert "[REDACTED]" in text
        assert "ssn" in found

    def test_ssn_spaces(self):
        text, found = scrub_pii("SSN: 123 45 6789")
        assert "123 45 6789" not in text
        assert "ssn" in found

    def test_ssn_no_separators(self):
        text, found = scrub_pii("Number 123456789 here")
        assert "ssn" in found

    def test_credit_card_16_digits(self):
        text, found = scrub_pii("Card: 4111-1111-1111-1111")
        assert "4111" not in text
        assert "credit_card" in found

    def test_email(self):
        text, found = scrub_pii("Contact me at john@example.com please")
        assert "john@example.com" not in text
        assert "email" in found

    def test_phone_us(self):
        text, found = scrub_pii("Call (617) 555-1234 for info")
        assert "555-1234" not in text
        assert "phone_us" in found

    def test_zip_code_not_scrubbed(self):
        text, found = scrub_pii("Zip code is 02134")
        assert "02134" in text
        assert "ssn" not in found

    def test_listing_id_not_scrubbed(self):
        text, found = scrub_pii("Check listing lst-abc123 details")
        assert "lst-abc123" in text

    def test_no_pii_returns_unchanged(self):
        original = "Show me apartments in Allston under $2500"
        text, found = scrub_pii(original)
        assert text == original
        assert found == []

    def test_multiple_pii_types(self):
        text, found = scrub_pii("SSN 123-45-6789 email test@x.com")
        assert "ssn" in found
        assert "email" in found
        assert text.count("[REDACTED]") == 2

    def test_short_numbers_not_phone(self):
        text, found = scrub_pii("There are 500 crimes in 30 days")
        assert "phone_us" not in found
        assert "500" in text


class TestCheckToolHealth:

    def test_no_tools(self):
        result = check_tool_health([])
        assert result["total"] == 0
        assert result["errors"] == 0
        assert result["all_failed"] is False

    def test_all_success(self):
        msgs = [
            ToolMessage(content='{"success": true, "data": [1]}', tool_call_id="c1"),
            ToolMessage(content='{"success": true, "data": [2]}', tool_call_id="c2"),
        ]
        result = check_tool_health(msgs)
        assert result["total"] == 2
        assert result["errors"] == 0
        assert result["all_failed"] is False

    def test_all_failed(self):
        msgs = [
            ToolMessage(content='{"success": false, "error": "timeout"}', tool_call_id="c1"),
            ToolMessage(content='{"success": false, "error": "connection"}', tool_call_id="c2"),
        ]
        result = check_tool_health(msgs)
        assert result["total"] == 2
        assert result["errors"] == 2
        assert result["all_failed"] is True

    def test_partial_failure(self):
        msgs = [
            ToolMessage(content='{"success": true, "data": [1]}', tool_call_id="c1"),
            ToolMessage(content='{"success": false, "error": "oops"}', tool_call_id="c2"),
        ]
        result = check_tool_health(msgs)
        assert result["total"] == 2
        assert result["errors"] == 1
        assert result["all_failed"] is False

    def test_ignores_non_tool_messages(self):
        from langchain_core.messages import AIMessage
        msgs = [AIMessage(content='{"success": false}')]
        result = check_tool_health(msgs)
        assert result["total"] == 0


class TestConstants:

    def test_tool_failure_msg_not_empty(self):
        assert len(TOOL_FAILURE_MSG) > 20

    def test_retry_nudge_not_empty(self):
        assert len(EMPTY_RETRY_NUDGE) > 20

    def test_exhausted_msg_not_empty(self):
        assert len(EMPTY_EXHAUSTED_MSG) > 20