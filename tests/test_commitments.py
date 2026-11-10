"""Commitment ledger: what the assistant said it would do, and how it ended (C-1..C-4)."""
import json
import sqlite3
import time

import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.conversation import ConversationRouter
from personal_agent.runtime import Runtime
from personal_agent.store import Store
from conftest import ScriptedModel, tool_reply, submit_written_artifact


def incoming(store, text, *, nonce, sources=None, reply_to=None):
    return store.accept_conversation(TaskSpec(text, sources or [], max_calls=40, max_tools=100, max_seconds=900),
                                     text=text, nonce=nonce, reply_to=reply_to)


def intake(store, text, decision, *, nonce, sources=None):
    incoming(store, text, nonce=nonce, sources=sources)
    run = store.claim_conversation()
    store.conversation_context(run)
    return store.apply_conversation(run, decision)


def commitments(store):
    return store.all("SELECT * FROM commitments ORDER BY created,rowid")


def notices(store, kind="commitment"):
    return [d["content"] for d in store.all("SELECT content FROM deliveries WHERE kind=? ORDER BY rowid", (kind,))]


def test_taking_on_work_registers_the_receipt_as_a_commitment_bound_to_its_task(store, source):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理这份资料，做好发你。"}, nonce="a", sources=[source])
    [c] = commitments(store)
    assert c["state"] == "open" and c["kind"] == "task" and c["task_id"] == result["task_id"]
    assert c["quote"] == "好，我去整理这份资料，做好发你。"
    said = store.one("SELECT content FROM deliveries WHERE id=?", (c["said_delivery_id"],))
    assert said["content"] == c["quote"]
    assert store.one("SELECT kind FROM commitment_events WHERE commitment_id=?", (c["id"],))["kind"] == "registered"


def test_reply_must_declare_promises_and_an_unbacked_promise_is_refused(store):
    incoming(store, "明天的天气怎么样", nonce="r")
    run = store.claim_conversation()
    store.conversation_context(run)
    with pytest.raises(ValueError, match="promise"):
        store.apply_conversation(run, {"action": "reply", "text": "我稍后查一下发你。"})
    with pytest.raises(ValueError, match="登记"):
        store.apply_conversation(run, {"action": "reply", "text": "我稍后查一下发你。", "promise": "我稍后查一下发你"})
    assert not commitments(store)
    store.apply_conversation(run, {"action": "reply", "text": "我这里查不到实时天气。", "promise": ""})
    assert not commitments(store)


@pytest.mark.asyncio
async def test_router_records_the_refused_promise_for_replay(store, tmp_path):
    incoming(store, "明天的天气怎么样", nonce="router")
    model = ScriptedModel([
        tool_reply("resolve_conversation", {"action": "reply", "text": "我稍后查一下发你。", "promise": "我稍后查一下发你"}),
        tool_reply("resolve_conversation", {"action": "reply", "text": "我这里查不到实时天气。", "promise": ""}, "second"),
    ])
    await Runtime(store, tmp_path / "data", model).execute(store.claim_conversation())
    event = store.one("SELECT * FROM commitment_events WHERE kind='unregistered_refused'")
    assert json.loads(event["data"])["quote"] == "我稍后查一下发你" and event["room_id"] == "local"


@pytest.mark.asyncio
async def test_accepted_result_fulfils_with_a_verified_artifact(store, source, tmp_path, successful_model):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理，做好发你。"}, nonce="f", sources=[source])
    await Runtime(store, tmp_path / "data", successful_model).execute(store.claim())
    [c] = commitments(store)
    assert c["state"] == "fulfilled"
    evidence = json.loads(c["evidence"])
    artifact = next(e for e in evidence if e["kind"] == "artifact")
    delivery = store.one("SELECT * FROM deliveries WHERE id=?", (c["closing_delivery_id"],))
    assert delivery["task_id"] == result["task_id"] and json.loads(delivery["artifact"])["sha256"] == artifact["sha256"]
    assert artifact["url"] == "/r/" + result["task_id"]


def test_fulfilment_refuses_evidence_that_does_not_exist(store, source):
    intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="x", sources=[source])
    [c] = commitments(store)
    with pytest.raises(ValueError):
        store.close_commitment(c["id"], "fulfilled", [{"kind": "artifact", "task_id": c["task_id"], "ref": "artifacts/none.md", "sha256": "0" * 64}])
    with pytest.raises(ValueError):
        store.close_commitment(c["id"], "fulfilled", [{"kind": "operation", "task_id": c["task_id"], "id": "invented"}])
    assert commitments(store)[0]["state"] == "open"


@pytest.mark.asyncio
async def test_unable_closes_honestly_with_the_message_the_user_received(store, tmp_path):
    intake(store, "两分钟后提醒我喝水", {"action": "new_task", "receipt": "好，两分钟后提醒你。"}, nonce="u")
    model = ScriptedModel([tool_reply("report_unable", {"explanation": "我这台电脑上的服务现在没法发提醒，你可以开着服务再交代一次。"})])
    await Runtime(store, tmp_path / "data", model).execute(store.claim())
    [c] = commitments(store)
    assert c["state"] == "unable"
    assert store.one("SELECT content FROM deliveries WHERE id=?", (c["closing_delivery_id"],))["content"].startswith("我这台电脑")


