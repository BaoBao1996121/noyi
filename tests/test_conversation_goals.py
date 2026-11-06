"""Conversation-to-scheduler authority, effect receipts and recovery contracts."""
import json
import time

import pytest

from personal_agent.contracts import TaskSpec, encode
from personal_agent.conversation_goals import ConversationGoals
from personal_agent.store import Store
from personal_agent.runtime import Runtime
from personal_agent.triggers import TriggerScheduler
from conftest import ScriptedModel, tool_reply, submit_written_artifact


def user_run(store, text="帮我持续盯着这份资料，有变化就整理新的行动清单", *, sources=(), nonce="follow", room="local", actor="default", source="web"):
    receipt = store.submit(TaskSpec(text, list(sources)), nonce=nonce, room=room, actor=actor, source=source)
    run = store.claim()
    assert run and run["task_id"] == receipt["task_id"]
    request = store.begin_request(run, [{"role": "user", "content": text}])
    store.end_request(request)
    return run, receipt["message_id"]


def start_args(message_id, source_id="policy", **changes):
    return {"title": "资料跟进", "objective": "读取最新资料并整理变化和行动清单；引用资料且不补造缺失要求", "source_ids": [source_id] if source_id else [],
            "authorization_message_id": message_id, "authorization_quote": "持续盯着这份资料", "request_key": "follow-source", **changes}


def finish(store, run):
    store.finish(run, status="succeeded", reason="test_complete", content="已处理", acceptance="passed")


def test_registers_existing_scheduler_and_preserves_exact_authority(store, source):
    run, mid = user_run(store, sources=[source])
    tools = ConversationGoals(store)
    context = tools.context(run)
    assert context["current_user_requests"][0]["id"] == mid
    result = tools.execute(run, "start_following", start_args(mid))
    policy = store.policy(result["registration_receipt"]["policy_id"])
    assert policy["state"] == "active"
    assert policy["registration_receipt"]["registration_id"] == result["registration_receipt"]["registration_id"]
    assert policy["schedule"]["kind"] == "watch" and policy["schedule"]["interval_seconds"] == 300
    assert policy["template"]["sources"] == [source]
    history = store.one("SELECT * FROM goal_history WHERE id=?", (result["authority_receipt"],))
    assert json.loads(history["data"])["message_id"] == mid
    assert json.loads(history["data"])["task_id"] == run["task_id"]
    assert source["location"] not in encode(result)
    assert "注册" not in result["goal"]["next_action"] + result["goal"]["wait_reason"]


def test_retry_is_idempotent_across_instance_and_changed_tool_call_key(store, source):
    run, mid = user_run(store, sources=[source])
    args = start_args(mid)
    first = ConversationGoals(store).execute(run, "start_following", args)
    duplicate = ConversationGoals(store).execute(run, "start_following", args)
    retry = ConversationGoals(store).execute(run, "start_following", {**args, "request_key": "new-model-call-key"})
    assert duplicate["duplicate"] and retry["duplicate"]
    assert first["registration_receipt"] == duplicate["registration_receipt"] == retry["registration_receipt"]
    assert store.one("SELECT COUNT(*) n FROM goals")["n"] == 1
    assert store.one("SELECT COUNT(*) n FROM routine_policies")["n"] == 1
    with pytest.raises(ValueError, match="另一项安排"):
        ConversationGoals(store).execute(run, "start_following", {**args, "title": "新标题"})


def test_crash_recovery_does_not_register_twice_or_reactivate_later_pause(store, source, tmp_path):
    run, mid = user_run(store, sources=[source])
    args = start_args(mid)
    first = ConversationGoals(store).execute(run, "start_following", args)
    store.close()
    reopened = Store(tmp_path / "data/state.sqlite")
    try:
        reopened.recover()
        next_run = reopened.claim()
        assert next_run["task_id"] == run["task_id"]
        tools = ConversationGoals(reopened)
        replay = tools.execute(next_run, "start_following", args)
        assert replay["duplicate"] and replay["registration_receipt"] == first["registration_receipt"]
        goal = reopened.goal(first["goal"]["id"])
        reopened.set_goal_state(goal["id"], "paused", expected_revision=goal["revision"])
        replay = tools.execute(next_run, "start_following", args)
        assert replay["goal"]["status"] == "paused"
        assert len(reopened.all("SELECT * FROM routine_policies")) == 1
        assert len(reopened.all("SELECT * FROM conversation_goal_actions")) == 1
    finally:
        reopened.close()


