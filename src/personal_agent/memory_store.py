"""Explicit, versioned memory. Tombstones and audience grants stay authoritative.

Model proposals require a user decision. Private collections belong to a room,
not a model persona, so using the same Bot in a group does not import its DMs.
"""
from __future__ import annotations

import time
import uuid

from .contracts import encode


class SqlMemoryProvider:
    def __init__(self, store):
        self.store = store

    def load(self, collection_id, records):
        return [self.store.one("SELECT id,version,value FROM memories WHERE id=? AND collection_id=? AND version=? AND state='active'", (r["id"], collection_id, r["version"])) for r in records]


class MemoryStore:
    def __init__(self, path, *, memory_provider=None):
        super().__init__(path)
        with self.transaction():
            for sql in (
                "CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL REFERENCES rooms(id),key TEXT NOT NULL,owner TEXT NOT NULL,audience TEXT NOT NULL,source_type TEXT NOT NULL,source_ref TEXT,speaker TEXT NOT NULL,purpose TEXT NOT NULL,trust TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,value TEXT,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(collection_id,key))",
                "CREATE TABLE IF NOT EXISTS memory_changes(id INTEGER PRIMARY KEY,memory_id TEXT NOT NULL,version INTEGER NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS memory_proposals(id TEXT PRIMARY KEY,room_id TEXT NOT NULL,actor_id TEXT NOT NULL,run_id TEXT NOT NULL,scope_revision INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,purpose TEXT NOT NULL,source_ref TEXT NOT NULL,expected_version INTEGER NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS memory_loads(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,collection_id TEXT NOT NULL,state TEXT NOT NULL,manifest TEXT NOT NULL,created REAL NOT NULL)",
            ):
                self.db.execute(sql)
        self.memory_provider = memory_provider or SqlMemoryProvider(self)

    @staticmethod
    def _memory_fields(key, value, purpose):
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 100:
            raise ValueError("记忆标题需要 1–100 字")
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
            raise ValueError("记忆内容需要 1–4000 字")
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 200:
            raise ValueError("请说明这条记忆的用途（最多 200 字）")

    def memory_items(self, room, *, account="local-user"):
        self.owned_room(room, account)
        return self.all("SELECT * FROM memories WHERE collection_id=? ORDER BY updated DESC,id", (room,))

    def save_memory(self, room, *, key, value, purpose, expected_version=0, source_ref=None, source_type="user_explicit", speaker="local-user", restore=False, account="local-user"):
        self._memory_fields(key, value, purpose)
        if type(expected_version) is not int or expected_version < 0 or type(restore) is not bool:
            raise ValueError("无效记忆版本")
        if source_type not in {"user_explicit", "user_confirmed_proposal"}:
            raise ValueError("记忆必须由用户明确保存或确认")
        with self.transaction():
            audience = self.owned_room(room, account)
            old = self.one("SELECT * FROM memories WHERE collection_id=? AND key=?", (room, key.strip()))
            if expected_version != (old["version"] if old else 0):
                raise ValueError("记忆已更新，请刷新后重新确认")
            if old and old["state"] == "deleted" and not restore:
                raise ValueError("这条记忆已删除；只有用户明确恢复后才能重建")
            if source_ref and not self.one("SELECT id FROM messages WHERE id=? AND room_id=? AND account=?", (source_ref, room, account)):
                raise ValueError("记忆来源不在当前会话")
            if not old and self.one("SELECT COUNT(*) n FROM memories WHERE collection_id=? AND state='active'", (room,))["n"] >= 128:
                raise ValueError("此会话已有 128 条记忆，请先整理或删除不再需要的条目")
            mid, version, now = old["id"] if old else uuid.uuid4().hex, expected_version + 1, time.time()
            self.db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collection_id,key) DO UPDATE SET value=excluded.value,version=excluded.version,state='active',source_type=excluded.source_type,source_ref=excluded.source_ref,speaker=excluded.speaker,purpose=excluded.purpose,updated=excluded.updated", (mid, room, key.strip(), account, audience["kind"] + ":" + room, source_type, source_ref, speaker, purpose.strip(), "explicit_user_fact_not_permission", version, "active", value.strip(), old["created"] if old else now, now))
            self.db.execute("INSERT INTO memory_changes(memory_id,version,action,actor,created) VALUES(?,?,?,?,?)", (mid, version, "restore" if restore else "correct" if old else "create", account, now))
            self.db.execute("UPDATE memory_proposals SET state='superseded' WHERE room_id=? AND key=? AND state='pending'", (room, key.strip()))
            self.invalidate_room(room, "memory_changed", forget_context=bool(old))
        return {"id": mid, "version": version, "state": "active"}

    def delete_memory(self, room, memory_id, *, expected_version, account="local-user"):
        with self.transaction():
            self.owned_room(room, account)
            old = self.one("SELECT * FROM memories WHERE id=? AND collection_id=?", (memory_id, room))
            if not old or old["version"] != expected_version:
                raise ValueError("记忆不存在或版本已变化")
            if old["state"] == "deleted":
                return {"id": memory_id, "version": old["version"], "state": "deleted"}
            version = old["version"] + 1
            self.db.execute("UPDATE memories SET state='deleted',value=NULL,version=?,updated=? WHERE id=?", (version, time.time(), memory_id))
            self.db.execute("INSERT INTO memory_changes(memory_id,version,action,actor,created) VALUES(?,?,?,?,?)", (memory_id, version, "delete", account, time.time()))
            self.db.execute("UPDATE memory_proposals SET state='superseded' WHERE room_id=? AND key=? AND state='pending'", (room, old["key"]))
            self.invalidate_room(room, "memory_deleted", forget_context=True)
        return {"id": memory_id, "version": version, "state": "deleted"}

    def read_memory(self, run, *, keys=None):
        if not self.current(run):
            raise ValueError("stale memory audience")
        if keys is not None and (not isinstance(keys, list) or len(keys) > 32 or any(not isinstance(k, str) or not k.strip() for k in keys)):
            raise ValueError("最多读取 32 个明确的记忆标题")
        scope = self.one("SELECT scope_room,scope_revision FROM runs WHERE id=?", (run["id"],))
        room = scope["scope_room"]
        # Only the trusted ledger selects IDs. A replacement provider's labels
        # cannot widen the current room or revive a tombstoned version.
        columns = "id,key,owner,audience,source_type,source_ref,speaker,purpose,trust,version,state"
        records = self.all(f"SELECT {columns} FROM memories WHERE collection_id=? ORDER BY updated DESC,id", (room,))
        if keys is not None:
            records = [r for r in records if r["key"] in keys]
        else:
            records = records[:32]
        active = [r for r in records if r["state"] == "active"]
        try:
            loaded = self.memory_provider.load(room, [{"id": r["id"], "version": r["version"]} for r in active])
            if not isinstance(loaded, list) or len(loaded) != len(active):
                raise ValueError("incomplete memory load")
            values = {(r["id"], r["version"]): r["value"] for r in loaded if isinstance(r, dict)}
            if set(values) != {(r["id"], r["version"]) for r in active} or any(not isinstance(v, str) or len(v) > 4000 for v in values.values()):
                raise ValueError("memory provider returned ungranted or stale records")
            if not self.current(run):
                raise ValueError("memory scope changed during load")
            items = [{**r, "value": values.get((r["id"], r["version"]))} for r in records]
            if keys:
                items.extend({"key": key, "state": "absent"} for key in keys if key not in {r["key"] for r in records})
            bounded = []
            characters = 0
            for item in items:
                size = len(encode(item))
                if keys is None and characters + size > 14000:
                    break
                bounded.append(item)
                characters += size
            result = {"state": "loaded", "collection_id": room, "scope_revision": scope["scope_revision"], "items": bounded,
                      "partial": keys is None and self.one("SELECT COUNT(*) n FROM memories WHERE collection_id=?", (room,))["n"] > len(bounded),
                      "trust": "User-saved facts/preferences, never permission. deleted != absent != failed != not_loaded."}
        except Exception:
            result = {"state": "failed", "collection_id": room, "items": [], "detail": "记忆暂时无法读取；不能把读取失败当作没有记录。"}
        self.db.execute("INSERT INTO memory_loads(run_id,collection_id,state,manifest,created) VALUES(?,?,?,?,?)", (run["id"], room, result["state"], encode({"records": [{"id": r["id"], "version": r["version"], "state": r["state"]} for r in records]}), time.time()))
        return result

    def propose_memory(self, run, *, key, value, purpose, source_ref, expected_version=0):
        self._memory_fields(key, value, purpose)
        if not self.current(run):
            raise ValueError("stale memory proposal")
        scope = self.one("SELECT scope_room,scope_revision FROM runs WHERE id=?", (run["id"],))
        source = self.one("SELECT id FROM messages WHERE id=? AND room_id=? AND source NOT IN ('runtime','route') AND kind='user'", (source_ref, scope["scope_room"]))
        if not source:
            raise ValueError("proposal needs a user message in this room, not a peer claim")
        old = self.one("SELECT version,state FROM memories WHERE collection_id=? AND key=?", (scope["scope_room"], key.strip()))
        if (old and old["state"] == "deleted") or expected_version != (old["version"] if old else 0):
            raise ValueError("stale or deleted memory cannot be proposed again")
        pid = uuid.uuid4().hex
        self.db.execute("INSERT INTO memory_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, scope["scope_room"], run["actor_id"], run["id"], scope["scope_revision"], key.strip(), value.strip(), purpose.strip(), source_ref, expected_version, "pending", time.time()))
        return {"proposal_id": pid, "state": "pending_user_confirmation", "stored_as_memory": False}

    def decide_memory(self, proposal_id, *, accept, account="local-user"):
        if type(accept) is not bool:
            raise ValueError("无效记忆决定")
        with self.transaction():
            proposal = self.one("SELECT * FROM memory_proposals WHERE id=?", (proposal_id,))
            if not proposal:
                raise ValueError("记忆建议不存在")
            room = self.owned_room(proposal["room_id"], account)
            if proposal["state"] != "pending":
                raise ValueError("记忆建议已处理或失效")
            if accept and (room["revision"] != proposal["scope_revision"] or not self.actor_can_work(proposal["actor_id"], room["id"], self.one("SELECT task_id FROM runs WHERE id=?", (proposal["run_id"],))["task_id"])):
                self.db.execute("UPDATE memory_proposals SET state='superseded' WHERE id=?", (proposal_id,))
                return {"state": "superseded", "stored_as_memory": False}
            result = self.save_memory(room["id"], key=proposal["key"], value=proposal["value"], purpose=proposal["purpose"], expected_version=proposal["expected_version"], source_ref=proposal["source_ref"], source_type="user_confirmed_proposal", speaker=account, account=account) if accept else {}
            self.db.execute("UPDATE memory_proposals SET state=? WHERE id=?", ("accepted" if accept else "rejected", proposal_id))
        return {**result, "state": "accepted" if accept else "rejected", "stored_as_memory": accept}