def test_cancel_releases_and_links_the_cancel_notice(store, source):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="c", sources=[source])
    incoming(store, "取消这件事", nonce="c2")
    [c] = commitments(store)
    assert c["state"] == "released" and c["task_id"] == result["task_id"]
    assert store.one("SELECT content FROM deliveries WHERE id=?", (c["closing_delivery_id"],))["content"] == "已取消这项委托。"


def test_user_deadline_is_verified_and_overdue_is_told_once(store, source):
    with pytest.raises(ValueError, match="时限"):
        intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。", "due_at": "2099-01-01 18:00", "due_basis": "周五前"}, nonce="d0", sources=[source])
    intake_run = store.one("SELECT id FROM runs WHERE trigger='conversation' AND phase='running'")
    store.db.execute("UPDATE runs SET phase='ended' WHERE id=?", (intake_run["id"],))
    store.db.execute("UPDATE conversation_inputs SET state='failed'")
    intake(store, "帮我整理这份资料，今晚六点前给我", {"action": "new_task", "receipt": "好，六点前整理好发你。", "due_at": "2099-01-01 18:00", "due_basis": "今晚六点前"}, nonce="d1", sources=[source])
    [c] = [c for c in commitments(store) if c["due_at"]]
    assert c["due_basis"] == "user" and c["due_quote"] == "今晚六点前"
    store.tick_commitments(now=c["due_at"] - 5)
    assert not notices(store)
    store.tick_commitments(now=c["due_at"] + 10)
    store.tick_commitments(now=c["due_at"] + 70)
    [line] = notices(store)
    assert "好，六点前整理好发你" in line and "还没做完" in line and "改个时间" in line
    assert store.one("SELECT state FROM commitments WHERE id=?", (c["id"],))["state"] == "open"


def test_overdue_found_after_downtime_says_so(store, source):
    intake(store, "帮我整理这份资料，今晚六点前给我", {"action": "new_task", "receipt": "好，六点前发你。", "due_at": "2099-01-01 18:00", "due_basis": "今晚六点前"}, nonce="dt", sources=[source])
    c = commitments(store)[0]
    store.tick_commitments(now=c["due_at"] - 60)
    store.tick_commitments(now=c["due_at"] + 3600)
    assert "服务没开" in notices(store)[0]


def test_assistant_can_set_its_own_deadline_only_with_words_it_says(store, source):
    intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我今晚八点前整理好发你。", "due_at": "2099-01-01 20:00", "due_basis": "今晚八点前"}, nonce="ad", sources=[source])
    assert commitments(store)[0]["due_basis"] == "assistant"


def test_blocked_without_deadline_is_reminded_once_after_a_day(store, source):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="s", sources=[source])
    run = store.claim()
    store.finish(run, status="paused", reason="time_budget", content="卡住了")
    c = commitments(store)[0]
    assert c["state"] == "open" and c["blocked_since"]
    store.tick_commitments(now=c["blocked_since"] + 3600)
    assert not notices(store)
    store.tick_commitments(now=c["blocked_since"] + 86400 + 1)
    store.tick_commitments(now=c["blocked_since"] + 2 * 86400)
    [line] = notices(store)
    assert "卡住" in line and "继续" in line and result["task_id"]


def test_followup_supersedes_the_earlier_commitment(store, source):
    first = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="p1", sources=[source])
    intake(store, "再加一段风险分析", {"action": "followup", "task_id": first["task_id"], "receipt": "好，加上风险分析一起发你。"}, nonce="p2")
    old, new = commitments(store)
    assert old["state"] == "superseded" and new["supersedes"] == old["id"] and new["state"] == "open"
    assert old["closing_delivery_id"] == new["said_delivery_id"]


def test_only_the_users_words_reschedule_or_release(store, source):
    intake(store, "帮我整理这份资料，今晚六点前给我", {"action": "new_task", "receipt": "好，六点前发你。", "due_at": "2099-01-01 18:00", "due_basis": "今晚六点前"}, nonce="m1", sources=[source])
    c = commitments(store)[0]
    incoming(store, "改到后天中午吧", nonce="m2")
    run = store.claim_conversation()
    context = store.conversation_context(run)
    assert context["open_commitments"][0]["id"] == c["id"]
    with pytest.raises(ValueError):
        store.apply_conversation(run, {"action": "commitment", "commitment_id": c["id"], "commitment_op": "reschedule", "due_at": "2099-01-03 12:00", "basis": "我自己改一下"})
    store.apply_conversation(run, {"action": "commitment", "commitment_id": c["id"], "commitment_op": "reschedule", "due_at": "2099-01-03 12:00", "basis": "改到后天中午吧"})
    old, new = commitments(store)
    assert old["state"] == "superseded" and new["due_basis"] == "user" and new["task_id"] == old["task_id"]
    incoming(store, "这件事不要了", nonce="m3")
    run = store.claim_conversation()
    store.conversation_context(run)
    store.apply_conversation(run, {"action": "commitment", "commitment_id": new["id"], "commitment_op": "release", "basis": "这件事不要了"})
    assert commitments(store)[1]["state"] == "released"
    assert store.task(new["task_id"])["cancel_state"] == "cancelled"


