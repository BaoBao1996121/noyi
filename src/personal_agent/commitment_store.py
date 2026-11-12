"""Commitment ledger: what the assistant told the user it would do (C-1..C-4).

The model decides whether its words promise future work; code checks the facts.
A promise is registered only together with the task or arrangement responsible
for it, in the same transaction that sends the words. It ends only by:
fulfilled (evidence the ledger can find), unable, released or superseded, and
every ending that is not a fulfilment is told to the user in a message the
ledger links. There is no silent closing path.

Accepted work is not yet a kept promise: it is fulfilled only when the channel
acknowledged the message that hands the result to the user. Until then the
ledger records it as done but not delivered.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import uuid

from .authority import own_words, verify_user_words
from .contracts import digest, encode
from .schedule import once_at
from .speech import check_receipt

OPEN, CLOSED = "open", {"fulfilled", "unable", "released", "superseded"}
BLOCKED_REMINDER = 24 * 3600   # C-1: no deadline → remind once after a day stuck
SCHEDULED_GRACE = 600          # a reminder runs shortly after its time
DISCLOSURE_GRACE = 60          # an ending nobody was told about gets a code-written line
DOWNTIME_GAP = 120
CLOCK_WRITE = 30
TIMEZONE = "Asia/Shanghai"

COMMITMENTS_SQL = """CREATE TABLE IF NOT EXISTS commitments(
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL,
 quote TEXT NOT NULL, user_message_id TEXT, said_delivery_id TEXT,
 task_id TEXT, goal_id TEXT, policy_id TEXT,
 due_at REAL, due_basis TEXT, due_quote TEXT,
 state TEXT NOT NULL, supersedes TEXT, evidence TEXT NOT NULL DEFAULT '[]',
 closing_delivery_id TEXT, close_reason TEXT, origin TEXT NOT NULL DEFAULT 'live',
 blocked_since REAL, blocked_notified REAL, overdue_notified REAL,
 created REAL NOT NULL, closed REAL)"""
EVENTS_SQL = """CREATE TABLE IF NOT EXISTS commitment_events(
 id TEXT PRIMARY KEY, commitment_id TEXT, room_id TEXT NOT NULL, kind TEXT NOT NULL,
 data TEXT NOT NULL, created REAL NOT NULL)"""


def cid():
    return uuid.uuid4().hex


def parse_due(text):
    try:
        return once_at({"at": text, "timezone": TIMEZONE})
    except ValueError:
        raise ValueError("时限格式不对：due_at 要写成本地时间 YYYY-MM-DD HH:MM") from None


def due_label(at):
    return time.strftime("%m月%d日 %H:%M", time.localtime(at)).lstrip("0")


def backup_before_upgrade(path, target):
    """Copy an existing older ledger aside before this version changes it."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    source = sqlite3.connect(path)
    try:
        prior = source.execute("PRAGMA user_version").fetchone()[0]
        if not 0 < prior < target:
            return None
        folder = path.parent / "backups"
        folder.mkdir(exist_ok=True)
        destination = folder / f"state-v{prior}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
        copy = sqlite3.connect(destination)
        try:
            source.backup(copy)
        finally:
            copy.close()
        return destination
    finally:
        source.close()


