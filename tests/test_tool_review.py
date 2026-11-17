"""Weekly tool-chain review: code counts recurring call patterns from the
ledger, the model only explains them, and nothing it does creates a template."""
import json
import time
import uuid

import pytest

from conftest import ScriptedModel, tool_reply
from personal_agent.contracts import TaskSpec
from personal_agent.service import Service
from personal_agent.tool_review import CONFIG_NAME, ReviewConfig, chain_stats, ensure_tool_review

FULL = [["web_search"], ["fetch_page"], ["fetch_page"], ["write_artifact"], ["submit_result"]]


def ledger_task(store, rounds, start):
    """A task whose ledger has these rounds: one model step, then its calls."""
    task_id = store.submit(TaskSpec("x"), nonce=uuid.uuid4().hex)["task_id"]
    for i, tools in enumerate(rounds):
        at = start + i * 10
        store.db.execute("INSERT INTO steps(task_id,run_id,kind,data,created) VALUES(?,?,?,?,?)",
                         (task_id, "run-" + task_id, "model", json.dumps({"input_tokens": 100 * (i + 1), "output_tokens": 10, "seconds": 2}), at))
        for j, name in enumerate(tools):
            store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,result,created,ended) VALUES(?,?,?,?,?,?,?,?,?)",
                             (uuid.uuid4().hex, task_id, "run-" + task_id, name, json.dumps({"q": "PRIVATE-WORDS"}), "succeeded",
                              json.dumps({"text": "PRIVATE-PAGE"}), at + 0.1 + j * 0.01, at + 1))
    return task_id


def config(tmp_path, extra=""):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / CONFIG_NAME).write_text("[tool_review]\nmin_tasks = 2\nmin_occurrences = 2\nmin_saved_rounds = 2\n" + extra, encoding="utf-8")
    return ReviewConfig.load(data)


def test_recurring_pattern_is_counted_from_the_ledger_with_cost_and_no_content(store, tmp_path):
    now = time.time()
    ledger_task(store, FULL, now - 86400)
    ledger_task(store, FULL + [["update_plan"]], now - 80000)
    ledger_task(store, [["web_search"], ["fetch_page", "web_search"], ["fetch_page"]], now - 70000)
    ledger_task(store, FULL, now - 30 * 86400)  # outside the 7-day window
    stats = chain_stats(store, config(tmp_path), now=now)
    assert stats["totals"]["tasks"] == 3 and stats["totals"]["rounds"] == 13
    [top] = stats["candidates"]
    assert top["pattern"] == ["web_search", "fetch_page", "write_artifact", "submit_result"]
    assert (top["tasks"], top["occurrences"], top["rounds"], top["calls"]) == (2, 2, 10, 10)
    assert top["input_tokens"] == 3000 and top["est_saved_rounds"] == 8 and top["est_saved_input_tokens"] == 2400
    assert top["seconds"] == 30  # each round: model start (2 s before its step) to its last tool's end (1 s after)
    assert top["covered_by"] == [] and not stats["no_candidates"]
    text = json.dumps(stats, ensure_ascii=False)
    assert "PRIVATE" not in text and "update_plan" not in text


def test_pattern_an_existing_template_covers_is_not_a_new_candidate(store, tmp_path):
    now = time.time()
    for k in range(2):
        ledger_task(store, FULL, now - 3600 * (k + 1))
    stats = chain_stats(store, config(tmp_path, '[[tool_review.templates]]\nname = "research_then_write"\n'
                                                'pattern = ["web_search", "fetch_page+", "write_artifact", "submit_result"]\n'), now=now)
    assert stats["candidates"] == [] and stats["no_candidates"]
    assert stats["covered"][0]["covered_by"] == ["research_then_write"]


