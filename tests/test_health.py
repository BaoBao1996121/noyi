"""Weight-loss assistant: code does every calculation, the model only copies or estimates."""
import json
import time

import pytest

from conftest import ScriptedModel, tool_reply
from personal_agent.contracts import ModelReply, TaskSpec
from personal_agent.health import (HealthError, alert_lines, daily_report, health_plugin, label_kcal, local_day, parse_local)
from personal_agent.service import Service


ACCOUNT = "local-user"


def profile(store, sex="female", height=165, birth_year=1994):
    store.set_health_profile(ACCOUNT, sex=sex, birth_year=birth_year, height_cm=height, target_weight_kg=None, source_message=None)


def label(**changes):
    args = {"food_name": "酸奶", "energy_value": 221, "energy_unit": "kJ", "basis_amount": 100, "basis_unit": "g",
            "protein_g": 1.5, "fat_g": 0.6, "carb_g": 10.2, "portion_amount": 250, "portion_unit": "g"}
    args.update(changes)
    return args


# ---- 读表：模型只抄数，换算全由代码做 -------------------------------------------------

def test_label_kcal_uses_code_for_units_and_portion():
    assert round(label_kcal(label()), 1) == 132.1                       # 221 kJ/100 g × 250 g
    assert round(label_kcal(label(energy_value=284, basis_unit="ml", portion_unit="ml", protein_g=3.1, fat_g=3.7, carb_g=4.8)), 1) == 169.7
    fda = label(energy_value=230, energy_unit="kcal", basis_amount=1, basis_unit="serving", serving_grams=55,
                protein_g=3, fat_g=8, carb_g=37, portion_amount=2, portion_unit="serving")
    assert label_kcal(fda) == 460
    assert label_kcal({**fda, "portion_amount": 110, "portion_unit": "g"}) == 460   # 每份 55 克，吃了 110 克


def test_label_units_that_cannot_be_converted_ask_instead_of_guessing():
    with pytest.raises(HealthError, match="换算不了"):
        label_kcal(label(portion_unit="ml"))


@pytest.mark.parametrize("changes", [
    {"energy_value": 2210},                       # 抄错一位：每 100 克 528 千卡，和三大营养素对不上
    {"energy_value": 4500, "protein_g": None, "fat_g": None, "carb_g": None},   # 超过纯脂肪的能量密度
])
def test_misread_labels_are_refused_by_physical_checks(changes):
    with pytest.raises(HealthError):
        label_kcal(label(**changes))


# ---- 记录、取代与估算偏差 ------------------------------------------------------------

def test_label_after_estimate_supersedes_it_and_records_bias(store):
    at = parse_local("2026-11-13 12:30")
    est = store.record_meal(ACCOUNT, method="estimate", food_name="蛋炒饭", eaten_at=at,
                            items=[{"name": "蛋炒饭", "amount": "一盘约 300 克", "kcal_low": 450, "kcal_high": 690}],
                            basis="按熟米饭和 1 个鸡蛋估算", ask="炒饭用油多吗？")
    lab = store.record_meal(ACCOUNT, method="label", food_name="蛋炒饭", eaten_at=at, label=label(energy_value=700, protein_g=5, fat_g=6, carb_g=23, portion_amount=300),
                            replaces=est["id"])
    meals = store.health_meals(ACCOUNT, local_day(at))
    assert [m["id"] for m in meals] == [lab["id"]]
    bias = store.one("SELECT * FROM health_bias")
    kcal = 700 / 4.184 * 3
    assert bias["in_range"] == 1 and abs(bias["relative_error"] - ((450 + 690) / 2 - kcal) / kcal) < 1e-6
    with pytest.raises(HealthError, match="已经被"):
        store.record_meal(ACCOUNT, method="label", food_name="蛋炒饭", eaten_at=at, label=label(), replaces=est["id"])


def test_estimate_far_beyond_one_meal_needs_confirmed_portion(store):
    big = [{"name": "香蕉", "amount": "约 100 根", "kcal_low": 6400, "kcal_high": 15000}]
    with pytest.raises(HealthError, match="确认份量"):
        store.record_meal(ACCOUNT, method="estimate", food_name="香蕉", eaten_at=time.time(), items=big, basis="按根数估", ask="吃了几根？")
    store.record_meal(ACCOUNT, method="estimate", food_name="香蕉", eaten_at=time.time(), items=big, basis="按根数估", ask="", portion_confirmed=True)


