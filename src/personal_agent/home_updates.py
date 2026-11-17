"""Small authenticated UI bridge to the home supervisor's fixed main updater.

No shell, repository URL, ref, executable or filesystem path comes from HTTP.
The supervisor launches the worker so it survives replacement of this service.
"""
import json
import os
from pathlib import Path


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class HomeUpdates:
    def __init__(self, data):
        value = os.environ.get("NOYI_HOME_ROOT")
        self.root = Path(value).resolve() if value else None
        if self.root and (self.root / "data").resolve() != Path(data).resolve():
            self.root = None

    def status(self):
        if not self.root:
            return {"available": False}
        current = read(self.root / "current.json")
        progress = read(self.root / "update-progress.json")
        result = read(self.root / "upgrade.json")
        supervisor = read(self.root / "supervisor.json")
        queued = (self.root / "update-request.json").exists()
        phase = progress.get("phase", "idle")
        busy = phase not in {"idle", "done", "error"} and alive(progress.get("pid"))
        if phase not in {"idle", "done", "error"} and not busy and not queued:
            phase = "interrupted"
        return {"available": True, "current": current.get("tag"), "previous": current.get("previous"),
                "busy": queued or busy, "phase": "queued" if queued else phase,
                "supervisor_online": alive(supervisor.get("supervisor_pid")),
                "target": progress.get("target"), "checked": result.get("checked"),
                "result": result.get("result"), "stage": result.get("detail") if result.get("detail") in {
                    "checkout", "install", "smoke", "backup", "rehearsal", "health", "rollback"} else None,
                "check_failed": bool(result.get("detail")) and result.get("result") == "none",
                "last_result": result.get("last_result"), "last_upgrade": result.get("last_upgrade")}

    def request(self):
        status = self.status()
        if not status["available"]:
            raise ValueError("此安装未启用家庭更新服务")
        if not status["supervisor_online"]:
            raise ValueError("更新守护进程未运行，请在电脑上启动 Noyi 服务")
        if status["busy"]:
            return {"accepted": True, "already_running": True}
        try:
            with (self.root / "update-request.json").open("x", encoding="utf-8") as handle:
                handle.write("{}")
        except FileExistsError:
            return {"accepted": True, "already_running": True}
        return {"accepted": True, "already_running": False}
