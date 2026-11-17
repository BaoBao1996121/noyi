"""Read-only tools a model calls in one turn run together; everything else stays in order."""
import asyncio
import json
import time

import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.service import Service
from personal_agent.web_tools import WebConfig, WebTools, web_plugin
from conftest import ScriptedModel, tool_reply

HTML = b"<html><head><title>Notes</title></head><body><p>Released 2026-11-01.</p></body></html>"


def batch(*calls):
    return ModelReply({"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for cid, name, args in calls]}, input_tokens=30, output_tokens=10, model="scripted-test-model")


class SlowFetcher:
    def __init__(self, delay=0.3):
        self.delay, self.urls, self.spans = delay, [], []

    async def fetch(self, url):
        start = time.monotonic()
        self.urls.append(url)
        await asyncio.sleep(self.delay)
        self.spans.append((start, time.monotonic()))
        return url, "text/html; charset=utf-8", HTML.replace(b"2026-11-01", url[-1:].encode())


class SlowSearch:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        await asyncio.sleep(0.2)
        return {"urls": ["https://example.org/a"], "summary": "", "billed_searches": 1}


def web_tools(monkeypatch, fetcher=None, limit=30):
    monkeypatch.setenv("WEB_TEST_KEY", "unit-test-only")
    return WebTools(WebConfig(api_key_env="WEB_TEST_KEY", task_search_limit=limit, daily_search_limit=100),
                    backend=SlowSearch(), fetcher=fetcher or SlowFetcher())


def overlapped(spans):
    (a0, a1), (b0, b1) = sorted(spans)
    return b0 < a1


@pytest.mark.asyncio
async def test_two_page_reads_in_one_turn_run_at_the_same_time(store, tmp_path, monkeypatch):
    fetcher = SlowFetcher()
    web = web_tools(monkeypatch, fetcher)
    tid = store.submit(TaskSpec("读两页", max_calls=3), nonce="p")["task_id"]
    model = ScriptedModel([batch(("a", "fetch_page", {"url": "https://example.org/1"}),
                                 ("b", "fetch_page", {"url": "https://example.org/2"})),
                           ModelReply({"role": "assistant", "content": "stop"})])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    ops = [o for o in store.operations(tid) if o["tool"] == "fetch_page"]
    assert [o["status"] for o in ops] == ["succeeded", "succeeded"]
    assert overlapped(fetcher.spans)
    # Results return to the model in the order it asked, each with its own page.
    tool_messages = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert [json.loads(m["content"])["url"] for m in tool_messages] == ["https://example.org/1", "https://example.org/2"]


@pytest.mark.asyncio
async def test_a_tool_with_side_effects_keeps_its_turn_in_order(store, tmp_path, monkeypatch):
    fetcher = SlowFetcher()
    web = web_tools(monkeypatch, fetcher)
    tid = store.submit(TaskSpec("读一页再写", max_calls=3), nonce="w")["task_id"]
    model = ScriptedModel([batch(("a", "fetch_page", {"url": "https://example.org/1"}),
                                 ("b", "write_artifact", {"name": "r.md", "content": "x"})),
                           ModelReply({"role": "assistant", "content": "stop"})])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    fetch, write = store.operations(tid)[:2]
    assert (fetch["tool"], write["tool"]) == ("fetch_page", "write_artifact")
    assert write["created"] >= fetch["ended"]


@pytest.mark.asyncio
async def test_parallel_searches_never_exceed_the_task_budget(store, tmp_path, monkeypatch):
    web = web_tools(monkeypatch, limit=2)
    tid = store.submit(TaskSpec("一起搜", max_calls=3), nonce="s")["task_id"]
    model = ScriptedModel([batch(*[(f"q{i}", "web_search", {"query": f"q{i}"}) for i in range(4)]),
                           ModelReply({"role": "assistant", "content": "stop"})])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    searches = [o for o in store.operations(tid) if o["tool"] == "web_search"]
    assert sorted(o["status"] for o in searches) == ["failed", "failed", "succeeded", "succeeded"]
    assert len(web.backend.queries) == 2


@pytest.mark.asyncio
async def test_the_same_page_read_twice_at_once_is_fetched_once_and_quotes_still_check(store, tmp_path, monkeypatch):
    fetcher = SlowFetcher()
    web = web_tools(monkeypatch, fetcher)
    tid = store.submit(TaskSpec("同一页读两次", max_calls=4), nonce="d")["task_id"]

    def write(messages, tools):
        page = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and "page_id" in json.loads(m["content"]))
        return tool_reply("write_artifact", {"name": "r.md", "content": f"发布于 1 [{page['page_id']}]"}, "w")

    def submit(messages, tools):
        page = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and "page_id" in json.loads(m["content"]))
        ref = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))["artifact_ref"]
        return tool_reply("submit_result", {"artifact_ref": ref, "summary": "发布于 1。", "limitations": [],
                                            "quotes": [{"source_id": page["page_id"], "quote": "Released 1."}]}, "s")

    model = ScriptedModel([batch(("a", "fetch_page", {"url": "https://example.org/1"}),
                                 ("b", "fetch_page", {"url": "https://example.org/1"})), write, submit])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    assert fetcher.urls == ["https://example.org/1"]
    pages = [o["result"] for o in store.operations(tid) if o["tool"] == "fetch_page"]
    assert pages[0]["sha256"] == pages[1]["sha256"]
    accept = [o for o in store.operations(tid) if o["tool"] == "submit_result"][-1]["result"]
    assert accept["passed"] is True and accept["quotes"][0]["verified"] is True


def test_only_plain_reads_may_declare_concurrent_running():
    from personal_agent.plugins import Plugin, PluginRegistry, ToolSpec
    from personal_agent.tools import tool
    schema = tool("note", "write a note", {"text": {"type": "string"}}, ["text"])
    for bad in ({"effect": "local_write"}, {"exclusive": True}, {"display_only": True}):
        with pytest.raises(ValueError, match="concurrently"):
            PluginRegistry([Plugin("ext", "1", [ToolSpec(schema, lambda c, a: {}, concurrent=True, **bad)])])
    PluginRegistry([Plugin("ext", "1", [ToolSpec(schema, lambda c, a: {}, concurrent=True)])])


@pytest.mark.asyncio
async def test_engine_is_told_which_tools_may_run_together(store, tmp_path, monkeypatch):
    seen = {}

    class Engine:
        identity = "schema-probe@1"

        async def run(self, messages, schemas, model_name, callback):
            seen.update({s["function"]["name"]: s.get("concurrent", False) for s in schemas})
            await callback("model", {"messages": messages})

    store.submit(TaskSpec("看工具", max_calls=1), nonce="e")
    model = ScriptedModel([ModelReply({"role": "assistant", "content": "stop"})])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web_tools(monkeypatch))], engine=Engine()).serve(once=True)
    assert seen["fetch_page"] and seen["web_search"] and seen["read_source"] and seen["read_memory"]
    assert not seen["write_artifact"] and not seen["submit_result"] and not seen["update_plan"]
    # The model itself sees the plain schemas, without the engine-only flag.
    assert all("concurrent" not in t for t in model.requests[0]["tools"])
