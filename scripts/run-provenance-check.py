"""Opt-in paid real-model check of P-1 (fulfil only when delivered) and P-3 (quotes).

Runs in a fresh data directory it creates; never touches .pa/personal.
Uses the model configured for that directory (copied model.json), no retries.

1. A document from a local source, through Runtime with a channel that never
   acknowledges: the work is accepted but the promise stays open and shows
   "done, not delivered". Then normal delivery: the promise is fulfilled with
   the acknowledged delivery.
2. The same task's acceptance: which quotes the model gave, and whether each
   was found verbatim in the version it read.
   The source invites the known over-inference (the lead must write the
   release notes); the check only shows where quotes came from.

Prints one JSON line per step and writes results.json. Structural only: it
does not judge whether the model's conclusions are right.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from personal_agent.contracts import TaskSpec
from personal_agent.model import ModelConfig, QwenModel
from personal_agent.runtime import Runtime
from personal_agent.service import Service
from personal_agent.store import Store

REPO = Path(__file__).resolve().parents[1]
POLICY = """# 发布规则（第 3 版）

1. 每次发布前必须经过一名维护者的人工审核。
2. 安全修复可以先发布，但要在 24 小时内补做审核。
3. 发布负责人是陈语；负责人不必亲自写发布说明，发布说明由当周值班的人写。
4. 本规则不适用于文档仓库。
"""


class SilentChannel:
    async def send(self, delivery):
        raise OSError("simulated channel outage")


def intake(store, text, sources, nonce):
    store.accept_conversation(TaskSpec(text, sources, max_calls=20, max_tools=40, max_seconds=600), text=text, nonce=nonce)


async def run_intake(runtime, store):
    run = store.claim_conversation()
    await runtime.execute(run)


def commitment(store, task_id):
    return store.one("SELECT * FROM commitments WHERE task_id=? ORDER BY created DESC LIMIT 1", (task_id,))


async def main(out, model_dir):
    out.mkdir(parents=True, exist_ok=False)
    source = out / "policy.md"
    source.write_text(POLICY, encoding="utf-8")
    store = Store(out / "state.sqlite")
    # Same model and key as the desktop instance; the key stays in the environment, never printed or copied.
    config = ModelConfig(**json.loads((model_dir / "model.json").read_text(encoding="utf-8")))
    key_file = model_dir / "credentials.key"
    if not os.environ.get(config.api_key_env) and key_file.is_file():
        os.environ[config.api_key_env] = key_file.read_text(encoding="utf-8").strip()
    model = QwenModel(config)
    runtime = Runtime(store, out, model)
    rows = []

    def report(step, **data):
        rows.append({"step": step, **data})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    try:
        src = [{"id": "policy", "kind": "file", "location": str(source.resolve()), "required": True, "description": "发布规则"}]
        intake(store, "看一下这份发布规则，用三条要点告诉我发布前要做什么、谁负责什么。", src, "p1")
        await run_intake(runtime, store)
        task = store.one("SELECT * FROM tasks WHERE parent_task_id IS NULL ORDER BY created DESC LIMIT 1")
        run = store.claim()
        await runtime.execute(run)
        task = store.task(task["id"])
        await Service(store, out, None, channel=SilentChannel()).deliver()
        c = commitment(store, task["id"])
        items = {i["id"]: i for i in store.commitment_items("local")}
        accept = [json.loads(o["result"] or "{}") for o in store.all("SELECT result FROM operations WHERE task_id=? AND tool='submit_result' ORDER BY created", (task["id"],))]
        report("1a_channel_down", task_status=task["status"], acceptance=task["acceptance"], commitment=c["state"] if c else None,
               done_not_delivered=items.get(c["id"], {}).get("done_not_delivered") if c else None,
               timeline_outcome=store.task_timeline(task["id"])["outcome"])
        await Service(store, out, None).deliver()
        c = commitment(store, task["id"])
        closing = store.one("SELECT state,content FROM deliveries WHERE id=?", (c["closing_delivery_id"],)) if c and c["closing_delivery_id"] else None
        report("1b_delivered", commitment=c["state"] if c else None, closing_delivery_state=closing["state"] if closing else None,
               evidence_kinds=[e["kind"] for e in json.loads(c["evidence"])] if c else None, timeline_outcome=store.task_timeline(task["id"])["outcome"])
        report("2_quotes", attempts=[{"passed": a.get("passed"), "quotes": a.get("quotes"), "errors": [e for e in a.get("errors", []) if "原文" in e]} for a in accept])
        timeline = store.task_timeline(task["id"])
        accepted = [dict(e["facts"]) for e in timeline["events"] if e["kind"] == "accepted"]
        memory = [e for e in timeline["events"] if e["kind"] == "memory_used"]
        report("2_replay", accepted_facts=accepted, memory_events=len(memory))
        report("usage", model_requests=len(store.all("SELECT id FROM model_requests")))
        (out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        await model.close()
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=REPO / ".pa" / "personal")
    args = parser.parse_args()
    asyncio.run(main(args.out, args.model_dir))
