"""Progress while execution works: a plan the model writes, and a last-action
line built from the ledger's real operations (not from what the model says)."""
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from personal_agent.contracts import TaskSpec, encode
from personal_agent.web_tools import WebConfig, WebTools, web_plugin
from personal_agent.webui import Console
from conftest import ScriptedModel, tool_reply
from test_web_tools import FakeFetcher, FakeSearch, page_citing, submit

WEB = Path(__file__).resolve().parents[1] / "src" / "personal_agent" / "web"
NODE = shutil.which("node")
STEPS = [{"text": "搜索当下流行的饮食方案", "state": "doing"},
         {"text": "逐个读权威来源", "state": "pending"},
         {"text": "按人群整理对照表", "state": "pending"}]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("WEB_TEST_KEY", "unit-test-only")
    return WebTools(WebConfig(api_key_env="WEB_TEST_KEY", task_search_limit=5, daily_search_limit=5), backend=FakeSearch(), fetcher=FakeFetcher())


def batch(*calls):
    reply = tool_reply(*calls[0])
    reply.message["tool_calls"] = [tool_reply(name, args, cid).message["tool_calls"][0] for name, args, cid in calls]
    return reply


@pytest.mark.asyncio
async def test_while_working_the_page_sees_the_models_plan_and_the_last_real_action(store, tmp_path, web):
    tid = store.submit(TaskSpec("当前比较火的饮食计划，适合什么人群？"), nonce="p")["task_id"]
    seen = {}

    def look(messages, tools):
        seen["mid"] = console.snapshot()
        steps = [dict(s, state="done") for s in STEPS[:1]] + [dict(STEPS[1], state="doing"), STEPS[2]]
        return batch(("update_plan", {"steps": steps}, "p2"), ("fetch_page", {"url": "https://example.org/notes"}, "f"))

    model = ScriptedModel([batch(("update_plan", {"steps": STEPS}, "p1"), ("web_search", {"query": "流行 饮食 方案"}, "q")),
                           look, page_citing, submit])
    console = Console(store, tmp_path / "data", model, run_service=False, plugins=[web_plugin(web)])
    await console.service.run_until_quiescent()
    assert store.task(tid)["status"] == "succeeded"
    assert "update_plan" in [t["function"]["name"] for t in model.requests[0]["tools"]]

    progress = seen["mid"]["progress"][tid]
    assert progress["plan"] == STEPS
    # The last action is the search the ledger recorded, not the plan update;
    # the model is now thinking about the next step, which the page shows too.
    assert progress["action"]["tool"] == "web_search" and progress["action"]["target"] == "流行 饮食 方案"
    assert progress["action"]["status"] == "succeeded" and progress["action"]["ended"]
    assert progress["thinking_since"] and progress["thinking_since"] >= progress["action"]["ended"]
    # Finished work leaves the live view; the plan stays in the ledger for replay.
    assert tid not in console.snapshot()["progress"]
    replayed = [e for e in store.task_timeline(tid)["events"] if e["title"] == "更新进度清单"]
    assert len(replayed) == 2 and "✓ 搜索当下流行的饮食方案" in replayed[-1]["text"]


@pytest.mark.asyncio
async def test_the_plan_is_display_only_never_a_promise_or_a_delivery(store, tmp_path, web):
    tid = store.submit(TaskSpec("查最新版本"), nonce="d")["task_id"]
    model = ScriptedModel([batch(("update_plan", {"steps": [{"text": "明天提醒你复查", "state": "doing"}]}, "p"),
                                 ("fetch_page", {"url": "https://example.org/notes"}, "f")), page_citing, submit])
    console = Console(store, tmp_path / "data", model, run_service=False, plugins=[web_plugin(web)])
    before = store.one("SELECT COUNT(*) n FROM commitments")["n"]
    await console.service.run_until_quiescent()
    assert store.one("SELECT COUNT(*) n FROM commitments")["n"] == before
    # Recorded for replay, but spends no tool budget and is not activity.
    assert [o["tool"] for o in store.operations(tid)].count("update_plan") == 1
    assert store.task(tid)["tools_used"] == 3
    assert not store.one("SELECT 1 FROM activity_log WHERE task_id=? AND tool='update_plan'", (tid,))
    assert not any("明天提醒你" in d["content"] for d in store.all("SELECT content FROM deliveries WHERE task_id=?", (tid,)))


@pytest.mark.parametrize("steps", [[], [{"text": "", "state": "doing"}], [{"text": "a", "state": "maybe"}],
                                   [{"text": "a" * 81, "state": "pending"}], [{"text": "a", "state": "done"}] * 9,
                                   [{"text": "a", "state": "done", "extra": 1}]])
def test_malformed_plans_are_refused(steps):
    from personal_agent.builtin_plugins import validate_plan
    with pytest.raises(ValueError):
        validate_plan({"steps": steps})


