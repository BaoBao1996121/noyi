"""Home upgrade drill: a local bare repository stands in for GitHub, a tiny stand-in
app stands in for Noyi, and the real supervisor and upgrader do the work.

The stand-in behaves like the real ledger where it matters: opening the ledger
migrates it to the release's version, and an older release refuses a newer
ledger. So "rolled back" is only true if the ledger really was restored.
"""
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HOME = Path(__file__).resolve().parents[1] / "scripts" / "home"
sys.path.insert(0, str(HOME))
from common import Layout, read_json  # noqa: E402
from supervise import Supervisor  # noqa: E402
from upgrade import Upgrader  # noqa: E402

STAND_IN = r'''
import json, os, sqlite3, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
root = Path(__file__).resolve().parents[2]
version, mode = int((root / "VERSION").read_text()), (root / "MODE").read_text().strip()
args = sys.argv[1:]
data, port, rehearsal = Path(args[args.index("--data") + 1]), int(args[args.index("--port") + 1]), "--rehearsal" in args
data.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(data / "state.sqlite", isolation_level=None)
have = db.execute("PRAGMA user_version").fetchone()[0]
if have > version:
    sys.exit(4)                                  # an older release refuses a newer ledger
if have < version:
    if mode == "migrate_fail":
        sys.exit(3)
    db.execute("CREATE TABLE IF NOT EXISTS marks(v INTEGER)")
    db.execute("INSERT INTO marks VALUES(?)", (version,))
    db.execute(f"PRAGMA user_version={version}")
if mode == "start_fail" and not rehearsal:
    sys.exit(1)
TOKEN = "t%d" % os.getpid()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path == "/api/bootstrap":
            return self.reply(200, {"title": "Noyi", "token": TOKEN, "version": version})
        if self.path.startswith("/api/state"):
            return self.reply(500 if mode == "health_fail" and not rehearsal else 200, {"v": version})
        self.reply(404, {})
    def do_POST(self):
        if self.path == "/api/shutdown" and self.headers.get("X-PA-Token") == TOKEN:
            self.reply(200, {"accepted": True})
            threading.Timer(0.1, lambda: os._exit(0)).start()
            return
        self.reply(403, {})
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class Drill:
    def __init__(self, tmp):
        self.src, self.origin, self.root = tmp / "src", tmp / "origin.git", tmp / "Noyi"
        self.version = 0
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.src)], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            git(self.src, "config", k, v)
        (self.src / "src" / "personal_agent").mkdir(parents=True)
        (self.src / "src" / "personal_agent" / "__main__.py").write_text(STAND_IN, encoding="utf-8")
        (self.src / "src" / "personal_agent" / "__init__.py").write_text("", encoding="utf-8")   # else the installed Noyi wins the import
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        git(self.src, "remote", "add", "origin", str(self.origin))
        self.root.mkdir()
        port = free_port()
        (self.root / "home.json").write_text(json.dumps({"port": port, "install": False, "python": sys.executable, "pythonpath_src": True,
                                                         "smoke": [], "health_seconds": 12, "stable_seconds": 2, "backoff": [0.2]}))
        self.port = port
        self.release("ok")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.root / "repo")], check=True)
        self.layout = Layout(self.root)
        self.supervisor = Supervisor(self.layout)
        self.thread = threading.Thread(target=self.supervisor.run, daemon=True)
        self.thread.start()

    def release(self, mode, *, branch="main"):
        self.version += 1
        if branch != "main":
            git(self.src, "checkout", "-q", "-B", branch)
        (self.src / "VERSION").write_text(str(self.version))
        (self.src / "MODE").write_text(mode)
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", f"v{self.version} {mode}")
        tag = f"home-20261114-{self.version}"
        git(self.src, "tag", tag)
        git(self.src, "push", "-q", "origin", branch, "--tags")
        if branch != "main":
            git(self.src, "checkout", "-q", "main")
        return tag

    def upgrade(self):
        return Upgrader(self.layout, log=lambda *_: None).upgrade()

    def ledger(self):
        db = sqlite3.connect(self.root / "data" / "state.sqlite")
        try:
            return db.execute("PRAGMA user_version").fetchone()[0], [r[0] for r in db.execute("SELECT v FROM marks ORDER BY rowid")]
        finally:
            db.close()

    def current(self):
        return (read_json(self.layout.current, {}) or {}).get("tag")

    def close(self):
        (self.root / "stop-supervisor").write_text("x")
        self.thread.join(60)


@pytest.fixture
def drill(tmp_path):
    d = Drill(tmp_path)
    yield d
    d.close()


def test_upgrade_drill_ok_then_every_failure_keeps_the_ledger(drill):
    # First install, then a normal upgrade.
    assert drill.upgrade() == "install" and drill.current() == "home-20261114-1"
    assert drill.ledger() == (1, [1])
    good = drill.release("ok")
    assert drill.upgrade() == "ok" and drill.current() == good
    assert drill.ledger() == (2, [1, 2])
    assert any((drill.root / "data" / "backups").glob("pre-home-20261114-2-*.sqlite"))
    assert not drill.upgrade() == "ok"                           # nothing newer: no work
    assert read_json(drill.layout.upgrade)["result"] == "none"

    # Migration fails on the rehearsal copy: never switched, the real ledger never touched.
    bad = drill.release("migrate_fail")
    assert drill.upgrade() == "rehearsal_failed"
    assert drill.current() == good and drill.ledger() == (2, [1, 2])
    assert not drill.layout.release(bad).exists()
    assert drill.upgrade() == "none"                             # a failed tag is not retried

    # Starts on the copy, crashes on the real ledger after migrating it: restore + old release.
    drill.release("start_fail")
    assert drill.upgrade() == "health_failed_rolled_back"
    assert drill.current() == good and drill.ledger() == (2, [1, 2])

    # Comes up but its pages fail: same.
    drill.release("health_fail")
    assert drill.upgrade() == "health_failed_rolled_back"
    assert drill.current() == good and drill.ledger() == (2, [1, 2])

    # A tag that is not on origin/main is never taken.
    drill.release("ok", branch="side")
    assert drill.upgrade() == "none" and drill.current() == good

    # The old release really is serving again after all of that.
    from common import bootstrap
    assert bootstrap(drill.port)["version"] == 2
    state = read_json(drill.layout.upgrade)
    assert state["last_result"] == "health_failed_rolled_back" and set(state["failed"]) == {
        "home-20261114-3", "home-20261114-4", "home-20261114-5"}


def test_supervisor_restarts_after_a_crash_and_stops_a_crash_loop(tmp_path):
    from common import Layout, write_json
    root = tmp_path / "Noyi"
    release = root / "releases" / "home-20261114-1"
    (release / "src" / "personal_agent").mkdir(parents=True)
    (release / "src" / "personal_agent" / "__main__.py").write_text("import sys; sys.exit(7)", encoding="utf-8")
    (release / "src" / "personal_agent" / "__init__.py").write_text("", encoding="utf-8")
    write_json(root / "home.json", {"port": free_port(), "python": sys.executable, "pythonpath_src": True, "backoff": [0.1], "crash_limit": 3, "crash_window": 60})
    write_json(root / "current.json", {"tag": "home-20261114-1"})
    layout = Layout(root)
    sup = Supervisor(layout)
    thread = threading.Thread(target=sup.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and (read_json(layout.supervisor, {}) or {}).get("state") != "crash_loop":
            time.sleep(0.2)
        status = read_json(layout.supervisor)
        assert status["state"] == "crash_loop" and status["crashes_24h"] == 3
        time.sleep(1.5)
        assert read_json(layout.supervisor)["crashes_24h"] == 3   # stopped: no fourth start
    finally:
        (root / "stop-supervisor").write_text("x")
        thread.join(30)
