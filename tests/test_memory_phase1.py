"""Memory phase 1: kinds, shared personal scope, reply-layer recall, candidates,
daily curation that only proposes, and non-interrupting additions."""
import json
import sqlite3
import time

import pytest

from personal_agent.contracts import ModelReply, TaskSpec, encode
from personal_agent.memory_curation import MemoryCurator
from personal_agent.runtime import Runtime
from personal_agent.store import Store
from conftest import ScriptedModel, tool_reply


def begin(store, actor="default", room="local", text="Prepare a useful result", nonce="root"):
    tid = store.submit(TaskSpec(text, max_calls=30, max_tools=70), nonce=nonce, actor=actor, room=room)["task_id"]
    run = store.claim()
    assert run["task_id"] == tid
    return run


def say(store, text, *, room="local", nonce=None, at=None):
    """A user's own chat message at a chosen time."""
    mid = store.submit(None, text=text, nonce=nonce or text, room=room)["message_id"]
    if at is not None:
        store.db.execute("UPDATE events SET created=? WHERE message_id=?", (at, mid))
    store.db.execute("UPDATE events SET state='consumed' WHERE message_id=?", (mid,))
    return mid


def test_private_chats_share_personal_memory_and_groups_never_see_it(store, tmp_path):
    a = store.create_bot("资料助手", "整理资料")
    group = store.create_group("协作", [a["id"], "default"])
    store.save_memory(a["private_room"], key="称呼", value="叫我小林", purpose="对话", kind="about_me")
    assert [m["value"] for m in store.memory_items("local")] == ["叫我小林"]
    assert store.memory_items(group["id"]) == []
    rt = Runtime(store, tmp_path / "data", ScriptedModel([]))
    private = begin(store, room="local", nonce="p")
    assert "叫我小林" in encode(rt.context(private))
    store.finish(private, status="succeeded", reason="test", content="ok")
    in_group = begin(store, actor=a["id"], room=group["id"], nonce="g")
    assert "叫我小林" not in encode(rt.context(in_group))


def test_adding_or_confirming_memory_keeps_running_work_but_corrections_stop_it(store):
    run = begin(store)
    first = store.save_memory("local", key="格式", value="先给结论", purpose="交付", kind="rule")
    assert store.current(run), "a new memory must not interrupt running work"
    message = store.one("SELECT id FROM messages WHERE task_id=?", (run["task_id"],))["id"]
    proposal = store.import_memory_proposals("local", [{"kind": "preference", "key": "长度", "value": "喜欢简短"}], origin="测试导入")["created"][0]
    store.decide_memory(proposal, accept=True)
    assert store.current(run), "confirming a proposal must not interrupt running work"
    store.save_memory("local", key="格式", value="先给结论再列要点", purpose="交付", expected_version=first["version"])
    assert not store.current(run), "a correction still takes effect immediately"
    assert message


def test_deleting_loaded_memory_stops_work_but_restoring_or_renewing_unloaded_does_not(store):
    saved = store.save_memory("local", key="出差", value="下周出差", purpose="安排", kind="ongoing", expires_at=time.time() + 86400)
    deleted = store.delete_memory("local", saved["id"], expected_version=saved["version"])
    run = begin(store)
    restored = store.save_memory("local", key="出差", value="下周出差", purpose="安排", kind="ongoing", expected_version=deleted["version"], restore=True, expires_at=time.time() - 10)
    assert store.current(run)
    store.renew_memory("local", restored["id"], expected_version=restored["version"])
    assert store.current(run), "renewing an expired item adds it back; nothing running held it"


def test_ongoing_memory_needs_expiry_defaults_to_30_days_and_stops_loading_when_expired(store):
    saved = store.save_memory("local", key="面试", value="在准备面试", purpose="安排", kind="ongoing")
    assert 29 * 86400 < saved["expires_at"] - time.time() <= 30 * 86400 + 5
    store.db.execute("UPDATE memories SET expires_at=? WHERE id=?", (time.time() - 1, saved["id"]))
    run = begin(store)
    loaded = store.read_memory(run)
    assert "在准备面试" not in encode(loaded)
    item = store.memory_items("local")[0]
    assert item["expired"] and not item["loadable"]


