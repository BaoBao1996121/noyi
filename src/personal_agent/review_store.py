"""Evidence-linked retrospectives. Interpretations never become measurements."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .contracts import TaskSpec, digest, encode
from .goal_store import gid


def short_text(value, label, maximum=2000):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(label + "不能为空或过长")
    return value.strip()


def historical_phase_error(text):
    # Narrow state-consistency lint, not a general semantic correctness claim.
    return any(term in text for term in ("观察期未满", "观察期尚未结束", "未到观察窗口末尾", "实验窗口未满", "实验成功", "实验失败"))


class ReviewStore:
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)
        with self.transaction():
            for sql in (
                "CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,title TEXT NOT NULL,hypothesis TEXT NOT NULL,metric TEXT NOT NULL,baseline TEXT NOT NULL,variant TEXT NOT NULL,success_criteria TEXT NOT NULL,window_start REAL NOT NULL,window_end REAL NOT NULL,next_check REAL NOT NULL,policy_versions TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS experiment_versions(experiment_id TEXT NOT NULL,version INTEGER NOT NULL,configuration TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(experiment_id,version))",
                "CREATE TABLE IF NOT EXISTS user_feedback(id TEXT PRIMARY KEY,goal_id TEXT,task_id TEXT NOT NULL,rating TEXT NOT NULL,note TEXT NOT NULL,management_kind TEXT NOT NULL,management_seconds REAL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS review_snapshots(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,goal_revision INTEGER NOT NULL,room_revision INTEGER NOT NULL,window_start REAL NOT NULL,window_end REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS goal_review_requests(task_id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,snapshot_id TEXT NOT NULL,experiment_id TEXT,experiment_version INTEGER,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS review_evidence_reads(task_id TEXT NOT NULL,snapshot_id TEXT NOT NULL,evidence_id TEXT NOT NULL,run_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(task_id,snapshot_id,evidence_id))",
                "CREATE TABLE IF NOT EXISTS goal_reviews(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,task_id TEXT NOT NULL,run_id TEXT NOT NULL,request_revision INTEGER NOT NULL,snapshot_id TEXT NOT NULL,experiment_id TEXT,experiment_version INTEGER,observed_facts TEXT NOT NULL,hypotheses TEXT NOT NULL,uncertainties TEXT NOT NULL,decision TEXT NOT NULL,rationale TEXT NOT NULL,evidence_refs TEXT NOT NULL,next_check REAL NOT NULL,next_validation TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS experiment_reminders(experiment_id TEXT NOT NULL,version INTEGER NOT NULL,next_check REAL NOT NULL,created REAL NOT NULL,PRIMARY KEY(experiment_id,version,next_check))",
            ):
                self.db.execute(sql)
            if "intent" not in {r["name"] for r in self.all("PRAGMA table_info(goal_review_requests)")}:
                self.db.execute("ALTER TABLE goal_review_requests ADD COLUMN intent TEXT NOT NULL DEFAULT '{}'")
            self.db.execute("PRAGMA user_version=7")

    def experiment(self, experiment_id, account=None):
        row = self.one("SELECT * FROM experiments WHERE id=?", (experiment_id,))
        if not row:
            raise ValueError("观察实验不存在")
        self.goal(row["goal_id"], account)
        row["policy_versions"] = json.loads(row["policy_versions"])
        return row

    def experiment_context(self, experiment_id):
        """Keep registered facts without reviving erased derived assumptions.

        Changing an experiment's state or reminder time does not reconfirm its
        original hypothesis, so use its creation time rather than updated time.
        The full audit remains available to the owner outside model context.
        """
        plan = self.experiment(experiment_id)
        room = self.goal(plan["goal_id"])["room_id"]
        floor = self.one("SELECT context_floor FROM rooms WHERE id=?", (room,))["context_floor"]
        if plan["created"] >= floor:
            return plan
        return {**{key: plan[key] for key in ("id", "goal_id", "state", "version", "window_start", "window_end", "next_check", "created", "updated", "policy_versions")},
                "context_note": "记忆已更新，此前观察假设与判断不再作为当前依据；只保留已经登记的时间和状态。"}

    def create_experiment(self, goal_id, *, title, hypothesis, metric, baseline, variant, success_criteria, window_start, window_end, next_check, account="local-user"):
        goal = self.goal(goal_id, account)
        values = [short_text(v, label) for v, label in [(title, "实验名称"), (hypothesis, "假设"), (metric, "衡量指标"), (baseline, "原方案"), (variant, "待观察变化"), (success_criteria, "检验条件")]]
        now = time.time()
        for value in (window_start, window_end, next_check):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("无效观察时间")
        if not goal["created"] - 1 <= window_start < window_end <= now + 366 * 86400 or not max(now, window_start) < next_check <= now + 366 * 86400:
            raise ValueError("请使用目标创建后、未来一年内的观察窗口和复盘时间")
        if goal["status"] != "active":
            raise ValueError("请先恢复持续目标")
        with self.transaction():
            if self.one("SELECT COUNT(*) n FROM experiments WHERE goal_id=? AND state='observing'", (goal_id,))["n"] >= 16:
                raise ValueError("同时观察的实验最多 16 项")
            experiment_id = gid()
            versions = self.all("SELECT id,revision,state FROM routine_policies WHERE goal_id=?", (goal_id,))
            self.db.execute("INSERT INTO experiments VALUES(?,?,1,'observing',?,?,?,?,?,?,?,?,?,?,?,?)", (experiment_id, goal_id, *values, window_start, window_end, next_check, encode(versions), now, now))
            current = self.experiment(experiment_id)
            self.db.execute("INSERT INTO experiment_versions VALUES(?,1,?,'explicit_user_plan',?)", (experiment_id, encode(current), now))
            self.goal_event(goal_id, "experiment_planned", {"experiment_id": experiment_id, "version": 1, "policy_change_applied": False})
        return current

    def revise_experiment(self, experiment_id, *, expected_version, reason, next_check=None, state=None, account="local-user"):
        reason = short_text(reason, "修改理由")
        with self.transaction():
            old = self.experiment(experiment_id, account)
            if old["version"] != expected_version:
                raise ValueError("实验记录已更新，请刷新")
            state = state or old["state"]
            if state not in {"observing", "stopped", "closed"}:
                raise ValueError("无效实验状态")
            check = old["next_check"] if next_check is None else next_check
            if type(check) not in (int, float) or not math.isfinite(check) or next_check is not None and not time.time() < check <= time.time() + 366 * 86400:
                raise ValueError("复盘时间须在未来一年内")
            version = old["version"] + 1
            self.db.execute("UPDATE experiments SET version=?,state=?,next_check=?,updated=? WHERE id=?", (version, state, check, time.time(), experiment_id))
            current = self.experiment(experiment_id)
            self.db.execute("INSERT INTO experiment_versions VALUES(?,?,?,?,?)", (experiment_id, version, encode(current), reason, time.time()))
            self.goal_event(old["goal_id"], "experiment_revised", {"experiment_id": experiment_id, "version": version, "reason": reason, "policy_change_applied": False})
        return current

    def save_feedback(self, task_id, *, rating, note, management_kind="feedback", management_seconds=None, account="local-user"):
        task = self._owned(task_id, account)
        root = self.task(task["root_task_id"])
        if rating not in {"useful", "needs_fix", "incorrect"} or management_kind not in {"feedback", "reminder", "addition", "correction", "takeover"}:
            raise ValueError("无效使用反馈")
        note = short_text(note, "反馈说明", 3000)
        if management_seconds is not None and (type(management_seconds) not in (int, float) or not math.isfinite(management_seconds) or not 0 <= management_seconds <= 86400):
            raise ValueError("投入时间应为 0–86400 秒；未知时留空")
        feedback_id = gid()
        with self.transaction():
            self.db.execute("INSERT INTO user_feedback VALUES(?,?,?,?,?,?,?,?)", (feedback_id, root["goal_id"], task_id, rating, note, management_kind, management_seconds, time.time()))
        return {"id": feedback_id, "trust": "explicit user report", "seconds_measured_automatically": False}

    def _review_snapshot(self, goal_id, window_start, window_end):
        goal = self.goal(goal_id)
        room = self.owned_room(goal["room_id"], goal["owner"])
        # Corrections/deletions invalidate automatic summaries of old memory.
        start = max(window_start, room["context_floor"], goal["created"])
        end = min(window_end, time.time())
        if end < start:
            end = start
        tasks = self.all("SELECT t.id,t.status,t.acceptance,t.created,t.parent_task_id FROM tasks t JOIN tasks root ON root.id=t.root_task_id WHERE root.goal_id=? AND t.created BETWEEN ? AND ?", (goal_id, start, end))
        usage = [json.loads(r["data"]) for r in self.all("SELECT s.data FROM steps s JOIN tasks t ON t.id=s.task_id JOIN tasks root ON root.id=t.root_task_id WHERE root.goal_id=? AND s.kind='model' AND s.created BETWEEN ? AND ?", (goal_id, start, end))]
        failed = self.one("SELECT COUNT(*) n FROM operations o JOIN tasks t ON t.id=o.task_id JOIN tasks root ON root.id=t.root_task_id WHERE root.goal_id=? AND o.status='failed' AND o.created BETWEEN ? AND ?", (goal_id, start, end))["n"]
        feedback = self.all("SELECT * FROM user_feedback WHERE goal_id=? AND created BETWEEN ? AND ? ORDER BY created", (goal_id, start, end))
        observations = self.all("SELECT o.* FROM observations o JOIN routine_policies p ON p.id=o.policy_id WHERE p.goal_id=? AND o.created BETWEEN ? AND ? ORDER BY o.created DESC LIMIT 48", (goal_id, start, end))
        deliveries = self.all("SELECT d.id,d.task_id,d.content,d.artifact,d.created FROM deliveries d JOIN tasks t ON t.id=d.task_id WHERE t.goal_id=? AND d.artifact IS NOT NULL AND d.created BETWEEN ? AND ? AND NOT EXISTS(SELECT 1 FROM goal_review_requests rr WHERE rr.task_id=t.id) ORDER BY d.created DESC LIMIT 16", (goal_id, start, end))
        inputs = self.one("SELECT COUNT(DISTINCT f.id) n FROM followups f JOIN tasks t ON t.id=f.owner_task JOIN tasks root ON root.id=t.root_task_id WHERE root.goal_id=? AND f.state!='context' AND f.created BETWEEN ? AND ?", (goal_id, start, end))["n"]
        metrics = {"window_start": start, "window_end": end, "root_tasks_created": sum(not t["parent_task_id"] for t in tasks), "worker_tasks_created": sum(bool(t["parent_task_id"]) for t in tasks), "tasks_currently_accepted": sum(t["acceptance"] == "passed" for t in tasks), "model_calls_with_response": len(usage), "input_tokens": sum(u["input_tokens"] for u in usage), "output_tokens": sum(u["output_tokens"] for u in usage), "failed_tool_operations": failed, "user_followups": inputs, "user_feedback_count": len(feedback), "reported_management_seconds": sum(f["management_seconds"] for f in feedback if f["management_seconds"] is not None) if any(f["management_seconds"] is not None for f in feedback) else None, "domain_outcome": None, "invoice_currency_cost": None, "cautions": ["Task acceptance is a structural check, not evidence of real-world improvement.", "No domain adoption, revenue, publication or growth metric was connected.", "Unknown human time and invoice cost are missing, not zero.", "Unchanged watch polls update lifetime health counters; this evidence catalog is a bounded set of retained observations."]}
        metrics["model_usage_scope"] = "Only execution of tasks associated with this goal. Taskless conversational intake and other unassociated requests are excluded; this is not the complete invoice."
        evidence = {"metrics": {"kind": "ledger_metrics", "data": metrics}}
        evidence["goal_state"] = {"kind": "state_at_snapshot", "data": {"created": time.time(), "current_state": goal["current_state"] if goal["assessment_at"] >= room["context_floor"] else "需要重新评估", "gaps": goal["gaps"] if goal["assessment_at"] >= room["context_floor"] else [], "unknowns": goal["unknowns"] if goal["assessment_at"] >= room["context_floor"] else ["此前评估已因记忆纠正失效"], "next_action": goal["next_action"] if goal["assessment_at"] >= room["context_floor"] else "重新检查", "health": self.all("SELECT h.source_id,h.state,h.checks,h.failures AS consecutive_failures,h.checked_at FROM source_health h JOIN routine_policies p ON p.id=h.policy_id WHERE p.goal_id=?", (goal_id,)), "scope": "Current state and lifetime health counters at snapshot time, not measurements restricted to this experiment window."}}
        for item in observations:
            evidence["observation:" + item["id"]] = {"kind": "source_observation", "data": {**item, "receipts": json.loads(item["receipts"])}}
        for item in deliveries:
            evidence["delivery:" + item["id"]] = {"kind": "artifact_delivery", "data": {**item, "artifact": json.loads(item["artifact"])}}
        for item in feedback[-32:]:
            evidence["feedback:" + item["id"]] = {"kind": "user_report", "data": item}
        payload = {"metrics": metrics, "evidence": evidence, "requested_window": {"start": window_start, "end": window_end}, "effective_window": {"start": start, "end": end}, "memory_floor_applied": start > window_start}
        raw, snapshot_id = encode(payload), gid()
        self.db.execute("INSERT INTO review_snapshots VALUES(?,?,?,?,?,?,?,?,?)", (snapshot_id, goal_id, goal["revision"], room["revision"], start, end, raw, digest(raw.encode()), time.time()))
        return snapshot_id

    def request_goal_review(self, goal_id, *, nonce, experiment_id=None, days=7, account="local-user"):
        if type(days) not in (int, float) or not math.isfinite(days) or not 0 < days <= 366:
            raise ValueError("复盘窗口应在 0–366 天之间")
        with self.transaction():
            goal = self.goal(goal_id, account)
            self.require_member(goal["actor_id"], goal["room_id"])
            # A fresh explicit historical review is independent of whether
            # future business work is paused or ended. Its task keeps this
            # goal revision and budget, without reactivating any policy.
            intent = encode({"goal_id": goal_id, "experiment_id": experiment_id, "days": days})
            old = self.one("SELECT t.id,r.intent FROM tasks t JOIN messages m ON m.task_id=t.id JOIN goal_review_requests r ON r.task_id=t.id WHERE m.source='goal_review' AND m.account=? AND m.client_nonce=?", (account, nonce))
            if old:
                if old["intent"] != intent:
                    raise ValueError("复盘标识已被不同请求使用")
                return {"task_id": old["id"], "duplicate": True}
            experiment = self.experiment(experiment_id, account) if experiment_id else None
            if experiment and experiment["goal_id"] != goal_id:
                raise ValueError("实验不属于这个目标")
            if self.one("SELECT r.task_id FROM goal_review_requests r JOIN tasks t ON t.id=r.task_id WHERE r.goal_id=? AND t.status NOT IN ('succeeded','cancelled')", (goal_id,)):
                raise ValueError("已有复盘尚未完成，请先处理当前复盘任务")
            now = time.time()
            start, end = (experiment["window_start"], experiment["window_end"]) if experiment else (max(goal["created"], now - days * 86400), now)
            snapshot = self._review_snapshot(goal_id, start, end)
            spec = TaskSpec("复盘持续目标「" + goal["title"] + "」。读取当前复盘证据，区分观测、解释、假设和未知。检查成果与用户反馈，说明保留、停止或修改现有做法的理由。没有连接的业务效果数据不要编造，没有反馈不代表满意。使用 record_goal_review 保存有证据的结论、下次检验时间与条件，然后写一份简洁中文复盘报告并提交。策略决定是建议，不在本次自动改变计划或权限。", max_calls=14, max_tools=40, max_workers=0, enable_nudge=True)
            spec.objective += (" 本次关联观察实验；根据实验的实际状态和观察窗口判断，未到窗口末尾不能宣称实验成功。" if experiment else " 本次是历史记录复盘，没有关联实验。观察范围在请求时已经截取结束；不能称为实验，也不能声称观察期尚未结束。")
            spec.objective += " 把字段转成中文说明，例如 domain_outcome=null 写成‘尚未接入业务效果测量’，useful 写成‘有用’，不要照抄代码字段。"
            spec.objective += " 若证据目录里有工作成果，必须实际读取并引用至少一份，不能只看数量。只有本次明确关联实验时才判断该实验阶段；没有关联时只是历史工作复盘，不继承旧报告的实验状态或结论。健康记录中的失败数是连续失败数，不是所有历史失败总数。报告使用自然中文；日期和下次检验时间写为北京时间的年月日时分，不展示 Unix 时间戳、英文内部字段名或状态代码。逐项引用实际证据；当前目标状态与累计检查计数须读取 goal_state，不能当作观察窗口内的指标。"
            submitted = self.submit(spec, text="请复盘持续目标「" + goal["title"] + "」的已有证据，说明未知、建议和下一次检验条件。", nonce=nonce, source="goal_review", account=account, actor=goal["actor_id"], room=goal["room_id"])
            task_id = submitted["task_id"]
            self.db.execute("UPDATE tasks SET goal_id=?,goal_revision=? WHERE id=?", (goal_id, goal["revision"], task_id))
            self.db.execute("INSERT INTO goal_review_requests VALUES(?,?,?,?,?,?,?)", (task_id, goal_id, snapshot, experiment_id, experiment["version"] if experiment else None, now, intent))
            self.goal_event(goal_id, "review_requested", {"task_id": task_id, "snapshot_id": snapshot, "experiment_id": experiment_id, "authority": "explicit_user"})
        return {"task_id": task_id, "snapshot_id": snapshot, "duplicate": False}

    def explicit_goal_task_granted(self, root):
        return bool(self.one("SELECT task_id FROM goal_review_requests WHERE task_id=? AND goal_id=?", (root["id"], root["goal_id"])))

    def review_context(self, run):
        if not run["task_id"]:
            return None
        root = self.task(self.task(run["task_id"])["root_task_id"])
        request = self.one("SELECT * FROM goal_review_requests WHERE task_id=?", (root["id"],))
        if not request:
            return None
        snapshot = self.one("SELECT * FROM review_snapshots WHERE id=?", (request["snapshot_id"],))
        floor = self.one("SELECT context_floor FROM rooms WHERE id=?", (root["room_id"],))["context_floor"]
        if snapshot["created"] < floor:
            return {"state": "stale_after_memory_correction", "action": "Cancel this review and request a fresh one; older derived evidence is not automatically reloaded."}
        payload = json.loads(snapshot["payload"])
        experiment = self.experiment_context(request["experiment_id"]) if request["experiment_id"] else None
        return {"state": "available", "review_type": "experiment_review" if experiment else "historical_snapshot", "experiment_phase": experiment["state"] if experiment else "not_applicable", "snapshot_window_closed": True, "snapshot_id": snapshot["id"], "window": payload["effective_window"], "requested_window": payload["requested_window"], "metrics": payload["metrics"], "evidence_catalog": [{"id": k, "kind": v["kind"], "created": v["data"].get("created")} for k, v in payload["evidence"].items()], "experiment": experiment, "experiment_version_at_request": request["experiment_version"], "suggested_next_check_unix_seconds": time.time() + 86400, "suggested_next_check_beijing": datetime.fromtimestamp(time.time() + 86400, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M 北京时间"), "trust": "Frozen operational receipts; user reports and model interpretations do not prove domain improvement. Read cited evidence with read_review_evidence before record_goal_review."}

    def goal_context(self, run):
        context = super().goal_context(run)
        if context and (review := self.review_context(run)):
            context["review"] = review
        return context

    def read_review_evidence(self, run, *, evidence_id, record_read=True):
        if not self.current(run):
            raise ValueError("stale review")
        context = self.review_context(run)
        if not context or context["state"] != "available":
            raise ValueError("没有当前有效的复盘证据快照")
        snapshot = self.one("SELECT * FROM review_snapshots WHERE id=?", (context["snapshot_id"],))
        if digest(snapshot["payload"].encode()) != snapshot["sha256"]:
            raise ValueError("复盘证据快照校验失败")
        evidence = json.loads(snapshot["payload"])["evidence"].get(evidence_id)
        if not evidence:
            raise ValueError("证据不在本次复盘范围内")
        # Artifact receipts identify immutable bytes. The orchestration adapter
        # may load the verified artifact separately; this method returns only
        # the scoped receipt and recorded summary, never arbitrary file paths.
        root = self.task(self.task(run["task_id"])["root_task_id"])
        if record_read:
            self.db.execute("INSERT OR REPLACE INTO review_evidence_reads VALUES(?,?,?,?,?)", (root["id"], snapshot["id"], evidence_id, run["id"], time.time()))
        return {"evidence_id": evidence_id, "snapshot_id": snapshot["id"], **evidence}

    def record_goal_review(self, run, *, observed_facts, hypotheses, uncertainties, decision, rationale, evidence_refs, next_check, next_validation):
        if not self.current(run):
            raise ValueError("stale review")
        task = self.task(run["task_id"])
        request = self.one("SELECT * FROM goal_review_requests WHERE task_id=?", (task["id"],))
        context = self.review_context(run)
        if task["parent_task_id"] or not request or not context or context["state"] != "available":
            raise ValueError("只有本次复盘负责人可以保存结论")
        if request["experiment_id"] and self.experiment(request["experiment_id"])["version"] != request["experiment_version"]:
            raise ValueError("实验已改版，请基于新版本重新发起复盘")
        for values in (observed_facts, hypotheses, uncertainties):
            if not isinstance(values, list) or len(values) > 16 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 1000 for x in values):
                raise ValueError("观察、假设和不确定性应各为最多 16 条简短记录")
        if not observed_facts or not uncertainties or decision not in {"keep", "stop", "modify", "insufficient"}:
            raise ValueError("需要实际观察、明确决定并披露不确定性")
        rationale, next_validation = short_text(rationale, "决定理由"), short_text(next_validation, "下次检验条件")
        if not request["experiment_id"] and historical_phase_error(encode([observed_facts, hypotheses, uncertainties, rationale])):
            raise ValueError("这是已截取结束的历史快照，没有关联实验或未满的观察期。请写‘长期效果尚未测量’，不要套用实验阶段结论。")
        if type(next_check) not in (int, float) or not math.isfinite(next_check) or not time.time() < next_check <= time.time() + 366 * 86400:
            raise ValueError("下次检验时间须为未来一年内的 Unix 秒数")
        reads = {r["evidence_id"] for r in self.all("SELECT evidence_id FROM review_evidence_reads WHERE task_id=? AND snapshot_id=?", (task["id"], request["snapshot_id"]))}
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 32 or any(r not in reads for r in evidence_refs):
            raise ValueError("结论只能引用本次实际读取的 evidence_id，不是 operation_id。已读取可引用：" + ", ".join(sorted(reads)))
        catalog = context["evidence_catalog"]
        artifact_ids = {e["id"] for e in catalog if e["kind"] == "artifact_delivery"}
        if artifact_ids and not artifact_ids.intersection(evidence_refs):
            raise ValueError("本次有工作成果，需要实际读取并引用至少一份成果再复盘")
        for evidence_id in evidence_refs:
            evidence = self.read_review_evidence(run, evidence_id=evidence_id, record_read=False)
            if evidence["kind"] != "artifact_delivery":
                continue
            pages = [o["result"]["artifact_page"] for o in self.operations(task["id"]) if o["tool"] == "read_review_evidence" and o["status"] == "succeeded" and (o["result"] or {}).get("snapshot_id") == request["snapshot_id"] and o["result"].get("evidence_id") == evidence_id and "artifact_page" in o["result"]]
            covered, total = 0, None
            for page in sorted(pages, key=lambda p: p["start"]):
                if page["sha256"] != evidence["data"]["artifact"]["sha256"] or page["start"] > covered or total is not None and page["total"] != total:
                    raise ValueError("引用产物前需要完整读取与快照一致的所有分页")
                total, covered = page["total"], max(covered, page["end"])
            if total is None or covered != total:
                raise ValueError("引用产物前需要完整读取与快照一致的所有分页")
        review_id = gid()
        with self.transaction():
            self.db.execute("INSERT INTO goal_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (review_id, task["goal_id"], task["id"], run["id"], run["revision"], request["snapshot_id"], request["experiment_id"], request["experiment_version"], encode(observed_facts), encode(hypotheses), encode(uncertainties), decision, rationale, encode(evidence_refs), next_check, next_validation, time.time()))
            self.goal_event(task["goal_id"], "review_recorded", {"review_id": review_id, "decision": decision, "next_check": next_check, "policy_change_applied": False}, run["id"])
        return {"review_id": review_id, "state": "assessment_recorded", "policy_change_applied": False, "next_check_is_proposed": True, "trust": "Evidence-linked assessment, not proof of improvement. User must confirm changes and future reminders."}

    def completion_errors(self, run):
        errors = super().completion_errors(run)
        if run["task_id"] and self.one("SELECT task_id FROM goal_review_requests WHERE task_id=?", (run["task_id"],)):
            if not self.one("SELECT id FROM goal_reviews WHERE task_id=? AND request_revision=?", (run["task_id"], run["revision"])):
                errors.append("This review needs record_goal_review with observed evidence, uncertainties and a next validation condition before completion.")
        return errors

    def review_report(self, review_id, account="local-user"):
        """A factual projection of frozen receipts, separate from model prose."""
        review = self.one("SELECT * FROM goal_reviews WHERE id=?", (review_id,))
        if not review:
            raise ValueError("复盘记录不存在")
        goal = self.goal(review["goal_id"], account)
        snapshot = self.one("SELECT * FROM review_snapshots WHERE id=?", (review["snapshot_id"],))
        if digest(snapshot["payload"].encode()) != snapshot["sha256"]:
            raise ValueError("复盘快照校验失败")
        payload = json.loads(snapshot["payload"])
        metrics, evidence = payload["metrics"], payload["evidence"]
        stamp = lambda value: datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%Y年%m月%d日 %H:%M")
        documents = [(key, item) for key, item in evidence.items() if item["kind"] == "artifact_delivery"]
        facts = [f"截取范围：{stamp(snapshot['window_start'])} 至 {stamp(snapshot['window_end'])}（北京时间）。",
                 "本次为历史记录复盘，没有关联实验。" if not review["experiment_id"] else f"本次关联观察计划第 {review['experiment_version']} 版；结论只覆盖已截取的证据。",
                 f"窗口内创建 {metrics['root_tasks_created']} 项根任务（包含工作和复盘），另有 {metrics['worker_tasks_created']} 项后台协作者任务。",
                 f"上述任务中当前通过结构检查的共 {metrics['tasks_currently_accepted']} 项；结构检查不等于内容无误或现实效果提升。",
                 f"本次证据目录收录文档回执 {len(documents)} 份（最多收录最近 16 份），后台协作者数不代表成果数。",
                 f"窗口内关联任务执行的模型响应 {metrics['model_calls_with_response']} 次，输入 {metrics['input_tokens']} token，输出 {metrics['output_tokens']} token；失败工具操作 {metrics['failed_tool_operations']} 次。",
                 "这一项只包含关联到本事项的任务执行；对话接待等未归入本事项的调用不在此列，不能据此当作全部收费或实际账单。",
                 f"窗口内有 {metrics['user_feedback_count']} 条用户反馈，{metrics['user_followups']} 条任务补充；反馈内容是用户报告。",
                 "人工管理耗时未填写，保持未知。" if metrics['reported_management_seconds'] is None else f"已填写反馈中的人工耗时合计 {metrics['reported_management_seconds']:g} 秒；未填写的投入不包含在内。",
                 "未接入业务效果测量和实际账单金额，不能据此证明采纳率、满意度或节省时间。"]
        state = evidence.get('goal_state', {}).get('data', {})
        for health in state.get('health', []):
            failures = health.get('consecutive_failures', health.get('failures'))
            facts.append(f"来源 {health['source_id']}：累计登记检查 {health['checks']} 次，快照时连续失败 {failures} 次；连续失败数不是全部历史失败总数。")
        feedback = [item['data'] for item in evidence.values() if item['kind'] == 'user_report']
        opinions = {k: json.loads(review[k]) for k in ['hypotheses','uncertainties','evidence_refs']}
        decision = {'keep':'保留','stop':'停止','modify':'调整','insufficient':'证据不足'}[review['decision']]
        text = '# ' + goal['title'] + ' · 复盘记录\n\n## 账本事实\n\n' + '\n'.join('- '+fact for fact in facts)
        if feedback:
            text += '\n\n## 用户反馈原文\n\n' + '\n'.join('- '+item['note'] for item in feedback)
        text += '\n\n## 模型建议（需要核对）\n\n建议：' + decision + '。\n\n' + review['rationale']
        text += '\n\n以上是模型解释，可能过度推断；次数、状态和测量范围以账本事实为准。'
        text += '\n\n假设：\n' + '\n'.join('- '+item for item in opinions['hypotheses'])
        text += '\n\n尚不确定：\n' + '\n'.join('- '+item for item in opinions['uncertainties'])
        text += '\n\n## 下一次检验\n\n建议时间：' + stamp(review['next_check']) + '（北京时间）。\n\n检验条件：' + review['next_validation']
        text += '\n\n建议不会自动修改检查计划、权限或提醒时间。\n\n证据引用：' + '、'.join(opinions['evidence_refs']) + '\n'
        return {'id': review_id, 'facts': facts, 'text': text, 'sha256': digest(text.encode()), 'task_id': review['task_id'], 'snapshot_id': snapshot['id'], 'fact_source': 'frozen_ledger_projection', 'model_interpretation_separate': True}

    def review_artifact_errors(self, run, artifact_ref):
        request = self.one("SELECT experiment_id FROM goal_review_requests WHERE task_id=?", (run["task_id"],))
        if not request or request["experiment_id"]:
            return []
        writes = [o for o in self.operations(run["task_id"]) if o["tool"] == "write_artifact" and o["status"] == "succeeded" and (o["result"] or {}).get("artifact_ref") == artifact_ref]
        if writes and historical_phase_error(writes[-1]["arguments"]["content"]):
            return ["本次历史复盘没有关联实验，报告不能套用未满观察期或实验成败。请改为长期效果尚未测量，并用当前 base_version 修正报告。"]
        return []

    def tick_review_reminders(self, *, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            for row in self.all("SELECT e.*,g.room_id FROM experiments e JOIN goals g ON g.id=e.goal_id WHERE e.state='observing' AND g.status='active' AND e.next_check<=?", (now,)):
                inserted = self.db.execute("INSERT OR IGNORE INTO experiment_reminders VALUES(?,?,?,?)", (row["id"], row["version"], row["next_check"], now))
                if inserted.rowcount:
                    self._notice(None, row["room_id"], f"「{row['title']}」到了约定的复盘时间。可以在持续目标中查看已有证据，再决定是否发起复盘。尚未自动调用模型，也不代表实验已有结论。")
