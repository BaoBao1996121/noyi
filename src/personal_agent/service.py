from __future__ import annotations

import asyncio
from pathlib import Path
import json
import os

from .contracts import encode
from .runtime import Runtime
from .tools import atomic_write, safe_child
from .triggers import TriggerScheduler


class ServiceLock:
    """OS-held process lock. A dead process releases it, including on Windows."""
    def __init__(self, data: Path):
        self.path = data / "service.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("another pa serve owns this data directory") from exc
        return self

    def __exit__(self, *args):
        self.file.close()


class FileChannel:
    """Idempotent local inbox, not a claim that a human read the message."""
    def __init__(self, data: Path):
        self.data = data.resolve()
        self.root = self.data / "inbox"

    async def send(self, delivery):
        if self.root.is_symlink() or self.root.resolve().parent != self.data:
            raise ValueError("inbox path changed")
        path = safe_child(self.root, delivery["id"] + ".json")
        payload = {k: delivery[k] for k in ("id", "task_id", "room_id", "content", "artifact")}
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != payload:
                raise ValueError("delivery receipt content mismatch")
        else:
            atomic_write(path, (encode(payload) + "\n").encode())
        return {"channel": "local_file", "id": delivery["id"], "accepted": True, "human_read": "unknown"}


class Service:
    def __init__(self, store, data, model, channel=None, *, concurrency=3, engine=None, source_reader=None, observer=None, clock=None, plugins=None):
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be an integer from 1 to 4")
        self.store, self.data = store, data
        self.runtime = Runtime(store, data, model, engine=engine, source_reader=source_reader, plugins=plugins)
        self.channel = channel or FileChannel(data)
        self.concurrency = concurrency
        self._active = {}
        self._delivery_lock = asyncio.Lock()
        self.triggers = TriggerScheduler(store, data, observer=observer, **({"clock": clock} if clock else {}))

    async def deliver(self):
        # A slow channel must not stall the scheduler's control polling, and two
        # callers must not race the same durable delivery or local inbox file.
        async with self._delivery_lock:
            await self._deliver()

    async def _deliver(self):
        for d in self.store.all("SELECT * FROM deliveries WHERE state IN ('pending','failed')"):
            self.store.db.execute("UPDATE deliveries SET attempts=attempts+1 WHERE id=?", (d["id"],))
            try:
                receipt = await self.channel.send(d)
                if not isinstance(receipt, dict) or receipt.get("accepted") is not True or receipt.get("id") != d["id"]:
                    raise ValueError("channel did not acknowledge this delivery")
                self.store.accept_delivery(d["id"], receipt)
            except Exception as exc:
                self.store.db.execute("UPDATE deliveries SET state='failed',last_error=? WHERE id=?", (type(exc).__name__, d["id"]))

    async def run_until_quiescent(self):
        if self._active:
            raise RuntimeError("the scheduler is already running")
        stopping = {}
        delivery = asyncio.create_task(self.deliver())
        delivery_needed = False
        try:
            while True:
                self.store.tick_waits()
                # Cancellation is requested for every affected run before we
                # reap any of them. A model call or subprocess cleanup never
                # blocks control checks for its peers.
                for run_id, (run, future) in list(self._active.items()):
                    reason = self.store.control_reason(run)
                    if not reason and run["task_id"] and not future.done() and self.store.remaining_team_seconds(self.store.task(run["task_id"])["root_task_id"]) <= 0:
                        reason = "team_time_budget"
                    if reason and run_id not in stopping:
                        stopping[run_id] = reason
                        if not future.done():
                            future.cancel()
                for run_id, (run, future) in list(self._active.items()):
                    if not future.done():
                        continue
                    reason = self.store.control_reason(run) or stopping.get(run_id)
                    if reason:
                        # execute() has returned and run_pi's finally block has
                        # awaited child exit. Only now acknowledge the control.
                        await asyncio.gather(future, return_exceptions=True)
                        self.store.ack_control(run, reason)
                    else:
                        await future
                    del self._active[run_id]
                    stopping.pop(run_id, None)
                    delivery_needed = True
                # One bounded intake slot remains available even while every
                # business slot is busy, so a spoken stop or correction is heard.
                if not any(r["trigger"] == "conversation" for r, _ in self._active.values()):
                    incoming = self.store.claim_conversation()
                    if incoming:
                        self._active[incoming["id"]] = (incoming, asyncio.create_task(self.runtime.execute(incoming)))
                # One Run per task, not per assistant: a quick question is not
                # queued behind the same assistant's long task (acceptance #3).
                while sum(r["trigger"] != "conversation" for r, _ in self._active.values()) < self.concurrency:
                    run = self.store.claim(exclude_task_ids={r["task_id"] for r, _ in self._active.values() if r["task_id"]})
                    if run is None:
                        break
                    self._active[run["id"]] = (run, asyncio.create_task(self.runtime.execute(run)))
                if delivery.done():
                    await delivery
                    if delivery_needed:
                        delivery = asyncio.create_task(self.deliver())
                        delivery_needed = False
                if not self._active:
                    # Durable WAIT states deliberately do not keep this call or
                    # an execution slot alive. serve() will poll their deadlines.
                    await delivery
                    if delivery_needed:
                        await self.deliver()
                    return
                await asyncio.sleep(0.05)
        finally:
            pending = [future for _, future in self._active.values()]
            for future in pending:
                if not future.done():
                    future.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._active.clear()
            if not delivery.done():
                delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

    async def serve(self, once=False):
        with ServiceLock(self.data):
            self.store.recover()
            if once:
                await self.triggers.tick()
                await self.run_until_quiescent()
                return
            job = asyncio.create_task(self.triggers.run())
            try:
                while True:
                    await self.run_until_quiescent()
                    if self.triggers.error:
                        raise RuntimeError("trigger scheduler failed: " + self.triggers.error)
                    # SQLite also picks up separate CLI processes.
                    await asyncio.sleep(0.5)
            finally:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