def test_reply_layer_gets_compact_memory_with_reference_titles_and_keyword_recall(store):
    store.save_memory("local", key="交付格式", value="先给一句结论", purpose="回复", kind="rule")
    store.save_memory("local", key="家里地址", value="ADDRESS_VALUE_93", purpose="寄东西", kind="reference", keywords=["快递", "寄"])
    store.save_memory("local", key="链接", value="LINK_VALUE_44", purpose="查资料", kind="reference")
    for i in range(80):
        store.save_memory("local", key=f"偏好{i}", value="很长的偏好内容" * 10, purpose="回复", kind="preference")
    store.accept_conversation(TaskSpec("帮我寄个快递"), text="帮我寄个快递", nonce="c")
    run = store.claim_conversation()
    memory = store.conversation_context(run)["memory"]
    text = encode(memory)
    assert "先给一句结论" in text and "ADDRESS_VALUE_93" in text
    assert "链接" in text and "LINK_VALUE_44" not in text
    assert sum(len(line) for line in memory["items"]) <= 2000 and memory["partial"]
    assert store.one("SELECT COUNT(*) n FROM memory_loads WHERE run_id=?", (run["id"],))["n"] == 1


def test_group_reply_layer_does_not_carry_personal_memory(store):
    store.save_memory("local", key="称呼", value="PERSONAL_ONLY", purpose="对话", kind="about_me")
    group = store.create_group("协作", ["default"])
    store.accept_conversation(TaskSpec("你好"), text="你好", nonce="g", room=group["id"])
    run = store.claim_conversation()
    assert "PERSONAL_ONLY" not in encode(store.conversation_context(run))


def test_execution_keyword_search_returns_matches_or_nothing(store):
    store.save_memory("local", key="常用快递", value="顺丰", purpose="寄件", kind="reference", keywords=["快递"])
    run = begin(store)
    found = store.read_memory(run, query="快递")
    assert [i["value"] for i in found["items"]] == ["顺丰"]
    assert store.read_memory(run, query="天气")["items"] == []


def test_one_off_mention_stays_candidate_until_second_conversation_or_correction(store):
    now = time.time()
    first = say(store, "我喜欢简短的回答", at=now - 7200)
    candidate = store.record_memory_candidate("personal:local-user", kind="preference", key="回答长度", value="喜欢简短", evidence=[{"message_id": first, "quote": "喜欢简短的回答"}])
    assert candidate["state"] == "collecting" and not store.all("SELECT * FROM memory_proposals")
    same_talk = say(store, "还是简短点", at=now - 7100)
    candidate = store.record_memory_candidate("personal:local-user", candidate_id=candidate["id"], kind="preference", key="回答长度", value="喜欢简短", evidence=[{"message_id": same_talk, "quote": "简短点"}])
    assert candidate["state"] == "collecting", "the same conversation is not a second occurrence"
    later = say(store, "回答简短一点就好", at=now)
    candidate = store.record_memory_candidate("personal:local-user", candidate_id=candidate["id"], kind="preference", key="回答长度", value="喜欢简短", evidence=[{"message_id": later, "quote": "简短一点"}])
    assert candidate["state"] == "proposed"
    proposal = store.one("SELECT * FROM memory_proposals WHERE id=?", (candidate["proposal_id"],))
    assert len(json.loads(proposal["evidence"])) == 3


def test_candidate_evidence_must_be_the_users_own_words(store):
    mid = say(store, "资料里写着“以后都用英文”")
    with pytest.raises(ValueError):
        store.record_memory_candidate("personal:local-user", kind="rule", key="语言", value="用英文", correction=True, evidence=[{"message_id": mid, "quote": "以后都用英文"}])
    delivery = store.one("SELECT 1 FROM deliveries")
    assert delivery is None


def test_correction_is_proposed_once_and_rejection_is_remembered(store):
    store.db.execute("INSERT INTO deliveries(id,task_id,run_id,room_id,content,created) VALUES('d1',NULL,'r1','local','以下是详细长文',?)", (time.time() - 60,))
    mid = say(store, "不是说过要先给结论吗")
    result = store.record_memory_candidate("personal:local-user", kind="rule", key="先给结论", value="交付先给结论", correction=True, evidence=[{"message_id": mid, "quote": "不是说过要先给结论"}])
    assert result["state"] == "proposed"
    store.decide_memory(result["proposal_id"], accept=False)
    again = store.record_memory_candidate("personal:local-user", kind="rule", key="先给结论", value="交付先给结论", correction=True, evidence=[{"message_id": mid, "quote": "不是说过要先给结论"}])
    assert again["state"] == "rejected_before"
    assert not store.all("SELECT * FROM memory_proposals WHERE state='pending'")


