"""Opt-in paid acceptance run of docs/plan-experience.md section 6 against a fresh web instance.

Starts its own web server (separate data directory and port), drives it over HTTP
the way the Grok probe did, and logs every user-visible line with seconds since
the triggering message. Judgement is done by reading the log, not by this script.

Env: PA_* model settings (see pa.example.toml), DASHSCOPE_API_KEY for search.
--progress-after shortens the 20-minute progress threshold for this instance only.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
SERVER = """
import sys
from personal_agent import speech
speech.PROGRESS_AFTER = float(sys.argv[3])
from personal_agent.model import ModelConfig
from personal_agent.web_tools import web_plugins
from personal_agent.webui import serve_web
serve_web(sys.argv[1], ModelConfig.load(), port=int(sys.argv[2]), plugins=web_plugins(None, sys.argv[1]))
"""


class Probe:
    def __init__(self, base, log):
        self.base, self.log = base, log
        self.token = self.get("/api/bootstrap")["token"]
        self.seen = set()

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=60))

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-PA-Token": self.token})
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as exc:
            return {"http_error": exc.code, "body": exc.read().decode("utf-8", "replace")[:300]}

    def record(self, step, **row):
        row = {"step": step, "at": round(time.time(), 1), **row}
        self.log.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.log.flush()
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def send(self, step, text, **extra):
        self.t0 = time.time()
        result = self.post("/api/messages", {"text": text, "nonce": uuid.uuid4().hex, "mode": "auto", **extra})
        self.record(step, user=text, submit=result)
        return result

    def visible(self):
        """New assistant-visible lines: deliveries, questions, goal events."""
        state = self.get("/api/state")
        out = []
        for d in state["deliveries"]:
            if d["id"] not in self.seen:
                self.seen.add(d["id"])
                out.append({"type": "delivery", "kind": d["kind"], "task_id": d["task_id"], "content": d["content"],
                            "artifact": bool(d["artifact"]), "version": (d["artifact"] or {}).get("version")})
        for q in state["questions"]:
            key = "q:" + q["id"] + ":" + q["state"]
            if key not in self.seen:
                self.seen.add(key)
                out.append({"type": "question", "id": q["id"], "task_id": q["task_id"], "question": q["question"], "options": q["options"], "state": q["state"]})
        for g in state["goals"]:
            key = "g:" + g["id"] + ":" + str(g.get("state")) + ":" + str(len(g.get("occurrences") or []))
            if key not in self.seen:
                self.seen.add(key)
                out.append({"type": "goal", "title": g.get("title"), "state": g.get("state"),
                            "occurrences": [(o["status"], o["reason"]) for o in g.get("occurrences") or []][:3]})
        return state, out

    def watch(self, step, seconds, until=None):
        """Record visible lines for up to `seconds`; stop early when until(state, lines) is true."""
        end, lines = time.time() + seconds, []
        while time.time() < end:
            state, new = self.visible()
            for row in new:
                self.record(step, t=round(time.time() - self.t0, 1), **row)
            lines += new
            if until and until(state, lines):
                break
            time.sleep(2)
        return self.get("/api/state"), lines

    def task(self, task_id):
        return next((t for t in self.get("/api/state")["tasks"] if t["id"] == task_id), None)


def delivered(task_id):
    return lambda state, lines: any(r.get("artifact") and r.get("task_id") == task_id for r in lines) or \
        any(t["id"] == task_id and t["status"] in {"failed", "cancelled", "stopped"} for t in state["tasks"])


def root_task(probe, after):
    tasks = [t for t in probe.get("/api/state")["tasks"] if t["created"] >= after and not t.get("parent_task_id")]
    return tasks[-1]["id"] if tasks else None


def wait_root(probe, after, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        tid = root_task(probe, after)
        if tid:
            return tid
        probe.watch("wait-task", 2)
    return None


def run(probe, steps, watch_file):
    p = probe
    if "S1" in steps:
        p.send("S1", "你好，今天星期几？")
        p.watch("S1", 40)
    if "S2" in steps:
        t = time.time()
        p.send("S2", "帮我调研一下 2026 年主流的开源 agent 框架，整理成一页对比表，做完发我")
        tid = wait_root(p, t)
        p.watch("S2", 25)
        p.send("S3", "顺便问下，北京今天天气怎么样？")
        p.watch("S3", 40, until=lambda s, l: any(r.get("task_id") != tid for r in l if r["type"] == "delivery"))
        p.t0 = t
        p.watch("S2", 900, until=delivered(tid))
        p.record("S2", final=p.task(tid) and {k: p.task(tid)[k] for k in ("status", "acceptance")}, task_id=tid)
    if "S4" in steps:
        t = time.time()
        p.send("S4", "帮我整理一份 Python、Go、Rust 三门语言的对比，从性能、学习曲线、生态三方面写，做完发我")
        tid = wait_root(p, t)
        p.watch("S4", 15)
        p.send("S4", "改一下需求：再加上 Java，并且加一列“适合的场景”")
        p.watch("S4", 900, until=lambda s, l: any(r.get("artifact") for r in l))
        p.watch("S4", 240, until=lambda s, l: all(x["status"] not in {"running", "queued", "waiting"} for x in s["tasks"] if x["created"] >= t))
        p.record("S4", original_task=tid, tasks=[(x["id"], x["status"], x.get("prior_task_id")) for x in p.get("/api/state")["tasks"] if x["created"] >= t])
    if "S5" in steps:
        t = time.time()
        p.send("S5", "帮我写一篇 1500 字左右的文章，讲远程办公对团队协作的影响，做完发我")
        tid = wait_root(p, t)
        p.watch("S5", 12)
        p.send("S5", "先停一下")
        p.watch("S5", 40)
        p.send("S5", "继续吧")
        p.watch("S5", 900, until=delivered(tid))
        p.record("S5", final=p.task(tid) and p.task(tid)["status"])
    if "S6" in steps:
        p.send("S6", r"帮我读一下 D:\不存在的目录\季度数据.csv，算出总销售额告诉我")
        p.watch("S6", 300, until=lambda s, l: any(r["type"] == "delivery" and r.get("task_id") for r in l[1:]) or any(r.get("artifact") for r in l))
    if "S7" in steps:
        t = time.time()
        p.send("S7", "帮我写一份下周团队周会的议程。你需要什么信息先问我，问清楚再写。")
        state, lines = p.watch("S7", 180, until=lambda s, l: any(r["type"] == "question" and r["state"] == "pending" for r in l))
        p.record("S7", note="quiet window: checking for nagging for 150s")
        p.watch("S7", 150)
        q = next((r for r in lines if r["type"] == "question"), None)
        if q:
            p.t0 = time.time()
            p.record("S7", answer="只答一部分")
            p.post("/api/actions/answer", {"question_id": q["id"], "text": "周会时长 30 分钟", "nonce": uuid.uuid4().hex})
            p.watch("S7", 240, until=lambda s, l: any(r["type"] == "question" and r["state"] == "pending" for r in l) or any(r.get("artifact") for r in l))
    if "S8" in steps:
        p.send("S8", "两分钟后提醒我喝水")
        p.watch("S8", 200)
        p.send("S8", "每天早上九点提醒我看周报")
        p.watch("S8", 60)
        p.send("S8", "把每天看周报的那个提醒删掉")
        p.watch("S8", 60)
        p.record("S8", goals=[(g.get("title"), g.get("state")) for g in p.get("/api/state")["goals"]])
    if "R1" in steps:
        p.send("R1", "记住，以后交付先写一句结论，再列三条要点，链接放最后")
        p.watch("R1", 40)
        t = time.time()
        p.send("R1", "帮我简单介绍一下 SQLite 的 WAL 模式，做完发我")
        tid = wait_root(p, t)
        p.watch("R1", 600, until=delivered(tid))
    if "R3" in steps:
        t = time.time()
        p.send("R3", "帮我整理一份家用 NAS 选购建议，做完发我")
        tid = wait_root(p, t)
        p.watch("R3", 10)
        for text in ["对了", "我预算大概三千", "主要存照片和电影"]:
            p.send("R3", text)
            time.sleep(1.5)
        p.watch("R3", 900, until=delivered(tid))
    if "R4" in steps:
        t = time.time()
        p.send("R4", "帮我调研一下 2026 年值得关注的三款笔记软件，做完发我")
        tid = wait_root(p, t)
        p.watch("R4", 10)
        p.send("R4", "另外我电脑开机好慢，大概要三分钟")
        p.watch("R4", 40)
        p.send("R4", "算了，开机慢这个你别管了")
        p.watch("R4", 900, until=delivered(tid))
        p.record("R4", final=p.task(tid) and p.task(tid)["status"])
    if "R5" in steps:
        watch_file.write_text("版本 1：初始内容。\n", encoding="utf-8")
        p.send("R5", f"帮我盯一下 {watch_file} 这个文件，有变化才告诉我变了什么，没变化别打扰我")
        p.watch("R5", 60)
        p.t0 = time.time()
        p.record("R5", note="no change for 180s; expect zero messages")
        p.watch("R5", 180)
        watch_file.write_text("版本 1：初始内容。\n版本 2：新增一行，负责人改为周宁。\n", encoding="utf-8")
        p.t0 = time.time()
        p.record("R5", note="file changed")
        p.watch("R5", 300, until=lambda s, l: any(r["type"] == "delivery" for r in l))
    if "R6" in steps:
        p.send("R6", "你之前那份 agent 框架对比表写错了，LangGraph 其实是微软出的")
        p.watch("R6", 300, until=lambda s, l: any(r["type"] == "delivery" for r in l[1:]) or any(r.get("artifact") for r in l))
    if "R7" in steps:
        p.send("R7", "那份对比表里的 GitHub star 数是什么时候的数据？准吗？")
        p.watch("R7", 600, until=lambda s, l: any(r.get("artifact") for r in l) or len([r for r in l if r["type"] == "delivery"]) >= 2)
    if "X15" in steps:
        t = time.time()
        p.send("X15", "帮我深入调研 2026 年开源向量数据库的现状，至少比较 6 款，每款都要查官网核实最新版本和许可证，整理成详细报告，做完发我")
        tid = wait_root(p, t)
        p.watch("X15", 1500, until=delivered(tid))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / ".pa" / ("acceptance-" + str(int(time.time()))))
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--progress-after", type=float, default=120)
    parser.add_argument("--steps", default="S1,S2,S4,S5,S6,S7,S8,R1,R3,R4,R5,R6,R7,X15")
    parser.add_argument("--reuse", action="store_true", help="reuse an existing output directory and running server")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=args.reuse)
    server = None
    if not args.reuse:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO / "src")}
        server = subprocess.Popen([sys.executable, "-c", SERVER, str(out / "data"), str(args.port), str(args.progress_after)], cwd=REPO, env=env,
                                  stdout=open(out / "server.log", "ab"), stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/bootstrap", timeout=2)
                break
            except OSError:
                time.sleep(1)
    try:
        with open(out / "log.jsonl", "a", encoding="utf-8") as log:
            probe = Probe(f"http://127.0.0.1:{args.port}", log)
            run(probe, set(args.steps.split(",")), out / "watched.txt")
    finally:
        if server:
            try:
                Probe(f"http://127.0.0.1:{args.port}", open(os.devnull, "w")).post("/api/shutdown", {})
                server.wait(timeout=30)
            except Exception:
                server.kill()


if __name__ == "__main__":
    main()
