import json
import asyncio
from pathlib import Path

import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.context import compact_request
from personal_agent.runtime import Runtime
from personal_agent.service import Service
from personal_agent.tools import Toolset
from conftest import ScriptedModel, tool_reply, submit_written_artifact
from test_m1_store import observe


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_nudge_reopens_pi_turn_for_real_authorized_gap_without_budget_reset(store, tmp_path, source, enabled):
    missing = {"id": "missing", "kind": "file", "location": str((tmp_path / "absent.txt").resolve()), "required": False}
    spec = TaskSpec("Find the release rule using authorized sources", [missing, source], max_calls=12, enable_nudge=enabled)
    tid = store.submit(spec, nonce="nudge")["task_id"]
    def alternate(messages, tools):
        reminder = next(m["content"] for m in messages if m["content"].startswith("Runtime ending-gate reminders"))
        assert "authorized source not yet investigated: policy" in reminder
        return tool_reply("read_source", {"source_id": "policy"})
    model = ScriptedModel([tool_reply("read_source", {"source_id": "missing"}),
                           ModelReply({"role": "assistant", "content": "The first source is unavailable; stopping."}),
                           alternate,
                           tool_reply("write_artifact", {"name": "result.md", "content": "Human review required [policy]. UNAVAILABLE [missing]"}),
                           lambda m,t: tool_reply("submit_result", {"artifact_ref": next(json.loads(x["content"])["artifact_ref"] for x in reversed(m) if x["role"] == "tool" and "artifact_ref" in json.loads(x["content"])), "summary": "Result", "limitations": ["missing"]})])
    await Service(store, tmp_path / "data", model).serve(once=True)
    task = store.task(tid)
    assert task["status"] == ("succeeded" if enabled else "review")
    assert task["calls_used"] == (5 if enabled else 2)
    assert task["nudge_count"] == int(enabled)
    assert task["spec"]["max_calls"] == 12
    assert store.one("SELECT * FROM progress_log WHERE kind='negative_evidence'")["fingerprint"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_twenty_repetitions_are_bounded_even_when_tools_succeed(store, tmp_path, source, success):
    if not success:
        source = {**source, "location": str((tmp_path / "absent.txt").resolve())}
    spec = TaskSpec("Repeated observation", [source], max_calls=50, max_tools=80, enable_nudge=True)
    tid = store.submit(spec, nonce="repeat")["task_id"]
    count = 21 if success else 20  # first successful observation was genuinely new
    model = ScriptedModel([tool_reply("read_source", {"source_id": "policy"}, str(i)) for i in range(count)])
    await Service(store, tmp_path / "data", model).serve(once=True)
    task = store.task(tid)
    assert task["status"] == "paused" and task["reason"] == "stagnation_budget"
    assert task["calls_used"] == count < task["spec"]["max_calls"]
    assert len(store.all("SELECT * FROM progress_log")) == 1
    assert len(store.all("SELECT * FROM activity_log")) == count
    assert task["nudge_count"] <= 2
    assert task["failure_streak"] == (0 if success else 20)


@pytest.mark.asyncio
async def test_artifact_version_conflict_preserves_old_bytes_then_allows_explicit_revision(store, tmp_path):
    tid = store.submit(TaskSpec("Produce a reviewed revision"), nonce="versions")["task_id"]
    def read_original(messages, tools):
        receipt = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))
        return tool_reply("read_artifact", {"artifact_ref": receipt["artifact_ref"]})
    model = ScriptedModel([tool_reply("write_artifact", {"name": "plan.md", "content": "Version one"}),
                           tool_reply("write_artifact", {"name": "plan.md", "content": "Blind overwrite", "base_version": 0}), read_original,
                           tool_reply("write_artifact", {"name": "plan.md", "content": "Version two", "base_version": 1}), submit_written_artifact])
    await Service(store, tmp_path / "data", model).serve(once=True)
    assert store.task(tid)["status"] == "succeeded"
    versions = store.all("SELECT * FROM artifact_versions ORDER BY version")
    assert [v["version"] for v in versions] == [1, 2]
    assert [(tmp_path / "data/tasks" / tid / v["artifact_ref"]).read_text() for v in versions] == ["Version one", "Version two"]
    failed = [o for o in store.operations(tid) if o["status"] == "failed"]
    assert len(failed) == 1 and "version conflict" in failed[0]["result"]["detail"]
    assert not (tmp_path / "data/tasks" / tid / "artifacts" / failed[0]["id"]).exists()


