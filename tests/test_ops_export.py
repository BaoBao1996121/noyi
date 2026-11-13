"""Run logs leave home through a column registry; health data and words never do."""
import json
import sqlite3

import pytest

from conftest import ScriptedModel
from personal_agent.contracts import TaskSpec
from personal_agent.health import parse_local
from personal_agent.ops_export import (EXPORTABLE_KINDS, PRIVATE_TABLES, REGISTRY, BundleError, build_bundle,
                                       log_errors, validate_bundle)
from personal_agent.webui import Console

# Tables that hold the user's words, health data, photos or memory: every column private.
MUST_BE_PRIVATE = {"health_profile", "health_metrics", "health_meals", "health_exercise", "health_nightly", "health_alerts",
                   "messages", "memories", "memory_candidates", "memory_proposals", "uploads", "attentions",
                   "conversation_inputs", "commitments", "commitment_events", "ingress_receipts", "answer_receipts",
                   "goals", "goal_history", "user_feedback", "memory_search_content", "local_metadata"}


@pytest.fixture
def console(store, tmp_path):
    return Console(store, tmp_path / "data", ScriptedModel([]), run_service=False)


def schema(store):
    tables = [r["name"] for r in store.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: {c["name"]: (c["type"] or "").upper() for c in store.all(f"PRAGMA table_info({t})")} for t in tables}


def test_every_table_and_column_is_registered(store, console):
    missing = []
    for table, columns in schema(store).items():
        if table in PRIVATE_TABLES:
            continue
        if table not in REGISTRY:
            missing.append(table)
            continue
        missing += [f"{table}.{c}" for c in columns if c not in REGISTRY[table]]
    assert not missing, "新表或新列还没决定能不能导出，请登记到 ops_export.REGISTRY 或 PRIVATE_TABLES：" + ", ".join(missing)
    stale = [f"{t}.{c}" for t, cols in REGISTRY.items() for c in cols if c not in schema(store).get(t, {})]
    assert not stale, "登记表里有已不存在的列：" + ", ".join(stale)


def test_tables_with_words_or_health_are_wholly_private(store, console):
    assert MUST_BE_PRIVATE <= PRIVATE_TABLES
    assert not set(REGISTRY) & PRIVATE_TABLES
    assert all(not t.startswith("health_") or t == "health_bias" for t in REGISTRY)
    assert set(REGISTRY["health_bias"]) - {c for c, k in REGISTRY["health_bias"].items() if k == "private"} == {"id", "relative_error", "in_range", "created"}


def test_no_free_text_kind_is_exportable():
    kinds = {k for cols in REGISTRY.values() for k in cols.values()}
    assert kinds <= EXPORTABLE_KINDS | {"private", "shared_note"} | {k for k in kinds if k.startswith("json:")}
    shared = [(t, c) for t, cols in REGISTRY.items() for c, k in cols.items() if k == "shared_note"]
    assert shared == [("feedback_marks", "note_shared")]


def _fill_everything(store, canary_text="CANARY私密原话", canary_number=73.41):
    """Put a canary in every text column of every table; private numbers get a canary number."""
    store.db.execute("PRAGMA foreign_keys=OFF")
    for table, columns in schema(store).items():
        if table.startswith("memory_search") and table != "memory_search":
            continue
        values = []
        for name, kind in columns.items():
            registered = REGISTRY.get(table, {}).get(name, "private")
            if registered == "shared_note":
                values.append("用户确认可以外传的一句说明")   # by design this one column may leave
            elif "INT" in kind:
                values.append(7)
            elif kind in {"REAL", "NUMERIC"}:
                values.append(parse_local("2026-11-13 10:00") if registered == "time" else canary_number if registered == "private" else 0.25)
            elif kind == "BLOB":
                values.append(b"x")
            else:
                values.append(f"{canary_text}:{table}.{name}")
        try:
            store.db.execute(f"INSERT INTO {table} VALUES({','.join('?' * len(values))})", values)
        except sqlite3.Error:
            pass  # a CHECK constraint (e.g. commitment_clock id=1) is fine to skip
    store.db.execute("PRAGMA foreign_keys=ON")


def test_canaries_in_every_column_never_reach_the_bundle(store, console):
    _fill_everything(store)
    at = parse_local("2026-11-13 10:00")
    # JSON columns are read field by field: extra keys and text in a code field never pass.
    store.db.execute("INSERT INTO steps(task_id,run_id,kind,data,created) VALUES('t','r','model',?,?)",
                     (json.dumps({"model": "CANARY私密原话", "input_tokens": 5, "note": "CANARY私密原话", "seconds": 2.5}), at))
    store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,result,created,ended) VALUES('o','t','r','record_body',?, 'failed', ?, ?, ?)",
                     (json.dumps({"weight_kg": 73.41, "quote": "CANARY私密原话"}), json.dumps({"error": "CANARY私密原话", "detail": "CANARY私密原话"}), at, at + 1))
    bundle = build_bundle(store, since=0, until=parse_local("2026-11-14 00:00"), salt="s", service={}, version={})
    text = json.dumps(bundle, ensure_ascii=False)
    assert "CANARY" not in text and "私密原话" not in text
    assert "73.41" not in text
    validate_bundle(bundle)


