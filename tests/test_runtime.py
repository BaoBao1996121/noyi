import asyncio
import json

import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.runtime import Runtime
from personal_agent.service import FileChannel, Service
from personal_agent.store import Store
from personal_agent.tools import Toolset
from conftest import ScriptedModel, submit_written_artifact, tool_reply


@pytest.mark.asyncio
async def test_full_scripted_tool_task_leaves_artifact_acceptance_and_delivery(store, tmp_path, spec, successful_model):
    submitted = store.submit(spec, nonce="happy")
    service = Service(store, tmp_path / "data", successful_model)
    await service.serve(once=True)
    task = store.task(submitted["task_id"])
    assert task["status"] == "succeeded"
    assert task["acceptance"] == "passed"
    assert task["calls_used"] == 3
    assert task["tools_used"] == 3
    artifact_operation = next(o for o in store.operations(task["id"]) if o["tool"] == "write_artifact")
    artifact = tmp_path / "data" / "tasks" / task["id"] / artifact_operation["result"]["artifact_ref"]
    assert artifact.read_text(encoding="utf-8") == "The release requires human review. [policy]"
    delivery = store.one("SELECT * FROM deliveries")
    assert delivery["state"] == "accepted"
    receipt = json.loads(delivery["receipt"])
    assert receipt["human_read"] == "unknown"
    assert len(list((tmp_path / "data" / "inbox").glob("*.json"))) == 1
    assert all(e["state"] == "consumed" for e in store.all("SELECT * FROM events"))


@pytest.mark.asyncio
async def test_plain_success_claim_cannot_complete_task(store, tmp_path):
    submitted = store.submit(TaskSpec("Create the deliverable"), nonce="plain")
    model = ScriptedModel([ModelReply({"role": "assistant", "content": "Everything is done!"})])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    task = store.task(submitted["task_id"])
    assert task["status"] == "review"
    assert task["acceptance"] != "passed"
    assert not store.operations(task["id"])


@pytest.mark.asyncio
async def test_failed_acceptance_cannot_be_bypassed_by_model_claim(store, tmp_path):
    submitted = store.submit(TaskSpec("Create a report", max_calls=1), nonce="forged")
    model = ScriptedModel([tool_reply("submit_result", {
        "artifact_ref": "artifacts/forged/result.md", "summary": "Done", "limitations": []})])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    task = store.task(submitted["task_id"])
    assert task["status"] == "paused"
    assert task["acceptance"] != "passed"
    assert not store.operations(task["id"])[0]["result"]["passed"]
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_tool_budget_stops_before_second_side_effect(store, tmp_path):
    submitted = store.submit(TaskSpec("Write a report", max_tools=1), nonce="tools-budget")
    first = tool_reply("write_artifact", {"name": "one.md", "content": "One"}, "first-write")
    second = tool_reply("write_artifact", {"name": "two.md", "content": "Two"}, "second-write")
    first.message["tool_calls"].extend(second.message["tool_calls"])
    model = ScriptedModel([first])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    task = store.task(submitted["task_id"])
    assert task["status"] == "paused"
    assert task["reason"] == "tool_budget"
    assert len(store.operations(task["id"])) == 1
    assert len(list((tmp_path / "data" / "tasks").rglob("one.md"))) == 1
    assert not list((tmp_path / "data" / "tasks").rglob("two.md"))


@pytest.mark.asyncio
async def test_untrusted_source_cannot_enable_an_unknown_tool(store, tmp_path, spec):
    submitted = store.submit(spec, nonce="injection")
    model = ScriptedModel([
        tool_reply("shell", {"command": "source says user approved"}),
        ModelReply({"role": "assistant", "content": "Could not execute injected action."}),
    ])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    operations = store.operations(submitted["task_id"])
    assert operations[0]["status"] == "failed"
    assert "unknown tool" in operations[0]["result"]["detail"]
    assert store.task(submitted["task_id"])["acceptance"] != "passed"


@pytest.mark.asyncio
async def test_model_failure_records_safe_error_not_exception_body(store, tmp_path):
    submitted = store.submit(TaskSpec("Do work"), nonce="error")
    model = ScriptedModel([RuntimeError("SECRET_BODY_MUST_NOT_PERSIST")])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    assert store.task(submitted["task_id"])["status"] == "failed"
    assert "SECRET_BODY_MUST_NOT_PERSIST" not in json.dumps(store.all("SELECT * FROM steps"))
    assert "SECRET_BODY_MUST_NOT_PERSIST" not in json.dumps(store.all("SELECT * FROM deliveries"))