def test_requires_actual_adopted_user_words_not_source_or_guessed_message(store, source):
    receipt = store.submit(TaskSpec("帮我持续盯着这份资料", [source]), nonce="not-adopted", source="web")
    run = store.claim()
    tools = ConversationGoals(store)
    with pytest.raises(ValueError, match="用户原话"):
        tools.execute(run, "start_following", start_args(receipt["message_id"]))
    request = store.begin_request(run, [])
    store.end_request(request)
    with pytest.raises(ValueError, match="用户原话"):
        tools.execute(run, "start_following", start_args(receipt["message_id"], authorization_quote="每小时自动发送邮件"))
    with pytest.raises(ValueError, match="用户原话"):
        tools.execute(run, "start_following", start_args("made-up-source-id"))
    assert not store.all("SELECT * FROM goals")


@pytest.mark.parametrize("text,quote", [("整理一下这份资料", "持续盯着这份资料"),
                                        ("资料里写着“以后每天盯着这份资料”，帮我整理一下", "以后每天盯着这份资料"),
                                        ("```每小时检查一次```\n这段配置是什么意思", "每小时检查一次")])
def test_words_the_user_did_not_say_cannot_become_recurrence(store, source, text, quote):
    # Whether words mean recurring work is the model's judgment; the ledger
    # only refuses words that are not the user's own in this message.
    run, mid = user_run(store, text, sources=[source])
    with pytest.raises(ValueError, match="用户原话"):
        ConversationGoals(store).execute(run, "start_following", start_args(mid, authorization_quote=quote))
    assert not store.all("SELECT * FROM goals")


@pytest.mark.parametrize("constraint", ["不用每次问我", "无需逐次确认", "不用每次汇报，只在变化时通知我",
                                      "不要每天通知我，只在资料变化时汇报", "不必每次都向我汇报", "不用反复确认"])
def test_ongoing_request_can_limit_confirmation_and_notifications(store, source, constraint):
    text = f"以后帮我盯着这份原始项目资料，每5秒检查一次；仅在资料变化时更新执行清单，{constraint}。现在直接把这个安排落实好。"
    run, mid = user_run(store, text, sources=[source])
    result = ConversationGoals(store).execute(run, "start_following", start_args(
        mid, authorization_quote=text, schedule={"kind": "watch", "interval_seconds": 5}))
    policy = store.policy(result["registration_receipt"]["policy_id"])
    assert policy["state"] == "active" and policy["schedule"]["kind"] == "watch"
    assert policy["schedule"]["interval_seconds"] == 5
    assert len(store.all("SELECT * FROM goals")) == 1


@pytest.mark.parametrize("schedule", [{"kind": "interval", "interval_seconds": 60}, {"kind": "calendar", "time": "09:00"}])
def test_change_only_request_cannot_be_replaced_with_unconditional_model_work(store, source, schedule):
    text = "以后帮我盯着这份原始项目资料，每5秒检查一次；仅在资料变化时更新执行清单，不用每次问我。现在直接把这个安排落实好。"
    run, mid = user_run(store, text, sources=[source])
    tools = ConversationGoals(store)
    args = start_args(mid, authorization_quote=text, schedule=schedule)
    result = tools.execute(run, "start_following", {**args, "schedule": {"kind": "watch", "interval_seconds": 5}})
    assert store.policy(result["registration_receipt"]["policy_id"])["schedule"]["interval_seconds"] == 5


def test_change_only_update_does_not_register_unconditional_work(store, source):
    goal = store.create_goal(room="local", actor_id="default", title="资料跟进", objective="持续跟进资料")
    policy = store.create_policy(goal["id"], schedule={"kind": "watch", "interval_seconds": 300}, template=TaskSpec("整理资料", [source]))
    text = "改为每5秒检查资料，只在变化时通知我，无需逐次确认。"
    run, mid = user_run(store, text, sources=[source])
    tools = ConversationGoals(store)
    args = {"goal_id": goal["id"], "policy_id": policy["id"], "action": "update", "schedule": {"kind": "interval", "interval_seconds": 60},
            "authorization_message_id": mid, "authorization_quote": text, "request_key": "update"}
    result = tools.execute(run, "manage_following", {**args, "schedule": {"kind": "watch", "interval_seconds": 5}})
    assert result["registration_receipt"]["revision"] == 2
    assert store.policy(policy["id"])["schedule"]["kind"] == "watch"


@pytest.mark.parametrize("source_name", ["routine", "admission", "goal_review", "runtime", "route"])
def test_generated_inputs_are_not_authorization(store, source, source_name):
    run, mid = user_run(store, sources=[source], source=source_name)
    with pytest.raises(ValueError, match="用户原话"):
        ConversationGoals(store).execute(run, "start_following", start_args(mid))


