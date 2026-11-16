"""First-reply latency: bounded model calls, a pre-started engine, visible intake state."""
import asyncio
from dataclasses import asdict
import json
import time
from types import SimpleNamespace

import pytest

from personal_agent import pi_client, runtime as runtime_module
from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.ports import PiEngine
from personal_agent.runtime import Runtime
from personal_agent.service import Service
from conftest import tool_reply


def incoming(store, text, nonce, room="local"):
    return store.accept_conversation(TaskSpec(text, [], max_calls=40, max_tools=100, max_seconds=900), text=text, nonce=nonce, room=room)


class HangingModel:
    """A provider that accepts the request and never answers."""
    config = SimpleNamespace(name="hanging-test-model", chat_timeout_seconds=1, timeout_seconds=90)

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_hung_intake_call_fails_within_its_deadline_and_tells_the_user(store, tmp_path, monkeypatch):
    # Before: a stalled request stayed open until the 90 s intake limit, then
    # failed with a generic line. Now each call has its own short deadline,
    # is retried as a transient fault, and the user hears what happened.
    monkeypatch.setattr(runtime_module, "RETRY_DELAYS", (0, 0))
    incoming(store, "你好", "hang")
    model = HangingModel()
    started = time.monotonic()
    await asyncio.wait_for(Runtime(store, tmp_path / "data", model).execute(store.claim_conversation()), timeout=30)
    assert time.monotonic() - started < 15
    assert model.calls == 3
    requests = store.all("SELECT state,ended-created AS seconds FROM model_requests ORDER BY sequence")
    assert [r["state"] for r in requests] == ["unknown"] * 3
    assert all(r["seconds"] < 3 for r in requests)
    assert store.one("SELECT state FROM conversation_inputs")["state"] == "failed"
    assert "模型服务" in store.one("SELECT content FROM deliveries")["content"]
    assert [json.loads(s["data"]).get("deadline_seconds") for s in store.all("SELECT data FROM steps WHERE kind='model_retry'")] == [1, 1]


@pytest.mark.asyncio
async def test_prestarted_engine_gives_each_run_its_own_child_and_reaps_the_spare(monkeypatch):
    real_spawn = pi_client.spawn_pi
    created = []

    def counting_spawn():
        task = real_spawn()
        created.append(task)
        return task

    monkeypatch.setattr(pi_client, "spawn_pi", counting_spawn)

    async def handle(method, params):
        if method == "model":
            return asdict(ModelReply({"role": "assistant", "content": "Done."}, finish_reason="stop"))
        return {"action": "end"}

    engine = PiEngine()
    engine.prepare()
    assert len(created) == 1  # loaded before any message arrives
    await asyncio.wait_for(engine.run([{"role": "user", "content": "one"}], [], "offline-test", handle), timeout=15)
    await asyncio.wait_for(engine.run([{"role": "user", "content": "two"}], [], "offline-test", handle), timeout=15)
    assert len(created) == 3  # one per run plus the spare waiting for the next
    processes = [await task for task in created]
    assert len({p.pid for p in processes}) == 3
    assert all(p.returncode is not None for p in processes[:2])
    await engine.close()
    assert all(p.returncode is not None for p in processes)


@pytest.mark.asyncio
async def test_engine_without_prepare_starts_no_spare(monkeypatch):
    created = []
    real_spawn = pi_client.spawn_pi
    monkeypatch.setattr(pi_client, "spawn_pi", lambda: created.append(1) or real_spawn())

    async def handle(method, params):
        if method == "model":
            return asdict(ModelReply({"role": "assistant", "content": "Done."}, finish_reason="stop"))
        return {"action": "end"}

    await asyncio.wait_for(PiEngine().run([{"role": "user", "content": "x"}], [], "offline-test", handle), timeout=15)
    assert len(created) == 1


@pytest.mark.asyncio
async def test_intake_state_goes_received_reading_answered(store, tmp_path):
    first = incoming(store, "第一句", "a")["message_id"]
    assert store.intake_states("local") == {first: "received"}
    run = store.claim_conversation()
    assert store.intake_states("local") == {first: "reading"}
    second = incoming(store, "第二句", "b")["message_id"]
    assert store.intake_states("local") == {first: "reading", second: "received"}
    # The second message makes the first stale: both are read together.
    store.ack_control(run, "scope_changed")
    run = store.claim_conversation()
    assert store.intake_states("local") == {first: "reading", second: "reading"}
    store.conversation_context(run)
    store.apply_conversation(run, {"action": "reply", "text": "好的", "promise": ""})
    assert store.intake_states("local") == {first: "answered", second: "answered"}


