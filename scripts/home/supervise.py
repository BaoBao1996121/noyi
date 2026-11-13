"""Keep the current Noyi release running on the home PC.

Started at boot by the scheduled task "\\Noyi\\Service". It runs the release named
in current.json and restarts it after a crash, backing off 5 s, 30 s, 2 min. Five
crashes inside 10 minutes means something is wrong with this release or this
machine: it stops restarting (so a crash loop cannot keep hammering the ledger)
and waits until current.json names a different release. While the upgrader holds
the root (the `hold` file), it starts nothing; an exit during a hold is not a crash.

    python supervise.py --root D:\\Noyi
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Layout, iso, kill_tree, read_json, serve_command, release_env, shutdown, write_json  # noqa: E402

POLL = 0.5


def user_environment(names):
    """Values the installer stored as user environment variables. A task that
    starts at boot may not see them in its own environment, so read the
    registry too (Windows only). Never logged."""
    found = {}
    for name in names:
        if os.environ.get(name):
            found[name] = os.environ[name]
            continue
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    found[name] = winreg.QueryValueEx(key, name)[0]
            except OSError:
                pass
    return found


class Supervisor:
    def __init__(self, layout):
        self.layout = layout
        self.cfg = layout.config()
        self.proc = None
        self.tag = None
        self.started = None
        self.crashes = []          # timestamps, last 24 h
        self.restarts = []
        self.tag_crashes = []      # crashes of the release running now; a crash loop is judged per release
        self.consecutive = 0
        self.state = "starting"
        self.stopped_for = None    # tag that crash-looped

    def status(self, **extra):
        now = time.time()
        self.crashes = [t for t in self.crashes if now - t < 86400]
        self.restarts = [t for t in self.restarts if now - t < 86400]
        write_json(self.layout.supervisor, {"state": self.state, "tag": self.tag, "pid": self.proc.pid if self.proc else None,
                                            "supervisor_pid": os.getpid(), "started": self.started, "updated": iso(now),
                                            "crashes_24h": len(self.crashes), "restarts_24h": len(self.restarts), **extra})

    def start(self, tag):
        self.cfg = self.layout.config()                   # home.json edits apply at the next start
        cfg, layout = self.cfg, self.layout
        if tag != self.tag:
            self.tag_crashes, self.consecutive = [], 0
        env = release_env(layout, cfg, tag)
        env.update(user_environment(["NOYI_DEEPSEEK_KEY"]))
        # Home default: DeepSeek official, deepseek-flash (it can read photos). A model
        # saved later in the page's settings (data/model.json) takes precedence.
        for name, value in (("PA_MODEL", "deepseek-flash"), ("PA_BASE_URL", "https://api.deepseek.com"), ("PA_API_KEY_ENV", "NOYI_DEEPSEEK_KEY")):
            env.setdefault(name, value)
        if cfg.get("remote_hosts"):
            env["NOYI_REMOTE_HOSTS"] = ",".join(cfg["remote_hosts"])
        if cfg.get("remote_login"):
            env["NOYI_REMOTE_LOGIN"] = cfg["remote_login"]
        logs = layout.data / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "server.log").open("ab") as log:
            log.write(f"\n--- supervisor starting {tag} at {iso()} ---\n".encode())
            log.flush()
            self.proc = subprocess.Popen(serve_command(layout, cfg, tag), cwd=layout.release(tag), env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.tag, self.started, self.state = tag, iso(), "running"
        self.status()

    def stop_child(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            shutdown(self.cfg["port"])
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                kill_tree(self.proc.pid)
                self.proc.wait(10)
        self.proc = None

    def run(self):
        stop_file = self.layout.root / "stop-supervisor"
        wait_until = 0.0
        while True:
            if stop_file.exists():
                self.stop_child()
                stop_file.unlink(missing_ok=True)
                self.state = "exited"
                self.status()
                return
            current = (read_json(self.layout.current, {}) or {}).get("tag")
            if self.proc and self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                if self.layout.hold.exists():
                    self.state = "held"                       # the upgrader stopped it on purpose
                elif code == 0:
                    self.restarts.append(time.time())         # stopped from the desktop page: always-on means start again
                    self.consecutive = 0
                else:
                    now = time.time()
                    self.crashes.append(now)
                    self.tag_crashes.append(now)
                    self.consecutive += 1
                    recent = [t for t in self.tag_crashes if now - t < self.cfg["crash_window"]]
                    if len(recent) >= self.cfg["crash_limit"]:
                        self.state, self.stopped_for = "crash_loop", self.tag
                    else:
                        backoff = self.cfg["backoff"]
                        wait_until = now + backoff[min(self.consecutive - 1, len(backoff) - 1)]
                        self.state = "backoff"
                self.status(exit_code=code)
            if self.proc is None:
                if self.layout.hold.exists():
                    if self.state != "held":
                        self.state = "held"
                        self.status()
                elif not current:
                    if self.state != "no_release":
                        self.state = "no_release"
                        self.status()
                elif self.state == "crash_loop" and current == self.stopped_for:
                    pass                                      # wait for a different release
                elif time.time() >= wait_until:
                    if self.tag and self.started:
                        self.restarts.append(time.time())
                    self.start(current)
            elif current and current != self.tag and not self.layout.hold.exists():
                # current.json changed without a hold (by hand): switch over cleanly.
                self.stop_child()
                self.start(current)
            time.sleep(POLL)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(r"D:\Noyi"))
    args = parser.parse_args()
    supervisor = Supervisor(Layout(args.root))
    try:
        supervisor.run()
    finally:
        if supervisor.proc and supervisor.proc.poll() is None:
            supervisor.stop_child()


if __name__ == "__main__":
    main()
