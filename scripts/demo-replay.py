"""Demo data for the ledger replay video, in its own data directory.

    python scripts/demo-replay.py build --data .pa/demo-replay     # four tasks + demo memories
    python scripts/demo-replay.py memories --data .pa/demo-replay  # add only the demo memories
    python scripts/demo-replay.py serve --data .pa/demo-replay    # read-only WebUI on port 8790
    python scripts/demo-replay.py export --data .pa/demo-site --out dist/site   # static site (GitHub Pages)
    python scripts/demo-replay.py site --out dist/site            # open the exported site locally on 8791

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

Demo memories (~44, for the memory galaxy): all five kinds, three superseded
memories, a corrected one, an expired and several near-expiry 进行中 items, and
five pending proposals. They go through the real memory API; their clock is the
real clock a day back, so expiry reads relative to today.

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

REAL_TIME = time.time  # demo staging replaces time.time; memory expiry is measured against the real clock

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

    def delivered():
        # The user's screen acknowledges each message, as Service.deliver does;
        # since P-1 a promise is only kept once its delivery is acknowledged.
        for d in store.all("SELECT id FROM deliveries WHERE state IN ('pending','failed')"):
            clock.now += 2
            store.accept_delivery(d["id"], {"id": d["id"], "accepted": True})

    async def say(text, nonce, source_file, intake_steps, work_steps, before_work=None):
        spec = TaskSpec(text, [{"id": source_file.stem, "kind": "file", "location": str(source_file.resolve()), "required": True, "description": source_file.name}],
                        max_calls=40, max_tools=100, max_seconds=900)
        store.accept_conversation(spec, text=text, nonce=nonce)
        await Runtime(store, data, Scripted(intake_steps)).execute(store.claim_conversation())
        delivered()
        clock.now += 20
        if before_work:
            task = store.one("SELECT id FROM tasks WHERE parent_task_id IS NULL ORDER BY created DESC,rowid DESC LIMIT 1")
            await before_work(task["id"])
        run = store.claim()
        await Runtime(store, data, Scripted(work_steps)).execute(run)
        delivered()
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
        delivered()

        async def reschedule(task_id):
            # Before the work starts the user moves the deadline in the same chat.
            clock.at("15:50")
            promise = store.one("SELECT id FROM commitments WHERE task_id=? AND state='open'", (task_id,))
            store.accept_conversation(TaskSpec("改到五点吧，四点有个会。", [], max_calls=40, max_tools=100, max_seconds=900), text="改到五点吧，四点有个会。", nonce="demo-4b")
            await Runtime(store, data, Scripted([call("resolve_conversation", {"action": "commitment", "commitment_id": promise["id"], "commitment_op": "reschedule",
                                                                                "due_at": f"{DAY} 17:00", "basis": "改到五点吧"}, "c6", (2260, 46))])).execute(store.claim_conversation())
            delivered()
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
        memories = seed_memories(store, clock)
    finally:
        store.close()
    print(json.dumps({"data": str(data), "kept_task": first, "overdue_task": second, "unable_task": third, "rescheduled_task": fourth, "memories": memories}, ensure_ascii=False))


def add_memories(data):
    """Add the demo memories to an existing demo library that has none yet."""
    from personal_agent.store import Store
    if data == (ROOT / ".pa" / "personal").resolve() or not (data / "state.sqlite").is_file():
        raise SystemExit("只给已有的演示库补记忆。")
    store = Store(data / "state.sqlite")
    try:
        if store.one("SELECT COUNT(*) n FROM memories")["n"]:
            raise SystemExit("这个演示库已经有记忆了。")
        clock = Clock(REAL_TIME() - 86400)
        time.time = clock
        try:
            print(json.dumps({"data": str(data), "memories": seed_memories(store, clock)}, ensure_ascii=False))
        finally:
            time.time = REAL_TIME
    finally:
        store.close()


# Demo memory for the galaxy video. All invented; nothing comes from the
# user's own library. (said, [(kind, key, value, keywords, days_to_expiry)])
# — one sentence can hold several memories, which become a "same source" link.
MEMORY_TALK = [
    ("以后汇报先说结论，再说原因，用中文。", [("rule", "先给结论", "汇报先说结论，再说原因", ["汇报"], None), ("rule", "用中文", "回复一律用中文", ["语言"], None)]),
    ("发我的文件都用 Markdown，表格别超过五列。", [("rule", "文件用 Markdown", "交付文件用 Markdown", ["文件", "格式"], None), ("preference", "表格不超过五列", "表格最多五列，再多就拆", ["表格", "格式"], None)]),
    ("改动我没确认之前不要发给别人。", [("rule", "外发先确认", "任何外发内容先给我看", ["外发"], None)]),
    ("周末别给我发提醒，除非真的很急。", [("rule", "周末免打扰", "周末不发提醒，紧急除外", ["提醒", "周末"], None)]),
    ("数字一律写阿拉伯数字，金额带单位。", [("rule", "数字写法", "数字用阿拉伯数字，金额带单位", ["格式", "金额"], None)]),
    ("引用资料要标出处，没出处的别写成事实。", [("rule", "标出处", "引用必须标出处，没出处不写成事实", ["出处"], None)]),
    ("我喝咖啡只喝美式，不加糖。", [("preference", "咖啡", "美式，不加糖", ["咖啡", "饮品"], None)]),
    ("早上脑子最清楚，难的事排上午。", [("preference", "难事排上午", "需要专注的事安排在上午", ["日程"], None), ("about_me", "作息", "早起型，上午效率最高", ["作息", "日程"], None)]),
    ("开会我喜欢 25 分钟一节，别排满。", [("preference", "会议时长", "会议 25 分钟一节，中间留空", ["会议", "日程"], None)]),
    ("清单比长段落好读，能列就列。", [("preference", "多用清单", "能列清单就别写长段落", ["格式"], None)]),
    ("周报我习惯分三段：做了什么、卡在哪、下周做什么。", [("preference", "周报三段", "周报分三段：完成、卡点、下周", ["周报", "汇报"], None)]),
    ("提醒提前一天就够了，别提前一周。", [("preference", "提醒提前量", "提醒提前一天", ["提醒"], None)]),
    ("图表配色用低饱和的，别太花。", [("preference", "图表配色", "低饱和配色", ["图表"], None)]),
    ("我在杭州上班，用的是 Windows 电脑。", [("about_me", "城市", "在杭州上班", ["城市"], None), ("about_me", "电脑", "日常用 Windows", ["电脑"], None)]),
    ("我是做产品的，带一个四个人的小组。", [("about_me", "职业", "产品经理，带四人小组", ["工作"], None)]),
    ("我对花生过敏，订餐要避开。", [("about_me", "过敏", "花生过敏", ["饮食", "订餐"], None)]),
    ("我女儿上小学二年级，周三下午要去接。", [("about_me", "家人", "女儿小学二年级", ["家人"], None), ("about_me", "周三接娃", "每周三下午接女儿", ["家人", "日程"], None)]),
    ("我英语读写可以，口语一般。", [("about_me", "英语", "读写可以，口语一般", ["语言"], None)]),
    ("这周在准备季度复盘，相关的先帮我盯着。", [("ongoing", "季度复盘", "这周准备季度复盘", ["复盘", "汇报"], 3)]),
    ("这个月在搬家，快递先寄公司。", [("ongoing", "搬家", "本月搬家，快递寄公司", ["快递", "地址"], 5)]),
    ("我在学 Python，遇到相关的可以顺带讲讲。", [("ongoing", "学 Python", "正在学 Python", ["学习"], 40)]),
    ("下个月去成都出差三天。", [("ongoing", "成都出差", "下月去成都出差三天", ["出差", "日程"], 25)]),
    ("这两周在控糖，推荐吃的注意一下。", [("ongoing", "控糖", "这两周控糖", ["饮食"], 2)]),
    ("新版安装包这周四前要交测试组。", [("ongoing", "安装包交付", "周四前交测试组", ["工作"], 1)]),
    ("公司地址是西湖区文三路 100 号。", [("reference", "公司地址", "西湖区文三路 100 号", ["地址", "快递"], None)]),
    ("报销走财务系统，链接是 finance.example.com。", [("reference", "报销入口", "finance.example.com", ["报销"], None)]),
    ("周会固定周一十点，会议室 3 楼 A。", [("reference", "周会", "周一 10:00，3 楼 A", ["会议"], None)]),
    ("常用的快递是顺丰。", [("reference", "快递", "常用顺丰", ["快递"], None)]),
    ("测试组的联系人是阿周。", [("reference", "测试组联系人", "阿周", ["工作"], None)]),
    ("设计稿放在团队网盘的「设计」文件夹。", [("reference", "设计稿位置", "团队网盘 / 设计", ["文件"], None)]),
]
MEMORY_LATER = [  # the user changes their mind: old version → wreck with an arrow to the new one
    ("周报改成两段吧，卡点合并进做了什么。", "周报三段", "preference", "周报两段", "周报分两段：进展（含卡点）、下周", ["周报", "汇报"]),
    ("快递以后改用京东了。", "快递", "reference", "快递（新）", "常用京东", ["快递"]),
    ("咖啡我现在改喝拿铁了，少糖。", "咖啡", "preference", "咖啡（新）", "拿铁，少糖", ["咖啡", "饮品"]),
]
MEMORY_PENDING = [  # waiting for the user: dim, dashed stars
    {"kind": "preference", "key": "回复长度", "value": "日常回复别超过一屏", "keywords": ["格式"]},
    {"kind": "about_me", "key": "通勤", "value": "地铁通勤约 40 分钟", "keywords": ["日程"]},
    {"kind": "rule", "key": "截图打码", "value": "截图里的人名要打码", "keywords": ["外发"]},
    {"kind": "reference", "key": "打印机", "value": "3 楼打印机叫 HZ-3F", "keywords": ["文件"]},
    {"kind": "ongoing", "key": "体检", "value": "下周三体检，前一天别排晚饭局", "keywords": ["日程"], "expires": None},
]


def seed_memories(store, clock):
    """~40 demo memories through the real memory API (save, merge, import).

    Memories are said over the last day of real time, so "进行中" items expire
    a few days after the library is built: rebuild before recording if the
    blinking ones have run out.
    """
    clock.now = REAL_TIME() - 86400  # about 14 hours of talk, ending before now
    say = lambda text, nonce: store.submit(None, text=text, nonce=nonce, room="local")["message_id"]
    saved = {}
    for i, (text, items) in enumerate(MEMORY_TALK):
        clock.now += 1200
        mid = say(text, f"mem-{i}")
        for kind, key, value, keywords, days in items:
            expires = clock.now + days * 86400 if days else None
            saved[key] = store.save_memory("local", key=key, value=value, purpose="演示记忆", kind=kind, source_ref=mid, keywords=keywords,
                                           expires_at=expires, evidence=[{"message_id": mid, "quote": text[:200]}])
    # One expired ongoing item stays as a dim star.
    clock.now += 600
    mid = say("上周在赶年中报告，先别安排别的。", "mem-expired")
    store.save_memory("local", key="赶年中报告", value="上周在赶年中报告", purpose="演示记忆", kind="ongoing", source_ref=mid, keywords=["汇报"],
                      expires_at=clock.now - 86400, evidence=[{"message_id": mid, "quote": "上周在赶年中报告，先别安排别的。"}])
    for i, (text, old_key, kind, key, value, keywords) in enumerate(MEMORY_LATER):
        clock.now += 3600
        mid = say(text, f"mem-later-{i}")
        new = store.save_memory("local", key=key, value=value, purpose="演示记忆", kind=kind, source_ref=mid, keywords=keywords,
                                evidence=[{"message_id": mid, "quote": text}])
        old = saved[old_key]
        # Confirmed tidy-sheet merge: the old item becomes a tombstone the new one supersedes.
        pid = store._proposal("personal:local-user", op="merge", key=key, value=value, kind=kind,
                              targets=[{"id": old["id"], "version": old["version"], "key": old_key}, {"id": new["id"], "version": new["version"], "key": key}],
                              reason="你后来换了说法", evidence=[{"message_id": mid, "quote": text}])
        store.decide_memory(pid, accept=True)
    # A corrected value gives a star more than one version.
    store.save_memory("local", key="会议时长", value="会议 30 分钟一节，中间留 10 分钟", purpose="演示记忆", expected_version=1)
    pending = [{**p, "expires": time.strftime("%Y-%m-%d", time.localtime(clock.now + 9 * 86400))} if p["kind"] == "ongoing" else p for p in MEMORY_PENDING]
    store.import_memory_proposals("local", pending, origin="演示：整理单建议")
    return store.one("SELECT COUNT(*) n FROM memories")["n"] + len(pending)


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


def export(data, out):
    """Build the static read-only site from a scripted demo library (see personal_agent.demo_site)."""
    from personal_agent.demo_site import DemoExportError, export_site
    import subprocess
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError:
        commit = ""
    try:
        manifest = asyncio.run(export_site(data, out, commit=commit))
    except DemoExportError as error:
        raise SystemExit(f"导出失败：{error}")
    print(json.dumps({"out": str(out), "files": len(manifest["files"]), "stories": manifest["stories"]}, ensure_ascii=False))


def site(out, port):
    """Serve an exported site under /noyi/ like GitHub Pages does."""
    from aiohttp import web
    if not (out / "index.html").is_file():
        raise SystemExit(f"{out} 里还没有导出的站点，先运行 export。")
    app = web.Application()
    app.router.add_get("/", lambda r: web.HTTPFound("/noyi/"))
    async def page(request):
        rel = request.match_info["path"] or "index.html"
        target = (out / rel).resolve()
        if target.is_dir():
            target = target / "index.html"
        if not target.is_relative_to(out) or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)
    app.router.add_get("/noyi/{path:.*}", page)
    print(f"静态演示站：http://127.0.0.1:{port}/noyi/", flush=True)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "serve", "memories", "export", "site"])
    parser.add_argument("--data", type=Path, default=ROOT / ".pa" / "demo-replay")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "site")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    data, out = args.data.resolve(), args.out.resolve()
    if data == (ROOT / ".pa" / "personal").resolve():
        raise SystemExit("演示数据不能放进自用库 .pa/personal。")
    {"build": build, "memories": add_memories, "export": lambda d: export(d, out), "site": lambda d: site(out, args.port or 8791)}.get(
        args.command, lambda d: serve(d, args.port or 8790))(data)