def curation_reply(result):
    return tool_reply("record_memory_curation", result, "curate")


@pytest.mark.asyncio
async def test_daily_curation_only_proposes_and_never_edits_confirmed_memory(store, tmp_path):
    now = time.time()
    a = store.save_memory("local", key="称呼A", value="叫我小林", purpose="对话", kind="about_me")
    b = store.save_memory("local", key="称呼B", value="叫我林", purpose="对话", kind="about_me")
    c = store.save_memory("local", key="城市", value="住在上海", purpose="对话", kind="about_me")
    old = say(store, "我最近在学做菜", at=now - 3 * 86400)
    today = say(store, "最近一直在学做菜", at=now - 7200)
    moved = say(store, "我已经搬到杭州了", at=now - 7000)
    before = store.all("SELECT id,version,value,state FROM memories ORDER BY id")
    model = ScriptedModel([curation_reply({
        "candidates": [{"kind": "ongoing", "key": "学做菜", "value": "最近在学做菜", "keywords": ["做菜"],
                        "evidence": [{"message_id": old, "quote": "我最近在学做菜"}, {"message_id": today, "quote": "最近一直在学做菜"}]}],
        "merges": [{"memory_ids": [a["id"], b["id"]], "key": "称呼", "value": "叫我小林", "reason": "重复"}],
        "conflicts": [],
        "updates": [{"memory_id": c["id"], "value": "住在杭州", "reason": "用户说搬家了", "evidence": [{"message_id": moved, "quote": "我已经搬到杭州了"}]}],
    })])
    curator = MemoryCurator(Runtime(store, tmp_path / "data", model))
    result = await curator.run("personal:local-user", trigger="manual", now=now)
    assert result["state"] == "done" and result["proposals"] == 3
    assert store.all("SELECT id,version,value,state FROM memories ORDER BY id") == before
    ops = sorted(p["op"] for p in store.all("SELECT op FROM memory_proposals WHERE state='pending'"))
    assert ops == ["add", "conflict", "merge"]
    assert len(model.requests) == 1
    assert store.one("SELECT COUNT(*) n FROM model_requests")["n"] == 1
    # Accepting the merge goes through versioned saves and records what it replaced.
    merge = store.one("SELECT id FROM memory_proposals WHERE op='merge'")["id"]
    store.decide_memory(merge, accept=True)
    merged = store.one("SELECT * FROM memories WHERE key='称呼' AND state='active'")
    assert set(json.loads(merged["supersedes"])) == {a["id"], b["id"]}
    assert {r["state"] for r in store.all("SELECT state FROM memories WHERE id IN (?,?)", (a["id"], b["id"]))} == {"deleted"}
    conflict = store.one("SELECT id FROM memory_proposals WHERE op='conflict'")["id"]
    store.decide_memory(conflict, accept=True, choice="new")
    assert store.one("SELECT value,version FROM memories WHERE id=?", (c["id"],)) == {"value": "住在杭州", "version": 2}


@pytest.mark.asyncio
async def test_curation_rejects_unverified_evidence_and_stale_proposals_fail_safely(store, tmp_path):
    now = time.time()
    a = store.save_memory("local", key="饮品", value="喜欢咖啡", purpose="对话", kind="preference")
    b = store.save_memory("local", key="早餐饮品", value="只喝茶", purpose="对话", kind="preference")
    mid = say(store, "今天天气不错", at=now - 7200)
    model = ScriptedModel([curation_reply({
        "candidates": [{"kind": "preference", "key": "运动", "value": "喜欢跑步", "evidence": [{"message_id": mid, "quote": "喜欢跑步"}], "correction": True}],
        "merges": [], "updates": [],
        "conflicts": [{"memory_ids": [a["id"], b["id"]], "reason": "咖啡和茶冲突"}],
    })])
    result = await MemoryCurator(Runtime(store, tmp_path / "data", model)).run("personal:local-user", trigger="manual", now=now)
    assert result["proposals"] == 1 and result["rejected_items"] == 1
    conflict = store.one("SELECT id FROM memory_proposals WHERE op='conflict'")["id"]
    store.save_memory("local", key="饮品", value="喜欢美式", purpose="对话", expected_version=a["version"])
    assert store.decide_memory(conflict, accept=True, choice="first")["state"] == "superseded"
    assert store.one("SELECT state FROM memories WHERE id=?", (b["id"],))["state"] == "active"


