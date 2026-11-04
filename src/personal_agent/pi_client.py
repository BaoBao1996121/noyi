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


async def run_pi(messages, tools, model_name, handle):
    node = shutil.which("node")
    if not node or not BRIDGE.is_file():
        raise RuntimeError("Pi requires Node >=22.19 and the installed bridge dependencies")
    # The child runs only the loop. API credentials and private source locations
    # are handled by Python; it receives only the explicit protocol payloads.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME",
    }}
    creation = asyncio.create_task(asyncio.create_subprocess_exec(
        node, str(BRIDGE), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(BRIDGE.parent), env=env, limit=8 * 1024 * 1024))
    try:
        proc = await asyncio.shield(creation)
    except asyncio.CancelledError:
        # Cancelling halfway through Windows process creation must still take
        # ownership of the resulting child and release its pipe transports.
        proc = await creation
        await close_pi(proc)
        raise
    try:
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
        await close_pi(proc)
