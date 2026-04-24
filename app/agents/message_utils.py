"""Message list sanitization.

One function: sanitize_messages. Enforces the OpenAI / DeepSeek
contract that every AIMessage with tool_calls is immediately
followed by ToolMessages — one per tool_call_id. Violations
cause the LLM API to reject the entire request.

Called at the top of every agent node (chat_react, search_node,
report_node, organizer_plan), before the LLM is invoked. Any
orphaned tool_calls that reach a node — from mid-turn crashes,
partial checkpointer writes, or a future persistent checkpointer —
get paired with a stub ToolMessage before the LLM sees them.

Pure function. Does not mutate input. Returns a new list.
"""

from __future__ import annotations

import json
from typing import Iterable

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


_STUB_CONTENT = json.dumps({
    "success": False,
    "error": "Tool execution did not complete.",
})


def sanitize_messages(messages: Iterable[BaseMessage]) -> list[BaseMessage]:
    """Return a well-formed message list.

    For every AIMessage with tool_calls, each tool_call_id must have a
    following ToolMessage in the list. Missing matches get a stub
    ToolMessage inserted immediately after the AIMessage.

    Does not mutate input.
    """
    msgs = list(messages)
    if not msgs:
        return msgs

    # Index every existing ToolMessage by tool_call_id
    existing_tool_ids = {
        m.tool_call_id for m in msgs
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }

    out: list[BaseMessage] = []
    for m in msgs:
        out.append(m)
        if not isinstance(m, AIMessage):
            continue
        tcs = getattr(m, "tool_calls", None) or []
        for tc in tcs:
            tc_id = tc.get("id") if isinstance(tc, dict) else None
            if not tc_id or tc_id in existing_tool_ids:
                continue
            out.append(ToolMessage(
                content=_STUB_CONTENT,
                tool_call_id=tc_id,
                name=tc.get("name", "unknown"),
            ))
            existing_tool_ids.add(tc_id)

    return out