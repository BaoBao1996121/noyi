import json
import time

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.store import Store


def test_duplicate_input_returns_original_task_but_changed_payload_is_rejected(store, spec):
    first = store.submit(spec, nonce="request-1")
    duplicate = store.submit(spec, nonce="request-1")
    assert duplicate == {**first, "duplicate": True}
    with pytest.raises(ValueError, match="nonce"):
        store.submit(TaskSpec("A different instruction"), nonce="request-1")
    assert len(store.all("SELECT * FROM tasks")) == 1
    assert len(store.all("SELECT * FROM messages")) == 1
    assert len(store.all("SELECT * FROM events")) == 1


def test_unauthorized_room_submission_leaves_no_work(store, spec):
    with pytest.raises(ValueError, match="room|会话"):
        store.submit(spec, nonce="foreign", account="another-user")
    assert not store.all("SELECT * FROM tasks")
    assert not store.all("SELECT * FROM events")


def test_two_clients_cannot_claim_two_runs_for_same_task(store, spec, tmp_path):
    first_task = store.submit(spec, nonce="first")["task_id"]
    other = Store(tmp_path / "data" / "state.sqlite")
    try:
        first = store.claim()
        assert first is not None and first["task_id"] == first_task
        store.db.execute("INSERT INTO events(id,actor_id,task_id,message_id,kind,created) SELECT 'again',actor_id,task_id,message_id,'user_resume',created+1 FROM events WHERE task_id=?", (first_task,))
        store.db.execute("UPDATE tasks SET status='ready' WHERE id=?", (first_task,))
        assert other.claim() is None
        assert len(other.all("SELECT * FROM runs WHERE phase='running'")) == 1
    finally:
        other.close()


def test_one_assistant_can_run_a_quick_task_beside_its_long_task(store, spec):
    # Acceptance #3: a quick question is not queued behind the same assistant's long work.
    long_task = store.submit(spec, nonce="long")["task_id"]
    quick_task = store.submit(spec, nonce="quick")["task_id"]
    first, second = store.claim(), store.claim(exclude_task_ids={long_task})
    assert {first["task_id"], second["task_id"]} == {long_task, quick_task}
    assert first["actor_id"] == second["actor_id"]


def test_claim_does_not_mean_input_was_used_by_model(store, spec):
    submitted = store.submit(spec, nonce="crash-before-request")
    store.claim()
    message = store.one("SELECT * FROM messages WHERE id=?", (submitted["message_id"],))
    assert message["adopted_by_run"] is None
    assert message["status"] != "adopted"


def test_recovery_retains_unprocessed_input_and_invalidates_old_run(store, spec):
    submitted = store.submit(spec, nonce="recover")
    old = store.claim()
    assert store.reserve(old, "model")
    assert store.recover() == 1
    assert store.recover() == 0
    assert store.task(submitted["task_id"])["calls_used"] == 1
    event = store.one("SELECT * FROM events")
    assert event["state"] == "pending"
    assert event["claimed_by_run"] is None
    new = store.claim()
    assert new["id"] != old["id"]
    assert new["epoch"] > old["epoch"]
    assert not store.current(old)
    with pytest.raises(RuntimeError, match="stale"):
        store.finish(old, status="succeeded", reason="old", content="Obsolete success")
    assert not store.all("SELECT * FROM deliveries")
    assert store.current(new)


def test_finalization_failure_rolls_back_task_run_event_and_outbox(store, spec):
    submitted = store.submit(spec, nonce="commit-failure")
    run = store.claim()

    def fail():
        raise OSError("simulated commit boundary failure")

    with pytest.raises(OSError):
        store.finish(run, status="failed", reason="test", content="failure", before_commit=fail)
    assert store.task(submitted["task_id"])["status"] == "running"
    assert store.current(run)
    assert store.one("SELECT * FROM events")["state"] == "claimed"
    assert not store.all("SELECT * FROM deliveries")
    store.finish(run, status="failed", reason="test", content="failure")
    assert store.task(submitted["task_id"])["status"] == "failed"
    assert len(store.all("SELECT * FROM deliveries")) == 1


def test_budget_is_not_refunded_after_recovery(store):
    submitted = store.submit(TaskSpec("Write a note", max_calls=1), nonce="budget")
    run = store.claim()
    assert store.reserve(run, "model")
    assert not store.reserve(run, "model")
    store.recover()
    recovered = store.claim()
    assert not store.reserve(recovered, "model")
    assert store.task(submitted["task_id"])["calls_used"] == 1


def test_requirement_change_rejects_old_final_result(store, spec):
    submitted = store.submit(spec, nonce="revision")
    run = store.claim()
    store.db.execute("UPDATE tasks SET requirement_revision=requirement_revision+1 WHERE id=?", (submitted["task_id"],))
    assert not store.current(run)
    with pytest.raises(RuntimeError, match="stale"):
        store.finish(run, status="succeeded", reason="old_requirement", content="Old result")
    assert not store.all("SELECT * FROM deliveries")


def test_chat_input_has_no_task(store):
    submitted = store.submit(None, text="Hello", nonce="chat")
    assert submitted["task_id"] is None
    assert not store.all("SELECT * FROM tasks")
    assert store.claim()["trigger"] == "chat"


