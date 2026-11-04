"""M3 audience, identity and explicit-memory scenarios, not quality claims."""
import asyncio
import json
import time

import pytest

from personal_agent.contracts import ModelReply, TaskSpec, encode
from personal_agent.memory_store import SqlMemoryProvider
from personal_agent.orchestration_tools import OrchestrationTools
from personal_agent.runtime import Runtime
from personal_agent.service import Service
from personal_agent.store import Store
from personal_agent.tools import Toolset
from conftest import ScriptedModel, tool_reply, submit_written_artifact
from test_webui import client_for


def team(store):
    a = store.create_bot("资料助手", "只基于实际资料整理事实")
    b = store.create_bot("复核助手", "独立核对证据与遗漏")
    g = store.create_group("资料协作", [a["id"], b["id"]])
    return a, b, g


def begin(store, actor="default", room="local", text="Prepare a useful result", nonce="root"):
    tid = store.submit(TaskSpec(text, max_calls=30, max_tools=70), nonce=nonce, actor=actor, room=room)["task_id"]
    run = store.claim()
    assert run["task_id"] == tid
    return run


def memory_context(messages):
    return json.loads(next(m["content"].split(": ", 1)[1] for m in messages if m["content"].startswith("Current room context")))


def test_t13_mentions_reply_and_ambiguous_route_survive_restart(store, tmp_path):
    a, b, group = team(store)
    room = group["id"]
    text = "@复核助手 请核对要求"
    first = store.route_input(TaskSpec(text), text=text, mode="task", nonce="mention", room=room)
    assert store.task(first["task_id"])["actor_id"] == b["id"]
    changed_default = TaskSpec(text, enable_nudge=True, review_workers=True)
    retry = store.route_input(changed_default, text=text, mode="task", nonce="mention", room=room)
    assert retry["task_id"] == first["task_id"] and retry["duplicate"]
    addition = store.route_input(None, text="另加例外情况", mode="followup", nonce="reply", room=room, reply_to=first["message_id"])
    assert addition["task_id"] == first["task_id"]
    assert store.task(first["task_id"])["requirement_revision"] == 2
    ambiguous = store.route_input(TaskSpec("Who owns this?"), text="Who owns this?", mode="task", nonce="ambiguous", room=room)
    assert ambiguous["state"] == "unassigned"
    assert len(store.all("SELECT * FROM tasks")) == 1
    store.close()
    store = Store(tmp_path / "data/state.sqlite")
    try:
        receipt = store.assign_route(ambiguous["route_id"], actor_id=a["id"])
        duplicate = store.assign_route(ambiguous["route_id"], actor_id=a["id"])
        assert duplicate["duplicate"] and receipt["task_id"] == duplicate["task_id"]
        assert store.one("SELECT COUNT(*) n FROM messages WHERE client_nonce='ambiguous'")["n"] == 1
        assert store.task(receipt["task_id"])["actor_id"] == a["id"]
    finally:
        store.close()


def test_group_ack_and_unaddressed_chat_are_background_without_response_storm(store):
    a, b, group = team(store)
    for i, text in enumerate(["谢谢", "本群使用周三的检查表"]):
        receipt = store.route_input(None, text=text, mode="chat", nonce=str(i), room=group["id"])
        assert receipt["state"] == "context"
    assert not store.all("SELECT * FROM events") and not store.all("SELECT * FROM deliveries")
    run = begin(store, a["id"], group["id"])
    assert "周三" in encode(store.room_context(run))
    assert len(store.all("SELECT * FROM tasks")) == 1