def test_source_scope_and_failed_registration_roll_back_goal(store, source, monkeypatch):
    run, mid = user_run(store, sources=[source])
    tools = ConversationGoals(store)
    with pytest.raises(ValueError, match="已授权"):
        tools.execute(run, "start_following", start_args(mid, source_id="ungranted"))
    with pytest.raises(ValueError, match="参数"):
        tools.execute(run, "start_following", {**start_args(mid), "calls_limit": 10000})
    def failed_registration(*args, **kwargs):
        raise RuntimeError("scheduler unavailable")
    monkeypatch.setattr(store, "create_policy", failed_registration)
    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        tools.execute(run, "start_following", start_args(mid))
    assert not store.all("SELECT * FROM goals") and not store.all("SELECT * FROM conversation_goal_actions")


def test_calendar_requires_real_recurring_request_and_preserves_timezone(store):
    text = "以后每周一上海时间九点提醒我整理本周计划"
    run, mid = user_run(store, text)
    result = ConversationGoals(store).execute(run, "start_following", start_args(mid, source_id=None, authorization_quote=text,
        schedule={"kind": "calendar", "time": "09:00", "weekdays": [0], "timezone": "Asia/Shanghai"}))
    policy = store.policy(result["registration_receipt"]["policy_id"])
    assert policy["schedule"]["time"] == "09:00" and policy["schedule"]["weekdays"] == [0]
    assert policy["template"]["sources"] == []
    assert policy["next_due"] > policy["created"]


def test_pause_resume_end_and_frequency_change_from_conversation(store, source):
    tools = ConversationGoals(store)
    run, mid = user_run(store, sources=[source])
    first = tools.execute(run, "start_following", start_args(mid))
    goal_id = first["goal"]["id"]
    policy_id = first["registration_receipt"]["policy_id"]
    finish(store, run)
    for action, text, expected in [("pause", "先暂停资料跟进", "paused"), ("resume", "继续跟进资料", "active")]:
        run, mid = user_run(store, text, nonce=action)
        result = tools.execute(run, "manage_following", {"goal_id": goal_id, "action": action, "authorization_message_id": mid, "authorization_quote": text, "request_key": action})
        assert result["goal"]["status"] == expected
        assert store.goal(goal_id)["calls_used"] == 0
        finish(store, run)
    text = "把这份资料改成每十分钟检查一次"
    run, mid = user_run(store, text, nonce="change")
    changed = tools.execute(run, "manage_following", {"goal_id": goal_id, "action": "update", "schedule": {"kind": "watch", "interval_seconds": 600}, "authorization_message_id": mid, "authorization_quote": text, "request_key": "change"})
    assert changed["registration_receipt"]["revision"] == 2
    assert store.policy(policy_id)["template"]["sources"] == [source]
    assert store.policy(policy_id)["schedule"]["interval_seconds"] == 600
    finish(store, run)
    text = "这件事不用再跟进了"
    run, mid = user_run(store, text, nonce="end")
    ended = tools.execute(run, "manage_following", {"goal_id": goal_id, "action": "end", "authorization_message_id": mid, "authorization_quote": text, "request_key": "end"})
    assert ended["goal"]["status"] == "retired"
    finish(store, run)
    run, mid = user_run(store, "继续之前的跟进", nonce="revive")
    with pytest.raises(ValueError, match="已结束"):
        tools.execute(run, "manage_following", {"goal_id": goal_id, "action": "resume", "authorization_message_id": mid, "authorization_quote": "继续之前的跟进", "request_key": "revive"})


@pytest.mark.parametrize("exhausted", ["calls", "tools", "both"])
def test_explicit_continue_renews_once_with_fixed_limits_and_keeps_consumption(store, source, exhausted):
    tools = ConversationGoals(store)
    initial, mid = user_run(store, sources=[source])
    first = tools.execute(initial, "start_following", start_args(mid))
    goal_id = first["goal"]["id"]
    finish(store, initial)
    calls, actions = (120 if exhausted in {"calls", "both"} else 5), (400 if exhausted in {"tools", "both"} else 9)
    store.db.execute("UPDATE goals SET calls_used=?,tools_used=?,status='needs_attention' WHERE id=?", (calls, actions, goal_id))
    text = "继续跟进这份资料"
    run, mid = user_run(store, text, nonce="continue-exhausted")
    args = {"goal_id": goal_id, "action": "resume", "authorization_message_id": mid, "authorization_quote": text, "request_key": "continue"}
    result = tools.execute(run, "manage_following", args)
    goal = store.goal(goal_id)
    assert goal["status"] == "active"
    assert goal["calls_limit"] == 140 and goal["tools_limit"] == 440
    assert goal["calls_used"] == calls and goal["tools_used"] == actions
    assert result["continuation_allowance"]["calls"] == 20
    assert result["continuation_allowance"]["tools"] == 40
    audit = json.loads(store.one("SELECT data FROM goal_history WHERE goal_id=? AND kind='conversation_resume_allowance'", (goal_id,))["data"])
    assert audit["authorization_message_id"] == mid and audit["authorization_quote"] == text
    tools.execute(run, "manage_following", {**args, "request_key": "same-intent"})
    assert store.goal(goal_id)["calls_limit"] == 140
    # Even a changed model call/quote cannot turn this one user message into
    # another allowance after the new work has exhausted its continuation.
    store.db.execute("UPDATE goals SET calls_used=calls_limit,status='needs_attention' WHERE id=?", (goal_id,))
    with pytest.raises(ValueError, match="新的继续"):
        tools.execute(run, "manage_following", {**args, "request_key": "repeat-allowance", "authorization_quote": "继续"})
    assert store.goal(goal_id)["calls_limit"] == 140


