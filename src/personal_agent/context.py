"""Bound request context without mutating the durable task/receipt history."""
import json

from .contracts import encode


def recovery_operations(operations):
    public = []
    for op in operations:
        result = dict(op["result"] or {})
        for key in ("text", "artifact_text", "operations", "task"):
            result.pop(key, None)
        arguments = {k: v for k, v in op["arguments"].items() if k != "content"} if isinstance(op["arguments"], dict) else {"rejected_arguments": "malformed or non-object"}
        public.append({k: op[k] for k in ("id", "run_id", "tool", "status")} | {"arguments": arguments, "result": result})
    return public


# Older tool results keep their identity and a short excerpt; their full
# bodies stay readable through the read tools. Dropping whole old groups made
# the model forget pages it had already read and fetch them again (#15).
EXCERPT_CHARS = 600
RECENT_GROUPS = 4


def _excerpt(message):
    try:
        data = json.loads(message["content"])
    except (TypeError, ValueError):
        return {**message, "content": str(message.get("content", ""))[:EXCERPT_CHARS] + " …[compacted]"}
    if not isinstance(data, dict):
        return {**message, "content": encode(data)[:EXCERPT_CHARS] + " …[compacted]"}
    short = {}
    for key, value in data.items():
        if isinstance(value, str) and len(value) > EXCERPT_CHARS:
            short[key] = value[:EXCERPT_CHARS] + " …[compacted; read again only if exact text is needed]"
        elif isinstance(value, (list, dict)) and len(encode(value)) > EXCERPT_CHARS:
            short[key] = encode(value)[:EXCERPT_CHARS] + " …[compacted]"
        else:
            short[key] = value
    return {**message, "content": encode(short)}


def compact_request(messages, pinned, *, threshold=80000):
    """Keep original requirements, current responsibility and every tool group.

    Recent groups stay whole; older tool results are shortened to their
    identity (source, URL, title, range) and an excerpt instead of being
    dropped. Negative evidence, investigations and review requirements are
    pinned from the ledger.
    """
    if len(encode(messages)) <= threshold:
        return messages, False
    initial = messages[:2]
    orchestration = [m for m in messages if m.get("role") == "user" and str(m.get("content", "")).startswith("Current orchestration context")]
    audience = [m for m in messages if m.get("role") == "user" and str(m.get("content", "")).startswith("Current room context")]
    goal = [m for m in messages if m.get("role") == "user" and str(m.get("content", "")).startswith("Current goal context")]
    groups, current = [], []
    for message in messages[2:]:
        if message["role"] == "assistant":
            if current:
                groups.append(current)
            current = [message]
        elif message["role"] == "tool" and current:
            current.append(message)
    if current:
        groups.append(current)
    recent = groups[-RECENT_GROUPS:]
    older = [[group[0]] + [_excerpt(m) for m in group[1:]] for group in groups[:-RECENT_GROUPS]]
    # If even the shortened history is too large, the oldest groups go first.
    while older and len(encode(older + recent)) > threshold - 20000:
        older.pop(0)
    summary = {"role": "user", "content": "Durable context after compaction (receipt evidence, not new permissions). Older tool results below are shortened to an excerpt; the progress list names every source range and page (URL, title) already read. Work from what you have; re-read only when you need exact text you no longer see, and record findings with record_investigation so they survive: " + encode(pinned)}
    return initial + audience[-1:] + orchestration[-1:] + goal[-1:] + [summary] + [m for group in older + recent for m in group], True
