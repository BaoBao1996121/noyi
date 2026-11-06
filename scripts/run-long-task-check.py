"""Opt-in paid Qwen check of a long task's conversation rhythm. No retries.

One user message goes through real intake and real execution (two Workers).
Prints every line the user would see, with seconds since the message.
--progress-after shortens the 20-minute progress threshold for this run only.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from personal_agent import speech
from personal_agent.contracts import TaskSpec
from personal_agent.model import ModelConfig, QwenModel
from personal_agent.service import Service
from personal_agent.store import Store

REPO = Path(__file__).resolve().parents[1]
FIXTURE = {
    "old": "旧版 2026-10-28：河湾工作坊负责人李青；11月8日交资料，11月10日开放报名；需要经理审批。已被新版替代。",
    "current": "现行版本 2026-11-04：河湾工作坊负责人周宁。资料整理截止2026-11-12。报名开放时间尚未确定。作者自查即可归档，不需要经理审批。场地容量未知，不得自行承诺席位。远程参加者不用到现场签到。预算额度尚未提供。",
    "notes": "会议纪要：旧版日期不可沿用。新增事项没有负责人或日期时保留待确认。资料整理截止并不代表活动举行日。",
}
TEXT = "帮我把这三份工作坊资料核对一遍，派两个助手分别核对现行版和旧版变化，再整合成一份行动清单给我。"


async def main(out, progress_after):
    out.mkdir(parents=True, exist_ok=False)
    speech.PROGRESS_AFTER = progress_after
    sources = []
    for sid, text in FIXTURE.items():
        (out / f"{sid}.txt").write_text(text, encoding="utf-8")
        sources.append({"id": sid, "kind": "file", "location": str((out / f"{sid}.txt").resolve()), "required": True})
    store = Store(out / "state.sqlite")
    model = QwenModel(ModelConfig.load())
    service = Service(store, out, model)
    t0 = time.time()
    store.accept_conversation(TaskSpec(TEXT, sources, max_calls=40, max_tools=100, max_seconds=900, max_workers=2, review_workers=True), text=TEXT, nonce="long")
    seen, timeline = set(), []

    async def watch():
        while True:
            # Worker deliveries are internal; the web UI hides them too.
            for d in store.all("SELECT d.id,d.task_id,d.content,d.artifact,d.kind,d.created FROM deliveries d LEFT JOIN tasks t ON t.id=d.task_id WHERE t.parent_task_id IS NULL ORDER BY d.rowid"):
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                row = {"t": round((d["created"] or time.time()) - t0, 1), "kind": d["kind"], "content": d["content"], "artifact": bool(d["artifact"])}
                timeline.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch())
    try:
        await service.run_until_quiescent()
        await asyncio.sleep(1)
    finally:
        watcher.cancel()
        await model.close()
    root = store.one("SELECT id,status FROM tasks WHERE parent_task_id IS NULL")
    summary = {"root_status": root["status"], "workers": store.one("SELECT COUNT(*) n FROM tasks WHERE parent_task_id IS NOT NULL")["n"],
               "model_calls": len(store.all("SELECT id FROM model_requests")), "seconds": round(time.time() - t0, 1),
               "user_visible_lines": len(timeline)}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    (out / "timeline.json").write_text(json.dumps({"timeline": timeline, "summary": summary}, ensure_ascii=False, indent=1), encoding="utf-8")
    store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / ".pa" / ("long-task-check-" + str(int(time.time()))))
    parser.add_argument("--progress-after", type=float, default=speech.PROGRESS_AFTER)
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve(), args.progress_after))