def test_continue_respects_lifetime_cap_and_requires_explicit_continue_words(store):
    goal = store.create_goal(room="local", actor_id="default", title="资料", objective="跟进资料", calls_limit=9995, tools_limit=29990)
    store.db.execute("UPDATE goals SET calls_used=9995,status='needs_attention' WHERE id=?", (goal["id"],))
    run, mid = user_run(store, "继续跟进资料", nonce="near-cap")
    tools = ConversationGoals(store)
    result = tools.execute(run, "manage_following", {"goal_id": goal["id"], "action": "resume", "authorization_message_id": mid, "authorization_quote": "继续跟进资料", "request_key": "near-cap"})
    assert result["goal"]["calls_limit"] == 10000
    assert store.goal(goal["id"])["tools_limit"] == 30000
    assert result["continuation_allowance"]["calls"] == 5 and result["continuation_allowance"]["tools"] == 10
    finish(store, run)
    store.db.execute("UPDATE goals SET calls_used=10000,status='needs_attention' WHERE id=?", (goal["id"],))
    run, mid = user_run(store, "继续跟进资料", nonce="at-cap")
    with pytest.raises(ValueError, match="总用量上限"):
        tools.execute(run, "manage_following", {"goal_id": goal["id"], "action": "resume", "authorization_message_id": mid, "authorization_quote": "继续跟进资料", "request_key": "at-cap"})
    assert store.goal(goal["id"])["calls_limit"] == store.goal(goal["id"])["calls_used"] == 10000
    finish(store, run)
    # A fabricated resume action citing an unrelated genuine message still
    # cannot increase available execution.
    run, mid = user_run(store, "现在进展怎么样", nonce="not-continue")
    with pytest.raises(ValueError, match="用户原话"):
        tools.execute(run, "manage_following", {"goal_id": goal["id"], "action": "resume", "authorization_message_id": mid, "authorization_quote": "继续跟进资料", "request_key": "invented-resume"})


def test_other_room_goals_are_invisible_and_cannot_be_controlled(store, source):
    bot = store.create_bot(name="另一位", role="独立资料助理")
    other_room = bot["private_room"]
    hidden = store.create_goal(room=other_room, actor_id=bot["id"], title="私聊事项", objective="私聊内容")
    run, mid = user_run(store, "暂停之前的资料跟进", sources=[source])
    tools = ConversationGoals(store)
    assert not tools.execute(run, "list_following", {})["following"]
    with pytest.raises(ValueError, match="当前会话"):
        tools.execute(run, "manage_following", {"goal_id": hidden["id"], "action": "pause", "authorization_message_id": mid, "authorization_quote": "暂停之前的资料跟进", "request_key": "pause"})
    assert store.goal(hidden["id"])["status"] == "active"


def test_worker_and_stale_run_cannot_create_or_manage_following(store, source):
    run, mid = user_run(store, sources=[source])
    tools = ConversationGoals(store)
    child = store.spawn_worker(run, worker_key="read", objective="持续盯资料", source_ids=["policy"])
    child_run = store.claim()
    assert child_run["task_id"] == child["task_id"]
    with pytest.raises(ValueError, match="协作者"):
        tools.execute(child_run, "start_following", start_args(mid))
    store.request_control(run["task_id"], "stop_task")
    with pytest.raises(ValueError, match="失效"):
        tools.execute(run, "start_following", start_args(mid))