def test_request_dispatch_crash_is_unknown_not_adopted_or_completed(store, spec):
    submitted = store.submit(spec, nonce="dispatch-crash")
    run = store.claim()
    request = store.begin_request(run, [{"role": "user", "content": spec.objective}])
    usage = store.one("SELECT * FROM input_usage WHERE request_id=?", (request,))
    assert usage["message_id"] == submitted["message_id"]
    assert usage["state"] == "dispatching"
    assert store.one("SELECT * FROM messages")["adopted_by_run"] is None
    store.recover()
    assert store.one("SELECT * FROM model_requests")["state"] == "unknown"
    assert store.one("SELECT * FROM input_usage")["state"] == "unknown"
    store.end_request(request, "completed")  # Late acknowledgement cannot rewrite the recovery fact.
    assert store.one("SELECT * FROM input_usage")["state"] == "unknown"
    assert store.one("SELECT * FROM messages")["adopted_by_run"] is None


def test_model_read_is_not_task_completion_and_review_can_resume(store, spec):
    submitted = store.submit(spec, nonce="read-not-done")
    run = store.claim()
    assert store.reserve(run, "model")
    request = store.begin_request(run, [{"role": "user", "content": spec.objective}])
    store.end_request(request)
    store.finish(run, status="review", reason="no_artifact", content="Needs work.", acceptance="needs_review")
    assert store.one("SELECT * FROM input_usage")["state"] == "completed"
    assert store.one("SELECT * FROM events")["state"] == "consumed"
    assert store.task(submitted["task_id"])["status"] == "review"
    assert store.claim() is None
    result = store.resume(submitted["task_id"])
    assert result["budget_reset"] is False
    assert store.task(submitted["task_id"])["calls_used"] == 1
    resumed = store.claim()
    assert resumed["trigger"] == "user_resume"
    assert resumed["epoch"] > run["epoch"]
    assert len(store.all("SELECT * FROM tasks")) == 1
    assert len(store.all("SELECT * FROM messages")) == 1


def test_failed_request_retains_input_but_does_not_auto_retry(store, spec):
    submitted = store.submit(spec, nonce="provider-fail")
    run = store.claim()
    request = store.begin_request(run, [{"role": "user", "content": spec.objective}])
    store.end_request(request, "failed")
    store.finish(run, status="failed", reason="provider_error", content="Unable to run.")
    assert store.one("SELECT * FROM events")["state"] == "pending"
    assert store.one("SELECT * FROM messages")["adopted_by_run"] is None
    assert store.claim() is None
    store.resume(submitted["task_id"])
    assert len(store.all("SELECT * FROM events")) == 1
    assert store.claim() is not None


def test_resume_exhausted_budget_requires_explicit_extension(store):
    submitted = store.submit(TaskSpec("Write a report", max_calls=1), nonce="resume-budget")
    run = store.claim()
    assert store.reserve(run, "model")
    store.finish(run, status="paused", reason="model_budget", content="Budget exhausted.")
    with pytest.raises(ValueError, match="budget exhausted"):
        store.resume(submitted["task_id"])
    assert store.task(submitted["task_id"])["status"] == "paused"
    store.resume(submitted["task_id"], extra_calls=1)
    task = store.task(submitted["task_id"])
    assert task["calls_used"] == 1
    assert task["spec"]["max_calls"] == 2
    assert store.reserve(store.claim(), "model")


def test_explicit_resume_cannot_resurrect_cancelled_or_completed_task(store, spec):
    submitted = store.submit(spec, nonce="no-resurrection")
    for status in ("cancelled", "succeeded"):
        store.db.execute("UPDATE tasks SET status=? WHERE id=?", (status, submitted["task_id"]))
        with pytest.raises(ValueError, match="only paused"):
            store.resume(submitted["task_id"])
        assert store.task(submitted["task_id"])["status"] == status


@pytest.mark.parametrize("already_spent", [0, 20])
def test_week_of_downtime_charges_only_remaining_authorized_time_and_can_resume(store, already_spent):
    submitted = store.submit(TaskSpec("Recover work", max_seconds=60), nonce="long-downtime")
    task_id = submitted["task_id"]
    store.db.execute("UPDATE tasks SET active_seconds=? WHERE id=?", (already_spent, task_id))
    run = store.claim()
    assert store.reserve(run, "model")
    store.db.execute("UPDATE runs SET started=? WHERE id=?", (time.time() - 7 * 24 * 3600, run["id"]))
    assert store.recover() == 1
    task = store.task(task_id)
    assert task["active_seconds"] == 60
    assert task["calls_used"] == 1
    assert store.recover() == 0
    assert store.task(task_id)["active_seconds"] == 60
    recovered = store.claim()
    store.finish(recovered, status="paused", reason="time_budget", content="Time budget exhausted.")
    with pytest.raises(ValueError, match="budget exhausted"):
        store.resume(task_id)
    store.resume(task_id, extra_seconds=10)
    resumed = store.task(task_id)
    assert resumed["spec"]["max_seconds"] == 70
    assert resumed["active_seconds"] == 60
    assert resumed["calls_used"] == 1
    assert store.claim() is not None
