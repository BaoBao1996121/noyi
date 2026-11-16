"""Chat display: double-escaped model text and where question cards sit."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from personal_agent.contracts import TaskSpec, chat_text
from personal_agent.webui import Console
from conftest import ScriptedModel
from test_conversation import incoming, claim_intake

BS = "\\"
WEB = Path(__file__).resolve().parents[1] / "src" / "personal_agent" / "web"


def test_double_escaped_line_breaks_become_real_ones():
    assert chat_text("可能是：" + BS + "n" + BS + "n1. 甲；" + BS + "n2. 乙") == "可能是：\n\n1. 甲；\n2. 乙"
    assert chat_text("a" + BS + "r" + BS + "nb" + BS + "tc") == "a\r\nb\tc"


def test_genuine_backslashes_are_left_alone():
    real = "第一行\n用 `print('a" + BS + "nb')` 换行"
    assert chat_text(real) == real                      # has real breaks: not double-escaped
    code = "在 Python 里写 `" + BS + "n` 表示换行"
    assert chat_text(code) == code                      # inside a code span
    path = "C:" + BS + "Users" + BS + "me" + BS + "notes.txt"
    assert chat_text("文件在 " + path) == "文件在 " + path  # \U is no JSON escape: not encoded text
    assert chat_text(None) is None and chat_text("") == ""


def test_reply_saved_from_a_double_escaped_tool_call_reads_with_real_breaks(store):
    incoming(store, "你这个可以吗")
    run = claim_intake(store)
    store.apply_conversation(run, {"action": "clarify", "text": "你指的是哪件事？" + BS + "n" + BS + "n1. 会话问题；" + BS + "n2. 记账页", "promise": ""})
    assert store.one("SELECT content FROM deliveries")["content"] == "你指的是哪件事？\n\n1. 会话问题；\n2. 记账页"


def test_question_text_from_the_model_is_stored_with_real_breaks(store):
    store.submit(TaskSpec("做个网页"), nonce="w")
    store.ask_user(store.claim(), question="做成什么？" + BS + "n比如简历", options=["简历" + BS + "n页"])
    q = store.one("SELECT question,options FROM attentions")
    assert q["question"] == "做成什么？\n比如简历" and json.loads(q["options"]) == ["简历\n页"]


def test_rows_saved_before_the_fix_display_with_real_breaks_and_questions_carry_time(store, tmp_path):
    incoming(store, "你这个可以吗")
    store.apply_conversation(claim_intake(store), {"action": "reply", "text": "好", "promise": ""})
    store.db.execute("UPDATE deliveries SET content=?", ("旧回复" + BS + "n" + BS + "n1. 甲",))
    store.submit(TaskSpec("做个网页"), nonce="w")
    store.ask_user(store.claim(), question="做成什么？", options=[])
    snap = Console(store, tmp_path / "data", ScriptedModel([])).snapshot()
    assert [d["content"] for d in snap["deliveries"] if d["kind"] != "question"] == ["旧回复\n\n1. 甲"]
    assert all(isinstance(q["created"], float) and q["created"] > 0 for q in snap["questions"])


NODE = shutil.which("node")


def place(items, questions):
    script = ("import {placeQuestions} from " + json.dumps((WEB / "timeline.js").as_uri()) + ";"
              "const r=placeQuestions(" + json.dumps(items) + "," + json.dumps(questions) + ",q=>'Q:'+q.id);"
              "console.log(JSON.stringify(r));")
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_closed_questions_return_to_history_and_only_pending_ones_stay_at_the_bottom():
    items = [{"sort": 10, "html": "old-msg"}, {"sort": 100, "html": "new-msg"}, {"sort": 101, "html": "new-reply"}]
    questions = [{"id": "answered", "state": "answered", "created": 11},
                 {"id": "stale", "state": "superseded", "created": 12},
                 {"id": "open", "state": "pending", "created": 50}]
    result = place(items, questions)
    assert result["history"] == ["old-msg", "Q:answered", "Q:stale", "new-msg", "new-reply"]
    assert result["pinned"] == ["Q:open"]