def test_new_source_expansion_requires_current_task_attachment(store, source, tmp_path):
    tools = ConversationGoals(store)
    run, mid = user_run(store, sources=[source])
    first = tools.execute(run, "start_following", start_args(mid))
    finish(store, run)
    text = "以后也关注新资料"
    run, mid = user_run(store, text, nonce="expand")
    args = {"goal_id": first["goal"]["id"], "action": "update", "source_ids": ["policy", "new"], "authorization_message_id": mid, "authorization_quote": text, "request_key": "expand"}
    with pytest.raises(ValueError, match="已授权"):
        tools.execute(run, "manage_following", args)
    assert store.policy(first["registration_receipt"]["policy_id"])["revision"] == 1
    finish(store, run)
    path = tmp_path / "data/uploads/new.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("A new uploaded copy", encoding="utf-8")
    new_source = {"id": "new", "kind": "file", "location": str(path), "description": "新资料.txt"}
    run, mid = user_run(store, text, sources=[new_source], nonce="authorized-expand")
    context = tools.context(run)
    assert context["authorized_sources"][0]["tracking_scope"] == "uploaded_copy"
    assert "原文件" in context["authorized_sources"][0]["scope_note"]
    assert str(path) not in encode(context)
    result = tools.execute(run, "manage_following", {**args, "authorization_message_id": mid})
    policy = store.policy(first["registration_receipt"]["policy_id"])
    assert policy["template"]["sources"] == [source, new_source]
    assert result["registration_receipt"]["revision"] == 2


@pytest.mark.asyncio
async def test_actual_pi_registers_following_and_delivers_action_receipt_without_document(store, source, tmp_path):
    text = "帮我持续盯着这份资料，有变化就整理新的行动清单"
    submitted = store.submit(TaskSpec(text, [source], result_kind="action_receipt"), nonce="pi-following", source="web")
    def finish_action(messages, schemas):
        receipts = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        registered = next(r for r in receipts if r.get("authority_receipt"))
        return tool_reply("complete_action", {"summary": "已开始跟进这份资料，后续有变化会更新行动清单。", "effect_refs": [registered["operation_id"]]}, "complete")
    model = ScriptedModel([
        tool_reply("complete_action", {"summary": "已安排", "effect_refs": ["invented-effect"]}, "fake"),
        tool_reply("start_following", start_args(submitted["message_id"]), "register"),
        finish_action,
    ])
    run = store.claim()
    await Runtime(store, tmp_path / "data", model).execute(run)
    task = store.task(submitted["task_id"])
    assert task["status"] == "succeeded" and task["acceptance"] == "passed"
    assert task["calls_used"] == 3
    operations = store.operations(task["id"])
    assert operations[0]["tool"] == "complete_action" and operations[0]["status"] == "failed"
    assert operations[-1]["result"]["passed"] is True
    assert not (tmp_path / "data/tasks" / task["id"] / "artifacts").exists()
    delivery = store.one("SELECT * FROM deliveries WHERE task_id=?", (task["id"],))
    assert delivery["artifact"] is None and "已开始跟进" in delivery["content"]
    assert store.one("SELECT COUNT(*) n FROM goals")["n"] == 1


@pytest.mark.asyncio
async def test_default_artifact_contract_cannot_be_completed_by_following_receipt(store, source, tmp_path):
    text = "帮我持续盯着这份资料，并先交付一份现状分析报告"
    submitted = store.submit(TaskSpec(text, [source]), nonce="mixed-delivery", source="web")
    def attempt_shortcut(messages, schemas):
        receipt = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and json.loads(m["content"]).get("authority_receipt"))
        return tool_reply("complete_action", {"summary": "已经安排，所以报告也算完成", "effect_refs": [receipt["operation_id"]]}, "shortcut")
    def repair(messages, schemas):
        assert store.task(submitted["task_id"])["status"] == "running"
        assert not store.one("SELECT id FROM deliveries WHERE task_id=?", (submitted["task_id"],))
        assert store.operations(submitted["task_id"])[-1]["status"] == "failed"
        return tool_reply("read_source", {"source_id": "policy"}, "read")
    model = ScriptedModel([
        tool_reply("start_following", start_args(submitted["message_id"]), "register"),
        attempt_shortcut,
        repair,
        tool_reply("write_artifact", {"name": "analysis.md", "content": "当前发布需要人工审核，这是本次资料的现行要求。[policy]"}, "write"),
        submit_written_artifact,
    ])
    await Runtime(store, tmp_path / "data", model).execute(store.claim())
    task = store.task(submitted["task_id"])
    assert task["status"] == "succeeded" and task["calls_used"] == 5
    delivery = store.one("SELECT artifact FROM deliveries WHERE task_id=?", (task["id"],))
    assert delivery["artifact"]