@pytest.mark.asyncio
async def test_scheduled_curation_runs_once_a_day_when_idle_and_skips_without_news(store, tmp_path):
    now = time.time()
    model = ScriptedModel([curation_reply({"candidates": [], "merges": [], "conflicts": [], "updates": []})])
    curator = MemoryCurator(Runtime(store, tmp_path / "data", model))
    say(store, "刚说的话", at=now - 60)
    assert await curator.tick(now=now) == [], "not idle yet"
    assert await curator.tick(now=now + 3600) != []
    assert len(model.requests) == 1
    assert await curator.tick(now=now + 7200) == [], "at most once per day"
    quiet = await MemoryCurator(Runtime(store, tmp_path / "data", ScriptedModel([]))).run("personal:local-user", trigger="manual", now=now + 7300, since=now + 7200)
    assert quiet["state"] == "skipped"


@pytest.mark.asyncio
async def test_curation_proposes_renewal_of_expired_ongoing_without_a_model_call(store, tmp_path):
    saved = store.save_memory("local", key="出差", value="下周出差", purpose="安排", kind="ongoing", expires_at=time.time() + 60)
    store.db.execute("UPDATE memories SET expires_at=? WHERE id=?", (time.time() - 5, saved["id"]))
    result = await MemoryCurator(Runtime(store, tmp_path / "data", ScriptedModel([]))).run("personal:local-user", trigger="manual")
    assert result["proposals"] == 1
    renew = store.one("SELECT id FROM memory_proposals WHERE op='renew'")["id"]
    store.decide_memory(renew, accept=True)
    assert store.memory_items("local")[0]["loadable"]


@pytest.mark.asyncio
async def test_curation_notice_is_sent_at_most_once_a_day(store, tmp_path):
    saved = store.save_memory("local", key="出差", value="下周出差", purpose="安排", kind="ongoing", expires_at=time.time() + 60)
    store.db.execute("UPDATE memories SET expires_at=? WHERE id=?", (time.time() - 5, saved["id"]))
    curator = MemoryCurator(Runtime(store, tmp_path / "data", ScriptedModel([])))
    now = time.time() + 4000
    await curator.tick(now=now)
    await curator.run("personal:local-user", trigger="manual", now=now + 10)
    notices = [d for d in store.all("SELECT content FROM deliveries") if "整理" in d["content"]]
    assert len(notices) == 1


def test_imported_seed_memories_wait_for_confirmation_and_are_not_duplicated(store):
    items = [{"kind": "rule", "key": "中文回复", "value": "用中文回复"}, {"kind": "ongoing", "key": "路线", "value": "记忆第一期", "expires": "2026-12-08"}]
    first = store.import_memory_proposals("local", items, origin="从 Codex / Claude Code 导入")
    assert len(first["created"]) == 2 and not store.memory_items("local")
    again = store.import_memory_proposals("local", items, origin="从 Codex / Claude Code 导入")
    assert again["created"] == [] and again["skipped"] == 2
    proposal = store.one("SELECT * FROM memory_proposals WHERE key='路线'")
    store.decide_memory(proposal["id"], accept=True)
    item = store.memory_items("local")[0]
    assert item["kind"] == "ongoing" and item["origin"] == "从 Codex / Claude Code 导入" and item["expires_at"]
    with pytest.raises(ValueError):
        store.import_memory_proposals("local", [{"kind": "ongoing", "key": "x", "value": "y"}], origin="导入")


