"""What the assistant says and when (decisions X-5, X-6, X-7, X-9).

The model writes receipts and delivery summaries; code checks them and falls
back to fixed wording. Timing lines ("已开跑", the 20-minute progress line)
are produced by code from ledger facts, never by the model.
"""
import re
import time

RECEIPT_MAX = 60
# Wording that claims finished work. A receipt is sent before anything ran.
PROMISE = re.compile(r"已(?:经)?(?:完成|做好|写好|整理好|生成|发送|提交|改好|处理好|搞定)|搞定|完成了|做完了|结果如下|见附件|如下[:：]")
STARTED = "已开跑，做好把成果发你。"
STARTED_AFTER = 10
RESTARTED = "已重新开跑，做好把成果发你。"
PROGRESS_AFTER = 20 * 60
WEEKDAYS = "一二三四五六日"


def local_now(now=None):
    """Current local time for the model: it has no clock of its own."""
    t = time.localtime(now)
    return time.strftime("%Y-%m-%d %H:%M", t) + f" 星期{WEEKDAYS[t.tm_wday]}（{time.strftime('%z', t)[:3]}:{time.strftime('%z', t)[3:]}）"
EMPTY_DELIVERY = "成果整理好了，见下方附件。"
MAX_POINTS = 3
HEAD_MAX = 80
REMEMBER = re.compile(r"记住|记一下|以后(?:都|就|每次|一律)|下次(?:都|也)|每次都")


def check_receipt(text):
    """Return a usable model-written receipt, or None to use the template."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text or len(text) > RECEIPT_MAX or PROMISE.search(text):
        return None
    return text


def shape_delivery(summary):
    """One conclusion line, then at most three points. The artifact holds the rest."""
    lines = [l.strip() for l in (summary or "").splitlines() if l.strip()]
    if not lines:
        return EMPTY_DELIVERY
    head, points = lines[0], [l for l in lines[1:] if re.match(r"(?:[-*•·]|\d+[.、)])\s*", l)]
    if not points and len(head) > HEAD_MAX:
        # A paragraph: first sentence is the conclusion, the next ones become points.
        sentences = [x for x in re.split(r"(?<=[。！？；])", head) if x.strip()]
        head, points = sentences[0], ["- " + x.strip().rstrip("。；") for x in sentences[1:]]
    return "\n".join([head, *points[:MAX_POINTS]])


def explicit_remember(text):
    return bool(REMEMBER.search(text or ""))


class SpeechStore:
    """Code-owned conversation lines, keyed so each is sent at most once."""

    def __init__(self, path):
        super().__init__(path)
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS speech_marks(task_id TEXT NOT NULL,kind TEXT NOT NULL,delivery_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(task_id,kind))")

    def _say_once(self, task, kind, content):
        if self.one("SELECT 1 FROM speech_marks WHERE task_id=? AND kind=?", (task["id"], kind)):
            return False
        did = self._notice(task["id"], task["room_id"], content, kind="result")
        self.db.execute("INSERT INTO speech_marks VALUES(?,?,?,?)", (task["id"], kind, did, time.time()))
        return True

    def _announces(self, task):
        # Only conversation work whose user waits for a result: not arrangements,
        # workers, routines, reviews (their receipts already say what happens).
        return (not task["parent_task_id"] and task["spec"].get("result_kind", "artifact") == "artifact"
                and not self.one("SELECT 1 FROM goal_review_requests WHERE task_id=?", (task["id"],))
                and bool(self.one("SELECT 1 FROM conversation_inputs c JOIN messages m ON m.id=c.message_id WHERE m.task_id=?", (task["id"],))))

    def claim(self, exclude_actor_ids=()):
        run = super().claim(exclude_actor_ids)
        if run and run["task_id"]:
            with self.transaction():
                task = self.task(run["task_id"])
                if self._announces(task):
                    if run["trigger"] == "user_resume":
                        self._say_once(task, f"restarted:{run['epoch']}", RESTARTED)
                    elif self._waited(task):
                        self._say_once(task, "started", STARTED)
        return run

    def _waited(self, task):
        # Starting right after the receipt adds nothing: the receipt already said it.
        # Intake resolves (and sends its receipt) in the same transaction that sets updated.
        receipt = self.one("SELECT c.updated AS created FROM conversation_inputs c JOIN messages m ON m.id=c.message_id WHERE m.task_id=? AND c.state='resolved' ORDER BY c.updated DESC LIMIT 1", (task["id"],))
        return not receipt or time.time() - receipt["created"] > STARTED_AFTER

    def tick_waits(self):
        super().tick_waits()
        now = time.time()
        with self.transaction():
            rows = self.all("""SELECT t.id, MIN(r.started) AS first FROM tasks t JOIN runs r ON r.task_id=t.id
                WHERE t.parent_task_id IS NULL AND t.status IN ('running','waiting_children','waiting_dependencies','ready')
                AND NOT EXISTS(SELECT 1 FROM speech_marks s WHERE s.task_id=t.id AND s.kind='progress')
                GROUP BY t.id HAVING first<=?""", (now - PROGRESS_AFTER,))
            for row in rows:
                task = self.task(row["id"])
                if self._announces(task):
                    self._say_once(task, "progress", self.progress_line(task, now - row["first"]))

    def progress_line(self, task, elapsed):
        ops = self.all("SELECT tool FROM operations o JOIN tasks t ON t.id=o.task_id WHERE t.root_task_id=? AND o.status='succeeded'", (task["root_task_id"],))
        reads = sum(o["tool"] == "read_source" for o in ops)
        workers = self.one("SELECT COUNT(*) n FROM tasks WHERE root_task_id=? AND parent_task_id IS NOT NULL", (task["root_task_id"],))["n"]
        facts = ([f"读了 {reads} 份资料"] if reads else []) + ([f"分出 {workers} 个子任务"] if workers else []) + [f"调用工具 {len(ops)} 次"]
        return f"还在做，已经 {max(1, round(elapsed / 60))} 分钟：" + "，".join(facts) + "。做完马上发你。"

    def finish(self, run, **kwargs):
        if run.get("task_id") and kwargs.get("artifact") and kwargs.get("content"):
            kwargs["content"] = shape_delivery(kwargs["content"])
        return super().finish(run, **kwargs)

