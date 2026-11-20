"""Reply layer decides how hard a question is; hard ones get a receipt, then a
considered answer from execution with thinking (2026-11-17)."""
import json

import pytest

from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.conversation import CONVERSATION_SCHEMA, INTAKE_PROMPT
from personal_agent.model import ModelConfig, QwenModel
from personal_agent.runtime import Runtime
from conftest import ScriptedModel, tool_reply


def incoming(store, text, nonce="n"):
    return store.accept_conversation(TaskSpec(text, [], max_calls=40, max_tools=100, max_seconds=900), text=text, nonce=nonce)


def deliver_all(store):
    for d in store.all("SELECT id FROM deliveries WHERE state IN ('pending','failed')"):
        store.accept_delivery(d["id"], {"id": d["id"], "accepted": True})


HARD = {"action": "new_task", "result_kind": "answer",
        "receipt": "我按你的身高体重和步数算一下每天该吃多少，稍等。"}


def test_intake_schema_lets_the_model_mark_a_question_for_a_considered_answer():
    props = CONVERSATION_SCHEMA[0]["function"]["parameters"]["properties"]
    assert "answer" in props["result_kind"]["enum"]
    # The model decides; the prompt describes what needs thinking, not topics or keywords.
    assert "result_kind=answer" in INTAKE_PROMPT


@pytest.mark.asyncio
async def test_hard_question_gets_receipt_then_a_plain_text_answer_without_artifact(store, tmp_path):
    incoming(store, "30岁男，175，80公斤，每天8000步，一个月瘦2公斤每天吃多少？")
    seen = {}

    def answer(messages, tools):
        seen["tools"] = sorted(t["function"]["name"] for t in tools)
        seen["system"] = messages[0]["content"]
        return tool_reply("deliver_answer", {"text": "每天大约吃 2000 千卡。\n- 基础代谢约 1750 千卡……", "promise": ""}, "ans")

    model = ScriptedModel([tool_reply("resolve_conversation", HARD), answer])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    [task] = store.all("SELECT id FROM tasks")
    assert store.task(task["id"])["spec"]["result_kind"] == "answer"
    await runtime.execute(store.claim())
    task = store.task(task["id"])
    assert task["status"] == "succeeded" and task["acceptance"] == "passed"
    # Only the answer path: no artifact writing, no document acceptance.
    assert "deliver_answer" in seen["tools"] and "write_artifact" not in seen["tools"] and "submit_result" not in seen["tools"]
    said = [d["content"] for d in store.all("SELECT content FROM deliveries ORDER BY rowid")]
    assert said[0] == HARD["receipt"]
    assert said[-1].startswith("每天大约吃 2000 千卡") and "基础代谢" in said[-1]
    assert not store.one("SELECT artifact FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (task["id"],))
    # The receipt promised an answer: kept only once the answer actually reached the user.
    [c] = store.all("SELECT * FROM commitments")
    assert c["state"] == "open"
    deliver_all(store)
    [c] = store.all("SELECT * FROM commitments")
    assert c["state"] == "fulfilled"
    assert any(e["kind"] == "delivery" for e in json.loads(c["evidence"]))


@pytest.mark.asyncio
async def test_answer_cannot_promise_later_work(store, tmp_path):
    incoming(store, "轻断食和少吃一点哪个好？")
    model = ScriptedModel([tool_reply("resolve_conversation", {**HARD, "receipt": "我对比一下两种做法的坚持难度和效果。"}),
                           tool_reply("deliver_answer", {"text": "少吃一点更好坚持。明天我再提醒你。", "promise": "明天我再提醒你"}, "a1"),
                           tool_reply("deliver_answer", {"text": "少吃一点更好坚持。", "promise": ""}, "a2")])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    await runtime.execute(store.claim())
    said = [d["content"] for d in store.all("SELECT content FROM deliveries ORDER BY rowid")]
    assert said[-1] == "少吃一点更好坚持。"
    assert "明天" not in " ".join(said)


@pytest.mark.asyncio
async def test_answer_written_as_plain_text_is_asked_once_to_send_it(store, tmp_path):
    incoming(store, "为什么节食容易反弹？")
    seen = []

    def resend(messages, tools):
        seen.append(messages[-1])
        return tool_reply("deliver_answer", {"text": "因为代谢适应和食欲上升。", "promise": ""}, "a")

    model = ScriptedModel([tool_reply("resolve_conversation", {**HARD, "receipt": "我从代谢和行为两方面讲讲节食为什么容易反弹。"}),
                           ModelReply({"role": "assistant", "content": "因为代谢适应和食欲上升。"}), resend])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    await runtime.execute(store.claim())
    assert store.all("SELECT content FROM deliveries ORDER BY rowid")[-1]["content"] == "因为代谢适应和食欲上升。"
    # The reminder is runtime guidance, not words from the user.
    assert seen[0]["role"] == "system"


@pytest.mark.asyncio
async def test_intake_reask_is_runtime_guidance_not_user_words(store, tmp_path):
    # Thinking models answer in prose first; the re-ask must not read as the
    # user asking to "record the reply", or the reply opens with "已记录".
    incoming(store, "鸡胸肉100克多少蛋白质")
    seen = []

    def second(messages, tools):
        seen.extend(messages)
        return tool_reply("resolve_conversation", {"action": "reply", "text": "约 23 克。", "promise": ""}, "r2")

    model = ScriptedModel([ModelReply({"role": "assistant", "content": "约 23 克。"}), second])
    await Runtime(store, tmp_path / "data", model).execute(store.claim_conversation())
    assert store.one("SELECT content FROM deliveries")["content"] == "约 23 克。"
    guidance = [m for m in seen if "resolve_conversation" in str(m.get("content"))]
    assert guidance and all(m["role"] == "system" for m in guidance)
    assert seen[-1] == {"role": "user", "content": "鸡胸肉100克多少蛋白质"}
    assert not any(m["role"] == "user" and "记录你的答复" in str(m["content"]) for m in seen)


def test_answer_task_runs_with_thinking_even_when_reply_layer_does_not(monkeypatch):
    monkeypatch.setenv("NOYI_TEST_KEY", "offline")
    model = QwenModel(ModelConfig(name="deepseek-flash", base_url="https://api.deepseek.com", api_key_env="NOYI_TEST_KEY", effort="medium"))
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop")

    model.client.chat.completions.create = create
    import asyncio
    from personal_agent.runtime import ANSWER_TOOL
    with pytest.raises(RuntimeError):
        asyncio.run(model.complete([{"role": "user", "content": "x"}], [ANSWER_TOOL]))
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}} and captured["reasoning_effort"] == "medium"
    assert "tool_choice" not in captured


