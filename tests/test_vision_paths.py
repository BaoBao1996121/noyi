"""A photo sent with a message is seen by whichever layer answers it (2026-11-18).

Before: only execution could see images (read_source); the reply layer and
answer tasks got the file name only, so "午饭，吃了一半" + photo was answered
blind. Images are shown only to models that can see them, only from sources
this message authorized, and only when their bytes match the pinned digest.
"""
import base64
import json

import pytest

from conftest import ScriptedModel, submit_written_artifact, tool_reply
from personal_agent.contracts import TaskSpec, digest
from personal_agent.model import ModelConfig
from personal_agent.runtime import Runtime
from personal_agent.service import Service

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
# Phone photos are usually far larger than a text source may be (128 KB).
PHOTO = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 1200


class Model(ScriptedModel):
    def __init__(self, steps, name="deepseek-flash"):
        super().__init__(steps)
        self.config = ModelConfig(name=name, base_url="https://api.deepseek.com")


def image_parts(request):
    return [part for m in request["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for part in m["content"] if part.get("type") == "image_url"]


def shown_bytes(part):
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


def photo_source(tmp_path, raw=PHOTO, name="label.jpg"):
    path = tmp_path / name
    path.write_bytes(raw)
    # What the web entry records for an uploaded image: its digest is pinned.
    return {"id": "s_photo", "kind": "file", "location": str(path.resolve()), "required": True,
            "description": name, "expected_sha256": digest(raw)}


def incoming(store, text, sources, nonce="n"):
    return store.accept_conversation(TaskSpec(text, sources, max_calls=40, max_tools=100, max_seconds=900), text=text, nonce=nonce)


REPLY = {"action": "reply", "text": "这一份大约 120 千卡。", "promise": ""}
ANSWER = {"action": "new_task", "result_kind": "answer", "receipt": "我按成分表算一下吃一半是多少热量。"}


@pytest.mark.asyncio
async def test_reply_layer_sees_the_photo_sent_with_this_message(store, tmp_path):
    incoming(store, "这个吃了一半，多少热量", [photo_source(tmp_path)])
    model = Model([tool_reply("resolve_conversation", REPLY)])
    await Runtime(store, tmp_path / "data", model).execute(store.claim_conversation())
    [part] = image_parts(model.requests[0])
    assert shown_bytes(part) == PHOTO
    assert store.one("SELECT content FROM deliveries")["content"] == REPLY["text"]


@pytest.mark.asyncio
async def test_answer_task_sees_the_photo_it_was_asked_about(store, tmp_path):
    incoming(store, "这个吃了一半，多少热量", [photo_source(tmp_path)])
    model = Model([tool_reply("resolve_conversation", ANSWER),
                   tool_reply("deliver_answer", {"text": "约 120 千卡。", "promise": ""}, "a")])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    await runtime.execute(store.claim())
    [part] = image_parts(model.requests[1])
    assert shown_bytes(part) == PHOTO
    assert store.all("SELECT content FROM deliveries ORDER BY rowid")[-1]["content"] == "约 120 千卡。"


@pytest.mark.asyncio
async def test_execution_sees_a_full_size_photo_not_a_truncated_one(store, tmp_path):
    spec = TaskSpec("Describe the photo.", [photo_source(tmp_path)])
    model = Model([tool_reply("read_source", {"source_id": "s_photo"}),
                   tool_reply("write_artifact", {"name": "photo.md", "content": "A label. [s_photo]"}),
                   submit_written_artifact])
    tid = store.submit(spec, nonce="exec")["task_id"]
    await Service(store, tmp_path / "data", model).serve(once=True)
    read = next(o for o in store.operations(tid) if o["tool"] == "read_source")
    assert read["status"] == "succeeded", read["result"]
    [part] = image_parts(model.requests[1])
    assert shown_bytes(part) == PHOTO


@pytest.mark.asyncio
async def test_photo_is_attached_only_to_the_message_that_carried_it(store, tmp_path):
    incoming(store, "这个吃了一半，多少热量", [photo_source(tmp_path)], nonce="1")
    model = Model([tool_reply("resolve_conversation", REPLY),
                   tool_reply("resolve_conversation", {"action": "reply", "text": "好的。", "promise": ""}, "r2")])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    incoming(store, "谢谢", [], nonce="2")
    await runtime.execute(store.claim_conversation())
    assert len(image_parts(model.requests[0])) == 1
    assert not image_parts(model.requests[1])


@pytest.mark.asyncio
async def test_changed_photo_is_not_shown(store, tmp_path):
    source = photo_source(tmp_path)
    incoming(store, "这个吃了一半，多少热量", [source])
    (tmp_path / "label.jpg").write_bytes(PHOTO + b"x")
    model = Model([tool_reply("resolve_conversation", REPLY)])
    await Runtime(store, tmp_path / "data", model).execute(store.claim_conversation())
    assert not image_parts(model.requests[0])
    context = json.loads(model.requests[0]["messages"][1]["content"].split(": ", 1)[1])
    assert context["current_message_images"]["shown"] == 0


@pytest.mark.asyncio
async def test_blind_model_is_told_it_cannot_see_the_photo(store, tmp_path):
    incoming(store, "这个吃了一半，多少热量", [photo_source(tmp_path)])
    model = Model([tool_reply("resolve_conversation", {"action": "reply", "text": "当前模型不能看图。", "promise": ""})], name="deepseek-v4-pro")
    await Runtime(store, tmp_path / "data", model).execute(store.claim_conversation())
    assert not image_parts(model.requests[0])
    context = json.loads(model.requests[0]["messages"][1]["content"].split(": ", 1)[1])
    assert context["current_message_images"] == {"attached": 1, "shown": 0, "note": "the current model cannot see images"}


@pytest.mark.asyncio
async def test_blind_answer_task_is_told_it_cannot_see_the_photo(store, tmp_path):
    incoming(store, "这个吃了一半，多少热量", [photo_source(tmp_path)])
    model = Model([tool_reply("resolve_conversation", ANSWER),
                   tool_reply("report_unable", {"explanation": "当前模型不能看图。", "needs": "nothing_available"}, "u")], name="deepseek-v4-pro")
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    await runtime.execute(store.claim())
    assert not image_parts(model.requests[1])
    assert any("cannot see images" in str(m["content"]) for m in model.requests[1]["messages"])


@pytest.mark.asyncio
async def test_web_entry_pins_the_digest_of_an_uploaded_photo(store, tmp_path):
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer
    from personal_agent.webui import create_app
    app = create_app(store, tmp_path / "data", Model([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        token = (await (await client.get("/api/bootstrap")).json())["token"]
        client.session.headers.update({"X-PA-Token": token})
        form = FormData()
        form.add_field("file", PNG, filename="label.png", content_type="image/png")
        upload = await (await client.post("/api/uploads", data=form)).json()
        await client.post("/api/messages", json={"text": "这个多少热量", "nonce": "p", "mode": "auto", "uploads": [upload["id"]]})
    [item] = store.all("SELECT spec FROM conversation_inputs")
    [source] = json.loads(item["spec"])["sources"]
    assert source["expected_sha256"] == digest(PNG)
