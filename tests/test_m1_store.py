"""M1 protocol tests: real SQLite, controlled executor receipts, no model claims.

Service/real-Pi artifact scenarios live separately in test_m1_scenarios.py.
"""
import json
import time

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.store import Store


def observe(store, run):
    context = store.orchestration_context(run)
    request = store.begin_request(run, [{"role": "user", "content": json.dumps(context)}])
    store.end_request(request)
    return context


def start_family(store, *, count=2, mode="all", timeout=300, spec=None):
    root = store.submit(spec or TaskSpec("Root work"), nonce="root")["task_id"]
    parent = store.claim()
    assert parent["task_id"] == root
    observe(store, parent)
    children = [store.spawn_worker(parent, worker_key=f"worker-{i}", objective=f"Worker {i}",
                                   source_ids=[])["task_id"] for i in range(count)]
    store.wait_workers(parent, task_ids=children, mode=mode, timeout_seconds=timeout)
    runs = [store.claim() for _ in children]
    assert {r["task_id"] for r in runs} == set(children)
    return root, parent, {r["task_id"]: r for r in runs}


def worker_receipt(store, run, content="Worker result", status="succeeded"):
    observe(store, run)
    store.finish(run, status=status, reason="executor_receipt", content=content,
                 acceptance="passed" if status == "succeeded" else "pending")


def next_parent(store, root):
    store.tick_waits()
    run = store.claim()
    assert run is not None and run["task_id"] == root
    return run


def assert_result_available(store, parent, child, content, context=None):
    context = context if context is not None else observe(store, parent)
    assert any(i["kind"] == "worker_result" and child["task_id"] in i["content"] for i in context["inputs"])
    result = store.worker_result(parent, child["task_id"])
    assert result["delivery"]["content"] == content
    assert result["task"]["execution_epoch"] == child["epoch"]