def test_exercise_uses_met_table_and_latest_weight(store):
    with pytest.raises(HealthError, match="体重"):
        store.record_exercise(ACCOUNT, activity="跑步", kind="running", minutes=30, done_at=time.time())
    store.record_metric(ACCOUNT, kind="weight_kg", value=70, measured_at=time.time() - 3600)
    row = store.record_exercise(ACCOUNT, activity="跑步", kind="running", minutes=30, done_at=time.time())
    # 净消耗：扣掉这段时间本来就有的静息消耗（基础消耗里已经算过）
    assert row["method"] == "estimate" and row["kcal"] == pytest.approx((8.0 - 1) * 70 * 0.5)
    device = store.record_exercise(ACCOUNT, activity="跑步", kind="running", minutes=30, done_at=time.time(), device_kcal=312)
    assert device["method"] == "device" and device["kcal"] == 312


# ---- 每晚汇总：准确值和估算值分开，区间一路保留 ----------------------------------------------

def test_daily_report_separates_exact_and_estimated(store):
    profile(store)
    day_at = parse_local("2026-11-13 08:00")
    store.record_metric(ACCOUNT, kind="weight_kg", value=70, measured_at=day_at)
    store.record_meal(ACCOUNT, method="label", food_name="酸奶", eaten_at=day_at, label=label())
    store.record_meal(ACCOUNT, method="estimate", food_name="牛肉面", eaten_at=day_at + 4 * 3600,
                      items=[{"name": "牛肉面", "amount": "一大碗", "kcal_low": 520, "kcal_high": 980}], basis="按碗大小", ask="汤喝了几成？")
    store.record_exercise(ACCOUNT, activity="快走", kind="brisk_walking", minutes=60, done_at=day_at + 9 * 3600)
    report = daily_report(store, ACCOUNT, "2026-11-13")
    bmr = 10 * 70 + 6.25 * 165 - 5 * (2026 - 1994) - 161
    assert report["intake"] == {"exact": pytest.approx(132.1, abs=0.1), "low": pytest.approx(652.1, abs=0.1), "high": pytest.approx(1112.1, abs=0.1),
                                "exact_meals": 1, "estimated_meals": 1}
    assert report["burn"]["base"] == pytest.approx(bmr * 1.2)
    assert report["burn"]["exercise_estimated"] == pytest.approx((4.3 - 1) * 70)
    assert report["deficit"]["low"] == pytest.approx(report["burn"]["total"] - report["intake"]["high"])
    text = report["text"]
    assert "读表" in text and "估算" in text and "汤喝了几成" in text
    assert "诊断" not in text


def test_report_says_what_is_missing_instead_of_inventing(store):
    report = daily_report(store, ACCOUNT, "2026-11-13")
    assert report["deficit"] is None
    assert "还没有记录饮食" in report["text"] and "身高" in report["text"]


# ---- 缺口过大提醒：代码判断，固定措辞，一周一次 ---------------------------------------------

def _week_of_low_intake(store, start="2026-11-07"):
    profile(store, sex="male", height=178)
    base = parse_local(start + " 08:00")
    store.record_metric(ACCOUNT, kind="weight_kg", value=90, measured_at=base - 8 * 86400)
    store.record_metric(ACCOUNT, kind="weight_kg", value=88.5, measured_at=base - 1 * 86400)
    store.record_metric(ACCOUNT, kind="weight_kg", value=87, measured_at=base + 6 * 86400)
    for d in range(7):
        for meal in range(2):
            store.record_meal(ACCOUNT, method="estimate", food_name="沙拉", eaten_at=base + d * 86400 + meal * 4 * 3600,
                              items=[{"name": "沙拉", "amount": "一碗", "kcal_low": 300, "kcal_high": 400}], basis="按碗估", ask="")


def test_alerts_fire_on_each_rule_with_fixed_wording(store):
    _week_of_low_intake(store)
    kinds = {a["kind"] for a in alert_lines(store, ACCOUNT, "2026-11-13")}
    assert kinds == {"avg_deficit", "low_intake", "fast_loss"}
    for a in alert_lines(store, ACCOUNT, "2026-11-13"):
        assert "诊断" not in a["text"] and "医生" in a["text"]