class ReworkModel:
    async def complete(self, messages, tools):
        spec = json.loads(messages[1]["content"])
        ctx = json.loads(next(m["content"].split(": ",1)[1] for m in messages if m["content"].startswith("Current orchestration")))
        progress = json.loads(next(m["content"].split(": ",1)[1] for m in messages if m["content"].startswith("Durable progress")))
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if spec["objective"] == "Root requires Owner: Ava":
            spawned = [r for r in results if r.get("status") == "ready" and "duplicate" in r]
            if not ctx["children"] and not spawned:
                return tool_reply("spawn_worker", {"worker_key": "draft", "objective": "Draft scope", "source_ids": [], "max_calls": 10})
            if spawned:
                return tool_reply("wait_workers", {"task_ids": [spawned[0]["task_id"]]})
            child = ctx["children"][0]
            reads = [r for r in results if "trust" in r]
            if not reads:
                return tool_reply("read_worker_result", {"child_id": child["id"]})
            if any(r.get("state") == "rework" for r in results):
                return tool_reply("wait_workers", {"task_ids": [child["id"]]})
            if "Owner: Ava" not in reads[-1]["artifact_text"]:
                return tool_reply("request_rework", {"child_id": child["id"], "rationale": "The parent requirement names the owner; add Owner: Ava to the same deliverable.", "required_text": ["Owner: Ava"]})
            if not any(r.get("state") == "adopted" for r in results):
                return tool_reply("adopt_worker_result", {"child_id": child["id"], "artifact_sha256": reads[-1]["delivery"]["artifact"]["sha256"], "rationale": "Verified the added named owner against the root requirement."})
        writes = [r for r in results if "artifact_ref" in r and "version" in r]
        if not writes:
            base = progress["artifact_heads"][0]["version"] if progress["artifact_heads"] else 0
            return tool_reply("write_artifact", {"name": "plan.md", "content": "Owner: Ava" if spec["required_text"] else "Scope only", "base_version": base})
        return tool_reply("submit_result", {"artifact_ref": writes[-1]["artifact_ref"], "summary": "Review result", "limitations": []})


@pytest.mark.asyncio
async def test_parent_rework_retains_child_identity_budget_versions_and_review_gate(store, tmp_path):
    tid = store.submit(TaskSpec("Root requires Owner: Ava", required_text=["Owner: Ava"], max_calls=40, max_tools=100, review_workers=True), nonce="rework")["task_id"]
    await Service(store, tmp_path / "data", ReworkModel()).serve(once=True)
    assert store.task(tid)["status"] == "succeeded"
    children = store.all("SELECT * FROM tasks WHERE parent_task_id=?", (tid,))
    assert len(children) == 1 and children[0]["execution_epoch"] == 2
    assert children[0]["calls_used"] == 4
    reviews = store.all("SELECT * FROM worker_reviews ORDER BY created")
    assert [r["decision"] for r in reviews] == ["rework", "accept"]
    assert reviews[0]["child_id"] == reviews[1]["child_id"]
    assert reviews[0]["child_epoch"] != reviews[1]["child_epoch"]
    assert [v["version"] for v in store.all("SELECT version FROM artifact_versions WHERE task_id=? ORDER BY version", (children[0]["id"],))] == [1, 2]