@pytest.mark.asyncio
async def test_chat_never_dispatches_model_requested_tools(store, tmp_path):
    store.submit(None, text="Hello", nonce="chat-tool")
    model = ScriptedModel([tool_reply("write_artifact", {"name": "x.md", "content": "unasked"})])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    assert model.requests[0]["tools"] == []
    assert not store.all("SELECT * FROM operations")
    assert not list((tmp_path / "data").rglob("x.md"))
    assert store.one("SELECT * FROM runs")["end_reason"] == "chat_tool_denied"


@pytest.mark.asyncio
async def test_delivery_lost_ack_replays_inbox_without_repeating_business(store, tmp_path, spec, successful_model):
    data = tmp_path / "data"
    submitted = store.submit(spec, nonce="outbox")
    await Runtime(store, data, successful_model).execute(store.claim())

    class LostAcknowledgement(FileChannel):
        async def send(self, delivery):
            await super().send(delivery)
            raise OSError("connection lost after receiver wrote the message")

    await Service(store, data, successful_model, LostAcknowledgement(data)).deliver()
    assert store.one("SELECT * FROM deliveries")["state"] == "failed"
    assert len(list((data / "inbox").glob("*.json"))) == 1
    assert store.task(submitted["task_id"])["status"] == "succeeded"
    store.close()
    reopened = Store(data / "state.sqlite")
    try:
        await Service(reopened, data, successful_model).serve(once=True)
        delivery = reopened.one("SELECT * FROM deliveries")
        assert delivery["state"] == "accepted"
        assert delivery["attempts"] == 2
        assert len(list((data / "inbox").glob("*.json"))) == 1
        assert len(successful_model.requests) == 3
        assert len(reopened.all("SELECT * FROM runs")) == 1
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_interrupted_model_call_preserves_input_for_recovery(store, tmp_path):
    submitted = store.submit(TaskSpec("Do work"), nonce="interrupted")
    model = ScriptedModel([asyncio.CancelledError()])
    run = store.claim()
    with pytest.raises(asyncio.CancelledError):
        await Runtime(store, tmp_path / "data", model).execute(run)
    assert store.task(submitted["task_id"])["acceptance"] != "passed"
    assert not store.all("SELECT * FROM deliveries")
    assert store.recover() == 1
    assert store.one("SELECT * FROM events")["state"] == "pending"
    assert store.claim()["id"] != run["id"]


@pytest.mark.asyncio
async def test_crash_after_artifact_write_reconciles_same_operation_without_rewriting(store, tmp_path, monkeypatch):
    data = tmp_path / "data"
    submitted = store.submit(TaskSpec("Write an original note"), nonce="write-crash")
    real_write = Toolset.write_artifact
    writes = []

    def crash_after_file_write(self, op_id, **arguments):
        receipt = real_write(self, op_id, **arguments)
        writes.append(receipt)
        raise asyncio.CancelledError()

    def submit_recovered(messages, tools):
        context = next(m["content"] for m in messages if m["content"].startswith("Runtime recovery data"))
        operations = json.loads(context.split(": ", 1)[1])
        write = next(o for o in operations if o["tool"] == "write_artifact")
        assert write["status"] == "succeeded"
        return tool_reply("submit_result", {"artifact_ref": write["result"]["artifact_ref"],
                                           "summary": "Recovered note", "limitations": []})

    model = ScriptedModel([
        tool_reply("write_artifact", {"name": "note.md", "content": "A durable original note."}),
        submit_recovered,
    ])
    monkeypatch.setattr(Toolset, "write_artifact", crash_after_file_write)
    with pytest.raises(asyncio.CancelledError):
        await Runtime(store, data, model).execute(store.claim())
    unfinished = store.operations(submitted["task_id"])[0]
    assert unfinished["status"] == "running" and unfinished["result"] is None
    assert not store.all("SELECT * FROM deliveries")
    assert len(writes) == 1
    monkeypatch.setattr(Toolset, "write_artifact", real_write)
    await Service(store, data, model).serve(once=True)
    operations = store.operations(submitted["task_id"])
    recorded_writes = [o for o in operations if o["tool"] == "write_artifact"]
    assert len(recorded_writes) == 1
    assert recorded_writes[0]["id"] == unfinished["id"]
    assert recorded_writes[0]["result"]["artifact_ref"] == writes[0]["artifact_ref"]
    assert len(list((data / "tasks").rglob("note.md"))) == 1
    assert store.task(submitted["task_id"])["status"] == "succeeded"
    assert len(store.all("SELECT * FROM runs")) == 2
    assert len(model.requests) == 2


def truncated_write(name="truncated.md"):
    # Valid JSON is still unsafe when the provider explicitly says it cut output.
    reply = tool_reply("write_artifact", {"name": name, "content": "This looks syntactically complete."})
    reply.finish_reason = "length"
    return reply