def test_b01_wait_ends_parent_run_without_finishing_or_cancelling_workers(store):
    root, parent, children = start_family(store)
    assert not store.current(parent)
    assert store.task(root)["status"] != "succeeded"
    assert all(store.current(run) for run in children.values())
    assert not store.all("SELECT * FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (root,))


def test_b02_all_worker_results_batch_into_one_parent_run(store):
    root, _, children = start_family(store)
    first, second = list(children.values())
    worker_receipt(store, first, "FIRST_RESULT")
    store.tick_waits()
    assert store.claim() is None
    worker_receipt(store, second, "SECOND_RESULT")
    parent = next_parent(store, root)
    context = observe(store, parent)
    assert_result_available(store, parent, first, "FIRST_RESULT", context)
    assert_result_available(store, parent, second, "SECOND_RESULT", context)
    assert store.claim() is None
    assert len(store.all("SELECT * FROM runs WHERE actor_id=? AND phase='running'", (parent["actor_id"],))) == 1


def test_b03_result_arriving_during_continuation_survives_for_next_run(store):
    root, _, children = start_family(store, mode="any")
    first, second = list(children.values())
    worker_receipt(store, first, "FIRST_RESULT")
    parent = next_parent(store, root)
    original = observe(store, parent)
    assert not any(i["kind"] == "worker_result" and second["task_id"] in i["content"] for i in original["inputs"])
    worker_receipt(store, second, "SECOND_RESULT")
    assert store.claim() is None  # The existing parent owns its actor slot.
    store.wait_workers(parent, task_ids=list(children), mode="all", timeout_seconds=30)
    following = next_parent(store, root)
    assert following["id"] != parent["id"]
    assert_result_available(store, following, second, "SECOND_RESULT")


def test_b04_stop_reply_preserves_worker_result_until_targeted_user_message(store):
    root, _, children = start_family(store, count=1)
    store.request_control(root, "stop_reply")
    child = next(iter(children.values()))
    assert store.current(child)
    worker_receipt(store, child, "RESULT_WHILE_PAUSED")
    store.tick_waits()
    assert store.task(root)["auto_continue"] == "paused"
    assert store.claim() is None
    reply = store.followup("Please include the conclusion.", nonce="new-user-message", task_id=root)
    assert reply["task_id"] == root
    parent = next_parent(store, root)
    context = observe(store, parent)
    assert store.task(root)["auto_continue"] == "on"
    assert_result_available(store, parent, child, "RESULT_WHILE_PAUSED", context)
    assert "Please include the conclusion." in json.dumps(context)


def test_b05_stop_targeting_old_run_cannot_stop_new_parent(store):
    root, old_parent, children = start_family(store, count=1)
    worker_receipt(store, next(iter(children.values())))
    current = next_parent(store, root)
    store.request_control(root, "stop_reply", run_id=old_parent["id"])
    assert store.control_reason(current) is None
    assert store.current(current)
    assert store.task(root)["auto_continue"] == "on"


def test_b06_stopping_one_worker_does_not_stop_sibling_and_waits_for_ack(store):
    root, _, children = start_family(store)
    first, sibling = list(children.values())
    store.request_control(first["task_id"], "stop_task", run_id=first["id"])
    assert store.one("SELECT phase FROM runs WHERE id=?", (first["id"],))["phase"] == "running"
    reason = store.control_reason(first)
    assert reason is not None
    store.ack_control(first, reason)
    assert store.one("SELECT phase FROM runs WHERE id=?", (first["id"],))["phase"] != "running"
    assert store.current(sibling)
    assert store.task(root)["auto_continue"] == "on"


def test_b07_cancelled_root_never_wakes_from_late_child_callback(store):
    root, _, children = start_family(store, count=1)
    child = next(iter(children.values()))
    store.request_control(root, "cancel")
    reason = store.control_reason(child)
    assert reason is not None
    store.ack_control(child, reason)
    with pytest.raises(RuntimeError):
        worker_receipt(store, child, "LATE_SUCCESS")
    store.tick_waits()
    assert store.task(root)["auto_continue"] == "forbidden"
    assert store.claim() is None


def test_b08_new_requirements_invalidate_active_parent_and_keep_completed_child_evidence(store):
    root, _, children = start_family(store, count=1)
    child = next(iter(children.values()))
    worker_receipt(store, child, "OLD_REQUIREMENT_RESULT")
    old = next_parent(store, root)
    previous_revision = old["revision"]
    update = store.followup("Use the revised conclusion.", nonce="revision", task_id=root)
    assert update["task_id"] == root
    reason = store.control_reason(old)
    assert reason == "requirements_updated"
    with pytest.raises(RuntimeError):
        store.finish(old, status="succeeded", reason="old", content="Old answer")
    store.ack_control(old, reason)
    current = next_parent(store, root)
    assert current["revision"] > previous_revision
    assert "Use the revised conclusion." in json.dumps(store.orchestration_context(current))
    assert store.task(child["task_id"])["status"] == "succeeded"


def test_b09_duplicate_worker_completion_does_not_duplicate_parent_inputs(store):
    root, _, children = start_family(store, count=1)
    child = next(iter(children.values()))
    worker_receipt(store, child)
    counts = {table: len(store.all(f"SELECT * FROM {table}")) for table in ("events", "messages", "deliveries")}
    with pytest.raises(RuntimeError):
        store.finish(child, status="succeeded", reason="duplicate", content="Duplicate")
    assert counts == {table: len(store.all(f"SELECT * FROM {table}")) for table in counts}
    parent = next_parent(store, root)
    assert store.claim() is None
    assert store.current(parent)


def test_b10_crash_after_parent_claim_releases_unconsumed_results_for_recovery(store, tmp_path):
    root, _, children = start_family(store, count=1)
    worker_receipt(store, next(iter(children.values())), "DURABLE_RESULT")
    interrupted = next_parent(store, root)
    store.close()
    reopened = Store(tmp_path / "data" / "state.sqlite")
    try:
        assert reopened.recover() == 1
        resumed = next_parent(reopened, root)
        assert resumed["id"] != interrupted["id"]
        assert_result_available(reopened, resumed, next(iter(children.values())), "DURABLE_RESULT")
    finally:
        reopened.close()


def test_b11_continuation_budget_stops_new_parent_run_without_losing_child_result(store):
    spec = TaskSpec("Root work", max_continuations=1)
    root, _, children = start_family(store, count=1, spec=spec)
    worker_receipt(store, next(iter(children.values())), "FIRST_STAGE")
    parent = next_parent(store, root)
    observe(store, parent)
    second = store.spawn_worker(parent, worker_key="stage-two", objective="Stage two", source_ids=[])["task_id"]
    store.wait_workers(parent, task_ids=[second], timeout_seconds=30)
    child = store.claim()
    assert child["task_id"] == second
    worker_receipt(store, child, "SECOND_STAGE")
    store.tick_waits()
    assert store.claim() is None
    assert store.task(root)["auto_continue"] == "paused"
    assert store.task(second)["status"] == "succeeded"
    assert len(store.all("SELECT * FROM runs WHERE task_id=?", (root,))) == 2


def test_b12_retried_worker_second_completion_has_fresh_execution_identity(store):
    root, _, children = start_family(store, count=1)
    first = next(iter(children.values()))
    worker_receipt(store, first, "FAILED_STAGE", status="failed")
    parent = next_parent(store, root)
    observe(store, parent)
    store.resume(first["task_id"])
    store.wait_workers(parent, task_ids=[first["task_id"]], timeout_seconds=30)
    retried = store.claim()
    assert retried["task_id"] == first["task_id"]
    assert retried["epoch"] > first["epoch"]
    worker_receipt(store, retried, "RETRIED_SUCCESS")
    continuation = next_parent(store, root)
    assert_result_available(store, continuation, retried, "RETRIED_SUCCESS")
    assert not store.current(first)


def test_b13_late_followup_cannot_be_marked_adopted_or_success_by_old_run(store):
    root = store.submit(TaskSpec("Root work"), nonce="late-root")["task_id"]
    old = store.claim()
    observe(store, old)
    update = store.followup("A requirement after your last request.", nonce="late-update", task_id=root)
    message = store.one("SELECT * FROM messages WHERE id=?", (update["message_id"],))
    assert message["adopted_by_run"] is None
    with pytest.raises(RuntimeError):
        store.finish(old, status="succeeded", reason="ignored-update", content="Incomplete")
    store.ack_control(old, store.control_reason(old))
    current = next_parent(store, root)
    assert "A requirement after your last request." in json.dumps(observe(store, current))
    assert store.completion_errors(current)


def test_b14_wait_deadline_flushes_partial_results_without_another_event(store, monkeypatch):
    now = time.time()
    monkeypatch.setattr("personal_agent.store.time.time", lambda: now)
    root, _, children = start_family(store, timeout=5)
    first, still_running = list(children.values())
    worker_receipt(store, first, "PARTIAL_RESULT")
    store.tick_waits()
    assert store.claim() is None
    now += 6
    parent = next_parent(store, root)
    assert_result_available(store, parent, first, "PARTIAL_RESULT")
    assert store.current(still_running)


def test_spawn_is_idempotent_and_cannot_expand_source_scope_or_nest_workers(store, spec):
    root = store.submit(spec, nonce="scope")["task_id"]
    run = store.claim()
    first = store.spawn_worker(run, worker_key="stable", objective="Read policy", source_ids=["policy"])
    duplicate = store.spawn_worker(run, worker_key="stable", objective="Read policy", source_ids=["policy"])
    assert duplicate["task_id"] == first["task_id"]
    with pytest.raises(ValueError):
        store.spawn_worker(run, worker_key="unauthorized", objective="Read secret", source_ids=["secret"])
    store.wait_workers(run, task_ids=[first["task_id"]])
    worker = store.claim()
    with pytest.raises(ValueError):
        store.spawn_worker(worker, worker_key="nested", objective="Spawn deeper", source_ids=[])
    assert len(store.all("SELECT * FROM tasks WHERE root_task_id=?", (root,))) == 2


def test_ds04_explicit_followups_do_not_cross_tasks_or_duplicate_work(store):
    first = store.submit(TaskSpec("Compare plan A"), nonce="a")["task_id"]
    second = store.submit(TaskSpec("Compare plan B"), nonce="b")["task_id"]
    update = store.followup("Plan A must include dates.", nonce="a-update", task_id=first)
    replay = store.followup("Plan A must include dates.", nonce="a-update", task_id=first)
    other = store.followup("Plan B must include costs.", nonce="b-update", task_id=second)
    assert replay["duplicate"] is True and replay["followup_id"] == update["followup_id"]
    assert other["task_id"] == second
    run = store.claim()
    assert run["task_id"] == first
    context = json.dumps(store.orchestration_context(run))
    assert "Plan A must include dates." in context
    assert "Plan B must include costs." not in context
    assert len(store.all("SELECT * FROM tasks")) == 2


def test_ds05_ambiguous_thread_keeps_input_unassigned_without_resuming_tasks(store):
    first = store.submit(TaskSpec("Plan A"), nonce="a")["task_id"]
    second = store.submit(TaskSpec("Plan B"), nonce="b")["task_id"]
    for task in (first, second):
        store.subscribe(task, thread="shared", mode="own")
        store.request_control(task, "stop_reply")
    result = store.followup("Please revise that plan.", nonce="ambiguous", thread="shared")
    assert result["state"] == "unassigned" and result["task_id"] is None
    assert store.task(first)["auto_continue"] == "paused"
    assert store.task(second)["auto_continue"] == "paused"
    assert len(store.all("SELECT * FROM tasks")) == 2


def test_ds08_targeted_user_resume_does_not_resume_other_paused_task_or_context(store):
    first = store.submit(TaskSpec("Plan A"), nonce="a")["task_id"]
    second = store.submit(TaskSpec("Plan B"), nonce="b")["task_id"]
    previous = store.followup("Old note for B", nonce="b-old", task_id=second)
    for task in (first, second):
        store.request_control(task, "stop_reply")
    store.followup("Here is a correction for A", nonce="a-new", task_id=first)
    replay = store.followup("Old note for B", nonce="b-old", task_id=second)
    assert replay["duplicate"] is True and replay["followup_id"] == previous["followup_id"]
    store.followup("Background only", nonce="b-context", task_id=second, context=True)
    assert store.task(first)["auto_continue"] == "on"
    assert store.task(second)["auto_continue"] == "paused"


def test_ds10_replaced_question_and_duplicate_answer_cannot_restore_old_wait(store, tmp_path):
    root = store.submit(TaskSpec("Choose a direction"), nonce="question-root")["task_id"]
    run = store.claim()
    observe(store, run)
    old = store.ask_user(run, question="Old question?", options=["A", "B"])["question_id"]
    new = store.replace_attention(old, "Current question?", ["C", "D"])["question_id"]
    ignored = store.answer_attention(old, "A", nonce="old-answer")
    assert ignored["state"] == "ignored_stale_answer"
    assert store.claim() is None
    deliveries = store.all("SELECT * FROM deliveries WHERE content LIKE '%Old question%'")
    assert deliveries and all(d["state"] not in {"pending", "failed", "accepted"} for d in deliveries)
    first = store.answer_attention(new, "C", nonce="current-answer")
    duplicate = store.answer_attention(new, "C", nonce="current-answer")
    assert duplicate["duplicate"] is True
    assert first["task_id"] == root
    store.close()
    reopened = Store(tmp_path / "data" / "state.sqlite")
    try:
        reopened.recover()
        continuation = next_parent(reopened, root)
        context = json.dumps(reopened.orchestration_context(continuation))
        assert "Current question?" in context and "C" in context
        assert len(reopened.all("SELECT * FROM deliveries WHERE content LIKE '%Current question%'")) == 1
    finally:
        reopened.close()
