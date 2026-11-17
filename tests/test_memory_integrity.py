"""Memory integrity: resident loading bounds, version history, secret refusal,
and curation bookkeeping."""
import time

from personal_agent.contracts import TaskSpec, encode
from personal_agent.memory_store import RESIDENT_BUDGET


def begin(store, nonce="root"):
    store.submit(TaskSpec("Prepare a useful result", max_calls=30, max_tools=70), nonce=nonce)
    return store.claim()


def fill(store, active, *, tombstones=0, expired=0, size=40):
    for i in range(tombstones):
        saved = store.save_memory("local", key=f"已删{i}", value="DELETED_VALUE", purpose="测试")
        store.delete_memory("local", saved["id"], expected_version=saved["version"])
    for i in range(expired):
        saved = store.save_memory("local", key=f"过期{i}", value="EXPIRED_VALUE", purpose="测试", kind="ongoing")
        store.db.execute("UPDATE memories SET expires_at=? WHERE id=?", (time.time() - 1, saved["id"]))
    for i in range(active):
        store.save_memory("local", key=f"偏好{i:03d}", value=f"内容{i:03d}" + "长" * size, purpose="测试")


def counted(store, fn):
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        return fn(), len(statements)
    finally:
        store.db.set_trace_callback(None)


def test_resident_load_skips_tombstones_before_limiting_and_lists_titles_it_could_not_give(store):
    fill(store, 100, tombstones=40, expired=10)
    run = begin(store)
    loaded = store.read_memory(run)
    text = encode(loaded)
    assert "DELETED_VALUE" not in text and "EXPIRED_VALUE" not in text
    assert {i["state"] for i in loaded["items"]} <= {"active", "title_only"}
    full = {i["key"] for i in loaded["items"] if i["state"] == "active"}
    rest = set(loaded["more_titles"])
    assert full | rest | {i["key"] for i in loaded["items"]} == {f"偏好{i:03d}" for i in range(100)}, "every active memory is at least named"
    assert loaded["partial"] and rest and not full & rest
    assert len(text) <= RESIDENT_BUDGET + 600
    # The manifest records only what the model actually saw in full.
    manifest = store.one("SELECT manifest FROM memory_loads WHERE run_id=? ORDER BY id DESC", (run["id"],))["manifest"]
    assert manifest.count('"active"') == len(full)
    by_key = store.read_memory(run, keys=[sorted(rest)[0]])
    assert by_key["items"][0]["value"].startswith("内容")


def test_resident_load_is_complete_and_not_partial_when_everything_fits(store):
    fill(store, 20, tombstones=50, expired=5, size=5)
    loaded = store.read_memory(begin(store))
    assert len(loaded["items"]) == 20 and all(i["value"] for i in loaded["items"])
    assert not loaded["partial"] and loaded["more_titles"] == []


def test_resident_count_limit_follows_the_save_limit(store, monkeypatch):
    import personal_agent.memory_store as ms
    fill(store, 10, tombstones=5, size=1)
    monkeypatch.setattr(ms, "MAX_ACTIVE", 4)
    loaded = store.read_memory(begin(store))
    assert len(loaded["items"]) == 4, "the resident count limit is the save limit, not a separate number"
    assert len(loaded["more_titles"]) == 6 and loaded["partial"]