def fill(store, tid, *, steps, ops):
    """A root with a worker, `ops` real tool calls and a plan of `steps` steps."""
    run = store.claim()
    child = uuid.uuid4().hex
    store.db.execute("INSERT INTO tasks(id,root_task_id,parent_task_id,actor_id,room_id,spec,created) SELECT ?,id,id,actor_id,room_id,spec,created FROM tasks WHERE id=?", (child, tid))
    now = time.time() - ops
    for i in range(ops):
        task = child if i % 2 else tid
        store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,result,created,ended) VALUES(?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, task, run["id"], "fetch_page", encode({"url": f"https://example.org/{i}"}), "succeeded", encode({"title": f"第 {i} 页"}), now + i, now + i + 0.5))
        if i % 3 == 0:
            store.db.execute("INSERT INTO model_requests VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, run["id"], i + 1, "x", "{}", "completed", now + i, now + i + 0.2))
        if i % 6 == 0:
            plan = [{"text": f"第 {k} 步", "state": "done" if k < i * steps // ops else "pending"} for k in range(min(steps, 8))]
            store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,result,created,ended) VALUES(?,?,?,?,?,?,?,?,?)",
                             (uuid.uuid4().hex, tid, run["id"], "update_plan", encode({"steps": plan}), "succeeded", "{}", now + i, now + i))
    return run


def counted_snapshot(console):
    queries = []
    console.store.db.set_trace_callback(queries.append)
    try:
        start = time.perf_counter()
        snap = console.snapshot()
        return snap, len(queries), time.perf_counter() - start
    finally:
        console.store.db.set_trace_callback(None)


def test_snapshot_queries_do_not_grow_with_steps_or_actions(tmp_path):
    from personal_agent.store import Store
    counts = {}
    for label, ops in [("small", 1), ("large", 100)]:  # 100 is ~3x the busiest real task
        store = Store(tmp_path / label / "state.sqlite")
        console = Console(store, tmp_path / label, ScriptedModel([]), run_service=False)
        tid = store.submit(TaskSpec("多步调研"), nonce="n")["task_id"]
        fill(store, tid, steps=18, ops=ops)
        snap, counts[label], seconds = counted_snapshot(console)
        assert snap["progress"][tid]["action"]["tool"] == "fetch_page"
        assert seconds < 1.0
        store.close()
    assert counts["large"] == counts["small"]


def render(progress, now):
    script = ("import {progressSummary,progressPanel} from " + json.dumps((WEB / "progress.js").as_uri()) + ";"
              "const p=" + json.dumps(progress) + ";const t0=performance.now();"
              "const out={summary:progressSummary(p," + str(now) + "),panel:progressPanel(p," + str(now) + ")};"
              "out.ms=performance.now()-t0;console.log(JSON.stringify(out));")
    run = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(run.stdout)
    out["text"] = re.sub(r"<[^>]+>", "", out["panel"])
    return out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_progress_line_reads_like_a_person_and_shows_how_long_ago():
    progress = {"plan": [{"text": "搜索方案", "state": "done"}, {"text": "读来源 <b>", "state": "doing"}, {"text": "整理对照表", "state": "pending"}],
                "action": {"tool": "fetch_page", "target": "某营养学会指南", "status": "succeeded", "at": 100, "ended": 103}, "thinking_since": None}
    out = render(progress, 115)
    assert "第 2/3 步" in out["summary"] and "读来源 &lt;b&gt;" in out["summary"]
    assert "读取网页「某营养学会指南」" in out["text"] and "12 秒前" in out["text"]
    assert out["panel"].count("✓") == 1
    thinking = render({**progress, "thinking_since": 103}, 163)
    assert "正在想下一步，已等 1 分钟" in thinking["text"]
    bare = render({"plan": [], "action": None, "thinking_since": 50}, 58)
    assert "还没有列步骤" in bare["text"] and "已等 8 秒" in bare["text"]
    many = render({**progress, "plan": [{"text": f"第 {i} 步", "state": "done" if i < 10 else "pending"} for i in range(18)]}, 115)
    assert many["ms"] < 50


def test_every_module_a_served_script_imports_is_served_too():
    # progress.js was first missing from the allowlist: the page loaded
    # app.js, its import 404ed, and nothing on the page started.
    from personal_agent.demo_site import ASSETS
    from personal_agent.webui import STATIC_FILES
    for served in (STATIC_FILES, set(ASSETS)):
        for name in sorted(n for n in served if n.endswith(".js") and n not in {"sw.js"}):
            for imported in re.findall(r"from\s+'\./([\w.-]+\.js)'", (WEB / name).read_text(encoding="utf-8")):
                assert imported in served, f"{name} imports {imported}, which is not served"
