"""Ledger replay: one task's walk through the ledger, read back in time order.

Read only. Every event is a row that already exists in the ledger; nothing is
written, sent or scheduled. A time the ledger never recorded stays unknown
(``at`` is null) instead of being estimated, and the event keeps the position
it has in its own table.
"""
from __future__ import annotations

import json
import time

from .commitment_store import due_label

STATIONS = [
    {"id": "intake", "label": "回复层判断"},
    {"id": "commit", "label": "登记承诺"},
    {"id": "start", "label": "开工"},
    {"id": "model", "label": "模型请求"},
    {"id": "tool", "label": "工具调用"},
    {"id": "deliver", "label": "交付"},
    {"id": "fulfil", "label": "承诺兑现"},
]
STATION_INDEX = {s["id"]: i for i, s in enumerate(STATIONS)}

ACTIONS = {"new_task": "接下这件事", "followup": "按补充要求改", "resume": "接着做", "goal_review": "开始复盘",
           "reply": "直接回答", "clarify": "先问清楚", "commitment": "改约或不要了"}
TRIGGERS = {"user_task": "你交代的", "conversation": "对话里接下的", "user_resume": "你让我继续", "recovered": "服务重启后接着做",
            "followup": "你补充了要求", "child_result": "后台协作有结果", "answer": "你回答了问题"}
OP_STATES = {"succeeded": "成功", "failed": "失败", "interrupted": "中断", "running": "进行中"}
REQUEST_STATES = {"completed": "完成", "failed": "失败", "unknown": "结果未知", "dispatching": "进行中"}
DONE_REASONS = {"accepted", "following_action_completed"}
TOLD_KINDS = {"blocked", "overdue_told", "blocked_told", "disclosed", "unable", "released"}


def _known(at):
    return at if isinstance(at, (int, float)) and at > 0 else None


def _loads(text):
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _event(station, kind, tone, title, *, at, text="", facts=None, end=None, has_end=False, links=None, ongoing=False):
    event = {"station": station, "kind": kind, "tone": tone, "title": title, "at": _known(at), "text": text or "",
             "facts": [[k, str(v)] for k, v in (facts or []) if v not in (None, "")], "links": links or []}
    event["time"] = "known" if event["at"] is not None else "unknown"
    if has_end:
        event["end"] = _known(end)
        event["end_time"] = "known" if event["end"] is not None else ("running" if ongoing else "unknown")
    return event