@pytest.mark.asyncio
async def test_t14_private_sources_artifacts_and_memory_never_enter_group_or_other_bot(store, tmp_path):
    a, b, group = team(store)
    store.save_memory(a["private_room"], key="个人偏好", value="PRIVATE_FACT_731", purpose="只给我的资料助手")
    store.save_memory(group["id"], key="协作规则", value="GROUP_FACT_652", purpose="群内检查")
    private = begin(store, a["id"], a["private_room"], nonce="private")
    rt = Runtime(store, tmp_path / "data", ScriptedModel([]))
    assert "PRIVATE_FACT_731" in encode(rt.context(private))
    store.finish(private, status="succeeded", reason="test", content="private output")
    group_run = begin(store, a["id"], group["id"], nonce="group")
    request = encode(rt.context(group_run))
    assert "GROUP_FACT_652" in request and "PRIVATE_FACT_731" not in request
    tools = OrchestrationTools(store, tmp_path / "data")
    with pytest.raises(ValueError):
        tools.execute(group_run, "read_memory", {"collection_id": a["private_room"]})
    with pytest.raises(ValueError):
        tools.execute(group_run, "read_worker_result", {"child_id": private["task_id"]})
    with pytest.raises(ValueError):
        await Toolset(tmp_path / "data", store.task(group_run["task_id"])).read_source("PRIVATE_FACT_731")
    with pytest.raises(ValueError):
        store.spawn_worker(group_run, worker_key="foreign", objective="Get private facts", source_ids=[], target_actor_id="default")
    store.finish(group_run, status="succeeded", reason="test", content="group output")
    other = begin(store, b["id"], b["private_room"], nonce="other")
    assert "PRIVATE_FACT_731" not in encode(rt.context(other)) and "GROUP_FACT_652" not in encode(rt.context(other))


def test_t15_copy_and_team_template_include_configuration_only(store):
    a, b, group = team(store)
    store.save_memory(a["private_room"], key="独有背景", value="DO_NOT_COPY", purpose="本 Bot 私有")
    store.submit(None, text="DO_NOT_COPY_CHAT", nonce="chat", actor=a["id"], room=a["private_room"])
    copied = store.copy_bot(a["id"])
    assert copied["role"] == a["role"] and copied["private_room"] != a["private_room"]
    assert not store.memory_items(copied["private_room"])
    assert not store.all("SELECT * FROM messages WHERE room_id=?", (copied["private_room"],))
    template = store.save_team_template(group["id"], "检查团队")
    new_group = store.instantiate_team(template["id"], name="另一套资料")
    new_members = store.members(new_group["id"])
    assert {m["id"] for m in new_members}.isdisjoint({a["id"], b["id"]})
    assert {m["role"] for m in new_members} == {a["role"], b["role"]}
    assert template["configuration"]["policies_active"] is False
    assert not store.all("SELECT * FROM tasks WHERE room_id=?", (new_group["id"],))
    assert not store.memory_items(new_group["id"])


def test_member_exit_fences_callbacks_and_preserves_work_for_explicit_reassignment(store):
    a, b, group = team(store)
    run = begin(store, a["id"], group["id"])
    tid = run["task_id"]
    store.reserve(run, "model")
    group = store.owned_room(group["id"])
    store.update_group(group["id"], name=group["name"], member_ids=[b["id"]], expected_revision=group["revision"])
    assert not store.current(run) and store.control_reason(run) == "scope_changed"
    with pytest.raises(RuntimeError):
        store.finish(run, status="succeeded", reason="stale", content="Must not publish")
    with pytest.raises(ValueError):
        store.reassign_task(tid, b["id"])
    store.ack_control(run, "scope_changed")
    assert store.task(tid)["status"] == "paused"
    assert store.claim() is None
    with pytest.raises(ValueError):
        store.resume(tid)
    store.reassign_task(tid, b["id"])
    new = store.claim()
    assert new["actor_id"] == b["id"] and new["task_id"] == tid
    assert store.task(tid)["calls_used"] == 1
    assert store.task(tid)["spec"]["objective"] == "Prepare a useful result"


@pytest.mark.asyncio
async def test_busy_member_revocation_reaps_actual_pi_before_new_owner_can_start(store, tmp_path):
    a, b, group = team(store)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    class SlowModel:
        async def complete(self, messages, tools):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    tid = store.submit(TaskSpec("Long operation"), nonce="busy", actor=a["id"], room=group["id"])["task_id"]
    service = Service(store, tmp_path / "data", SlowModel())
    scheduler = asyncio.create_task(service.run_until_quiescent())
    await asyncio.wait_for(entered.wait(), 5)
    store.update_group(group["id"], name="只留复核", member_ids=[b["id"]], expected_revision=group["revision"])
    await asyncio.wait_for(scheduler, 5)
    assert cancelled.is_set() and not service._active
    assert not store.all("SELECT * FROM runs WHERE phase='running'")
    assert store.task(tid)["status"] == "paused"
    assert not store.all("SELECT * FROM deliveries WHERE content='Must not publish'")