OLD_MEMORIES = "CREATE TABLE memories(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL REFERENCES rooms(id),key TEXT NOT NULL,owner TEXT NOT NULL,audience TEXT NOT NULL,source_type TEXT NOT NULL,source_ref TEXT,speaker TEXT NOT NULL,purpose TEXT NOT NULL,trust TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,value TEXT,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(collection_id,key))"
OLD_PROPOSALS = "CREATE TABLE memory_proposals(id TEXT PRIMARY KEY,room_id TEXT NOT NULL,actor_id TEXT NOT NULL,run_id TEXT NOT NULL,scope_revision INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,purpose TEXT NOT NULL,source_ref TEXT NOT NULL,expected_version INTEGER NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)"


def test_legacy_room_memories_migrate_into_shared_personal_collection(tmp_path):
    path = tmp_path / "data" / "state.sqlite"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE rooms(id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL DEFAULT '会话', kind TEXT NOT NULL DEFAULT 'private', revision INTEGER NOT NULL DEFAULT 1, context_floor REAL NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0)")
    db.executemany("INSERT INTO rooms(id,owner,name,kind) VALUES(?,?,?,?)", [("local", "local-user", "个人助理", "private"), ("private-b", "local-user", "资料助手", "private"), ("group-1", "local-user", "协作群", "group")])
    db.execute(OLD_MEMORIES)
    db.execute(OLD_PROPOSALS)
    row = "INSERT INTO memories VALUES(?,?,?,'local-user','private:x','user_explicit',NULL,'local-user','用途','explicit_user_fact_not_permission',?,?,?,?,?)"
    db.execute(row, ("m1", "local", "称呼", 2, "active", "小林", 1.0, 10.0))
    db.execute(row, ("m2", "private-b", "称呼", 1, "active", "老林", 1.0, 5.0))
    db.execute(row, ("m3", "group-1", "称呼", 1, "active", "群里叫林工", 1.0, 3.0))
    db.execute(row, ("m4", "private-b", "旧事", 3, "deleted", None, 1.0, 2.0))
    db.execute("INSERT INTO memory_proposals VALUES('p1','private-b','default','run1',1,'格式','表格','用途','msg1',0,'pending',1.0)")
    db.commit()
    db.close()
    store = Store(path)
    try:
        rows = {r["id"]: r for r in store.all("SELECT * FROM memories")}
        assert rows["m1"]["collection_id"] == rows["m2"]["collection_id"] == rows["m4"]["collection_id"] == "personal:local-user"
        assert rows["m3"]["collection_id"] == "group-1" and rows["m3"]["key"] == "称呼"
        assert rows["m1"]["key"] == "称呼" and rows["m2"]["key"] == "称呼（原「资料助手」）"
        assert (rows["m1"]["version"], rows["m1"]["value"], rows["m2"]["value"]) == (2, "小林", "老林")
        assert rows["m4"]["state"] == "deleted" and rows["m4"]["value"] is None
        assert {r["kind"] for r in rows.values()} == {"preference"}
        proposal = store.one("SELECT * FROM memory_proposals WHERE id='p1'")
        assert proposal["collection_id"] == "personal:local-user" and proposal["op"] == "add" and proposal["state"] == "pending"
        assert json.loads(proposal["evidence"]) == [{"message_id": "msg1"}]
        assert store.one("PRAGMA user_version")["user_version"] == Store.SCHEMA_VERSION
        assert store.save_memory("private-b", key="称呼", value="林", purpose="对话", expected_version=2)["version"] == 3
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.one("SELECT COUNT(*) n FROM memories")["n"] == 4
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_web_memory_page_groups_by_kind_and_accepts_with_versions(store, tmp_path):
    from test_webui import client_for
    store.save_memory("local", key="中文", value="用中文", purpose="回复", kind="rule")
    store.import_memory_proposals("local", [{"kind": "about_me", "key": "系统", "value": "日常用 Windows"}], origin="导入")
    async with client_for(store, tmp_path) as client:
        state = await (await client.get("/api/state?room=local")).json()
        assert state["memory_scope"] == "personal"
        assert [m["kind"] for m in state["memories"]] == ["rule"]
        proposal = state["memory_proposals"][0]
        assert proposal["op"] == "add" and proposal["kind"] == "about_me"
        decided = await (await client.post("/api/manage/decide_memory", json={"id": proposal["id"], "accept": True})).json()
        assert decided["stored_as_memory"]
        again = await client.post("/api/manage/decide_memory", json={"id": proposal["id"], "accept": True})
        assert again.status == 400
