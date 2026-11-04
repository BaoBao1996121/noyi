from __future__ import annotations

import asyncio
from pathlib import Path
import json
import os

from .contracts import encode
from .runtime import Runtime
from .tools import atomic_write, safe_child


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
    def __init__(self, store, data, model, channel=None):
        self.store, self.data = store, data
        self.runtime = Runtime(store, data, model)
        self.channel = channel or FileChannel(data)

    async def deliver(self):
        for d in self.store.all("SELECT * FROM deliveries WHERE state IN ('pending','failed')"):
            self.store.db.execute("UPDATE deliveries SET attempts=attempts+1 WHERE id=?", (d["id"],))
            try:
                receipt = await self.channel.send(d)
                if not isinstance(receipt, dict) or receipt.get("accepted") is not True or receipt.get("id") != d["id"]:
                    raise ValueError("channel did not acknowledge this delivery")
                self.store.db.execute("UPDATE deliveries SET state='accepted',receipt=?,last_error=NULL WHERE id=?", (encode(receipt), d["id"]))
            except Exception as exc:
                self.store.db.execute("UPDATE deliveries SET state='failed',last_error=? WHERE id=?", (type(exc).__name__, d["id"]))

    async def run_until_quiescent(self):
        # M0 one Bot: concurrency bound is deliberately one.
        while run := self.store.claim():
            await self.runtime.execute(run)
            await self.deliver()
        await self.deliver()

    async def serve(self, once=False):
        with ServiceLock(self.data):
            self.store.recover()
            while True:
                await self.run_until_quiescent()
                if once:
                    return
                # SQLite is authoritative; polling also picks up separate CLI processes.
                await asyncio.sleep(0.5)
