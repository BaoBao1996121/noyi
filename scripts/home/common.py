"""Shared by the home supervisor, upgrader and log uploader.

Standard library only: these run from the system Python, outside any release's
virtualenv, so a broken release cannot break the tools that roll it back.

Layout under the root (default D:\\Noyi):
    repo/              clone of the public noyi repository (fetches tags)
    releases/<tag>/    one checkout per release, each with its own .venv and node_modules
    data/              the ledger and files; never inside a release
    ops/               clone of the private noyi-ops repository (log bundles)
    bin/               copies of these scripts, refreshed after each good upgrade
    current.json       {"tag", "previous"}: what the supervisor runs
    hold               while present, the supervisor starts nothing
    supervisor.json    written by the supervisor: state, pid, crash counts
    upgrade.json       written by the upgrader: last result, tags that failed
    home.json          optional settings (port, timings); tests point it at fakes
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

TAG = re.compile(r"home-(\d{8})-(\d{1,3})")
DEFAULTS = {
    "port": 8765,
    "install": True,              # create the venv and run npm ci for each release
    "python": None,               # tests: run releases with this interpreter instead of the release venv
    "pythonpath_src": False,      # tests: import the release from its src/ directory
    "health_seconds": 120,
    "backoff": [5, 30, 120],
    "crash_limit": 5,
    "crash_window": 600,
    "keep_backups": 10,
    "smoke": ["tests/test_store.py", "tests/test_health.py", "tests/test_ops_export.py"],
}


class Layout:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.repo = self.root / "repo"
        self.releases = self.root / "releases"
        self.data = self.root / "data"
        self.ops = self.root / "ops"
        self.keys = self.root / "keys"
        self.bin = self.root / "bin"
        self.current = self.root / "current.json"
        self.hold = self.root / "hold"
        self.supervisor = self.root / "supervisor.json"
        self.upgrade = self.root / "upgrade.json"
        self.settings = self.root / "home.json"
        self.lock = self.root / "upgrade.lock"

    def release(self, tag):
        if not TAG.fullmatch(tag):
            raise ValueError("not a home release tag: " + tag)
        return self.releases / tag

    def config(self):
        return {**DEFAULTS, **read_json(self.settings, {})}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def tag_key(tag):
    m = TAG.fullmatch(tag)
    return (m.group(1), int(m.group(2))) if m else ("", -1)


def release_python(layout, cfg, tag):
    if cfg.get("python"):
        return cfg["python"]
    venv = layout.release(tag) / ".venv"
    return str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def release_env(layout, cfg, tag):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if cfg.get("pythonpath_src"):
        env["PYTHONPATH"] = str(layout.release(tag) / "src")
    return env


def serve_command(layout, cfg, tag):
    return [release_python(layout, cfg, tag), "-m", "personal_agent", "--data", str(layout.data), "web", "--port", str(cfg["port"])]


def http(port, path, *, body=None, token=None, timeout=3):
    """(status, json-or-None); status 0 when nothing answers."""
    headers = {"X-PA-Token": token} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return 0, None


def bootstrap(port):
    status, body = http(port, "/api/bootstrap")
    return body if status == 200 and isinstance(body, dict) and body.get("title") == "Noyi" else None


def shutdown(port):
    """Ask a running Noyi to stop; it saves execution state first. True if it accepted."""
    boot = bootstrap(port)
    if not boot:
        return False
    status, _ = http(port, "/api/shutdown", body={}, token=boot.get("token"))
    return status == 200


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259                                                # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_tree(pid):
    import subprocess
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def iso(at=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at or time.time()))
