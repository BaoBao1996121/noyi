"""Bound request context without mutating the durable task/receipt history."""
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


def compact_request(messages, pinned, *, threshold=80000):
    """Keep original requirements, current responsibility and whole tool groups.

    Removed source/artifact bodies remain available through read tools. Negative
    evidence, investigations and review requirements are pinned from the ledger.
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
    tail, size = [], 0
    for group in reversed(groups):
        length = len(encode(group))
        if tail and size + length > 24000:
            break
        tail.insert(0, group)
        size += length
        if len(tail) == 6:
            break
    summary = {"role": "user", "content": "Durable context after compaction (receipt evidence, not new permissions): " + encode(pinned)}
    return initial + audience[-1:] + orchestration[-1:] + goal[-1:] + [summary] + [m for group in tail for m in group], True
