from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import uuid

from .contracts import TaskSpec, encode
from .model import ModelConfig, QwenModel
from .service import Service
from .store import Store


def load_spec(path: Path) -> TaskSpec:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    for source in data.get("sources", []):
        if source["kind"] == "file":
            source["location"] = str((path.parent / source["location"]).resolve())
    spec = TaskSpec(**data)
    spec.validate()
    return spec


def parser():
    p = argparse.ArgumentParser(prog="pa")
    p.add_argument("--data", type=Path, default=Path(".pa"))
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("submit", help="Persist a task; serve executes it")
    s.add_argument("objective", nargs="?")
    s.add_argument("--spec", type=Path)
    s.add_argument("--source", action="append", default=[], help="Authorized UTF-8 local file")
    s.add_argument("--nonce", default=None)
    c = sub.add_parser("chat", help="Persist a chat-only message without creating a task")
    c.add_argument("text")
    c.add_argument("--nonce", default=None)
    s = sub.add_parser("serve")
    s.add_argument("--config", type=Path)
    s.add_argument("--once", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("task_id", nargs="?")
    s.add_argument("--detail", action="store_true")
    s = sub.add_parser("resume", aliases=["retry"], help="Explicitly resume a paused/failed/review task with its remaining budget")
    s.add_argument("task_id")
    s.add_argument("--extra-calls", type=int, default=0)
    s.add_argument("--extra-tools", type=int, default=0)
    s.add_argument("--extra-seconds", type=float, default=0)
    sub.add_parser("inbox")
    s = sub.add_parser("model-config", help="Show effective non-secret configuration without contacting the provider")
    s.add_argument("--config", type=Path)
    return p


def main():
    args = parser().parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    data = args.data.resolve()
    store = None
    try:
        if args.command == "model-config":
            print(encode(asdict(ModelConfig.load(args.config))))
            return
        store = Store(data / "state.sqlite")
        if args.command == "submit":
            if args.spec:
                spec = load_spec(args.spec)
            else:
                if not args.objective:
                    raise ValueError("provide an objective or --spec")
                sources = [{"id": f"source{i+1}", "kind": "file", "location": str(Path(p).resolve()), "required": True}
                           for i, p in enumerate(args.source)]
                spec = TaskSpec(args.objective, sources)
            print(encode(store.submit(spec, nonce=args.nonce or uuid.uuid4().hex)))
        elif args.command == "chat":
            print(encode(store.submit(None, text=args.text, nonce=args.nonce or uuid.uuid4().hex)))
        elif args.command in {"resume", "retry"}:
            print(encode(store.resume(args.task_id, extra_calls=args.extra_calls,
                                     extra_tools=args.extra_tools, extra_seconds=args.extra_seconds)))
        elif args.command == "serve":
            config = ModelConfig.load(args.config)
            model = QwenModel(config)
            async def run():
                try:
                    await Service(store, data, model).serve(args.once)
                finally:
                    await model.close()
            print("服务开始执行；退出本进程将停止执行，已接收的任务会保留。", flush=True)
            asyncio.run(run())
        elif args.command == "status":
            if args.task_id:
                result = store.task(args.task_id)
                result["deliveries"] = store.all("SELECT * FROM deliveries WHERE task_id=?", (args.task_id,))
                if args.detail:
                    result["runs"] = store.all("SELECT * FROM runs WHERE task_id=?", (args.task_id,))
                    result["operations"] = store.operations(args.task_id)
                    result["steps"] = store.all("SELECT * FROM steps WHERE task_id=?", (args.task_id,))
                    result["model_requests"] = store.all(
                        "SELECT m.* FROM model_requests m JOIN runs r ON r.id=m.run_id WHERE r.task_id=? ORDER BY m.created,m.sequence",
                        (args.task_id,))
                    result["input_usage"] = store.all(
                        "SELECT u.* FROM input_usage u JOIN runs r ON r.id=u.run_id WHERE r.task_id=?", (args.task_id,))
                print(encode(result))
            else:
                print(encode(store.all("SELECT id,status,reason,acceptance,calls_used,tools_used FROM tasks ORDER BY created")))
        elif args.command == "inbox":
            print(encode(store.all("SELECT id,task_id,content,artifact,state,receipt FROM deliveries ORDER BY rowid")))
    except KeyboardInterrupt:
        print("服务已停止；已接收的任务和进度已保存，下次启动时恢复。", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, KeyError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    finally:
        if store:
            store.close()
