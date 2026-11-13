"""Static demo site: only a scripted demo library can be exported, and nothing local leaks out."""
import json
from pathlib import Path

import pytest

from personal_agent.demo_site import DemoExportError, export_site
from personal_agent.runtime import Runtime
from personal_agent.store import Store
from conftest import ScriptedModel, submit_written_artifact, tool_reply
from test_commitments import deliver_all, intake


class DemoModel(ScriptedModel):
    metadata = {"provider": "demo", "requested_model": "demo-scripted"}


def steps():
    return [tool_reply("read_source", {"source_id": "policy"}, "read"),
            tool_reply("write_artifact", {"name": "report.md", "content": "The release requires human review. [policy]"}, "write"),
            submit_written_artifact]


async def library(tmp_path, model, *, text="帮我整理这份资料", sources_in_data=True):
    data = tmp_path / "lib"
    data.mkdir()
    folder = data / "sources" if sources_in_data else tmp_path / "elsewhere"
    folder.mkdir()
    path = folder / "policy.txt"
    path.write_text("Current policy: the release requires a human review.\n", encoding="utf-8")
    store = Store(data / "state.sqlite")
    try:
        result = intake(store, text, {"action": "new_task", "receipt": "好，我去整理，做好发你。"}, nonce="n1",
                        sources=[{"id": "policy", "kind": "file", "location": str(path.resolve()), "required": True}])
        await Runtime(store, data, model).execute(store.claim())
        deliver_all(store)
    finally:
        store.close()
    return data, result["task_id"]


def read(out, rel):
    return json.loads((out / rel).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_library_with_real_model_calls_is_refused(tmp_path):
    data, _ = await library(tmp_path, ScriptedModel(steps()))  # no demo metadata: what real use records
    with pytest.raises(DemoExportError, match="demo-scripted"):
        await export_site(data, tmp_path / "site")
    assert not (tmp_path / "site").exists()


def test_an_empty_or_missing_library_is_refused(tmp_path):
    Store(tmp_path / "empty" / "state.sqlite").close()
    with pytest.raises(DemoExportError, match="没有"):
        import asyncio
        asyncio.run(export_site(tmp_path / "empty", tmp_path / "site"))
    with pytest.raises(DemoExportError):
        asyncio.run(export_site(tmp_path / "missing", tmp_path / "site"))


@pytest.mark.asyncio
async def test_export_writes_every_read_the_app_makes_and_no_write_endpoint(tmp_path):
    data, task_id = await library(tmp_path, DemoModel(steps()))
    out = tmp_path / "site"
    manifest = await export_site(data, out, commit="abc123")
    state = read(out, "app/api/state.json")
    assert [t["id"] for t in state["tasks"]] == [task_id]
    assert read(out, f"app/api/tasks/{task_id}/timeline.json")["outcome"] == "fulfilled"
    assert read(out, f"app/api/tasks/{task_id}.json")["task"]["id"] == task_id
    assert "stars" in read(out, "app/api/memory/galaxy.json") or read(out, "app/api/memory/galaxy.json") is not None
    boot = read(out, "app/api/bootstrap.json")
    assert boot["static"] is True and boot["token"] == ""
    delivery = next(d for d in state["deliveries"] if d["artifact"])
    assert "human review" in read(out, f"app/api/artifacts/{delivery['id']}.json")["text"]
    assert (out / "app" / "r" / f"{task_id}.html").is_file()
    html = (out / "app" / "index.html").read_text(encoding="utf-8")
    assert 'data-static="1"' in html and '"/assets/' not in html
    assert (out / "index.html").is_file() and (out / ".nojekyll").is_file()
    built = read(out, "build.json")
    assert built["commit"] == "abc123" and built["stories"][task_id] == "fulfilled"
    assert manifest["files"] == built["files"]
    names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert not any(n.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm")) for n in names)
    assert not any(part in n for n in names for part in ("backup", "settings", "messages", "uploads", "shutdown", "manage"))


@pytest.mark.asyncio
async def test_paths_inside_the_library_are_made_relative(tmp_path):
    data, task_id = await library(tmp_path, DemoModel(steps()))
    store = Store(data / "state.sqlite")
    try:  # a message that quotes a file in the library by its full path
        store.db.execute("UPDATE messages SET content=content||? WHERE kind='user'", (" 见 " + str((data / "sources" / "policy.txt").resolve()),))
    finally:
        store.close()
    out = tmp_path / "site"
    await export_site(data, out)
    text = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.json")) + "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.html"))
    assert str(data.resolve()) not in text and json.dumps(str(data.resolve()))[1:-1] not in text
    assert "sources/policy.txt" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("leak", [r"C:\Users\someone\notes.md", "/home/someone/notes.md", "/Users/someone/notes.md"])
async def test_any_other_local_path_stops_the_export_and_names_where(tmp_path, leak):
    data, _ = await library(tmp_path, DemoModel(steps()))
    store = Store(data / "state.sqlite")
    try:
        store.db.execute("UPDATE messages SET content=content||? WHERE kind='user'", (" 参考 " + leak,))
    finally:
        store.close()
    with pytest.raises(DemoExportError) as error:
        await export_site(data, tmp_path / "site")
    assert "api/state.json" in str(error.value) and "someone" in str(error.value)
    assert not (tmp_path / "site").exists()


@pytest.mark.asyncio
async def test_the_personal_library_is_refused_by_location(tmp_path, monkeypatch):
    import personal_agent.demo_site as demo_site
    data, _ = await library(tmp_path, DemoModel(steps()))
    monkeypatch.setattr(demo_site, "PERSONAL", data.resolve())
    with pytest.raises(DemoExportError, match="自用库"):
        await export_site(data, tmp_path / "site")


def test_the_demo_build_tells_the_four_stories_it_promises(tmp_path):
    """The landing page links to these outcomes; a build that drifts from them must fail, not ship."""
    import importlib.util
    import time
    script = Path(__file__).resolve().parents[1] / "scripts" / "demo-replay.py"
    spec = importlib.util.spec_from_file_location("demo_replay", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real = time.time
    try:
        module.build(tmp_path / "demo")
    finally:
        time.time = real
    store = Store(tmp_path / "demo" / "state.sqlite")
    try:
        roots = [r["id"] for r in store.all("SELECT id FROM tasks WHERE parent_task_id IS NULL ORDER BY created,rowid")]
        assert [store.task_timeline(t)["outcome"] for t in roots] == ["fulfilled", "overdue", "unable", "fulfilled"]
    finally:
        store.close()
