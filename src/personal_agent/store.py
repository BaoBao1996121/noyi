from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
import time
import uuid

from .contracts import TaskSpec, encode, digest


def uid() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS actors(id TEXT PRIMARY KEY, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY, owner TEXT NOT NULL);
INSERT OR IGNORE INTO actors(id,kind) VALUES('default','bot');
INSERT OR IGNORE INTO rooms(id,owner) VALUES('local','local-user');
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, root_task_id TEXT NOT NULL, parent_task_id TEXT,
 actor_id TEXT NOT NULL REFERENCES actors(id), room_id TEXT NOT NULL REFERENCES rooms(id),
 spec TEXT NOT NULL, requirement_revision INTEGER NOT NULL DEFAULT 1,
 execution_epoch INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ready',
 reason TEXT, acceptance TEXT NOT NULL DEFAULT 'pending',
 calls_used INTEGER NOT NULL DEFAULT 0, tools_used INTEGER NOT NULL DEFAULT 0,
 active_seconds REAL NOT NULL DEFAULT 0, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, account TEXT NOT NULL, client_nonce TEXT NOT NULL,
 actor_id TEXT NOT NULL, room_id TEXT NOT NULL, task_id TEXT REFERENCES tasks(id),
 kind TEXT NOT NULL, content TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'received', adopted_by_run TEXT,
 UNIQUE(source,account,client_nonce)
);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, task_id TEXT REFERENCES tasks(id),
 trigger TEXT NOT NULL, epoch INTEGER, revision INTEGER, phase TEXT NOT NULL,
 end_reason TEXT, started REAL NOT NULL, ended REAL, context_manifest TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_actor ON runs(actor_id) WHERE phase='running';
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, task_id TEXT REFERENCES tasks(id),
 message_id TEXT NOT NULL REFERENCES messages(id), kind TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', claimed_by_run TEXT, consumed_by_run TEXT,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps(
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT NOT NULL UNIQUE,
 room_id TEXT NOT NULL, content TEXT NOT NULL, artifact TEXT,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 receipt TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS model_requests(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 sequence INTEGER NOT NULL, payload_sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, ended REAL,
 UNIQUE(run_id,sequence)
);
CREATE TABLE IF NOT EXISTS input_usage(
 request_id TEXT NOT NULL REFERENCES model_requests(id), event_id TEXT NOT NULL REFERENCES events(id),
 message_id TEXT NOT NULL REFERENCES messages(id), run_id TEXT NOT NULL REFERENCES runs(id),
 input_sha256 TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(request_id,event_id)
);
"""


def same_submission(old_payload, new_payload):
    """Additive TaskSpec defaults do not turn a retried old input into new work."""
    old, new = json.loads(old_payload), json.loads(new_payload)
    if not isinstance(old, dict) or not isinstance(new, dict):
        return old == new
    for payload in (old, new):
        if isinstance(payload.get("spec"), dict):
            payload["spec"] = asdict(TaskSpec(**payload["spec"]))
    return old == new


class BaseStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        # Public operations compose atomically (e.g. ingress receipt + task).
        # Nested calls use savepoints; an inner exception cannot commit outer work.
        nested = self.db.in_transaction
        point = "sp_" + uid()
        self.db.execute("SAVEPOINT " + point if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("RELEASE SAVEPOINT " + point if nested else "COMMIT")
        except BaseException:
            if nested:
                self.db.execute("ROLLBACK TO SAVEPOINT " + point)
                self.db.execute("RELEASE SAVEPOINT " + point)
            else:
                self.db.execute("ROLLBACK")
            raise

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def submit(self, spec: TaskSpec | None, *, text: str = "", nonce: str,
               source="cli", account="local-user", actor="default", room="local", thread=None) -> dict:
        if spec:
            spec.validate()
        content = spec.objective if spec else text
        if not content.strip() or not nonce:
            raise ValueError("message and nonce required")
        payload_data = {"spec": asdict(spec) if spec else None, "text": content,
                        "actor": actor, "room": room}
        if thread is not None:
            payload_data["thread"] = thread
        payload = encode(payload_data)
        with self.transaction():
            old = self.one("SELECT * FROM messages WHERE source=? AND account=? AND client_nonce=?",
                           (source, account, nonce))
            if old:
                if not same_submission(old["payload"], payload):
                    raise ValueError("nonce already used for different input")
                return {"message_id": old["id"], "task_id": old["task_id"], "duplicate": True}
            if not self.one("SELECT id FROM actors WHERE id=?", (actor,)):
                raise ValueError("unknown actor")
            if not self.one("SELECT id FROM rooms WHERE id=? AND owner=?", (room, account)):
                raise ValueError("room not owned by account")
            mid, eid = uid(), uid()
            tid = uid() if spec else None
            if spec:
                self.db.execute("INSERT INTO tasks(id,root_task_id,actor_id,room_id,spec,created) VALUES(?,?,?,?,?,?)",
                                (tid, tid, actor, room, encode(asdict(spec)), time.time()))
            self.db.execute("INSERT INTO messages(id,source,account,client_nonce,actor_id,room_id,task_id,kind,content,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (mid, source, account, nonce, actor, room, tid, "user", content, payload))
            self.db.execute("INSERT INTO events(id,actor_id,task_id,message_id,kind,created) VALUES(?,?,?,?,?,?)",
                            (eid, actor, tid, mid, "user_task" if spec else "chat", time.time()))
            self._submitted(mid, tid, source, account, thread or room)
        return {"message_id": mid, "task_id": tid, "duplicate": False}

    def claim(self) -> dict | None:
        with self.transaction():
            event = self.one("""SELECT e.* FROM events e WHERE e.state='pending'
                AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.actor_id=e.actor_id AND r.phase='running')
                AND (e.task_id IS NULL OR EXISTS(SELECT 1 FROM tasks t WHERE t.id=e.task_id AND t.status='ready'))
                ORDER BY e.created,e.id LIMIT 1""")
            if not event:
                return None
            tid, rid = event["task_id"], uid()
            epoch, revision = None, None
            if tid:
                self.db.execute("UPDATE tasks SET execution_epoch=execution_epoch+1,status='running' WHERE id=?", (tid,))
                task = self.task(tid)
                epoch, revision = task["execution_epoch"], task["requirement_revision"]
            self.db.execute("INSERT INTO runs(id,actor_id,task_id,trigger,epoch,revision,phase,started) VALUES(?,?,?,?,?,?,?,?)",
                            (rid, event["actor_id"], tid, event["kind"], epoch, revision, "running", time.time()))
            self.db.execute("UPDATE events SET state='claimed',claimed_by_run=? WHERE id=?", (rid, event["id"]))
            return self.one("SELECT * FROM runs WHERE id=?", (rid,))

    def begin_request(self, run, messages, metadata=None):
        """Persist dispatch intent, not a claim that a provider received/understood it."""
        with self.transaction():
            if not self.current(run):
                raise RuntimeError("stale run cannot send a model request")
            request_id = uid()
            seq = self.one("SELECT COUNT(*) AS n FROM model_requests WHERE run_id=?", (run["id"],))["n"] + 1
            self.db.execute("INSERT INTO model_requests VALUES(?,?,?,?,?,?,?,?)",
                            (request_id, run["id"], seq, digest(encode(messages).encode()),
                             encode(metadata or {}), "dispatching", time.time(), None))
            for event in self.all("SELECT e.id,e.message_id,m.payload FROM events e JOIN messages m ON m.id=e.message_id WHERE e.claimed_by_run=? AND e.state='claimed'", (run["id"],)):
                self.db.execute("INSERT INTO input_usage VALUES(?,?,?,?,?,?)",
                                (request_id, event["id"], event["message_id"], run["id"],
                                 digest(event["payload"].encode()), "dispatching"))
        return request_id

    def end_request(self, request_id, state="completed"):
        if state not in {"completed", "failed", "unknown"}:
            raise ValueError("invalid request outcome")
        with self.transaction():
            self.db.execute("UPDATE model_requests SET state=?,ended=? WHERE id=? AND state='dispatching'", (state, time.time(), request_id))
            self.db.execute("UPDATE input_usage SET state=? WHERE request_id=? AND state='dispatching'", (state, request_id))
            if state == "completed":
                self.db.execute("UPDATE messages SET status='adopted',adopted_by_run=(SELECT run_id FROM model_requests WHERE id=?) WHERE id IN (SELECT message_id FROM input_usage WHERE request_id=? AND state='completed')", (request_id, request_id))

    def resume(self, tid, *, extra_calls=0, extra_tools=0, extra_seconds=0):
        """Explicit local retry, retaining budgets and historical Runs/receipts."""
        with self.transaction():
            task = self.task(tid)
            if task["status"] not in {"paused", "failed", "review"}:
                raise ValueError("only paused, failed or review tasks can resume")
            if type(extra_calls) is not int or type(extra_tools) is not int or min(extra_calls, extra_tools, extra_seconds) < 0:
                raise ValueError("budget extensions must be nonnegative")
            spec = TaskSpec(**task["spec"])
            spec.max_calls += extra_calls
            spec.max_tools += extra_tools
            spec.max_seconds += extra_seconds
            spec.validate()
            if (task["calls_used"] >= spec.max_calls or task["tools_used"] >= spec.max_tools
                    or task["active_seconds"] >= spec.max_seconds):
                raise ValueError("budget exhausted; explicitly extend the exhausted budget to resume")
            self.db.execute("UPDATE tasks SET spec=? WHERE id=?", (encode(asdict(spec)), tid))
            self.db.execute("UPDATE tasks SET status='ready',reason='user_resume',acceptance='pending' WHERE id=?", (tid,))
            if not self.one("SELECT id FROM events WHERE task_id=? AND state='pending'", (tid,)):
                msg = self.one("SELECT id FROM messages WHERE task_id=? ORDER BY rowid LIMIT 1", (tid,))
                self.db.execute("INSERT INTO events(id,actor_id,task_id,message_id,kind,created) VALUES(?,?,?,?,?,?)", (uid(), task["actor_id"], tid, msg["id"], "user_resume", time.time()))
        return {"task_id": tid, "status": "ready", "budget_reset": False}

    def task(self, tid):
        task = self.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not task:
            raise KeyError(tid)
        task["spec"] = json.loads(task["spec"])
        return task

    def current(self, run):
        r = self.one("SELECT phase FROM runs WHERE id=?", (run["id"],))
        if not r or r["phase"] != "running":
            return False
        if run["task_id"] is None:
            return True
        t = self.task(run["task_id"])
        return (t["status"] == "running" and t["execution_epoch"] == run["epoch"]
                and t["requirement_revision"] == run["revision"])

    def step(self, run, kind, data):
        self.db.execute("INSERT INTO steps(task_id,run_id,kind,data,created) VALUES(?,?,?,?,?)",
                        (run["task_id"], run["id"], kind, encode(data), time.time()))

    def reserve(self, run, kind):
        column = {"model": "calls_used", "tool": "tools_used"}[kind]
        limit = {"model": "max_calls", "tool": "max_tools"}[kind]
        with self.transaction():
            if not self.current(run):
                return False
            task = self.task(run["task_id"])
            if task[column] >= task["spec"][limit]:
                return False
            self.db.execute(f"UPDATE tasks SET {column}={column}+1 WHERE id=?", (task["id"],))
        return True

    def operations(self, tid):
        rows = self.all("SELECT * FROM operations WHERE task_id=? ORDER BY created,id", (tid,))
        for row in rows:
            row["arguments"] = json.loads(row["arguments"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
        return rows

    def finish(self, run, *, status, reason, content, artifact=None, acceptance="pending", before_commit=None):
        with self.transaction():
            if not self.current(run):
                raise RuntimeError("stale run cannot finalize")
            if run["task_id"]:
                task = self.task(run["task_id"])
                elapsed = min(max(0, time.time()-run["started"]), max(0, task["spec"]["max_seconds"]-task["active_seconds"]))
                self.db.execute("UPDATE tasks SET status=?,reason=?,acceptance=?,active_seconds=active_seconds+? WHERE id=?",
                                (status, reason, acceptance, elapsed, run["task_id"]))
            self.db.execute("UPDATE operations SET status='interrupted' WHERE run_id=? AND status='running'", (run["id"],))
            self.db.execute("UPDATE runs SET phase='ended',end_reason=?,ended=? WHERE id=?", (reason, time.time(), run["id"]))
            self.db.execute("UPDATE events SET state='consumed',consumed_by_run=? WHERE claimed_by_run=? AND id IN (SELECT event_id FROM input_usage WHERE run_id=? AND state='completed')", (run["id"], run["id"], run["id"]))
            room = self.one("SELECT m.room_id FROM messages m JOIN events e ON e.message_id=m.id WHERE e.claimed_by_run=?", (run["id"],))["room_id"]
            self.db.execute("INSERT INTO deliveries(id,task_id,run_id,room_id,content,artifact) VALUES(?,?,?,?,?,?)",
                            (uid(), run["task_id"], run["id"], room, content, encode(artifact) if artifact else None))
            # Failed chat is explicitly reported once; a task retains unresolved input for retry.
            self.db.execute("UPDATE events SET state=?,claimed_by_run=NULL WHERE claimed_by_run=? AND state='claimed'",
                            ("pending" if run["task_id"] else "archived", run["id"]))
            if before_commit:
                before_commit()

    def recover(self):
        """Only the process holding ServiceLock may call this."""
        with self.transaction():
            rows = self.all("SELECT * FROM runs WHERE phase='running'")
            for run in rows:
                self.db.execute("UPDATE runs SET phase='ended',end_reason='interrupted',ended=? WHERE id=?", (time.time(), run["id"]))
                if run["task_id"]:
                    # Unknown interruption time: conservatively charge elapsed time,
                    # capped at authorized remaining budget, never days of downtime.
                    task = self.task(run["task_id"])
                    self._charge_elapsed(task, run)
                    self.db.execute("UPDATE tasks SET status='ready',reason='recovered' WHERE id=? AND status='running'", (run["task_id"],))
                self.step(run, "recovery", {"reason": "interrupted", "remote_model_billing": "unknown"})
            self.db.execute("UPDATE events SET state='pending',claimed_by_run=NULL WHERE state='claimed'")
            self.db.execute("UPDATE operations SET status='interrupted' WHERE status='running'")
            self.db.execute("UPDATE model_requests SET state='unknown',ended=? WHERE state='dispatching'", (time.time(),))
            self.db.execute("UPDATE input_usage SET state='unknown' WHERE state='dispatching'")
        return len(rows)


from .collaboration_store import CollaborationStore
from .lifecycle_store import LifecycleStore


from .identity_store import IdentityStore
from .memory_store import MemoryStore
from .goal_store import GoalStore


from .admission_store import AdmissionStore
from .review_store import ReviewStore
from .conversation import ConversationStore


class Store(ConversationStore, ReviewStore, AdmissionStore, GoalStore, MemoryStore, IdentityStore, LifecycleStore, CollaborationStore, BaseStore):
    """Durable task ledger with versioned collaboration protocols."""