def test_review_following_queues_real_evidence_task_not_a_claimed_conclusion(store):
    goal = store.create_goal(room="local", actor_id="default", title="资料跟进", objective="长期跟进资料")
    text = "帮我回顾一下这件事最近一周做得怎么样"
    run, mid = user_run(store, text, nonce="review")
    tools = ConversationGoals(store)
    args = {"goal_id": goal["id"], "authorization_message_id": mid, "authorization_quote": text, "request_key": "review"}
    result = tools.execute(run, "review_following", args)
    assert result["action"] == "review" and result["review_completed"] is False
    review = store.task(result["review_task_id"])
    assert review["status"] == "ready" and review["goal_id"] == goal["id"]
    assert review["spec"]["max_calls"] == 14 and review["spec"]["result_kind"] == "artifact"
    request = store.one("SELECT * FROM goal_review_requests WHERE task_id=?", (review["id"],))
    snapshot = store.one("SELECT * FROM review_snapshots WHERE id=?", (request["snapshot_id"],))
    assert json.loads(snapshot["payload"])["metrics"]["domain_outcome"] is None
    assert not store.all("SELECT * FROM goal_reviews")
    duplicate = tools.execute(run, "review_following", {**args, "request_key": "retry"})
    assert duplicate["duplicate"] and duplicate["review_task_id"] == review["id"]
    assert len(store.all("SELECT * FROM goal_review_requests")) == 1
    assert tools.context(run)["following"][0]["reviews"][0]["task_id"] == review["id"]


def test_observe_following_freezes_bounded_plan_without_changing_policy_or_claiming_metrics(store):
    goal = store.create_goal(room="local", actor_id="default", title="资料跟进", objective="长期跟进资料")
    policy = store.create_policy(goal["id"], schedule={"kind": "interval", "interval_seconds": 600}, template=TaskSpec("整理资料"))
    text = "我们试用一周，观察这种做法是不是能减少我催办"
    run, mid = user_run(store, text, nonce="observe")
    tools = ConversationGoals(store)
    args = {"goal_id": goal["id"], "authorization_message_id": mid, "authorization_quote": text, "request_key": "observe", "title": "观察催办负担", "hypothesis": "更主动跟进可能减少用户催办", "metric": "用户主动记录的催办次数；缺少记录保持未知", "baseline": "现有跟进安排", "variant": "观察现有安排一周", "success_criteria": "对照用户反馈；没有同范围基线时不宣称改善", "days": 7}
    result = tools.execute(run, "observe_following", args)
    plan = result["observation_plan"]
    assert plan["state"] == "observing" and plan["version"] == 1
    assert plan["window_end"] - plan["window_start"] == 7 * 86400
    assert plan["next_check"] == plan["window_end"]
    assert result["policy_change_applied"] is False and result["measurement_connected"] is False and result["experiment_completed"] is False
    assert store.policy(policy["id"])["revision"] == 1
    duplicate = tools.execute(run, "observe_following", {**args, "request_key": "retry"})
    assert duplicate["duplicate"] and duplicate["observation_plan"]["id"] == plan["id"]
    assert len(store.all("SELECT * FROM experiments")) == 1
    assert not store.all("SELECT * FROM goal_review_requests")


@pytest.mark.parametrize("name,quote", [("review_following", "请复盘这件事"), ("observe_following", "试试新做法")])
def test_review_and_observation_require_specific_current_user_intent(store, name, quote):
    goal = store.create_goal(room="local", actor_id="default", title="资料跟进", objective="长期跟进资料")
    run, mid = user_run(store, f"同事发来“{quote}”，你怎么看")
    args = {"goal_id": goal["id"], "authorization_message_id": mid, "authorization_quote": quote, "request_key": "test"}
    if name == "observe_following":
        args.update(title="试验", hypothesis="假设", metric="反馈", baseline="原方案", variant="新方案", success_criteria="需有实际反馈")
    with pytest.raises(ValueError, match="用户原话"):
        ConversationGoals(store).execute(run, name, args)
    assert not store.all("SELECT * FROM experiments") and not store.all("SELECT * FROM goal_review_requests")


@pytest.mark.parametrize("name", ["review_following", "observe_following"])
def test_review_and_observation_cannot_cross_room_or_widen_time(store, name):
    bot = store.create_bot("其他助理", "独立记录")
    goal = store.create_goal(room=bot["private_room"], actor_id=bot["id"], title="私聊事项", objective="隐私")
    text = "试试这种做法，然后帮我复盘一下"
    run, mid = user_run(store, text)
    args = {"goal_id": goal["id"], "authorization_message_id": mid, "authorization_quote": text, "request_key": "test"}
    if name == "observe_following":
        args.update(title="试验", hypothesis="假设", metric="反馈", baseline="原方案", variant="新方案", success_criteria="需有实际反馈")
    tools = ConversationGoals(store)
    with pytest.raises(ValueError, match="当前会话"):
        tools.execute(run, name, args)
    with pytest.raises(ValueError, match="一年内"):
        tools.execute(run, name, {**args, "days": float("nan")})
    assert not store.all("SELECT * FROM experiments") and not store.all("SELECT * FROM goal_review_requests")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "retired", "achieved"])
