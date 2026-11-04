"""Cross-process tests: the real installed Node Pi Agent runs every case."""
import asyncio
from dataclasses import asdict
import json

import pytest

from personal_agent.contracts import ModelReply
from personal_agent.pi_client import run_pi
from conftest import tool_reply


@pytest.mark.asyncio
async def test_actual_pi_loop_round_trips_model_tool_and_continuation():
    callbacks = []
    count = 0

    async def handle(method, params):
        nonlocal count
        callbacks.append(method)
        if method == "model":
            count += 1
            if count == 1:
                assert params["messages"][0]["content"] == "Follow the authorized request."
                return asdict(tool_reply("echo", {"value": "observed-input"}, "echo-call"))
            assert count == 2
            messages = params["messages"]
            assert messages[-2]["tool_calls"][0]["id"] == "echo-call"
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "echo-call"
            assert json.loads(messages[-1]["content"]) == {"echo": "observed-input"}
            return asdict(ModelReply({"role": "assistant", "content": "Result ready."}, finish_reason="stop"))
        if method == "tool":
            assert params["batch_size"] == 1
            assert params["call"]["function"]["name"] == "echo"
            value = json.loads(params["call"]["function"]["arguments"])["value"]
            return {"result": {"echo": value}, "is_error": False, "terminate": False}
        if method == "turn_end":
            return {"action": "end"} if count == 2 else {}
        raise AssertionError(f"Unexpected Pi callback: {method}")

    schema = {"type": "function", "function": {"name": "echo", "description": "Echo a value",
              "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                             "required": ["value"], "additionalProperties": False}}}
    await asyncio.wait_for(run_pi([
        {"role": "system", "content": "Follow the authorized request."},
        {"role": "user", "content": "Echo the input once."},
    ], [schema], "offline-transport-test", handle), timeout=10)
    assert callbacks == ["model", "tool", "turn_end", "model", "turn_end"]


@pytest.mark.asyncio
async def test_actual_pi_child_is_reaped_after_host_cancellation_and_receives_no_api_key(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    children = []
    monkeypatch.setenv("DASHSCOPE_API_KEY", "OFFLINE_TEST_SENTINEL_NOT_A_KEY")

    async def observe_spawn(*args, **kwargs):
        assert "DASHSCOPE_API_KEY" not in kwargs["env"]
        child = await real_spawn(*args, **kwargs)
        children.append(child)
        return child

    async def cancel_when_model_requested(method, params):
        assert method == "model"
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_pi([
            {"role": "user", "content": "Offline cancellation probe"},
        ], [], "offline-transport-test", cancel_when_model_requested), timeout=10)
    assert len(children) == 1
    assert children[0].returncode is not None
