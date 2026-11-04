from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "data" / "state.sqlite")
    yield database
    database.close()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "policy.txt"
    path.write_text("Current policy: the release requires a human review.\n", encoding="utf-8")
    return {"id": "policy", "kind": "file", "location": str(path.resolve()), "required": True}


@pytest.fixture
def spec(source):
    return TaskSpec("Summarize the release policy in a cited report.", [source], ["human review"])


def tool_reply(name, arguments, call_id="call_1"):
    return ModelReply({"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
    ]}, input_tokens=30, output_tokens=10, model="scripted-test-model")


def submit_written_artifact(messages, tools):
    receipt = next(json.loads(m["content"]) for m in reversed(messages)
                   if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))
    return tool_reply("submit_result", {"artifact_ref": receipt["artifact_ref"],
                                       "summary": "Report ready.", "limitations": []}, "submit")


class ScriptedModel:
    """A controllable protocol peer; never pretends to validate model quality."""

    def __init__(self, steps):
        self.steps = deque(steps)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        if not self.steps:
            raise AssertionError("unexpected additional model call")
        step = self.steps.popleft()
        if isinstance(step, BaseException):
            raise step
        return step(messages, tools) if callable(step) else step


@pytest.fixture
def successful_model():
    return ScriptedModel([
        tool_reply("read_source", {"source_id": "policy"}, "read"),
        tool_reply("write_artifact", {"name": "report.md", "content": "The release requires human review. [policy]"}, "write"),
        submit_written_artifact,
    ])
