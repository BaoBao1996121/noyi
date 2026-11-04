"""Local user's persistent goal controls; no model-facing registration API."""
from dataclasses import asdict
import json
from pathlib import Path
import time

from .contracts import TaskSpec, digest, encode


def goal_snapshot(store, room):
    result = []
    for row in store.all("SELECT id FROM goals WHERE room_id=? AND owner='local-user' ORDER BY created DESC", (room,)):
        goal = store.goal(row["id"], "local-user")
        policies = []
        for p in store.all("SELECT id FROM routine_policies WHERE goal_id=? ORDER BY created", (goal["id"],)):
            policy = store.policy(p["id"])
            policy["template"]["sources"] = [{k: v for k, v in s.items() if k != "location"} for s in policy["template"]["sources"]]
            policy["health"] = store.all("SELECT source_id,state,sha256,checked_at,checks,failures,error_type FROM source_health WHERE policy_id=?", (p["id"],))
            policies.append(policy)
        rules = [store.admission_rule(r["id"]) for r in store.all("SELECT id FROM admission_rules WHERE goal_id=? ORDER BY created", (goal["id"],))]
        for rule in rules:
            rule["template"]["sources"] = [{k: v for k, v in s.items() if k != "location"} for s in rule["template"]["sources"]]
        goal.update(policies=policies, rules=rules,
                    occurrences=store.all("SELECT o.id,o.policy_id,o.status,o.task_id,o.reason,o.scheduled_at,o.observed_at FROM policy_occurrences o JOIN routine_policies p ON p.id=o.policy_id WHERE p.goal_id=? ORDER BY o.created DESC LIMIT 12", (goal["id"],)),
                    admissions=store.all("SELECT d.id,d.state,d.reason,d.task_id,d.created FROM admission_decisions d JOIN admission_rules r ON r.id=d.rule_id WHERE r.goal_id=? ORDER BY d.created DESC LIMIT 12", (goal["id"],)),
                    proposals=store.all("SELECT * FROM policy_proposals WHERE goal_id=? AND state IN ('pending','approved') ORDER BY created", (goal["id"],)))
        result.append(goal)
    return result


def user_template(body):
    sources = []
    locations = body.get("sources_text", "")
    if not isinstance(locations, str) or len(locations) > 15000:
        raise ValueError("来源列表过长")
    for i, value in enumerate(line.strip().strip('"') for line in locations.splitlines() if line.strip()):
        kind = "http" if value.startswith(("https://", "http://")) else "file"
        if kind == "file":
            path = Path(value)
            if not path.is_absolute():
                raise ValueError("本地来源请填写完整文件路径")
            value = str(path.resolve())
        sources.append({"id": "source" + str(i + 1), "kind": kind, "location": value, "required": True})
    spec = TaskSpec(body["objective"], sources=sources, max_calls=body.get("max_calls", 12), max_tools=40, max_workers=2, enable_nudge=True, review_workers=True)
    spec.validate()
    return asdict(spec)


def goal_action(store, name, body):
    if not isinstance(body.get("nonce"), str) or not 1 <= len(body["nonce"]) <= 150:
        raise ValueError("操作缺少重复提交标识，请重新打开表单")
    # Management retries are atomic too: a lost browser response cannot create
    # a second goal, registration, budget extension or signal.
    with store.transaction():
        store.db.execute("CREATE TABLE IF NOT EXISTS goal_ui_receipts(nonce TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,result TEXT NOT NULL)")
        payload = digest(encode({"name": name, "body": body}).encode())
        prior = store.one("SELECT * FROM goal_ui_receipts WHERE nonce=?", (body["nonce"],))
        if prior:
            if prior["payload_hash"] != payload:
                raise ValueError("重复提交标识已被其他操作使用")
            return {**json.loads(prior["result"]), "duplicate": True}
        result = _apply(store, name, body)
        store.db.execute("INSERT INTO goal_ui_receipts VALUES(?,?,?)", (body["nonce"], payload, encode(result)))
        return result


def _apply(s, name, b):
    if name == "create":
        return s.create_goal(room=b["room"], actor_id=b["actor_id"], title=b["title"], objective=b["objective"], calls_limit=b.get("calls_limit", 120), tools_limit=b.get("tools_limit", 400), allow_policy_adjustments=b.get("allow_policy_adjustments", False))
    if name == "state":
        return s.set_goal_state(b["id"], b["status"], expected_revision=b["revision"])
    if name == "budget":
        return s.extend_goal_budget(b["id"], calls=b.get("calls", 0), tools=b.get("tools", 0))
    if name == "create_policy":
        return s.create_policy(b["id"], schedule=b["schedule"], template=user_template(b))
    if name == "update_policy":
        p = s.policy(b["id"], "local-user")
        return s.update_policy(p["id"], schedule=b.get("schedule", p["schedule"]), template=user_template(b) if "objective" in b else p["template"], expected_revision=b["revision"], state=b.get("state", p["state"]))
    if name == "check":
        result = s.request_policy_check(b["id"], nonce=b["nonce"])
        if not result:
            raise ValueError("请先恢复目标和检查计划，再立即检查")
        return result
    if name == "create_rule":
        goal = s.goal(b["id"], "local-user")
        return s.create_admission_rule(goal["id"], mode=b["mode"], source="local_signal", thread=goal["room_id"], contains_text=b["contains_text"], template=user_template(b), expires=b["expires"], cooldown=b.get("cooldown", 300), max_occurrences=b.get("max_occurrences", 10))
    if name == "rule_mode":
        return s.set_admission_mode(b["id"], b["mode"], expected_revision=b["revision"])
    if name == "signal":
        s.owned_room(b["room"], "local-user")
        text = b["text"]
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 12000:
            raise ValueError("线索须为 1–12000 字")
        return s.followup(text, nonce=b["nonce"], source="local_signal", room=b["room"], context=True)
    if name == "proposal":
        return s.decide_policy_proposal(b["id"], accept=b["accept"])
    raise ValueError("未知持续目标操作")