@pytest.mark.asyncio
async def test_one_time_reminder_stays_open_until_the_reminder_itself_is_delivered(store, tmp_path):
    from personal_agent.triggers import TriggerScheduler
    result = intake(store, "明天早上九点提醒我交周报", {"action": "new_task", "result_kind": "action_receipt", "receipt": "好，明早九点提醒你交周报。"}, nonce="o")
    message = store.one("SELECT id FROM messages WHERE task_id=?", (result["task_id"],))["id"]
    def complete(messages, tools):
        receipt = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and "registration_receipt" in m["content"])
        return tool_reply("complete_action", {"summary": "已安排明早九点提醒你交周报。", "effect_refs": [receipt["operation_id"]]}, "done")
    model = ScriptedModel([
        tool_reply("start_following", {"title": "交周报提醒", "objective": "提醒用户交周报", "source_ids": [], "authorization_message_id": message,
                                        "authorization_quote": "明天早上九点提醒我交周报", "request_key": "remind", "schedule": {"kind": "once", "at": "2099-01-02 09:00"}}, "reg"),
        complete])
    await Runtime(store, tmp_path / "data", model).execute(store.claim())
    [c] = commitments(store)
    assert c["state"] == "open" and c["kind"] == "scheduled" and c["policy_id"] and c["goal_id"]
    from personal_agent.schedule import once_at
    assert c["due_at"] == once_at({"at": "2099-01-02 09:00", "timezone": "Asia/Shanghai"})
    occurrence_task = store.submit(TaskSpec("提醒用户交周报"), nonce="occ", source="routine")["task_id"]
    goal, policy = store.goal(c["goal_id"]), store.policy(c["policy_id"])
    store.db.execute("UPDATE tasks SET goal_id=?,goal_revision=?,policy_id=?,policy_revision=? WHERE id=?", (goal["id"], goal["revision"], policy["id"], policy["revision"], occurrence_task))
    run = store.claim()
    store.finish(run, status="succeeded", reason="accepted", content="该交周报了。", acceptance="passed")
    c = commitments(store)[0]
    assert c["state"] == "fulfilled"
    assert store.one("SELECT content FROM deliveries WHERE id=?", (c["closing_delivery_id"],))["content"] == "该交周报了。"


@pytest.mark.asyncio
async def test_executor_cannot_promise_later_work_without_registering_it(store, source, tmp_path):
    intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="e", sources=[source])
    def promising(messages, tools):
        receipt = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))
        return tool_reply("submit_result", {"artifact_ref": receipt["artifact_ref"], "summary": "整理好了。", "limitations": [], "promise": "之后资料有变化我会再告诉你"}, "promise")
    model = ScriptedModel([
        tool_reply("read_source", {"source_id": "policy"}, "read"),
        tool_reply("write_artifact", {"name": "report.md", "content": "The release requires human review. [policy]"}, "write"),
        promising, submit_written_artifact])
    await Runtime(store, tmp_path / "data", model).execute(store.claim())
    failed = [o for o in store.all("SELECT * FROM operations WHERE tool='submit_result'") if "promise" in o["arguments"]]
    assert "登记" in failed[0]["result"]
    assert commitments(store)[0]["state"] == "fulfilled"


def test_an_unexplained_closure_is_disclosed_by_the_safety_net(store, source):
    intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="n", sources=[source])
    c = commitments(store)[0]
    store.close_commitment(c["id"], "released", [], reason="背后的跟进已结束")
    store.tick_commitments(now=time.time() + 120)
    [line] = notices(store)
    assert "好，我去整理" in line and "背后的跟进已结束" in line
    assert store.one("SELECT closing_delivery_id FROM commitments")["closing_delivery_id"]


def test_upgrade_backs_up_then_backfills_existing_work(tmp_path, source, successful_model):
    path = tmp_path / "data" / "state.sqlite"
    store = Store(path)
    intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="b", sources=[source])
    store.db.execute("DROP TABLE commitments")
    store.db.execute("DROP TABLE commitment_events")
    store.db.execute("PRAGMA user_version=9")
    store.close()
    upgraded = Store(path)
    try:
        backups = list((path.parent / "backups").glob("state-v9-*.sqlite"))
        assert len(backups) == 1
        assert sqlite3.connect(backups[0]).execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        [c] = commitments(upgraded)
        assert c["quote"] == "好，我去整理。" and c["state"] == "open" and c["origin"] == "backfill" and c["said_delivery_id"]
        assert upgraded.one("PRAGMA user_version")["user_version"] == 10
    finally:
        upgraded.close()