def test_thresholds_come_from_configuration(store, tmp_path):
    now = time.time()
    for k in range(2):
        ledger_task(store, FULL, now - 3600 * (k + 1))
    assert chain_stats(store, config(tmp_path), now=now)["candidates"]
    data = tmp_path / "data"
    (data / CONFIG_NAME).write_text("[tool_review]\nmin_tasks = 3\n", encoding="utf-8")
    assert chain_stats(store, ReviewConfig.load(data), now=now)["no_candidates"]
    (data / CONFIG_NAME).write_text("[tool_review]\nsurprise = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ReviewConfig.load(data)


def test_weekly_review_is_registered_once_on_the_existing_scheduler(store, tmp_path):
    data = tmp_path / "data"
    first = ensure_tool_review(store, data)
    assert ensure_tool_review(store, data) == first
    policy = store.one("SELECT * FROM routine_policies WHERE goal_id=?", (first,))
    schedule = json.loads(policy["schedule"])
    assert schedule["kind"] == "calendar" and schedule["weekdays"] == [0] and schedule["time"] == "09:00"
    assert store.one("SELECT COUNT(*) n FROM commitments WHERE policy_id=? AND kind='standing' AND state='open'", (policy["id"],))["n"] == 1
    (data / CONFIG_NAME).write_text("[tool_review]\nenabled = false\n", encoding="utf-8")
    ensure_tool_review(store, data)
    assert store.goal(first)["status"] == "retired"
    assert store.one("SELECT state FROM commitments WHERE policy_id=?", (policy["id"],))["state"] == "released"


async def run_review(store, data, write):
    goal = ensure_tool_review(store, data)
    due = store.one("SELECT next_due FROM routine_policies WHERE goal_id=?", (goal,))["next_due"]
    model = ScriptedModel([tool_reply("tool_chain_stats", {}, "stats"), write,
                           lambda messages, tools: tool_reply("submit_result", {"artifact_ref": next(
                               json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in m["content"])["artifact_ref"],
                               "summary": "本周没有新候选", "limitations": []}, "submit")])
    service = Service(store, data, model, clock=lambda: due + 1)
    await service.triggers.tick()
    await service.run_until_quiescent()
    task = store.one("SELECT * FROM tasks WHERE goal_id=?", (goal,))
    return task, model


def stats_table(messages):
    return next(json.loads(m["content"])["table"] for m in messages if m["role"] == "tool" and '"table"' in m["content"])


@pytest.mark.asyncio
async def test_weekly_review_delivers_the_code_computed_table(store, tmp_path):
    data = tmp_path / "data"
    write = lambda messages, tools: tool_reply("write_artifact", {"name": "review.md", "content": "# 工具调用复盘\n\n" + stats_table(messages) + "\n\n本周没有新候选。"}, "write")
    task, model = await run_review(store, data, write)
    assert task["status"] == "succeeded" and task["acceptance"] == "passed"
    assert "tool_chain_stats" in [t["function"]["name"] for t in model.requests[0]["tools"]]
    delivery = store.one("SELECT * FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (task["id"],))
    assert delivery and delivery["content"].startswith("本周没有新候选")


@pytest.mark.asyncio
async def test_review_whose_numbers_differ_from_the_table_is_not_accepted(store, tmp_path):
    data = tmp_path / "data"
    write = lambda messages, tools: tool_reply("write_artifact", {"name": "review.md", "content": stats_table(messages).replace("| 0 |", "| 7 |", 1) + "\n本周没有新候选。"}, "write")
    task, model = await run_review(store, data, write)
    assert task["acceptance"] != "passed"
    rejected = json.loads(model.requests[-1]["messages"][-1]["content"])
    assert any("tool_chain_stats" in e for e in rejected["errors"])


@pytest.mark.asyncio
async def test_ordinary_tasks_do_not_see_the_review_tool(store, tmp_path, spec, successful_model):
    ensure_tool_review(store, tmp_path / "data")
    store.submit(spec, nonce="plain")
    await Service(store, tmp_path / "data", successful_model).run_until_quiescent()
    assert "tool_chain_stats" not in [t["function"]["name"] for t in successful_model.requests[0]["tools"]]
