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
