"""X-2/X-3/X-4: web search + page reading plugin with budgets, and the delivery page."""
import json

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.result_page import render
from personal_agent.service import Service
from personal_agent.web_tools import WebConfig, WebTools, page_text, public_address, web_plugin
from conftest import ScriptedModel, tool_reply

HTML = b"<html><head><title>Release notes</title><script>alert(1)</script></head><body><h1>v2.0</h1><p>Released 2026-11-01.</p><style>p{}</style></body></html>"


class FakeSearch:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return {"urls": ["https://example.org/notes"], "summary": "v2.0 was released", "billed_searches": 1}


class FakeFetcher:
    def __init__(self):
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        return url, "text/html; charset=utf-8", HTML


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("WEB_TEST_KEY", "unit-test-only")
    return WebTools(WebConfig(api_key_env="WEB_TEST_KEY", task_search_limit=2, daily_search_limit=3), backend=FakeSearch(), fetcher=FakeFetcher())


def test_page_text_drops_scripts_and_keeps_title():
    title, text = page_text(HTML, "text/html")
    assert title == "Release notes"
    assert "alert" not in text and "p{}" not in text
    assert "v2.0" in text and "Released 2026-11-01." in text


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.8", "169.254.169.254", "::1"])
def test_page_reader_refuses_non_public_hosts(host):
    with pytest.raises(ValueError, match="public"):
        public_address(host)


def test_tools_hidden_without_key(monkeypatch, store, tmp_path):
    from personal_agent.runtime import Runtime
    monkeypatch.delenv("WEB_TEST_KEY", raising=False)
    plugin = web_plugin(WebTools(WebConfig(api_key_env="WEB_TEST_KEY")))
    tid = store.submit(TaskSpec("查一下"), nonce="a")["task_id"]
    run = store.claim()
    names = {s["function"]["name"] for s in Runtime(store, tmp_path / "data", ScriptedModel([]), plugins=[plugin]).schemas(run)}
    assert "web_search" not in names and run["task_id"] == tid


def page_citing(messages, tools):
    page = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "page_id" in json.loads(m["content"]))
    return tool_reply("write_artifact", {"name": "r.md", "content": f"# 版本\n\n最新是 v2.0 [{page['page_id']}]，另见 [{page['page_id']}](https://evil.example/x)"}, "w")


def submit(messages, tools):
    ref = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))["artifact_ref"]
    return tool_reply("submit_result", {"artifact_ref": ref, "summary": "最新是 v2.0。", "limitations": []}, "s")


@pytest.mark.asyncio
async def test_search_then_fetch_then_cited_delivery_page(store, tmp_path, web):
    tid = store.submit(TaskSpec("查最新版本"), nonce="a")["task_id"]
    model = ScriptedModel([tool_reply("web_search", {"query": "最新 版本"}, "q"),
                           tool_reply("fetch_page", {"url": "https://example.org/notes"}, "f"), page_citing, submit])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    assert store.task(tid)["status"] == "succeeded"
    assert web.backend.queries == ["最新 版本"] and web.fetcher.urls == ["https://example.org/notes"]
    assert "web_search only for current" in model.requests[0]["messages"][0]["content"]
    html = render(store, tmp_path / "data", tid)
    assert "<script" not in html and "已验收" in html
    assert 'href="https://example.org/notes"' in html and "evil.example" not in html
    kinds = {r["kind"] for r in store.all("SELECT kind FROM progress_log WHERE task_id=?", (tid,))}
    assert {"web_search", "web_coverage"} <= kinds


@pytest.mark.asyncio
async def test_citing_an_unfetched_page_fails_acceptance(store, tmp_path, web):
    tid = store.submit(TaskSpec("查最新版本", max_calls=6), nonce="a")["task_id"]
    model = ScriptedModel([tool_reply("write_artifact", {"name": "r.md", "content": "最新是 v2.0 [page_0123456789]"}, "w"), submit])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    accept = [o for o in store.operations(tid) if o["tool"] == "submit_result"][0]
    assert accept["result"]["passed"] is False
    assert any("page_0123456789" in e for e in accept["result"]["errors"])
    assert render(store, tmp_path / "data", tid) is None


@pytest.mark.asyncio
async def test_search_budget_stops_and_points_to_ask_user(store, tmp_path, web):
    tid = store.submit(TaskSpec("多搜几次", max_calls=8), nonce="a")["task_id"]
    model = ScriptedModel([tool_reply("web_search", {"query": f"q{i}"}, f"q{i}") for i in range(3)]
                          + [tool_reply("write_artifact", {"name": "r.md", "content": "没查全"}, "w"), submit])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)]).run_until_quiescent()
    searches = [o for o in store.operations(tid) if o["tool"] == "web_search"]
    assert [o["status"] for o in searches] == ["succeeded", "succeeded", "failed"]
    assert "search budget used up" in searches[2]["result"]["detail"] and "ask_user" in searches[2]["result"]["detail"]
    assert web.backend.queries == ["q0", "q1"]
    assert searches[1]["result"]["budget"] == {"task_used": 2, "task_limit": 2, "today_used": 2, "daily_limit": 3}


@pytest.mark.asyncio
async def test_daily_budget_spans_tasks(store, tmp_path, web):
    for n in range(2):
        store.submit(TaskSpec(f"任务{n}", max_calls=6), nonce=f"t{n}")
    model = ScriptedModel([tool_reply("web_search", {"query": "a"}, "a"), tool_reply("web_search", {"query": "b"}, "b"),
                           tool_reply("write_artifact", {"name": "r.md", "content": "x"}, "w"), submit,
                           tool_reply("web_search", {"query": "c"}, "c"), tool_reply("web_search", {"query": "d"}, "d"),
                           tool_reply("write_artifact", {"name": "r.md", "content": "y"}, "w2"), submit])
    await Service(store, tmp_path / "data", model, plugins=[web_plugin(web)], concurrency=1).run_until_quiescent()
    assert web.backend.queries == ["a", "b", "c"]
