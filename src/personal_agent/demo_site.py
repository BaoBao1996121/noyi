"""Export a scripted demo library as a static, read-only site (GitHub Pages).

The site is the real WebUI with every GET it makes saved as a file. Three
checks make it impossible to publish a personal library by mistake:

1. Source: every model request in the library must be ``demo-scripted``;
   real use always records the real model, so pointing this at a personal
   library fails before anything is written.
2. Whitelist: only the read endpoints the UI calls are exported, through the
   same handlers the app serves; no database file is copied.
3. Paths: the library's own folder becomes a relative path; any other local
   path left in the output stops the export and names the file it was found in.

The output is written to a temporary folder and moved into place only after
all checks pass, so a failed export never leaves a half-built site.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tempfile

from aiohttp.test_utils import TestClient, TestServer

from .model import ModelConfig
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
PERSONAL = (ROOT / ".pa" / "personal").resolve()
WEB = Path(__file__).resolve().parent / "web"
DEMO_MODEL = "demo-scripted"
ASSETS = ["app.js", "style.css", "modules.css", "goals.js", "goals.css", "reviews.js", "replay.js", "replay.css",
          "replay-pixel.js", "galaxy.js", "galaxy.css"]
# Windows drive paths, UNC paths and the usual home/system roots on Unix.
LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+[^\s\"'<>]*|(?<![\\\w])\\\\[A-Za-z][A-Za-z0-9.-]+\\[A-Za-z0-9$_.-]+"
                        r"|(?<![\w.:/])/(?:home|Users|root|tmp|var|etc|opt|mnt|srv|private)/[^\s\"'<>]*")


class DemoExportError(RuntimeError):
    pass


class _ReadOnlyModel:
    ready = True
    config = ModelConfig(name=DEMO_MODEL, base_url="https://api.deepseek.com")

    async def complete(self, messages, tools):
        raise RuntimeError("the demo site never calls a model")

    async def close(self):
        pass


def check_source(data: Path) -> list[str]:
    """Refuse anything but a non-empty, scripted-only demo library."""
    data = Path(data).resolve()
    if data == PERSONAL:
        raise DemoExportError("演示站不能从自用库 .pa/personal 导出。")
    if not (data / "state.sqlite").is_file():
        raise DemoExportError(f"{data.name} 里没有演示库，先运行 demo-replay.py build。")
    store = Store(data / "state.sqlite")
    try:
        rows = store.all("SELECT metadata FROM model_requests")
        tasks = [r["id"] for r in store.all("SELECT id FROM tasks WHERE parent_task_id IS NULL ORDER BY created,rowid")]
    finally:
        store.close()
    real = sorted({json.loads(r["metadata"] or "{}").get("requested_model") or "（未记录）" for r in rows} - {DEMO_MODEL})
    if real:
        raise DemoExportError(f"库里有 {DEMO_MODEL} 以外的模型调用（{', '.join(real)}），不是演示库，拒绝导出。")
    if not rows or not tasks:
        raise DemoExportError("库里没有演示事情可导出。")
    return tasks


def _scrub_string(text: str, data: Path, where: str) -> str:
    # A path inside the library folder becomes relative with "/" separators.
    # Strings can hold JSON-encoded text (tool arguments), so the folder is
    # matched with its separators escaped any number of times.
    head, *rest = re.split(r"[\\/]", str(data))
    folder = re.escape(head) + "".join(r"[\\/]+" + re.escape(part) for part in rest)
    text = re.sub(folder + r"(?:[\\/]+([^\s\"'<>]*))?", lambda m: re.sub(r"[\\/]+", "/", m.group(1)) if m.group(1) else ".", text, flags=re.IGNORECASE)
    found = LOCAL_PATH.search(text)
    if found:
        raise DemoExportError(f"{where} 里还有本机路径 {found.group(0)[:80]}，拒绝导出。")
    return text


def _scrub(body: str, data: Path, where: str) -> str:
    """Scrub every string value (JSON) or the page text (HTML), never the JSON syntax itself."""
    if not where.endswith(".json"):
        return _scrub_string(body, data, where)
    def walk(value):
        if isinstance(value, str):
            return _scrub_string(value, data, where)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {walk(k): walk(v) for k, v in value.items()}
        return value
    return json.dumps(walk(json.loads(body)), ensure_ascii=False, sort_keys=True)


async def export_site(data, out, *, commit: str = "") -> dict:
    data, out = Path(data).resolve(), Path(out).resolve()
    tasks = check_source(data)
    from .webui import create_app
    store = Store(data / "state.sqlite")
    files: dict[str, str] = {}
    stories: dict[str, str] = {}
    try:
        async with TestClient(TestServer(create_app(store, data, _ReadOnlyModel(), run_service=False))) as client:
            async def get(path, rel, kind="json"):
                response = await client.get(path)
                if response.status != 200:
                    raise DemoExportError(f"{path} 返回 {response.status}，拒绝导出。")
                body = await response.text()
                if kind == "json":
                    body = json.dumps(json.loads(body), ensure_ascii=False, sort_keys=True)
                files[rel] = body
                return json.loads(body) if kind == "json" else body
            boot = await get("/api/bootstrap", "app/api/bootstrap.json")
            state = await get("/api/state?room=local", "app/api/state.json")
            await get("/api/memory/galaxy?room=local", "app/api/memory/galaxy.json")
            for task_id in tasks:
                await get(f"/api/tasks/{task_id}", f"app/api/tasks/{task_id}.json")
                stories[task_id] = (await get(f"/api/tasks/{task_id}/timeline", f"app/api/tasks/{task_id}/timeline.json"))["outcome"]
            for delivery in state["deliveries"]:
                if delivery.get("artifact"):
                    await get(f"/api/artifacts/{delivery['id']}", f"app/api/artifacts/{delivery['id']}.json")
                    if delivery.get("task_id") and f"app/r/{delivery['task_id']}.html" not in files:
                        await get(f"/r/{delivery['task_id']}", f"app/r/{delivery['task_id']}.html", "html")
    finally:
        store.close()
    # A static copy has no session: no token, and it says it is static.
    files["app/api/bootstrap.json"] = json.dumps({**boot, "token": "", "static": True}, ensure_ascii=False, sort_keys=True)
    files = {rel: _scrub(body, data, rel) for rel, body in files.items()}

    index = (WEB / "index.html").read_text(encoding="utf-8").replace('href="/assets/', 'href="assets/').replace('src="/assets/', 'src="assets/')
    files["app/index.html"] = index.replace("<html ", '<html data-static="1" ', 1)
    landing = WEB / "site"
    for name in ("index.html", "landing.css", "landing.js"):
        if (landing / name).is_file():
            files[name] = (landing / name).read_text(encoding="utf-8")
    files[".nojekyll"] = ""
    manifest = {"commit": commit, "stories": stories, "files": sorted(files) + [f"app/assets/{a}" for a in ASSETS]}
    files["build.json"] = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True)

    staging = Path(tempfile.mkdtemp(prefix="noyi-site-", dir=out.parent if out.parent.exists() else None))
    try:
        for rel, body in files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8", newline="\n")
        (staging / "app" / "assets").mkdir(parents=True, exist_ok=True)
        for name in ASSETS:
            shutil.copyfile(WEB / name, staging / "app" / "assets" / name)
        images = ROOT / "docs" / "images"
        if images.is_dir():
            shutil.copytree(images, staging / "images")
        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(out))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest
