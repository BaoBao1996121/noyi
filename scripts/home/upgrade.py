"""Pull a newer home-* release from GitHub and switch to it without risking the ledger.

Run every 10 minutes by the scheduled task "\\Noyi\\Upgrade", and once by the installer.

1. Fetch; take the newest `home-YYYYMMDD-N` tag that is newer than the running
   one, has not failed before, and points at a commit already on origin/main.
2. Check it out into its own release directory; install its dependencies; run
   its smoke tests.
3. Copy the ledger with SQLite's online backup and check the copy's integrity.
4. Rehearse: the new release opens the copy (running any migration) on another
   port with background work off; its pages must answer.
5. Switch: stop the running release (it saves execution state), take the
   restore point, point current.json at the new release; the supervisor starts it.
6. Health check for two minutes. If it fails: stop it, restore the ledger from
   the restore point, point back at the old release. An older release refuses a
   newer ledger, so going back always means restoring, never only swapping code.

    python upgrade.py --root D:\\Noyi            # check once
    python upgrade.py --root D:\\Noyi --tag home-20261114-1
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (Layout, TAG, bootstrap, http, iso, kill_tree, read_json, release_env, release_python,  # noqa: E402
                    shutdown, tag_key, write_json)


class UpgradeFailed(Exception):
    def __init__(self, result, stage, detail=""):
        super().__init__(f"{stage}: {detail}")
        self.result, self.stage, self.detail = result, stage, detail


def run(cmd, *, cwd=None, env=None, timeout=900):
    done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout + done.stderr)[-2000:]


def git(layout, *args, timeout=300):
    return run(["git", "-C", str(layout.repo), *args], timeout=timeout)


class Upgrader:
    def __init__(self, layout, log=print):
        self.layout, self.cfg, self.log = layout, layout.config(), log
        self.state = read_json(layout.upgrade, {}) or {}
        self.state.setdefault("failed", {})

    # ---- choosing -----------------------------------------------------------------
    def candidates(self):
        code, out = git(self.layout, "fetch", "--quiet", "--force", "--tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
        if code:
            raise UpgradeFailed("none", "fetch", out)
        code, out = git(self.layout, "tag", "--list", "home-*")
        tags = sorted((t for t in out.split() if TAG.fullmatch(t)), key=tag_key)
        current = (read_json(self.layout.current, {}) or {}).get("tag")
        newer = [t for t in tags if not current or tag_key(t) > tag_key(current)]
        good = []
        for tag in newer:
            if tag in self.state["failed"]:
                continue
            code, _ = git(self.layout, "merge-base", "--is-ancestor", tag + "^{commit}", "refs/remotes/origin/main")
            if code == 0:
                good.append(tag)
            else:
                self.log(f"skip {tag}: not on origin/main")
        return good

    # ---- preparing ------------------------------------------------------------------
    def prepare(self, tag):
        target = self.layout.release(tag)
        if target.exists():
            shutil.rmtree(target)                         # a half-prepared earlier attempt
        target.parent.mkdir(parents=True, exist_ok=True)
        code, out = run(["git", "clone", "--quiet", "--no-hardlinks", "--branch", tag, str(self.layout.repo), str(target)], timeout=600)
        if code:
            raise UpgradeFailed("rehearsal_failed", "checkout", out)
        if self.cfg["install"]:
            py = sys.executable
            steps = [[py, "-m", "venv", str(target / ".venv")],
                     [release_python(self.layout, self.cfg, tag), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "requirements-lock.txt"],
                     [release_python(self.layout, self.cfg, tag), "-m", "pip", "install", "--quiet", "--no-deps", "-e", "."],
                     [shutil.which("npm") or "npm", "ci", "--prefix", "src/personal_agent/pi_bridge", "--ignore-scripts", "--no-audit", "--no-fund"]]
            for step in steps:
                code, out = run(step, cwd=target, timeout=1800)
                if code:
                    raise UpgradeFailed("rehearsal_failed", "install", out)
        if self.cfg["smoke"]:
            code, out = run([release_python(self.layout, self.cfg, tag), "-m", "pytest", "-q", "-p", "no:cacheprovider", *self.cfg["smoke"]],
                            cwd=target, env=release_env(self.layout, self.cfg, tag), timeout=1800)
            if code:
                raise UpgradeFailed("rehearsal_failed", "smoke", out)

    # ---- ledger copies -----------------------------------------------------------------
    @property
    def ledger(self):
        return self.layout.data / "state.sqlite"

    def backup(self, label):
        """Online-consistent copy that passed integrity_check, or None when there is no ledger yet."""
        if not self.ledger.exists():
            return None
        folder = self.layout.data / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
        source = sqlite3.connect(f"file:{self.ledger.as_posix()}?mode=ro", uri=True, timeout=30)
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            ok = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
            source.close()
        if ok != "ok":
            target.unlink(missing_ok=True)
            raise UpgradeFailed("rehearsal_failed", "backup", "integrity_check: " + str(ok)[:200])
        return target

    def restore(self, backup):
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.ledger) + suffix).unlink(missing_ok=True)
        if backup:
            shutil.copyfile(backup, self.ledger)

    def prune_backups(self):
        folder = self.layout.data / "backups"
        for prefix in ("pre-", "switch-"):
            old = sorted(folder.glob(prefix + "*.sqlite"))[:-self.cfg["keep_backups"]]
            for path in old:
                path.unlink(missing_ok=True)

    # ---- rehearsal ---------------------------------------------------------------------
    def rehearse(self, tag, backup):
        place = self.layout.root / "rehearsal"
        shutil.rmtree(place, ignore_errors=True)
        place.mkdir(parents=True)
        if backup:
            shutil.copyfile(backup, place / "state.sqlite")
        port = self.cfg["port"] + 1
        env = release_env(self.layout, self.cfg, tag)
        for name in ("NOYI_DEEPSEEK_KEY", "NOYI_REMOTE_HOSTS"):
            env.pop(name, None)                            # the copy never talks to a model or a phone
        cmd = [release_python(self.layout, self.cfg, tag), "-m", "personal_agent", "--data", str(place), "web", "--port", str(port), "--rehearsal"]
        with (place / "rehearsal.log").open("wb") as log:
            proc = subprocess.Popen(cmd, cwd=self.layout.release(tag), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.time() + 120
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise UpgradeFailed("rehearsal_failed", "rehearsal", f"exited {proc.returncode}: " + (place / "rehearsal.log").read_text("utf-8", "replace")[-1500:])
                if bootstrap(port) and http(port, "/api/state?room=local")[0] == 200:
                    return
                time.sleep(0.5)
            raise UpgradeFailed("rehearsal_failed", "rehearsal", "pages did not answer in 120 s")
        finally:
            shutdown(port)
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                proc.wait(10)
            shutil.rmtree(place, ignore_errors=True)

    # ---- switching ---------------------------------------------------------------------
    def stop_running(self):
        """Hold the supervisor and stop whatever runs now. Waits for the process to exit."""
        self.layout.hold.write_text(iso(), encoding="utf-8")
        port = self.cfg["port"]
        shutdown(port)
        sup = read_json(self.layout.supervisor, {}) or {}
        deadline = time.time() + 60
        while time.time() < deadline:
            sup = read_json(self.layout.supervisor, {}) or {}
            if bootstrap(port) is None and sup.get("state") in {"held", "no_release", "crash_loop", "backoff", "exited", None}:
                return
            time.sleep(0.5)
        if sup.get("pid"):
            kill_tree(sup["pid"])
        time.sleep(2)

    def healthy(self, tag):
        port, need = self.cfg["port"], self.cfg.get("stable_seconds", 20)
        deadline, since = time.time() + self.cfg["health_seconds"], None
        while time.time() < deadline:
            sup = read_json(self.layout.supervisor, {}) or {}
            ok = sup.get("tag") == tag and sup.get("state") == "running" and bootstrap(port) and http(port, "/api/state?room=local")[0] == 200
            if ok:
                since = since or time.time()
                if time.time() - since >= need:
                    return True
            else:
                since = None
            time.sleep(0.5)
        return False

    def switch(self, tag, previous):
        self.stop_running()
        point = self.backup("switch-" + tag)               # after the stop: nothing written since is lost on rollback
        write_json(self.layout.current, {"tag": tag, "previous": previous, "switched": iso()})
        self.layout.hold.unlink(missing_ok=True)
        if self.healthy(tag):
            return point
        self.log(f"{tag} failed its health check; restoring")
        self.stop_running()
        self.restore(point)
        write_json(self.layout.current, {"tag": previous, "previous": tag, "switched": iso()} if previous else {})
        self.layout.hold.unlink(missing_ok=True)
        if previous and not self.healthy(previous):
            raise UpgradeFailed("rolled_back", "rollback", "the previous release did not come back healthy either")
        raise UpgradeFailed("health_failed_rolled_back", "health", "new release did not stay healthy; ledger restored")

    def after_success(self, tag):
        source = self.layout.release(tag) / "scripts" / "home"
        if source.is_dir():
            self.layout.bin.mkdir(parents=True, exist_ok=True)
            for path in source.iterdir():
                if path.suffix in {".py", ".ps1"}:
                    shutil.copyfile(path, self.layout.bin / path.name)
        current = read_json(self.layout.current, {}) or {}
        keep = {current.get("tag"), current.get("previous")}
        if self.layout.releases.is_dir():
            for path in self.layout.releases.iterdir():
                if path.name not in keep:
                    shutil.rmtree(path, ignore_errors=True)
        self.prune_backups()

    # ---- the whole thing ---------------------------------------------------------------
    def upgrade(self, tag=None):
        previous = (read_json(self.layout.current, {}) or {}).get("tag")
        try:
            tags = [tag] if tag else self.candidates()
        except UpgradeFailed as exc:
            return self.record("none", None, previous, f"{exc.stage}: {exc.detail[-300:]}")
        if not tags:
            return self.record("none", None, previous)
        tag = tags[-1]
        self.log(f"upgrading {previous or '(nothing)'} -> {tag}")
        try:
            self.prepare(tag)
            first = self.backup("pre-" + tag)
            self.rehearse(tag, first)
            self.switch(tag, previous)
        except UpgradeFailed as exc:
            self.state["failed"][tag] = {"stage": exc.stage, "at": iso()}
            if exc.result == "rehearsal_failed":
                shutil.rmtree(self.layout.release(tag), ignore_errors=True)
            self.log(f"{tag} failed at {exc.stage}: {exc.detail[-600:]}")
            return self.record(exc.result, tag, previous, exc.stage)
        self.after_success(tag)
        return self.record("ok" if previous else "install", tag, previous)

    def record(self, result, tag, previous, detail=None):
        now = iso()
        self.state.update({"checked": now, "result": result, "detail": detail})
        if result != "none":
            self.state.update({"last_upgrade": now, "last_result": result, "tag": tag, "previous_tag": previous})
        write_json(self.layout.upgrade, self.state)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(r"D:\Noyi"))
    parser.add_argument("--tag", help="upgrade to this tag (still checked against origin/main)")
    args = parser.parse_args()
    layout = Layout(args.root)
    try:
        with open(layout.lock, "x", encoding="utf-8") as lock:
            lock.write(iso())
    except FileExistsError:
        if time.time() - layout.lock.stat().st_mtime < 3 * 3600:
            print("another upgrade is running")
            return
        layout.lock.write_text(iso(), encoding="utf-8")     # left over from a crash hours ago
    try:
        upgrader = Upgrader(layout)
        if args.tag:
            if not TAG.fullmatch(args.tag) or args.tag not in upgrader.candidates():
                print(f"{args.tag} is not an eligible release (not newer, failed before, or not on origin/main)")
                sys.exit(2)
        print(upgrader.upgrade(args.tag))
    finally:
        layout.lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
