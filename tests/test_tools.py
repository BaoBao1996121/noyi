from pathlib import Path

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.tools import MAX_SOURCE_BYTES, PAGE_CHARS, Toolset, validate_arguments, verify


def recorded(tool, result, arguments=None, status="succeeded"):
    return {"tool": tool, "status": status, "result": result, "arguments": arguments or {}}


def setup_tools(store, tmp_path, spec):
    task_id = store.submit(spec, nonce="tools")["task_id"]
    return Toolset(tmp_path / "data", store.task(task_id))


def proposal(receipt, limitations=None):
    return {"artifact_ref": receipt["artifact_ref"], "summary": "A report", "limitations": limitations or []}


@pytest.mark.asyncio
async def test_coverage_requires_every_page_and_source_version(store, tmp_path, source):
    Path(source["location"]).write_text("A" * (PAGE_CHARS + 100), encoding="utf-8")
    tools = setup_tools(store, tmp_path, TaskSpec("Read full source", [source]))
    first = await tools.read_source("policy")
    last = await tools.read_source("policy", PAGE_CHARS)
    receipt = tools.write_artifact("write1", "report.md", "Full source summary. [policy]")
    operations = [recorded("write_artifact", receipt), recorded("read_source", first)]
    assert not verify(tools, operations, proposal(receipt))["passed"]
    operations.append(recorded("read_source", last))
    assert verify(tools, operations, proposal(receipt))["passed"]
    operations[-1] = recorded("read_source", {**last, "sha256": "different-version"})
    assert not verify(tools, operations, proposal(receipt))["passed"]


@pytest.mark.asyncio
async def test_duplicate_page_cannot_fill_missing_coverage(store, tmp_path, source):
    Path(source["location"]).write_text("B" * (PAGE_CHARS + 1), encoding="utf-8")
    tools = setup_tools(store, tmp_path, TaskSpec("Read full source", [source]))
    page = await tools.read_source("policy")
    receipt = tools.write_artifact("write1", "report.md", "Summary. [policy]")
    operations = [recorded("write_artifact", receipt), recorded("read_source", page), recorded("read_source", page)]
    assert not verify(tools, operations, proposal(receipt))["passed"]


@pytest.mark.asyncio
async def test_required_citation_and_text_are_verified(store, tmp_path, spec):
    tools = setup_tools(store, tmp_path, spec)
    read = await tools.read_source("policy")
    receipt = tools.write_artifact("write1", "report.md", "A release summary.")
    outcome = verify(tools, [recorded("read_source", read), recorded("write_artifact", receipt)], proposal(receipt))
    assert not outcome["passed"]
    assert any("citation" in error for error in outcome["errors"])
    assert any("required text" in error for error in outcome["errors"])


def test_written_file_without_receipt_and_tampering_do_not_pass(store, tmp_path):
    tools = setup_tools(store, tmp_path, TaskSpec("Write a report"))
    receipt = tools.write_artifact("write1", "report.md", "Original result")
    assert not verify(tools, [], proposal(receipt))["passed"]
    tools.artifact_path(receipt["artifact_ref"]).write_text("Changed result", encoding="utf-8")
    outcome = verify(tools, [recorded("write_artifact", receipt)], proposal(receipt))
    assert not outcome["passed"]
    assert "artifact changed since write receipt" in outcome["errors"]


@pytest.mark.asyncio
async def test_unavailable_optional_source_requires_attempt_and_explicit_disclosure(store, tmp_path, source):
    source["required"] = False
    source["location"] = str((tmp_path / "missing.txt").resolve())
    tools = setup_tools(store, tmp_path, TaskSpec("Use optional material", [source]))
    receipt = tools.write_artifact("write1", "report.md", "UNAVAILABLE [policy]: the result does not rely on it.")
    writes = [recorded("write_artifact", receipt)]
    assert not verify(tools, writes, proposal(receipt, ["policy"]))["passed"]
    with pytest.raises(ValueError) as unavailable:
        await tools.read_source("policy")
    operations = writes + [recorded("read_source", {"error": type(unavailable.value).__name__}, {"source_id": "policy"}, "failed")]
    assert not verify(tools, operations, proposal(receipt))["passed"]
    assert verify(tools, operations, proposal(receipt, ["policy"]))["passed"]


@pytest.mark.asyncio
async def test_source_id_does_not_allow_arbitrary_paths(store, tmp_path, spec):
    tools = setup_tools(store, tmp_path, spec)
    for unauthorized in ["../../secret.txt", "file:///secret.txt", "https://example.test", "another-source"]:
        with pytest.raises(ValueError, match="authorized"):
            await tools.read_source(unauthorized)


@pytest.mark.parametrize("name", ["../escape.md", "C:\\escape.md", "folder/report.md", "NUL.txt", "CON", "LPT1.md"])
def test_artifact_names_cannot_escape_or_use_windows_devices(store, tmp_path, name):
    tools = setup_tools(store, tmp_path, TaskSpec("Write a report"))
    with pytest.raises(ValueError):
        tools.write_artifact("write1", name, "A report")
    assert not (tmp_path / "escape.md").exists()


@pytest.mark.parametrize("name,args", [
    ("shell", {"command": "anything"}),
    ("read_source", {"source_id": "policy", "path": "elsewhere"}),
    ("read_source", {"source_id": "policy", "offset": True}),
    ("read_source", {"source_id": "policy", "offset": -1}),
    ("write_artifact", {"name": "report.md"}),
    ("submit_result", {"artifact_ref": "a", "summary": "x", "limitations": [1]}),
])
def test_tool_dispatch_rejects_unknown_capabilities_and_malformed_arguments(name, args):
    with pytest.raises(ValueError):
        validate_arguments(name, args)


@pytest.mark.asyncio
async def test_oversized_source_is_not_claimed_as_truncated_success(store, tmp_path, source):
    Path(source["location"]).write_bytes(b"X" * (MAX_SOURCE_BYTES + 1))
    tools = setup_tools(store, tmp_path, TaskSpec("Read a source", [source]))
    with pytest.raises(ValueError, match="too large"):
        await tools.read_source("policy")
    assert not list((tools.root / "sources").glob("*.txt"))


@pytest.mark.asyncio
async def test_source_snapshot_is_pinned_between_pages(store, tmp_path, source):
    source_path = Path(source["location"])
    source_path.write_text("A" * PAGE_CHARS + "original tail", encoding="utf-8")
    tools = setup_tools(store, tmp_path, TaskSpec("Read source", [source]))
    first = await tools.read_source("policy")
    source_path.write_text("Replaced content", encoding="utf-8")
    last = await tools.read_source("policy", PAGE_CHARS)
    assert first["sha256"] == last["sha256"]
    assert last["text"] == "original tail"