def test_t22_memory_states_correction_delete_and_stale_provider_do_not_resurrect(store, tmp_path):
    first = store.save_memory("local", key="时段", value="OLD_PRIVATE_VALUE", purpose="排日程")
    run = begin(store)
    assert store.room_context(run)["memory"]["state"] == "not_loaded"
    loaded = store.read_memory(run, keys=["时段", "未存过"])
    assert loaded["items"][1]["state"] == "absent"
    assert loaded["items"][0]["version"] == 1
    store.save_memory("local", key="时段", value="NEW_VALUE", purpose="排日程", expected_version=1)
    assert not store.current(run)
    store.ack_control(run, "scope_changed")
    run = store.claim()
    assert "NEW_VALUE" in encode(store.read_memory(run))
    with pytest.raises(ValueError):
        store.save_memory("local", key="时段", value="STALE", purpose="排日程", expected_version=1)
    store.delete_memory("local", first["id"], expected_version=2)
    store.ack_control(run, "scope_changed")
    run = store.claim()
    deleted = store.read_memory(run, keys=["时段"])
    assert deleted["items"][0]["state"] == "deleted" and deleted["items"][0]["value"] is None
    class StaleProvider:
        def load(self, collection_id, records):
            return [{"id": first["id"], "version": 1, "value": "OLD_PRIVATE_VALUE"}]
    store.memory_provider = StaleProvider()
    failed = store.read_memory(run, keys=["时段"])
    assert failed["state"] == "failed" and "OLD_PRIVATE_VALUE" not in encode(failed)
    with pytest.raises(ValueError):
        store.save_memory("local", key="时段", value="Recreated implicitly", purpose="排日程", expected_version=3)
    assert store.one("SELECT state,value FROM memories WHERE id=?", (first["id"],)) == {"state": "deleted", "value": None}
    store.memory_provider = SqlMemoryProvider(store)
    store.close()
    store = Store(tmp_path / "data/state.sqlite")
    try:
        store.recover()
        fresh = store.claim()
        messages = Runtime(store, tmp_path / "data", ScriptedModel([])).context(fresh)
        assert "OLD_PRIVATE_VALUE" not in encode(messages) and "NEW_VALUE" not in encode(messages)
        store.save_memory("local", key="时段", value="Explicit restore", purpose="排日程", expected_version=3, restore=True)
        assert store.memory_items("local")[0]["version"] == 4
    finally:
        store.close()


def test_memory_provider_replacement_preserves_ledger_acl_and_version_authority(store):
    a, b, group = team(store)
    record = store.save_memory(group["id"], key="协作时段", value="Wednesday", purpose="安排检查")
    private = store.save_memory(a["private_room"], key="个人", value="PRIVATE", purpose="private")
    run = begin(store, b["id"], group["id"])
    class GoodProvider:
        def load(self, collection_id, records):
            assert collection_id == group["id"] and records == [{"id": record["id"], "version": 1}]
            return [{"id": record["id"], "version": 1, "value": "Wednesday"}]
    store.memory_provider = GoodProvider()
    assert store.read_memory(run)["items"][0]["value"] == "Wednesday"
    class ForgedProvider:
        def load(self, collection_id, records):
            return [{"id": private["id"], "version": 1, "value": "PRIVATE"}]
    store.memory_provider = ForgedProvider()
    assert store.read_memory(run)["state"] == "failed"
    assert store.memory_items(a["private_room"])[0]["value"] == "PRIVATE"


def test_memory_suggestions_need_user_confirmation_and_original_scope(store):
    a, b, group = team(store)
    run = begin(store, a["id"], group["id"])
    original = store.one("SELECT id FROM messages WHERE task_id=? AND kind='user'", (run["task_id"],))
    proposal = store.propose_memory(run, key="格式", value="只在这次输出表格", purpose="格式背景", source_ref=original["id"])
    assert not store.memory_items(group["id"]) and not proposal["stored_as_memory"]
    rejected = store.decide_memory(proposal["proposal_id"], accept=False)
    assert not rejected["stored_as_memory"] and not store.memory_items(group["id"])
    proposal = store.propose_memory(run, key="周会", value="每周三", purpose="安排周会", source_ref=original["id"])
    store.update_group(group["id"], name="changed", member_ids=[b["id"]], expected_revision=group["revision"])
    assert store.decide_memory(proposal["proposal_id"], accept=True)["state"] == "superseded"
    assert not store.memory_items(group["id"])


