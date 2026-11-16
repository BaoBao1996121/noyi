"""Managed transport to Pi's Agent loop; task authority remains in Python."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil

from .contracts import encode


BRIDGE = Path(__file__).parent / "pi_bridge" / "bridge.mjs"


async def close_pi(proc):
    """Release every pipe while its loop is alive, including Windows handles."""
    if proc.stdin:
        proc.stdin.close()
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
    if proc.stdin:
        try:
            await proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
    if proc.stdout:
        await proc.stdout.read()


async def _reap_created(creation):
    # Process creation owns OS resources before it returns a Process instance.
    # Keep ownership even when cancellation arrives during that interval.
    proc = await creation
    await close_pi(proc)


def spawn_pi():
    """Start one Pi child. It loads its modules, then waits for its start line."""
    node = shutil.which("node")
    if not node or not BRIDGE.is_file():
        raise RuntimeError("Pi requires Node >=22.19 and the installed bridge dependencies")
    # The child runs only the loop. API credentials and private source locations
    # are handled by Python; it receives only the explicit protocol payloads.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME",
    }}
    return asyncio.create_task(asyncio.create_subprocess_exec(
        node, str(BRIDGE), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(BRIDGE.parent), env=env, limit=8 * 1024 * 1024))


class WarmPi:
    """Keeps one Pi child started ahead of need.

    Starting Node and loading the Pi packages takes 1-2 s, and several seconds
    on a machine whose virus scanner inspects every file, which the user felt
    as a slow first reply. A child that has loaded but not yet received its
    start line holds no task data, so starting it early changes nothing about
    isolation: every run still gets its own fresh process, used once.
    """
    def __init__(self):
        self._next = None
        self._loop = None
        # Only an owner that will call close() (the long-running service)
        # turns pre-starting on; otherwise every run starts its own child.
        self.enabled = False

    def take(self):
        """The ready child's creation task for this run; the next one starts now."""
        loop = asyncio.get_running_loop()
        ready = self._next if self._next is not None and self._loop is loop else None
        self._next = None
        if ready is not None and ready.done() and (ready.cancelled() or ready.exception()):
            ready = None
        current = ready or spawn_pi()
        self._prestart(loop)
        return current

    def _prestart(self, loop):
        if not self.enabled:
            return
        try:
            self._next, self._loop = spawn_pi(), loop
        except RuntimeError:
            # No usable Node here: runs report it themselves when they start.
            self._next = None

    def start(self):
        self.enabled = True
        if self._next is None:
            self._prestart(asyncio.get_running_loop())

    async def close(self):
        self.enabled = False
        pending, self._next = self._next, None
        if pending is not None:
            await asyncio.gather(_reap_created(pending), return_exceptions=True)


async def run_pi(messages, tools, model_name, handle, *, warm=None):
    creation = warm.take() if warm else spawn_pi()
    try:
        proc = await asyncio.shield(creation)
        if proc.returncode is not None:
            # A pre-started child that exited while waiting is not reused.
            await close_pi(proc)
            creation = spawn_pi()
            proc = await asyncio.shield(creation)
        proc.stdin.write((encode({"type": "start", "messages": messages,
                                  "tools": tools, "model": model_name}) + "\n").encode())
        await proc.stdin.drain()
        while line := await proc.stdout.readline():
            event = json.loads(line)
            if event.get("type") == "request":
                result = await handle(event["method"], event.get("params", {}))
                proc.stdin.write((encode({"id": event["id"], "result": result}) + "\n").encode())
                await proc.stdin.drain()
            elif event.get("type") == "done":
                return
            elif event.get("type") == "fatal":
                raise RuntimeError("Pi execution failed")
            else:
                raise RuntimeError("invalid Pi transport event")
        raise RuntimeError("Pi process exited before finishing")
    finally:
        # A service stop can cancel a run already being stopped by the user.
        # Shielding without awaiting again would leave cleanup running after
        # the event loop closes. Drain it fully before acknowledging either stop.
        cleanup = asyncio.create_task(_reap_created(creation))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
