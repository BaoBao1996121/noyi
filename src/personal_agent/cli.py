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
from .plugins import configured_plugins
from .web_tools import web_plugins
from .service import Service, ServiceLock
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
    s = sub.add_parser("followup", help="Attach a durable addition to an existing task")
    s.add_argument("task_id")
    s.add_argument("text")
    s.add_argument("--nonce")
    s.add_argument("--context-only", action="store_true")
    s = sub.add_parser("control", help="Stop a reply, stop one task, or cancel the whole delegation")
    s.add_argument("task_id")
    s.add_argument("kind", choices=["stop_reply", "stop_task", "cancel"])
    s.add_argument("--run-id")
    s = sub.add_parser("answer", help="Answer a persisted question by identity")
    s.add_argument("question_id")
    s.add_argument("text")
    s.add_argument("--nonce")
    sub.add_parser("questions")
    s = sub.add_parser("serve")
    s.add_argument("--config", type=Path)
    s.add_argument("--once", action="store_true")
    s = sub.add_parser("web", help="Start the local WebUI and background worker service")
    s.add_argument("--config", type=Path)
    s.add_argument("--port", type=int, default=8765)
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
    s = sub.add_parser("backup", help="Back up managed data without credentials (stop the service first)")
    s.add_argument("--output", type=Path, required=True)
    s = sub.add_parser("memory-import", help="Add memories from a JSON list as pending suggestions; nothing is active until confirmed")
    s.add_argument("file", type=Path)
    s.add_argument("--origin", required=True, help="Shown on each suggestion, e.g. 从 Codex / Claude Code 导入")
    s.add_argument("--room", default="local")
    s = sub.add_parser("restore", help="Restore into a new directory with work paused")
    s.add_argument("archive", type=Path)
    s.add_argument("--destination", type=Path, required=True)
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
        if args.command == "restore":
            from .backup import restore_backup
            print(encode(restore_backup(args.archive, args.destination)))
            return
        if args.command == "backup":
            from .backup import create_backup
            if not (data / "state.sqlite").is_file():
                raise ValueError("数据目录中没有可备份的资料库")
            with ServiceLock(data):
                store = Store(data / "state.sqlite")
                print(encode(create_backup(store, data, args.output)))
            return
        if args.command == "web":
            from .webui import serve_web
            if not 1024 <= args.port <= 65535:
                raise ValueError("port must be between 1024 and 65535")
            serve_web(data, ModelConfig.load(args.config), port=args.port, plugins=configured_plugins(args.config) + web_plugins(args.config, data))
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
                spec = TaskSpec(args.objective, sources, enable_nudge=True, review_workers=True)
            print(encode(store.submit(spec, nonce=args.nonce or uuid.uuid4().hex)))
        elif args.command == "chat":
            print(encode(store.submit(None, text=args.text, nonce=args.nonce or uuid.uuid4().hex)))
        elif args.command == "followup":
            print(encode(store.followup(args.text, task_id=args.task_id, nonce=args.nonce or uuid.uuid4().hex, context=args.context_only)))
        elif args.command == "control":
            print(encode(store.request_control(args.task_id, args.kind, run_id=args.run_id)))
        elif args.command == "answer":
            print(encode(store.answer_attention(args.question_id, args.text, nonce=args.nonce or uuid.uuid4().hex)))
        elif args.command == "questions":
            print(encode(store.all("SELECT id,task_id,question,options,state,answer FROM attentions ORDER BY created")))
        elif args.command in {"resume", "retry"}:
            print(encode(store.resume(args.task_id, extra_calls=args.extra_calls,
                                     extra_tools=args.extra_tools, extra_seconds=args.extra_seconds)))
        elif args.command == "serve":
            config = ModelConfig.load(args.config)
            model = QwenModel(config)
            async def run():
                try:
                    await Service(store, data, model, plugins=configured_plugins(args.config) + web_plugins(args.config, data)).serve(args.once)
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
        elif args.command == "memory-import":
            items = json.loads(args.file.read_text(encoding="utf-8-sig"))
            print(encode(store.import_memory_proposals(args.room, items, origin=args.origin)))
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