# ---- an answer that needs tools becomes ordinary work (2026-11-21) -----------------

LOOKUP = {"action": "new_task", "result_kind": "answer", "receipt": "我去查一下 playwright 在 PyPI 上的最新版本号。"}


@pytest.mark.asyncio
async def test_answer_that_declares_it_needs_tools_continues_as_ordinary_work_with_one_receipt(store, tmp_path):
    incoming(store, "playwright 在 PyPI 上最新版本是多少？")
    seen = {}

    def answer_route(messages, tools):
        seen["answer_tools"] = {t["function"]["name"] for t in tools}
        return tool_reply("report_unable", {"explanation": "要上网查。", "needs": "outside_information_or_tools"}, "u1")

    def worker(messages, tools):
        seen["worker_tools"] = {t["function"]["name"] for t in tools}
        return tool_reply("report_unable", {"explanation": "测试到此为止。"}, "end")

    model = ScriptedModel([tool_reply("resolve_conversation", LOOKUP), answer_route, worker])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    [task] = store.all("SELECT id FROM tasks")
    commitments = store.all("SELECT id FROM commitments")
    await runtime.execute(store.claim())
    task = store.task(task["id"])
    # Same task, same message, same promise; nothing more was said to the user.
    assert task["spec"]["result_kind"] == "artifact" and task["status"] == "ready"
    assert [d["content"] for d in store.all("SELECT content FROM deliveries ORDER BY rowid")] == [LOOKUP["receipt"]]
    assert store.all("SELECT id FROM commitments") == commitments
    assert store.one("SELECT state FROM commitments")["state"] == "open"
    await runtime.execute(store.claim())
    assert "deliver_answer" in seen["answer_tools"]
    assert "write_artifact" in seen["worker_tools"] and "deliver_answer" not in seen["worker_tools"]
    assert len(store.all("SELECT id FROM tasks")) == 1
    said = [d["content"] for d in store.all("SELECT content FROM deliveries ORDER BY rowid")]
    assert said.count(LOOKUP["receipt"]) == 1 and len(said) == 2      # the receipt, then the worker's own end


@pytest.mark.asyncio
async def test_answer_unable_for_other_reasons_is_still_told_to_the_user(store, tmp_path):
    incoming(store, "帮我看看这张图里写了什么")
    model = ScriptedModel([tool_reply("resolve_conversation", {**LOOKUP, "receipt": "我看一下这张图。"}),
                           tool_reply("report_unable", {"explanation": "当前模型看不了图片，换个能看图的模型再试。", "needs": "nothing_available"}, "u1")])
    runtime = Runtime(store, tmp_path / "data", model)
    await runtime.execute(store.claim_conversation())
    await runtime.execute(store.claim())
    [task] = store.all("SELECT id FROM tasks")
    task = store.task(task["id"])
    assert task["spec"]["result_kind"] == "answer" and task["status"] == "paused" and task["reason"] == "unable_reported"
    assert store.all("SELECT content FROM deliveries ORDER BY rowid")[-1]["content"].startswith("当前模型看不了图片")


def test_answer_route_declares_needing_tools_in_a_structured_field():
    from personal_agent.runtime import ANSWER_UNABLE
    needs = ANSWER_UNABLE["function"]["parameters"]["properties"]["needs"]
    assert set(needs["enum"]) == {"outside_information_or_tools", "nothing_available"}
    assert "needs" in ANSWER_UNABLE["function"]["parameters"]["required"]