async def test_actual_pi_can_review_paused_or_ended_history_without_resuming_business(store, tmp_path, state):
    goal = store.create_goal(room="local", actor_id="default", title="历史资料", objective="长期跟进资料")
    policy = store.create_policy(goal["id"], schedule={"kind": "interval", "interval_seconds": 60}, template=TaskSpec("整理资料"), now=1000)
    store.set_goal_state(goal["id"], state, expected_revision=goal["revision"])
    before = store.goal(goal["id"])
    text = "帮我回顾这件事之前做得怎么样"
    run, mid = user_run(store, text, nonce="review-paused")
    registered = ConversationGoals(store).execute(run, "review_following", {"goal_id": goal["id"], "authorization_message_id": mid, "authorization_quote": text, "request_key": "review-paused"})
    assert store.goal(goal["id"])["status"] == state
    finish(store, run)
    review_run = store.claim()
    assert review_run["task_id"] == registered["review_task_id"]
    assert store.current(review_run)
    with pytest.raises(ValueError, match="历史回顾"):
        store.record_goal_progress(review_run, current_state="changed", gaps=[], unknowns=[], next_action="resume", evidence_refs=[])
    with pytest.raises(ValueError, match="历史回顾"):
        store.propose_policy_adjustment(review_run, policy_id=policy["id"], reason="change", evidence_refs=[], expected_cost="same", interval_seconds=120)
    with pytest.raises(ValueError, match="历史回顾"):
        store.review_policy_proposal(review_run, proposal_id="fake", accept=True, reason="change")
    model = ScriptedModel([
        tool_reply("read_review_evidence", {"evidence_id": "metrics"}, "metrics"),
        tool_reply("read_review_evidence", {"evidence_id": "goal_state"}, "state"),
        tool_reply("write_artifact", {"name": "history-review.md", "content": "本次仅回顾已有记录，业务效果未测量，不能证明改善；继续保留未知。[metrics] [goal_state]"}, "write"),
        submit_written_artifact,
    ])
    await Runtime(store, tmp_path / "data", model).execute(review_run)
    reviewed = store.task(registered["review_task_id"])
    assert reviewed["status"] == "succeeded"
    after = store.goal(goal["id"])
    assert after["status"] == state and after["revision"] == before["revision"]
    assert after["current_state"] == before["current_state"] and after["wait_reason"] == before["wait_reason"]
    assert after["calls_used"] - before["calls_used"] == 4
    await TriggerScheduler(store, tmp_path / "data", clock=lambda: 2000).tick()
    assert not store.all("SELECT * FROM policy_occurrences")
    assert store.policy(policy["id"])["revision"] == 1
    assert not store.all("SELECT * FROM goal_reviews")
    assert {r["evidence_id"] for r in store.all("SELECT evidence_id FROM review_evidence_reads WHERE task_id=?", (reviewed["id"],))} == {"metrics", "goal_state"}


@pytest.mark.parametrize("state", ["paused", "retired"])
def test_fresh_stop_revokes_review_grant_at_old_revision_and_budget_keeps_stopped_state(store, state):
    goal = store.create_goal(room="local", actor_id="default", title="历史", objective="保留历史", calls_limit=1, tools_limit=10)
    stopped = store.set_goal_state(goal["id"], state, expected_revision=goal["revision"])
    receipt = store.request_goal_review(goal["id"], nonce="fresh-history")
    run = store.claim()
    assert run["task_id"] == receipt["task_id"]
    assert store.reserve(run, "model") is True
    assert store.reserve(run, "model") is False
    assert store.goal(goal["id"])["status"] == state
    assert store.goal(goal["id"])["calls_used"] == 1
    changed = store.set_goal_state(goal["id"], state, expected_revision=stopped["revision"])
    assert changed["revision"] == stopped["revision"] + 1
    assert not store.current(run)
    assert store.goal_gate(store.task(receipt["task_id"])) == "goal_revision_changed"