def test_ingress_and_nested_mutations_roll_back_as_one_operation(store, monkeypatch):
    def crash(*args):
        raise RuntimeError("failure between task insert and ingress receipt")
    monkeypatch.setattr(store, "_submitted", crash)
    with pytest.raises(RuntimeError):
        store.route_input(TaskSpec("Durable input"), text="Durable input", nonce="atomic", mode="task")
    assert not store.all("SELECT * FROM tasks")
    assert not store.all("SELECT * FROM messages")
    assert not store.all("SELECT * FROM ingress_receipts")


@pytest.mark.asyncio
async def test_web_bot_group_memory_copy_and_cross_room_guessed_ids(store, tmp_path):
    async with client_for(store, tmp_path) as client:
        bot = await (await client.post("/api/manage/create_bot", json={"name": "新助理", "role": "读资料"})).json()
        saved = await (await client.post("/api/manage/save_memory", json={"room": bot["private_room"], "key": "秘密", "value": "PRIVATE_UI", "purpose": "本私聊"})).json()
        group = await (await client.post("/api/manage/create_group", json={"name": "群聊", "members": [bot["id"], "default"]})).json()
        group_state = await (await client.get("/api/state?room=" + group["id"])).json()
        assert group_state["memories"] == [] and "PRIVATE_UI" not in encode(group_state)
        forged = await client.post("/api/manage/delete_memory", json={"room": group["id"], "id": saved["id"], "version": 1})
        assert forged.status == 400
        private_task = await (await client.post("/api/messages", json={"room": bot["private_room"], "text": "Private task", "nonce": "private"})).json()
        bad_reply = await client.post("/api/messages", json={"room": group["id"], "text": "Bad reply", "nonce": "forged", "mode": "followup", "reply_to": private_task["message_id"]})
        assert bad_reply.status == 400
        copied = await (await client.post("/api/manage/copy_bot", json={"id": bot["id"]})).json()
        copied_state = await (await client.get("/api/state?room=" + copied["private_room"])).json()
        assert not copied_state["messages"] and not copied_state["memories"]


@pytest.mark.asyncio
async def test_persistent_peer_delegation_uses_real_pi_and_one_shared_budget(store, tmp_path):
    a, b, group = team(store)
    class PeerModel:
        async def complete(self, messages, tools):
            scope = memory_context(messages)
            assert "PRIVATE_" not in encode(messages)
            ctx = json.loads(next(m["content"].split(": ", 1)[1] for m in messages if m["content"].startswith("Current orchestration context")))
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if scope["self"]["id"] == a["id"]:
                if not ctx["children"] and not results:
                    return tool_reply("spawn_worker", {"worker_key": "review", "objective": "Independent scoped check", "source_ids": [], "target_actor_id": b["id"]})
                if any("duplicate" in r for r in results):
                    return tool_reply("wait_workers", {})
                if not any("artifact_text" in r for r in results):
                    return tool_reply("read_worker_result", {"child_id": ctx["children"][0]["id"]})
            if not any("artifact_ref" in r for r in results):
                return tool_reply("write_artifact", {"name": "result.md", "content": scope["self"]["name"] + " completed scoped work."})
            return submit_written_artifact(messages, tools)
    store.save_memory(a["private_room"], key="隐藏背景", value="PRIVATE_A", purpose="私聊")
    store.save_memory(b["private_room"], key="隐藏背景", value="PRIVATE_B", purpose="私聊")
    tid = store.submit(TaskSpec("Work as peers", max_calls=20, max_tools=40), nonce="peers", actor=a["id"], room=group["id"])["task_id"]
    await Service(store, tmp_path / "data", PeerModel()).serve(once=True)
    assert store.task(tid)["status"] == "succeeded"
    child = store.one("SELECT id,actor_id FROM tasks WHERE parent_task_id=?", (tid,))
    assert child["actor_id"] == b["id"] and store.task(child["id"])["status"] == "succeeded"
    assert store.task(tid)["calls_used"] == store.one("SELECT COUNT(*) n FROM model_requests")["n"]
    assert {d["room_id"] for d in store.all("SELECT room_id FROM deliveries")} == {group["id"]}


