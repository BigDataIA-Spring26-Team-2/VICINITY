"""Message sanitization for LLM-input safety.

The LLM APIs require that every AIMessage with tool_calls be followed
by ToolMessages matching each tool_call_id. If the state history
contains an AIMessage with tool_calls that were never answered (HITL
rejected/modified, crash between tool_call and ToolMessage, partial
checkpointer load), the next LLM call rejects the conversation with:

  "An assistant message with 'tool_calls' must be followed by tool
   messages responding to each 'tool_call_id'."

sanitize_messages removes any AIMessage whose tool_calls have no
matching ToolMessage. Safe to call on any state["messages"] slice
before building an LLM input.
"""

from __future__ import annotations

from typing import Iterable, List

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


def sanitize_messages(messages: Iterable[BaseMessage]) -> List[BaseMessage]:
    """Return a copy of messages with orphaned tool_calls stripped.

    For each AIMessage with tool_calls, if any tool_call_id has no
    matching ToolMessage later in the list, that AIMessage is replaced
    with a plain AIMessage (content only, tool_calls dropped). Content-
    less AIMessages with orphaned tool_calls are removed entirely.
    ToolMessages whose tool_call_id is not declared by any AIMessage
    are also dropped.

    Relative order of surviving messages is preserved.
    """
    msg_list = list(messages)

    answered_ids = {
        m.tool_call_id
        for m in msg_list
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }

    declared_ids = set()
    for m in msg_list:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    declared_ids.add(tc_id)

    out: List[BaseMessage] = []
    for m in msg_list:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            tc_ids = [
                tc.get("id") for tc in m.tool_calls
                if isinstance(tc, dict) and tc.get("id")
            ]
            if tc_ids and all(tc_id in answered_ids for tc_id in tc_ids):
                out.append(m)
            else:
                content = m.content or ""
                if content:
                    out.append(AIMessage(content=content))
            continue

        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", None)
            if tc_id and tc_id in declared_ids and tc_id in answered_ids:
                out.append(m)
            continue

        out.append(m)

    return out