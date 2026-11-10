"""Demo data for the ledger replay video, in its own data directory.

    python scripts/demo-replay.py build --data .pa/demo-replay    # create the two demo tasks
    python scripts/demo-replay.py serve --data .pa/demo-replay    # read-only WebUI on port 8790

build runs the real pipeline (conversation layer, Pi runtime, ledger, deadline
check) with a scripted model, so every row comes from the same code paths as
real use. No model API is called and the model name in the ledger says so
("demo-scripted"). The clock is staged so the story reads like one working
day; that is demo staging, not ledger data of real use.

Four tasks:
  1. 周会纪要 → three action items: a spoken promise is refused, the work is
     taken on, sources read, artifact accepted, promise fulfilled with its sha256.
  2. 报价对比表, due 15:00: the run stops on an error, the user is told it is
     stuck, and after 15:00 the ledger reminds them once that the promise is overdue.
  3. 每天七点打电话: the assistant cannot do it and says so; the promise closes as unable.
  4. 测试组通知, due 16:00: the user moves it to 17:00 (改约), then it is kept.

serve opens the WebUI with the background service off: nothing runs, nothing
is sent. It never touches .pa/personal.
"""
import argparse
import asyncio
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from personal_agent.contracts import ModelReply, TaskSpec  # noqa: E402

DAY = "2026-11-10"
NOTES = """# 周会纪要（11 月 9 日）

- 新版安装包要在周四前给测试组试用，负责人：小林。
- 演示视频的脚本还没定稿，周三下午评审，负责人：阿周。
- 用户反馈里提到三次「提醒发得太晚」，需要查清楚是时区还是服务没开，负责人：小林。
"""
QUOTES = """# 三家供应商报价（摘录）

- 甲：单价 42 元，30 天账期，含运费。
- 乙：单价 39 元，现结，不含运费（约 3 元/件）。
- 丙：单价 40 元，45 天账期，含运费，起订 500 件。
"""


class Clock:
    """Staged wall clock: every reading moves a little; jumps move to a set local time."""
    def __init__(self, start):
        self.now = start

    def __call__(self):
        self.now += 0.35
        return self.now

    def at(self, hhmm):
        target = datetime.strptime(f"{DAY} {hhmm}", "%Y-%m-%d %H:%M").timestamp()
        self.now = max(self.now, target)


class Scripted:
    """Plays back fixed replies; the ledger records them as model "demo-scripted"."""
    metadata = {"provider": "demo", "requested_model": "demo-scripted"}

    def __init__(self, steps):
        self.steps = deque(steps)

    async def complete(self, messages, tools):
        step = self.steps.popleft()
        if isinstance(step, BaseException):
            raise step
        return step(messages) if callable(step) else step


def call(name, arguments, call_id, tokens=(820, 64)):
    return ModelReply({"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
                       "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
                      input_tokens=tokens[0], output_tokens=tokens[1], model="demo-scripted")


def submit_last_artifact(messages):
    receipt = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))
    return call("submit_result", {"artifact_ref": receipt["artifact_ref"], "summary": "三条行动项整理好了，每条都标了负责人和时间。", "limitations": []}, "submit", (1460, 52))