@pytest.mark.asyncio
async def test_dependencies_do_not_start_on_child_success_without_review(store, tmp_path):
    tid = store.submit(TaskSpec("Review dependency", review_workers=True), nonce="dependency")["task_id"]
    parent = store.claim()
    observe(store, parent)
    first = store.spawn_worker(parent, worker_key="first", objective="First", source_ids=[])["task_id"]
    second = store.spawn_worker(parent, worker_key="second", objective="Second", source_ids=[], depends_on=[first])["task_id"]
    child = store.claim()
    await Runtime(store, tmp_path / "data", ScriptedModel([tool_reply("write_artifact", {"name": "result.md", "content": "Evidence"}), submit_written_artifact])).execute(child)
    store.tick_waits()
    assert store.task(second)["status"] == "waiting_dependencies" and store.claim() is None
    assert store.completion_errors(parent)
    runtime = Runtime(store, tmp_path / "data", ScriptedModel([]))
    tools = Toolset(tmp_path / "data", store.task(tid))
    read = await runtime.execute_tool(parent, tools, tool_reply("read_worker_result", {"child_id": first}).message["tool_calls"][0], 1)
    await runtime.execute_tool(parent, tools, tool_reply("adopt_worker_result", {"child_id": first, "artifact_sha256": read["result"]["delivery"]["artifact"]["sha256"], "rationale": "Checked prerequisite evidence"}).message["tool_calls"][0], 1)
    assert store.task(second)["status"] == "ready"
    second_run = store.claim()
    assert second_run["task_id"] == second
    assert store.completion_errors(second_run) == ["dependency needs current accepted evidence read: " + first]
    dep_tools = Toolset(tmp_path / "data", store.task(second))
    granted = await runtime.execute_tool(second_run, dep_tools, tool_reply("read_dependency", {"dependency_id": first}).message["tool_calls"][0], 1)
    assert granted["result"]["artifact_text"] == "Evidence"
    assert store.completion_errors(second_run) == []
    denied = await runtime.execute_tool(second_run, dep_tools, tool_reply("read_dependency", {"dependency_id": tid}).message["tool_calls"][0], 1)
    assert denied["is_error"]
    store.request_rework(parent, child_id=first, rationale="New check of the same prerequisite")
    assert store.completion_errors(second_run)  # old dependency evidence no longer unlocks completion


@pytest.mark.asyncio
async def test_handoff_preserves_requirements_followup_generation_and_negative_evidence(store, tmp_path):
    source = {"id": "missing", "kind": "file", "location": str((tmp_path / "missing").resolve()), "required": False}
    tid = store.submit(TaskSpec("Owner must retain requirements", [source]), nonce="handoff")["task_id"]
    addition = store.followup("Keep the action list", nonce="addition", task_id=tid)
    parent = store.claim()
    observe(store, parent)
    worker = store.spawn_worker(parent, worker_key="helper", objective="Helper", source_ids=[], required=False)["task_id"]
    helper = store.claim()
    store.finish(helper, status="succeeded", reason="test", content="Helper done", acceptance="passed")
    runtime = Runtime(store, tmp_path / "data", ScriptedModel([]))
    tools = Toolset(tmp_path / "data", store.task(tid))
    read = await runtime.execute_tool(parent, tools, tool_reply("read_source", {"source_id": "missing"}).message["tool_calls"][0], 1)
    store.record_investigation(parent, kind="ruled_out", statement="Original route unavailable", evidence_refs=[read["result"]["operation_id"]])
    actor = store.task(worker)["actor_id"]
    before = store.task(tid)["tools_used"]
    store.handoff_task(parent, target_actor_id=actor, reason="A different authorized teammate continues the same work")
    assert not store.current(parent)
    with pytest.raises(ValueError):
        store.resolve_followup(parent, followup_id=addition["followup_id"], generation=1, evidence_ref="stale")
    new = store.claim()
    assert new["task_id"] == tid and new["actor_id"] == actor and new["epoch"] > parent["epoch"]
    assert store.task(tid)["tools_used"] == before
    context = runtime.context(new)
    assert "Original route unavailable" in str(context)
    assert "Keep the action list" in str(context)
    assert store.one("SELECT generation FROM followups")["generation"] == 2
    assert len(store.all("SELECT * FROM handoffs")) == 1


