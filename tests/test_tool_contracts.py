from dataclasses import asdict
from pathlib import Path

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.tools import SourceUnavailable, Toolset, verify


def make_tools(tmp_path, sources=None):
    return Toolset(tmp_path / "data", {"id": "task1", "spec": asdict(TaskSpec("Produce a report", sources or []))})


@pytest.mark.parametrize("changes", [
    {"objective": None}, {"max_calls": True}, {"max_tools": 1.5},
    {"max_seconds": float("nan")}, {"max_seconds": True},
    {"max_seconds": 10 ** 1000}, {"sources": [{"kind": [], "id": "x", "location": "x"}]},
    {"sources": [None]}, {"sources": "not a list"}, {"required_text": "not a list"},
])
def test_malformed_task_values_rejected_before_persistence(changes):
    with pytest.raises(ValueError):
        TaskSpec(**{"objective": "Do work", **changes}).validate()


@pytest.mark.parametrize("kind,location", [
    ("file", "relative.txt"), ("http", "file:///etc/passwd"),
    ("http", "https://user:password@example.test/secret"),
    ("http", "https:///missing-host"), ("http", "https://example.test/#fragment"),
    ("http", "https://example.test:invalid/"),
])
def test_source_scope_must_be_explicit_and_parseable(kind, location):
    with pytest.raises(ValueError):
        TaskSpec("Read source", [{"id": "source", "kind": kind, "location": location}]).validate()


def test_write_replay_is_identical_and_cannot_overwrite(tmp_path):
    tools = make_tools(tmp_path)
    first = tools.write_artifact("op1", "report.md", "original")
    assert tools.write_artifact("op1", "report.md", "original") == first
    with pytest.raises(ValueError, match="conflicts"):
        tools.write_artifact("op1", "report.md", "replacement")
    with pytest.raises(ValueError, match="another artifact"):
        tools.write_artifact("op1", "other.md", "original")
    assert tools.artifact_path(first["artifact_ref"]).read_text() == "original"


def test_recover_write_reads_existing_output_without_creating_it(tmp_path):
    tools = make_tools(tmp_path)
    args = {"name": "report.md", "content": "completed before receipt"}
    assert tools.recover_write("op1", args) is None
    assert not (tools.root / "artifacts" / "op1").exists()
    receipt = tools.write_artifact("op1", **args)
    recovered = Toolset(tmp_path / "data", tools.task).recover_write("op1", args)
    assert recovered == receipt
    assert len(list((tools.root / "artifacts").glob("*/*.md"))) == 1


def test_recover_does_not_promote_half_written_temporary_file(tmp_path):
    tools = make_tools(tmp_path)
    folder = tools.root / "artifacts" / "op1"
    folder.mkdir(parents=True)
    (folder / "report.md.tmp").write_text("partial")
    assert tools.recover_write("op1", {"name": "report.md", "content": "complete"}) is None


@pytest.mark.parametrize("op_id", ["../escape", "C:\\escape", "trailing."])
def test_operation_identity_cannot_select_a_directory(tmp_path, op_id):
    tools = make_tools(tmp_path)
    with pytest.raises(ValueError):
        tools.write_artifact(op_id, "report.md", "text")


@pytest.mark.asyncio
async def test_bad_offset_does_not_make_optional_source_unavailable(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("actual source")
    tools = make_tools(tmp_path, [{"id": "source", "kind": "file", "location": str(source.resolve()), "required": False}])
    with pytest.raises(ValueError) as caught:
        await tools.read_source("source", 999)
    assert not isinstance(caught.value, SourceUnavailable)
    receipt = tools.write_artifact("op1", "report.md", "[source] could not be read.")
    operations = [
        {"tool": "write_artifact", "status": "succeeded", "result": receipt, "arguments": {}},
        {"tool": "read_source", "status": "failed", "result": {"error": "ValueError"}, "arguments": {"source_id": "source", "offset": 999}},
    ]
    assert not verify(tools, operations, {"artifact_ref": receipt["artifact_ref"], "summary": "Done", "limitations": ["source"]})["passed"]


@pytest.mark.asyncio
async def test_missing_optional_source_can_be_truthfully_disclosed(tmp_path):
    tools = make_tools(tmp_path, [{"id": "missing", "kind": "file", "location": str((tmp_path / "missing.txt").resolve()), "required": False}])
    with pytest.raises(SourceUnavailable):
        await tools.read_source("missing")
    receipt = tools.write_artifact("op1", "report.md", "UNAVAILABLE [missing]: no claims rely on it.")
    operations = [
        {"tool": "write_artifact", "status": "succeeded", "result": receipt, "arguments": {}},
        {"tool": "read_source", "status": "failed", "result": {"error": "SourceUnavailable"}, "arguments": {"source_id": "missing"}},
    ]
    assert verify(tools, operations, {"artifact_ref": receipt["artifact_ref"], "summary": "Done", "limitations": ["missing"]})["passed"]


@pytest.mark.asyncio
async def test_citation_alone_is_not_unavailable_source_disclosure(tmp_path):
    tools = make_tools(tmp_path, [{"id": "missing", "kind": "file", "location": str((tmp_path / "missing.txt").resolve()), "required": False}])
    with pytest.raises(SourceUnavailable):
        await tools.read_source("missing")
    receipt = tools.write_artifact("op1", "report.md", "[missing] confirms approval granted.")
    operations = [
        {"tool": "write_artifact", "status": "succeeded", "result": receipt, "arguments": {}},
        {"tool": "read_source", "status": "failed", "result": {"error": "SourceUnavailable"}, "arguments": {"source_id": "missing"}},
    ]
    result = verify(tools, operations, {"artifact_ref": receipt["artifact_ref"], "summary": "Done", "limitations": ["missing"]})
    assert not result["passed"]
    assert any("UNAVAILABLE [missing]" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_fully_read_source_cannot_be_declared_unavailable(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("actual source")
    tools = make_tools(tmp_path, [{"id": "source", "kind": "file", "location": str(source.resolve()), "required": False}])
    read = await tools.read_source("source")
    receipt = tools.write_artifact("op1", "report.md", "[source] says actual source.")
    operations = [
        {"tool": "write_artifact", "status": "succeeded", "result": receipt, "arguments": {}},
        {"tool": "read_source", "status": "succeeded", "result": read, "arguments": {"source_id": "source"}},
    ]
    assert not verify(tools, operations, {"artifact_ref": receipt["artifact_ref"], "summary": "Done", "limitations": ["source"]})["passed"]


def test_cross_task_artifact_reference_rejected(tmp_path):
    own = make_tools(tmp_path)
    other = Toolset(tmp_path / "data", {"id": "other", "spec": asdict(TaskSpec("Other task"))})
    receipt = other.write_artifact("op1", "report.md", "private to other task")
    with pytest.raises(ValueError):
        own.artifact_path(str(other.artifact_path(receipt["artifact_ref"])))
    with pytest.raises(ValueError):
        own.artifact_path("../other/" + receipt["artifact_ref"])
