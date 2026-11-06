"""Opt-in paid Qwen check of conversation wording (X-5/X-7). No retries.

Sends a few fixed messages through real intake and prints what the user would
see: model receipts, template fallbacks, remember decisions. Structural only.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from personal_agent.contracts import TaskSpec
from personal_agent.model import ModelConfig, QwenModel
from personal_agent.runtime import Runtime
from personal_agent.store import Store

REPO = Path(__file__).resolve().parents[1]
MESSAGES = [
    "帮我调研一下最近三个月开源 Agent 框架的变化，整理成一页对比",
    "记住，以后交付先写一句结论，再列三条要点，链接放最后",
    "这次报告先写结论吧",
    "你好，今天忙吗",
    "以后每天早上九点提醒我看一下周报",
]


async def main(out):
    out.mkdir(parents=True, exist_ok=False)
    store = Store(out / "state.sqlite")
    model = QwenModel(ModelConfig.load())
    runtime = Runtime(store, out, model)
    rows = []
    try:
        for i, text in enumerate(MESSAGES):
            store.accept_conversation(TaskSpec(text, [], max_calls=40, max_tools=100, max_seconds=900), text=text, nonce=f"m{i}")
            run = store.claim_conversation()
            started = time.monotonic()
            await runtime.execute(run)
            item = store.one("SELECT decision,state FROM conversation_inputs WHERE message_id=(SELECT id FROM messages WHERE client_nonce=?)", (f"m{i}",))
            reply = store.one("SELECT content FROM deliveries WHERE run_id=?", (run["id"],))
            decision = json.loads(item["decision"] or "{}")
            rows.append({"user": text, "action": decision.get("action"), "model_receipt": decision.get("receipt"),
                         "shown": reply["content"] if reply else None, "state": item["state"], "seconds": round(time.monotonic() - started, 1)})
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
        rows.append({"memories": [dict(m) for m in store.all("SELECT key,value,source_type FROM memories")],
                     "model_calls": len(store.all("SELECT id FROM model_requests"))})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
        (out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        await model.close()
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / ".pa" / ("speech-check-" + str(int(time.time()))))
    asyncio.run(main(parser.parse_args().output.resolve()))