@pytest.mark.asyncio
async def test_compaction_keeps_current_requirements_negative_evidence_and_full_read_receipts(store, tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("Useful data. " * 7500, encoding="utf-8")
    sources = [{"id": "long", "kind": "file", "location": str(path.resolve()), "required": True},
               {"id": "absent", "kind": "file", "location": str((tmp_path / "absent").resolve()), "required": False}]
    tid = store.submit(TaskSpec("KEEP_THIS_REQUIREMENT", sources, ["KEEP_THIS_REQUIREMENT"], max_calls=30, max_tools=50), nonce="compact")["task_id"]
    steps = [tool_reply("read_source", {"source_id": "absent"})]
    steps += [tool_reply("read_source", {"source_id": "long", "offset": i}, str(i)) for i in range(0,len(path.read_text()),8000)]
    steps += [tool_reply("write_artifact", {"name": "result.md", "content": "KEEP_THIS_REQUIREMENT [long]. UNAVAILABLE [absent]"}),
              lambda m,t: tool_reply("submit_result", {"artifact_ref": next(json.loads(x["content"])["artifact_ref"] for x in reversed(m) if x["role"] == "tool" and "artifact_ref" in json.loads(x["content"])), "summary": "Result", "limitations": ["absent"]})]
    model = ScriptedModel(steps)
    await Service(store, tmp_path / "data", model).serve(once=True)
    assert store.task(tid)["status"] == "succeeded"
    assert store.one("SELECT id FROM steps WHERE kind='context_compacted'")
    compressed = [r for r in model.requests if any(m["content"].startswith("Durable context after compaction") for m in r["messages"] if isinstance(m.get("content"),str))]
    assert compressed and "negative_evidence" in str(compressed[-1]) and "absent" in str(compressed[-1])
    assert all(json.loads(r["messages"][1]["content"])["objective"] == "KEEP_THIS_REQUIREMENT" for r in compressed)


def test_compaction_keeps_whole_assistant_tool_groups():
    messages = [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Requirements"}]
    for i in range(12):
        reply = tool_reply("read_source", {"source_id": "s"}, str(i))
        messages += [reply.message, {"role": "tool", "tool_call_id": str(i), "content": "a" * 8000}]
    result, changed = compact_request(messages, {"negative_evidence": "Do not forget"})
    assert changed
    expected = {c["id"] for m in result if m["role"] == "assistant" for c in m["tool_calls"]}
    actual = {m["tool_call_id"] for m in result if m["role"] == "tool"}
    assert actual == expected and len(result) < len(messages)


def test_bounded_input_batches_do_not_lose_unseen_responsibility(store):
    tid = store.submit(TaskSpec("Bound inputs"), nonce="batch")["task_id"]
    ids = [store.followup(str(i) * 12000, nonce=str(i), task_id=tid)["followup_id"] for i in range(4)]
    run = store.claim()
    context = observe(store, run)
    assert sum(len(i["content"]) for i in context["inputs"]) <= 24000
    assert len(context["followups"]) < len(ids)
    assert store.lifecycle_context(run)["pending_inputs_not_in_this_run"] > 0
    assert store.completion_errors(run)


def test_additive_spec_migration_preserves_original_nonce_idempotence(store):
    original = store.submit(TaskSpec("Original intent"), nonce="legacy")
    payload = json.loads(store.one("SELECT payload FROM messages WHERE id=?", (original["message_id"],))["payload"])
    for key in ("enable_nudge", "max_nudges", "max_stagnant_tools", "review_workers", "max_handoffs"):
        payload["spec"].pop(key)
    store.db.execute("UPDATE messages SET payload=? WHERE id=?", (json.dumps(payload), original["message_id"]))
    retry = store.submit(TaskSpec("Original intent"), nonce="legacy")
    assert retry["duplicate"] and retry["task_id"] == original["task_id"]
    with pytest.raises(ValueError):
        store.submit(TaskSpec("Changed intent"), nonce="legacy")


@pytest.mark.asyncio
async def test_ending_gate_turns_live_child_work_into_wait_without_nudge_spin(store, tmp_path):
    class WaitModel:
        def __init__(self):
            self.calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            spec = json.loads(messages[1]["content"])
            if spec["objective"] == "Child needs user":
                return tool_reply("ask_user", {"question": "Choose A or B", "options": ["A", "B"]})
            if not any(m["role"] == "tool" for m in messages):
                return tool_reply("spawn_worker", {"worker_key": "wait", "objective": "Child needs user", "source_ids": []})
            return ModelReply({"role": "assistant", "content": "I'll stop now."})
    model = WaitModel()
    tid = store.submit(TaskSpec("Root with active child", enable_nudge=True), nonce="wait-gate")["task_id"]
    service = Service(store, tmp_path / "data", model)
    await service.serve(once=True)
    assert store.task(tid)["status"] == "waiting_children"
    assert model.calls == 3
    assert not store.all("SELECT * FROM nudges")
    await service.serve(once=True)
    assert model.calls == 3
    assert store.one("SELECT state FROM attentions")["state"] == "pending"