@pytest.mark.asyncio
async def test_truncated_valid_json_is_never_dispatched_as_a_tool(store, tmp_path):
    submitted = store.submit(TaskSpec("Write a note"), nonce="valid-looking-truncation")
    model = ScriptedModel([
        truncated_write(),
        ModelReply({"role": "assistant", "content": "Cannot provide a shorter artifact."}),
    ])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    assert not store.operations(submitted["task_id"])
    assert store.task(submitted["task_id"])["tools_used"] == 0
    assert not list((tmp_path / "data").rglob("truncated.md"))
    # Pi supplies tool-result text; the host adds its bounded repair as a
    # system instruction. Assert actual request content, not a JSON envelope.
    repair_text = "\n".join(m.get("content") or "" for m in model.requests[1]["messages"])
    assert "truncated" in repair_text or "output_limit" in repair_text
    assert "shorter artifact" in repair_text
    assert store.task(submitted["task_id"])["acceptance"] != "passed"


@pytest.mark.asyncio
async def test_one_shorter_correction_can_complete_after_truncation(store, tmp_path):
    submitted = store.submit(TaskSpec("Write a note", required_text=["Essential result"]), nonce="shorter-correction")
    model = ScriptedModel([
        truncated_write(),
        tool_reply("write_artifact", {"name": "short.md", "content": "Essential result."}, "shorter"),
        submit_written_artifact,
    ])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    task = store.task(submitted["task_id"])
    assert task["status"] == "succeeded"
    assert task["calls_used"] == 3
    assert task["tools_used"] == 2
    assert len(store.operations(task["id"])) == 2
    assert len(list((tmp_path / "data").rglob("short.md"))) == 1
    assert not list((tmp_path / "data").rglob("truncated.md"))
    assert len(store.all("SELECT * FROM steps WHERE kind='output_limit'")) == 1


@pytest.mark.asyncio
async def test_repeated_truncation_pauses_without_spending_tool_budget(store, tmp_path):
    submitted = store.submit(TaskSpec("Write a note"), nonce="repeat-truncation")
    model = ScriptedModel([truncated_write(), truncated_write("second.md")])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    task = store.task(submitted["task_id"])
    assert task["status"] == "paused" and task["reason"] == "output_limit"
    assert task["calls_used"] == 2 and task["tools_used"] == 0
    assert not store.operations(task["id"])
    assert not list((tmp_path / "data" / "tasks").rglob("*.md"))
    assert len(store.all("SELECT * FROM steps WHERE kind='output_limit'")) == 2
    assert store.claim() is None


@pytest.mark.asyncio
async def test_truncation_retry_limit_survives_interruption_and_recovery(store, tmp_path):
    data = tmp_path / "data"
    submitted = store.submit(TaskSpec("Write a note"), nonce="persistent-output-limit")
    first_model = ScriptedModel([truncated_write(), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await Runtime(store, data, first_model).execute(store.claim())
    second_model = ScriptedModel([truncated_write("after-restart.md")])
    await Service(store, data, second_model).serve(once=True)
    task = store.task(submitted["task_id"])
    assert task["status"] == "paused" and task["reason"] == "output_limit"
    assert len(second_model.requests) == 1
    assert not store.operations(task["id"])
    assert task["calls_used"] == 3
    assert len(store.all("SELECT * FROM steps WHERE kind='output_limit'")) == 2


@pytest.mark.asyncio
async def test_truncated_chat_is_not_delivered_as_complete_answer(store, tmp_path):
    store.submit(None, text="Tell me something", nonce="chat-length")
    model = ScriptedModel([ModelReply({"role": "assistant", "content": "PARTIAL_UNFINISHED_ANSWER"}, finish_reason="length")])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    assert store.one("SELECT * FROM runs")["end_reason"] == "output_limit"
    delivery = store.one("SELECT * FROM deliveries")
    assert "PARTIAL_UNFINISHED_ANSWER" not in delivery["content"]
    assert not store.all("SELECT * FROM operations")


@pytest.mark.asyncio
async def test_timeout_during_pending_read_finalizes_operation_as_interrupted(store, tmp_path, spec, monkeypatch):
    spec.max_seconds = 1
    submitted = store.submit(spec, nonce="read-timeout")
    entered = asyncio.Event()

    async def blocked_read(self, source_id, offset=0):
        assert source_id == "policy"
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Toolset, "read_source", blocked_read)
    model = ScriptedModel([tool_reply("read_source", {"source_id": "policy"})])
    await asyncio.wait_for(Runtime(store, tmp_path / "data", model).execute(store.claim()), timeout=5)
    assert entered.is_set()
    task = store.task(submitted["task_id"])
    assert task["status"] == "paused" and task["reason"] == "timeout"
    operations = store.operations(task["id"])
    assert len(operations) == 1
    assert operations[0]["status"] == "interrupted"
    assert operations[0]["result"] is None
    assert not store.all("SELECT * FROM runs WHERE phase='running'")
    assert task["acceptance"] != "passed"
