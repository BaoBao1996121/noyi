"""Images are sources too: shown only to models verified to see them."""
import base64
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import ScriptedModel, submit_written_artifact, tool_reply
from personal_agent.contracts import TaskSpec
from personal_agent.model import ModelConfig
from personal_agent.service import Service
from personal_agent.webui import create_app

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


class SeeingModel(ScriptedModel):
    def __init__(self, steps, name):
        super().__init__(steps)
        self.config = ModelConfig(name=name, base_url="https://api.deepseek.com")


def image_spec(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(PNG)
    return TaskSpec("Describe the photo.", [{"id": "photo", "kind": "file", "location": str(path.resolve()), "required": True}])


def image_parts(request):
    return [part for m in request["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for part in m["content"] if part.get("type") == "image_url"]


async def test_vision_model_sees_image_as_user_content_and_coverage_counts(store, tmp_path):
    model = SeeingModel([tool_reply("read_source", {"source_id": "photo"}),
                         tool_reply("write_artifact", {"name": "photo.md", "content": "A single pixel. [photo]"}),
                         submit_written_artifact], "deepseek-flash")
    tid = store.submit(image_spec(tmp_path), nonce="see")["task_id"]
    await Service(store, tmp_path / "data", model).serve(once=True)
    assert store.task(tid)["status"] == "succeeded"
    parts = image_parts(model.requests[1])
    assert len(parts) == 1 and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # Bytes are reloaded per request; the ledger keeps only the receipt.
    read = next(o for o in store.operations(tid) if o["tool"] == "read_source")
    assert read["result"]["image"]["media_type"] == "image/png" and "base64" not in json.dumps(read["result"])
    assert len(image_parts(model.requests[2])) == 1


async def test_blind_model_gets_an_honest_unavailable_instead_of_an_image(store, tmp_path):
    model = SeeingModel([tool_reply("read_source", {"source_id": "photo"}),
                         tool_reply("report_unable", {"explanation": "当前模型看不到图片。"})], "deepseek-v4-pro")
    tid = store.submit(image_spec(tmp_path), nonce="blind")["task_id"]
    await Service(store, tmp_path / "data", model).serve(once=True)
    read = next(o for o in store.operations(tid) if o["tool"] == "read_source")
    assert read["status"] == "failed" and read["result"]["error"] == "SourceUnavailable"
    assert "cannot see images" in read["result"]["detail"]
    assert not image_parts(model.requests[1])


async def test_tampered_image_snapshot_is_not_shown(store, tmp_path):
    def tamper(messages, tools):
        result = json.loads(next(m for m in reversed(messages) if m["role"] == "tool")["content"])
        (tmp_path / "data" / "tasks" / tid / result["snapshot"]).write_bytes(PNG + b"x")
        return tool_reply("write_artifact", {"name": "photo.md", "content": "pixel [photo]"})
    model = SeeingModel([tool_reply("read_source", {"source_id": "photo"}), tamper,
                         lambda m, t: tool_reply("report_unable", {"explanation": "图片被改动。"})], "deepseek-flash")
    tid = store.submit(image_spec(tmp_path), nonce="tamper")["task_id"]
    await Service(store, tmp_path / "data", model).serve(once=True)
    assert not image_parts(model.requests[2])


@pytest.mark.asyncio
async def test_upload_decides_image_by_bytes_not_name(store, tmp_path):
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        token = (await (await client.get("/api/bootstrap")).json())["token"]
        client.session.headers.update({"X-PA-Token": token})
        import aiohttp
        image = aiohttp.FormData()
        image.add_field("file", PNG, filename="notes.txt", content_type="text/plain")
        uploaded = await (await client.post("/api/uploads", data=image)).json()
        assert store.one("SELECT path FROM uploads WHERE id=?", (uploaded["id"],))["path"].endswith(".image")
        fake = aiohttp.FormData()
        fake.add_field("file", b"\x00\x01binary", filename="photo.png", content_type="image/png")
        assert (await client.post("/api/uploads", data=fake)).status == 400
        big = aiohttp.FormData()
        big.add_field("file", b"a" * 200_000, filename="big.txt", content_type="text/plain")
        assert (await client.post("/api/uploads", data=big)).status == 400
