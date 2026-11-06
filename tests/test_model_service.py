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


@pytest.mark.parametrize("names,forced", [
    (["resolve_conversation"], True),
    (["read_source"], False),
    (["read_source", "write_artifact"], False),
    (["resolve_conversation", "read_source"], False),
    ([], False),
])
async def test_only_single_conversation_protocol_tool_is_forced_on_actual_http_payload(monkeypatch, names, forced):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unit-test-only")
    model = QwenModel(ModelConfig())
    await model.close()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "正常业务回复"}
        if forced:
            message.update(content=None, tool_calls=[{"id": "intake-1", "type": "function", "function": {"name": "resolve_conversation", "arguments": '{"action":"new_task"}'}}])
        return httpx.Response(200, json={"id": "choice-test", "object": "chat.completion", "created": 1,
            "model": "qwen3.7-plus", "choices": [{"index": 0, "finish_reason": "tool_calls" if forced else "stop", "message": message}]})

    model.client = AsyncOpenAI(api_key="unit-test-only", base_url=model.config.base_url,
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in names]
    try:
        result = await model.complete([{"role": "user", "content": "帮我回顾这件事"}], tools)
    finally:
        await model.close()
    assert len(requests) == 1
    if forced:
        assert requests[0]["tool_choice"] == {"type": "function", "function": {"name": "resolve_conversation"}}
        assert result.message["tool_calls"][0]["function"]["name"] == "resolve_conversation"
    else:
        assert "tool_choice" not in requests[0]
    assert requests[0].get("tools", []) == tools


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


@pytest.mark.parametrize("base", ["http://127.0.0.1:3425/v1", "http://localhost:3425/v1/"])
def test_local_gateway_endpoint_is_accepted(base):
    config = ModelConfig(name="newapi/claude-opus-5-5", base_url=base, effort="medium", chat_effort="low")
    assert config.gateway


@pytest.mark.parametrize("base", ["http://10.0.0.5:3425/v1", "http://127.0.0.1/v1", "http://127.0.0.1:3425/other",
                                  "http://user:pw@127.0.0.1:3425/v1", "https://ai-gw.example.com/v1"])
def test_non_loopback_or_odd_gateway_endpoint_is_rejected(base):
    with pytest.raises(ValueError):
        ModelConfig(base_url=base)


def test_unknown_effort_is_rejected():
    with pytest.raises(ValueError):
        ModelConfig(base_url="http://127.0.0.1:3425/v1", effort="turbo")


async def test_gateway_sends_role_effort_and_never_forces_tool_choice(monkeypatch):
    monkeypatch.setenv("PA_GATEWAY_TEST_KEY", "unit-test-only")
    config = ModelConfig(name="newapi/claude-opus-5-5", base_url="http://127.0.0.1:3425/v1",
                         api_key_env="PA_GATEWAY_TEST_KEY", effort="medium", chat_effort="low")
    model = QwenModel(config)
    await model.close()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": "claude-opus-5-5",
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    model.client = AsyncOpenAI(api_key="unit-test-only", base_url=config.base_url, max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    intake = [{"type": "function", "function": {"name": "resolve_conversation", "parameters": {"type": "object"}}}]
    work = [{"type": "function", "function": {"name": "read_source", "parameters": {"type": "object"}}}]
    try:
        await model.complete([{"role": "user", "content": "hi"}], intake)
        await model.complete([{"role": "user", "content": "work"}], work)
    finally:
        await model.close()
    assert requests[0]["reasoning_effort"] == "low" and "tool_choice" not in requests[0]
    assert requests[1]["reasoning_effort"] == "medium"
    assert "enable_thinking" not in requests[0] and "enable_thinking" not in requests[1]
    assert model.metadata["provider"] == "local_gateway"


async def test_official_qwen_keeps_forced_intake_and_no_effort(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unit-test-only")
    model = QwenModel(ModelConfig(effort="high"))
    await model.close()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": "q",
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    model.client = AsyncOpenAI(api_key="unit-test-only", base_url=model.config.base_url, max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        await model.complete([{"role": "user", "content": "hi"}],
                             [{"type": "function", "function": {"name": "resolve_conversation", "parameters": {"type": "object"}}}])
    finally:
        await model.close()
    assert requests[0]["tool_choice"]["function"]["name"] == "resolve_conversation"
    assert "reasoning_effort" not in requests[0]


@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://api.deepseek.com/v1", "https://api.deepseek.com/"])
def test_deepseek_official_endpoint_is_a_registered_provider(base):
    config = ModelConfig(name="deepseek-flash", base_url=base)
    assert config.provider == "deepseek_official" and not config.gateway


@pytest.mark.parametrize("base", ["http://api.deepseek.com", "https://api.deepseek.com.evil.example",
                                  "https://api.deepseek.com/other", "https://api.deepseek.com:8443"])
def test_deepseek_lookalikes_are_rejected(base):
    with pytest.raises(ValueError):
        ModelConfig(name="deepseek-flash", base_url=base)


def test_vision_is_a_verified_model_property():
    assert ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com").vision
    assert not ModelConfig(name="deepseek-v4-pro", base_url="https://api.deepseek.com").vision
    assert not ModelConfig().vision


async def _deepseek_requests(monkeypatch, config, tools):
    monkeypatch.setenv("PA_DEEPSEEK_TEST_KEY", "unit-test-only")
    model = QwenModel(config)
    await model.close()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": config.name,
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok", "reasoning_content": "private"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    model.client = AsyncOpenAI(api_key="unit-test-only", base_url=config.base_url, max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        replies = [await model.complete([{"role": "user", "content": "hi"}], t) for t in tools]
    finally:
        await model.close()
    assert all("reasoning_content" not in r.message for r in replies)
    assert model.metadata["provider"] == "deepseek_official"
    return requests


INTAKE = [{"type": "function", "function": {"name": "resolve_conversation", "parameters": {"type": "object"}}}]
WORK = [{"type": "function", "function": {"name": "read_source", "parameters": {"type": "object"}}}]


async def test_deepseek_without_effort_turns_thinking_off_and_forces_intake(monkeypatch):
    config = ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com", api_key_env="PA_DEEPSEEK_TEST_KEY")
    intake, work = await _deepseek_requests(monkeypatch, config, [INTAKE, WORK])
    assert intake["thinking"] == {"type": "disabled"} and intake["tool_choice"]["function"]["name"] == "resolve_conversation"
    assert work["thinking"] == {"type": "disabled"} and "tool_choice" not in work
    assert "enable_thinking" not in intake and "reasoning_effort" not in work


async def test_deepseek_thinking_never_sends_a_forced_choice(monkeypatch):
    # DeepSeek returns 400 for a forced tool choice while thinking.
    config = ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com", api_key_env="PA_DEEPSEEK_TEST_KEY",
                         effort="high", chat_effort="low")
    intake, work = await _deepseek_requests(monkeypatch, config, [INTAKE, WORK])
    assert intake["thinking"] == {"type": "enabled"} and intake["reasoning_effort"] == "low" and "tool_choice" not in intake
    assert work["reasoning_effort"] == "high"