@pytest.mark.asyncio
async def test_new_message_wakes_an_idle_scheduler(store, tmp_path):
    service = Service(store, tmp_path / "data", None)
    started = time.monotonic()
    asyncio.get_running_loop().call_later(0.05, service.wake)
    await service.idle(5)
    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_model_client_is_built_before_the_first_message(tmp_path, monkeypatch):
    from personal_agent.model import ModelConfig, QwenModel
    from personal_agent.webui import LocalModel
    monkeypatch.setenv("NOYI_TEST_KEY", "offline-test-key")
    loaded = []
    real = QwenModel.preload
    monkeypatch.setattr(QwenModel, "preload", staticmethod(lambda: loaded.append(1) or real()))
    async def no_network(self):
        pass
    monkeypatch.setattr(QwenModel, "warm_up", no_network)
    model = LocalModel(tmp_path, ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com", api_key_env="NOYI_TEST_KEY"))
    await asyncio.gather(model.prepare(), model.prepare())
    assert model.client is not None and loaded == [1]
    await model.close()
    assert model.client is None


@pytest.mark.asyncio
async def test_page_state_waits_for_a_change_and_returns_as_soon_as_it_happens(store, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from personal_agent.webui import create_app
    from conftest import ScriptedModel
    app = create_app(store, tmp_path / "data", ScriptedModel([]), run_service=False)
    async with TestClient(TestServer(app)) as client:
        first = await (await client.get("/api/state")).json()
        # Nothing changes: the request is held, not answered at once.
        started = time.monotonic()
        waiting = asyncio.ensure_future(client.get("/api/state", params={"since": first["version"]}))
        await asyncio.sleep(0.4)
        assert not waiting.done()
        incoming(store, "你好", "wait")
        response = await asyncio.wait_for(waiting, timeout=3)
        changed = await response.json()
        assert time.monotonic() - started < 1.5
        assert changed["version"] != first["version"]
        assert list(changed["intake"].values()) == ["received"]
        # Reading the state is not a change: a page cannot wake itself up.
        assert (await (await client.get("/api/state")).json())["version"] == changed["version"]


@pytest.mark.asyncio
async def test_model_connection_is_opened_before_the_first_message(tmp_path, monkeypatch):
    from personal_agent.model import ModelConfig, QwenModel
    from personal_agent.webui import LocalModel
    monkeypatch.setenv("NOYI_TEST_KEY", "offline-test-key")
    opened = []

    async def fake_warm_up(self):
        opened.append(1)

    monkeypatch.setattr(QwenModel, "warm_up", fake_warm_up)
    model = LocalModel(tmp_path, ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com", api_key_env="NOYI_TEST_KEY"))
    await model.prepare()
    assert opened == [1]
    await model.close()


def _count_queries(store, action):
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        action()
    finally:
        store.db.set_trace_callback(None)
    return len(statements)


def _fill_history(store, n, prefix):
    """Answered turns, as accumulated by real use."""
    for i in range(n):
        incoming(store, f"历史消息 {i}", f"{prefix}{i}")
        run = store.claim_conversation()
        store.conversation_context(run)
        store.apply_conversation(run, {"action": "reply", "text": f"回复 {i}", "promise": ""})


def test_intake_cost_does_not_grow_with_history(store):
    # The real ledger held 17 conversational turns when this was measured;
    # 60 is more than three times that.
    _fill_history(store, 20, "small")
    small_states = _count_queries(store, lambda: store.intake_states("local"))
    incoming(store, "现在这句", "now-small")
    small_claim = _count_queries(store, lambda: store.conversation_context(store.claim_conversation()))
    run = store.one("SELECT * FROM runs WHERE phase='running'")
    store.apply_conversation(run, {"action": "reply", "text": "好", "promise": ""})

    _fill_history(store, 40, "large")
    assert store.one("SELECT COUNT(*) n FROM conversation_inputs")["n"] == 61
    large_states = _count_queries(store, lambda: store.intake_states("local"))
    incoming(store, "现在这句", "now-large")
    started = time.perf_counter()
    large_claim = _count_queries(store, lambda: store.conversation_context(store.claim_conversation()))
    elapsed = time.perf_counter() - started
    assert large_states == small_states == 1
    assert large_claim == small_claim
    assert elapsed < 0.5