def _target(arguments):
    for key in ("url", "query", "source_id", "name", "artifact_ref", "question"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value[:160]
    return None


class ReplayView:
    """Builds the timeline of one root task from rows already in the ledger."""

    def __init__(self, store, task_id):
        self.store = store
        task = store.task(task_id)
        self.root = store.task(task["root_task_id"])
        self.tasks = [r["id"] for r in store.all("SELECT id FROM tasks WHERE root_task_id=? ORDER BY created,id", (self.root["id"],))]
        self.marks = ",".join("?" * len(self.tasks))
        self.deliveries = {d["id"]: d for d in store.all(f"SELECT * FROM deliveries WHERE task_id IN ({self.marks}) ORDER BY rowid", self.tasks)}

    def delivery_text(self, delivery_id):
        if not delivery_id:
            return ""
        row = self.deliveries.get(delivery_id) or self.store.one("SELECT content FROM deliveries WHERE id=?", (delivery_id,))
        return (row or {}).get("content") or ""

    # ---- sources: each list is in its own table's order --------------------
    def intake(self):
        s, root = self.store, self.root["id"]
        rows = s.all("""SELECT c.*,m.content,(SELECT MIN(e.created) FROM events e WHERE e.message_id=c.message_id) AS received
            FROM conversation_inputs c JOIN messages m ON m.id=c.message_id
            WHERE json_extract(c.receipt,'$.task_id')=? OR json_extract(c.decision,'$.task_id')=? ORDER BY c.created,c.id""", (root, root))
        out = []
        if not rows:
            # Work handed over without the conversation layer (CLI, schedules).
            first = s.one("SELECT m.content,(SELECT MIN(e.created) FROM events e WHERE e.message_id=m.id) AS received FROM messages m WHERE m.task_id=? ORDER BY m.rowid LIMIT 1", (root,))
            if first:
                out.append(_event("intake", "received", "info", "收到", at=first["received"], text=first["content"]))
            return out
        for item in rows:
            decision = _loads(item["decision"])
            out.append(_event("intake", "received", "info", "收到你的话", at=item["received"], text=item["content"]))
            for refused in s.all("SELECT * FROM commitment_events WHERE kind='unregistered_refused' AND json_extract(data,'$.message_id')=? ORDER BY created,id", (item["message_id"],)):
                data = _loads(refused["data"])
                out.append(_event("intake", "unregistered_refused", "bad", "口头许诺被账本拦下", at=refused["created"], text=data.get("quote"),
                                  facts=[["原本想说", data.get("text")], ["结果", "没登记负责的任务，不准说出口"]]))
            run = s.one("SELECT id FROM runs WHERE trigger='conversation' AND json_extract(context_manifest,'$.current_message_id')=?", (item["message_id"],))
            said = s.one("SELECT content FROM deliveries WHERE run_id=?", (run["id"],)) if run else None
            rejected = s.one("SELECT COUNT(*) AS n FROM steps WHERE run_id=? AND kind='conversation_rejected'", (run["id"],))["n"] if run else 0
            out.append(_event("intake", "decided", "ok", ACTIONS.get(decision.get("action"), "判断"), at=item["updated"],
                              text=(said or {}).get("content", ""), facts=[["判断", ACTIONS.get(decision.get("action"), decision.get("action"))],
                                                                          ["被账本退回", f"{rejected} 次" if rejected else None]]))
        return out

    def commitments(self):
        s, out = self.store, []
        rows = s.all(f"""SELECT e.*,c.quote,c.task_id,c.due_at,c.due_basis,c.due_quote,c.origin,c.supersedes FROM commitment_events e
            JOIN commitments c ON c.id=e.commitment_id WHERE c.task_id IN ({self.marks}) ORDER BY e.created,e.id""", self.tasks)
        for row in rows:
            data, kind = _loads(row["data"]), row["kind"]
            if kind == "registered":
                due = None
                if data.get("due_at"):
                    who = {"user": "你说的", "assistant": "我说的", "schedule": "约定的提醒时间"}.get(data.get("due_basis"), "")
                    due = due_label(data["due_at"]) + (f"（{who}：{row['due_quote']}）" if row["due_quote"] else f"（{who}）" if who else "")
                out.append(_event("commit", kind, "ok", "改约后重新登记" if data.get("supersedes") else "登记承诺", at=row["created"], text=data.get("quote") or row["quote"],
                                  facts=[["时限", due or "没说时限"], ["来历", "升级账本时按旧回执补登" if data.get("origin") == "backfill" else None]]))
            elif kind == "superseded":
                text = f"你说「{data['basis']}」，换成新的约定" if data.get("basis") else "你补充了要求，按新要求重新登记：" + self.delivery_text(data.get("by_delivery_id"))
                out.append(_event("commit", kind, "warn", "改约", at=row["created"], text=text, facts=[["原来的约定", row["quote"]]]))
            elif kind == "arranged":
                out.append(_event("commit", kind, "info", "安排好了", at=row["created"], text=row["quote"]))
            elif kind == "blocked":
                # The user-facing line already says why; internal reason codes stay in the ledger.
                out.append(_event("start", kind, "warn", "卡住了", at=row["created"], text=self.delivery_text(data.get("delivery_id")) or "停在半路，进度已保存"))
            elif kind == "resumed":
                out.append(_event("start", kind, "info", "接着做", at=row["created"]))
            elif kind == "fulfilled":
                facts, links = [], []
                for item in data.get("evidence") or []:
                    if item.get("kind") == "artifact":
                        facts.append(["成果校验值", item.get("sha256")])
                        links.append({"label": "查看成果", "url": "/r/" + item["task_id"]})
                    elif item.get("kind") == "operation":
                        facts.append(["成功操作", item.get("id")])
                    elif item.get("kind") == "page":
                        facts.append(["读过的网页", item.get("page_id")])
                    elif item.get("kind") == "delivery":
                        facts.append(["告知你的消息", self.delivery_text(item.get("id"))[:80]])
                out.append(_event("fulfil", kind, "ok", "承诺兑现", at=row["created"], text=row["quote"], facts=facts, links=links))
            elif kind == "unable":
                out.append(_event("fulfil", kind, "warn", "做不到，如实告诉你", at=row["created"], text=self.delivery_text(data.get("closing_delivery_id")) or data.get("reason"),
                                  facts=[["原来的约定", row["quote"]]]))
            elif kind == "released":
                out.append(_event("fulfil", kind, "warn", "不再继续", at=row["created"], text=data.get("reason") or "", facts=[["原来的约定", row["quote"]]]))
            elif kind == "overdue_told":
                out.append(_event("fulfil", kind, "bad", "逾期，已如实提醒", at=row["created"], text=self.delivery_text(data.get("delivery_id")),
                                  facts=[["约定时间", due_label(row["due_at"]) if row["due_at"] else None], ["到点时服务", "没开，开机后补提" if data.get("service_was_down") else None]]))
            elif kind == "blocked_told":
                out.append(_event("fulfil", kind, "bad", "卡住超过一天，已提醒", at=row["created"], text=self.delivery_text(data.get("delivery_id"))))
            elif kind == "disclosed":
                out.append(_event("fulfil", kind, "info", "告诉你结果", at=row["created"], text=self.delivery_text(data.get("delivery_id"))))
            elif kind == "arrangement_confirmed":
                out.append(_event("fulfil", kind, "info", "安排已确认", at=row["created"], text=row["quote"]))
        return out

    def runs(self):
        out, root = [], self.root["id"]
        for run in self.store.all(f"SELECT * FROM runs WHERE task_id IN ({self.marks}) ORDER BY started,id", self.tasks):
            child = run["task_id"] != root
            title = "后台协作开工" if child else ("接着做" if run["trigger"] in {"user_resume", "recovered"} else "开工")
            seconds = round(run["ended"] - run["started"]) if _known(run["ended"]) and _known(run["started"]) else None
            out.append(_event("start", "run", "info", title, at=run["started"], end=run["ended"], has_end=True, ongoing=run["phase"] == "running",
                              facts=[["起因", TRIGGERS.get(run["trigger"], run["trigger"])], ["第几轮", run["epoch"]], ["用时", f"{seconds} 秒" if seconds is not None else None]]))
        return out

    def model_requests(self):
        out, s = [], self.store
        runs = [r["id"] for r in s.all(f"SELECT id FROM runs WHERE task_id IN ({self.marks}) ORDER BY started,id", self.tasks)]
        for run_id in runs:
            requests = s.all("SELECT * FROM model_requests WHERE run_id=? ORDER BY sequence", (run_id,))
            steps = [_loads(r["data"]) for r in s.all("SELECT data FROM steps WHERE run_id=? AND kind='model' ORDER BY id", (run_id,))]
            completed = [r for r in requests if r["state"] == "completed"]
            # Usage is only attributed when every completed request has exactly one model step.
            usage = dict(zip((r["id"] for r in completed), steps)) if len(completed) == len(steps) else {}
            for r in requests:
                meta, step = _loads(r["metadata"]), usage.get(r["id"])
                facts = [["模型", (step or {}).get("model") or meta.get("requested_model") or "未知"],
                         ["输入 token", step["input_tokens"] if step else "未知"], ["输出 token", step["output_tokens"] if step else "未知"],
                         ["状态", REQUEST_STATES.get(r["state"], r["state"])]]
                out.append(_event("model", "request", "ok" if r["state"] == "completed" else "warn", f"第 {r['sequence']} 次模型请求",
                                  at=r["created"], end=r["ended"], has_end=True, ongoing=r["state"] == "dispatching", facts=facts))
        return out

    def operations(self):
        out = []
        for op in self.store.all(f"SELECT * FROM operations WHERE task_id IN ({self.marks}) ORDER BY created,id", self.tasks):
            arguments, result = _loads(op["arguments"]), _loads(op["result"])
            facts = [["工具", op["tool"]], ["状态", OP_STATES.get(op["status"], op["status"])], ["对象", _target(arguments)],
                     ["成果校验值", result.get("sha256")], ["验收", ("通过" if result.get("passed") else "没通过") if "passed" in result else None]]
            tone = "ok" if op["status"] == "succeeded" and result.get("passed") is not False else "warn"
            out.append(_event("tool", "operation", tone, op["tool"], at=op["created"], end=op.get("ended"), has_end=True, ongoing=op["status"] == "running", facts=facts))
        return out

    def acceptance(self):
        out = []
        for step in self.store.all(f"SELECT * FROM steps WHERE task_id IN ({self.marks}) AND kind='acceptance' ORDER BY id", self.tasks):
            data = _loads(step["data"])
            artifact = data.get("artifact") or {}
            out.append(_event("deliver", "accepted", "ok", "验收通过", at=step["created"], facts=[["成果校验值", artifact.get("sha256")], ["字节数", artifact.get("bytes")]]))
        return out

    def messages(self, told):
        out = []
        for d in self.deliveries.values():
            if d["id"] in told or d["kind"] in {"commitment", "question", "clarification"}:
                continue
            run = self.store.one("SELECT end_reason FROM runs WHERE id=?", (d["run_id"],))
            artifact = _loads(d["artifact"])
            if d["run_id"].startswith("notice:"):
                out.append(_event("start", "said", "info", "对你说", at=d["created"], text=d["content"]))
            elif run and run["end_reason"] not in DONE_REASONS and not artifact:
                out.append(_event("start", "stopped", "warn", "停下了", at=d["created"], text=d["content"]))
            else:
                out.append(_event("deliver", "delivered" if run else "message", "ok" if run else "info", "交付给你" if run else "发给你的消息",
                                  at=d["created"], text=d["content"], facts=[["成果校验值", artifact.get("sha256")]]))
        return out

    # ---- assembly ------------------------------------------------------------
    def told_deliveries(self):
        told = set()
        for row in self.store.all(f"SELECT e.kind,e.data FROM commitment_events e JOIN commitments c ON c.id=e.commitment_id WHERE c.task_id IN ({self.marks})", self.tasks):
            if row["kind"] in TOLD_KINDS:
                data = _loads(row["data"])
                told.update(v for v in (data.get("delivery_id"), data.get("closing_delivery_id")) if v)
        return told

    def outcome(self):
        row = self.store.one("SELECT * FROM commitments WHERE task_id=? AND state!='superseded' ORDER BY created DESC,rowid DESC LIMIT 1", (self.root["id"],))
        if not row:
            return "none"
        if row["state"] != "open":
            return row["state"]
        if row["overdue_notified"] or (row["due_at"] and row["due_at"] < time.time()):
            return "overdue"
        return "blocked" if row["blocked_since"] else "open"

    def build(self):
        sources = [self.intake(), self.commitments(), self.runs(), self.model_requests(), self.operations(), self.acceptance(), self.messages(self.told_deliveries())]
        keyed = []
        for source_index, events in enumerate(sources):
            previous = 0.0
            for position, event in enumerate(events):
                # Unknown time: keep the event right after its predecessor in its own table.
                event["order"] = "time" if event["at"] is not None else "inferred"
                key = event["at"] if event["at"] is not None else previous
                previous = key
                keyed.append(((key, event["at"] is None, STATION_INDEX[event["station"]], source_index, position), event))
        events = [e for _, e in sorted(keyed, key=lambda pair: pair[0])]
        for seq, event in enumerate(events):
            event["seq"] = seq
        unknown = sum(e["at"] is None for e in events) + sum(e.get("end_time") == "unknown" for e in events)
        objective = self.root["spec"]["objective"]
        return {"task": {"id": self.root["id"], "title": objective.split("\n")[0].split("。")[0][:80], "status": self.root["status"], "created": self.root["created"]},
                "stations": STATIONS, "outcome": self.outcome(), "unknown_times": unknown, "events": events}


class ReplayStore:
    """Schema 11: tools record when they ended, so replay can show real durations."""

    REPLAY_VERSION = 11

    def __init__(self, path):
        super().__init__(path)
        with self.transaction():
            if "ended" not in {r["name"] for r in self.all("PRAGMA table_info(operations)")}:
                # Old rows keep NULL: their end time was never recorded.
                self.db.execute("ALTER TABLE operations ADD COLUMN ended REAL")

    def task_timeline(self, task_id):
        return ReplayView(self, task_id).build()
