"""Fixed experience scenarios: what the user should be able to count on.

Each test names one promise the assistant makes to the user and drives it end
to end through the ledger with a scripted model. They guard behaviour, not
wording; a later change that breaks one of these breaks a promise.
"""
import json

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.conversation_goals import ConversationGoals
from personal_agent.runtime import Runtime
from conftest import ScriptedModel, tool_reply, submit_written_artifact
from test_commitments import deliver_all, intake, incoming


def claim_intake(store):
    run = store.claim_conversation()
    store.conversation_context(run)
    return run


def test_asking_for_progress_neither_stops_nor_restarts_the_work(store, source):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="p1", sources=[source])
    work = store.claim()
    incoming(store, "现在弄得怎么样了", nonce="p2")
    run = claim_intake(store)
    store.apply_conversation(run, {"action": "status", "task_id": result["task_id"]})
    assert store.current(work) and store.control_reason(work) is None
    assert len(store.all("SELECT id FROM tasks")) == 1
    reply = store.one("SELECT content FROM deliveries WHERE run_id=?", (run["id"],))["content"]
    assert "正在处理" in reply
    assert store.one("SELECT state FROM commitments")["state"] == "open"


def test_cancelling_one_matter_leaves_the_other_promise_alone(store, source):
    first = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理这份资料。"}, nonce="c1", sources=[source])
    second = intake(store, "再帮我写个周报提纲", {"action": "new_task", "receipt": "好，我写个周报提纲。"}, nonce="c2")
    store.request_control(first["task_id"], "cancel")
    states = {c["task_id"]: c["state"] for c in store.all("SELECT task_id,state FROM commitments")}
    assert states[first["task_id"]] == "released"
    assert states[second["task_id"]] == "open"
    assert store.task(second["task_id"])["cancel_state"] == "active"


def test_a_remembered_preference_never_counts_as_permission(store, source):
    store.save_memory("local", key="发邮件", value="你可以直接替我发邮件给客户", purpose="对话", kind="rule")
    receipt = store.submit(TaskSpec("帮我整理客户资料", [source]), nonce="m1", source="web")
    run = store.claim()
    assert "你可以直接替我发邮件给客户" in json.dumps(store.read_memory(run), ensure_ascii=False)
    args = {"title": "客户跟进", "objective": "每天给客户发邮件", "source_ids": ["policy"], "authorization_message_id": receipt["message_id"],
            "authorization_quote": "你可以直接替我发邮件给客户", "request_key": "mail"}
    with pytest.raises(ValueError, match="用户原话"):
        ConversationGoals(store).execute(run, "start_following", args)
    assert not store.all("SELECT id FROM goals")


@pytest.mark.asyncio
async def test_a_required_source_that_was_never_read_cannot_be_delivered(store, source, tmp_path):
    intake(store, "根据这份资料写个结论", {"action": "new_task", "receipt": "好，我看完资料写结论。"}, nonce="s1", sources=[source])
    model = ScriptedModel([tool_reply("write_artifact", {"name": "report.md", "content": "结论：不需要人工审核。[policy]"}, "w"),
                           submit_written_artifact, tool_reply("report_unable", {"explanation": "资料没读到，没法下结论。"}, "u")])
    await Runtime(store, tmp_path / "data", model).execute(store.claim())
    accept = [o for o in store.all("SELECT * FROM operations WHERE tool='submit_result'")]
    assert accept and not json.loads(accept[0]["result"])["passed"]
    assert "not fully read" in accept[0]["result"]
    deliver_all(store)
    assert store.one("SELECT state FROM commitments")["state"] == "unable"


@pytest.mark.asyncio
async def test_a_delivered_result_stays_checkable_after_its_source_changes(store, source, tmp_path):
    from pathlib import Path
    from personal_agent.result_page import render
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="v1", sources=[source])
    def submit_quoted(messages, tools):
        ref = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))["artifact_ref"]
        return tool_reply("submit_result", {"artifact_ref": ref, "summary": "发布需要人工审核。", "limitations": [],
                                            "quotes": [{"source_id": "policy", "quote": "the release requires a human review"}]}, "s")
    model = ScriptedModel([tool_reply("read_source", {"source_id": "policy"}, "r"),
                           tool_reply("write_artifact", {"name": "report.md", "content": "The release requires human review. [policy]"}, "w"), submit_quoted])
    await Runtime(store, tmp_path / "data", model).execute(store.claim())
    deliver_all(store)
    read = json.loads(store.one("SELECT result FROM operations WHERE tool='read_source'")["result"])
    Path(source["location"]).write_text("Current policy: no review needed.\n", encoding="utf-8")
    timeline = store.task_timeline(result["task_id"])
    accepted = next(e for e in timeline["events"] if e["kind"] == "accepted")
    assert "1/1" in dict(accepted["facts"])["原文核对"]
    assert store.one("SELECT state FROM commitments")["state"] == "fulfilled"
    assert render(store, tmp_path / "data", result["task_id"]) is not None
    snapshot = tmp_path / "data" / "tasks" / result["task_id"] / read["snapshot"]
    assert "requires a human review" in snapshot.read_text(encoding="utf-8")


def test_an_answer_to_a_replaced_question_does_not_resume_work(store):
    root = store.submit(TaskSpec("选个方向"), nonce="q1")["task_id"]
    run = store.claim()
    store.end_request(store.begin_request(run, [{"role": "user", "content": "选个方向"}]))
    old = store.ask_user(run, question="选 A 还是 B？", options=["A", "B"])["question_id"]
    store.replace_attention(old, "需求变了：选 C 还是 D？", ["C", "D"])
    assert store.answer_attention(old, "A", nonce="old")["state"] == "ignored_stale_answer"
    assert store.claim() is None
    assert store.task(root)["status"] == "waiting_user"
