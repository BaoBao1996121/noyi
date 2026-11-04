"""Persistent ongoing goals and explicitly authorized routine policies."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
import time
import uuid

from .contracts import TaskSpec, encode, digest
from .schedule import validate_schedule, next_due


def gid():
    return uuid.uuid4().hex


class GoalStore:
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)
        with self.transaction():
            present = {r["name"] for r in self.all("PRAGMA table_info(tasks)")}
            for column, definition in {"goal_id": "TEXT", "goal_revision": "INTEGER", "policy_id": "TEXT", "policy_revision": "INTEGER", "occurrence_id": "TEXT", "admission_rule_id": "TEXT", "admission_revision": "INTEGER"}.items():
                if column not in present:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
            for sql in (
                "CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,owner TEXT NOT NULL,room_id TEXT NOT NULL,actor_id TEXT NOT NULL,title TEXT NOT NULL,objective TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,current_state TEXT NOT NULL,gaps TEXT NOT NULL,unknowns TEXT NOT NULL,next_action TEXT NOT NULL,wait_reason TEXT NOT NULL,evidence TEXT NOT NULL,calls_limit INTEGER NOT NULL,tools_limit INTEGER NOT NULL,calls_used INTEGER NOT NULL DEFAULT 0,tools_used INTEGER NOT NULL DEFAULT 0,allow_policy_adjustments INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS routine_policies(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL REFERENCES goals(id),actor_id TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,schedule TEXT NOT NULL,template TEXT NOT NULL,next_due REAL NOT NULL,last_check REAL,last_success REAL,fingerprint TEXT,registration_receipt TEXT,authority_ref TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS policy_occurrences(id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,policy_revision INTEGER NOT NULL,goal_revision INTEGER NOT NULL,logical_key TEXT NOT NULL,status TEXT NOT NULL,scheduled_at REAL NOT NULL,observed_at REAL,payload TEXT NOT NULL,task_id TEXT,reason TEXT,created REAL NOT NULL,UNIQUE(policy_id,policy_revision,logical_key))",
                "CREATE TABLE IF NOT EXISTS source_health(policy_id TEXT NOT NULL,source_id TEXT NOT NULL,state TEXT NOT NULL,sha256 TEXT,snapshot_ref TEXT,checked_at REAL NOT NULL,checks INTEGER NOT NULL,failures INTEGER NOT NULL,error_type TEXT,PRIMARY KEY(policy_id,source_id))",
                "CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,policy_revision INTEGER NOT NULL,goal_revision INTEGER NOT NULL,state TEXT NOT NULL,fingerprint TEXT,receipts TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS goal_history(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,run_id TEXT,kind TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS policy_proposals(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,goal_revision INTEGER NOT NULL,policy_id TEXT NOT NULL,policy_revision INTEGER NOT NULL,task_id TEXT NOT NULL,actor_id TEXT NOT NULL,change TEXT NOT NULL,reason TEXT NOT NULL,evidence TEXT NOT NULL,expected_cost TEXT NOT NULL,state TEXT NOT NULL,review_run TEXT,created REAL NOT NULL)",
            ):
                self.db.execute(sql)
            if "assessment_at" not in {r["name"] for r in self.all("PRAGMA table_info(goals)")}:
                self.db.execute("ALTER TABLE goals ADD COLUMN assessment_at REAL NOT NULL DEFAULT 0")
            self.db.execute("PRAGMA user_version=6")

    def goal(self, goal_id, account=None):
        value = self.one("SELECT * FROM goals WHERE id=?", (goal_id,))
        if not value or account is not None and value["owner"] != account:
            raise ValueError("持续目标不存在或没有访问权限")
        for key in ("gaps", "unknowns", "evidence"):
            value[key] = json.loads(value[key])
        return value

    def policy(self, policy_id, account=None):
        value = self.one("SELECT * FROM routine_policies WHERE id=?", (policy_id,))
        if not value:
            raise ValueError("检查计划不存在")
        self.goal(value["goal_id"], account)
        for key in ("schedule", "template", "registration_receipt"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def goal_event(self, goal_id, kind, data, run_id=None):
        event = gid()
        self.db.execute("INSERT INTO goal_history VALUES(?,?,?,?,?,?)", (event, goal_id, run_id, kind, encode(data), time.time()))
        return event

    def create_goal(self, *, room, actor_id, title, objective, calls_limit=120, tools_limit=400, allow_policy_adjustments=False, account="local-user"):
        self.owned_room(room, account)
        self.require_member(actor_id, room)
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 6000:
            raise ValueError("请填写目标名称和具体职责（最多 6000 字）")
        if type(calls_limit) is not int or not 1 <= calls_limit <= 10000 or type(tools_limit) is not int or not 1 <= tools_limit <= 30000 or type(allow_policy_adjustments) is not bool:
            raise ValueError("无效目标累计预算")
        goal_id, now = gid(), time.time()
        with self.transaction():
            self.db.execute("INSERT INTO goals(id,owner,room_id,actor_id,title,objective,status,revision,current_state,gaps,unknowns,next_action,wait_reason,evidence,calls_limit,tools_limit,allow_policy_adjustments,created,updated) VALUES(?,?,?,?,?,?,'active',1,?,?,?,?,?,?,?,?,?,?,?)", (goal_id, account, room, actor_id, title.strip(), objective.strip(), "尚未开展首次检查", encode(["尚无已验收成果"]), encode([]), "注册检查计划或明确开始首次工作", "尚未注册检查计划", encode([]), calls_limit, tools_limit, int(allow_policy_adjustments), now, now))
            self.goal_event(goal_id, "user_authorized", {"actor_id": actor_id, "calls_limit": calls_limit, "tools_limit": tools_limit, "authority": "explicit_local_user"})
        return self.goal(goal_id, account)

    def create_policy(self, goal_id, *, schedule, template, account="local-user", now=None):
        now = time.time() if now is None else now
        config = validate_schedule(schedule)
        spec = template if isinstance(template, TaskSpec) else TaskSpec(**template)
        spec.validate()
        if len(spec.sources) > 12 or len(spec.objective) > 6000:
            raise ValueError("持续检查最多 12 个明确来源，任务说明最多 6000 字")
        if config["kind"] == "watch" and not spec.sources:
            raise ValueError("资料变化检查至少需要一个来源")
        with self.transaction():
            goal = self.goal(goal_id, account)
            self.require_member(goal["actor_id"], goal["room_id"])
            if goal["status"] != "active":
                raise ValueError("请先恢复持续目标")
            if self.one("SELECT COUNT(*) n FROM routine_policies WHERE goal_id=? AND state!='retired'", (goal_id,))["n"] >= 8:
                raise ValueError("同一目标最多启用 8 项检查计划")
            policy_id = gid()
            due = now if config["kind"] == "watch" else next_due(config, now)
            authority = self.goal_event(goal_id, "policy_authorized", {"policy_id": policy_id, "schedule": config, "source_ids": [s["id"] for s in spec.sources], "budget": {"max_calls": spec.max_calls, "max_tools": spec.max_tools}})
            self.db.execute("INSERT INTO routine_policies(id,goal_id,actor_id,state,revision,schedule,template,next_due,authority_ref,created,updated) VALUES(?,?,?,'planned',1,?,?,?,?,?,?)", (policy_id, goal_id, goal["actor_id"], encode(config), encode(asdict(spec)), due, authority, now, now))
            receipt = {"registration_id": gid(), "adapter": "local-sqlite-scheduler/v1", "policy_id": policy_id, "revision": 1, "registered_at": now, "next_due": due, "status": "registered", "service_requirement": "local service must be running; missed occurrences handled on restart"}
            self.db.execute("UPDATE routine_policies SET state='active',registration_receipt=? WHERE id=?", (encode(receipt), policy_id))
            readback = self.policy(policy_id, account)
            if readback["registration_receipt"]["registration_id"] != receipt["registration_id"] or readback["state"] != "active":
                raise RuntimeError("policy registration readback failed")
            self.db.execute("UPDATE goals SET next_action=?,wait_reason=?,updated=? WHERE id=?", ("等待下一次资料检查", "检查计划已注册；等待到期或来源变化", now, goal_id))
        return readback

    def _stop_goal_tasks(self, goal_id, reason, policy_id=None):
        roots = self.all("SELECT id FROM tasks WHERE goal_id=? AND parent_task_id IS NULL AND status NOT IN ('succeeded','cancelled')" + (" AND policy_id=?" if policy_id else ""), (goal_id, policy_id) if policy_id else (goal_id,))
        for root in roots:
            for task in self.all("SELECT id FROM tasks WHERE root_task_id=? AND status NOT IN ('succeeded','cancelled')", (root["id"],)):
                self.db.execute("UPDATE tasks SET auto_continue='paused',status=CASE WHEN status='running' THEN status ELSE 'paused' END,reason=? WHERE id=?", (reason, task["id"]))
                self.db.execute("UPDATE runs SET stop_requested_at=?,stop_reason=? WHERE task_id=? AND phase='running'", (time.time(), reason, task["id"]))

    def set_goal_state(self, goal_id, status, *, expected_revision, account="local-user"):
        if status not in {"active", "paused", "achieved", "retired"}:
            raise ValueError("无效持续目标状态")
        with self.transaction():
            goal = self.goal(goal_id, account)
            if goal["revision"] != expected_revision:
                raise ValueError("目标已更新，请刷新")
            if status == goal["status"]:
                return goal
            if goal["status"] in {"achieved", "retired"} and status == "active":
                raise ValueError("已结束目标请创建新的明确委托")
            if status == "active":
                self.require_member(goal["actor_id"], goal["room_id"])
                if goal["calls_used"] >= goal["calls_limit"] or goal["tools_used"] >= goal["tools_limit"]:
                    raise ValueError("目标累计预算已用完，请先增加额度")
            revision = goal["revision"] + 1
            self.db.execute("UPDATE goals SET status=?,revision=?,wait_reason=?,updated=? WHERE id=?", (status, revision, "用户暂停" if status == "paused" else "等待已注册计划" if status == "active" else "目标已结束", time.time(), goal_id))
            if status != "active":
                self._stop_goal_tasks(goal_id, "goal_" + status)
            else:
                # Explicit resume may continue unfinished work only if its
                # registered policy definition is still the same revision.
                self.db.execute("UPDATE policy_occurrences SET goal_revision=? WHERE status='queued' AND policy_id IN (SELECT id FROM routine_policies WHERE goal_id=? AND state='active') AND policy_revision=(SELECT revision FROM routine_policies WHERE id=policy_occurrences.policy_id)", (revision, goal_id))
                # A paused in-flight read is not accepted on a newer grant.
                # Resume also schedules a fresh check, so a changed source is
                # not forgotten merely because the old cursor had advanced.
                self.db.execute("UPDATE routine_policies SET next_due=MIN(next_due,?) WHERE goal_id=? AND state='active'", (time.time(), goal_id))
                for root in self.all("SELECT * FROM tasks WHERE goal_id=? AND parent_task_id IS NULL AND status NOT IN ('succeeded','cancelled')", (goal_id,)):
                    p = self.policy(root["policy_id"]) if root["policy_id"] else None
                    same_grant = p and p["state"] == "active" and p["revision"] == root["policy_revision"]
                    if root["admission_rule_id"]:
                        same_grant = self.admission_gate(root) is None
                    if same_grant:
                        self.db.execute("UPDATE tasks SET goal_revision=? WHERE id=?", (revision, root["id"]))
                        for task in self.all("SELECT id,actor_id,room_id FROM tasks WHERE root_task_id=? AND status NOT IN ('succeeded','cancelled')", (root["id"],)):
                            if self.actor_can_work(task["actor_id"], task["room_id"], task["id"]):
                                held = self.task(task["id"])
                                wait = held["wait_spec"] or {}
                                state = "waiting_user" if self.one("SELECT id FROM attentions WHERE task_id=? AND state='pending'", (task["id"],)) else "waiting_children" if wait.get("task_ids") else "waiting_dependencies" if held["parent_task_id"] and not self.dependencies_accepted(self.task(held["parent_task_id"]), json.loads(held["dependencies"])) else "ready"
                                self.db.execute("UPDATE tasks SET auto_continue='on',status=CASE WHEN status='running' THEN status ELSE ? END,reason='user_goal_resume' WHERE id=?", (state, task["id"]))
                                if not self.one("SELECT id FROM events WHERE task_id=? AND state IN ('pending','claimed')", (task["id"],)):
                                    self._event(task["id"], "user_resume", "用户明确恢复持续目标")
            self.goal_event(goal_id, "state_changed", {"from": goal["status"], "to": status, "revision": revision})
        return self.goal(goal_id, account)

    def extend_goal_budget(self, goal_id, *, calls=0, tools=0, account="local-user"):
        if type(calls) is not int or type(tools) is not int or calls < 0 or tools < 0 or calls > 10000 or tools > 30000:
            raise ValueError("无效目标预算增加量")
        with self.transaction():
            self.goal(goal_id, account)
            self.db.execute("UPDATE goals SET calls_limit=calls_limit+?,tools_limit=tools_limit+?,updated=? WHERE id=?", (calls, tools, time.time(), goal_id))
            self.goal_event(goal_id, "budget_extended", {"calls": calls, "tools": tools, "used_reset": False})
        return self.goal(goal_id, account)

    def update_policy(self, policy_id, *, schedule, template, expected_revision, state="active", account="local-user", now=None):
        now = time.time() if now is None else now
        config, spec = validate_schedule(schedule), TaskSpec(**template)
        spec.validate()
        if len(spec.sources) > 12 or len(spec.objective) > 6000:
            raise ValueError("持续检查最多 12 个来源，说明最多 6000 字")
        if state not in {"active", "paused", "retired"} or config["kind"] == "watch" and not spec.sources:
            raise ValueError("无效检查计划状态或来源")
        with self.transaction():
            old = self.policy(policy_id, account)
            goal = self.goal(old["goal_id"], account)
            if old["revision"] != expected_revision:
                raise ValueError("检查计划已经更新，请刷新")
            if state == "active" and goal["status"] != "active":
                raise ValueError("暂停或结束的目标不能激活检查计划")
            self.require_member(old["actor_id"], goal["room_id"])
            revision = old["revision"] + 1
            due = now if config["kind"] == "watch" else next_due(config, now)
            receipt = {"registration_id": gid(), "adapter": "local-sqlite-scheduler/v1", "policy_id": policy_id, "revision": revision, "registered_at": now, "next_due": due, "status": "registered" if state == "active" else state}
            authority = self.goal_event(goal["id"], "policy_updated", {"policy_id": policy_id, "revision": revision, "state": state})
            self.db.execute("UPDATE routine_policies SET schedule=?,template=?,state=?,revision=?,next_due=?,registration_receipt=?,authority_ref=?,fingerprint=NULL,updated=? WHERE id=?", (encode(config), encode(asdict(spec)), state, revision, due, encode(receipt), authority, now, policy_id))
            self._stop_goal_tasks(goal["id"], "policy_updated", policy_id)
            self.db.execute("UPDATE policy_occurrences SET status='ignored',reason='policy_revision_changed' WHERE policy_id=? AND status IN ('observing','queued')", (policy_id,))
        return self.policy(policy_id, account)

    def goal_gate(self, task):
        root = self.task(task["root_task_id"])
        if not root["goal_id"]:
            return None
        goal = self.goal(root["goal_id"])
        if goal["status"] != "active":
            return "goal_" + goal["status"]
        if root["goal_revision"] != goal["revision"]:
            return "goal_revision_changed"
        if not self.actor_can_work(goal["actor_id"], goal["room_id"]):
            return "goal_owner_unavailable"
        if root["policy_id"]:
            policy = self.policy(root["policy_id"])
            if policy["state"] != "active" or policy["revision"] != root["policy_revision"]:
                return "policy_revision_changed"
        if root["admission_rule_id"]:
            return self.admission_gate(root)
        return None

    def current(self, run):
        return super().current(run) and (not run["task_id"] or self.goal_gate(self.task(run["task_id"])) is None)

    def control_reason(self, run):
        active = self.one("SELECT phase,stop_reason FROM runs WHERE id=?", (run["id"],))
        if active and active["phase"] == "running" and run["task_id"]:
            if reason := self.goal_gate(self.task(run["task_id"])):
                return active["stop_reason"] or reason
        return super().control_reason(run)

    def claim(self, exclude_actor_ids=()):
        with self.transaction():
            for task in self.all("SELECT id FROM tasks WHERE status='ready'"):
                t = self.task(task["id"])
                if reason := self.goal_gate(t):
                    self.db.execute("UPDATE tasks SET status='paused',auto_continue='paused',reason=? WHERE id=?", (reason, t["id"]))
            return super().claim(exclude_actor_ids=exclude_actor_ids)

    def ack_control(self, run, reason):
        with self.transaction():
            active = self.one("SELECT phase FROM runs WHERE id=?", (run["id"],))
            result = super().ack_control(run, reason)
            if active and active["phase"] == "running" and run["task_id"] and reason.startswith(("goal_", "policy_", "admission_")):
                task = self.task(run["task_id"])
                if task["cancel_state"] == "active":
                    state = "ready" if self.goal_gate(task) is None and task["auto_continue"] == "on" else "paused"
                    self.db.execute("UPDATE tasks SET status=?,reason=? WHERE id=?", (state, reason, task["id"]))
        return result

    def resume(self, tid, **kwargs):
        if self.goal_gate(self.task(tid)):
            raise ValueError("目标已暂停或此任务来自旧策略；请在持续目标中恢复或按新计划检查")
        return super().resume(tid, **kwargs)

    def reserve(self, run, kind):
        with self.transaction():
            task = self.task(run["task_id"])
            root = self.task(task["root_task_id"])
            goal = self.goal(root["goal_id"]) if root["goal_id"] else None
            column, limit = ("calls_used", "calls_limit") if kind == "model" else ("tools_used", "tools_limit")
            if goal and goal[column] >= goal[limit]:
                self.db.execute("UPDATE goals SET status='needs_attention',wait_reason='累计预算已用完',updated=? WHERE id=?", (time.time(), goal["id"]))
                self.goal_event(goal["id"], "budget_exhausted", {"kind": kind, "used": goal[column], "limit": goal[limit]}, run["id"])
                self._stop_goal_tasks(goal["id"], "goal_budget")
                self._notice(None, goal["room_id"], f"持续目标「{goal['title']}」的累计预算已用完，进度已保存。可以增加预算后继续。")
                return False
            accepted = super().reserve(run, kind)
            if accepted and goal:
                self.db.execute(f"UPDATE goals SET {column}={column}+1,updated=? WHERE id=?", (time.time(), goal["id"]))
            return accepted

    def finish(self, run, **kwargs):
        with self.transaction():
            result = super().finish(run, **kwargs)
            if run["task_id"]:
                task = self.task(run["task_id"])
                if task["goal_id"] and task["parent_task_id"] is None:
                    goal = self.goal(task["goal_id"])
                    self.goal_event(goal["id"], "task_" + kwargs["status"], {"task_id": task["id"], "reason": kwargs["reason"], "acceptance": kwargs.get("acceptance", "pending"), "artifact": kwargs.get("artifact")}, run["id"])
                    if kwargs["status"] == "succeeded":
                        self.db.execute("UPDATE goals SET current_state=?,wait_reason='本次成果已交付，持续职责仍保留',assessment_at=?,updated=? WHERE id=?", ("最近一次检查已完成：" + kwargs["content"][:500], time.time(), time.time(), goal["id"]))
                        self.db.execute("UPDATE policy_occurrences SET status='completed',reason='task_accepted' WHERE task_id=?", (task["id"],))
                        for proposal in self.all("SELECT id FROM policy_proposals WHERE goal_id=? AND state='approved'", (goal["id"],)):
                            self._apply_policy_proposal(proposal["id"])
        return result

    def invalidate_room(self, room, reason, **kwargs):
        super().invalidate_room(room, reason, **kwargs)
        for row in self.all("SELECT id,actor_id FROM goals WHERE room_id=? AND status='active'", (room,)):
            if not self.actor_can_work(row["actor_id"], room):
                self.db.execute("UPDATE goals SET status='needs_attention',revision=revision+1,wait_reason='负责成员不可用，检查已暂停',updated=? WHERE id=?", (time.time(), row["id"]))
                self._stop_goal_tasks(row["id"], "goal_owner_unavailable")
                self.goal_event(row["id"], "owner_unavailable", {"reason": reason})

    def goal_context(self, run):
        if not run["task_id"]:
            return None
        task = self.task(run["task_id"])
        root = self.task(task["root_task_id"])
        if not root["goal_id"]:
            return None
        goal = self.goal(root["goal_id"])
        floor = self.one("SELECT context_floor FROM rooms WHERE id=?", (goal["room_id"],))["context_floor"]
        if goal["assessment_at"] < floor:
            goal.update(current_state="记忆已更新，旧判断需重新验证", gaps=[], unknowns=[], evidence=[], next_action="基于当前授权资料重新检查")
        policies = [self.policy(p["id"]) for p in self.all("SELECT id FROM routine_policies WHERE goal_id=?", (goal["id"],))]
        for policy in policies:
            policy["template"]["sources"] = [{k: v for k, v in s.items() if k != "location"} for s in policy["template"]["sources"]]
        return {"goal": goal, "source_policy_id": root["policy_id"], "policies": policies,
                "observations": self.all("SELECT id,state,fingerprint,created FROM observations WHERE policy_id IN (SELECT id FROM routine_policies WHERE goal_id=?) ORDER BY created DESC LIMIT 8", (goal["id"],)),
                "recent_outcomes": [{**r, "data": json.loads(r["data"])} for r in self.all("SELECT kind,data,created FROM goal_history WHERE goal_id=? AND kind LIKE 'task_%' AND created>=? ORDER BY created DESC LIMIT 4", (goal["id"], floor))],
                "proposals": self.all("SELECT id,policy_id,policy_revision,change,reason,evidence,expected_cost,state FROM policy_proposals WHERE goal_id=? AND state IN ('pending','approved') AND created>=? ORDER BY created", (goal["id"], floor)),
                "authority": "Ongoing responsibility remains after one task completes. Only registered active receipts establish scheduling. Worker suggestions do not activate policy or expand permissions."}

    def record_goal_progress(self, run, *, current_state, gaps, unknowns, next_action, evidence_refs):
        if not self.current(run):
            raise ValueError("stale goal work")
        task = self.task(run["task_id"])
        if not task["goal_id"] or task["parent_task_id"]:
            raise ValueError("只有持续目标的当前根任务可以更新目标进展")
        if any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 1500 for s in (current_state, next_action)):
            raise ValueError("请简明记录当前状态与下一步")
        for values in (gaps, unknowns):
            if not isinstance(values, list) or len(values) > 16 or any(not isinstance(v, str) or not 1 <= len(v) <= 500 for v in values):
                raise ValueError("差距和未知问题最多各 16 条")
        allowed = {r["id"] for r in self.all("SELECT id FROM operations WHERE task_id=? AND status IN ('succeeded','failed')", (task["id"],))}
        allowed |= {r["id"] for r in self.all("SELECT o.id FROM observations o JOIN routine_policies p ON p.id=o.policy_id WHERE p.goal_id=?", (task["goal_id"],))}
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 20 or any(r not in allowed for r in evidence_refs):
            raise ValueError("进展记录必须引用本目标实际的观察或工具回执")
        with self.transaction():
            self.db.execute("UPDATE goals SET current_state=?,gaps=?,unknowns=?,next_action=?,evidence=?,assessment_at=?,updated=? WHERE id=?", (current_state, encode(gaps), encode(unknowns), next_action, encode(evidence_refs), time.time(), time.time(), task["goal_id"]))
            self.goal_event(task["goal_id"], "evidence_linked_progress", {"current_state": current_state, "gaps": gaps, "unknowns": unknowns, "next_action": next_action, "evidence_refs": evidence_refs, "trust": "agent assessment, not independently verified truth"}, run["id"])
        return {"goal_id": task["goal_id"], "state": "progress_recorded", "ongoing": True}

    def prepare_occurrence(self, policy_id, window, *, now=None, manual=False):
        now = time.time() if now is None else now
        with self.transaction():
            policy = self.policy(policy_id)
            goal = self.goal(policy["goal_id"])
            if policy["state"] != "active" or goal["status"] != "active" or not self.actor_can_work(policy["actor_id"], goal["room_id"]):
                return None
            prior = self.one("SELECT * FROM policy_occurrences WHERE policy_id=? AND policy_revision=? AND logical_key=?", (policy_id, policy["revision"], window["key"]))
            if prior:
                return prior
            occurrence = gid()
            status = "skipped" if window["decision"] == "skipped" else "observing"
            self.db.execute("INSERT INTO policy_occurrences(id,policy_id,policy_revision,goal_revision,logical_key,status,scheduled_at,payload,reason,created) VALUES(?,?,?,?,?,?,?,?,?,?)", (occurrence, policy_id, policy["revision"], goal["revision"], window["key"], status, window["scheduled"], encode({"window": window, "force": manual}), "missed_occurrence_policy" if status == "skipped" else None, now))
            if not manual:
                self.db.execute("UPDATE routine_policies SET next_due=?,updated=? WHERE id=?", (window["next_due"], now, policy_id))
            if status == "skipped":
                self.goal_event(goal["id"], "occurrence_skipped", {"occurrence_id": occurrence, "window": window})
            return self.one("SELECT * FROM policy_occurrences WHERE id=?", (occurrence,))

    def request_policy_check(self, policy_id, *, nonce, account="local-user", now=None):
        now = time.time() if now is None else now
        self.policy(policy_id, account)
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 150:
            raise ValueError("无效检查请求标识")
        return self.prepare_occurrence(policy_id, {"key": "manual:" + nonce, "decision": "explicit_user_check", "scheduled": now, "next_due": now}, now=now, manual=True)

    def occurrence_current(self, occurrence):
        policy = self.policy(occurrence["policy_id"])
        goal = self.goal(policy["goal_id"])
        return (policy["state"] == "active" and goal["status"] == "active" and policy["revision"] == occurrence["policy_revision"]
                and goal["revision"] == occurrence["goal_revision"] and self.actor_can_work(policy["actor_id"], goal["room_id"]))

    def complete_observation(self, occurrence_id, receipts, *, now=None):
        """Commit observations only against the version that authorized them."""
        now = time.time() if now is None else now
        with self.transaction():
            occurrence = self.one("SELECT * FROM policy_occurrences WHERE id=?", (occurrence_id,))
            if not occurrence or occurrence["status"] != "observing":
                return {"state": "ignored", "reason": "not_observing"}
            if not self.occurrence_current(occurrence):
                self.db.execute("UPDATE policy_occurrences SET status='ignored',reason='stale_authority',observed_at=? WHERE id=?", (now, occurrence_id))
                return {"state": "ignored", "reason": "stale_authority"}
            policy = self.policy(occurrence["policy_id"])
            goal = self.goal(policy["goal_id"])
            sources = {s["id"]: s for s in policy["template"]["sources"]}
            if not isinstance(receipts, list) or {r.get("source_id") for r in receipts} != set(sources) or len(receipts) != len(sources):
                raise ValueError("observation source set differs from registered grant")
            before = self.all("SELECT * FROM source_health WHERE policy_id=?", (policy["id"],))
            previous_health = {r["source_id"]: r for r in before}
            health_changed = False
            for receipt in receipts:
                if receipt.get("state") not in {"ok", "failed"} or receipt["state"] == "ok" and not all(isinstance(receipt.get(k), str) and receipt[k] for k in ("sha256", "snapshot_ref")):
                    raise ValueError("invalid observation receipt")
                old = previous_health.get(receipt["source_id"])
                health_changed |= not old or old["state"] != receipt["state"] or old["error_type"] != receipt.get("error_type")
                self.db.execute("INSERT INTO source_health VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(policy_id,source_id) DO UPDATE SET state=excluded.state,sha256=COALESCE(excluded.sha256,source_health.sha256),snapshot_ref=COALESCE(excluded.snapshot_ref,source_health.snapshot_ref),checked_at=excluded.checked_at,checks=source_health.checks+1,failures=CASE WHEN excluded.state='failed' THEN source_health.failures+1 ELSE 0 END,error_type=excluded.error_type", (policy["id"], receipt["source_id"], receipt["state"], receipt.get("sha256"), receipt.get("snapshot_ref"), now, int(receipt["state"] == "failed"), receipt.get("error_type")))
            blocked = any(r["state"] == "failed" and sources[r["source_id"]].get("required", True) for r in receipts)
            fingerprint = digest(encode(sorted((r["source_id"], r["state"], r.get("sha256")) for r in receipts)).encode())
            payload = json.loads(occurrence["payload"])
            changed = not blocked and (not sources or fingerprint != policy["fingerprint"] or payload.get("force"))
            state = "failed" if blocked else "changed" if changed else "unchanged"
            self.db.execute("UPDATE routine_policies SET last_check=?,last_success=CASE WHEN ? THEN last_success ELSE ? END,fingerprint=CASE WHEN ? THEN fingerprint ELSE ? END WHERE id=?", (now, int(blocked), now, int(blocked), fingerprint, policy["id"]))
            # Watch polls without a change are represented by source-health
            # counters, not a growing stream of indistinguishable notices.
            quiet = policy["schedule"]["kind"] == "watch" and not changed and not health_changed and not payload.get("force")
            observation_id = gid()
            if not quiet:
                self.db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)", (observation_id, policy["id"], policy["revision"], goal["revision"], state, fingerprint, encode(receipts), now))
            if blocked:
                self.db.execute("UPDATE goals SET wait_reason='来源读取失败，不能判断为没有变化',next_action='等待来源恢复或用户修复资料位置',updated=? WHERE id=?", (now, goal["id"]))
                if health_changed:
                    self._notice(None, goal["room_id"], f"持续目标「{goal['title']}」有资料暂时无法读取，检查没有完成。请查看来源状态；已有成果保留。")
            elif not changed:
                self.db.execute("UPDATE goals SET wait_reason='来源已成功检查，本次没有变化',updated=? WHERE id=?", (now, goal["id"]))
                if any(r["state"] == "failed" for r in before):
                    self._notice(None, goal["room_id"], f"持续目标「{goal['title']}」的资料已恢复可读，内容没有变化。")
            payload.update(observation_id=observation_id if not quiet else None, receipts=receipts, fingerprint=fingerprint)
            if quiet:
                self.db.execute("DELETE FROM policy_occurrences WHERE id=?", (occurrence_id,))
            else:
                self.db.execute("UPDATE policy_occurrences SET status=?,observed_at=?,payload=?,reason=? WHERE id=?", ("queued" if changed else state, now, encode(payload), "source_change" if changed else state, occurrence_id))
            if changed:
                self.db.execute("UPDATE policy_occurrences SET status='coalesced',reason=? WHERE policy_id=? AND policy_revision=? AND status='queued' AND id!=?", ("superseded_by:" + occurrence_id, policy["id"], policy["revision"], occurrence_id))
                self.db.execute("UPDATE goals SET next_action='根据新观察更新资料成果',wait_reason='检查发现新内容，工作已入队',updated=? WHERE id=?", (now, goal["id"]))
            return {"state": state, "observation_id": observation_id if not quiet else None, "occurrence_id": occurrence_id, "quiet": quiet}

    def propose_policy_adjustment(self, run, *, policy_id, reason, evidence_refs, expected_cost, interval_seconds=None, local_time=None):
        if not self.current(run):
            raise ValueError("stale policy proposal")
        root = self.task(self.task(run["task_id"])["root_task_id"])
        policy = self.policy(policy_id)
        if not root["goal_id"] or root["goal_id"] != policy["goal_id"]:
            raise ValueError("不能建议修改其他目标的检查计划")
        if any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 1500 for s in (reason, expected_cost)):
            raise ValueError("需要说明证据理由与预期成本")
        allowed = {r["id"] for r in self.all("SELECT o.id FROM operations o JOIN tasks t ON t.id=o.task_id WHERE t.root_task_id=? AND o.status IN ('succeeded','failed')", (root["id"],))}
        allowed |= {r["id"] for r in self.all("SELECT o.id FROM observations o JOIN routine_policies p ON p.id=o.policy_id WHERE p.goal_id=?", (root["goal_id"],))}
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 16 or any(e not in allowed for e in evidence_refs):
            raise ValueError("建议必须引用本目标的实际观察或本次团队工具回执")
        change = {"interval_seconds": interval_seconds} if interval_seconds is not None else {"time": local_time}
        if (interval_seconds is None) == (local_time is None):
            raise ValueError("一次只调整检查间隔或本地时间")
        config = validate_schedule({**policy["schedule"], **change})
        if interval_seconds is not None:
            old = policy["schedule"].get("interval_seconds", 60)
            if config["kind"] not in {"watch", "interval"} or not old / 2 <= interval_seconds <= old * 4:
                raise ValueError("建议只能在现有间隔的 1/2 至 4 倍内调整")
        elif config["kind"] != "calendar":
            raise ValueError("仅日历计划支持修改本地时间")
        goal = self.goal(root["goal_id"])
        proposal = gid()
        with self.transaction():
            if self.one("SELECT COUNT(*) n FROM policy_proposals WHERE goal_id=? AND state IN ('pending','approved')", (goal["id"],))["n"] >= 16:
                raise ValueError("待处理建议已达到上限")
            self.db.execute("INSERT INTO policy_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,?)", (proposal, goal["id"], goal["revision"], policy_id, policy["revision"], run["task_id"], run["actor_id"], encode(change), reason, encode(evidence_refs), expected_cost, time.time()))
        return {"proposal_id": proposal, "state": "pending", "active": False, "authority": "requires user approval or explicitly authorized goal owner review"}

    def review_policy_proposal(self, run, *, proposal_id, accept, reason):
        if not self.current(run):
            raise ValueError("stale policy review")
        task = self.task(run["task_id"])
        proposal = self.one("SELECT * FROM policy_proposals WHERE id=?", (proposal_id,))
        if not proposal or task["parent_task_id"] or proposal["goal_id"] != task["goal_id"]:
            raise ValueError("只有当前持续目标根任务的负责人可审核")
        goal = self.goal(task["goal_id"])
        if not goal["allow_policy_adjustments"] or run["actor_id"] != goal["actor_id"]:
            raise ValueError("用户未授权此负责人自动调整检查计划")
        if type(accept) is not bool or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1500:
            raise ValueError("审核需要明确决定和理由")
        policy = self.policy(proposal["policy_id"])
        if proposal["state"] != "pending" or proposal["goal_revision"] != goal["revision"] or proposal["policy_revision"] != policy["revision"]:
            raise ValueError("建议已处理或授权版本已过期")
        with self.transaction():
            self.db.execute("UPDATE policy_proposals SET state=?,review_run=? WHERE id=?", ("approved" if accept else "rejected", run["id"], proposal_id))
            self.goal_event(goal["id"], "policy_proposal_reviewed", {"proposal_id": proposal_id, "accept": accept, "reason": reason, "authority": "explicitly_delegated_goal_owner"}, run["id"])
        return {"proposal_id": proposal_id, "state": "approved_pending_task_acceptance" if accept else "rejected", "active": False}

    def _apply_policy_proposal(self, proposal_id):
        proposal = self.one("SELECT * FROM policy_proposals WHERE id=?", (proposal_id,))
        if not proposal or proposal["state"] != "approved":
            return None
        goal, policy = self.goal(proposal["goal_id"]), self.policy(proposal["policy_id"])
        if goal["status"] != "active" or policy["state"] != "active" or goal["revision"] != proposal["goal_revision"] or policy["revision"] != proposal["policy_revision"]:
            self.db.execute("UPDATE policy_proposals SET state='stale' WHERE id=?", (proposal_id,))
            return {"state": "stale"}
        updated = self.update_policy(policy["id"], schedule={**policy["schedule"], **json.loads(proposal["change"])}, template=policy["template"], expected_revision=policy["revision"], account=goal["owner"])
        self.db.execute("UPDATE policy_proposals SET state='applied' WHERE id=?", (proposal_id,))
        self.goal_event(goal["id"], "policy_proposal_applied", {"proposal_id": proposal_id, "registration_receipt": updated["registration_receipt"]})
        return {"state": "applied", "policy": updated}

    def decide_policy_proposal(self, proposal_id, *, accept, account="local-user"):
        if type(accept) is not bool:
            raise ValueError("无效建议决定")
        with self.transaction():
            proposal = self.one("SELECT * FROM policy_proposals WHERE id=?", (proposal_id,))
            if not proposal:
                raise ValueError("检查建议不存在")
            self.goal(proposal["goal_id"], account)
            if proposal["state"] not in {"pending", "approved"}:
                raise ValueError("检查建议已处理")
            self.db.execute("UPDATE policy_proposals SET state=?,review_run=NULL WHERE id=?", ("approved" if accept else "rejected", proposal_id))
            self.goal_event(proposal["goal_id"], "policy_proposal_user_decision", {"proposal_id": proposal_id, "accept": accept})
            return self._apply_policy_proposal(proposal_id) if accept else {"state": "rejected"}
