"""Hourly: export a desensitized run bundle and push it to the private noyi-ops repository.

The bundle is built by the running release's own `ops-export` (column registry,
closed schema; health data, words and photos cannot be in it). This script only
adds the supervisor's and upgrader's numbers and does the git work, using a
deploy key that can write to noyi-ops and nothing else.

    python ops_push.py --root D:\\Noyi
    python ops_push.py --root D:\\Noyi --no-push      # export and commit locally only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Layout, TAG, bootstrap, read_json, release_env, release_python, write_json  # noqa: E402

UPGRADE_RESULT = {"ok", "rehearsal_failed", "health_failed_rolled_back", "rolled_back", "none", "install"}


def service_status(layout, cfg):
    sup = read_json(layout.supervisor, {}) or {}
    up = read_json(layout.upgrade, {}) or {}
    status = {"web_ok": bootstrap(cfg["port"]) is not None}
    for key in ("crashes_24h", "restarts_24h"):
        if type(sup.get(key)) is int:
            status[key] = sup[key]
    tag = sup.get("tag")
    status["tag"] = tag if isinstance(tag, str) and TAG.fullmatch(tag) else "none"
    prev = up.get("previous_tag")
    if isinstance(prev, str) and TAG.fullmatch(prev):
        status["previous_tag"] = prev
    if up.get("last_result") in UPGRADE_RESULT:
        status["upgrade_result"] = up["last_result"]
    if isinstance(up.get("last_upgrade"), str):
        status["last_upgrade"] = up["last_upgrade"][:16]
    free = shutil.disk_usage(layout.root).free / 1e9
    status["disk_free_gb"] = round(free, 1)
    return status


def version_info(layout, cfg, tag):
    info = {"tag": tag}
    try:
        info["commit"] = subprocess.run(["git", "-C", str(layout.release(tag)), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                                        text=True, timeout=30).stdout.strip()
        node = subprocess.run([shutil.which("node") or "node", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        info["node"] = node.lstrip("v")
    except (OSError, subprocess.SubprocessError):
        pass
    py = subprocess.run([release_python(layout, cfg, tag), "-c", "import platform;print(platform.python_version())"], capture_output=True,
                        text=True, timeout=30).stdout.strip()
    if py:
        info["python"] = py
    return {k: v for k, v in info.items() if v}


def git(layout, *args, env=None):
    return subprocess.run(["git", "-C", str(layout.ops), *args], capture_output=True, text=True, timeout=300, env=env, stdin=subprocess.DEVNULL)


def push_env(layout):
    key = layout.keys / "noyi-ops"
    env = dict(os.environ)
    if key.exists():
        known = layout.keys / "known_hosts"
        env["GIT_SSH_COMMAND"] = f'ssh -i "{key.as_posix()}" -o IdentitiesOnly=yes -o UserKnownHostsFile="{known.as_posix()}" -o StrictHostKeyChecking=accept-new'
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(r"D:\Noyi"))
    parser.add_argument("--hours", type=float, default=1.0)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    layout = Layout(args.root)
    cfg = layout.config()
    tag = (read_json(layout.current, {}) or {}).get("tag")
    if not tag or not (layout.ops / ".git").exists():
        print("nothing to export: no current release or no noyi-ops clone")
        sys.exit(1)
    env = push_env(layout)
    git(layout, "pull", "--quiet", "--rebase", "--autostash", env=env)
    tmp = layout.root / "ops-tmp"
    tmp.mkdir(exist_ok=True)
    write_json(tmp / "service.json", service_status(layout, cfg))
    write_json(tmp / "version.json", version_info(layout, cfg, tag))
    done = subprocess.run([release_python(layout, cfg, tag), "-m", "personal_agent", "--data", str(layout.data), "ops-export", "--out", str(layout.ops),
                           "--hours", str(args.hours), "--service", str(tmp / "service.json"), "--version", str(tmp / "version.json")],
                          cwd=layout.release(tag), env=release_env(layout, cfg, tag), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if done.returncode:
        # The export refused (schema check) or crashed: nothing is written, nothing leaves.
        write_json(layout.root / "ops-last.json", {"ok": False, "stage": "export", "code": done.returncode})
        print("export refused; nothing pushed")
        sys.exit(1)
    bundle = Path(done.stdout.strip().splitlines()[-1])
    latest = json.loads(bundle.read_text(encoding="utf-8"))
    write_json(layout.ops / "latest.json", {"generated_at": latest.get("generated_at"), "version": latest.get("version"), "service": latest.get("service")})
    git(layout, "add", "bundles", "latest.json")
    if git(layout, "diff", "--cached", "--quiet").returncode == 0:
        print("no change")
        return
    commit = git(layout, "-c", "user.name=noyi-home", "-c", "user.email=noyi-home@localhost", "commit", "--quiet", "-m", "ops: " + bundle.stem)
    if commit.returncode:
        print("commit failed")
        sys.exit(1)
    if args.no_push:
        print("committed locally:", bundle.relative_to(layout.ops))
        return
    pushed = git(layout, "push", "--quiet", "origin", "HEAD", env=env)
    write_json(layout.root / "ops-last.json", {"ok": pushed.returncode == 0, "stage": "push", "bundle": bundle.relative_to(layout.ops).as_posix()})
    print("pushed" if pushed.returncode == 0 else "push failed; will retry next hour (the commit stays queued)")
    sys.exit(0 if pushed.returncode == 0 else 1)


if __name__ == "__main__":
    main()
