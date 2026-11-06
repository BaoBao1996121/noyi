"""Opt-in paid check of a real web task: intake, search, fetch, cited delivery. No retries.

Uses the configured model (PA_* env) and the [web] search key. Prints what the
user would see, the search/fetch counts, and whether the delivery page renders.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from personal_agent.contracts import TaskSpec
from personal_agent.model import ModelConfig, QwenModel
from personal_agent.result_page import render
from personal_agent.service import Service
from personal_agent.store import Store
from personal_agent.web_tools import web_plugins

REPO = Path(__file__).resolve().parents[1]
TEXT = "查一下 Python 3.14 目前最新的补丁版本号和发布日期，给我来源。"


async def main(out, text, config):
    out.mkdir(parents=True, exist_ok=False)
    store = Store(out / "state.sqlite")
    model = QwenModel(ModelConfig.load())
    plugins = web_plugins(config)
    if not plugins[0].tools[0].available(None, {"task_id": "probe"}):
        raise SystemExit("web search key is not configured")
    service = Service(store, out, model, plugins=plugins)
    t0 = time.time()
    store.accept_conversation(TaskSpec(text, [], max_calls=30, max_tools=60, max_seconds=600), text=text, nonce="web")
    try:
        await service.run_until_quiescent()
    finally:
        await model.close()
    for d in store.all("SELECT d.content,d.kind,d.created FROM deliveries d LEFT JOIN tasks t ON t.id=d.task_id WHERE t.parent_task_id IS NULL OR d.task_id IS NULL ORDER BY d.rowid"):
        print(json.dumps({"t": round(d["created"] - t0, 1), "kind": d["kind"], "content": d["content"]}, ensure_ascii=False))
    root = store.one("SELECT id,status,acceptance FROM tasks WHERE parent_task_id IS NULL")
    ops = store.all("SELECT tool,COUNT(*) n FROM operations WHERE tool IN ('web_search','fetch_page') GROUP BY tool")
    page = render(store, out, root["id"]) if root else None
    print(json.dumps({"status": root and root["status"], "acceptance": root and root["acceptance"], "web": {o["tool"]: o["n"] for o in ops},
                      "model_calls": len(store.all("SELECT id FROM model_requests")), "seconds": round(time.time() - t0, 1),
                      "page_bytes": len(page) if page else None}, ensure_ascii=False))
    store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / ".pa" / ("web-check-" + str(int(time.time()))))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--text", default=TEXT)
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve(), args.text, args.config))
