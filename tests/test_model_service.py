import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI, RateLimitError

from personal_agent.cli import parser
from personal_agent.model import ModelConfig, QwenModel, WORKSPACE_REGIONS
from personal_agent.service import FileChannel, Service, ServiceLock
from personal_agent.store import Store


@pytest.mark.parametrize("base", [
    "https://maas.qianwenaiapi.com/compatible-mode/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/",
    "https://dashscope-us.aliyuncs.com:443/compatible-mode/v1",
    *[f"https://workspace-123.{region}.maas.aliyuncs.com/compatible-mode/v1" for region in sorted(WORKSPACE_REGIONS)],
])
def test_official_endpoint_shapes(base):
    assert ModelConfig(base_url=base).base_url == base


@pytest.mark.parametrize("base", [
    "http://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope.aliyuncs.com.attacker.example/compatible-mode/v1",
    "https://evil.maas.aliyuncs.com/compatible-mode/v1",
    "https://workspace.unknown-region.maas.aliyuncs.com/compatible-mode/v1",
    "https://a.b.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://username:password@dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
    "https://dashscope.aliyuncs.com:secret/compatible-mode/v1",
    "https://dashscope.aliyuncs.com/other-path",
    "https://dashscope.aliyuncs.com/compatible-mode/v1?key=secret",
    "https://dashscope.aliyuncs.com/compatible-mode/v1#secret",
])
def test_endpoint_rejected_before_credentials_or_network(base):
    with pytest.raises(ValueError) as exc:
        ModelConfig(base_url=base)
    assert "secret" not in str(exc.value)


def test_example_and_defaults_agree_and_env_override_is_validated(monkeypatch):
    monkeypatch.delenv("PA_MODEL", raising=False)
    monkeypatch.delenv("PA_BASE_URL", raising=False)
    sample = Path(__file__).resolve().parents[1] / "pa.example.toml"
    assert ModelConfig.load(sample) == ModelConfig()
    monkeypatch.setenv("PA_MODEL", "qwen3.7-plus-test")
    assert ModelConfig.load(sample).name == "qwen3.7-plus-test"
    monkeypatch.setenv("PA_BASE_URL", "https://attacker.example/compatible-mode/v1")
    with pytest.raises(ValueError):
        ModelConfig.load(sample)


@pytest.mark.parametrize("values", [
    {"timeout_seconds": float("nan")}, {"timeout_seconds": True},
    {"max_output_tokens": 0}, {"max_output_tokens": True},
    {"enable_thinking": "false"}, {"api_key_env": "key=value"},
])
def test_invalid_configuration_is_rejected(values):
    with pytest.raises(ValueError):
        ModelConfig(**values)


@pytest.mark.parametrize("finish_reason", ["tool_calls", "length"])
async def test_official_model_serializes_tools_and_keeps_public_metadata(monkeypatch, finish_reason):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unit-test-only")
    model = QwenModel(ModelConfig())
    assert model.client.max_retries == 0
    assert model.client._client.follow_redirects is False
    await model.close()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"x-request-id": "request-123"}, json={
            "id": "completion-123", "object": "chat.completion", "created": 1,
            "model": "qwen3.7-plus-returned",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": {
                "role": "assistant", "content": None, "reasoning_content": "private reasoning",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "read_source", "arguments": '{"source_id":"source1"}'}}]}}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 8, "total_tokens": 25},
        })

    model.client = AsyncOpenAI(api_key="unit-test-only", base_url=model.config.base_url,
                              max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    tools = [{"type": "function", "function": {"name": "read_source", "parameters": {"type": "object"}}}]
    try:
        result = await model.complete([{"role": "user", "content": "read"}], tools)
    finally:
        await model.close()
    assert len(requests) == 1
    assert requests[0]["model"] == "qwen3.7-plus"
    assert requests[0]["enable_thinking"] is False
    assert requests[0]["tools"] == tools
    assert result.message["tool_calls"][0]["function"]["name"] == "read_source"
    assert "reasoning_content" not in result.message
    assert result.request_id == "request-123"
    assert result.finish_reason == finish_reason
    assert (result.input_tokens, result.output_tokens) == (17, 8)
    assert "unit-test-only" not in json.dumps(model.metadata)


async def test_rate_limit_has_no_hidden_sdk_retry(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unit-test-only")
    model = QwenModel(ModelConfig())
    await model.close()
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(429, json={"error": {"message": "rate limit", "type": "rate_limit"}})

    model.client = AsyncOpenAI(api_key="unit-test-only", base_url=model.config.base_url,
                              max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        with pytest.raises(RateLimitError):
            await model.complete([{"role": "user", "content": "read"}], [])
    finally:
        await model.close()
    assert count == 1


async def test_file_receipt_replay_and_payload_mismatch(tmp_path):
    channel = FileChannel(tmp_path)
    delivery = {"id": "delivery-1", "task_id": "task-1", "room_id": "local", "content": "result", "artifact": None}
    first = await channel.send(delivery)
    assert await channel.send(delivery) == first
    assert first["accepted"] is True and first["human_read"] == "unknown"
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 1
    with pytest.raises(ValueError, match="mismatch"):
        await channel.send({**delivery, "content": "replacement"})
    stored = json.loads((tmp_path / "inbox" / "delivery-1.json").read_text(encoding="utf-8"))
    assert stored["content"] == "result"
    with pytest.raises(ValueError):
        await channel.send({**delivery, "id": "../outside"})
    assert not (tmp_path / "outside.json").exists()


def test_service_lock_excludes_second_owner_and_releases(tmp_path):
    with ServiceLock(tmp_path):
        with pytest.raises(RuntimeError, match="another pa serve"):
            with ServiceLock(tmp_path):
                pytest.fail("second process owner was admitted")
    with ServiceLock(tmp_path):
        pass


@pytest.mark.parametrize("receipt", [None, {}, {"id": "delivery-1", "accepted": False},
                                     {"id": "other-delivery", "accepted": True}])
async def test_non_acknowledgement_does_not_mark_sent(tmp_path, receipt):
    store = Store(tmp_path / "state.sqlite")
    store.db.execute("INSERT INTO deliveries(id,run_id,room_id,content) VALUES(?,?,?,?)",
                     ("delivery-1", "run-1", "local", "result"))

    class BadChannel:
        async def send(self, delivery):
            return receipt

    try:
        await Service(store, tmp_path, None, channel=BadChannel()).deliver()
        delivery = store.one("SELECT * FROM deliveries")
        assert delivery["state"] == "failed"
        assert delivery["receipt"] is None
    finally:
        store.close()


def test_resume_cli_exposes_explicit_budget_extension():
    args = parser().parse_args(["retry", "task-1", "--extra-calls", "3", "--extra-tools", "5", "--extra-seconds", "45"])
    assert args.command == "retry"
    assert (args.task_id, args.extra_calls, args.extra_tools, args.extra_seconds) == ("task-1", 3, 5, 45)