def test_resident_load_query_count_does_not_grow_with_memory_count(tmp_path):
    from personal_agent.store import Store
    counts = []
    for n in (10, 100):
        store = Store(tmp_path / f"s{n}" / "state.sqlite")
        try:
            fill(store, n, tombstones=n // 3, expired=n // 10)
            run = begin(store)
            _, statements = counted(store, lambda: store.read_memory(run))
            counts.append(statements)
        finally:
            store.close()
    assert counts[0] == counts[1], counts


# ---- 2. every version keeps what it said --------------------------------------

def test_each_version_keeps_its_own_content_through_corrections_renewal_and_deletion(store):
    first = store.save_memory("local", key="称呼", value="叫我小林", purpose="对话")
    second = store.save_memory("local", key="称呼", value="叫我林工", purpose="对话", expected_version=first["version"])
    store.delete_memory("local", second["id"], expected_version=second["version"])
    item = next(m for m in store.memory_items("local") if m["key"] == "称呼")
    assert item["value"] is None, "a tombstone itself still holds no value"
    assert [(h["version"], h["action"], h["value"]) for h in item["history"]] == [(1, "create", "叫我小林"), (2, "correct", "叫我林工"), (3, "delete", None)]
    trip = store.save_memory("local", key="出差", value="下周出差", purpose="安排", kind="ongoing")
    store.renew_memory("local", trip["id"], expected_version=trip["version"])
    trip_item = next(m for m in store.memory_items("local") if m["key"] == "出差")
    assert [h["value"] for h in trip_item["history"]] == ["下周出差", "下周出差"]


def test_galaxy_history_shows_what_each_version_said(store):
    first = store.save_memory("local", key="住址", value="B 区", purpose="寄快递", kind="reference")
    store.save_memory("local", key="住址", value="C 区", purpose="寄快递", kind="reference", expected_version=first["version"])
    star = next(s for s in store.memory_galaxy("local")["stars"] if s["key"] == "住址")
    assert [(h["version"], h["value"]) for h in star["history"]] == [(1, "B 区"), (2, "C 区")]


def test_history_never_reaches_a_model(store):
    first = store.save_memory("local", key="时段", value="OLD_HISTORY_VALUE", purpose="排日程")
    store.save_memory("local", key="时段", value="NEW_VALUE", purpose="排日程", expected_version=first["version"])
    run = begin(store)
    assert "OLD_HISTORY_VALUE" not in encode(store.read_memory(run))
    assert "OLD_HISTORY_VALUE" not in encode(store.read_memory(run, keys=["时段"]))


def test_upgrade_backs_up_then_adds_history_content_without_losing_rows(tmp_path):
    import sqlite3
    from personal_agent.store import Store
    path = tmp_path / "data" / "state.sqlite"
    old = Store(path)
    saved = old.save_memory("local", key="称呼", value="叫我小林", purpose="对话")
    gone = old.save_memory("local", key="旧事", value="不要了", purpose="对话")
    old.delete_memory("local", gone["id"], expected_version=gone["version"])
    old.close()
    # Rebuild the table as version 12 had it: no content column.
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE mc_old(id INTEGER PRIMARY KEY,memory_id TEXT NOT NULL,version INTEGER NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);
        INSERT INTO mc_old SELECT id,memory_id,version,action,actor,created FROM memory_changes; DROP TABLE memory_changes; ALTER TABLE mc_old RENAME TO memory_changes; PRAGMA user_version=12;""")
    before = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("memories", "memory_changes", "messages")}
    db.close()
    upgraded = Store(path)
    try:
        assert list((path.parent / "backups").glob("state-v12-*.sqlite")), "the old ledger is copied aside first"
        assert {t: upgraded.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in before} == before
        history = {h["action"]: h for h in next(m for m in upgraded.memory_items("local") if m["key"] == "称呼")["history"]}
        assert history["create"]["value"] == "叫我小林", "the current version's content is filled in from the memory itself"
        deleted = next(m for m in upgraded.memory_items("local") if m["key"] == "旧事")["history"]
        assert [h["value"] for h in deleted] == [None, None], "content that was never recorded stays unknown, not invented"
        assert upgraded.save_memory("local", key="称呼", value="叫我林工", purpose="对话", expected_version=saved["version"])["version"] == 2
    finally:
        upgraded.close()


# ---- 3. credentials never become memory ---------------------------------------
import secrets
import string

import pytest

from personal_agent.credential_check import credential_shape

AN = string.ascii_letters + string.digits
ORDINARY = ["ThinkPadX1Carbon2024", "XiaomiRedmiK70Pro16GB512GB", "MacBookPro16M3Max36GB1TB", "node-v24.22.2-win-x64",
            "SN-AB12-CD34-EF56-GH78", "2026-11-17T18:30:00", "WeChatMiniProgramDemo2024", "MyProject_v2_final_2024_Q3",
            "https://www.bilibili.com/video/BV1GJ411x7h7", "https://mp.weixin.qq.com/s/AbCdEfGhIjKlMnOpQrStUv",
            "顺丰单号 SF1234567890123", "手机 13800138000", "user_explicit_fact_not_permission", "github.com/BaoBao1996121/noyi",
            "我喜欢简短的回复，先给结论"]


def test_ordinary_names_ids_and_links_are_not_mistaken_for_credentials():
    assert [t for t in ORDINARY if credential_shape(t)] == []


@pytest.mark.parametrize("make", [
    lambda: "sk-" + "".join(secrets.choice(AN) for _ in range(48)),
    lambda: "ghp_" + "".join(secrets.choice(AN) for _ in range(36)),
    lambda: secrets.token_urlsafe(32),
    lambda: secrets.token_hex(20),
    lambda: "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----",
    lambda: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    lambda: "数据库连上 postgres://admin:Hunter2pass@db.example.com/app",
    lambda: "回调地址 https://hooks.example.com/notify?token=" + secrets.token_urlsafe(32),
])
def test_machine_made_secrets_are_recognised_by_shape(make):
    hits = sum(bool(credential_shape("我的 key 是 " + make())) for _ in range(50))
    assert hits >= 45, hits


def test_every_path_into_memory_refuses_credentials_and_says_why(store):
    key = "sk-" + "".join(secrets.choice(AN) for _ in range(48))
    with pytest.raises(ValueError, match="不保存密码、密钥和令牌") as refused:
        store.save_memory("local", key="接口", value="用这个 " + key, purpose="调用")
    assert key not in str(refused.value), "the refusal never repeats the secret"
    with pytest.raises(ValueError, match="不保存"):
        store.import_memory_proposals("local", [{"kind": "reference", "key": "接口", "value": key}], origin="导入")
    run = begin(store)
    message = store.one("SELECT id FROM messages WHERE task_id=?", (run["task_id"],))["id"]
    with pytest.raises(ValueError, match="不保存"):
        store.propose_memory(run, key="接口密钥", value=key, purpose="调用", source_ref=message)
    assert store.one("SELECT COUNT(*) n FROM memories")["n"] == 0
    assert store.one("SELECT COUNT(*) n FROM memory_proposals")["n"] == 0


def test_curation_cannot_turn_a_pasted_secret_into_a_suggestion(store):
    from test_memory_phase1 import say
    key = secrets.token_urlsafe(32)
    mid = say(store, "记一下，服务器令牌 " + key)
    with pytest.raises(ValueError, match="不保存"):
        store.record_memory_candidate("personal:local-user", kind="reference", key="服务器令牌", value=key,
                                      evidence=[{"message_id": mid, "quote": "服务器令牌 " + key}], correction=True)
    saved = store.save_memory("local", key="服务器", value="在机房", purpose="运维")
    with pytest.raises(ValueError, match="不保存"):
        store.propose_memory_change("personal:local-user", op="conflict", targets=[saved["id"]], value="令牌 " + key, reason="更新",
                                    evidence=[{"message_id": mid, "quote": "服务器令牌 " + key}], curation_id="c1")
    assert store.one("SELECT COUNT(*) n FROM memory_candidates")["n"] == 0


def test_asking_to_remember_a_secret_in_chat_is_refused_with_a_reason_the_model_can_relay(store):
    from test_speech import incoming, intake
    key = "ghp_" + "".join(secrets.choice(AN) for _ in range(36))
    incoming(store, "记住我的 GitHub 令牌 " + key, nonce="a")
    with pytest.raises(ValueError, match="密码管理器"):
        intake(store, {"action": "remember", "basis": "记住我的 GitHub 令牌", "memory_key": "GitHub 令牌", "memory_value": key, "receipt": "记住了你的令牌。"})
    assert not store.all("SELECT * FROM memories")


# ---- 4. quiet curation checks and export enums --------------------------------

@pytest.mark.asyncio
async def test_quiet_curation_checks_update_one_row_instead_of_adding_one_each_time(store, tmp_path):
    from conftest import ScriptedModel
    from test_memory_phase1 import curation_reply, say
    from personal_agent.memory_curation import MemoryCurator
    from personal_agent.runtime import Runtime
    now = time.time()
    model = ScriptedModel([curation_reply({"candidates": [], "merges": [], "conflicts": [], "updates": []})])
    curator = MemoryCurator(Runtime(store, tmp_path / "data", model))
    say(store, "刚说的话", at=now - 7200)
    assert [r["state"] for r in await curator.tick(now=now)] == ["done"]
    for i in range(1, 13):  # an hour of five-minute checks with nothing new
        await curator.tick(now=now + MIN_INTERVAL_FOR_TEST + 300 * i)
    rows = store.all("SELECT state,created,until FROM memory_curations ORDER BY created")
    assert [r["state"] for r in rows] == ["done", "skipped"], "a quiet streak is one row"
    assert rows[1]["until"] == now + MIN_INTERVAL_FOR_TEST + 300 * 12, "it carries the latest check time"
    say(store, "又说了新的话", at=now + MIN_INTERVAL_FOR_TEST + 4000)
    model.steps.append(curation_reply({"candidates": [], "merges": [], "conflicts": [], "updates": []}))
    await curator.tick(now=now + MIN_INTERVAL_FOR_TEST + 8000)
    await curator.tick(now=now + MIN_INTERVAL_FOR_TEST + 8300)
    await curator.tick(now=now + 2 * MIN_INTERVAL_FOR_TEST + 9000)
    assert [r["state"] for r in store.all("SELECT state FROM memory_curations ORDER BY created")] == ["done", "skipped", "done", "skipped"]


MIN_INTERVAL_FOR_TEST = 20 * 3600


@pytest.mark.asyncio
async def test_export_trigger_enum_matches_every_trigger_curation_writes(store, tmp_path):
    from conftest import ScriptedModel
    from personal_agent.memory_curation import TRIGGERS, MemoryCurator
    from personal_agent.ops_export import ENUMS, build_bundle
    from personal_agent.runtime import Runtime
    assert ENUMS["memory_curations.trigger"] == set(TRIGGERS)
    curator = MemoryCurator(Runtime(store, tmp_path / "data", ScriptedModel([])))
    for trigger in TRIGGERS:
        store.db.execute("DELETE FROM memory_curations")
        await curator.run("personal:local-user", trigger=trigger)
        bundle = build_bundle(store, since=0, until=10**10, salt="s", service={}, version={})
        assert [c["trigger"] for c in bundle["curations"]] == [trigger]
    with pytest.raises(ValueError):
        await curator.run("personal:local-user", trigger="idle")
