"""Desensitized run bundle for the private ops repository (noyi-ops).

Nothing leaves by default. Every column of every table is registered here as
private or as one of a few non-text kinds; tests fail when a new table or
column appears unregistered, when a table holding words or health data is not
wholly private, and when a canary placed in any column shows up in a bundle.
The bundle is then validated against a closed schema before it is written.

Kinds:
  id        hashed with a per-install salt (joins rows, reveals nothing)
  time      ISO minute, local time
  count     non-negative integer
  seconds   duration or other plain number
  enum      a value from ENUMS[table.column]; anything else becomes "other"
  json:<n>  a JSON column read through the field extractor named n
  shared_note  the one sentence the user explicitly confirmed may leave home
  private   never read
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .health import HEALTH_TABLES, TIMEZONE

EXPORTABLE_KINDS = {"id", "time", "count", "seconds", "enum"}
P = "private"

# Wholly private: the user's words, memory, health data, photos and uploads.
PRIVATE_TABLES = set(HEALTH_TABLES) - {"health_bias"} | {
    "messages", "memories", "memory_candidates", "memory_proposals", "memory_rejections", "memory_changes", "memory_loads",
    "memory_search", "memory_search_config", "memory_search_content", "memory_search_data", "memory_search_docsize", "memory_search_idx",
    "uploads", "attentions", "conversation_inputs", "commitments", "commitment_events", "commitment_clock",
    "ingress_receipts", "answer_receipts", "goals", "goal_history", "goal_reviews", "goal_review_requests", "review_snapshots",
    "review_evidence_reads", "user_feedback", "local_metadata", "admission_rules", "admission_decisions", "routine_policies",
    "policy_occurrences", "policy_proposals", "observations", "source_health", "experiments", "experiment_versions",
    "experiment_reminders", "investigation", "progress_log", "nudges", "handoffs", "worker_reviews", "worker_adoptions",
    "identity_audit", "bots", "team_templates", "rooms", "room_members", "actors", "thread_subscriptions", "followups",
    "followup_history", "pending_routes", "events", "input_usage", "artifact_heads", "artifact_versions", "coverage_reads",
    "dependency_reads", "speech_marks", "activity_log", "controls", "push_subscriptions", "push_sent", "app_presence", "paired_devices", "pairing_codes",
    "conversation_goal_actions",
}

REGISTRY = {
    "runs": {"id": "id", "actor_id": P, "task_id": "id", "trigger": "enum", "epoch": "count", "revision": "count", "phase": "enum",
             "end_reason": "enum", "started": "time", "ended": "time", "context_manifest": P, "stop_requested_at": "time",
             "stop_reason": "enum", "scope_room": P, "scope_revision": "count", "bot_revision": "count"},
    "model_requests": {"id": "id", "run_id": "id", "sequence": "count", "payload_sha256": P, "metadata": P, "state": "enum",
                       "created": "time", "ended": "time"},
    "steps": {"id": P, "task_id": "id", "run_id": "id", "kind": "enum", "data": "json:step", "created": "time"},
    "operations": {"id": "id", "task_id": "id", "run_id": "id", "tool": "enum", "arguments": P, "status": "enum",
                   "result": "json:operation", "created": "time", "ended": "time"},
    "deliveries": {"id": "id", "task_id": "id", "run_id": P, "room_id": P, "content": P, "artifact": P, "state": "enum",
                   "attempts": "count", "receipt": P, "last_error": "enum", "kind": "enum", "attention_id": P, "created": "time"},
    "tasks": {"id": "id", "root_task_id": "id", "parent_task_id": "id", "actor_id": P, "room_id": P, "spec": P,
              "requirement_revision": "count", "execution_epoch": "count", "status": "enum", "reason": "enum", "acceptance": "enum",
              "calls_used": "count", "tools_used": "count", "active_seconds": "seconds", "created": "time", "auto_continue": "enum",
              "cancel_state": "enum", "continuations": "count", "wait_spec": P, "prior_task_id": P, "parent_revision": "count",
              "required": "count", "worker_key": P, "stagnant_tools": "count", "failure_streak": "count", "nudge_count": "count",
              "dependencies": P, "goal_id": P, "goal_revision": "count", "policy_id": P, "policy_revision": "count",
              "occurrence_id": P, "admission_rule_id": P, "admission_revision": "count"},
    "memory_curations": {"id": P, "collection_id": P, "trigger": "enum", "state": "enum", "run_id": "id", "since": "time",
                         "until": "time", "input_chars": "count", "proposals": "count", "rejected_items": "count", "notified": "count",
                         "error": "enum", "created": "time", "ended": "time"},
    # Estimate vs. later label: only the relative error and whether it fell in range.
    "health_bias": {"id": "id", "estimate_id": P, "label_id": P, "kcal_low": P, "kcal_high": P, "label_kcal": P,
                    "relative_error": "seconds", "in_range": "count", "created": "time"},
    "feedback_marks": {"id": "id", "delivery_id": "id", "task_id": "id", "delivery_kind": "enum", "category": "enum",
                       "note_shared": "shared_note", "note_private": P, "share": "count", "created": "time"},
}

ENUMS = {
    "runs.trigger": {"user_task", "conversation", "answer", "memory_curation", "routine", "worker", "resume", "followup", "chat", "review"},
    "runs.phase": {"running", "ended"},
    "runs.end_reason": {"accepted", "conversation_reply", "scope_changed", "waiting_user", "requirements_updated", "memory_curation",
                        "interrupted", "time_budget", "timeout", "model_or_runtime_error", "model_budget", "tool_budget",
                        "stagnation_budget", "output_limit", "no_accepted_artifact", "engine_ended_without_acceptance",
                        "unable_reported", "cancelled", "waiting_children", "waiting_dependencies", "action_completed", "chat_tool_denied"},
    "runs.stop_reason": {"scope_changed", "user_stop", "cancel", "requirements_updated", "memory_changed"},
    "model_requests.state": {"dispatching", "completed", "failed", "unknown"},
    "steps.kind": {"model", "acceptance", "recovery", "conversation_rejected", "nudge", "tool_error"},
    "operations.status": {"running", "succeeded", "failed", "interrupted"},
    "deliveries.state": {"pending", "accepted", "failed", "cancelled"},
    "deliveries.kind": {"result", "question", "commitment", "system", "health", "health_alert", "progress", "conversation"},
    "deliveries.last_error": {"ValueError", "OSError", "TimeoutError", "HTTPError", "ConnectionError", "RuntimeError", "PushGone"},
    "tasks.status": {"ready", "running", "succeeded", "failed", "paused", "review", "waiting_user", "waiting_children",
                     "waiting_dependencies", "cancelled"},
    "tasks.reason": None,      # same values as runs.end_reason plus a few states; checked against that set
    "tasks.acceptance": {"pending", "passed", "failed"},
    "tasks.auto_continue": {"on", "off", "paused"},
    "tasks.cancel_state": {"active", "cancelled", "forbidden"},
    "memory_curations.trigger": {"idle", "manual", "daily"},
    "memory_curations.state": {"running", "done", "failed", "skipped"},
    "memory_curations.error": {"ValueError", "TimeoutError", "RuntimeError", "model_error"},
    "feedback_marks.delivery_kind": {"result", "question", "commitment", "system", "health", "health_alert", "conversation", "progress"},
    "feedback_marks.category": {"wrong_fact", "wrong_math", "misunderstood", "verbose", "missed", "other"},
}
ENUMS["tasks.reason"] = ENUMS["runs.end_reason"] | {"recovered", "user_resume", "user_goal_resume", "test_complete", "member_unavailable", "accepted"}
FEEDBACK_CATEGORIES = ENUMS["feedback_marks.category"]
NOTE_MAX = 300


class BundleError(ValueError):
    pass


def _tool_names():
    from .builtin_plugins import builtin_plugins
    from .health import TOOLS as HEALTH_TOOLS
    from .web_tools import FETCH, SEARCH
    from .conversation import CONVERSATION_SCHEMA
    names = {s.name for p in builtin_plugins() for s in p.tools}
    names |= {t["function"]["name"] for t in HEALTH_TOOLS + [FETCH, SEARCH] + CONVERSATION_SCHEMA}
    return names


ENUMS["operations.tool"] = _tool_names()
ERROR_TYPES = {"ValueError", "TypeError", "OSError", "UnicodeError", "SourceUnavailable", "HealthError", "HTTPError", "TimeoutError",
               "ConnectError", "ReadTimeout", "inactive_run", "tool_budget", "StaleMemory", "RuntimeError", "KeyError"}
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,60}")


# ---- field extractors for JSON columns -------------------------------------------

def _num(value):
    return value if type(value) in (int, float) and value == value else None


def _step(data):
    """A model step: token counts, duration and the provider model id only."""
    model = data.get("model")
    return {"model": model if isinstance(model, str) and MODEL_NAME.fullmatch(model) and not re.search(r"[一-鿿]", model) else "other",
            "input_tokens": _num(data.get("input_tokens")), "output_tokens": _num(data.get("output_tokens")),
            "seconds": _num(data.get("seconds")), "finish_reason": data.get("finish_reason") if data.get("finish_reason") in {"stop", "tool_calls", "length", "content_filter", None} else "other"}


def _operation(data):
    error = data.get("error") if isinstance(data, dict) else None
    return {"error": None if error is None else error if error in ERROR_TYPES else "other"}


EXTRACTORS = {"step": _step, "operation": _operation}


# ---- reading through the registry -------------------------------------------------

def _iso(at):
    return datetime.fromtimestamp(at, TIMEZONE).strftime("%Y-%m-%dT%H:%M") if type(at) in (int, float) else None


def _rows(store, table, where="", params=(), *, salt, span=None):
    """Rows read only through REGISTRY. span=(start, end) adds a derived duration
    in seconds from two registered time columns of the same row."""
    cols = REGISTRY[table]
    present = {c["name"] for c in store.all(f"PRAGMA table_info({table})")}
    if not present:
        return []   # an older ledger without this table yet
    readable = [c for c, kind in cols.items() if kind != P and c in present]
    out = []
    for row in store.all(f"SELECT {','.join(readable)} FROM {table} {where}", params):
        item = {}
        if span:
            start, end = row[span[0]], row[span[1]]
            item["_seconds"] = round(end - start, 3) if type(start) in (int, float) and type(end) in (int, float) else None
        for column in readable:
            kind, value = cols[column], row.get(column) if isinstance(row, dict) else row[column]
            if value is None:
                item[column] = None
            elif kind == "id":
                item[column] = hashlib.sha256((salt + ":" + str(value)).encode()).hexdigest()[:16]
            elif kind == "time":
                item[column] = _iso(value)
            elif kind == "count":
                item[column] = int(value) if type(value) is int and value >= 0 else None
            elif kind == "seconds":
                item[column] = round(float(value), 4) if type(value) in (int, float) else None
            elif kind == "enum":
                allowed = ENUMS[f"{table}.{column}"]
                item[column] = value if value in allowed else "other"
            elif kind.startswith("json:"):
                try:
                    data = json.loads(value)
                except (TypeError, ValueError):
                    data = {}
                item[column] = EXTRACTORS[kind[5:]](data if isinstance(data, dict) else {})
            elif kind == "shared_note":
                item[column] = value
            else:  # unknown kind: refuse rather than leak
                raise BundleError(f"unknown export kind {kind}")
        out.append(item)
    return out


def build_bundle(store, *, since, until, salt, service, version):
    """Everything that leaves home, read only through REGISTRY."""
    window = (since, until)
    q = "WHERE {col}>=? AND {col}<?"
    runs = _rows(store, "runs", q.format(col="started"), window, salt=salt, span=("started", "ended"))
    steps = [s for s in _rows(store, "steps", q.format(col="created") + " AND kind='model'", window, salt=salt)]
    requests = _rows(store, "model_requests", q.format(col="created"), window, salt=salt)
    ops = _rows(store, "operations", q.format(col="created"), window, salt=salt, span=("created", "ended"))
    deliveries = _rows(store, "deliveries", q.format(col="created"), window, salt=salt)
    tasks = _rows(store, "tasks", q.format(col="created"), window, salt=salt)
    curations = _rows(store, "memory_curations", q.format(col="created"), window, salt=salt)
    bias = _rows(store, "health_bias", q.format(col="created"), window, salt=salt)
    marks = _rows(store, "feedback_marks", q.format(col="created"), window, salt=salt)
    bundle = {
        "schema": 1, "generated_at": _iso(until), "window": {"from": _iso(since), "to": _iso(until)},
        "version": {k: v for k, v in version.items() if k in {"tag", "commit", "python", "node"}},
        "service": service,
        "runs": [{"run": r["id"], "task": r["task_id"], "trigger": r["trigger"], "phase": r["phase"], "end_reason": r["end_reason"],
                  "stop_reason": r["stop_reason"], "at": r["started"], "seconds": r["_seconds"]} for r in runs],
        "model_calls": [{"run": s["run_id"], "task": s["task_id"], "at": s["created"], **s["data"]} for s in steps],
        "model_requests": [{"run": r["run_id"], "state": r["state"], "at": r["created"]} for r in requests],
        "tools": [{"run": o["run_id"], "tool": o["tool"], "status": o["status"], "error": o["result"]["error"] if o["result"] else None, "at": o["created"], "seconds": o["_seconds"]} for o in ops],
        "deliveries": [{"task": d["task_id"], "kind": d["kind"], "state": d["state"], "attempts": d["attempts"], "error": d["last_error"], "at": d["created"]} for d in deliveries],
        "tasks": [{"task": t["id"], "root": t["root_task_id"], "status": t["status"], "reason": t["reason"], "acceptance": t["acceptance"],
                   "calls": t["calls_used"], "tools": t["tools_used"], "active_seconds": t["active_seconds"], "at": t["created"]} for t in tasks],
        "curations": [{"state": c["state"], "trigger": c["trigger"], "input_chars": c["input_chars"], "proposals": c["proposals"], "error": c["error"], "at": c["created"]} for c in curations],
        "estimate_bias": [{"relative_error": b["relative_error"], "in_range": b["in_range"], "at": b["created"]} for b in bias],
        "feedback": [{"mark": m["id"], "task": m["task_id"], "delivery_kind": m["delivery_kind"], "category": m["category"], "at": m["created"],
                      **({"note": m["note_shared"]} if m["share"] == 1 and m["note_shared"] else {})} for m in marks],
    }
    validate_bundle(bundle)
    return bundle


# ---- closed schema ---------------------------------------------------------------

HASH = re.compile(r"[0-9a-f]{16}")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
SERVICE_KEYS = {"tag": str, "previous_tag": str, "last_upgrade": str, "upgrade_result": str, "crashes_24h": int, "restarts_24h": int,
                "disk_free_gb": float, "uptime_hours": float, "errors": list, "pending_push": int, "web_ok": bool}
UPGRADE_RESULTS = {"ok", "rehearsal_failed", "health_failed_rolled_back", "rolled_back", "none", "install"}
TAG = re.compile(r"(home-\d{8}-\d{1,3}|none|dev)")


def _check(cond, where):
    if not cond:
        raise BundleError("bundle refused at " + where)


def _is(value, kind):
    if kind == "hash":
        return value is None or (isinstance(value, str) and HASH.fullmatch(value))
    if kind == "iso":
        return value is None or (isinstance(value, str) and ISO.fullmatch(value))
    if kind == "num":
        return value is None or type(value) in (int, float)
    if kind == "int":
        return value is None or (type(value) is int and value >= 0)
    if isinstance(kind, (set, frozenset)):
        return value is None or value in kind or value == "other"
    if kind == "model":
        return value is None or (isinstance(value, str) and MODEL_NAME.fullmatch(value))
    if kind == "note":
        return isinstance(value, str) and 1 <= len(value) <= NOTE_MAX
    return False


SECTIONS = {
    "runs": {"run": "hash", "task": "hash", "trigger": ENUMS["runs.trigger"], "phase": ENUMS["runs.phase"], "end_reason": ENUMS["runs.end_reason"],
             "stop_reason": ENUMS["runs.stop_reason"], "at": "iso", "seconds": "num"},
    "model_calls": {"run": "hash", "task": "hash", "at": "iso", "model": "model", "input_tokens": "int", "output_tokens": "int", "seconds": "num",
                    "finish_reason": {"stop", "tool_calls", "length", "content_filter"}},
    "model_requests": {"run": "hash", "state": ENUMS["model_requests.state"], "at": "iso"},
    "tools": {"run": "hash", "tool": ENUMS["operations.tool"], "status": ENUMS["operations.status"], "error": ERROR_TYPES, "at": "iso", "seconds": "num"},
    "deliveries": {"task": "hash", "kind": ENUMS["deliveries.kind"], "state": ENUMS["deliveries.state"], "attempts": "int", "error": ENUMS["deliveries.last_error"], "at": "iso"},
    "tasks": {"task": "hash", "root": "hash", "status": ENUMS["tasks.status"], "reason": ENUMS["tasks.reason"], "acceptance": ENUMS["tasks.acceptance"],
              "calls": "int", "tools": "int", "active_seconds": "num", "at": "iso"},
    "curations": {"state": ENUMS["memory_curations.state"], "trigger": ENUMS["memory_curations.trigger"], "input_chars": "int", "proposals": "int",
                  "error": ENUMS["memory_curations.error"], "at": "iso"},
    "estimate_bias": {"relative_error": "num", "in_range": "int", "at": "iso"},
    "feedback": {"mark": "hash", "task": "hash", "delivery_kind": ENUMS["feedback_marks.delivery_kind"], "category": FEEDBACK_CATEGORIES, "at": "iso", "note": "note"},
}


def validate_bundle(bundle):
    """Refuse any bundle with a field or value outside the closed schema."""
    top = {"schema", "generated_at", "window", "version", "service", *SECTIONS}
    _check(isinstance(bundle, dict) and set(bundle) == top, "top-level keys")
    _check(bundle["schema"] == 1 and _is(bundle["generated_at"], "iso"), "schema")
    _check(set(bundle["window"]) == {"from", "to"} and all(_is(v, "iso") for v in bundle["window"].values()), "window")
    version = bundle["version"]
    _check(isinstance(version, dict) and set(version) <= {"tag", "commit", "python", "node"}, "version keys")
    _check(all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", v) for v in version.values()), "version values")
    service = bundle["service"]
    _check(isinstance(service, dict) and set(service) <= set(SERVICE_KEYS), "service keys")
    for key, value in service.items():
        if key == "errors":
            _check(isinstance(value, list) and len(value) <= 50, "service.errors")
            for e in value:
                _check(isinstance(e, dict) and set(e) == {"type", "where", "count"}, "service.errors item")
                _check(isinstance(e["type"], str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", e["type"]), "service.errors type")
                _check(e["where"] is None or (isinstance(e["where"], str) and re.fullmatch(r"[A-Za-z0-9_./-]{1,80}:\d{1,6}", e["where"])), "service.errors where")
                _check(type(e["count"]) is int and e["count"] >= 0, "service.errors count")
        elif key in {"tag", "previous_tag"}:
            _check(isinstance(value, str) and TAG.fullmatch(value), "service." + key)
        elif key == "upgrade_result":
            _check(value in UPGRADE_RESULTS, "service.upgrade_result")
        elif key == "last_upgrade":
            _check(_is(value, "iso"), "service.last_upgrade")
        elif SERVICE_KEYS[key] is bool:
            _check(type(value) is bool, "service." + key)
        elif SERVICE_KEYS[key] is int:
            _check(type(value) is int and value >= 0, "service." + key)
        else:
            _check(type(value) in (int, float), "service." + key)
    for section, shape in SECTIONS.items():
        rows = bundle[section]
        _check(isinstance(rows, list) and len(rows) <= 20000, section)
        for row in rows:
            optional = {"note"} if section == "feedback" else set()
            _check(isinstance(row, dict) and set(shape) - optional <= set(row) <= set(shape), section + " keys")
            for key, value in row.items():
                _check(_is(value, shape[key]), f"{section}.{key}")
    return bundle


# ---- server log: type and place only ---------------------------------------------

FRAME = re.compile(r'File "([^"]+)", line (\d+)')
ERROR_LINE = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?([A-Z][A-Za-z0-9_]*(?:Error|Exception|Unavailable|Gone|Exit))\b")


def log_errors(path, *, limit=50):
    """Count exceptions in a log by type and innermost project file:line; drop messages."""
    path = Path(path)
    if not path.exists():
        return []
    counts, last_frame = Counter(), None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        frame = FRAME.search(line)
        if frame:
            name = re.split(r"[\\/]+", frame.group(1))[-1]
            last_frame = f"{name}:{frame.group(2)}" if re.fullmatch(r"[A-Za-z0-9_.-]{1,60}\.(py|mjs|js)", name) else None
            continue
        match = ERROR_LINE.match(line.strip())
        if match:
            counts[(match.group(1), last_frame)] += 1
            last_frame = None
    return [{"type": t, "where": w, "count": n} for (t, w), n in counts.most_common(limit)]


# ---- writing a bundle from the live ledger -----------------------------------------

class ReadOnlyLedger:
    """The two read helpers build_bundle needs, on a read-only connection.

    Opening Store would run migrations and contend with the service; the
    exporter must never write to the database it reads."""

    def __init__(self, path):
        import sqlite3
        self.db = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=10)
        self.db.row_factory = sqlite3.Row

    def all(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params)]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def close(self):
        self.db.close()


def export_bundle(data, out_dir, *, since, until, service=None, version=None):
    """Write bundles/<day>/<HHMM>.json under out_dir; returns its path. The salt
    lives in the data directory, so hashed ids join across bundles but cannot be
    reversed by anyone without this machine."""
    import os
    import secrets
    data, out_dir = Path(data), Path(out_dir)
    salt_file = data / "ops-salt"
    if not salt_file.exists():
        salt_file.write_text(secrets.token_hex(16), encoding="ascii")
    salt = salt_file.read_text(encoding="ascii").strip()
    service = dict(service or {})
    service.setdefault("errors", log_errors(data / "logs" / "server.log"))
    ledger = ReadOnlyLedger(data / "state.sqlite")
    try:
        bundle = build_bundle(ledger, since=since, until=until, salt=salt, service=service, version=version or {})
    finally:
        ledger.close()
    stamp = datetime.fromtimestamp(until, TIMEZONE)
    target = out_dir / "bundles" / stamp.strftime("%Y-%m-%d") / (stamp.strftime("%H%M") + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target