def test_alerts_are_sent_at_most_once_a_week_per_kind(store):
    _week_of_low_intake(store)
    tid = store.submit(TaskSpec("每晚汇总"), nonce="n")["task_id"]
    store.enable_nightly(ACCOUNT, "local", at="21:30", task_id=tid, quote="每晚汇总")
    first = store.tick_health(now=parse_local("2026-11-13 21:31"))
    again = store.tick_health(now=parse_local("2026-11-14 21:31"))
    sent = store.all("SELECT kind FROM deliveries WHERE kind IN ('health','health_alert') ORDER BY created")
    assert first["alerts"] == 3 and again["alerts"] == 0
    assert [r["kind"] for r in sent].count("health") == 2
    assert store.tick_health(now=parse_local("2026-11-14 22:00"))["reports"] == 0   # 同一天不重复发


def test_nightly_summary_registers_a_standing_commitment_and_late_send_says_so(store):
    run_task = store.submit(TaskSpec("每晚汇总"), nonce="n")["task_id"]
    row = store.enable_nightly(ACCOUNT, "local", at="21:30", task_id=run_task, quote="以后每晚九点半帮我汇总")
    c = store.commitment(row["commitment_id"])
    assert c["kind"] == "standing" and c["state"] == "open" and c["due_at"] is None
    store.tick_health(now=parse_local("2026-11-13 23:50"))
    text = store.one("SELECT content FROM deliveries WHERE kind='health'")["content"]
    assert "补发" in text
    store.disable_nightly(ACCOUNT)
    assert store.commitment(row["commitment_id"])["state"] == "released"


# ---- 工具只对私聊开放，所说的份量必须是用户原话 ---------------------------------------------

def call_tool(name, args, call_id="h1"):
    return tool_reply(name, args, call_id)


async def test_tools_run_through_the_ledger_and_require_user_words(store, tmp_path):
    profile(store)
    record = call_tool("record_meal_label", {**label(), "portion_quote": "吃了 250 克"})
    bad = call_tool("record_meal_label", {**label(), "portion_quote": "吃了一整盒"}, "h2")
    done = call_tool("report_unable", {"explanation": "测试结束，不交付。"}, "end")
    model = ScriptedModel([record, bad, done])
    tid = store.submit(TaskSpec("酸奶 吃了 250 克，记一下"), nonce="meal")["task_id"]
    await Service(store, tmp_path / "data", model, plugins=[health_plugin()]).serve(once=True)
    ops = {o["id"]: o for o in store.operations(tid)}
    good, wrong = [o for o in ops.values() if o["tool"] == "record_meal_label"]
    assert good["status"] == "succeeded" and good["result"]["kcal"] == pytest.approx(132.1, abs=0.1)
    assert wrong["status"] == "failed" and "原话" in wrong["result"]["detail"]
    assert store.one("SELECT COUNT(*) n FROM health_meals")["n"] == 1
    tools = [t["function"]["name"] for t in model.requests[0]["tools"]]
    assert {"record_meal_label", "record_meal_estimate", "record_exercise", "health_report"} <= set(tools)


def test_upgrade_to_v12_backs_up_first_and_keeps_health_out_of_older_copies(tmp_path):
    from personal_agent.store import Store
    path = tmp_path / "data" / "state.sqlite"
    old = Store(path)
    for table in ("health_profile", "health_metrics", "health_meals", "health_exercise", "health_bias", "health_nightly", "health_alerts"):
        old.db.execute(f"DROP TABLE {table}")
    old.db.execute("PRAGMA user_version=11")
    old.close()
    upgraded = Store(path)
    try:
        assert list((path.parent / "backups").glob("state-v11-*.sqlite"))
        assert upgraded.one("PRAGMA user_version")["user_version"] == 12
        assert upgraded.one("SELECT name FROM sqlite_master WHERE name='health_meals'")
    finally:
        upgraded.close()


async def test_health_tools_are_not_offered_in_group_rooms(store, tmp_path):
    room = store.create_group("家人群", ["default"])["id"]
    model = ScriptedModel([call_tool("report_unable", {"explanation": "测试结束，不交付。"}, "end")])
    store.submit(TaskSpec("记一下午饭"), nonce="g", room=room)
    await Service(store, tmp_path / "data", model, plugins=[health_plugin()]).serve(once=True)
    names = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert not names & {"record_meal_label", "record_body", "health_report"}