@pytest.mark.asyncio
async def test_chat_continuity_uses_both_speakers_and_forget_fences_old_context(store, tmp_path):
    memory = store.save_memory("local", key="私聊事实", value="OLD_MEMORY_CONTEXT", purpose="用于聊天")
    first_model = ScriptedModel([ModelReply({"role": "assistant", "content": "ASSISTANT_ONLY_DETAIL about OLD_MEMORY_CONTEXT"})])
    store.submit(None, text="USER_ONLY_DETAIL", nonce="first-chat")
    await Service(store, tmp_path / "data", first_model).serve(once=True)
    store.submit(None, text="Continue that discussion", nonce="next-chat")
    run = store.claim()
    messages = Runtime(store, tmp_path / "data", ScriptedModel([])).context(run)
    assert "ASSISTANT_ONLY_DETAIL" in encode(messages) and "USER_ONLY_DETAIL" in encode(messages)
    assert json.loads(store.one("SELECT context_manifest FROM runs WHERE id=?", (run["id"],))["context_manifest"])["room"] == "local"
    store.delete_memory("local", memory["id"], expected_version=1)
    assert not store.current(run)
    store.ack_control(run, "scope_changed")
    fresh = store.claim()
    messages = Runtime(store, tmp_path / "data", ScriptedModel([])).context(fresh)
    assert "OLD_MEMORY_CONTEXT" not in encode(messages) and "ASSISTANT_ONLY_DETAIL" not in encode(messages)
    assert messages[-1]["content"] == "Continue that discussion"


def test_ambiguous_followup_assignment_keeps_one_original_message_identity(store):
    a, b, group = team(store)
    first = store.submit(TaskSpec("First task"), nonce="first", actor=a["id"], room=group["id"])
    store.submit(TaskSpec("Another task"), nonce="second", actor=a["id"], room=group["id"])
    pending = store.route_input(None, text="还要保留日期", nonce="unbound", mode="followup", room=group["id"], actor_id=a["id"])
    assert pending["state"] == "unassigned"
    assigned = store.assign_route(pending["route_id"], task_id=first["task_id"])
    again = store.assign_route(pending["route_id"], task_id=first["task_id"])
    assert assigned["message_id"] == pending["message_id"] and again["duplicate"]
    assert len(store.all("SELECT * FROM messages WHERE content='还要保留日期'")) == 1
    assert store.one("SELECT owner_task FROM followups WHERE message_id=?", (pending["message_id"],))["owner_task"] == first["task_id"]


def test_memory_proposal_accepts_only_explicit_user_choice(store):
    run = begin(store)
    message = store.one("SELECT id FROM messages WHERE task_id=? AND kind='user'", (run["task_id"],))
    proposal = store.propose_memory(run, key="偏好", value="简短清单", purpose="平常的输出格式", source_ref=message["id"])
    assert not store.memory_items("local")
    result = store.decide_memory(proposal["proposal_id"], accept=True)
    assert result["stored_as_memory"]
    stored = store.memory_items("local")[0]
    assert stored["source_type"] == "user_confirmed_proposal" and stored["source_ref"] == message["id"]
    assert stored["speaker"] == "local-user" and stored["value"] == "简短清单"


def test_legacy_ui_nonce_replay_ignores_changed_implicit_runtime_defaults(store):
    old = store.submit(TaskSpec("Old UI task", max_calls=20), nonce="old-ui", source="web")
    new_defaults = TaskSpec("Old UI task", max_calls=40, enable_nudge=True, review_workers=True)
    replay = store.route_input(new_defaults, text="Old UI task", nonce="old-ui", mode="task")
    assert replay["duplicate"] and replay["task_id"] == old["task_id"]
    assert store.task(old["task_id"])["spec"]["max_calls"] == 20
    assert not store.task(old["task_id"])["spec"]["enable_nudge"]
    with pytest.raises(ValueError):
        store.route_input(TaskSpec("Different input"), text="Different input", nonce="old-ui", mode="task")
