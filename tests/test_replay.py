"""Ledger replay: one task's walk through the ledger, read back in order, never written."""
import json

from personal_agent.contracts import encode
import sqlite3

import pytest

from personal_agent.commitment_store import due_label
from personal_agent.runtime import Runtime
from personal_agent.store import Store
from conftest import ScriptedModel, tool_reply
from test_commitments import deliver_all, incoming, intake
from test_webui import client_for

STATIONS = ["intake", "commit", "start", "model", "tool", "deliver", "fulfil"]


def first_seen(events):
    order = []
    for e in events:
        if e["station"] not in order:
            order.append(e["station"])
    return order


@pytest.mark.asyncio
async def test_a_fulfilled_task_replays_all_seven_stations_in_order_without_writing(store, source, tmp_path, successful_model):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理，做好发你。"}, nonce="f", sources=[source])
    await Runtime(store, tmp_path / "data", successful_model).execute(store.claim())
    deliver_all(store)
    before = store.db.total_changes
    timeline = store.task_timeline(result["task_id"])
    assert store.db.total_changes == before
    assert [s["id"] for s in timeline["stations"]] == STATIONS
    assert first_seen(timeline["events"]) == STATIONS
    assert timeline["outcome"] == "fulfilled"
    at = [e["at"] for e in timeline["events"]]
    assert at == sorted(at)
    intake_event = next(e for e in timeline["events"] if e["station"] == "intake")
    assert "帮我整理这份资料" in intake_event["text"]
    registered = next(e for e in timeline["events"] if e["kind"] == "registered")
    assert registered["text"] == "好，我去整理，做好发你。"
    model = [e for e in timeline["events"] if e["station"] == "model"]
    assert len(model) == 3 and dict(model[0]["facts"])["模型"] == "scripted-test-model"
    assert dict(model[0]["facts"])["输入 token"] == "30"
    tools = [dict(e["facts"])["工具"] for e in timeline["events"] if e["station"] == "tool"]
    assert tools == ["read_source", "write_artifact", "submit_result"]
    assert all(e["end_time"] == "known" for e in timeline["events"] if e["station"] == "tool")
    delivered = next(e for e in timeline["events"] if e["station"] == "deliver" and e["kind"] == "delivered")
    artifact = json.loads(store.one("SELECT artifact FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (result["task_id"],))["artifact"])
    assert dict(delivered["facts"])["成果校验值"] == artifact["sha256"]
    fulfilled = timeline["events"][-1]
    assert fulfilled["kind"] == "fulfilled" and fulfilled["tone"] == "ok"
    assert {"label": "查看成果", "url": "/r/" + result["task_id"]} in fulfilled["links"]
    assert timeline["unknown_times"] == 0


def test_overdue_branch_replays_the_reminder_the_user_actually_got(store, source):
    result = intake(store, "帮我整理这份资料，今晚六点前给我", {"action": "new_task", "receipt": "好，六点前整理好发你。", "due_at": "2099-01-01 18:00", "due_basis": "今晚六点前"}, nonce="d1", sources=[source])
    c = store.one("SELECT * FROM commitments")
    store.tick_commitments(now=c["due_at"] + 60)
    timeline = store.task_timeline(result["task_id"])
    assert timeline["outcome"] == "overdue"
    registered = next(e for e in timeline["events"] if e["kind"] == "registered")
    assert ["时限", due_label(c["due_at"]) + "（你说的：今晚六点前）"] in registered["facts"]
    told = timeline["events"][-1]
    sent = store.one("SELECT content FROM deliveries WHERE kind='commitment'")["content"]
    assert told["kind"] == "overdue_told" and told["tone"] == "bad" and told["station"] == "fulfil"
    assert told["text"] == sent and "还没做完" in sent


def test_refused_promise_and_reschedule_are_shown_as_branches(store, source):
    incoming(store, "帮我整理这份资料，今晚六点前给我", nonce="p", sources=[source])
    run = store.claim_conversation()
    store.conversation_context(run)
    with pytest.raises(ValueError):
        store.apply_conversation(run, {"action": "reply", "text": "我稍后整理好发你。", "promise": "我稍后整理好发你"})
    result = store.apply_conversation(run, {"action": "new_task", "receipt": "好，六点前发你。", "due_at": "2099-01-01 18:00", "due_basis": "今晚六点前"})
    c = store.one("SELECT * FROM commitments")
    incoming(store, "改到后天中午吧", nonce="p2")
    run = store.claim_conversation()
    store.conversation_context(run)
    store.apply_conversation(run, {"action": "commitment", "commitment_id": c["id"], "commitment_op": "reschedule", "due_at": "2099-01-03 12:00", "basis": "改到后天中午吧"})
    events = store.task_timeline(result["task_id"])["events"]
    refused = next(e for e in events if e["kind"] == "unregistered_refused")
    assert refused["station"] == "intake" and refused["tone"] == "bad" and refused["text"] == "我稍后整理好发你"
    superseded = next(e for e in events if e["kind"] == "superseded")
    assert superseded["tone"] == "warn" and "改到后天中午吧" in superseded["text"]
    assert [e["kind"] for e in events].count("registered") == 2


def test_unknown_times_stay_unknown_and_are_never_invented(store, source):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="u", sources=[source])
    run = store.claim()
    store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,created,ended) VALUES('old',?,?,'read_source','{}','succeeded',100,NULL)", (result["task_id"], run["id"]))
    store.db.execute("INSERT INTO deliveries(id,task_id,run_id,room_id,content,created) VALUES('legacy',?,'legacy-run','local','旧交付',0)", (result["task_id"],))
    timeline = store.task_timeline(result["task_id"])
    tool = next(e for e in timeline["events"] if e["station"] == "tool")
    assert tool["end"] is None and tool["end_time"] == "unknown"
    legacy = next(e for e in timeline["events"] if e["text"] == "旧交付")
    assert legacy["at"] is None and legacy["time"] == "unknown" and legacy["order"] == "inferred"
    assert timeline["unknown_times"] >= 2