def build(data):
    if (data / "state.sqlite").exists():
        raise SystemExit(f"{data} 已经有数据；演示数据只往空目录里造，换个目录或先手动移走。")
    data.mkdir(parents=True, exist_ok=True)
    sources = data / "sources"
    sources.mkdir(exist_ok=True)
    (sources / "weekly-notes.md").write_text(NOTES, encoding="utf-8")
    (sources / "supplier-quotes.md").write_text(QUOTES, encoding="utf-8")

    clock = Clock(datetime.strptime(f"{DAY} 09:12", "%Y-%m-%d %H:%M").timestamp())
    time.time = clock  # staged demo clock; see module docstring

    from personal_agent.runtime import Runtime
    from personal_agent.store import Store
    store = Store(data / "state.sqlite")

    async def say(text, nonce, source_file, intake_steps, work_steps, before_work=None):
        spec = TaskSpec(text, [{"id": source_file.stem, "kind": "file", "location": str(source_file.resolve()), "required": True, "description": source_file.name}],
                        max_calls=40, max_tools=100, max_seconds=900)
        store.accept_conversation(spec, text=text, nonce=nonce)
        await Runtime(store, data, Scripted(intake_steps)).execute(store.claim_conversation())
        clock.now += 20
        if before_work:
            task = store.one("SELECT id FROM tasks WHERE parent_task_id IS NULL ORDER BY created DESC,rowid DESC LIMIT 1")
            await before_work(task["id"])
        run = store.claim()
        await Runtime(store, data, Scripted(work_steps)).execute(run)
        return run["task_id"]

    async def story():
        # 1. Kept: a spoken promise is refused first, then the work is really taken on.
        notes = sources / "weekly-notes.md"
        first = await say("帮我把这周的周会纪要整理成三条行动项，标上负责人。", "demo-1", notes, [
            call("resolve_conversation", {"action": "reply", "text": "好的，我稍后整理好发你。", "promise": "我稍后整理好发你"}, "c1", (2100, 48)),
            call("resolve_conversation", {"action": "new_task", "receipt": "好，我去整理成三条行动项，做好发你。", "result_kind": "artifact"}, "c2", (2240, 40)),
        ], [
            call("read_source", {"source_id": "weekly-notes"}, "read", (1180, 30)),
            call("write_artifact", {"name": "action-items.md", "content": "# 本周行动项\n\n1. 周四前把新版安装包交给测试组试用（小林）[weekly-notes]\n2. 周三下午评审演示视频脚本（阿周）[weekly-notes]\n3. 查清「提醒发得太晚」是时区还是服务没开（小林）[weekly-notes]\n"}, "write", (1390, 210)),
            submit_last_artifact,
        ])
        # 2. Overdue: a deadline the user gave, the run stops, the reminder is told once.
        clock.at("10:05")
        quotes = sources / "supplier-quotes.md"
        second = await say("把三家供应商的报价整理成一张对比表，今天下午三点前给我。", "demo-2", quotes, [
            call("resolve_conversation", {"action": "new_task", "receipt": "好，三点前整理好对比表发你。", "result_kind": "artifact",
                                          "due_at": f"{DAY} 15:00", "due_basis": "今天下午三点前"}, "c3", (2310, 58)),
        ], [
            call("read_source", {"source_id": "supplier-quotes"}, "read", (1150, 28)),
            RuntimeError("demo: the model service stopped answering"),
        ])
        clock.at("15:01")
        store.tick_commitments()

        async def reschedule(task_id):
            # Before the work starts the user moves the deadline in the same chat.
            clock.at("15:50")
            promise = store.one("SELECT id FROM commitments WHERE task_id=? AND state='open'", (task_id,))
            store.accept_conversation(TaskSpec("改到五点吧，四点有个会。", [], max_calls=40, max_tools=100, max_seconds=900), text="改到五点吧，四点有个会。", nonce="demo-4b")
            await Runtime(store, data, Scripted([call("resolve_conversation", {"action": "commitment", "commitment_id": promise["id"], "commitment_op": "reschedule",
                                                                                "due_at": f"{DAY} 17:00", "basis": "改到五点吧"}, "c6", (2260, 46))])).execute(store.claim_conversation())
            clock.at("16:30")

        # 3. Unable: something this machine cannot do is closed honestly, not left hanging.
        clock.at("15:20")
        third = await say("每天早上七点打电话叫我起床。", "demo-3", notes, [
            call("resolve_conversation", {"action": "new_task", "receipt": "好，我看看能不能每天七点打电话叫你。", "result_kind": "artifact"}, "c4", (2050, 44)),
        ], [
            call("report_unable", {"explanation": "我没有打电话的能力，这台电脑上也没接电话服务，没法每天七点叫你。可以用手机闹钟，我帮你把作息写成提醒清单。"}, "unable", (1320, 76)),
        ])
        # 4. Rescheduled, then kept: the user moves the deadline; the new promise is fulfilled.
        clock.at("15:40")
        fourth = await say("帮我把周会纪要改写成给测试组的通知，下午四点前给我。", "demo-4", notes, [
            call("resolve_conversation", {"action": "new_task", "receipt": "好，四点前写好通知发你。", "result_kind": "artifact",
                                          "due_at": f"{DAY} 16:00", "due_basis": "下午四点前"}, "c5", (2180, 52)),
        ], [
            call("read_source", {"source_id": "weekly-notes"}, "read", (1170, 30)),
            call("write_artifact", {"name": "notice.md", "content": "# 致测试组\n\n新版安装包周四前交给你们试用（小林负责）[weekly-notes]。\n"}, "write", (1310, 120)),
            lambda messages: call("submit_result", {"artifact_ref": next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool" and "artifact_ref" in json.loads(m["content"]))["artifact_ref"],
                                                    "summary": "给测试组的通知写好了。", "limitations": []}, "submit", (1400, 40)),
        ], before_work=reschedule)
        return first, second, third, fourth

    try:
        first, second, third, fourth = asyncio.run(story())
    finally:
        store.close()
    print(json.dumps({"data": str(data), "kept_task": first, "overdue_task": second, "unable_task": third, "rescheduled_task": fourth}, ensure_ascii=False))


class ReadOnlyModel:
    """The demo server never runs work, so the model is never asked anything."""
    ready = True
    config = None

    async def complete(self, messages, tools):
        raise RuntimeError("demo server does not call a model")

    async def close(self):
        pass


def serve(data, port):
    from aiohttp import web
    from personal_agent.model import ModelConfig
    from personal_agent.store import Store
    from personal_agent.webui import create_app
    if not (data / "state.sqlite").is_file():
        raise SystemExit(f"{data} 里还没有演示数据，先运行 build。")
    model = ReadOnlyModel()
    model.config = ModelConfig(name="demo-scripted", base_url="https://api.deepseek.com")
    store = Store(data / "state.sqlite")
    print(f"演示回放：http://127.0.0.1:{port}  （只读，不运行任何任务）", flush=True)
    web.run_app(create_app(store, data, model, run_service=False), host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "serve"])
    parser.add_argument("--data", type=Path, default=ROOT / ".pa" / "demo-replay")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    data = args.data.resolve()
    if data == (ROOT / ".pa" / "personal").resolve():
        raise SystemExit("演示数据不能放进自用库 .pa/personal。")
    build(data) if args.command == "build" else serve(data, args.port)
