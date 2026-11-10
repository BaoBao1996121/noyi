"""Explicit, versioned memory. Tombstones and audience grants stay authoritative.

Collections: every private chat of an account shares one personal collection
(``personal:<account>``); a group keeps its own collection and never sees the
personal one. Five kinds decide where an item is resident (see KINDS).

Only the user's explicit words or confirmation create memory. Model output is
a proposal or a silent candidate; code checks facts (the cited words are the
user's own, versions still match, thresholds are met), the model judges what
the words mean. Adding or confirming never interrupts running work; a
correction or deletion stops work that may hold the old value.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid

from .authority import verify_user_words
from .contracts import encode

KINDS = {"rule": "规则", "preference": "偏好", "about_me": "关于我", "ongoing": "进行中", "reference": "常用信息"}
# Reply layer order: rules first; reference titles are an index, cheap to keep.
REPLY_ORDER = ("rule", "about_me", "ongoing", "preference")
OPS = {"add", "merge", "rewrite", "delete", "renew", "conflict"}
DEFAULT_ONGOING_DAYS = 30
DAY = 86400
REPLY_BUDGET = 2000
SESSION_GAP = 3600  # an hour of silence separates two conversations
MAX_ACTIVE = 128


class StaleMemory(ValueError):
    """The memory changed since it was shown; the decision must not apply."""

MEMORY_COLUMNS = "id,collection_id,key,owner,audience,source_type,source_ref,speaker,purpose,trust,version,state,value,created,updated,kind,expires_at,supersedes,subject,keywords,origin,evidence"
MEMORIES_SQL = """CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL,key TEXT NOT NULL,owner TEXT NOT NULL,
 audience TEXT NOT NULL,source_type TEXT NOT NULL,source_ref TEXT,speaker TEXT NOT NULL,purpose TEXT NOT NULL,trust TEXT NOT NULL,
 version INTEGER NOT NULL,state TEXT NOT NULL,value TEXT,created REAL NOT NULL,updated REAL NOT NULL,
 kind TEXT NOT NULL DEFAULT 'preference',expires_at REAL,supersedes TEXT,subject TEXT,keywords TEXT,origin TEXT,evidence TEXT,
 UNIQUE(collection_id,key))"""
PROPOSALS_SQL = """CREATE TABLE IF NOT EXISTS memory_proposals(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL,room_id TEXT,actor_id TEXT,run_id TEXT,
 scope_revision INTEGER,op TEXT NOT NULL DEFAULT 'add',kind TEXT,key TEXT,value TEXT,purpose TEXT,subject TEXT,keywords TEXT,expires_at REAL,
 evidence TEXT NOT NULL DEFAULT '[]',source_ref TEXT,targets TEXT NOT NULL DEFAULT '[]',expected_version INTEGER NOT NULL DEFAULT 0,
 origin TEXT,reason TEXT,curation_id TEXT,candidate_id TEXT,state TEXT NOT NULL,created REAL NOT NULL,decided REAL)"""


def norm(text):
    return re.sub(r"[\W_]+", "", (text or "").lower())


def parse_expiry(value):
    """A date the user/model gave ('YYYY-MM-DD' → end of that local day) or a timestamp."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("到期日格式无效")
    if isinstance(value, str):
        try:
            return time.mktime(time.strptime(value.strip()[:10], "%Y-%m-%d")) + DAY - 1
        except ValueError:
            raise ValueError("到期日请写成 YYYY-MM-DD") from None
    if value <= 0:
        raise ValueError("到期日格式无效")
    return float(value)


def date_label(stamp):
    return time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else None


class SqlMemoryProvider:
    def __init__(self, store):
        self.store = store

    def load(self, collection_id, records):
        return [self.store.one("SELECT id,version,value FROM memories WHERE id=? AND collection_id=? AND version=? AND state='active'", (r["id"], collection_id, r["version"])) for r in records]


