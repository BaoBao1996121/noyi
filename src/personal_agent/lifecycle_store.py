"""M2 progress, ending gates, review, handoff and immutable artifact versions."""
from __future__ import annotations

from dataclasses import asdict
import json
import time
import uuid

from .contracts import TaskSpec, digest, encode


class LifecycleStore:
    def __init__(self, path):
        super().__init__(path)
        with self.transaction():
            present = {r["name"] for r in self.all("PRAGMA table_info(tasks)")}
            for name, definition in {"stagnant_tools": "INTEGER NOT NULL DEFAULT 0", "failure_streak": "INTEGER NOT NULL DEFAULT 0", "nudge_count": "INTEGER NOT NULL DEFAULT 0", "dependencies": "TEXT NOT NULL DEFAULT '[]'"}.items():
                if name not in present:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            for sql in (
                "CREATE TABLE IF NOT EXISTS activity_log(operation_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT NOT NULL,tool TEXT NOT NULL,outcome TEXT NOT NULL,new_progress INTEGER NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS progress_log(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT NOT NULL,kind TEXT NOT NULL,fingerprint TEXT NOT NULL,evidence_ref TEXT NOT NULL,summary TEXT NOT NULL,created REAL NOT NULL,UNIQUE(task_id,kind,fingerprint))",
                "CREATE TABLE IF NOT EXISTS investigation(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT NOT NULL,kind TEXT NOT NULL,statement TEXT NOT NULL,evidence_refs TEXT NOT NULL,created REAL NOT NULL,UNIQUE(task_id,kind,statement))",
                "CREATE TABLE IF NOT EXISTS nudges(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,run_id TEXT NOT NULL,revision INTEGER NOT NULL,reason TEXT NOT NULL,gaps TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS artifact_versions(task_id TEXT NOT NULL,name TEXT NOT NULL,version INTEGER NOT NULL,operation_id TEXT NOT NULL UNIQUE,run_id TEXT NOT NULL,revision INTEGER NOT NULL,base_version INTEGER NOT NULL,artifact_ref TEXT NOT NULL,sha256 TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(task_id,name,version))",
                "CREATE TABLE IF NOT EXISTS artifact_heads(task_id TEXT NOT NULL,name TEXT NOT NULL,version INTEGER NOT NULL,operation_id TEXT NOT NULL,PRIMARY KEY(task_id,name))",
                "CREATE TABLE IF NOT EXISTS worker_reviews(id TEXT PRIMARY KEY,parent_id TEXT NOT NULL,child_id TEXT NOT NULL,child_epoch INTEGER NOT NULL,parent_revision INTEGER NOT NULL,decision TEXT NOT NULL,rationale TEXT NOT NULL,artifact_sha256 TEXT NOT NULL,run_id TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS handoffs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,from_actor TEXT NOT NULL,to_actor TEXT NOT NULL,run_id TEXT NOT NULL,epoch INTEGER NOT NULL,revision INTEGER NOT NULL,reason TEXT NOT NULL,manifest TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS dependency_reads(task_id TEXT NOT NULL,revision INTEGER NOT NULL,dependency_id TEXT NOT NULL,dependency_epoch INTEGER NOT NULL,parent_revision INTEGER NOT NULL,sha256 TEXT NOT NULL,operation_id TEXT NOT NULL,PRIMARY KEY(task_id,revision,dependency_id,dependency_epoch,parent_revision))",
            ):
                self.db.execute(sql)
            self.db.execute("PRAGMA user_version=4")
            # M0/M1 immutable writes keep their original run/revision on upgrade.
            for op in self.all("SELECT o.*,r.revision FROM operations o JOIN runs r ON r.id=o.run_id WHERE o.tool='write_artifact' AND o.status='succeeded' AND NOT EXISTS(SELECT 1 FROM artifact_versions v WHERE v.operation_id=o.id) ORDER BY o.created,o.id"):
                args, receipt = json.loads(op["arguments"]), json.loads(op["result"] or "{}")
                if not isinstance(args, dict) or not all(k in receipt for k in ("artifact_ref", "sha256")):
                    continue
                head = self.one("SELECT version FROM artifact_heads WHERE task_id=? AND name=?", (op["task_id"], args["name"]))
                base = head["version"] if head else 0
                self.db.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?,?)", (op["task_id"], args["name"], base+1, op["id"], op["run_id"], op["revision"], base, receipt["artifact_ref"], receipt["sha256"], op["created"]))
                self.db.execute("INSERT INTO artifact_heads VALUES(?,?,?,?) ON CONFLICT(task_id,name) DO UPDATE SET version=excluded.version,operation_id=excluded.operation_id", (op["task_id"], args["name"], base+1, op["id"]))

    def record_activity(self, run, operation_id):
        with self.transaction():
            if self.one("SELECT operation_id FROM activity_log WHERE operation_id=?", (operation_id,)):
                return
            op = next(o for o in self.operations(run["task_id"]) if o["id"] == operation_id)
            result, args, tool = op["result"] or {}, op["arguments"], op["tool"]
            failed = op["status"] != "succeeded" or result.get("passed") is False
            kind, fingerprint, summary = None, None, ""
            if tool == "read_source" and not failed:
                kind = "source_coverage"
                fingerprint = encode([result["source_id"], result["sha256"], result["start"], result["end"]])
                summary = encode({k: result[k] for k in ("source_id", "start", "end", "total", "eof", "sha256")})
            elif tool == "read_source" and result.get("error") == "SourceUnavailable":
                kind, fingerprint, summary = "negative_evidence", args["source_id"], "Authorized source unavailable: " + args["source_id"]
            elif tool == "read_review_evidence" and not failed:
                page = result.get("artifact_page", {})
                kind = "review_evidence"
                fingerprint = encode([result["snapshot_id"], result["evidence_id"], page.get("sha256"), page.get("start"), page.get("end")])
                summary = encode({"snapshot_id": result["snapshot_id"], "evidence_id": result["evidence_id"], "start": page.get("start"), "end": page.get("end")})
            elif tool in {"read_worker_result", "read_dependency"} and not failed:
                kind = "child_observation"
                child_id = args.get("child_id", args.get("dependency_id"))
                fingerprint = encode([child_id, result.get("status"), ((result.get("delivery") or {}).get("artifact") or {}).get("sha256")])
                summary = encode({"child_id": child_id, "status": result.get("status")})
                if tool == "read_dependency" and result.get("artifact_text"):
                    parent_revision = self.task(self.task(run["task_id"])["parent_task_id"])["requirement_revision"]
                    self.db.execute("INSERT OR REPLACE INTO dependency_reads VALUES(?,?,?,?,?,?,?)", (run["task_id"], run["revision"], child_id, result["task"]["execution_epoch"], parent_revision, result["delivery"]["artifact"]["sha256"], operation_id))
            elif tool in {"adopt_worker_result", "request_rework", "resolve_followup", "handback_followup"} and not failed:
                kind, fingerprint, summary = "responsibility", encode(result), encode(result)
            elif tool == "submit_result" and result.get("passed"):
                kind, fingerprint, summary = "accepted_artifact", result["artifact"]["sha256"], encode(result["artifact"])
            progress = False
            if kind:
                cursor = self.db.execute("INSERT OR IGNORE INTO progress_log VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["task_id"], run["id"], kind, fingerprint, operation_id, summary, time.time()))
                progress = cursor.rowcount == 1
            self.db.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?,?)", (operation_id, run["task_id"], run["id"], tool, "failure" if failed else "success", int(progress), time.time()))
            # Artifact edits or invented hypotheses alone do not reset stagnation.
            self.db.execute("UPDATE tasks SET stagnant_tools=CASE WHEN ? THEN 0 ELSE stagnant_tools+1 END,failure_streak=CASE WHEN ? THEN failure_streak+1 ELSE 0 END WHERE id=?", (int(progress), int(failed), run["task_id"]))

    def resume(self, tid, **kwargs):
        # An explicit user retry starts a fresh consecutive-failure window.
        # Lifetime model/tool/time usage and all failure evidence remain intact.
        with self.transaction():
            result = super().resume(tid, **kwargs)
            self.db.execute("UPDATE tasks SET stagnant_tools=0,failure_streak=0 WHERE id=?", (tid,))
        return result

    def record_investigation(self, run, *, kind, statement, evidence_refs):
        if not self.current(run) or kind not in {"hypothesis", "ruled_out", "coverage", "unknown"} or not isinstance(statement, str) or not statement.strip() or len(statement) > 1500:
            raise ValueError("invalid investigation note or stale run")
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 20:
            raise ValueError("investigation needs scoped evidence operation IDs")
        valid = {o["id"] for o in self.operations(run["task_id"]) if o["status"] in {"succeeded", "failed"}}
        if any(ref not in valid for ref in evidence_refs):
            raise ValueError("investigation evidence outside this task")
        if self.one("SELECT COUNT(*) n FROM investigation WHERE task_id=?", (run["task_id"],))["n"] >= 16 and not self.one("SELECT id FROM investigation WHERE task_id=? AND kind=? AND statement=?", (run["task_id"], kind, statement)):
            raise ValueError("investigation note budget exhausted; consolidate in an artifact")
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO investigation VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["task_id"], run["id"], kind, statement, encode(evidence_refs), time.time()))
        return {"kind": kind, "statement": statement, "evidence_refs": evidence_refs, "trust": "agent interpretation linked to receipts; not verified truth"}

    def lifecycle_context(self, run):
        tid = run["task_id"]
        task = self.task(tid)
        return {"requirements_revision": run["revision"], "activity_count": self.one("SELECT COUNT(*) n FROM activity_log WHERE task_id=?", (tid,))["n"],
                "stagnant_tools": task["stagnant_tools"], "failure_streak": task["failure_streak"], "nudge_count": task["nudge_count"],
                "progress": self.all("SELECT kind,evidence_ref,summary FROM progress_log WHERE task_id=? ORDER BY created", (tid,)),
                "investigation": self.all("SELECT kind,statement,evidence_refs FROM investigation WHERE task_id=? ORDER BY created", (tid,)),
                "artifact_heads": self.all("SELECT h.name,h.version,v.artifact_ref,v.sha256 FROM artifact_heads h JOIN artifact_versions v ON v.operation_id=h.operation_id WHERE h.task_id=?", (tid,)),
                "reviews": self.all("SELECT child_id,child_epoch,parent_revision,decision,rationale FROM worker_reviews WHERE parent_id=? ORDER BY created", (tid,)),
                "handoffs": self.all("SELECT from_actor,to_actor,reason FROM handoffs WHERE task_id=? ORDER BY created", (tid,)),
                "eligible_team_actors": self.all("SELECT DISTINCT actor_id FROM tasks WHERE root_task_id=? AND room_id=?", (task["root_task_id"], task["room_id"])),
                "dependencies": json.loads(task["dependencies"]),
                "pending_inputs_not_in_this_run": self.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND state='pending'", (tid,))["n"],
                "budget": {k: task[k] for k in ("calls_used", "tools_used", "active_seconds")}}

    def ending_gaps(self, run):
        gaps = self.completion_errors(run)
        task = self.task(run["task_id"])
        observed = set()
        operations = self.operations(task["id"])
        for source in task["spec"]["sources"]:
            source_reads = [o["result"] for o in operations if o["tool"] == "read_source" and o["status"] == "succeeded" and (o["result"] or {}).get("source_id") == source["id"]]
            if source_reads and len({r["sha256"] for r in source_reads}) == 1:
                cursor = 0
                for result in sorted(source_reads, key=lambda r: r["start"]):
                    if result["start"] <= cursor:
                        cursor = max(cursor, result["end"])
                if cursor == source_reads[0]["total"]:
                    observed.add(source["id"])
        negative = {p["fingerprint"] for p in self.all("SELECT fingerprint FROM progress_log WHERE task_id=? AND kind='negative_evidence'", (task["id"],))}
        for source in task["spec"]["sources"]:
            if source["id"] not in observed and source["id"] not in negative:
                gaps.append("authorized source not yet investigated: " + source["id"])
        gaps.append("No final artifact has passed current task acceptance; use receipts and remaining authorized routes, or state a concrete blocker.")
        return gaps

    def waiting_children_at_end(self, run):
        return [c["id"] for c in self.all("SELECT id,status FROM tasks WHERE parent_task_id=? AND required=1", (run["task_id"],)) if c["status"] in {"ready", "running", "waiting_children", "waiting_dependencies", "waiting_user"}]

    def issue_nudge(self, run, reason):
        with self.transaction():
            if not self.current(run):
                return None
            task = self.task(run["task_id"])
            spec = task["spec"]
            root = self.task(task["root_task_id"])
            # Nudge is a continuation, never a budget refill or a way around waits.
            if (not spec.get("enable_nudge", False) or task["auto_continue"] != "on"
                    or task["nudge_count"] >= spec.get("max_nudges", 2)
                    or root["nudge_count"] >= root["spec"].get("max_nudges", 2)
                    or root["calls_used"] >= root["spec"]["max_calls"]
                    or self.remaining_team_seconds(root["id"]) <= 0):
                return None
            gaps = self.ending_gaps(run)
            nid = uuid.uuid4().hex
            self.db.execute("INSERT INTO nudges VALUES(?,?,?,?,?,?,?)", (nid, task["id"], run["id"], run["revision"], reason, encode(gaps), time.time()))
            self.db.execute("UPDATE tasks SET nudge_count=nudge_count+1 WHERE id=?", (task["id"],))
            if task["id"] != root["id"]:
                self.db.execute("UPDATE tasks SET nudge_count=nudge_count+1 WHERE id=?", (root["id"],))
        return {"id": nid, "reason": reason, "gaps": gaps}

    def nudge_context(self, run):
        return [dict(n, gaps=json.loads(n["gaps"])) for n in self.all("SELECT id,reason,gaps FROM nudges WHERE task_id=? AND revision=? ORDER BY created", (run["task_id"], run["revision"]))]

    def check_artifact_version(self, run, op_id, args):
        if not self.current(run):
            raise ValueError("stale run cannot write an artifact")
        old = self.one("SELECT * FROM artifact_versions WHERE operation_id=?", (op_id,))
        if old:
            return old["base_version"]
        head = self.one("SELECT version FROM artifact_heads WHERE task_id=? AND name=?", (run["task_id"], args["name"]))
        version = head["version"] if head else 0
        base = args.get("base_version", 0)
        if base != version:
            raise ValueError(f"artifact version conflict: current base_version is {version}; read current version before revising")
        return version

    def record_artifact_version(self, run, op_id, args, receipt):
        with self.transaction():
            base = self.check_artifact_version(run, op_id, args)
            old = self.one("SELECT * FROM artifact_versions WHERE operation_id=?", (op_id,))
            if old:
                if old["sha256"] != receipt["sha256"] or old["artifact_ref"] != receipt["artifact_ref"]:
                    raise ValueError("artifact recovery receipt conflicts")
                return {**receipt, "version": old["version"], "name": old["name"]}
            original = self.one("SELECT r.id,r.revision FROM operations o JOIN runs r ON r.id=o.run_id WHERE o.id=?", (op_id,)) or {"id": run["id"], "revision": run["revision"]}
            self.db.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?,?)", (run["task_id"], args["name"], base + 1, op_id, original["id"], original["revision"], base, receipt["artifact_ref"], receipt["sha256"], time.time()))
            self.db.execute("INSERT INTO artifact_heads VALUES(?,?,?,?) ON CONFLICT(task_id,name) DO UPDATE SET version=excluded.version,operation_id=excluded.operation_id", (run["task_id"], args["name"], base + 1, op_id))
        return {**receipt, "version": base + 1, "name": args["name"]}

    def artifact_version_errors(self, run, artifact_ref):
        version = self.one("SELECT v.*,h.version AS head FROM artifact_versions v JOIN artifact_heads h ON h.task_id=v.task_id AND h.name=v.name WHERE v.task_id=? AND v.artifact_ref=?", (run["task_id"], artifact_ref))
        if version and (version["revision"] != run["revision"] or version["version"] != version["head"]):
            return ["artifact is not the current version for current requirements; revise with the current base_version"]
        return []

    def adopt_worker_result(self, run, **args):
        with self.transaction():
            result = super().adopt_worker_result(run, **args)
            child = self.task(args["child_id"])
            if not self.one("SELECT id FROM worker_reviews WHERE parent_id=? AND child_id=? AND child_epoch=? AND parent_revision=? AND decision='accept'", (run["task_id"], child["id"], child["execution_epoch"], run["revision"])):
                self.db.execute("INSERT INTO worker_reviews VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["task_id"], child["id"], child["execution_epoch"], run["revision"], "accept", args["rationale"], args["artifact_sha256"], run["id"], time.time()))
        self.tick_waits()
        return result

    def request_rework(self, run, *, child_id, rationale, required_text=None):
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("rework needs concrete unmet acceptance criteria")
        with self.transaction():
            result = self.worker_result(run, child_id)
            child = result["task"]
            if child["status"] != "succeeded" or child["cancel_state"] != "active":
                raise ValueError("review requires a completed, active child")
            artifact = (result["delivery"] or {}).get("artifact") or {}
            reads = [o for o in self.operations(run["task_id"]) if o["run_id"] == run["id"] and o["tool"] == "read_worker_result" and o["status"] == "succeeded" and o["arguments"].get("child_id") == child_id and (o["result"] or {}).get("artifact_text") and ((o["result"].get("delivery") or {}).get("artifact") or {}).get("sha256") == artifact.get("sha256")]
            if not reads:
                raise ValueError("read the verified submitted child artifact before review")
            spec = TaskSpec(**child["spec"])
            if required_text is not None:
                if not isinstance(required_text, list):
                    raise ValueError("required_text must be a list")
                spec.required_text = list(dict.fromkeys(spec.required_text + required_text))
            spec.validate()
            if child["calls_used"] >= spec.max_calls:
                raise ValueError("child budget exhausted; explicit user extension is required")
            self.db.execute("INSERT INTO worker_reviews VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["task_id"], child_id, child["execution_epoch"], run["revision"], "rework", rationale, artifact["sha256"], run["id"], time.time()))
            self.db.execute("UPDATE tasks SET status='ready',acceptance='pending',requirement_revision=requirement_revision+1,parent_revision=?,spec=?,reason='review_rework' WHERE id=?", (run["revision"], encode(asdict(spec)), child_id))
            self._event(child_id, "review_rework", encode({"reviewer_task": run["task_id"], "rationale": rationale, "required_text": spec.required_text, "prior_artifact": artifact}))
        return {"child_id": child_id, "state": "rework", "same_task": True, "budget_reset": False}

    def completion_errors(self, run):
        errors = super().completion_errors(run)
        if run["task_id"]:
            task = self.task(run["task_id"])
            for dependency_id in json.loads(task["dependencies"]):
                dependency = self.task(dependency_id)
                parent = self.task(task["parent_task_id"])
                if not self.dependencies_accepted(parent, [dependency_id]) or not self.one("SELECT operation_id FROM dependency_reads WHERE task_id=? AND revision=? AND dependency_id=? AND dependency_epoch=? AND parent_revision=?", (task["id"], run["revision"], dependency_id, dependency["execution_epoch"], parent["requirement_revision"])):
                    errors.append("dependency needs current accepted evidence read: " + dependency_id)
        if run["task_id"] and self.task(run["task_id"])["spec"].get("review_workers", False):
            for child in self.all("SELECT id,execution_epoch FROM tasks WHERE parent_task_id=? AND required=1", (run["task_id"],)):
                if not self.one("SELECT child_id FROM worker_adoptions WHERE child_id=? AND child_epoch=? AND parent_revision=?", (child["id"], child["execution_epoch"], run["revision"])):
                    errors.append("required child awaits explicit parent review: " + child["id"])
        return errors

    def dependency_result(self, run, dependency_id):
        if not self.current(run):
            raise ValueError("stale run")
        task = self.task(run["task_id"])
        if dependency_id not in json.loads(task["dependencies"]) or not task["parent_task_id"]:
            raise ValueError("dependency outside assignment")
        parent = self.task(task["parent_task_id"])
        if not self.dependencies_accepted(parent, [dependency_id]):
            raise ValueError("dependency has not been accepted for current requirements")
        dependency = self.task(dependency_id)
        delivery = self.one("SELECT content,artifact FROM deliveries WHERE task_id=? AND artifact IS NOT NULL ORDER BY rowid DESC LIMIT 1", (dependency_id,))
        if delivery:
            delivery["artifact"] = json.loads(delivery["artifact"])
        return {"task": dependency, "task_id": dependency_id, "status": dependency["status"], "acceptance": dependency["acceptance"], "parent_revision": parent["requirement_revision"], "stale": False, "delivery": delivery, "operations": self.operations(dependency_id)}

    def validate_dependencies(self, parent, ids):
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("invalid child dependencies")
        assigned = {r["id"] for r in self.all("SELECT id FROM tasks WHERE parent_task_id=?", (parent["id"],))}
        if set(ids) - assigned:
            raise ValueError("dependencies must be previously assigned siblings")

    def dependencies_accepted(self, parent, ids):
        for cid in ids:
            child = self.task(cid)
            if child["status"] != "succeeded" or not self.one("SELECT child_id FROM worker_adoptions WHERE child_id=? AND child_epoch=? AND parent_revision=?", (cid, child["execution_epoch"], parent["requirement_revision"])):
                return False
        return True

    def tick_waits(self):
        super().tick_waits()
        with self.transaction():
            for child in self.all("SELECT * FROM tasks WHERE status='waiting_dependencies' AND cancel_state='active' AND auto_continue='on'"):
                parent = self.task(child["parent_task_id"])
                if parent["cancel_state"] == "active" and self.dependencies_accepted(parent, json.loads(child["dependencies"])):
                    self.db.execute("UPDATE tasks SET status='ready' WHERE id=?", (child["id"],))

    def handoff_targets(self, task):
        allowed = {r["actor_id"] for r in self.all("SELECT actor_id FROM tasks WHERE root_task_id=? AND room_id=?", (task["root_task_id"], task["room_id"]))}
        if task["room_id"] == "local":
            allowed.add("default")
        return allowed

    def handoff_task(self, run, *, target_actor_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("handoff needs a reason")
        with self.transaction():
            if not self.current(run):
                raise ValueError("stale run")
            task = self.task(run["task_id"])
            allowed = self.handoff_targets(task)
            if target_actor_id not in allowed or target_actor_id == task["actor_id"]:
                raise ValueError("handoff target outside current team or unchanged")
            if self.one("SELECT COUNT(*) n FROM handoffs WHERE task_id=?", (task["id"],))["n"] >= task["spec"].get("max_handoffs", 2):
                raise ValueError("handoff budget exhausted")
            manifest = {"requirements": task["spec"], "responsibilities": self.orchestration_context(run), "investigation": self.lifecycle_context(run)}
            # Inline finalization keeps ownership transfer, event and old-run retirement atomic.
            self._charge_elapsed(task, run)
            self.db.execute("UPDATE runs SET phase='ended',end_reason='handoff',ended=? WHERE id=?", (time.time(), run["id"]))
            self.db.execute("UPDATE tasks SET actor_id=?,status='ready',reason='handoff' WHERE id=?", (target_actor_id, task["id"]))
            self.db.execute("UPDATE events SET state='pending',claimed_by_run=NULL WHERE claimed_by_run=? AND state='claimed'", (run["id"],))
            self.db.execute("UPDATE events SET actor_id=? WHERE task_id=? AND (state IN ('pending','background') OR id IN (SELECT event_id FROM followups WHERE owner_task=? AND state IN ('pending','in_progress')))", (target_actor_id, task["id"], task["id"]))
            for f in self.all("SELECT id FROM followups WHERE owner_task=? AND state IN ('pending','in_progress')", (task["id"],)):
                self.db.execute("UPDATE followups SET generation=generation+1,state='pending' WHERE id=?", (f["id"],))
                self._history(f["id"], "actor_handoff")
            hid = uuid.uuid4().hex
            self.db.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,?,?,?)", (hid, task["id"], task["actor_id"], target_actor_id, run["id"], run["epoch"], run["revision"], reason, encode(manifest), time.time()))
            self._event(task["id"], "handoff", encode({"handoff_id": hid, "reason": reason, "same_task": True}))
        return {"task_id": task["id"], "actor_id": target_actor_id, "state": "handed_off", "budget_reset": False}

    def yield_task(self, run, *, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("continuation needs a concrete reason")
        # Uses the same continuation budget as worker wakes; does not create a new task.
        self.finish(run, status="ready", reason="bounded_continuation", content="", before_commit=lambda: self._event(run["task_id"], "nudge", reason))
        return {"task_id": run["task_id"], "state": "continued", "budget_reset": False}