def test_paused_goal_does_not_allow_ordinary_task_without_explicit_review_grant(store):
    goal = store.create_goal(room="local", actor_id="default", title="暂停事项", objective="普通业务")
    stopped = store.set_goal_state(goal["id"], "paused", expected_revision=goal["revision"])
    task = store.submit(TaskSpec("继续处理业务"), nonce="ordinary", source="routine")
    store.db.execute("UPDATE tasks SET goal_id=?,goal_revision=? WHERE id=?", (goal["id"], stopped["revision"], task["task_id"]))
    assert store.goal_gate(store.task(task["task_id"])) == "goal_paused"
    assert store.claim() is None
    assert store.task(task["task_id"])["status"] == "paused"


@pytest.mark.parametrize("change", ["correct", "delete"])
def test_memory_changes_remove_old_following_assumptions_from_context_and_receipt_replay(store, source, change):
    secret = "不可再回流的旧判断"
    memory = store.save_memory("local", key="关注事项", value=secret, purpose="帮助整理资料")
    goal = store.create_goal(room="local", actor_id="default", title="资料跟进", objective="继续跟进明确授权的资料")
    policy = store.create_policy(goal["id"], schedule={"kind": "interval", "interval_seconds": 600},
                                 template=TaskSpec(secret, [{**source, "description": secret}]))
    store.db.execute("UPDATE goals SET current_state=?,next_action=?,wait_reason=?,assessment_at=? WHERE id=?", (secret, secret, secret, time.time(), goal["id"]))
    text = "试用一周后看看效果"
    run, mid = user_run(store, text, nonce="old-observe")
    tools = ConversationGoals(store)
    plan_args = {"goal_id": goal["id"], "authorization_message_id": mid, "authorization_quote": text, "request_key": "observe",
                 **{key: secret for key in ["title", "hypothesis", "metric", "baseline", "variant", "success_criteria"]}}
    result = tools.execute(run, "observe_following", plan_args)
    cached = store.one("SELECT * FROM conversation_goal_actions WHERE task_id=?", (run["task_id"],))
    assert secret in encode(tools.context(run))
    finish(store, run)
    if change == "correct":
        store.save_memory("local", key="关注事项", value="新的关注信息", purpose="帮助整理资料", expected_version=memory["version"])
    else:
        store.delete_memory("local", memory["id"], expected_version=memory["version"])
    run, _ = user_run(store, "看看目前安排了什么", nonce="ordinary-after-memory")
    assert store.task(run["task_id"])["goal_id"] is None
    context = tools.context(run)
    assert secret not in encode(context)
    public = context["following"][0]
    assert public["objective"] == goal["objective"] and public["status"] == "active"
    assert public["policies"][0]["schedule"] == policy["schedule"]
    assert public["policies"][0]["source_ids"] == [source["id"]]
    assert public["policies"][0]["registration_receipt"] == policy["registration_receipt"]
    plan = public["observation_plans"][0]
    assert plan["id"] == result["observation_plan"]["id"] and plan["next_check"] == result["observation_plan"]["next_check"]
    assert "hypothesis" not in plan and "title" not in plan
    assert secret not in encode(tools._replay(cached))
    # Audit records are retained. A later scheduling edit does not reauthorize
    # their obsolete hypotheses or cause an old assumption to be loaded again.
    assert store.experiment(plan["id"])["hypothesis"] == secret
    store.revise_experiment(plan["id"], expected_version=1, reason="调整提醒时间", next_check=time.time() + 86400)
    assert secret not in encode(tools.context(run))
    finish(store, run)
    review = store.request_goal_review(goal["id"], nonce="review-after-memory", experiment_id=plan["id"])
    review_run = store.claim()
    assert review_run["task_id"] == review["task_id"]
    assert secret not in encode(store.review_context(review_run))
    assert secret not in encode(store.goal_context(review_run))
    # A genuinely fresh assessment can appear; the room floor is not an
    # indefinite blanket removal of all future follow-up progress.
    store.db.execute("UPDATE goals SET current_state=?,next_action=?,wait_reason=?,assessment_at=? WHERE id=?", ("已依据新资料检查", "等待新情况", "按约定继续", time.time(), goal["id"]))
    assert tools.context(review_run)["following"][0]["current_state"] == "已依据新资料检查"


def test_following_created_after_memory_correction_keeps_its_fresh_registration_status(store, source):
    memory = store.save_memory("local", key="旧记忆", value="已失效", purpose="整理资料")
    store.delete_memory("local", memory["id"], expected_version=memory["version"])
    run, mid = user_run(store, sources=[source])
    result = ConversationGoals(store).execute(run, "start_following", start_args(mid))
    assert result["goal"]["current_state"] == "还没有首次跟进结果"
    assert result["goal"]["wait_reason"] == "已安排后续跟进，等待约定时间或资料变化"
