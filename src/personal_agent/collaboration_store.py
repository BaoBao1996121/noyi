from __future__ import annotations

from dataclasses import asdict
import json
import time
import uuid

from .contracts import TaskSpec, encode


def uid():
    return uuid.uuid4().hex


TERMINAL = {"succeeded", "failed", "cancelled", "stopped", "review", "paused"}


class CollaborationStore:
    """M1 protocols; all state transitions use the same SQLite authority."""

    def __init__(self, path):
        super().__init__(path)
        with self.transaction():
            columns = {
                "tasks": {"auto_continue": "TEXT NOT NULL DEFAULT 'on'", "cancel_state": "TEXT NOT NULL DEFAULT 'active'",
                          "continuations": "INTEGER NOT NULL DEFAULT 0", "wait_spec": "TEXT", "prior_task_id": "TEXT",
                          "parent_revision": "INTEGER", "required": "INTEGER NOT NULL DEFAULT 1", "worker_key": "TEXT"},
                "runs": {"stop_requested_at": "REAL", "stop_reason": "TEXT"},
                "messages": {"native_thread_id": "TEXT"},
                "deliveries": {"kind": "TEXT NOT NULL DEFAULT 'result'", "attention_id": "TEXT", "created": "REAL NOT NULL DEFAULT 0"},
            }
            for table, additions in columns.items():
                present = {r["name"] for r in self.all(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in present:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            for sql in (
                "CREATE UNIQUE INDEX IF NOT EXISTS worker_key ON tasks(parent_task_id,worker_key) WHERE parent_task_id IS NOT NULL",
                "CREATE TABLE IF NOT EXISTS thread_subscriptions(task_id TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,thread TEXT NOT NULL,mode TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(task_id,source,account,thread))",
                "CREATE TABLE IF NOT EXISTS followups(id TEXT PRIMARY KEY,message_id TEXT NOT NULL UNIQUE,event_id TEXT,owner_task TEXT,state TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 1,revision INTEGER,reason TEXT,evidence_ref TEXT,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS followup_history(id INTEGER PRIMARY KEY,followup_id TEXT NOT NULL,owner_task TEXT,generation INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT,kind TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,ended REAL)",
                "CREATE TABLE IF NOT EXISTS attentions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT,account TEXT NOT NULL,revision INTEGER NOT NULL,kind TEXT NOT NULL,question TEXT NOT NULL,options TEXT NOT NULL,state TEXT NOT NULL,answer TEXT,delivery_id TEXT,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS answer_receipts(account TEXT NOT NULL,nonce TEXT NOT NULL,payload TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(account,nonce))",
                "CREATE TABLE IF NOT EXISTS worker_adoptions(child_id TEXT NOT NULL,child_epoch INTEGER NOT NULL,parent_revision INTEGER NOT NULL,run_id TEXT NOT NULL,artifact_sha256 TEXT NOT NULL,rationale TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(child_id,child_epoch,parent_revision))",
            ):
                self.db.execute(sql)
            self.db.execute("PRAGMA user_version=3")

    def task(self, tid):
        task = super().task(tid)
        task["wait_spec"] = json.loads(task["wait_spec"]) if task.get("wait_spec") else None
        return task

    def _owned(self, tid, account="local-user", room=None):
        task = self.task(tid)
        if room is not None and task["room_id"] != room:
            raise ValueError("task outside room")
        if not self.one("SELECT id FROM rooms WHERE id=? AND owner=?", (task["room_id"], account)):
            raise ValueError("task outside account")
        return task

    def submit(self, spec, *, text="", nonce, source="cli", account="local-user", actor="default", room="local", thread=None):
        return super().submit(spec, text=text, nonce=nonce, source=source, account=account, actor=actor, room=room, thread=thread)

    def _submitted(self, mid, tid, source, account, thread):
        if tid:
            self.subscribe(tid, thread=thread, source=source, account=account)
        self.db.execute("UPDATE messages SET native_thread_id=? WHERE id=?", (thread, mid))

    def subscribe(self, task_id, *, thread, mode="own", source="cli", account="local-user"):
        self._owned(task_id, account)
        if mode not in {"own", "context"} or not isinstance(thread, str) or not thread:
            raise ValueError("invalid subscription")
        self.db.execute("INSERT INTO thread_subscriptions VALUES(?,?,?,?,?,1) ON CONFLICT(task_id,source,account,thread) DO UPDATE SET mode=excluded.mode,active=1", (task_id, source, account, thread, mode))

    def _event(self, tid, kind, content, *, message_id=None):
        task = self.task(tid)
        mid, eid = message_id or uid(), uid()
        if message_id is None:
            account = self.one("SELECT owner FROM rooms WHERE id=?", (task["room_id"],))["owner"]
            self.db.execute("INSERT INTO messages(id,source,account,client_nonce,actor_id,room_id,task_id,kind,content,payload,native_thread_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (mid, "runtime", account, mid, task["actor_id"], task["room_id"], tid, "system_event", content, encode({"kind": kind, "data": content}), task["room_id"]))
        self.db.execute("INSERT INTO events(id,actor_id,task_id,message_id,kind,created) VALUES(?,?,?,?,?,?)", (eid, task["actor_id"], tid, mid, kind, time.time()))
        return eid

    def _notice(self, task_id, room, content, *, kind="system", attention_id=None):
        did = uid()
        self.db.execute("INSERT INTO deliveries(id,task_id,run_id,room_id,content,kind,attention_id,created) VALUES(?,?,?,?,?,?,?,?)",
                        (did, task_id, "notice:" + did, room, content, kind, attention_id, time.time()))
        return did

    @staticmethod
    def _thanks(text):
        return text.strip().lower().rstrip(".!。！~～ ") in {"谢谢", "多谢", "好的谢谢", "收到", "thanks", "thank you", "ok", "好的", "辛苦了"}

    def _history(self, fid, reason=None):
        f = self.one("SELECT * FROM followups WHERE id=?", (fid,))
        self.db.execute("INSERT INTO followup_history(followup_id,owner_task,generation,state,reason,created) VALUES(?,?,?,?,?,?)",
                        (fid, f["owner_task"], f["generation"], f["state"], reason or f["reason"], time.time()))

    def followup(self, text, *, nonce, task_id=None, reply_to=None, source="cli", account="local-user", room="local", thread=None, context=False):
        if not isinstance(text, str) or not text.strip() or not nonce:
            raise ValueError("message and nonce required")
        if not self.one("SELECT id FROM rooms WHERE id=? AND owner=?", (room, account)):
            raise ValueError("room not owned by account")
        payload = encode({"text": text, "task_id": task_id, "reply_to": reply_to, "room": room, "thread": thread or room, "context": context})
        with self.transaction():
            old = self.one("SELECT * FROM messages WHERE source=? AND account=? AND client_nonce=?", (source, account, nonce))
            if old:
                if old["payload"] != payload:
                    raise ValueError("nonce already used for different input")
                f = self.one("SELECT * FROM followups WHERE message_id=?", (old["id"],))
                return {"message_id": old["id"], "task_id": old["task_id"], "followup_id": f["id"], "state": f["state"], "duplicate": True}
            target = self._owned(task_id, account, room) if task_id else None
            if not target and reply_to:
                msg = self.one("SELECT task_id FROM messages WHERE id=? AND room_id=? AND account=?", (reply_to, room, account))
                if not msg:
                    raise ValueError("reply reference outside room")
                target = self._owned(msg["task_id"], account, room) if msg["task_id"] else None
            if not target and not context:
                candidates = self.all("SELECT t.id FROM thread_subscriptions s JOIN tasks t ON t.id=s.task_id WHERE s.source=? AND s.account=? AND s.thread=? AND s.mode='own' AND s.active=1 AND t.room_id=? AND t.parent_task_id IS NULL AND t.status NOT IN ('succeeded','cancelled')", (source, account, thread or room, room))
                if len(candidates) == 1:
                    target = self.task(candidates[0]["id"])
            mid, fid = uid(), uid()
            owner = target["id"] if target else None
            state, reason = "pending" if owner else "unassigned", None
            if context:
                state, reason = "context", "background_not_authority"
            elif target and target["cancel_state"] == "cancelled":
                state, reason = "unassigned", "cancelled_task_not_resumed"
                owner = None
            elif target and target["status"] == "succeeded":
                if self._thanks(text):
                    state, reason = "handled", "acknowledgement_no_work"
                else:
                    prior = target["id"]
                    spec = TaskSpec(**target["spec"])
                    spec.objective = text + "\nPrevious task objective (context only): " + spec.objective
                    owner = uid()
                    self.db.execute("INSERT INTO tasks(id,root_task_id,actor_id,room_id,spec,prior_task_id,created) VALUES(?,?,?,?,?,?,?)", (owner, owner, target["actor_id"], room, encode(asdict(spec)), prior, time.time()))
                    target = self.task(owner)
                    self.db.execute("INSERT INTO thread_subscriptions VALUES(?,?,?,?,?,1)", (owner, source, account, thread or room, "own"))
            elif target and target["auto_continue"] == "paused" and text.strip().lower().rstrip("。.!！ ") in {"继续", "继续吧", "请继续", "continue", "resume"}:
                state, reason = "handled", "explicit_user_resume"
                self.db.execute("UPDATE tasks SET auto_continue='on',status=CASE WHEN status='running' THEN status ELSE 'ready' END,reason='user_resume' WHERE id=?", (owner,))
            actor = target["actor_id"] if target else "default"
            self.db.execute("INSERT INTO messages(id,source,account,client_nonce,actor_id,room_id,task_id,kind,content,payload,native_thread_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mid, source, account, nonce, actor, room, owner, "context" if context else "user", text, payload, thread or room))
            eid, revision = None, target["requirement_revision"] if target else None
            if owner and state == "pending":
                revision += 1
                self.db.execute("UPDATE tasks SET requirement_revision=?,auto_continue='on',acceptance='pending',status=CASE WHEN status='running' THEN status ELSE 'ready' END WHERE id=?", (revision, owner))
                self._invalidate_questions(owner)
                eid = self._event(owner, "followup", text, message_id=mid)
            elif owner and state == "context":
                eid = self._event(owner, "context", text, message_id=mid)
                self.db.execute("UPDATE events SET state='background' WHERE id=?", (eid,))
            elif owner and reason == "explicit_user_resume":
                eid = self._event(owner, "user_resume", text, message_id=mid)
            for subscription in self.all("SELECT task_id FROM thread_subscriptions WHERE source=? AND account=? AND thread=? AND mode='context' AND active=1", (source, account, thread or room)):
                if subscription["task_id"] != owner and self.task(subscription["task_id"])["room_id"] == room:
                    context_id = self._event(subscription["task_id"], "context", text, message_id=mid)
                    self.db.execute("UPDATE events SET state='background' WHERE id=?", (context_id,))
            self.db.execute("INSERT INTO followups(id,message_id,event_id,owner_task,state,revision,reason,created) VALUES(?,?,?,?,?,?,?,?)", (fid, mid, eid, owner, state, revision, reason, time.time()))
            self._history(fid)
            if state == "unassigned":
                self._notice(None, room, "这条补充属于哪项任务？请选择后继续。", kind="clarification")
        return {"message_id": mid, "task_id": owner, "followup_id": fid, "state": state, "duplicate": False}

    def assign_followup(self, followup_id, task_id, *, account="local-user"):
        with self.transaction():
            t = self._owned(task_id, account)
            f = self.one("SELECT f.*,m.room_id,m.account FROM followups f JOIN messages m ON m.id=f.message_id WHERE f.id=?", (followup_id,))
            if not f or f["account"] != account or f["room_id"] != t["room_id"]:
                raise ValueError("followup outside account/room")
            if f["owner_task"] or f["state"] not in {"unassigned", "handed_back"} or t["status"] in {"succeeded", "cancelled"}:
                raise ValueError("followup cannot be assigned")
            revision = t["requirement_revision"] + 1
            self.db.execute("UPDATE tasks SET requirement_revision=?,auto_continue='on',status=CASE WHEN status='running' THEN status ELSE 'ready' END WHERE id=?", (revision, task_id))
            self._invalidate_questions(task_id)
            eid = self._event(task_id, "followup", "", message_id=f["message_id"])
            self.db.execute("UPDATE messages SET task_id=?,actor_id=? WHERE id=?", (task_id, t["actor_id"], f["message_id"]))
            self.db.execute("UPDATE followups SET owner_task=?,state='pending',generation=generation+1,revision=?,event_id=? WHERE id=?", (task_id, revision, eid, followup_id))
            self._history(followup_id, "user_assignment")
        return self.one("SELECT * FROM followups WHERE id=?", (followup_id,))

    def _valid_followup(self, run, followup_id, generation):
        if not self.current(run):
            raise ValueError("stale run")
        f = self.one("SELECT * FROM followups WHERE id=?", (followup_id,))
        if not f or f["owner_task"] != run["task_id"] or f["generation"] != generation or f["state"] not in {"pending", "in_progress"}:
            raise ValueError("stale followup owner or generation")
        if not self.one("SELECT request_id FROM input_usage WHERE message_id=? AND run_id=? AND state='completed'", (f["message_id"], run["id"])):
            raise ValueError("followup not in a completed model request")
        return f

    def resolve_followup(self, run, *, followup_id, generation, evidence_ref):
        with self.transaction():
            self._valid_followup(run, followup_id, generation)
            receipts = [o for o in self.operations(run["task_id"]) if o["run_id"] == run["id"] and o["tool"] == "write_artifact" and o["status"] == "succeeded" and (o["result"] or {}).get("artifact_ref") == evidence_ref]
            if not receipts:
                raise ValueError("followup needs a current-run artifact receipt")
            self.db.execute("UPDATE followups SET state='handled',evidence_ref=? WHERE id=?", (evidence_ref, followup_id))
            self._history(followup_id, "artifact_evidence")
        return {"followup_id": followup_id, "state": "handled", "evidence_ref": evidence_ref}

    def handback_followup(self, run, *, followup_id, generation, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("handback requires reason")
        with self.transaction():
            f = self._valid_followup(run, followup_id, generation)
            self.db.execute("UPDATE followups SET state='handed_back',reason=? WHERE id=?", (reason, followup_id))
            self._history(followup_id)
            self.db.execute("UPDATE followups SET state='unassigned',owner_task=NULL,generation=generation+1 WHERE id=?", (followup_id,))
            self._history(followup_id)
            self.db.execute("UPDATE events SET state='archived' WHERE id=?", (f["event_id"],))
            self._notice(None, self.task(run["task_id"])["room_id"], "这项补充需要重新分配：" + reason, kind="clarification")
        return {"followup_id": followup_id, "state": "unassigned"}

    def orchestration_context(self, run):
        inputs = self.all("SELECT e.id AS event_id,e.kind,m.id AS message_id,m.content FROM events e JOIN messages m ON m.id=e.message_id WHERE e.claimed_by_run=? ORDER BY e.created,e.id", (run["id"],))
        followups = self.all("SELECT f.*,m.content FROM followups f JOIN messages m ON m.id=f.message_id JOIN events e ON e.id=f.event_id WHERE f.owner_task=? AND e.claimed_by_run=? AND f.state IN ('pending','in_progress') ORDER BY f.created", (run["task_id"], run["id"]))
        children = self.all("SELECT id,status,reason,acceptance,required,parent_revision,worker_key FROM tasks WHERE parent_task_id=? ORDER BY created", (run["task_id"],))
        attentions = self.all("SELECT id,question,answer,state,kind,revision FROM attentions WHERE task_id=? ORDER BY created", (run["task_id"],))
        return {"inputs": inputs, "followups": followups, "children": children, "attentions": attentions}

    def claim(self, exclude_actor_ids=()):
        self.tick_waits()
        with self.transaction():
            excluded = tuple(exclude_actor_ids)
            actor_filter = (" AND e.actor_id NOT IN (" + ",".join("?" for _ in excluded) + ")") if excluded else ""
            event = self.one("""SELECT e.* FROM events e WHERE e.state='pending' AND e.kind!='conversation'
                AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.actor_id=e.actor_id AND r.phase='running')
                AND (e.task_id IS NULL OR EXISTS(SELECT 1 FROM tasks t WHERE t.id=e.task_id AND t.status='ready' AND t.auto_continue='on' AND t.cancel_state='active'))
                """ + actor_filter + " ORDER BY CASE WHEN e.kind IN ('followup','answer','user_task','chat') THEN 0 ELSE 1 END,e.created,e.id LIMIT 1", excluded)
            if not event:
                return None
            tid, rid = event["task_id"], uid()
            epoch, revision = None, None
            if tid:
                t = self.task(tid)
                continuation = event["kind"] in {"worker_result", "wait_timeout", "nudge"}
                if continuation and t["continuations"] >= t["spec"].get("max_continuations", 12):
                    self.db.execute("UPDATE tasks SET status='paused',auto_continue='paused',reason='continuation_budget' WHERE id=?", (tid,))
                    self._notice(tid, t["room_id"], "已达到自动续接次数预算，已有结果已保存。")
                    return None
                self.db.execute("UPDATE tasks SET execution_epoch=execution_epoch+1,status='running',continuations=continuations+?,wait_spec=NULL WHERE id=?", (int(continuation), tid))
                t = self.task(tid)
                epoch, revision = t["execution_epoch"], t["requirement_revision"]
            self.db.execute("INSERT INTO runs(id,actor_id,task_id,trigger,epoch,revision,phase,started) VALUES(?,?,?,?,?,?,?,?)", (rid, event["actor_id"], tid, event["kind"], epoch, revision, "running", time.time()))
            selected = self.all("""SELECT e.id,length(m.content) AS chars FROM events e JOIN messages m ON m.id=e.message_id WHERE e.task_id=? AND e.actor_id=? AND
                (e.state IN ('pending','background') OR EXISTS(SELECT 1 FROM followups f WHERE f.event_id=e.id AND f.owner_task=e.task_id AND f.state IN ('pending','in_progress')))
                ORDER BY CASE WHEN e.state='background' THEN 1 ELSE 0 END,e.created,e.id LIMIT 50""", (tid, event["actor_id"])) if tid else [{"id": event["id"]}]
            selected_chars = 0
            for e in selected:
                if selected_chars and selected_chars + e.get("chars", 0) > 24000:
                    break
                selected_chars += e.get("chars", 0)
                self.db.execute("UPDATE events SET state='claimed',claimed_by_run=? WHERE id=?", (rid, e["id"]))
                self.db.execute("UPDATE followups SET state='in_progress' WHERE event_id=? AND state='pending'", (e["id"],))
            return self.one("SELECT * FROM runs WHERE id=?", (rid,))

    def current(self, run):
        if not super().current(run):
            return False
        r = self.one("SELECT stop_requested_at FROM runs WHERE id=?", (run["id"],))
        return r["stop_requested_at"] is None and (not run["task_id"] or self.task(run["task_id"])["cancel_state"] == "active")

    def control_reason(self, run):
        r = self.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        if not r or r["phase"] != "running":
            return None
        if r["stop_reason"]:
            return r["stop_reason"]
        if run["task_id"]:
            t = self.task(run["task_id"])
            if t["cancel_state"] == "cancelled":
                return "cancel"
            if t["requirement_revision"] != run["revision"]:
                return "requirements_updated"
        return None

    def remaining_team_seconds(self, root_id):
        root = self.task(root_id)
        elapsed = sum(max(0, time.time() - r["started"]) for r in self.all("SELECT r.started FROM runs r JOIN tasks t ON t.id=r.task_id WHERE t.root_task_id=? AND r.phase='running'", (root_id,)))
        return max(0, root["spec"]["max_seconds"] - root["active_seconds"] - elapsed)

    def request_control(self, task_id, kind, *, run_id=None, account="local-user"):
        if kind not in {"stop_reply", "stop_task", "cancel"}:
            raise ValueError("invalid control")
        with self.transaction():
            task = self._owned(task_id, account)
            active = self.one("SELECT * FROM runs WHERE task_id=? AND phase='running'", (task_id,))
            if run_id and (not active or active["id"] != run_id):
                return {"task_id": task_id, "state": "ignored_stale_run"}
            targets = self.all("SELECT id FROM tasks WHERE root_task_id=?", (task["root_task_id"],)) if kind == "cancel" else [{"id": task_id}]
            cid = uid()
            for item in targets:
                tid = item["id"]
                t = self.task(tid)
                if t["status"] == "succeeded":
                    continue
                r = self.one("SELECT id FROM runs WHERE task_id=? AND phase='running'", (tid,))
                if kind == "cancel":
                    self.db.execute("UPDATE tasks SET cancel_state='cancelled',auto_continue='forbidden',reason='cancel_requested' WHERE id=?", (tid,))
                    self._invalidate_questions(tid)
                elif kind == "stop_reply":
                    self.db.execute("UPDATE tasks SET auto_continue='paused',reason='stop_requested' WHERE id=?", (tid,))
                if r:
                    self.db.execute("UPDATE runs SET stop_requested_at=?,stop_reason=? WHERE id=?", (time.time(), kind, r["id"]))
                else:
                    state = "cancelled" if kind == "cancel" else "paused" if kind == "stop_reply" else "stopped"
                    self.db.execute("UPDATE tasks SET status=?,reason=? WHERE id=?", (state, kind, tid))
                    if t["parent_task_id"]:
                        self._child_event(tid)
            self.db.execute("INSERT INTO controls VALUES(?,?,?,?,?,?,?)", (cid, task_id, active["id"] if active else None, kind, "requested" if active else "stopped", time.time(), None if active else time.time()))
        return {"control_id": cid, "task_id": task_id, "state": "stopping" if active else "stopped"}

    def ack_control(self, run, reason):
        with self.transaction():
            r = self.one("SELECT * FROM runs WHERE id=?", (run["id"],))
            if not r or r["phase"] != "running":
                return {"state": "ignored_stale_run"}
            self.db.execute("UPDATE runs SET phase='ended',end_reason=?,ended=? WHERE id=?", (reason, time.time(), run["id"]))
            self.db.execute("UPDATE operations SET status='interrupted' WHERE run_id=? AND status='running'", (run["id"],))
            self.db.execute("UPDATE model_requests SET state='unknown',ended=? WHERE run_id=? AND state='dispatching'", (time.time(), run["id"]))
            self.db.execute("UPDATE input_usage SET state='unknown' WHERE run_id=? AND state='dispatching'", (run["id"],))
            self.db.execute("UPDATE events SET state='pending',claimed_by_run=NULL WHERE claimed_by_run=? AND state='claimed'", (run["id"],))
            self.db.execute("UPDATE events SET state='background' WHERE kind='context' AND state='pending'")
            if run["task_id"]:
                t = self.task(run["task_id"])
                if t["execution_epoch"] == run["epoch"]:
                    resumed = t["auto_continue"] == "on" and (t["requirement_revision"] != run["revision"] or t["reason"] == "user_resume")
                    state = "cancelled" if t["cancel_state"] == "cancelled" else "paused" if reason == "team_time_budget" else "ready" if reason == "requirements_updated" or resumed else "paused" if reason == "stop_reply" else "stopped"
                    self._charge_elapsed(t, run)
                    self.db.execute("UPDATE tasks SET status=?,reason=? WHERE id=?", (state, reason, t["id"]))
                    if state == "cancelled":
                        self.db.execute("UPDATE events SET state='archived' WHERE task_id=? AND state='pending'", (t["id"],))
                    if t["parent_task_id"] and state != "ready":
                        self._child_event(t["id"])
            self.db.execute("UPDATE controls SET state='stopped',ended=? WHERE run_id=? AND state='requested'", (time.time(), run["id"]))
        return {"state": "stopped"}

    def _charge_elapsed(self, task, run):
        elapsed = min(max(0, time.time() - run["started"]), max(0, task["spec"]["max_seconds"] - task["active_seconds"]))
        self.db.execute("UPDATE tasks SET active_seconds=active_seconds+? WHERE id=?", (elapsed, task["id"]))
        if task["parent_task_id"]:
            root = self.task(task["root_task_id"])
            charge = min(elapsed, max(0, root["spec"]["max_seconds"] - root["active_seconds"]))
            self.db.execute("UPDATE tasks SET active_seconds=active_seconds+? WHERE id=?", (charge, root["id"]))

    def reserve(self, run, kind):
        column, limit = {"model": ("calls_used", "max_calls"), "tool": ("tools_used", "max_tools")}[kind]
        with self.transaction():
            if not self.current(run):
                return False
            t = self.task(run["task_id"])
            root = self.task(t["root_task_id"])
            if (t[column] >= t["spec"][limit] or root[column] >= root["spec"][limit]
                    or self.remaining_team_seconds(root["id"]) <= 0 or root["cancel_state"] != "active"):
                return False
            self.db.execute(f"UPDATE tasks SET {column}={column}+1 WHERE id=?", (t["id"],))
            if root["id"] != t["id"]:
                self.db.execute(f"UPDATE tasks SET {column}={column}+1 WHERE id=?", (root["id"],))
        return True

    def spawn_worker(self, run, *, worker_key, objective, source_ids, required_text=None, max_calls=8, required=True, depends_on=None, target_actor_id=None):
        with self.transaction():
            if not self.current(run):
                raise ValueError("stale run")
            parent = self.task(run["task_id"])
            if target_actor_id is not None:
                self.require_member(target_actor_id, parent["room_id"])
                if target_actor_id == parent["actor_id"]:
                    raise ValueError("delegate to another member or use a temporary worker")
            if parent["parent_task_id"]:
                raise ValueError("only one worker layer is allowed")
            if not isinstance(worker_key, str) or not worker_key.strip() or len(worker_key) > 100 or type(required) is not bool:
                raise ValueError("invalid worker identity")
            sources = {s["id"]: s for s in parent["spec"]["sources"]}
            if not isinstance(source_ids, list) or any(s not in sources for s in source_ids) or len(set(source_ids)) != len(source_ids):
                raise ValueError("worker source grant must be a subset")
            spec = TaskSpec(objective, [sources[s] for s in source_ids], required_text or [], max_calls=max_calls,
                            max_tools=min(40, parent["spec"]["max_tools"]), max_seconds=parent["spec"]["max_seconds"], max_workers=0,
                            enable_nudge=parent["spec"].get("enable_nudge", False), max_nudges=parent["spec"].get("max_nudges", 2))
            spec.validate()
            depends_on = [] if depends_on is None else depends_on
            self.validate_dependencies(parent, depends_on)
            old = self.one("SELECT id,spec,required,parent_revision,dependencies,actor_id FROM tasks WHERE parent_task_id=? AND worker_key=?", (parent["id"], worker_key))
            if old:
                if old["spec"] != encode(asdict(spec)) or bool(old["required"]) != required or old["parent_revision"] != parent["requirement_revision"] or json.loads(old["dependencies"]) != depends_on:
                    raise ValueError("worker key conflicts with existing work/version")
                if target_actor_id and target_actor_id != old["actor_id"]:
                    raise ValueError("worker key belongs to another actor")
                return {"task_id": old["id"], "status": self.task(old["id"])["status"], "duplicate": True}
            if self.one("SELECT COUNT(*) n FROM tasks WHERE parent_task_id=?", (parent["id"],))["n"] >= parent["spec"].get("max_workers", 4):
                raise ValueError("worker budget exhausted")
            tid, actor = uid(), target_actor_id or "worker-" + uid()
            if target_actor_id is None:
                self.db.execute("INSERT INTO actors(id,kind) VALUES(?,'worker')", (actor,))
            status = "ready" if self.dependencies_accepted(parent, depends_on) else "waiting_dependencies"
            self.db.execute("INSERT INTO tasks(id,root_task_id,parent_task_id,actor_id,room_id,spec,parent_revision,required,worker_key,created,dependencies,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (tid, parent["root_task_id"], parent["id"], actor, parent["room_id"], encode(asdict(spec)), run["revision"], int(required), worker_key, time.time(), encode(depends_on), status))
            self._event(tid, "worker_start", objective)
        return {"task_id": tid, "status": status, "duplicate": False}

    def worker_result(self, run, child_id):
        if not self.current(run):
            raise ValueError("stale run")
        child = self.task(child_id)
        if child["parent_task_id"] != run["task_id"] or child["room_id"] != self.task(run["task_id"])["room_id"]:
            raise ValueError("worker result outside assignment")
        delivery = self.one("SELECT content,artifact FROM deliveries WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (child_id,))
        if delivery and delivery["artifact"]:
            delivery["artifact"] = json.loads(delivery["artifact"])
        return {"task": child, "task_id": child_id, "status": child["status"], "acceptance": child["acceptance"], "parent_revision": child["parent_revision"], "stale": child["parent_revision"] != run["revision"], "delivery": delivery, "operations": self.operations(child_id)}

    def adopt_worker_result(self, run, *, child_id, artifact_sha256, rationale):
        """Review immutable prior evidence against the current requirements."""
        with self.transaction():
            result = self.worker_result(run, child_id)
            child = result["task"]
            artifact = (result["delivery"] or {}).get("artifact") or {}
            if child["status"] != "succeeded" or child["acceptance"] != "passed" or artifact.get("sha256") != artifact_sha256:
                raise ValueError("child needs an accepted artifact with this hash")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("review needs a concrete rationale")
            reads = [op for op in self.operations(run["task_id"]) if op["run_id"] == run["id"] and op["tool"] == "read_worker_result" and op["status"] == "succeeded" and op["arguments"].get("child_id") == child_id and (op["result"] or {}).get("artifact_text") and ((op["result"].get("delivery") or {}).get("artifact") or {}).get("sha256") == artifact_sha256]
            if not reads:
                raise ValueError("read the verified child artifact in this run before review")
            self.db.execute("INSERT INTO worker_adoptions VALUES(?,?,?,?,?,?,?) ON CONFLICT(child_id,child_epoch,parent_revision) DO NOTHING", (child_id, child["execution_epoch"], run["revision"], run["id"], artifact_sha256, rationale, time.time()))
        return {"child_id": child_id, "parent_revision": run["revision"], "state": "adopted", "artifact_sha256": artifact_sha256}

    def wait_workers(self, run, *, task_ids=None, mode="all", timeout_seconds=300):
        if mode not in {"all", "any"} or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600:
            raise ValueError("invalid worker wait")
        children = self.all("SELECT id FROM tasks WHERE parent_task_id=?", (run["task_id"],))
        ids = task_ids if task_ids is not None else [c["id"] for c in children]
        if not isinstance(ids, list) or not ids or set(ids) - {c["id"] for c in children}:
            raise ValueError("wait requires assigned children")
        wait = {"task_ids": ids, "mode": mode, "deadline": time.time() + timeout_seconds}
        self.finish(run, status="waiting_children", reason="waiting_children", content="", wait_spec=wait)
        self.tick_waits()
        return {"status": "waiting_children", "task_ids": ids}

    def _child_event(self, tid):
        child = self.task(tid)
        if not child["parent_task_id"]:
            return
        parent = self.task(child["parent_task_id"])
        marker = f"worker-result:{tid}:{child['execution_epoch']}:{child['requirement_revision']}:{child['status']}"
        if self.one("SELECT id FROM messages WHERE source='runtime' AND client_nonce=?", (marker,)):
            return
        eid = self._event(parent["id"], "worker_result", encode({"child_id": tid, "status": child["status"], "parent_revision": child["parent_revision"], "untrusted_peer_data": True}))
        self.db.execute("UPDATE messages SET client_nonce=? WHERE id=(SELECT message_id FROM events WHERE id=?)", (marker, eid))
        if parent["cancel_state"] == "cancelled" or parent["status"] == "succeeded":
            self.db.execute("UPDATE events SET state='archived' WHERE id=?", (eid,))

    def tick_waits(self):
        with self.transaction():
            for row in self.all("SELECT id FROM tasks WHERE status='waiting_children' AND auto_continue='on' AND cancel_state='active'"):
                t = self.task(row["id"])
                wait = t["wait_spec"] or {}
                children = [self.task(tid) for tid in wait.get("task_ids", [])]
                terminal = [c for c in children if c["status"] in TERMINAL]
                failed = any(c["status"] in TERMINAL and c["status"] != "succeeded" for c in children)
                due = time.time() >= wait.get("deadline", time.time())
                ready = due or failed or (bool(terminal) if wait.get("mode") == "any" else len(terminal) == len(children))
                if ready:
                    self.db.execute("UPDATE tasks SET status='ready',reason=? WHERE id=?", ("wait_timeout" if due else "worker_results_ready", t["id"]))
                    if not self.one("SELECT id FROM events WHERE task_id=? AND state='pending'", (t["id"],)):
                        self._event(t["id"], "wait_timeout" if due else "worker_result", "Wait condition satisfied; inspect actual child results.")

    def completion_errors(self, run):
        if not self.current(run):
            return ["stale run or requirements"]
        errors = []
        if self.one("SELECT id FROM followups WHERE owner_task=? AND state IN ('pending','in_progress')", (run["task_id"],)):
            errors.append("unresolved followup responsibilities")
        for c in self.all("SELECT id,status,acceptance,parent_revision,execution_epoch FROM tasks WHERE parent_task_id=? AND required=1", (run["task_id"],)):
            adopted = self.one("SELECT child_id FROM worker_adoptions WHERE child_id=? AND child_epoch=? AND parent_revision=?", (c["id"], c["execution_epoch"], run["revision"]))
            if c["status"] != "succeeded" or c["acceptance"] != "passed" or (c["parent_revision"] != run["revision"] and not adopted):
                errors.append("required child not accepted for current requirements: " + c["id"])
        if self.one("SELECT id FROM attentions WHERE task_id=? AND state='pending'", (run["task_id"],)):
            errors.append("unanswered question")
        return errors

    def finish(self, run, *, status, reason, content, artifact=None, acceptance="pending", before_commit=None, wait_spec=None):
        with self.transaction():
            if not self.current(run):
                raise RuntimeError("stale run cannot finalize")
            if run["task_id"]:
                if status == "succeeded" and self.completion_errors(run):
                    raise ValueError("task has unresolved work: " + "; ".join(self.completion_errors(run)))
                task = self.task(run["task_id"])
                self._charge_elapsed(task, run)
                self.db.execute("UPDATE tasks SET status=?,reason=?,acceptance=?,wait_spec=? WHERE id=?", (status, reason, acceptance, encode(wait_spec) if wait_spec else None, task["id"]))
            self.db.execute("UPDATE operations SET status='interrupted' WHERE run_id=? AND status='running'", (run["id"],))
            self.db.execute("UPDATE runs SET phase='ended',end_reason=?,ended=? WHERE id=?", (reason, time.time(), run["id"]))
            self.db.execute("UPDATE events SET state='consumed',consumed_by_run=? WHERE claimed_by_run=? AND id IN (SELECT event_id FROM input_usage WHERE run_id=? AND state='completed')", (run["id"], run["id"], run["id"]))
            if content:
                room = self.one("SELECT m.room_id FROM messages m JOIN events e ON e.message_id=m.id WHERE e.claimed_by_run=?", (run["id"],))["room_id"]
                self.db.execute("INSERT INTO deliveries(id,task_id,run_id,room_id,content,artifact,created) VALUES(?,?,?,?,?,?,?)", (uid(), run["task_id"], run["id"], room, content, encode(artifact) if artifact else None, time.time()))
            self.db.execute("UPDATE events SET state=?,claimed_by_run=NULL WHERE claimed_by_run=? AND state='claimed'", ("pending" if run["task_id"] else "archived", run["id"]))
            if run["task_id"] and status in TERMINAL:
                self._child_event(run["task_id"])
            if before_commit:
                before_commit()

    def _invalidate_questions(self, tid):
        self.db.execute("UPDATE deliveries SET state='cancelled' WHERE attention_id IN (SELECT id FROM attentions WHERE task_id=? AND state='pending') AND state IN ('pending','failed')", (tid,))
        self.db.execute("UPDATE attentions SET state='superseded' WHERE task_id=? AND state='pending'", (tid,))

    def ask_user(self, run, *, question, options, kind="information"):
        if not isinstance(question, str) or not question.strip() or kind not in {"information", "authorization", "review"}:
            raise ValueError("invalid question")
        if not isinstance(options, list) or len(options) > 4 or any(not isinstance(o, str) or not o.strip() for o in options):
            raise ValueError("invalid answer options")
        if not self.current(run):
            raise ValueError("stale run")
        # Attention and WAIT are committed together; no gap can lose the question.
        with self.transaction():
            t = self.task(run["task_id"])
            account = self.one("SELECT owner FROM rooms WHERE id=?", (t["room_id"],))["owner"]
            self._invalidate_questions(t["id"])
            qid = uid()
            self.db.execute("INSERT INTO attentions(id,task_id,run_id,account,revision,kind,question,options,state,created) VALUES(?,?,?,?,?,?,?,?,?,?)", (qid, t["id"], run["id"], account, run["revision"], kind, question, encode(options), "pending", time.time()))
            did = self._notice(t["id"], t["room_id"], question, kind="question", attention_id=qid)
            self.db.execute("UPDATE attentions SET delivery_id=? WHERE id=?", (did, qid))
            self._charge_elapsed(t, run)
            self.db.execute("UPDATE tasks SET status='waiting_user',reason='question',wait_spec=? WHERE id=?", (encode({"question_id": qid}), t["id"]))
            self.db.execute("UPDATE runs SET phase='ended',end_reason='waiting_user',ended=? WHERE id=?", (time.time(), run["id"]))
            self.db.execute("UPDATE events SET state='consumed',consumed_by_run=? WHERE claimed_by_run=? AND id IN (SELECT event_id FROM input_usage WHERE run_id=? AND state='completed')", (run["id"], run["id"], run["id"]))
            self.db.execute("UPDATE events SET state='pending',claimed_by_run=NULL WHERE claimed_by_run=? AND state='claimed'", (run["id"],))
        return {"question_id": qid, "status": "waiting_user"}

    def answer_attention(self, question_id, answer, *, nonce, account="local-user"):
        if not isinstance(answer, str) or not answer.strip() or not nonce:
            raise ValueError("answer and nonce required")
        payload = encode({"question_id": question_id, "answer": answer})
        with self.transaction():
            old = self.one("SELECT * FROM answer_receipts WHERE account=? AND nonce=?", (account, nonce))
            if old:
                if old["payload"] != payload:
                    raise ValueError("nonce already used for different answer")
                return {**json.loads(old["receipt"]), "duplicate": True}
            q = self.one("SELECT * FROM attentions WHERE id=?", (question_id,))
            if not q or q["account"] != account:
                raise ValueError("question outside account")
            t = self._owned(q["task_id"], account)
            valid = q["state"] == "pending" and q["revision"] == t["requirement_revision"] and t["cancel_state"] == "active"
            if valid:
                self.db.execute("UPDATE attentions SET state='answered',answer=? WHERE id=?", (answer, question_id))
                self.db.execute("UPDATE deliveries SET state='cancelled' WHERE id=? AND state IN ('pending','failed')", (q["delivery_id"],))
                self.db.execute("UPDATE tasks SET status='ready',auto_continue='on',wait_spec=NULL WHERE id=?", (t["id"],))
                self._event(t["id"], "answer", encode({"question_id": question_id, "answer": answer, "kind": q["kind"]}))
            receipt = {"question_id": question_id, "task_id": t["id"], "state": "answered" if valid else "ignored_stale_answer", "duplicate": False}
            self.db.execute("INSERT INTO answer_receipts VALUES(?,?,?,?)", (account, nonce, payload, encode(receipt)))
        return receipt

    def replace_attention(self, question_id, question, options, *, account="local-user"):
        if not isinstance(question, str) or not question.strip() or not isinstance(options, list) or len(options) > 4 or any(not isinstance(o, str) for o in options):
            raise ValueError("invalid question")
        with self.transaction():
            q = self.one("SELECT * FROM attentions WHERE id=?", (question_id,))
            if not q or q["account"] != account or q["state"] != "pending":
                raise ValueError("question not pending for account")
            t = self._owned(q["task_id"], account)
            self._invalidate_questions(t["id"])
            qid = uid()
            did = self._notice(t["id"], t["room_id"], question, kind="question", attention_id=qid)
            self.db.execute("INSERT INTO attentions(id,task_id,run_id,account,revision,kind,question,options,state,delivery_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (qid, t["id"], q["run_id"], account, t["requirement_revision"], q["kind"], question, encode(options), "pending", did, time.time()))
            self.db.execute("UPDATE tasks SET wait_spec=? WHERE id=?", (encode({"question_id": qid}), t["id"]))
        return {"question_id": qid, "status": "waiting_user"}

    def resume(self, tid, *, extra_calls=0, extra_tools=0, extra_seconds=0):
        t = self.task(tid)
        if t["cancel_state"] != "active":
            raise ValueError("cancelled task cannot resume")
        if t["status"] == "stopped":
            self.db.execute("UPDATE tasks SET status='paused' WHERE id=?", (tid,))
        result = super().resume(tid, extra_calls=extra_calls, extra_tools=extra_tools, extra_seconds=extra_seconds)
        self.db.execute("UPDATE tasks SET auto_continue='on' WHERE id=?", (tid,))
        return result

    def recover(self):
        # Honor already-persisted control/revision changes before generic recovery.
        for run in self.all("SELECT * FROM runs WHERE phase='running'"):
            if reason := self.control_reason(run):
                self.ack_control(run, reason)
        count = super().recover()
        with self.transaction():
            self.db.execute("UPDATE followups SET state='pending' WHERE state='in_progress'")
            self.db.execute("UPDATE events SET state='background' WHERE kind='context' AND state='pending'")
        self.tick_waits()
        return count
