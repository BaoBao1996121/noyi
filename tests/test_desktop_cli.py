"""Focused tests for scripts/desktop.py status/restart decision logic."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import desktop  # noqa: E402


URL = "http://127.0.0.1:8765"


def write_marker(path: Path, pid=4321, instance_id="inst-1"):
    path.write_text(
        json.dumps({"pid": pid, "port": 8765, "data": str(path.parent), "instance_id": instance_id}),
        encoding="utf-8",
    )


def test_describe_status_reports_stopped_when_nothing_responds(tmp_path, monkeypatch):
    marker = tmp_path / "desktop-process.json"
    monkeypatch.setattr(desktop, "status", lambda url: None)

    info = desktop.describe_status(URL, marker)

    assert info == {"running": False, "url": URL, "pid": None, "stale_marker": False}


def test_describe_status_reports_stale_marker_when_stopped_with_leftover_file(tmp_path, monkeypatch):
    marker = tmp_path / "desktop-process.json"
    write_marker(marker)
    monkeypatch.setattr(desktop, "status", lambda url: None)

    info = desktop.describe_status(URL, marker)

    assert info["running"] is False
    assert info["stale_marker"] is True


def test_describe_status_reports_running_with_pid_when_marker_matches(tmp_path, monkeypatch):
    marker = tmp_path / "desktop-process.json"
    write_marker(marker, pid=4321, instance_id="inst-1")
    monkeypatch.setattr(desktop, "status", lambda url: {"instance_id": "inst-1", "title": "Noyi"})

    info = desktop.describe_status(URL, marker)

    assert info == {"running": True, "url": URL, "pid": 4321, "stale_marker": False}


def test_describe_status_reports_running_without_pid_when_no_marker(tmp_path, monkeypatch):
    marker = tmp_path / "desktop-process.json"
    monkeypatch.setattr(desktop, "status", lambda url: {"instance_id": "inst-x", "title": "Noyi"})

    info = desktop.describe_status(URL, marker)

    assert info == {"running": True, "url": URL, "pid": None, "stale_marker": False}


def test_describe_status_flags_stale_marker_when_instance_differs(tmp_path, monkeypatch):
    marker = tmp_path / "desktop-process.json"
    write_marker(marker, pid=111, instance_id="old-inst")
    monkeypatch.setattr(desktop, "status", lambda url: {"instance_id": "new-inst", "title": "Noyi"})

    info = desktop.describe_status(URL, marker)

    assert info["running"] is True
    assert info["pid"] is None
    assert info["stale_marker"] is True


def test_run_status_prints_running_with_pid(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "desktop-process.json"
    write_marker(marker, pid=4321, instance_id="inst-1")
    monkeypatch.setattr(desktop, "status", lambda url: {"instance_id": "inst-1", "title": "Noyi"})
    args = Namespace(data=tmp_path, port=8765)

    desktop.run_status(args)

    out = capsys.readouterr().out
    assert "正在运行" in out
    assert "4321" in out
    assert URL in out


def test_run_status_prints_stopped_and_stale_hint(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "desktop-process.json"
    write_marker(marker)
    monkeypatch.setattr(desktop, "status", lambda url: None)
    args = Namespace(data=tmp_path, port=8765)

    desktop.run_status(args)

    out = capsys.readouterr().out
    assert "未运行" in out
    assert "残留" in out


def test_restart_calls_stop_then_start(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "do_stop", lambda args: calls.append("stop"))
    monkeypatch.setattr(desktop, "do_start", lambda args: calls.append("start"))
    monkeypatch.setattr(sys, "argv", ["desktop.py", "restart"])

    desktop.main()

    assert calls == ["stop", "start"]


def test_status_action_does_not_touch_start_or_stop(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(desktop, "do_stop", lambda args: calls.append("stop"))
    monkeypatch.setattr(desktop, "do_start", lambda args: calls.append("start"))
    monkeypatch.setattr(desktop, "status", lambda url: None)
    monkeypatch.setattr(sys, "argv", ["desktop.py", "status", "--data", str(tmp_path)])

    desktop.main()

    assert calls == []


class Namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_service_titles_accept_new_name_and_old_running_service():
    # A service started before the rename still answers "Personal Agent"; start must
    # recognise both so `noyi restart` across the rename does not time out.
    assert desktop.SERVICE_TITLES[0] == "Noyi"
    assert "Personal Agent" in desktop.SERVICE_TITLES
