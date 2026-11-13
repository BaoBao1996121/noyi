"""Scripted diet demo for checking the phone layout (never touches .pa/personal).

    python scripts/demo-health.py build --data .pa/demo-health
    python scripts/demo-health.py serve --data .pa/demo-health --port 8792

The meals, numbers and replies are made up; no model is called.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from personal_agent.contracts import TaskSpec  # noqa: E402
from personal_agent.health import daily_report, local_day  # noqa: E402

LABEL = {"energy_value": 284, "energy_unit": "kJ", "basis_amount": 100, "basis_unit": "ml", "protein_g": 3.1, "fat_g": 3.7, "carb_g": 4.8,
         "portion_amount": 250, "portion_unit": "ml"}


def build(data):
    from personal_agent.store import Store
    data.mkdir(parents=True, exist_ok=True)
    if (data / "state.sqlite").exists():
        raise SystemExit("演示库已存在，换一个目录或先删掉它")
    store = Store(data / "state.sqlite")
    try:
        now = time.time()
        account = "local-user"
        store.set_health_profile(account, sex="male", birth_year=1996, height_cm=175, target_weight_kg=72)
        store.record_metric(account, kind="weight_kg", value=79.2, measured_at=now - 7 * 86400)
        store.record_metric(account, kind="weight_kg", value=78.6, measured_at=now - 3600 * 12)

        def say(user, reply, kind="result"):
            tid = store.submit(TaskSpec(user), nonce="demo-" + user[:12], source="web")["task_id"]
            store.db.execute("UPDATE tasks SET status='succeeded',acceptance='passed' WHERE id=?", (tid,))
            did = store._notice(tid, "local", reply, kind=kind)
            store.db.execute("UPDATE deliveries SET state='accepted' WHERE id=?", (did,))
            return tid

        say("早上一盒牛奶，拍了成分表，喝了 250 毫升", "记好了：牛奶 250 毫升，**读表** 170 千卡（每 100 毫升 284 千焦 × 2.5）。")
        store.record_meal(account, method="label", food_name="牛奶", eaten_at=now - 3600 * 10, label=LABEL)
        say("午饭一碗牛肉面（照片）", "记好了：牛肉面 **估算** 520–980 千卡。依据：碗口约 18 厘米，面约 150–200 克，红油较多。\n汤你喝了几成？说一声我把区间收窄。")
        store.record_meal(account, method="estimate", food_name="牛肉面", eaten_at=now - 3600 * 6,
                          items=[{"name": "牛肉面", "amount": "一大碗，碗口约 18 厘米", "kcal_low": 520, "kcal_high": 980}],
                          basis="按碗大小和可见红油估算，最大的不确定是汤喝了多少", ask="汤喝了几成？")
        say("下班快走了 40 分钟", "记好了：快走 40 分钟，**估算** 消耗约 174 千卡（按体重 78.6 公斤）。")
        store.record_exercise(account, activity="快走", kind="brisk_walking", minutes=40, done_at=now - 3600 * 2)
        report = daily_report(store, account, local_day(now))
        tid = store.submit(TaskSpec("每晚汇总"), nonce="demo-nightly")["task_id"]
        store.enable_nightly(account, "local", at="21:30", task_id=tid, quote="以后每晚九点半帮我汇总")
        did = store._notice(tid, "local", report["text"], kind="health")
        store.db.execute("UPDATE deliveries SET state='pushed' WHERE id=?", (did,))   # pushed to the phone, not seen yet
        print(report["text"])
    finally:
        store.close()


def serve(data, port):
    from aiohttp import web
    from personal_agent.model import ModelConfig
    from personal_agent.store import Store
    from personal_agent.webui import LocalModel, create_app
    store = Store(data / "state.sqlite")
    model = LocalModel(data, ModelConfig.load())
    web.run_app(create_app(store, data, model, run_service=False), host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "serve"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    data = args.data.resolve()
    if data == (ROOT / ".pa" / "personal").resolve():
        raise SystemExit("演示数据不能放进自用库 .pa/personal。")
    build(data) if args.command == "build" else serve(data, args.port)