class CommitmentStore:
    COMMITMENT_VERSION = 10

    def __init__(self, path):
        # The backup before upgrading is taken by Store for every schema version.
        super().__init__(path)
        with self.transaction():
            tables = {r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type='table'")}
            self.db.execute(COMMITMENTS_SQL)
            self.db.execute(EVENTS_SQL)
            self.db.execute("CREATE TABLE IF NOT EXISTS commitment_clock(id INTEGER PRIMARY KEY CHECK(id=1),last REAL NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS commitments_open ON commitments(state,due_at)")
            self.db.execute("CREATE INDEX IF NOT EXISTS commitments_task ON commitments(task_id)")
            if "commitments" not in tables:
                self._backfill_commitments()

    # ---- facts ---------------------------------------------------------------
    def _commitment_event(self, commitment, kind, data=None, room=None):
        self.db.execute("INSERT INTO commitment_events VALUES(?,?,?,?,?,?)",
                        (cid(), commitment["id"] if commitment else None, room or commitment["room_id"], kind, encode(data or {}), time.time()))

    def commitment(self, commitment_id):
        row = self.one("SELECT * FROM commitments WHERE id=?", (commitment_id,))
        if not row:
            raise ValueError("没有这条承诺")
        return row

    def register_commitment(self, *, room, account, quote, said_delivery_id, user_message_id=None, task_id=None,
                            goal_id=None, policy_id=None, kind="task", due=None, supersedes=None, origin="live", created=None):
        if not (task_id or goal_id or policy_id):
            raise ValueError("承诺必须有负责的任务或安排，不能只是一句话")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("承诺需要原话")
        due = due or {}
        row = {"id": cid(), "room_id": room, "account": account, "kind": kind, "quote": quote.strip()[:200],
               "user_message_id": user_message_id, "said_delivery_id": said_delivery_id, "task_id": task_id,
               "goal_id": goal_id, "policy_id": policy_id, "due_at": due.get("at"), "due_basis": due.get("basis"),
               "due_quote": due.get("quote"), "state": OPEN, "supersedes": supersedes, "origin": origin,
               "created": created or time.time()}
        columns = ",".join(row)
        self.db.execute(f"INSERT INTO commitments({columns}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        self._commitment_event(row, "registered", {"quote": row["quote"], "said_delivery_id": said_delivery_id, "task_id": task_id,
                                                    "due_at": row["due_at"], "due_basis": row["due_basis"], "origin": origin, "supersedes": supersedes})
        return row

    def _commitment_source(self, commitment, item, *, delivered_only=False):
        if not isinstance(item, dict):
            raise ValueError("出处格式无效")
        kind = item.get("kind")
        sent = " AND state='accepted'" if delivered_only else ""
        if kind == "artifact":
            delivered = [json.loads(d["artifact"]) for d in self.all("SELECT artifact FROM deliveries WHERE task_id=? AND artifact IS NOT NULL" + sent, (item.get("task_id"),))]
            if not any(a.get("ref") == item.get("ref") and a.get("sha256") == item.get("sha256") for a in delivered):
                raise ValueError("这份成果没有送达你的交付记录" if delivered_only else "这份成果没有通过验收的交付记录")
            from .tools import Toolset
            path = Toolset(self.conversation_data, self.task(item["task_id"])).artifact_path(item["ref"])
            if digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("成果文件已经变化，和交付时的校验值对不上")
            return {"kind": "artifact", "task_id": item["task_id"], "ref": item["ref"], "sha256": item["sha256"], "url": "/r/" + item["task_id"]}
        if kind == "operation":
            if not self.one("SELECT 1 FROM operations WHERE id=? AND task_id=? AND status='succeeded'", (item.get("id"), item.get("task_id"))):
                raise ValueError("账本里没有这次成功操作")
            return {"kind": "operation", "task_id": item["task_id"], "id": item["id"]}
        if kind == "page":
            pages = [json.loads(p["result"] or "{}") for p in self.all("SELECT result FROM operations WHERE task_id=? AND tool='fetch_page' AND status='succeeded'", (item.get("task_id"),))]
            read = next((p for p in pages if p.get("page_id") == item.get("page_id")), None)
            if not read:
                raise ValueError("这项任务没有成功读过这个网页")
            if item.get("sha256") and item["sha256"] != read.get("sha256"):
                raise ValueError("这个网页读到的不是这一版内容")
            # The page as it was read, not whatever the address shows today.
            return {"kind": "page", "task_id": item["task_id"], "page_id": item["page_id"], "sha256": read.get("sha256"), "url": read.get("url")}
        if kind == "delivery":
            row = self.one("SELECT state FROM deliveries WHERE id=? AND room_id=?", (item.get("id"), commitment["room_id"]))
            if not row:
                raise ValueError("这条消息不在这个会话里")
            if delivered_only and row["state"] != "accepted":
                raise ValueError("这条消息还没送达，不能作为兑现的出处")
            return {"kind": "delivery", "id": item["id"]}
        raise ValueError("未知出处类型")

    def close_commitment(self, commitment_id, state, evidence, *, reason=None, closing_delivery_id=None):
        """The only way a commitment ends; every item is checked against the ledger."""
        if state not in CLOSED:
            raise ValueError("无效承诺状态")
        with self.transaction():
            commitment = self.commitment(commitment_id)
            if commitment["state"] != OPEN:
                raise ValueError("这条承诺已经收口")
            fulfil = state == "fulfilled"
            checked = [self._commitment_source(commitment, item, delivered_only=fulfil) for item in evidence or []]
            if closing_delivery_id:
                checked.append(self._commitment_source(commitment, {"kind": "delivery", "id": closing_delivery_id}, delivered_only=fulfil))
            if fulfil and not checked:
                raise ValueError("兑现必须引用能核对的出处")
            now = time.time()
            self.db.execute("UPDATE commitments SET state=?,evidence=?,closing_delivery_id=?,close_reason=?,closed=? WHERE id=?",
                            (state, encode(checked), closing_delivery_id, reason, now, commitment_id))
            self._commitment_event(commitment, state, {"evidence": checked, "closing_delivery_id": closing_delivery_id, "reason": reason})
        return self.commitment(commitment_id)

    def _open_for_task(self, task_id, kinds=("task",)):
        return self.all("SELECT * FROM commitments WHERE task_id=? AND state='open' AND kind IN (%s) ORDER BY created" % ",".join("?" * len(kinds)), (task_id, *kinds))

    def _link_disclosure(self, delivery_id, *, task_id=None, goal_id=None):
        """The next message the user gets about the matter tells them how it ended."""
        rows = self.all("SELECT * FROM commitments WHERE state IN ('unable','released') AND closing_delivery_id IS NULL AND ((task_id IS NOT NULL AND task_id=?) OR (goal_id IS NOT NULL AND goal_id=?))", (task_id, goal_id))
        for row in rows:
            evidence = json.loads(row["evidence"]) + [{"kind": "delivery", "id": delivery_id}]
            self.db.execute("UPDATE commitments SET closing_delivery_id=?,evidence=? WHERE id=?", (delivery_id, encode(evidence), row["id"]))
            self._commitment_event(row, "disclosed", {"delivery_id": delivery_id})

    # ---- intake ----------------------------------------------------------------
    def _commitment_due(self, item, decision, receipt_text):
        if not decision.get("due_at"):
            if decision.get("due_basis"):
                raise ValueError("给了时限依据却没有 due_at；没有说时间就都不要填")
            return None
        at = parse_due(decision["due_at"])
        if at <= time.time():
            raise ValueError("时限已经过去了；请按用户说的时间重新换算，拿不准就问用户")
        basis = (decision.get("due_basis") or "").strip()
        if len(basis) >= 2 and basis in own_words(item["content"]):
            return {"at": at, "basis": "user", "quote": basis}
        if len(basis) >= 2 and receipt_text and basis in receipt_text:
            return {"at": at, "basis": "assistant", "quote": basis}
        raise ValueError("时限依据要逐字摘自用户这条消息或你的回执原话；没人说过时间就不要设时限")

    def apply_conversation(self, run, decision):
        if isinstance(decision, dict) and decision.get("action") in {"reply", "clarify"}:
            if "promise" not in decision or not isinstance(decision["promise"], str):
                raise ValueError("reply/clarify 必须填写 promise：这段话里答应以后去做的事的原话，没有就填空字符串")
            promise = decision["promise"].strip()
            if promise:
                item = self.conversation_input(run)
                with self.transaction():
                    self._commitment_event(None, "unregistered_refused", {"quote": promise[:200], "text": (decision.get("text") or "")[:500], "message_id": item["message_id"] if item else None}, room=item["room_id"] if item else "local")
                raise ValueError("这句话答应了以后要做的事，但没有登记负责的任务。要做就改用 new_task 真的接下；做不到就只说现在能做的，不要许诺")
        due = None
        if isinstance(decision, dict) and decision.get("action") in {"new_task", "followup"}:
            item = self.conversation_input(run)
            if item and item["state"] == "pending":
                due = self._commitment_due(item, decision, check_receipt(decision.get("receipt")))
        elif isinstance(decision, dict) and (decision.get("due_at") or decision.get("due_basis")) and decision.get("action") != "commitment":
            raise ValueError("只有接下工作或改约时才能设时限")
        with self.transaction():
            item = self.conversation_input(run)
            result = super().apply_conversation(run, decision)
            if result.get("duplicate") or not item:
                return result
            said = self.one("SELECT id,content FROM deliveries WHERE run_id=?", (run["id"],))
            action, task_id = decision["action"], result.get("task_id")
            if said and action in {"new_task", "followup"} and task_id:
                previous = []
                if action == "followup":
                    previous = self._open_for_task(decision["task_id"], ("task", "scheduled", "standing"))
                    for old in previous:
                        self.db.execute("UPDATE commitments SET state='superseded',closing_delivery_id=?,evidence=?,closed=? WHERE id=?",
                                        (said["id"], encode([{"kind": "delivery", "id": said["id"]}]), time.time(), old["id"]))
                        self._commitment_event(old, "superseded", {"by_delivery_id": said["id"], "task_id": task_id})
                if not due and previous and previous[-1]["due_at"]:
                    old = previous[-1]
                    due = {"at": old["due_at"], "basis": old["due_basis"], "quote": old["due_quote"]}
                self.register_commitment(room=item["room_id"], account=item["account"], quote=said["content"], said_delivery_id=said["id"],
                                         user_message_id=item["message_id"], task_id=task_id, due=due,
                                         supersedes=previous[-1]["id"] if previous else None)
            return result

    def finish_conversation(self, run, content, *, decision=None, receipt=None, failed=False):
        result = super().finish_conversation(run, content, decision=decision, receipt=receipt, failed=failed)
        said = self.one("SELECT id FROM deliveries WHERE run_id=?", (run["id"],))
        if said and receipt and (receipt.get("goal_id") or receipt.get("task_id")) and (decision or {}).get("action") not in {"new_task", "followup"}:
            self._link_disclosure(said["id"], task_id=receipt.get("task_id"), goal_id=receipt.get("goal_id"))
        return result

    def conversation_context(self, run):
        context = super().conversation_context(run)
        rows = self.all("SELECT * FROM commitments WHERE room_id=? AND state='open' ORDER BY created DESC LIMIT 12", (context["room"]["id"],))
        now = time.time()
        context["open_commitments"] = [{"id": r["id"], "said": r["quote"], "kind": r["kind"], "task_id": r["task_id"], "goal_id": r["goal_id"],
                                        "due": due_label(r["due_at"]) if r["due_at"] else None, "overdue": bool(r["due_at"] and r["due_at"] < now)} for r in rows]
        return context

    def _conversation_commitment(self, run, item, decision):
        """C-3: only the user's own words change the time of, or drop, a promise."""
        commitment = self.commitment(decision.get("commitment_id"))
        if commitment["room_id"] != item["room_id"] or commitment["state"] != OPEN:
            raise ValueError("这条承诺不在当前会话，或已经收口")
        op = decision.get("commitment_op")
        if op not in {"reschedule", "release"}:
            raise ValueError("commitment_op 只能是 reschedule 或 release")
        basis = verify_user_words(item["content"], decision.get("basis"), "改约或不要了，必须是用户自己说的")
        if op == "reschedule":
            if not decision.get("due_at"):
                raise ValueError("改约需要新的 due_at")
            at = parse_due(decision["due_at"])
            if at <= time.time():
                raise ValueError("新的时间已经过去了")
            content = f"好，改到 {due_label(at)}。"
            result = self.finish_conversation(run, content, decision=decision, receipt={"commitment_id": commitment["id"]})
            said = self.one("SELECT id FROM deliveries WHERE run_id=?", (run["id"],))
            self.db.execute("UPDATE commitments SET state='superseded',closing_delivery_id=?,evidence=?,closed=? WHERE id=?",
                            (said["id"], encode([{"kind": "delivery", "id": said["id"]}]), time.time(), commitment["id"]))
            self._commitment_event(commitment, "superseded", {"by_delivery_id": said["id"], "rescheduled_by": item["message_id"], "basis": basis})
            self.register_commitment(room=item["room_id"], account=item["account"], quote=commitment["quote"], said_delivery_id=said["id"],
                                     user_message_id=item["message_id"], task_id=commitment["task_id"], goal_id=commitment["goal_id"],
                                     policy_id=commitment["policy_id"], kind=commitment["kind"], due={"at": at, "basis": "user", "quote": basis},
                                     supersedes=commitment["id"])
            return result
        content = "好，这件事不做了。"
        if commitment["goal_id"]:
            goal = self.goal(commitment["goal_id"], item["account"])
            if goal["status"] not in {"achieved", "retired"}:
                self.set_goal_state(goal["id"], "retired", expected_revision=goal["revision"], account=item["account"])
                content = "好，这件事不做了，相关的提醒和检查都已停掉。"
        self.close_commitment(commitment["id"], "released", [], reason="你说不要了：" + basis)
        if commitment["task_id"]:
            task = self.task(commitment["task_id"])
            if task["cancel_state"] == "active" and task["status"] != "succeeded":
                self.request_control(task["id"], "cancel", account=item["account"])
        result = self.finish_conversation(run, content, decision=decision, receipt={"commitment_id": commitment["id"], "task_id": commitment["task_id"], "goal_id": commitment["goal_id"]})
        return result

    # ---- execution -----------------------------------------------------------
    def executor_promise_errors(self, run, promise):
        """A delivered summary may promise later work only if this run registered it."""
        if not isinstance(promise, str) or not promise.strip():
            return []
        if self.one("SELECT 1 FROM operations WHERE run_id=? AND tool='start_following' AND status='succeeded'", (run["id"],)):
            return []
        return ["摘要里答应了以后的事（%s），但这次没有登记对应的安排。要么用 start_following 真的登记，要么去掉这句承诺" % promise.strip()[:80]]

    def bind_commitment_schedule(self, task_id, *, goal_id, policy_id, schedule):
        """An arrangement registered for a promise becomes what the promise rests on."""
        for row in self._open_for_task(task_id, ("task", "scheduled")):
            if schedule["kind"] == "once":
                at = once_at(schedule)
                self.db.execute("UPDATE commitments SET kind='scheduled',goal_id=?,policy_id=?,due_at=?,due_basis='schedule',due_quote=? WHERE id=?",
                                (goal_id, policy_id, at, schedule["at"], row["id"]))
            else:
                self.db.execute("UPDATE commitments SET kind='standing',goal_id=?,policy_id=?,due_at=NULL,due_basis=NULL,due_quote=NULL WHERE id=?",
                                (goal_id, policy_id, row["id"]))
            self._commitment_event(row, "arranged", {"goal_id": goal_id, "policy_id": policy_id, "schedule": schedule})

    def claim(self, exclude_task_ids=()):
        run = super().claim(exclude_task_ids)
        if run and run.get("task_id"):
            with self.transaction():
                root = self.task(run["task_id"])["root_task_id"]
                for row in self.all("SELECT * FROM commitments WHERE task_id=? AND state='open' AND blocked_since IS NOT NULL", (root,)):
                    self.db.execute("UPDATE commitments SET blocked_since=NULL,blocked_notified=NULL WHERE id=?", (row["id"],))
                    self._commitment_event(row, "resumed", {"run_id": run["id"]})
        return run

    def finish(self, run, **kwargs):
        with self.transaction():
            result = super().finish(run, **kwargs)
            if run.get("task_id"):
                self._settle(run, kwargs)
        return result

    def _settle(self, run, outcome):
        task = self.task(run["task_id"])
        if task["parent_task_id"]:
            return
        delivery = self.one("SELECT * FROM deliveries WHERE run_id=?", (run["id"],))
        status, reason = outcome["status"], outcome["reason"]
        if status == "succeeded" and outcome.get("acceptance") == "passed":
            evidence = []
            artifact = outcome.get("artifact")
            if artifact:
                evidence.append({"kind": "artifact", "task_id": task["id"], "ref": artifact["ref"], "sha256": artifact["sha256"]})
                accepted = self.one("SELECT data FROM steps WHERE run_id=? AND kind='acceptance' ORDER BY id DESC LIMIT 1", (run["id"],))
                for check in (json.loads(accepted["data"]).get("checks", []) if accepted else []):
                    if check.get("page"):
                        evidence.append({"kind": "page", "task_id": task["id"], "page_id": check["page"], "sha256": check["sha256"]})
            action = self.one("SELECT result FROM operations WHERE run_id=? AND tool='complete_action' AND status='succeeded' ORDER BY created DESC LIMIT 1", (run["id"],))
            for ref in (json.loads(action["result"]).get("effect_refs", []) if action else []):
                evidence.append({"kind": "operation", "task_id": task["id"], "id": ref})
            due = list(self._open_for_task(task["id"]))
            for row in self.all("SELECT * FROM commitments WHERE task_id=? AND state='open' AND kind IN ('scheduled','standing')", (task["id"],)):
                self._commitment_event(row, "arrangement_confirmed", {"delivery_id": delivery["id"] if delivery else None, "evidence": evidence})
            if task["policy_id"]:
                due += self.all("SELECT * FROM commitments WHERE policy_id=? AND kind='scheduled' AND state='open'", (task["policy_id"],))
            for row in due:
                # Done, not yet kept: fulfilment waits for the channel to acknowledge this delivery.
                self._commitment_event(row, "done_awaiting_delivery", {"delivery_id": delivery["id"] if delivery else None, "evidence": evidence})
            if delivery and delivery["state"] == "accepted":
                self._settle_delivered(delivery["id"])
        elif reason == "unable_reported":
            for row in self._open_for_task(task["id"], ("task", "scheduled", "standing")):
                self.close_commitment(row["id"], "unable", [], reason="做不到，已如实说明", closing_delivery_id=delivery["id"] if delivery else None)
        elif status in {"paused", "failed", "stopped", "review"}:
            for row in self._open_for_task(task["id"]):
                if not row["blocked_since"]:
                    self.db.execute("UPDATE commitments SET blocked_since=?,blocked_notified=NULL WHERE id=?", (time.time(), row["id"]))
                self._commitment_event(row, "blocked", {"reason": reason, "delivery_id": delivery["id"] if delivery else None})

    def _awaiting(self, delivery_id=None):
        """Open commitments whose work is accepted, with the delivery they wait on (latest wins)."""
        rows = self.all("""SELECT e.commitment_id,e.data FROM commitment_events e JOIN commitments c ON c.id=e.commitment_id
            WHERE e.kind='done_awaiting_delivery' AND c.state='open' ORDER BY e.created,e.id""")
        latest = {r["commitment_id"]: json.loads(r["data"]) for r in rows}
        return {key: data for key, data in latest.items() if data.get("delivery_id") and (delivery_id is None or data["delivery_id"] == delivery_id)}

    def _settle_delivered(self, delivery_id):
        for commitment_id, data in self._awaiting(delivery_id).items():
            self.close_commitment(commitment_id, "fulfilled", data.get("evidence") or [], closing_delivery_id=delivery_id)

    def accept_delivery(self, delivery_id, receipt):
        with self.transaction():
            super().accept_delivery(delivery_id, receipt)
            self._settle_delivered(delivery_id)

    def request_control(self, task_id, kind, *, run_id=None, account="local-user"):
        with self.transaction():
            receipt = super().request_control(task_id, kind, run_id=run_id, account=account)
            if kind == "cancel":
                root = self.task(task_id)["root_task_id"]
                for row in self._open_for_task(root, ("task", "scheduled", "standing")):
                    self.close_commitment(row["id"], "released", [], reason="你取消了这件事")
        return receipt

    def set_goal_state(self, goal_id, status, *, expected_revision, account="local-user"):
        with self.transaction():
            goal = super().set_goal_state(goal_id, status, expected_revision=expected_revision, account=account)
            if status in {"retired", "achieved"}:
                for row in self.all("SELECT * FROM commitments WHERE goal_id=? AND state='open'", (goal_id,)):
                    self.close_commitment(row["id"], "released", [], reason="这件事的跟进已经结束")
        return goal

    def _notice(self, task_id, room, content, *, kind="system", attention_id=None):
        did = super()._notice(task_id, room, content, kind=kind, attention_id=attention_id)
        if task_id and kind != "commitment":
            for row in self.all("SELECT * FROM commitments WHERE task_id=? AND state='open'", (task_id,)):
                self._commitment_event(row, "said", {"delivery_id": did, "kind": kind})
            self._link_disclosure(did, task_id=task_id)
        return did

    # ---- time ------------------------------------------------------------------
    def tick_waits(self):
        super().tick_waits()
        self.tick_commitments()

    def _status_words(self, row):
        if row["kind"] in {"scheduled", "standing"} and not row["task_id"]:
            return "约定的安排还没有执行"
        task = self.task(row["task_id"])
        return {"running": "还在做", "ready": "排着队，马上接着做", "waiting_user": "在等你回答一个问题",
                "waiting_children": "还在做", "waiting_dependencies": "还在做"}.get(task["status"], "卡住了，停在半路") if row["kind"] == "task" else "约定的提醒还没发出"

    def tick_commitments(self, *, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            clock = self.one("SELECT last FROM commitment_clock WHERE id=1")
            last = clock["last"] if clock else None
            overdue = self.all("SELECT * FROM commitments WHERE state='open' AND due_at IS NOT NULL AND overdue_notified IS NULL AND due_at + (CASE kind WHEN 'scheduled' THEN ? ELSE 0 END) <= ?", (SCHEDULED_GRACE, now))
            for row in overdue:
                missed = last is not None and last < row["due_at"] and now - row["due_at"] > DOWNTIME_GAP
                line = f"我答应你「{row['quote']}」，约在 {due_label(row['due_at'])}，现在过了还没做完：{self._status_words(row)}。"
                if missed:
                    line += "（到时间的时候这台电脑上的服务没开，现在才发现。）"
                line += "你可以让我继续、改个时间，或者告诉我不要了。"
                did = super()._notice(row["task_id"], row["room_id"], line, kind="commitment")
                self.db.execute("UPDATE commitments SET overdue_notified=? WHERE id=?", (now, row["id"]))
                self._commitment_event(row, "overdue_told", {"delivery_id": did, "service_was_down": missed})
            stuck = self.all("SELECT * FROM commitments WHERE state='open' AND due_at IS NULL AND blocked_since IS NOT NULL AND blocked_notified IS NULL AND blocked_since <= ?", (now - BLOCKED_REMINDER,))
            for row in stuck:
                line = f"「{row['quote']}」这件事卡住超过一天了，还没做完。你说「继续」我就接着做；不要了也可以直接告诉我。"
                did = super()._notice(row["task_id"], row["room_id"], line, kind="commitment")
                self.db.execute("UPDATE commitments SET blocked_notified=? WHERE id=?", (now, row["id"]))
                self._commitment_event(row, "blocked_told", {"delivery_id": did})
            untold = self.all("SELECT * FROM commitments WHERE state IN ('unable','released') AND closing_delivery_id IS NULL AND closed <= ?", (now - DISCLOSURE_GRACE,))
            for row in untold:
                line = f"关于你让我做的「{row['quote']}」：{row['close_reason'] or '这件事停下了'}，所以不再继续。"
                did = super()._notice(row["task_id"], row["room_id"], line, kind="commitment")
                self._link_disclosure(did, task_id=row["task_id"], goal_id=row["goal_id"])
                if self.one("SELECT closing_delivery_id FROM commitments WHERE id=?", (row["id"],))["closing_delivery_id"] is None:
                    self.db.execute("UPDATE commitments SET closing_delivery_id=? WHERE id=?", (did, row["id"]))
            if last is None or now - last >= CLOCK_WRITE or now < last:
                self.db.execute("INSERT INTO commitment_clock(id,last) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET last=excluded.last", (now,))

    # ---- view & upgrade --------------------------------------------------------
    def commitment_items(self, room):
        now = time.time()
        rows = self.all("SELECT * FROM commitments WHERE room_id=? AND state!='superseded' ORDER BY created DESC LIMIT 200", (room,))
        awaiting = self._awaiting()
        for row in rows:
            row["done_not_delivered"] = row["id"] in awaiting
            row["evidence"] = json.loads(row["evidence"])
            row["overdue"] = bool(row["state"] == OPEN and row["due_at"] and row["due_at"] < now)
            row["said_at"] = (self.one("SELECT created FROM deliveries WHERE id=?", (row["said_delivery_id"],)) or {}).get("created") or row["created"]
            row["closing_text"] = (self.one("SELECT content FROM deliveries WHERE id=?", (row["closing_delivery_id"],)) or {}).get("content") if row["closing_delivery_id"] else None
        return rows

    def _backfill_commitments(self):
        """Receipts sent before the ledger existed become commitments, ended by what actually happened."""
        inputs = self.all("""SELECT c.*,r.id AS run_id FROM conversation_inputs c JOIN runs r ON r.trigger='conversation'
            AND json_extract(r.context_manifest,'$.current_message_id')=c.message_id WHERE c.state='resolved' ORDER BY c.created""")
        latest = {}
        for item in inputs:
            decision, receipt = json.loads(item["decision"] or "{}"), json.loads(item["receipt"] or "{}")
            said = self.one("SELECT id,content,created FROM deliveries WHERE run_id=?", (item["run_id"],))
            if decision.get("action") not in {"new_task", "followup"} or not receipt.get("task_id") or not said or not self.one("SELECT 1 FROM tasks WHERE id=?", (receipt["task_id"],)):
                continue
            previous = latest.get(decision.get("task_id")) if decision.get("action") == "followup" else None
            if previous and self.commitment(previous)["state"] == OPEN:
                self.db.execute("UPDATE commitments SET state='superseded',closing_delivery_id=?,closed=? WHERE id=?", (said["id"], said["created"], previous))
            row = self.register_commitment(room=item["room_id"], account=item["account"], quote=said["content"], said_delivery_id=said["id"],
                                           user_message_id=item["message_id"], task_id=receipt["task_id"], supersedes=previous, origin="backfill", created=said["created"] or item["created"])
            latest[receipt["task_id"]] = row["id"]
            if decision.get("task_id"):
                latest[decision["task_id"]] = row["id"]
        for row in self.all("SELECT * FROM commitments WHERE state='open' AND origin='backfill'"):
            task = self.task(row["task_id"])
            last = self.one("SELECT id,artifact,state FROM deliveries WHERE task_id=? AND kind='result' ORDER BY rowid DESC LIMIT 1", (task["id"],))
            if task["status"] == "succeeded" and task["acceptance"] == "passed" and last and last["state"] != "accepted":
                artifact = json.loads(last["artifact"]) if last["artifact"] else None
                evidence = [{"kind": "artifact", "task_id": task["id"], "ref": artifact["ref"], "sha256": artifact["sha256"]}] if artifact else []
                self._commitment_event(row, "done_awaiting_delivery", {"delivery_id": last["id"], "evidence": evidence})
            elif task["status"] == "succeeded" and task["acceptance"] == "passed" and last:
                artifact = json.loads(last["artifact"]) if last["artifact"] else None
                evidence = [{"kind": "artifact", "task_id": task["id"], "ref": artifact["ref"], "sha256": artifact["sha256"]}] if artifact else []
                try:
                    self.close_commitment(row["id"], "fulfilled", evidence, closing_delivery_id=last["id"])
                except (ValueError, OSError):
                    self.close_commitment(row["id"], "fulfilled", [], closing_delivery_id=last["id"], reason="补登时成果文件无法核对，只保留交付消息")
            elif task["cancel_state"] == "cancelled":
                self.close_commitment(row["id"], "released", [], reason="你取消了这件事", closing_delivery_id=last["id"] if last else row["said_delivery_id"])
            elif task["status"] in {"paused", "failed", "stopped"}:
                self.db.execute("UPDATE commitments SET blocked_since=? WHERE id=?", (time.time(), row["id"]))