def test_bias_leaves_only_relative_numbers(store):
    at = parse_local("2026-11-13 12:00")
    est = store.record_meal("local-user", method="estimate", food_name="鸡腿饭", eaten_at=at,
                            items=[{"name": "鸡腿饭", "amount": "一份", "kcal_low": 600, "kcal_high": 900}], basis="按盒饭估", ask="")
    store.record_meal("local-user", method="label", food_name="鸡腿饭", eaten_at=at, replaces=est["id"],
                      label={"energy_value": 800, "energy_unit": "kJ", "basis_amount": 100, "basis_unit": "g", "protein_g": 9, "fat_g": 8, "carb_g": 24,
                             "portion_amount": 400, "portion_unit": "g"})
    store.record_metric("local-user", kind="weight_kg", value=81.37, measured_at=at)
    bundle = build_bundle(store, since=0, until=10**10, salt="s", service={}, version={})
    [bias] = bundle["estimate_bias"]
    assert set(bias) == {"relative_error", "in_range", "at"}
    text = json.dumps(bundle, ensure_ascii=False)
    assert "鸡腿饭" not in text and "81.37" not in text and "600" not in text.replace("600.0", "")


def test_feedback_note_leaves_only_when_the_user_confirmed_sharing(store):
    tid = store.submit(TaskSpec("x"), nonce="f")["task_id"]
    did = store._notice(tid, "local", "回复内容不应外传", kind="result")
    store.mark_feedback(did, category="wrong_fact", note_shared="热量算多了", note_private="其实我吃了两碗", share=True)
    store.mark_feedback(did, category="verbose", note_shared="不想外传这句", note_private="", share=False)
    bundle = build_bundle(store, since=0, until=10**10, salt="s", service={}, version={})
    notes = [f.get("note") for f in bundle["feedback"]]
    assert sorted(f["category"] for f in bundle["feedback"]) == ["verbose", "wrong_fact"]
    assert notes.count("热量算多了") == 1 and "不想外传这句" not in notes
    text = json.dumps(bundle, ensure_ascii=False)
    assert "其实我吃了两碗" not in text and "回复内容不应外传" not in text


def test_mark_feedback_validates_category_and_length(store):
    did = store._notice(None, "local", "x")
    with pytest.raises(ValueError):
        store.mark_feedback(did, category="whatever", note_shared="", note_private="", share=False)
    with pytest.raises(ValueError):
        store.mark_feedback(did, category="other", note_shared="长" * 301, note_private="", share=True)


@pytest.mark.parametrize("bad", [
    {"runs": [{"task": "abc", "trigger": "user_task", "end_reason": "accepted", "phase": "ended", "seconds": 1.0, "at": "2026-11-13T10:00", "text": "多出来的字段"}]},
    {"service": {"crashes_24h": 1, "last_error": {"type": "ValueError", "where": "health.py:12", "message": "体重 81 公斤"}}},
    {"tools": [{"run": "abcdabcdabcdabcd", "tool": "我吃了", "status": "ok", "seconds": 1, "error": None}]},
])
def test_bundle_with_any_unexpected_field_or_value_is_refused(bad):
    bundle = {"schema": 1, "generated_at": "2026-11-13T10:00", "window": {"from": "2026-11-13T09:00", "to": "2026-11-13T10:00"},
              "version": {}, "service": {}, "runs": [], "model_calls": [], "model_requests": [], "tools": [], "deliveries": [], "tasks": [],
              "curations": [], "estimate_bias": [], "feedback": []}
    bundle.update(bad)
    with pytest.raises(BundleError):
        validate_bundle(bundle)


def test_log_errors_keep_type_and_place_but_not_the_message(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("""Traceback (most recent call last):
  File "E:\\\\Users\\\\me\\\\noyi\\\\src\\\\personal_agent\\\\health.py", line 120, in record_meal
    raise HealthError("体重 81.37 公斤的记录有问题")
personal_agent.health.HealthError: 体重 81.37 公斤的记录有问题
ValueError: user said 我今天吃了火锅
""", encoding="utf-8")
    errors = log_errors(log)
    assert errors == [{"type": "HealthError", "where": "health.py:120", "count": 1}, {"type": "ValueError", "where": None, "count": 1}]
    assert "81.37" not in json.dumps(errors) and "火锅" not in json.dumps(errors)


def test_export_reads_the_live_ledger_without_writing_it(store, tmp_path):
    from personal_agent.ops_export import export_bundle
    store.record_metric("local-user", kind="weight_kg", value=66.66, measured_at=0)
    data = tmp_path / "data"
    before = (data / "state.sqlite").stat().st_mtime_ns
    (data / "logs").mkdir()
    (data / "logs" / "server.log").write_text("ValueError: 我的体重 66.66\n", encoding="utf-8")
    path = export_bundle(data, tmp_path / "ops", since=0, until=10**10, service={"crashes_24h": 0}, version={"tag": "home-20261113-1"})
    text = path.read_text(encoding="utf-8")
    assert "66.66" not in text and json.loads(text)["service"]["errors"] == [{"type": "ValueError", "where": None, "count": 1}]
    assert (data / "state.sqlite").stat().st_mtime_ns == before
