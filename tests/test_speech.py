"""X-5/X-6/X-7/X-9: what the assistant says, and when; code guards model wording."""
import time

import pytest

from personal_agent.contracts import TaskSpec
from personal_agent.service import Service
from personal_agent.speech import check_receipt, shape_delivery
from conftest import ScriptedModel, tool_reply


def incoming(store, text, *, nonce="auto", sources=None):
    return store.accept_conversation(TaskSpec(text, sources or [], max_calls=40, max_tools=100, max_seconds=900), text=text, nonce=nonce)


def intake(store, decision):
    run = store.claim_conversation()
    store.conversation_context(run)
    return store.apply_conversation(run, decision)


def contents(store, task_id=None):
    sql = "SELECT content FROM deliveries" + (" WHERE task_id=?" if task_id else "") + " ORDER BY rowid"
    return [r["content"] for r in store.all(sql, (task_id,) if task_id else ())]


@pytest.mark.parametrize("text", ["好，我去核对这份资料里的负责人和截止日期，整理成清单。", "收到，先查三家的报价再对比。"])
def test_specific_short_receipt_is_kept(text):
    assert check_receipt(text) == text


@pytest.mark.parametrize("text", [None, "", "  ", "已经整理好了，清单见附件。", "搞定，结果如下", "报告已完成。", "好" * 61])
def test_promising_empty_or_long_receipt_is_rejected(text):
    assert check_receipt(text) is None


def test_delivery_keeps_one_conclusion_and_at_most_three_points():
    summary = "结论：现行负责人是周宁。\n\n- 截止 11 月 12 日\n- 报名日期未定\n- 无需经理审批\n- 远程免签到\n- 预算未知\n\n另外还有一段很长的说明。"
    shaped = shape_delivery(summary)
    lines = shaped.splitlines()
    assert lines[0] == "结论：现行负责人是周宁。"
    assert [l for l in lines[1:] if l] == ["- 截止 11 月 12 日", "- 报名日期未定", "- 无需经理审批"]
    assert shape_delivery("  ") == "成果整理好了，见下方附件。"
    paragraph = "现行负责人是周宁，旧版已作废，资料截止改到 11 月 12 日，审批改为作者自查。资料截止改到11月12日。审批改为作者自查。报名时间、预算和场地容量待确认。另有一句。"
    assert shape_delivery(paragraph).splitlines() == ["现行负责人是周宁，旧版已作废，资料截止改到 11 月 12 日，审批改为作者自查。", "- 资料截止改到11月12日", "- 审批改为作者自查", "- 报名时间、预算和场地容量待确认"]


def test_model_receipt_replaces_template_and_bad_receipt_falls_back(store):
    incoming(store, "帮我核对这份资料的负责人", nonce="a")
    intake(store, {"action": "new_task", "receipt": "好，我去核对资料里的负责人，整理好发你。"})
    incoming(store, "再帮我写一份邀请函", nonce="b")
    intake(store, {"action": "new_task", "receipt": "邀请函已经写好了。"})
    assert contents(store) == ["好，我去核对资料里的负责人，整理好发你。", "这件事我接下了，处理好后把结果给你；需要补充信息时我会问你。"]


def test_started_line_is_sent_once_when_work_actually_starts(store):
    incoming(store, "帮我整理一份周报", nonce="a")
    tid = intake(store, {"action": "new_task"})["task_id"]
    assert not contents(store, tid)  # only the intake receipt exists; nothing has started yet
    store.db.execute("UPDATE conversation_inputs SET updated=updated-30")  # queued for a while
    run = store.claim()
    assert run["task_id"] == tid
    assert contents(store, tid)[-1] == "已开跑，做好把成果发你。"
    store.recover()  # crash: the same event is claimed again
    store.claim()
    assert contents(store, tid).count("已开跑，做好把成果发你。") == 1


def test_no_started_line_when_work_begins_right_after_receipt(store):
    incoming(store, "帮我整理一份周报", nonce="a")
    tid = intake(store, {"action": "new_task"})["task_id"]
    store.claim()
    assert not contents(store, tid)


def test_arrangement_only_and_direct_tasks_do_not_announce_start(store):
    incoming(store, "以后每天九点盯这份资料", nonce="a")
    tid = intake(store, {"action": "new_task", "result_kind": "action_receipt"})["task_id"]
    assert store.claim()["task_id"] == tid
    store.db.execute("UPDATE runs SET phase='ended' WHERE task_id=?", (tid,))
    plain = store.submit(TaskSpec("直接提交的任务"), nonce="cli")["task_id"]
    assert store.claim()["task_id"] == plain
    assert not [c for c in contents(store) if c.startswith("已开跑")]


def test_explicit_remember_saves_immediately_and_restates(store):
    incoming(store, "记住，以后交付先写一句结论，再列三条要点", nonce="a")
    intake(store, {"action": "remember", "memory_key": "交付格式", "memory_value": "先写一句结论，再列三条要点", "receipt": "记住了，以后交付先给一句结论，再列三条要点。"})
    memory = store.one("SELECT * FROM memories WHERE key='交付格式'")
    assert memory["state"] == "active" and memory["source_type"] == "user_explicit"
    assert memory["value"] == "先写一句结论，再列三条要点"
    assert contents(store) == ["记住了，以后交付先给一句结论，再列三条要点。"]


def test_remember_needs_explicit_user_words(store):
    incoming(store, "这次交付先写结论吧", nonce="a")
    with pytest.raises(ValueError, match="明确"):
        intake(store, {"action": "remember", "memory_key": "交付格式", "memory_value": "先写结论"})
    assert not store.all("SELECT * FROM memories")


def test_first_time_over_twenty_minutes_sends_one_line_with_ledger_numbers(store):
    incoming(store, "帮我做一份长调研", nonce="a")
    tid = intake(store, {"action": "new_task"})["task_id"]
    run = store.claim()
    store.db.execute("INSERT INTO operations(id,task_id,run_id,tool,arguments,status,created) VALUES('op1',?,?,'read_source','{}','succeeded',?)", (tid, run["id"], time.time()))
    store.tick_waits()
    assert not [c for c in contents(store, tid) if c.startswith("还在做")]
    store.db.execute("UPDATE runs SET started=started-1260 WHERE task_id=?", (tid,))
    store.tick_waits(), store.tick_waits()
    progress = [c for c in contents(store, tid) if c.startswith("还在做")]
    assert progress == ["还在做，已经 21 分钟：读了 1 份资料，调用工具 1 次。做完马上发你。"]


@pytest.mark.asyncio
async def test_submitted_summary_is_shaped_before_delivery(store, tmp_path):
    incoming(store, "整理一份清单", nonce="a")
    tid = intake(store, {"action": "new_task"})["task_id"]
    long = "清单整理好了。\n- 一\n- 二\n- 三\n- 四\n- 五"

    def submit(messages, tools):
        import json
        ref = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))["artifact_ref"]
        return tool_reply("submit_result", {"artifact_ref": ref, "summary": long, "limitations": []}, "submit")

    model = ScriptedModel([tool_reply("write_artifact", {"name": "list.md", "content": "# 清单\n内容"}, "w"), submit])
    await Service(store, tmp_path / "data", model).run_until_quiescent()
    assert store.task(tid)["status"] == "succeeded"
    final = store.one("SELECT content FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (tid,))["content"]
    assert final == "清单整理好了。\n- 一\n- 二\n- 三"
