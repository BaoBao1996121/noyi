"""Durable local triggers. Observations never grant new work permissions."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from .contracts import TaskSpec, digest, encode
from .schedule import due_window
from .sources import ExactSourceReader
from .tools import MAX_SOURCE_BYTES, SourceUnavailable, atomic_write, safe_child


class ExactObservationSource:
    identity = "exact-source-observer/v1"

    async def observe(self, source):
        return await ExactSourceReader().read(source)


class TriggerScheduler:
    def __init__(self, store, data, *, observer=None, clock=time.time):
        self.store, self.data = store, Path(data).resolve()
        self.observer, self.clock = observer or ExactObservationSource(), clock
        self._lock = asyncio.Lock()
        self.error = None

    def snapshot_path(self, name):
        root = self.data / "observations"
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)() or root.resolve().parent != self.data:
            raise ValueError("observation storage was redirected")
        return safe_child(root, name)

    async def observe(self, occurrence):
        if not self.store.occurrence_current(occurrence):
            return self.store.complete_observation(occurrence["id"], [], now=self.clock())
        policy = self.store.policy(occurrence["policy_id"])
        receipts = []
        for source in policy["template"]["sources"]:
            receipt = {"source_id": source["id"], "observer": self.observer.identity}
            try:
                async with asyncio.timeout(25):
                    raw = await self.observer.observe(dict(source))
                if not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES:
                    raise SourceUnavailable("observation must be bounded UTF-8 bytes")
                raw = raw.decode("utf-8-sig").encode("utf-8")
                sha = digest(raw)
                path = self.snapshot_path(sha + ".txt")
                if path.exists():
                    if digest(path.read_bytes()) != sha:
                        raise ValueError("stored observation integrity mismatch")
                else:
                    atomic_write(path, raw)
                receipt.update(state="ok", sha256=sha, snapshot_ref=path.name, bytes=len(raw))
            except (OSError, ValueError, UnicodeError, TimeoutError) as exc:
                receipt.update(state="failed", error_type=type(exc).__name__)
            except Exception as exc:
                # HTTP failures and trusted adapter errors are source-health
                # evidence. Bodies, tokens and exception strings are not logged.
                receipt.update(state="failed", error_type=type(exc).__name__)
            receipts.append(receipt)
        return self.store.complete_observation(occurrence["id"], receipts, now=self.clock())

    def materialize(self, occurrence_id):
        with self.store.transaction():
            row = self.store.one("SELECT * FROM policy_occurrences WHERE id=?", (occurrence_id,))
            if not row or row["status"] != "queued":
                return None
            if not self.store.occurrence_current(row):
                self.store.db.execute("UPDATE policy_occurrences SET status='ignored',reason='stale_authority' WHERE id=?", (occurrence_id,))
                return None
            policy = self.store.policy(row["policy_id"])
            goal = self.store.goal(policy["goal_id"])
            # A failed or user-paused attempt needs resolution, not a fresh
            # task that silently circumvents its budget or unresolved question.
            active = self.store.one("SELECT id FROM tasks WHERE policy_id=? AND policy_revision=? AND parent_task_id IS NULL AND status NOT IN ('succeeded','cancelled') LIMIT 1", (policy["id"], policy["revision"]))
            if active:
                self.store.db.execute("UPDATE policy_occurrences SET reason=? WHERE id=?", ("waiting_for:" + active["id"], occurrence_id))
                return None
            if goal["calls_used"] >= goal["calls_limit"] or goal["tools_used"] >= goal["tools_limit"]:
                self.store.db.execute("UPDATE goals SET status='needs_attention',wait_reason='累计预算已用完' WHERE id=?", (goal["id"],))
                self.store.goal_event(goal["id"], "budget_exhausted_before_dispatch", {"occurrence_id": occurrence_id})
                self.store._notice(None, goal["room_id"], f"持续目标「{goal['title']}」的累计预算已用完，新的检查结果已保存，等待增加预算。")
                return None
            receipts = {r["source_id"]: r for r in json.loads(row["payload"])["receipts"]}
            spec = dict(policy["template"])
            spec["sources"] = []
            for original in policy["template"]["sources"]:
                receipt = receipts[original["id"]]
                if receipt["state"] == "ok":
                    path = self.snapshot_path(receipt["snapshot_ref"])
                    if not path.is_file() or digest(path.read_bytes()) != receipt["sha256"]:
                        self.store.db.execute("UPDATE policy_occurrences SET status='failed',reason='snapshot_integrity' WHERE id=?", (occurrence_id,))
                        self.store._notice(None, goal["room_id"], "已保存的来源快照缺失或改变，本次工作没有启动。请重新检查来源。")
                        return None
                    spec["sources"].append({**original, "kind": "file", "location": str(path), "expected_sha256": receipt["sha256"]})
                else:
                    if original.get("required", True):
                        raise ValueError("required source failure cannot dispatch work")
                    spec["sources"].append({**original, "kind": "file", "location": str(self.snapshot_path("unavailable-" + row["id"] + ".txt")), "observed_unavailable": True})
            submitted = self.store.submit(TaskSpec(**spec), nonce="occurrence:" + row["id"], source="routine", account=goal["owner"], actor=policy["actor_id"], room=goal["room_id"])
            task_id = submitted["task_id"]
            self.store.db.execute("UPDATE tasks SET goal_id=?,goal_revision=?,policy_id=?,policy_revision=?,occurrence_id=? WHERE id=?", (goal["id"], goal["revision"], policy["id"], policy["revision"], row["id"], task_id))
            self.store.db.execute("UPDATE policy_occurrences SET status='dispatched',task_id=?,reason='authorized_routine' WHERE id=?", (task_id, row["id"]))
            self.store.goal_event(goal["id"], "routine_dispatched", {"occurrence_id": row["id"], "task_id": task_id, "policy_revision": policy["revision"]})
            return task_id

    async def tick(self):
        async with self._lock:
            now = self.clock()
            # Each due decision advances its cursor in the same transaction as
            # the durable observing row. Crash recovery resumes that exact row.
            due = self.store.all("SELECT p.id FROM routine_policies p JOIN goals g ON g.id=p.goal_id WHERE p.state='active' AND g.status='active' AND p.next_due<=? ORDER BY p.next_due LIMIT 64", (now,))
            for value in due:
                policy = self.store.policy(value["id"])
                if not self.store.one("SELECT id FROM policy_occurrences WHERE policy_id=? AND status='observing'", (policy["id"],)):
                    window = due_window(policy["schedule"], policy["next_due"], now)
                    if window:
                        self.store.prepare_occurrence(policy["id"], window, now=now)
            rows = self.store.all("SELECT * FROM policy_occurrences WHERE status='observing' ORDER BY created LIMIT 64")
            # Slow external reads do not block unrelated policies or user
            # controls; observation parallelism remains bounded.
            semaphore = asyncio.Semaphore(4)
            async def observe_one(row):
                async with semaphore:
                    return await self.observe(row)
            await asyncio.gather(*(observe_one(row) for row in rows))
            for row in self.store.all("SELECT id FROM policy_occurrences WHERE status='queued' ORDER BY created"):
                self.materialize(row["id"])
            if hasattr(self.store, "apply_pending_admissions"):
                self.store.apply_pending_admissions(now=now)
            if hasattr(self.store, "tick_review_reminders"):
                self.store.tick_review_reminders(now=now)

    async def run(self):
        try:
            while True:
                await self.tick()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = type(exc).__name__
            # Keep a fault visible and stop this scheduler, rather than retry
            # inconsistent state indefinitely or report false health.
            return