def test_upgrade_to_v11_backs_up_first_and_adds_tool_end_times(tmp_path, source):
    path = tmp_path / "data" / "state.sqlite"
    store = Store(path)
    store.db.execute("ALTER TABLE operations DROP COLUMN ended")
    store.db.execute("PRAGMA user_version=10")
    store.close()
    upgraded = Store(path)
    try:
        [backup] = list((path.parent / "backups").glob("state-v10-*.sqlite"))
        copy = sqlite3.connect(backup)
        assert "ended" not in {r[1] for r in copy.execute("PRAGMA table_info(operations)")}
        copy.close()
        assert "ended" in {r["name"] for r in upgraded.all("PRAGMA table_info(operations)")}
        assert upgraded.one("PRAGMA user_version")["user_version"] == 11
    finally:
        upgraded.close()
    again = Store(path)
    again.close()
    assert len(list((path.parent / "backups").glob("state-v*.sqlite"))) == 1


@pytest.mark.asyncio
async def test_timeline_endpoint_is_read_only_and_scoped_to_the_users_tasks(store, source, tmp_path):
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="w", sources=[source])
    async with client_for(store, tmp_path) as client:
        before = store.db.total_changes
        response = await client.get(f"/api/tasks/{result['task_id']}/timeline")
        assert response.status == 200
        body = await response.json()
        assert body["task"]["id"] == result["task_id"] and body["events"]
        assert store.db.total_changes == before
        assert (await client.get("/api/tasks/nope/timeline")).status == 400
        assert (await client.post(f"/api/tasks/{result['task_id']}/timeline", json={})).status == 405
        assert (await client.get("/assets/replay.js")).status == 200


@pytest.mark.asyncio
async def test_pixel_skin_is_served_and_draws_only_from_code(store, tmp_path):
    async with client_for(store, tmp_path) as client:
        response = await client.get("/assets/replay-pixel.js")
        assert response.status == 200
        text = await response.text()
    # Self-drawn: grids and a palette in code, no image files or outside URLs.
    assert "const PAL" in text and "const SPRITES" in text
    for banned in ("http://", "https://", ".png", ".gif", "new Image", "import "):
        assert banned not in text


@pytest.mark.asyncio
async def test_replay_shows_which_memories_the_work_relied_on(store, source, tmp_path, successful_model):
    rule = store.save_memory("local", key="格式", value="先给结论", purpose="交付", kind="rule")
    gone = store.save_memory("local", key="旧称呼", value="小明", purpose="对话", kind="about_me")
    store.delete_memory("local", gone["id"], expected_version=gone["version"])
    store.import_memory_proposals("local", [{"kind": "preference", "key": "长度", "value": "喜欢简短"}], origin="测试导入")
    result = intake(store, "帮我整理这份资料", {"action": "new_task", "receipt": "好，我去整理。"}, nonce="mem", sources=[source])
    await Runtime(store, tmp_path / "data", successful_model).execute(store.claim())
    timeline = store.task_timeline(result["task_id"])
    [used] = [e for e in timeline["events"] if e["kind"] == "memory_used"]
    assert used["station"] == "start"
    facts = dict(used["facts"])
    assert facts["格式"] == f"规则 · 第 {rule['version']} 版"
    # Only what was actually handed to the model: not deleted items, not unconfirmed proposals.
    assert "旧称呼" not in facts and "长度" not in facts and "先给结论" not in encode(used)
