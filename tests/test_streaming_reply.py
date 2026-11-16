"""Replies shown while written: display only, never a delivery until the ledger accepts them."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.model import ModelConfig, QwenModel, partial_text
from personal_agent.runtime import Runtime
from conftest import tool_reply


def incoming(store, text, nonce):
    return store.accept_conversation(TaskSpec(text, [], max_calls=40, max_tools=100, max_seconds=900), text=text, nonce=nonce)


@pytest.mark.parametrize("partial,expected", [
    ('{"action": "reply"', None),
    ('{"action": "reply", "text": "', ""),
    ('{"action": "reply", "text": "你好，', "你好，"),
    ('{"action": "reply", "text": "第一行\\n第二', "第一行\n第二"),
    ('{"action": "reply", "text": "引号\\"内', '引号"内'),
    ('{"action": "reply", "text": "半个转义\\', "半个转义"),
    ('{"action": "reply", "text": "\\u4f60\\u59', "你"),
    ('{"action": "reply", "text": "笑\\ud83d\\ude04', "笑😄"),
    ('{"action": "reply", "text": "笑\\ud83d', "笑"),
    ('{"action": "new_task", "receipt": "好，我去整理', "好，我去整理"),
    ('{"action": "reply", "text": "完整", "promise": ""}', "完整"),
])
def test_partial_text_reads_only_what_has_arrived(partial, expected):
    assert partial_text(partial) == expected


def _chunk(arguments=None, *, finish=None, usage=None, call_id=None, name=None):
    part = SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))
    delta = SimpleNamespace(content=None, tool_calls=[part] if arguments is not None or name else None)
    return SimpleNamespace(id="req-1", model="deepseek-flash", usage=usage,
                           choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


@pytest.mark.asyncio
async def test_streamed_intake_assembles_the_same_decision(monkeypatch):
    monkeypatch.setenv("NOYI_TEST_KEY", "offline-test-key")
    model = QwenModel(ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com", api_key_env="NOYI_TEST_KEY"))
    pieces = ['{"action": "re', 'ply", "text": "你', '好，', '在的', '", "promise": ""}']
    chunks = [_chunk("", call_id="call-1", name="resolve_conversation")] + [_chunk(p) for p in pieces]
    chunks += [_chunk(None, finish="tool_calls"), SimpleNamespace(id="req-1", model="deepseek-flash", choices=[], usage=SimpleNamespace(prompt_tokens=50, completion_tokens=9))]
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return FakeStream(chunks)

    monkeypatch.setattr(model.client.chat.completions, "create", create)
    shown = []
    tools = [{"type": "function", "function": {"name": "resolve_conversation", "parameters": {}}}]
    reply = await model.complete([{"role": "user", "content": "在吗"}], tools, on_text=shown.append)
    await model.close()
    assert sent["stream"] is True and sent["tool_choice"]["function"]["name"] == "resolve_conversation"
    assert shown == ["你", "你好，", "你好，在的"]
    call = reply.message["tool_calls"][0]
    assert call["id"] == "call-1" and call["function"]["name"] == "resolve_conversation"
    assert json.loads(call["function"]["arguments"]) == {"action": "reply", "text": "你好，在的", "promise": ""}
    assert (reply.input_tokens, reply.output_tokens, reply.finish_reason) == (50, 9, "tool_calls")


class StreamingModel:
    """Writes each scripted reply's text in pieces before returning it."""
    streams_text = True
    config = SimpleNamespace(name="streaming-test", chat_timeout_seconds=20, timeout_seconds=90)

    def __init__(self, decisions, board_seen):
        self.decisions, self.board_seen = list(decisions), board_seen

    async def complete(self, messages, tools, on_text=None):
        decision = self.decisions.pop(0)
        text = decision.get("text") or decision.get("receipt") or ""
        for i in range(1, len(text) + 1, 3):
            on_text(text[:i])
            self.board_seen.append(text[:i])
        return tool_reply("resolve_conversation", decision)


@pytest.mark.asyncio
async def test_accepted_reply_replaces_its_draft_and_draft_was_never_a_delivery(store, tmp_path):
    seen = []
    mid = incoming(store, "你好", "ok")["message_id"]
    runtime = Runtime(store, tmp_path / "data", StreamingModel([{"action": "reply", "text": "你好，有什么可以帮你？", "promise": ""}], seen))
    states = []
    original = runtime.drafts.write

    def observe(message_id, room, text):
        original(message_id, room, text)
        states.append((runtime.drafts.for_room("local"), store.all("SELECT id FROM deliveries")))

    runtime.drafts.write = observe
    await runtime.execute(store.claim_conversation())
    assert seen and states
    # While shown, no delivery existed: nothing could be marked seen or settle a promise.
    assert all(deliveries == [] and board[mid]["state"] == "writing" for board, deliveries in states)
    assert runtime.drafts.for_room("local") == {}
    assert store.one("SELECT content FROM deliveries")["content"] == "你好，有什么可以帮你？"


@pytest.mark.asyncio
async def test_refused_reply_draft_is_withdrawn_visibly_and_the_corrected_one_is_delivered(store, tmp_path):
    seen = []
    mid = incoming(store, "明天能帮我查下吗", "promise")["message_id"]
    model = StreamingModel([
        {"action": "reply", "text": "好的，我明天帮你查好发你。", "promise": "我明天帮你查好发你"},
        {"action": "reply", "text": "我现在没法替你安排明天的事，你可以直接把要查的内容发我。", "promise": ""},
    ], seen)
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    deliveries = store.all("SELECT content FROM deliveries")
    assert [d["content"] for d in deliveries] == ["我现在没法替你安排明天的事，你可以直接把要查的内容发我。"]
    assert "好的，我明天帮你查好发你。" in seen
    # The refused promise was shown as a draft, then withdrawn; the new reply
    # differs from what was first shown, so the page says it was corrected.
    assert runtime.drafts.for_room("local")[mid]["state"] == "corrected"
    assert store.one("SELECT kind FROM commitment_events WHERE kind='unregistered_refused'")


@pytest.mark.asyncio
async def test_failed_intake_leaves_a_withdrawn_notice_not_a_hanging_draft(store, tmp_path):
    mid = incoming(store, "你好", "fail")["message_id"]

    class Broken(StreamingModel):
        async def complete(self, messages, tools, on_text=None):
            on_text("我正在")
            raise ValueError("provider refused")

    runtime = Runtime(store, tmp_path / "data", Broken([], []))
    await runtime.execute(store.claim_conversation())
    assert runtime.drafts.for_room("local")[mid] == {"text": "", "state": "withdrawn"}
    assert store.one("SELECT state FROM conversation_inputs")["state"] == "failed"


@pytest.mark.asyncio
async def test_page_state_carries_drafts_and_wakes_on_each_new_piece(store, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from personal_agent.webui import CONSOLE, create_app
    from conftest import ScriptedModel
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        mid = incoming(store, "你好", "page")["message_id"]
        first = await (await client.get("/api/state")).json()
        waiting = asyncio.ensure_future(client.get("/api/state", params={"since": first["version"]}))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        app[CONSOLE].service.runtime.drafts.write(mid, "local", "你好")
        body = await (await asyncio.wait_for(waiting, timeout=2)).json()
        assert body["drafts"] == {mid: {"text": "你好", "state": "writing"}}
        assert body["deliveries"] == []