class MemoryStore:
    def __init__(self, path, *, memory_provider=None):
        super().__init__(path)
        with self.transaction():
            self._migrate_memory()
            for sql in (
                MEMORIES_SQL, PROPOSALS_SQL,
                "CREATE TABLE IF NOT EXISTS memory_changes(id INTEGER PRIMARY KEY,memory_id TEXT NOT NULL,version INTEGER NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS memory_loads(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,collection_id TEXT NOT NULL,state TEXT NOT NULL,manifest TEXT NOT NULL,created REAL NOT NULL)",
                # Silent candidates: inferred, never resident, never shown as memory.
                "CREATE TABLE IF NOT EXISTS memory_candidates(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,subject TEXT,keywords TEXT,expires_at REAL,evidence TEXT NOT NULL,correction INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,proposal_id TEXT,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS memory_rejections(id INTEGER PRIMARY KEY,collection_id TEXT NOT NULL,fingerprint TEXT NOT NULL,proposal_id TEXT,created REAL NOT NULL,UNIQUE(collection_id,fingerprint))",
                "CREATE TABLE IF NOT EXISTS memory_curations(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL,trigger TEXT NOT NULL,state TEXT NOT NULL,run_id TEXT,since REAL,until REAL,input_chars INTEGER NOT NULL DEFAULT 0,proposals INTEGER NOT NULL DEFAULT 0,rejected_items INTEGER NOT NULL DEFAULT 0,notified INTEGER NOT NULL DEFAULT 0,error TEXT,created REAL NOT NULL,ended REAL)",
            ):
                self.db.execute(sql)
            try:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(memory_id UNINDEXED,collection_id UNINDEXED,body,tokenize='trigram')")
                self.db.execute("DELETE FROM memory_search")
                self.db.execute("INSERT INTO memory_search(memory_id,collection_id,body) SELECT id,collection_id,key||' '||COALESCE(value,'')||' '||COALESCE(keywords,'') FROM memories WHERE state='active'")
                self.memory_fts = True
            except sqlite3.OperationalError:
                self.memory_fts = False  # LIKE fallback still finds substrings
        self.memory_provider = memory_provider or SqlMemoryProvider(self)

    # ---- migration -------------------------------------------------------
    def _migrate_memory(self):
        """Room-scoped memory → shared personal collection, plus phase-1 fields.

        Rebuilt rather than ALTERed: the old table bound collection_id to rooms,
        which cannot hold a personal collection. Values, versions, tombstones and
        change history keep their identities.
        """
        tables = {r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type='table'")}
        if "memories" in tables and "kind" not in {c["name"] for c in self.all("PRAGMA table_info(memories)")}:
            rooms = {r["id"]: r for r in self.all("SELECT id,owner,name,kind FROM rooms")}
            old = self.all("SELECT * FROM memories ORDER BY updated DESC,id")
            self.db.execute("ALTER TABLE memories RENAME TO memories_legacy")
            self.db.execute(MEMORIES_SQL)
            taken = set()
            for row in old:
                room = rooms.get(row["collection_id"])
                collection = "personal:" + room["owner"] if room and room["kind"] == "private" else row["collection_id"]
                key = row["key"]
                if (collection, key) in taken:
                    # Two private chats saved the same title: the newer keeps it,
                    # the other stays visible under its original chat's name.
                    key = (key + "（原「" + (room["name"] if room else row["collection_id"]) + "」）")[:100]
                taken.add((collection, key))
                audience = "private:" + collection if collection.startswith("personal:") else row["audience"]
                self.db.execute(f"INSERT INTO memories({MEMORY_COLUMNS}) VALUES({','.join('?' * 22)})",
                                (row["id"], collection, key, row["owner"], audience, row["source_type"], row["source_ref"], row["speaker"], row["purpose"], row["trust"],
                                 row["version"], row["state"], row["value"], row["created"], row["updated"], "preference", None, None, None, None, None,
                                 encode([{"message_id": row["source_ref"]}]) if row["source_ref"] else "[]"))
            self.db.execute("DROP TABLE memories_legacy")
        if "memory_proposals" in tables and "op" not in {c["name"] for c in self.all("PRAGMA table_info(memory_proposals)")}:
            rooms = {r["id"]: r for r in self.all("SELECT id,owner,kind FROM rooms")}
            old = self.all("SELECT * FROM memory_proposals")
            self.db.execute("ALTER TABLE memory_proposals RENAME TO memory_proposals_legacy")
            self.db.execute(PROPOSALS_SQL)
            for p in old:
                room = rooms.get(p["room_id"])
                collection = "personal:" + room["owner"] if room and room["kind"] == "private" else p["room_id"]
                self.db.execute("INSERT INTO memory_proposals(id,collection_id,room_id,actor_id,run_id,scope_revision,op,kind,key,value,purpose,evidence,source_ref,expected_version,state,created) VALUES(?,?,?,?,?,?,'add','preference',?,?,?,?,?,?,?,?)",
                                (p["id"], collection, p["room_id"], p["actor_id"], p["run_id"], p["scope_revision"], p["key"], p["value"], p["purpose"],
                                 encode([{"message_id": p["source_ref"]}]), p["source_ref"], p["expected_version"], p["state"], p["created"]))
            self.db.execute("DROP TABLE memory_proposals_legacy")

    # ---- scope -----------------------------------------------------------
    def memory_collection(self, room, account="local-user"):
        row = self.owned_room(room, account)
        return "personal:" + row["owner"] if row["kind"] == "private" else row["id"]

    def _collection_rooms(self, collection):
        if collection.startswith("personal:"):
            return [r["id"] for r in self.all("SELECT id FROM rooms WHERE owner=? AND kind='private' ORDER BY rowid", (collection[len("personal:"):],))]
        return [collection]

    def memory_home_room(self, collection):
        rooms = self._collection_rooms(collection)
        return "local" if "local" in rooms else rooms[0] if rooms else None

    def _invalidate_collection(self, collection, reason):
        # A correction or deletion stops every run that may hold the old value.
        for room in self._collection_rooms(collection):
            self.invalidate_room(room, reason, forget_context=True)

    def current(self, run):
        if run.get("trigger") == "memory_curation":
            row = self.one("SELECT phase,stop_requested_at FROM runs WHERE id=?", (run["id"],))
            return bool(row and row["phase"] == "running" and row["stop_requested_at"] is None)
        return super().current(run)

    # ---- validation ------------------------------------------------------
    @staticmethod
    def _memory_fields(key, value, purpose):
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 100:
            raise ValueError("记忆标题需要 1–100 字")
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
            raise ValueError("记忆内容需要 1–4000 字")
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 200:
            raise ValueError("请说明这条记忆的用途（最多 200 字）")

    @staticmethod
    def _memory_extras(kind, keywords, subject):
        if kind not in KINDS:
            raise ValueError("记忆类别只能是：规则、偏好、关于我、进行中、常用信息")
        if keywords is not None and (not isinstance(keywords, list) or len(keywords) > 12 or any(not isinstance(k, str) or not 1 <= len(k.strip()) <= 30 for k in keywords)):
            raise ValueError("关键词最多 12 个，每个 1–30 字")
        if subject is not None and (not isinstance(subject, str) or len(subject.strip()) > 40):
            raise ValueError("「说的是谁」最多 40 字")
        return [k.strip() for k in keywords] if keywords is not None else None, (subject.strip() or None) if subject else None

    def _message_time(self, message_id):
        row = self.one("SELECT COALESCE((SELECT created FROM conversation_inputs WHERE message_id=?),(SELECT MIN(created) FROM events WHERE message_id=?),(SELECT created FROM followups WHERE message_id=?)) t", (message_id, message_id, message_id))
        return row["t"] if row else None

    def _verify_evidence(self, collection, evidence, *, account="local-user", correction=False):
        """Evidence is only the user's own words in this collection's rooms."""
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
            raise ValueError("证据需要 1–12 条用户原话")
        rooms = self._collection_rooms(collection)
        verified = []
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("message_id"), str):
                raise ValueError("证据需要消息编号和原话")
            message = self.one(f"SELECT id,room_id,content FROM messages WHERE id=? AND account=? AND kind='user' AND source NOT IN ('runtime','route') AND room_id IN ({','.join('?' * len(rooms))})", (item["message_id"], account, *rooms)) if rooms else None
            if not message:
                raise ValueError("证据必须是用户本人在这些会话里的消息")
            quote = verify_user_words(message["content"], item.get("quote"), "记忆证据")
            verified.append({"message_id": message["id"], "quote": quote[:200], "room_id": message["room_id"], "created": self._message_time(message["id"])})
        if correction:
            first = min(verified, key=lambda e: e["created"] or 0)
            if not self.one("SELECT 1 FROM deliveries WHERE room_id=? AND created>0 AND created<?", (first["room_id"], first["created"] or 0)):
                raise ValueError("纠正需要助理此前说过话")
        return verified

    def _conversations(self, collection, evidence, account="local-user"):
        """How many separate conversations the evidence comes from (fact, not judgment)."""
        count = 0
        for room in {e["room_id"] for e in evidence}:
            times = sorted(t for t in {e["created"] for e in evidence if e["room_id"] == room} if t is not None)
            if not times:
                continue
            count += 1
            all_times = [r["t"] for r in self.all("SELECT MIN(e.created) t FROM messages m JOIN events e ON e.message_id=m.id WHERE m.room_id=? AND m.account=? AND m.kind='user' GROUP BY m.id HAVING t BETWEEN ? AND ? ORDER BY t", (room, account, times[0], times[-1]))]
            points = sorted(set(all_times) | set(times))
            count += sum(1 for a, b in zip(points, points[1:]) if b - a >= SESSION_GAP)
        return count

    # ---- items -----------------------------------------------------------
    def _decorate(self, row, now=None):
        now = now or time.time()
        item = dict(row)
        item["keywords"] = json.loads(row["keywords"]) if row.get("keywords") else []
        item["supersedes"] = json.loads(row["supersedes"]) if row.get("supersedes") else []
        item["evidence"] = json.loads(row["evidence"]) if row.get("evidence") else []
        item["kind_label"] = KINDS.get(row["kind"], row["kind"])
        item["expired"] = row["kind"] == "ongoing" and row["expires_at"] is not None and row["expires_at"] <= now
        item["loadable"] = row["state"] == "active" and not item["expired"]
        quote = next((e.get("quote") for e in item["evidence"] if e.get("quote")), None)
        if not quote and row.get("source_ref"):
            source = self.one("SELECT content FROM messages WHERE id=?", (row["source_ref"],))
            quote = source["content"][:200] if source else None
        item["quote"] = quote
        return item

    def memory_items(self, room, *, account="local-user"):
        collection = self.memory_collection(room, account)
        order = {k: i for i, k in enumerate(KINDS)}
        rows = [self._decorate(r) for r in self.all("SELECT * FROM memories WHERE collection_id=? ORDER BY updated DESC,id", (collection,))]
        used = self.memory_last_used(collection)
        for row in rows:
            row["last_used"] = used.get(row["id"])
        return sorted(rows, key=lambda r: order.get(r["kind"], 9))

    def memory_galaxy(self, room, *, account="local-user"):
        """Read-only map of one collection for the galaxy view.

        Stars are every memory (tombstones included, value withheld) plus
        pending ``add`` proposals as unconfirmed stars. Links are facts already
        in the rows: ``supersedes`` pointers, a shared source message, a shared
        keyword. Shared groups are chained in creation order (n-1 links, not a
        clique) so a few hundred stars stay a few hundred links.
        """
        collection = self.memory_collection(room, account)
        now = time.time()
        history = {}
        for change in self.all("SELECT memory_id,version,action,created FROM memory_changes WHERE memory_id IN (SELECT id FROM memories WHERE collection_id=?) ORDER BY created,id", (collection,)):
            history.setdefault(change["memory_id"], []).append({"version": change["version"], "action": change["action"], "at": change["created"]})
        used = self.memory_last_used(collection)
        stars = []
        for row in self.all("SELECT * FROM memories WHERE collection_id=? ORDER BY created,id", (collection,)):
            item = self._decorate(row, now)
            stars.append({"id": row["id"], "key": row["key"], "value": row["value"], "kind": row["kind"], "state": row["state"], "version": row["version"],
                          "expired": item["expired"], "expires_at": row["expires_at"], "source_type": row["source_type"], "origin": row["origin"],
                          "quote": item["quote"], "evidence": item["evidence"], "keywords": item["keywords"], "subject": row["subject"], "supersedes": item["supersedes"],
                          "created": row["created"], "updated": row["updated"], "last_used": used.get(row["id"]), "history": history.get(row["id"], [])})
        for p in self.all("SELECT * FROM memory_proposals WHERE collection_id=? AND state='pending' AND op='add' ORDER BY created,id", (collection,)):
            evidence = json.loads(p["evidence"] or "[]")
            stars.append({"id": p["id"], "key": p["key"], "value": p["value"], "kind": p["kind"] or "preference", "state": "pending", "version": 0,
                          "expired": False, "expires_at": p["expires_at"], "source_type": "proposal", "origin": p["origin"], "reason": p["reason"],
                          "quote": next((e.get("quote") for e in evidence if e.get("quote")), None), "evidence": evidence,
                          "keywords": json.loads(p["keywords"]) if p["keywords"] else [], "subject": p["subject"], "supersedes": [],
                          "created": p["created"], "updated": p["created"], "last_used": None, "history": []})
        known = {s["id"] for s in stars}
        links, seen = [], set()

        def link(kind, a, b, label=None):
            pair = (kind, frozenset((a, b)))
            if a != b and pair not in seen:
                seen.add(pair)
                links.append({"kind": kind, "from": a, "to": b, **({"label": label} if label else {})})

        for star in stars:
            for old in star["supersedes"]:
                if old in known:
                    link("supersedes", star["id"], old)
        groups = {}
        for star in stars:
            if star["state"] == "deleted":
                continue  # a wreck only points to what replaced it
            sources = {e.get("message_id") for e in star["evidence"] if e.get("message_id")}
            for source in sources:
                groups.setdefault(("source", source), []).append(star["id"])
            for word in {k.strip().lower() for k in star["keywords"] if k.strip()}:
                groups.setdefault(("keyword", word), []).append(star["id"])
        for (kind, label), members in groups.items():
            for a, b in zip(members, members[1:]):
                link(kind, a, b, label if kind == "keyword" else None)
        return {"now": now, "kinds": KINDS, "stars": stars, "links": links}

    def memory_last_used(self, collection, since=None):
        """When each item was last actually handed to a model (from load manifests)."""
        since = time.time() - 90 * DAY if since is None else since
        used = {}
        for row in self.all("SELECT manifest,created FROM memory_loads WHERE collection_id=? AND state='loaded' AND created>=? ORDER BY created", (collection, since)):
            for record in json.loads(row["manifest"]).get("records", []):
                if record.get("state") in {"active", "title_only"}:
                    used[record["id"]] = row["created"]
        return used

    def _index_memory(self, memory_id):
        if not self.memory_fts:
            return
        self.db.execute("DELETE FROM memory_search WHERE memory_id=?", (memory_id,))
        self.db.execute("INSERT INTO memory_search(memory_id,collection_id,body) SELECT id,collection_id,key||' '||COALESCE(value,'')||' '||COALESCE(keywords,'') FROM memories WHERE id=? AND state='active'", (memory_id,))

    # ---- writes ----------------------------------------------------------
    def save_memory(self, room, *, key, value, purpose, expected_version=0, source_ref=None, source_type="user_explicit", speaker="local-user", restore=False, account="local-user",
                    kind=None, expires_at=None, keywords=None, subject=None, supersedes=None, origin=None, evidence=None):
        self._memory_fields(key, value, purpose)
        if type(expected_version) is not int or expected_version < 0 or type(restore) is not bool:
            raise ValueError("无效记忆版本")
        if source_type not in {"user_explicit", "user_confirmed_proposal"}:
            raise ValueError("记忆必须由用户明确保存或确认")
        with self.transaction():
            audience = self.owned_room(room, account)
            collection = self.memory_collection(room, account)
            key = key.strip()
            old = self.one("SELECT * FROM memories WHERE collection_id=? AND key=?", (collection, key))
            if expected_version != (old["version"] if old else 0):
                raise StaleMemory("记忆已更新，请刷新后重新确认")
            if old and old["state"] == "deleted" and not restore:
                raise StaleMemory("这条记忆已删除；只有用户明确恢复后才能重建")
            rooms = self._collection_rooms(collection)
            if source_ref and not self.one(f"SELECT id FROM messages WHERE id=? AND account=? AND room_id IN ({','.join('?' * len(rooms))})", (source_ref, account, *rooms)):
                raise ValueError("记忆来源不在当前会话")
            if (not old or old["state"] != "active") and self.one("SELECT COUNT(*) n FROM memories WHERE collection_id=? AND state='active'", (collection,))["n"] >= MAX_ACTIVE:
                raise ValueError(f"已有 {MAX_ACTIVE} 条记忆，请先整理或删除不再需要的条目")
            kind = kind or (old["kind"] if old else "preference")
            keywords, subject = self._memory_extras(kind, keywords, subject)
            now = time.time()
            expiry, default_expiry = None, False
            if kind == "ongoing":
                expiry = parse_expiry(expires_at) if expires_at is not None else (old["expires_at"] if old and old["kind"] == "ongoing" and old["state"] == "active" and old["expires_at"] else None)
                if expiry is None:
                    expiry, default_expiry = now + DEFAULT_ONGOING_DAYS * DAY, True
            if keywords is None and old:
                keywords = json.loads(old["keywords"]) if old["keywords"] else None
            if subject is None and old:
                subject = old["subject"]
            if supersedes is None and old:
                supersedes = json.loads(old["supersedes"]) if old["supersedes"] else None
            if origin is None and old:
                origin = old["origin"]
            evidence = evidence if evidence is not None else ([{"message_id": source_ref}] if source_ref else [])
            mid, version = old["id"] if old else uuid.uuid4().hex, expected_version + 1
            values = (mid, collection, key, account, audience["kind"] + ":" + collection, source_type, source_ref, speaker, purpose.strip(), "explicit_user_fact_not_permission",
                      version, "active", value.strip(), old["created"] if old else now, now, kind, expiry, encode(supersedes) if supersedes else None, subject,
                      encode(keywords) if keywords else None, origin, encode(evidence))
            updates = ",".join(f"{c}=excluded.{c}" for c in MEMORY_COLUMNS.split(",") if c not in {"id", "collection_id", "key", "owner", "created"})
            self.db.execute(f"INSERT INTO memories({MEMORY_COLUMNS}) VALUES({','.join('?' * 22)}) ON CONFLICT(collection_id,key) DO UPDATE SET {updates}", values)
            self.db.execute("INSERT INTO memory_changes(memory_id,version,action,actor,created) VALUES(?,?,?,?,?)", (mid, version, "restore" if old and old["state"] == "deleted" else "correct" if old else "create", account, now))
            self.db.execute("UPDATE memory_proposals SET state='superseded',decided=? WHERE collection_id=? AND key=? AND op='add' AND state='pending'", (now, collection, key))
            self._index_memory(mid)
            if old and old["state"] == "active":
                self._invalidate_collection(collection, "memory_changed")
        return {"id": mid, "version": version, "state": "active", "kind": kind, "expires_at": expiry, "default_expiry": default_expiry}

    def delete_memory(self, room, memory_id, *, expected_version, account="local-user"):
        with self.transaction():
            collection = self.memory_collection(room, account)
            old = self.one("SELECT * FROM memories WHERE id=? AND collection_id=?", (memory_id, collection))
            if not old or old["version"] != expected_version:
                raise StaleMemory("记忆不存在或版本已变化")
            if old["state"] == "deleted":
                return {"id": memory_id, "version": old["version"], "state": "deleted"}
            version, now = old["version"] + 1, time.time()
            self.db.execute("UPDATE memories SET state='deleted',value=NULL,version=?,updated=? WHERE id=?", (version, now, memory_id))
            self.db.execute("INSERT INTO memory_changes(memory_id,version,action,actor,created) VALUES(?,?,?,?,?)", (memory_id, version, "delete", account, now))
            self.db.execute("UPDATE memory_proposals SET state='superseded',decided=? WHERE collection_id=? AND key=? AND op='add' AND state='pending'", (now, collection, old["key"]))
            self._index_memory(memory_id)
            self._invalidate_collection(collection, "memory_deleted")
        return {"id": memory_id, "version": version, "state": "deleted"}

    def renew_memory(self, room, memory_id, *, expected_version, days=DEFAULT_ONGOING_DAYS, account="local-user"):
        """Extend an ongoing item; it only adds back what had stopped loading."""
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("续期天数需要 1–365 天")
        with self.transaction():
            collection = self.memory_collection(room, account)
            old = self.one("SELECT * FROM memories WHERE id=? AND collection_id=?", (memory_id, collection))
            if not old or old["version"] != expected_version or old["state"] != "active" or old["kind"] != "ongoing":
                raise StaleMemory("只能续期仍然有效的「进行中」记忆，或版本已变化")
            now = time.time()
            expiry = max(now, old["expires_at"] or now) + days * DAY
            self.db.execute("UPDATE memories SET expires_at=?,version=version+1,updated=? WHERE id=?", (expiry, now, memory_id))
            self.db.execute("INSERT INTO memory_changes(memory_id,version,action,actor,created) VALUES(?,?,?,?,?)", (memory_id, old["version"] + 1, "renew", account, now))
        return {"id": memory_id, "version": old["version"] + 1, "state": "active", "expires_at": expiry}

    # ---- reads -----------------------------------------------------------
    def _load_values(self, collection, records):
        """Values come from the provider but only for ledger-selected versions.

        The provider is asked even for an empty selection, so a provider that
        answers with records nobody asked for is caught, never trusted."""
        loaded = self.memory_provider.load(collection, [{"id": r["id"], "version": r["version"]} for r in records])
        if not isinstance(loaded, list) or len(loaded) != len(records):
            raise ValueError("incomplete memory load")
        values = {(r["id"], r["version"]): r["value"] for r in loaded if isinstance(r, dict)}
        if set(values) != {(r["id"], r["version"]) for r in records} or any(not isinstance(v, str) or len(v) > 4000 for v in values.values()):
            raise ValueError("memory provider returned ungranted or stale records")
        return values

    def search_memory(self, collection, query, *, limit=8):
        """Zero hits return nothing; never pad with unrelated items."""
        terms = [t for t in re.split(r"[\s,，。；;、!！?？]+", query.strip()) if t][:8]
        rows = {r["id"]: r for r in self.all("SELECT * FROM memories WHERE collection_id=? AND state='active'", (collection,))}
        hits = []
        for term in terms:
            found = set()
            if self.memory_fts and len(term) >= 3:
                # trigram needs three characters; shorter terms use LIKE.
                found |= {r["memory_id"] for r in self.all("SELECT memory_id FROM memory_search WHERE collection_id=? AND body MATCH ?", (collection, '"' + term.replace('"', '""') + '"'))}
            else:
                needle = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                found |= {r["id"] for r in self.all("SELECT id FROM memories WHERE collection_id=? AND state='active' AND (key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\' OR keywords LIKE ? ESCAPE '\\')", (collection, needle, needle, needle))}
            # A stored keyword contained in the query is also a hit.
            found |= {mid for mid, r in rows.items() if any(k and k in term for k in json.loads(r["keywords"] or "[]"))}
            hits.extend(mid for mid in sorted(found, key=lambda m: -rows[m]["updated"]) if mid in rows and mid not in hits)
        now = time.time()
        return [rows[m] for m in hits if not self._decorate(rows[m], now)["expired"]][:limit]

    def read_memory(self, run, *, keys=None, query=None):
        if not self.current(run):
            raise ValueError("stale memory audience")
        if keys is not None and (not isinstance(keys, list) or len(keys) > 32 or any(not isinstance(k, str) or not k.strip() for k in keys)):
            raise ValueError("最多读取 32 个明确的记忆标题")
        if query is not None and (not isinstance(query, str) or not 1 <= len(query.strip()) <= 200):
            raise ValueError("检索词需要 1–200 字")
        scope = self.one("SELECT scope_room,scope_revision FROM runs WHERE id=?", (run["id"],))
        collection = self.memory_collection(scope["scope_room"], self.one("SELECT owner FROM rooms WHERE id=?", (scope["scope_room"],))["owner"])
        columns = "id,key,kind,owner,audience,source_type,source_ref,speaker,purpose,trust,version,state,expires_at,subject,keywords"
        now = time.time()
        records = self.all(f"SELECT {columns} FROM memories WHERE collection_id=? ORDER BY updated DESC,id", (collection,))
        expired = {r["id"] for r in records if r["kind"] == "ongoing" and r["expires_at"] and r["expires_at"] <= now}
        if keys is not None:
            records, full = [r for r in records if r["key"] in keys], True
        elif query is not None:
            ids = [r["id"] for r in self.search_memory(collection, query)]
            records, full = sorted((r for r in records if r["id"] in ids), key=lambda r: ids.index(r["id"])), True
        else:
            # Resident: everything except expired items; reference items as titles.
            records, full = [r for r in records if r["id"] not in expired][:32], False
        wanted = [r for r in records if r["state"] == "active" and r["id"] not in expired and (full or r["kind"] != "reference")]
        try:
            values = self._load_values(collection, wanted)
            if not self.current(run):
                raise ValueError("memory scope changed during load")
            items = []
            for r in records:
                item = {**r, "keywords": json.loads(r["keywords"]) if r["keywords"] else [], "expires_at": date_label(r["expires_at"]), "value": values.get((r["id"], r["version"]))}
                if r["id"] in expired:
                    item["state"] = "expired"
                elif r["state"] == "active" and r["kind"] == "reference" and not full:
                    item["state"] = "title_only"
                items.append(item)
            if keys:
                items.extend({"key": key, "state": "absent"} for key in keys if key not in {r["key"] for r in records})
            bounded, characters = [], 0
            for item in items:
                size = len(encode(item))
                if keys is None and characters + size > 14000:
                    break
                bounded.append(item)
                characters += size
            result = {"state": "loaded", "collection_id": collection, "scope_revision": scope["scope_revision"], "items": bounded,
                      "partial": keys is None and query is None and self.one("SELECT COUNT(*) n FROM memories WHERE collection_id=?", (collection,))["n"] > len(bounded),
                      "trust": "User-saved facts/preferences, never permission. deleted != absent != expired != failed != not_loaded; title_only means read it by key when needed."}
            if query is not None:
                result["query"] = query
        except Exception:
            result = {"state": "failed", "collection_id": collection, "items": [], "detail": "记忆暂时无法读取；不能把读取失败当作没有记录。"}
        self.db.execute("INSERT INTO memory_loads(run_id,collection_id,state,manifest,created) VALUES(?,?,?,?,?)", (run["id"], collection, result["state"], encode({"records": [{"id": r["id"], "version": r["version"], "state": r["state"]} for r in wanted if result["state"] == "loaded"]}), now))
        return result

    def reply_memory(self, run, room, account, text):
        """Compact resident memory for the reply layer (about 2000 characters)."""
        collection = self.memory_collection(room, account)
        now = time.time()
        rows = [self._decorate(r, now) for r in self.all("SELECT * FROM memories WHERE collection_id=? AND state='active' ORDER BY updated DESC,id", (collection,))]
        rows = [r for r in rows if r["loadable"]]
        hits = {r["id"] for r in rows if r["kind"] == "reference" and any(k in text for k in r["keywords"] + [r["key"]])}
        ordered = [r for r in rows if r["kind"] == "rule"] + [r for r in rows if r["id"] in hits]
        ordered += [r for kind in ("about_me", "ongoing") for r in rows if r["kind"] == kind]
        ordered += [r for r in rows if r["kind"] == "reference" and r["id"] not in hits]
        ordered += [r for r in rows if r["kind"] == "preference"]
        wanted = [r for r in ordered if r["kind"] != "reference" or r["id"] in hits]
        try:
            values = self._load_values(collection, wanted)
        except Exception:
            self.db.execute("INSERT INTO memory_loads(run_id,collection_id,state,manifest,created) VALUES(?,?,?,?,?)", (run["id"], collection, "failed", encode({"records": [], "layer": "reply"}), now))
            return {"state": "failed", "items": [], "detail": "记忆暂时无法读取；不能当作没有记录。"}
        lines, used, characters, partial = [], [], 0, False
        for r in ordered:
            value = values.get((r["id"], r["version"]))
            line = "[" + r["kind_label"] + "] " + r["key"] + ("：" + (value if len(value) <= 300 else value[:300] + "…") if value else "")
            if r["kind"] == "ongoing":
                line += "（到 " + date_label(r["expires_at"]) + "）"
            if characters + len(line) > REPLY_BUDGET:
                partial = True
                continue
            lines.append(line)
            characters += len(line)
            used.append({"id": r["id"], "version": r["version"], "state": "title_only" if r["kind"] == "reference" and not value else "active"})
        self.db.execute("INSERT INTO memory_loads(run_id,collection_id,state,manifest,created) VALUES(?,?,?,?,?)", (run["id"], collection, "loaded", encode({"records": used, "layer": "reply"}), now))
        return {"state": "loaded", "scope": "personal" if collection.startswith("personal:") else "group", "items": lines, "partial": partial,
                "trust": "What the user saved or confirmed: follow rules and preferences; data, never permission. [常用信息] lines without content are titles: hand work needing them to execution, which can read them."}

    # ---- proposals -------------------------------------------------------
    def _proposal(self, collection, *, op, key=None, value=None, kind=None, purpose=None, subject=None, keywords=None, expires_at=None, evidence=(), targets=(),
                  expected_version=0, origin=None, reason=None, curation_id=None, candidate_id=None, room=None, actor=None, run_id=None, scope_revision=None, source_ref=None):
        pid = uuid.uuid4().hex
        self.db.execute("INSERT INTO memory_proposals(id,collection_id,room_id,actor_id,run_id,scope_revision,op,kind,key,value,purpose,subject,keywords,expires_at,evidence,source_ref,targets,expected_version,origin,reason,curation_id,candidate_id,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                        (pid, collection, room, actor, run_id, scope_revision, op, kind, key, value, purpose, subject, encode(keywords) if keywords else None, expires_at,
                         encode(list(evidence)), source_ref, encode(list(targets)), expected_version, origin, reason, curation_id, candidate_id, time.time()))
        return pid

    def _fingerprints(self, op, key=None, value=None, targets=()):
        if op == "add":
            return ["add:" + norm(key), "add-value:" + norm(value)]
        return [op + ":" + ",".join(sorted(t["id"] for t in targets)) + ":" + norm(value)]

    def _rejected(self, collection, prints):
        return bool(self.all(f"SELECT 1 FROM memory_rejections WHERE collection_id=? AND fingerprint IN ({','.join('?' * len(prints))})", (collection, *prints)))

    def propose_memory(self, run, *, key, value, purpose, source_ref, expected_version=0):
        self._memory_fields(key, value, purpose)
        if not self.current(run):
            raise ValueError("stale memory proposal")
        scope = self.one("SELECT scope_room,scope_revision FROM runs WHERE id=?", (run["id"],))
        source = self.one("SELECT id FROM messages WHERE id=? AND room_id=? AND source NOT IN ('runtime','route') AND kind='user'", (source_ref, scope["scope_room"]))
        if not source:
            raise ValueError("proposal needs a user message in this room, not a peer claim")
        room = self.one("SELECT owner FROM rooms WHERE id=?", (scope["scope_room"],))
        collection = self.memory_collection(scope["scope_room"], room["owner"])
        old = self.one("SELECT version,state FROM memories WHERE collection_id=? AND key=?", (collection, key.strip()))
        if (old and old["state"] == "deleted") or expected_version != (old["version"] if old else 0):
            raise ValueError("stale or deleted memory cannot be proposed again")
        pid = self._proposal(collection, op="add", key=key.strip(), value=value.strip(), purpose=purpose.strip(), evidence=[{"message_id": source_ref}], expected_version=expected_version,
                             room=scope["scope_room"], actor=run["actor_id"], run_id=run["id"], scope_revision=scope["scope_revision"], source_ref=source_ref)
        return {"proposal_id": pid, "state": "pending_user_confirmation", "stored_as_memory": False}

    def memory_proposals(self, room, *, account="local-user"):
        collection = self.memory_collection(room, account)
        out = []
        for p in self.all("SELECT * FROM memory_proposals WHERE collection_id=? AND state='pending' ORDER BY created,id", (collection,)):
            p["evidence"], p["targets"] = json.loads(p["evidence"] or "[]"), json.loads(p["targets"] or "[]")
            p["keywords"] = json.loads(p["keywords"]) if p["keywords"] else []
            p["kind_label"] = KINDS.get(p["kind"], p["kind"])
            p["target_items"] = [self._decorate(m) for t in p["targets"] for m in [self.one("SELECT * FROM memories WHERE id=? AND collection_id=?", (t["id"], collection))] if m]
            p["column"] = "tidy" if p["curation_id"] else "pending"
            out.append(p)
        return out

    def decide_memory(self, proposal_id, *, accept, choice=None, account="local-user"):
        if type(accept) is not bool:
            raise ValueError("无效记忆决定")
        with self.transaction():
            p = self.one("SELECT * FROM memory_proposals WHERE id=?", (proposal_id,))
            if not p:
                raise ValueError("记忆建议不存在")
            rooms = self._collection_rooms(p["collection_id"])
            if not rooms or not self.one("SELECT 1 FROM rooms WHERE id=? AND owner=?", (rooms[0], account)):
                raise ValueError("没有这个会话的访问权限")
            if p["state"] == "superseded":
                return {"state": "superseded", "stored_as_memory": False}
            if p["state"] != "pending":
                raise ValueError("记忆建议已处理或失效")
            now = time.time()
            targets = json.loads(p["targets"] or "[]")
            if not accept:
                self.db.execute("UPDATE memory_proposals SET state='rejected',decided=? WHERE id=?", (now, proposal_id))
                for fp in self._fingerprints(p["op"], p["key"], p["value"], targets):
                    self.db.execute("INSERT OR IGNORE INTO memory_rejections(collection_id,fingerprint,proposal_id,created) VALUES(?,?,?,?)", (p["collection_id"], fp, proposal_id, now))
                if p["candidate_id"]:
                    self.db.execute("UPDATE memory_candidates SET state='rejected',updated=? WHERE id=?", (now, p["candidate_id"]))
                return {"state": "rejected", "stored_as_memory": False}
            room = p["room_id"] if p["room_id"] in rooms else self.memory_home_room(p["collection_id"])
            current = {t["id"]: self.one("SELECT * FROM memories WHERE id=? AND collection_id=?", (t["id"], p["collection_id"])) for t in targets}
            stale = any(not m or m["version"] != t["version"] or m["state"] != "active" for t, m in zip(targets, current.values()))
            if p["run_id"] and p["actor_id"]:
                run = self.one("SELECT task_id FROM runs WHERE id=?", (p["run_id"],))
                stale = stale or not self.actor_can_work(p["actor_id"], p["room_id"], run["task_id"] if run else None)
            if p["op"] == "add":
                # The title may have been saved, corrected or deleted since.
                old = self.one("SELECT version FROM memories WHERE collection_id=? AND key=?", (p["collection_id"], p["key"]))
                stale = stale or (old["version"] if old else 0) != p["expected_version"]
            if stale:
                self.db.execute("UPDATE memory_proposals SET state='superseded',decided=? WHERE id=?", (now, proposal_id))
                return {"state": "superseded", "stored_as_memory": False}
            result = self._apply_proposal(p, room, targets, current, choice, account)
            self.db.execute("UPDATE memory_proposals SET state='accepted',decided=? WHERE id=?", (now, proposal_id))
            if p["candidate_id"]:
                self.db.execute("UPDATE memory_candidates SET state='accepted',updated=? WHERE id=?", (now, p["candidate_id"]))
        return {**result, "state": "accepted", "stored_as_memory": True}

    def _apply_proposal(self, p, room, targets, current, choice, account):
        evidence = json.loads(p["evidence"] or "[]")
        source_ref = p["source_ref"] or next((e["message_id"] for e in evidence if e.get("message_id")), None)
        common = dict(source_type="user_confirmed_proposal", speaker=account, account=account)
        purpose = p["purpose"] or "用户确认的记忆"
        keywords = json.loads(p["keywords"]) if p["keywords"] else None
        if p["op"] == "add":
            old = self.one("SELECT version,state FROM memories WHERE collection_id=? AND key=?", (p["collection_id"], p["key"]))
            return self.save_memory(room, key=p["key"], value=p["value"], purpose=purpose, expected_version=p["expected_version"], source_ref=source_ref, kind=p["kind"],
                                    expires_at=p["expires_at"], keywords=keywords, subject=p["subject"], origin=p["origin"], evidence=evidence,
                                    restore=bool(old and old["state"] == "deleted"), **common)
        if p["op"] == "rewrite":
            m = current[targets[0]["id"]]
            return self.save_memory(room, key=m["key"], value=p["value"], purpose=m["purpose"], expected_version=m["version"], source_ref=source_ref,
                                    evidence=evidence, origin=p["origin"], **common)
        if p["op"] == "delete":
            m = current[targets[0]["id"]]
            return self.delete_memory(room, m["id"], expected_version=m["version"], account=account)
        if p["op"] == "renew":
            m = current[targets[0]["id"]]
            return self.renew_memory(room, m["id"], expected_version=m["version"], account=account)
        if p["op"] == "merge":
            same = next((m for m in current.values() if m["key"] == p["key"]), None)
            others = [m for m in current.values() if m is not same]
            for m in others:
                self.delete_memory(room, m["id"], expected_version=m["version"], account=account)
            if same:
                return self.save_memory(room, key=same["key"], value=p["value"], purpose=same["purpose"], expected_version=same["version"], supersedes=[m["id"] for m in others],
                                        evidence=evidence, origin=p["origin"], **common)
            first = next(iter(current.values()))
            existing = self.one("SELECT version,state FROM memories WHERE collection_id=? AND key=?", (p["collection_id"], p["key"]))
            return self.save_memory(room, key=p["key"], value=p["value"], purpose=first["purpose"], kind=p["kind"] or first["kind"], expected_version=existing["version"] if existing else 0,
                                    restore=bool(existing), supersedes=[m["id"] for m in others], evidence=evidence, origin=p["origin"], **common)
        if p["op"] == "conflict" and len(targets) == 1:
            # Confirmed memory vs. what the user said later: the user picks.
            m = current[targets[0]["id"]]
            if choice == "old":
                return {"kept": [m["id"]]}
            if choice != "new":
                raise ValueError("请选择保留原来的，还是改成新的说法")
            return self.save_memory(room, key=m["key"], value=p["value"], purpose=m["purpose"], expected_version=m["version"], source_ref=source_ref,
                                    evidence=evidence, origin=p["origin"], **common)
        if p["op"] == "conflict":
            ordered = [current[t["id"]] for t in targets]
            if choice == "both":
                return {"kept": [m["id"] for m in ordered]}
            if choice not in {"first", "second"}:
                raise ValueError("请选择保留哪一条，或两条都留")
            keep, drop = (ordered[0], ordered[1]) if choice == "first" else (ordered[1], ordered[0])
            self.delete_memory(room, drop["id"], expected_version=drop["version"], account=account)
            self.db.execute("UPDATE memories SET supersedes=? WHERE id=?", (encode([drop["id"]]), keep["id"]))
            return {"kept": [keep["id"]], "removed": [drop["id"]]}
        raise ValueError("未知的整理操作")

    # ---- candidates and curation proposals -------------------------------
    def record_memory_candidate(self, collection, *, kind, key, value, evidence, candidate_id=None, correction=False, keywords=None, subject=None, expires_at=None, curation_id=None, account="local-user"):
        """Silently accumulate inferred memory; promote only on checked thresholds."""
        self._memory_fields(key, value, "候选")
        keywords, subject = self._memory_extras(kind, keywords, subject)
        with self.transaction():
            verified = self._verify_evidence(collection, evidence, account=account, correction=correction)
            now = time.time()
            if self._rejected(collection, self._fingerprints("add", key, value)):
                return {"state": "rejected_before", "id": candidate_id}
            existing = self.one("SELECT * FROM memories WHERE collection_id=? AND key=?", (collection, key.strip()))
            if existing:
                raise ValueError("已有同名记忆；请改为改写或合并提议")
            row = self.one("SELECT * FROM memory_candidates WHERE id=? AND collection_id=?", (candidate_id, collection)) if candidate_id else None
            if candidate_id and not row:
                raise ValueError("候选不存在")
            if row and row["state"] != "collecting":
                return {"state": row["state"], "id": row["id"], "proposal_id": row["proposal_id"]}
            merged = (json.loads(row["evidence"]) if row else []) + [e for e in verified if not row or e["message_id"] not in {x["message_id"] for x in json.loads(row["evidence"])}]
            expiry = parse_expiry(expires_at) if expires_at is not None else (now + DEFAULT_ONGOING_DAYS * DAY if kind == "ongoing" else None)
            cid = row["id"] if row else uuid.uuid4().hex
            corrected = bool(correction or (row and row["correction"]))
            if row:
                self.db.execute("UPDATE memory_candidates SET kind=?,key=?,value=?,subject=?,keywords=?,expires_at=?,evidence=?,correction=?,updated=? WHERE id=?",
                                (kind, key.strip(), value.strip(), subject, encode(keywords) if keywords else None, expiry, encode(merged), int(corrected), now, cid))
            else:
                self.db.execute("INSERT INTO memory_candidates VALUES(?,?,?,?,?,?,?,?,?,?,'collecting',NULL,?,?)",
                                (cid, collection, kind, key.strip(), value.strip(), subject, encode(keywords) if keywords else None, expiry, encode(merged), int(corrected), now, now))
            if not corrected and self._conversations(collection, merged, account) < 2:
                return {"state": "collecting", "id": cid}
            evidence_public = [{k: e[k] for k in ("message_id", "quote", "created")} for e in merged]
            pid = self._proposal(collection, op="add", key=key.strip(), value=value.strip(), kind=kind, purpose="整理时从你的原话里发现", subject=subject, keywords=keywords,
                                 expires_at=expiry, evidence=evidence_public, curation_id=curation_id or "candidate", candidate_id=cid,
                                 reason="你纠正过助理" if corrected else "在两段不同对话里都这样说过", source_ref=merged[0]["message_id"])
            self.db.execute("UPDATE memory_candidates SET state='proposed',proposal_id=?,updated=? WHERE id=?", (pid, now, cid))
            return {"state": "proposed", "id": cid, "proposal_id": pid}

    def propose_memory_change(self, collection, *, op, targets, value=None, key=None, kind=None, reason, evidence=None, curation_id, account="local-user"):
        """A tidy-sheet item about confirmed memory. Never edits it."""
        if op not in OPS - {"add"}:
            raise ValueError("未知的整理操作")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200:
            raise ValueError("整理建议需要 1–200 字的理由")
        ids = list(targets) if isinstance(targets, list) else []
        # A one-target conflict is "confirmed memory vs. the user's later words".
        sizes = {"rewrite": {1}, "delete": {1}, "renew": {1}, "merge": set(range(2, 7)), "conflict": {2} if value is None else {1}}[op]
        if len(ids) not in sizes or len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids):
            raise ValueError("整理建议指向的记忆数量不对")
        rows = [self.one("SELECT * FROM memories WHERE id=? AND collection_id=? AND state='active'", (i, collection)) for i in ids]
        if any(r is None for r in rows):
            raise ValueError("整理建议指向的记忆不存在")
        if op in {"rewrite", "merge"} or (op == "conflict" and value is not None):
            self._memory_fields(key or rows[0]["key"], value, "整理")
        if op == "rewrite" or (op == "conflict" and value is not None):
            evidence = self._verify_evidence(collection, evidence or [], account=account)
            evidence = [{k: e[k] for k in ("message_id", "quote", "created")} for e in evidence]
        targets = [{"id": r["id"], "version": r["version"], "key": r["key"]} for r in rows]
        prints = self._fingerprints(op, key, value, targets)
        if self._rejected(collection, prints):
            return None
        for p in self.all("SELECT targets,op FROM memory_proposals WHERE collection_id=? AND state='pending' AND op=?", (collection, op)):
            if sorted(t["id"] for t in json.loads(p["targets"])) == sorted(ids):
                return None
        return self._proposal(collection, op=op, key=(key or rows[0]["key"]).strip() if op in {"merge", "rewrite"} else rows[0]["key"], value=value.strip() if value else None,
                              kind=kind or rows[0]["kind"], evidence=evidence or [], targets=targets, reason=reason.strip(), curation_id=curation_id)

    def import_memory_proposals(self, room, items, *, origin, account="local-user"):
        """Seed memories wait in 「待确认」; nothing becomes active here."""
        if not isinstance(origin, str) or not 1 <= len(origin.strip()) <= 80 or not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ValueError("导入需要来源说明和 1–200 条记忆")
        with self.transaction():
            collection = self.memory_collection(room, account)
            created, skipped = [], 0
            for item in items:
                if not isinstance(item, dict) or set(item) - {"kind", "key", "value", "purpose", "expires", "keywords", "subject"}:
                    raise ValueError("导入条目格式无效")
                purpose = item.get("purpose") or origin
                self._memory_fields(item.get("key"), item.get("value"), purpose)
                keywords, subject = self._memory_extras(item.get("kind"), item.get("keywords"), item.get("subject"))
                if item["kind"] == "ongoing" and not item.get("expires"):
                    raise ValueError("「进行中」记忆需要到期日：" + item["key"])
                key = item["key"].strip()
                if (self.one("SELECT 1 FROM memories WHERE collection_id=? AND key=?", (collection, key))
                        or self.one("SELECT 1 FROM memory_proposals WHERE collection_id=? AND op='add' AND key=? AND origin=?", (collection, key, origin.strip()))):
                    skipped += 1
                    continue
                created.append(self._proposal(collection, op="add", key=key, value=item["value"].strip(), kind=item["kind"], purpose=purpose.strip(), subject=subject, keywords=keywords,
                                              expires_at=parse_expiry(item.get("expires")), origin=origin.strip(), reason="导入的初始记忆，确认后才生效"))
        return {"created": created, "skipped": skipped}
