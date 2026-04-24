"""Diagnostic: dump the message history for a chat session.

Finds orphaned AIMessage.tool_calls (the cause of the
"assistant message with tool_calls must be followed by tool
messages" OpenAI error).

USAGE:

    python scripts/diagnose_session.py

By default, dumps the most recent 2 sessions. Pass a specific
session_id to target one:

    python scripts/diagnose_session.py 6ba7b810-9dad-11d1-80b4-00c04fd430c8

Output is printed to stdout. Copy everything and paste back.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# --- Snowflake bootstrap (match what the API uses) -------------------
try:
    from app.core.snowflake import get_cursor
except ImportError:
    print("ERROR: cannot import app.core.snowflake — run from project root "
          "with the venv active:")
    print("  cd C:/Users/Admin/OneDrive/Desktop/BIGDATA/VICINITY")
    print("  python scripts/diagnose_session.py")
    sys.exit(1)


SEPARATOR = "=" * 78
SUB_SEP = "-" * 78


def safe_json_loads(raw: Any) -> Any:
    """Snowflake VARIANT / TEXT round-trip can be string or dict."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def format_content(content: Any, limit: int = 200) -> str:
    """Compact preview of message content."""
    if content is None:
        return "<null>"
    if isinstance(content, str):
        s = content.strip()
        if len(s) > limit:
            return repr(s[:limit]) + f"...[+{len(s) - limit} chars]"
        return repr(s)
    if isinstance(content, list):
        return f"<list len={len(content)}>: " + repr(str(content)[:limit])
    return repr(str(content)[:limit])


def dump_session(cursor, session_id: str):
    """Print a full diagnostic dump of one session's message history."""
    print(SEPARATOR)
    print(f"SESSION: {session_id}")
    print(SEPARATOR)

    # Fetch all messages in order
    cursor.execute("""
        SELECT
            message_id,
            role,
            content,
            tool_calls,
            tool_call_id,
            tool_name,
            created_at
        FROM USER_DATA.CONVERSATION_MESSAGES
        WHERE session_id = %s
        ORDER BY created_at ASC, message_id ASC
    """, (session_id,))

    rows = cursor.fetchall()
    cols = [d[0].lower() for d in cursor.description]
    messages = [dict(zip(cols, row)) for row in rows]

    if not messages:
        print("  (no messages)")
        return

    print(f"  Total messages: {len(messages)}")
    print()

    # Index by tool_call_id for orphan detection
    tool_msgs_by_tcid: dict[str, int] = {}
    for idx, m in enumerate(messages):
        if m["role"] == "tool" and m["tool_call_id"]:
            tool_msgs_by_tcid[m["tool_call_id"]] = idx

    orphans = []
    for idx, m in enumerate(messages):
        role = m["role"] or "?"
        tc_raw = m["tool_calls"]
        tc = safe_json_loads(tc_raw)
        tcid = m["tool_call_id"]
        tname = m["tool_name"]
        created = m["created_at"]
        content_preview = format_content(m["content"])

        # Header line
        line = f"  [{idx:3d}] {created!s:26} {role:10s}"
        if tc:
            if isinstance(tc, list):
                line += f" tool_calls={len(tc)}"
                tc_ids = [t.get("id") for t in tc if isinstance(t, dict)]
                tc_names = [t.get("name") for t in tc if isinstance(t, dict)]
                line += f" ids={tc_ids} names={tc_names}"
            else:
                line += f" tool_calls=<malformed: {type(tc).__name__}>"
        if tcid:
            line += f" tool_call_id={tcid}"
        if tname:
            line += f" tool_name={tname}"
        print(line)
        print(f"        content: {content_preview}")

        # Orphan check: AIMessage with tool_calls → each call ID should
        # have a following ToolMessage with matching tool_call_id
        if role == "assistant" and tc and isinstance(tc, list):
            for t in tc:
                if not isinstance(t, dict):
                    continue
                call_id = t.get("id")
                if not call_id:
                    continue
                matching_idx = tool_msgs_by_tcid.get(call_id)
                if matching_idx is None:
                    orphans.append({
                        "ai_idx": idx,
                        "tool_call_id": call_id,
                        "tool_name": t.get("name"),
                        "reason": "no matching tool_call_id anywhere in history",
                    })
                elif matching_idx < idx:
                    orphans.append({
                        "ai_idx": idx,
                        "tool_call_id": call_id,
                        "tool_name": t.get("name"),
                        "reason": f"matching ToolMessage is at idx {matching_idx} (BEFORE this AIMessage)",
                    })

    print()
    print(SUB_SEP)
    if orphans:
        print(f"  ORPHANED tool_calls FOUND: {len(orphans)}")
        for o in orphans:
            print(f"    - AIMessage idx={o['ai_idx']} "
                  f"tool_call_id={o['tool_call_id']} "
                  f"tool={o['tool_name']} "
                  f"reason={o['reason']}")
    else:
        print("  No orphaned tool_calls — history is well-formed.")
    print()

    # Summary stats
    role_counts: dict[str, int] = {}
    for m in messages:
        r = m["role"] or "?"
        role_counts[r] = role_counts.get(r, 0) + 1
    print("  Role counts:", role_counts)
    print()


def list_recent_sessions(cursor, limit: int = 5) -> list[dict]:
    cursor.execute("""
        SELECT
            session_id,
            user_id,
            MAX(created_at)  AS last_activity,
            COUNT(*)         AS msg_count
        FROM USER_DATA.CONVERSATION_MESSAGES
        GROUP BY session_id, user_id
        ORDER BY last_activity DESC
        LIMIT %s
    """, (limit,))
    rows = cursor.fetchall()
    cols = [d[0].lower() for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


def main():
    target_session = sys.argv[1] if len(sys.argv) > 1 else None

    cursor = get_cursor()
    try:
        if target_session:
            dump_session(cursor, target_session)
            return

        # No arg: dump the 2 most recent sessions
        print(SEPARATOR)
        print("RECENT SESSIONS")
        print(SEPARATOR)
        recent = list_recent_sessions(cursor, limit=5)
        if not recent:
            print("  No sessions found in USER_DATA.CONVERSATION_MESSAGES")
            return

        for s in recent:
            print(f"  {s['session_id']}  user={s['user_id']}  "
                  f"msgs={s['msg_count']}  last={s['last_activity']}")
        print()

        # Dump the 2 most recent
        for s in recent[:2]:
            dump_session(cursor, s["session_id"])

    finally:
        cursor.close()


if __name__ == "__main__":
    main()