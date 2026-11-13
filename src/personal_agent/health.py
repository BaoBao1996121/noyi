"""Weight-loss assistant: meals, exercise and body metrics kept only on this machine.

Division of labour (user decision 2026-11-13):
- the model copies numbers from a nutrition label, or estimates a kcal range
  for a meal photo and says why; it never multiplies, sums or diagnoses;
- code converts units, multiplies by the portion the user stated, adds up the
  day, computes the deficit and decides when an alert is due, with fixed words.

Every number keeps its method: 读表 (label), 估算 (estimate) or 设备读数 (device).
These tables are private: ops_export registers every column of them as such.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .authority import verify_user_words
from .contracts import encode
from .plugins import Plugin, ToolSpec
from .tools import tool

TIMEZONE = ZoneInfo("Asia/Shanghai")
KJ_PER_KCAL = 4.184
# A single eaten portion above this needs the user's confirmation (probe:
# a fruit-stall photo was estimated at 6,400–15,000 kcal as "one meal").
MEAL_CONFIRM_KCAL = 2500
# Net MET values (Compendium of Physical Activities, rounded). Resting burn is
# already in the base estimate, so exercise counts MET-1 to avoid double counting.
MET = {"walking": 3.5, "brisk_walking": 4.3, "running": 8.0, "cycling": 6.8, "swimming": 6.0,
       "strength": 5.0, "hiit": 8.0, "yoga": 2.5, "jump_rope": 11.0, "hiking": 6.0,
       "ball_games": 6.5, "dancing": 5.0, "elliptical": 5.0, "rowing": 7.0, "other": 4.0}
ACTIVITY_FACTOR = 1.2           # sedentary; recorded exercise is added on top
# Alert thresholds (user confirmed 2026-11-13; NHS: aim for 0.5–1 kg a week).
AVG_DEFICIT_LIMIT = 1000
LOW_INTAKE = {"female": 1200, "male": 1500}
WEEKLY_LOSS_LIMIT = 1.0
ALERT_COOLDOWN = 7 * 86400
ALERT_TEXT = {
    "avg_deficit": "近 7 天平均每天热量缺口约 {value:,.0f} 千卡，超过 1,000 千卡。常见建议是每周减 0.5～1 公斤，缺口太大不容易坚持，也可能掉肌肉。如果出现乏力、头晕、心慌等不适，建议咨询医生。",
    "low_intake": "{day}记录的摄入约 {value:,.0f} 千卡，低于 {limit:,} 千卡。长期吃得这么少需要专业指导，并不适合所有人。如果身体不适，建议咨询医生。",
    "fast_loss": "最近两周体重每周都下降超过 1 公斤（{detail}）。常见建议是每周 0.5～1 公斤。如果减得这么快并非有意安排，或者感到不适，建议咨询医生。",
}

SQL = [
    """CREATE TABLE IF NOT EXISTS health_profile(account TEXT PRIMARY KEY, sex TEXT, birth_year INTEGER,
        height_cm REAL, target_weight_kg REAL, source_message TEXT, updated REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health_metrics(id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL,
        value REAL NOT NULL, measured_at REAL NOT NULL, source_message TEXT, operation_id TEXT, created REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health_meals(id TEXT PRIMARY KEY, account TEXT NOT NULL, eaten_at REAL NOT NULL,
        day TEXT NOT NULL, food_name TEXT NOT NULL, method TEXT NOT NULL, kcal REAL, kcal_low REAL, kcal_high REAL,
        protein_g REAL, fat_g REAL, carb_g REAL, label TEXT, items TEXT, basis TEXT, ask TEXT, portion_quote TEXT,
        photo_source TEXT, photo_sha256 TEXT, superseded_by TEXT, replaces TEXT, source_message TEXT,
        operation_id TEXT, created REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health_exercise(id TEXT PRIMARY KEY, account TEXT NOT NULL, done_at REAL NOT NULL,
        day TEXT NOT NULL, activity TEXT NOT NULL, kind TEXT NOT NULL, minutes REAL NOT NULL, method TEXT NOT NULL,
        kcal REAL NOT NULL, weight_kg REAL, source_message TEXT, operation_id TEXT, created REAL NOT NULL)""",
    # Estimate vs. a later label for the same meal: only numbers, no food names.
    """CREATE TABLE IF NOT EXISTS health_bias(id TEXT PRIMARY KEY, estimate_id TEXT NOT NULL, label_id TEXT NOT NULL,
        kcal_low REAL NOT NULL, kcal_high REAL NOT NULL, label_kcal REAL NOT NULL, relative_error REAL NOT NULL,
        in_range INTEGER NOT NULL, created REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health_nightly(account TEXT PRIMARY KEY, room_id TEXT NOT NULL, at TEXT NOT NULL,
        commitment_id TEXT, task_id TEXT, state TEXT NOT NULL, last_day TEXT, created REAL NOT NULL, updated REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health_alerts(id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL,
        day TEXT NOT NULL, delivery_id TEXT, created REAL NOT NULL)""",
    # 「这里不对」: who marked which reply, a category, the sentence the user
    # confirmed may leave home, and a private note that never leaves.
    """CREATE TABLE IF NOT EXISTS feedback_marks(id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, task_id TEXT,
        delivery_kind TEXT, category TEXT NOT NULL, note_shared TEXT, note_private TEXT, share INTEGER NOT NULL,
        created REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS health_meals_day ON health_meals(account,day)",
    "CREATE INDEX IF NOT EXISTS health_exercise_day ON health_exercise(account,day)",
    "CREATE INDEX IF NOT EXISTS health_metrics_kind ON health_metrics(account,kind,measured_at)",
]
HEALTH_TABLES = ("health_profile", "health_metrics", "health_meals", "health_exercise", "health_bias", "health_nightly", "health_alerts")


class HealthError(ValueError):
    """Said to the model as the tool's failure: fix the input or ask the user."""


def uid():
    return uuid.uuid4().hex


def local_day(at):
    return datetime.fromtimestamp(at, TIMEZONE).strftime("%Y-%m-%d")


def parse_local(text):
    """'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' in Asia/Shanghai -> epoch seconds."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=TIMEZONE).timestamp()
        except (ValueError, AttributeError):
            continue
    raise HealthError("时间格式要写成 YYYY-MM-DD HH:MM（北京时间）")


def day_bounds(day):
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _number(value, name, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise HealthError(f"{name} 应是 {low}–{high} 之间的数字")
    return float(value)


def label_kcal(label):
    """kcal for the portion eaten, computed only from copied label numbers."""
    energy = _number(label.get("energy_value"), "energy_value", 0, 10000)
    unit = label.get("energy_unit")
    if unit not in {"kJ", "kcal"}:
        raise HealthError("energy_unit 只能是 kJ 或 kcal，照表上写的抄")
    per_basis = energy / KJ_PER_KCAL if unit == "kJ" else energy
    basis_amount = _number(label.get("basis_amount"), "basis_amount", 0.01, 5000)
    basis_unit, portion_unit = label.get("basis_unit"), label.get("portion_unit")
    portion = _number(label.get("portion_amount"), "portion_amount", 0, 50000)
    serving_grams = label.get("serving_grams")
    if basis_unit not in {"g", "ml", "serving"} or portion_unit not in {"g", "ml", "serving"}:
        raise HealthError("计量单位只能是 g、ml 或 serving")
    if basis_unit in {"g", "ml"}:
        per_unit = per_basis / basis_amount
        # Physical sanity: nothing edible is denser than pure fat (~9 kcal/g).
        if per_unit > 9.2:
            raise HealthError("每克/每毫升超过 9 千卡，比纯脂肪还高，多半抄错了数字或单位，请对照照片重抄")
    macros = [label.get(k) for k in ("protein_g", "fat_g", "carb_g")]
    if all(type(m) in (int, float) for m in macros):
        expected = 4 * macros[0] + 9 * macros[1] + 4 * macros[2]
        # Labels round and fibre/alcohol add a little; a wide band still catches a misread digit.
        if expected > 5 and not 0.6 * expected - 15 <= per_basis <= 1.4 * expected + 15:
            raise HealthError(f"能量和三大营养素对不上（按蛋白质、脂肪、碳水算约 {expected:.0f} 千卡，表上折合 {per_basis:.0f} 千卡），请对照照片重抄")
    if basis_unit == portion_unit:
        return per_basis * portion / basis_amount
    if basis_unit == "serving" and portion_unit == "g" and type(serving_grams) in (int, float) and serving_grams > 0:
        return per_basis * portion / (basis_amount * serving_grams)
    if basis_unit == "g" and portion_unit == "serving" and type(serving_grams) in (int, float) and serving_grams > 0:
        return per_basis * portion * serving_grams / basis_amount
    raise HealthError(f"表上按 {basis_unit} 计，你说的份量是 {portion_unit}，换算不了；问用户吃了多少 {basis_unit}")


def bmr(profile, weight, year):
    """Mifflin-St Jeor resting energy; None when a required fact is missing."""
    if not profile or not weight or profile.get("sex") not in LOW_INTAKE or not profile.get("height_cm") or not profile.get("birth_year"):
        return None
    age = year - profile["birth_year"]
    return 10 * weight + 6.25 * profile["height_cm"] - 5 * age + (5 if profile["sex"] == "male" else -161)


class HealthStore:
    """Mixed into Store; schema version 12."""

    def __init__(self, path):
        super().__init__(path)
        with self.transaction():
            for statement in SQL:
                self.db.execute(statement)

    # ---- facts -------------------------------------------------------------
    def set_health_profile(self, account, *, sex=None, birth_year=None, height_cm=None, target_weight_kg=None, source_message=None):
        old = self.one("SELECT * FROM health_profile WHERE account=?", (account,)) or {}
        if sex is not None and sex not in LOW_INTAKE:
            raise HealthError("sex 只能是 female 或 male")
        row = {"sex": sex or old.get("sex"),
               "birth_year": int(_number(birth_year, "birth_year", 1900, 2030)) if birth_year is not None else old.get("birth_year"),
               "height_cm": _number(height_cm, "height_cm", 80, 250) if height_cm is not None else old.get("height_cm"),
               "target_weight_kg": _number(target_weight_kg, "target_weight_kg", 25, 300) if target_weight_kg is not None else old.get("target_weight_kg")}
        self.db.execute("INSERT INTO health_profile(account,sex,birth_year,height_cm,target_weight_kg,source_message,updated) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(account) DO UPDATE SET sex=excluded.sex,birth_year=excluded.birth_year,height_cm=excluded.height_cm,"
                        "target_weight_kg=excluded.target_weight_kg,source_message=excluded.source_message,updated=excluded.updated",
                        (account, row["sex"], row["birth_year"], row["height_cm"], row["target_weight_kg"], source_message, time.time()))
        return row

    def health_profile(self, account):
        return self.one("SELECT * FROM health_profile WHERE account=?", (account,))

    def record_metric(self, account, *, kind, value, measured_at, source_message=None, operation_id=None):
        limits = {"weight_kg": (25, 300), "waist_cm": (30, 250), "body_fat_pct": (2, 70)}
        if kind not in limits:
            raise HealthError("kind 只能是 weight_kg、waist_cm 或 body_fat_pct")
        value = _number(value, kind, *limits[kind])
        row = {"id": uid(), "account": account, "kind": kind, "value": value, "measured_at": measured_at,
               "source_message": source_message, "operation_id": operation_id, "created": time.time()}
        self.db.execute(f"INSERT INTO health_metrics({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return row

    def latest_metric(self, account, kind, before=None):
        before = time.time() + 1 if before is None else before
        return self.one("SELECT * FROM health_metrics WHERE account=? AND kind=? AND measured_at<? ORDER BY measured_at DESC LIMIT 1", (account, kind, before))

    def record_meal(self, account, *, method, food_name, eaten_at, label=None, items=None, basis=None, ask=None,
                    portion_quote=None, photo_source=None, photo_sha256=None, replaces=None, portion_confirmed=False,
                    source_message=None, operation_id=None):
        if not isinstance(food_name, str) or not 1 <= len(food_name.strip()) <= 80:
            raise HealthError("food_name 要写清吃的是什么（80 字以内）")
        row = {"id": uid(), "account": account, "eaten_at": eaten_at, "day": local_day(eaten_at), "food_name": food_name.strip(),
               "method": method, "kcal": None, "kcal_low": None, "kcal_high": None, "protein_g": None, "fat_g": None, "carb_g": None,
               "label": None, "items": None, "basis": None, "ask": None, "portion_quote": portion_quote, "photo_source": photo_source,
               "photo_sha256": photo_sha256, "superseded_by": None, "replaces": replaces, "source_message": source_message,
               "operation_id": operation_id, "created": time.time()}
        if method == "label":
            kcal = label_kcal(label)
            ratio = kcal / (label["energy_value"] / KJ_PER_KCAL if label["energy_unit"] == "kJ" else label["energy_value"]) if label["energy_value"] else 0
            row.update(kcal=kcal, kcal_low=kcal, kcal_high=kcal, label=encode(label),
                       **{k: (label[k] * ratio if type(label.get(k)) in (int, float) else None) for k in ("protein_g", "fat_g", "carb_g")})
        elif method == "estimate":
            if not isinstance(items, list) or not 1 <= len(items) <= 12:
                raise HealthError("items 要列出照片里每样食物（1–12 项）")
            low = high = 0
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("amount"), str):
                    raise HealthError("每项要有 name、amount（份量和依据）、kcal_low、kcal_high")
                lo, hi = _number(item.get("kcal_low"), "kcal_low", 0, 20000), _number(item.get("kcal_high"), "kcal_high", 0, 20000)
                if lo > hi:
                    raise HealthError("kcal_low 不能大于 kcal_high")
                low, high = low + lo, high + hi
            if not isinstance(basis, str) or not basis.strip():
                raise HealthError("basis 要写出估算依据和最大的不确定来源")
            if high > MEAL_CONFIRM_KCAL and not portion_confirmed:
                raise HealthError(f"估出来超过 {MEAL_CONFIRM_KCAL} 千卡，超出一个人一顿的常见范围。先问用户确认份量（这些是不是都吃了），确认后再记")
            row.update(kcal_low=low, kcal_high=high, items=encode(items), basis=basis.strip()[:400], ask=(ask or "").strip()[:200] or None)
        else:
            raise HealthError("method 只能是 label 或 estimate")
        with self.transaction():
            old = None
            if replaces:
                old = self.one("SELECT * FROM health_meals WHERE id=? AND account=?", (replaces, account))
                if not old:
                    raise HealthError("要修正的那条饮食记录不存在")
                if old["superseded_by"]:
                    raise HealthError("那条记录已经被后来的记录取代了，请修正最新的一条")
            self.db.execute(f"INSERT INTO health_meals({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
            if old:
                self.db.execute("UPDATE health_meals SET superseded_by=? WHERE id=?", (row["id"], old["id"]))
                if old["method"] == "estimate" and method == "label" and row["kcal"]:
                    mid = (old["kcal_low"] + old["kcal_high"]) / 2
                    self.db.execute("INSERT INTO health_bias VALUES(?,?,?,?,?,?,?,?,?)",
                                    (uid(), old["id"], row["id"], old["kcal_low"], old["kcal_high"], row["kcal"],
                                     (mid - row["kcal"]) / row["kcal"], int(old["kcal_low"] <= row["kcal"] <= old["kcal_high"]), time.time()))
        return row

    def health_meals(self, account, day):
        return self.all("SELECT * FROM health_meals WHERE account=? AND day=? AND superseded_by IS NULL ORDER BY eaten_at", (account, day))

    def record_exercise(self, account, *, activity, kind, minutes, done_at, device_kcal=None, source_message=None, operation_id=None):
        if kind not in MET:
            raise HealthError("kind 只能是：" + "、".join(MET))
        minutes = _number(minutes, "minutes", 1, 1440)
        weight = self.latest_metric(account, "weight_kg", before=done_at + 86400)
        if device_kcal is not None:
            kcal, method = _number(device_kcal, "device_kcal", 0, 10000), "device"
        else:
            if not weight:
                raise HealthError("还不知道用户体重，算不了运动消耗；先问用户体重，或请用户提供手环上的消耗读数")
            kcal, method = (MET[kind] - 1) * weight["value"] * minutes / 60, "estimate"
        row = {"id": uid(), "account": account, "done_at": done_at, "day": local_day(done_at), "activity": str(activity).strip()[:60] or kind,
               "kind": kind, "minutes": minutes, "method": method, "kcal": kcal, "weight_kg": weight["value"] if weight else None,
               "source_message": source_message, "operation_id": operation_id, "created": time.time()}
        self.db.execute(f"INSERT INTO health_exercise({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return row

    # ---- 「这里不对」 ---------------------------------------------------------
    def mark_feedback(self, delivery_id, *, category, note_shared="", note_private="", share=False, account="local-user"):
        from .ops_export import FEEDBACK_CATEGORIES, NOTE_MAX
        if category not in FEEDBACK_CATEGORIES:
            raise ValueError("请选一类问题")
        if type(share) is not bool or not isinstance(note_shared, str) or not isinstance(note_private, str):
            raise ValueError("标记格式不对")
        if len(note_shared.strip()) > NOTE_MAX or len(note_private.strip()) > 2000:
            raise ValueError(f"外传的说明最多 {NOTE_MAX} 字")
        with self.transaction():
            d = self.one("SELECT d.id,d.task_id,d.kind,d.room_id FROM deliveries d JOIN rooms r ON r.id=d.room_id WHERE d.id=? AND r.owner=?", (delivery_id, account))
            if not d:
                raise ValueError("这条回复不在你的会话里")
            row = {"id": uid(), "delivery_id": d["id"], "task_id": d["task_id"], "delivery_kind": d["kind"], "category": category,
                   "note_shared": note_shared.strip() if share and note_shared.strip() else None, "note_private": note_private.strip() or None,
                   "share": int(share and bool(note_shared.strip())), "created": time.time()}
            self.db.execute(f"INSERT INTO feedback_marks({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return {"id": row["id"], "shared": bool(row["share"])}

    # ---- nightly summary ---------------------------------------------------
    def enable_nightly(self, account, room, *, at, task_id, quote, said_delivery_id=None):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at or ""):
            raise HealthError("汇总时间要写成 HH:MM")
        now = time.time()
        with self.transaction():
            old = self.one("SELECT * FROM health_nightly WHERE account=?", (account,))
            commitment_id = old["commitment_id"] if old and old["state"] == "on" else None
            if not commitment_id:
                commitment_id = self.register_commitment(room=room, account=account, quote=quote, said_delivery_id=said_delivery_id,
                                                         task_id=task_id, kind="standing")["id"]
            self.db.execute("INSERT INTO health_nightly(account,room_id,at,commitment_id,task_id,state,last_day,created,updated) VALUES(?,?,?,?,?,'on',?,?,?) "
                            "ON CONFLICT(account) DO UPDATE SET room_id=excluded.room_id,at=excluded.at,commitment_id=excluded.commitment_id,state='on',updated=excluded.updated",
                            (account, room, at, commitment_id, task_id, old["last_day"] if old else None, now, now))
        return self.one("SELECT * FROM health_nightly WHERE account=?", (account,))

    def disable_nightly(self, account, reason="你说不用再每晚汇总了"):
        with self.transaction():
            row = self.one("SELECT * FROM health_nightly WHERE account=? AND state='on'", (account,))
            if not row:
                return None
            self.db.execute("UPDATE health_nightly SET state='off',updated=? WHERE account=?", (time.time(), account))
            if row["commitment_id"] and self.commitment(row["commitment_id"])["state"] == "open":
                self.close_commitment(row["commitment_id"], "released", [], reason=reason)
        return row

    def tick_health(self, *, now=None):
        """Send each due nightly summary once, then any alerts not sent this week."""
        now = time.time() if now is None else now
        sent = {"reports": 0, "alerts": 0}
        for row in self.all("SELECT * FROM health_nightly WHERE state='on'"):
            today = local_day(now)
            due = parse_local(today + " " + row["at"])
            if now < due or row["last_day"] == today:
                continue
            with self.transaction():
                report = daily_report(self, row["account"], today)
                text = report["text"]
                if now - due > 3600:
                    text = f"（今晚 {row['at']} 该发的汇总，这台电脑上的服务当时没开，现在补发。）\n" + text
                did = self._health_notice(row, text, "health")
                self.db.execute("UPDATE health_nightly SET last_day=?,updated=? WHERE account=?", (today, now, row["account"]))
                if row["commitment_id"]:
                    self._commitment_event({"id": row["commitment_id"], "room_id": row["room_id"]}, "occurrence_sent", {"delivery_id": did, "day": today})
                sent["reports"] += 1
                for alert in alert_lines(self, row["account"], today, now=now):
                    did = self._health_notice(row, alert["text"], "health_alert")
                    self.db.execute("INSERT INTO health_alerts VALUES(?,?,?,?,?,?)", (uid(), row["account"], alert["kind"], today, did, now))
                    sent["alerts"] += 1
        return sent

    def _health_notice(self, row, text, kind):
        # Same delivery path as every other message: the channel's acceptance is
        # what counts as delivered (P-1), and a missed push still shows in the page.
        return self._notice(row["task_id"], row["room_id"], text, kind=kind)

    def tick_waits(self):
        super().tick_waits()
        self.tick_health()


# ---- reports (pure reads) ------------------------------------------------------

def _fmt(x):
    return f"{x:,.0f}"


def _range(low, high):
    return _fmt(low) if abs(high - low) < 1 else f"{_fmt(low)}–{_fmt(high)}"


def daily_report(store, account, day):
    meals = store.health_meals(account, day)
    exercise = store.all("SELECT * FROM health_exercise WHERE account=? AND day=? ORDER BY done_at", (account, day))
    start, end = day_bounds(day)
    weight = store.latest_metric(account, "weight_kg", before=end)
    profile = store.health_profile(account)
    exact = sum(m["kcal"] for m in meals if m["method"] == "label")
    est = [m for m in meals if m["method"] == "estimate"]
    intake = {"exact": exact, "low": exact + sum(m["kcal_low"] for m in est), "high": exact + sum(m["kcal_high"] for m in est),
              "exact_meals": len(meals) - len(est), "estimated_meals": len(est)}
    base = bmr(profile, weight["value"] if weight else None, int(day[:4]))
    burn = None
    if base is not None:
        device = sum(e["kcal"] for e in exercise if e["method"] == "device")
        estimated = sum(e["kcal"] for e in exercise if e["method"] == "estimate")
        burn = {"base": base * ACTIVITY_FACTOR, "exercise_device": device, "exercise_estimated": estimated,
                "total": base * ACTIVITY_FACTOR + device + estimated}
    deficit = {"low": burn["total"] - intake["high"], "high": burn["total"] - intake["low"]} if burn and meals else None
    lines = [f"{day[5:7].lstrip('0')}月{day[8:].lstrip('0')}日汇总"]
    if meals:
        parts = []
        if intake["exact_meals"]:
            parts.append(f"读表 {_fmt(exact)}（{intake['exact_meals']} 餐）")
        if est:
            parts.append(f"估算 {_range(intake['low'] - exact, intake['high'] - exact)}（{len(est)} 餐）")
        lines.append(f"摄入 {_range(intake['low'], intake['high'])} 千卡：" + "，".join(parts) + "。")
    else:
        lines.append("今天还没有记录饮食。")
    if burn:
        extra = []
        if burn["exercise_device"]:
            extra.append(f"运动设备读数 {_fmt(burn['exercise_device'])}")
        if burn["exercise_estimated"]:
            extra.append(f"运动估算 {_fmt(burn['exercise_estimated'])}")
        lines.append(f"消耗约 {_fmt(burn['total'])} 千卡：基础消耗按公式估算 {_fmt(burn['base'])}" + ("，" + "，".join(extra) if extra else "") + "。")
    else:
        missing = [name for name, ok in (("性别", profile and profile.get("sex")), ("出生年份", profile and profile.get("birth_year")),
                                         ("身高", profile and profile.get("height_cm")), ("体重", weight)) if not ok]
        lines.append("算不了消耗：还缺" + "、".join(missing) + "。告诉我以后就能算。")
    if deficit:
        word = "缺口" if deficit["high"] >= 0 else "超出"
        lo, hi = sorted((abs(deficit["low"]), abs(deficit["high"]))) if deficit["low"] * deficit["high"] > 0 else (0, max(abs(deficit["low"]), abs(deficit["high"])))
        lines.append(f"热量{word}约 {_range(lo, hi)} 千卡。" if deficit["low"] * deficit["high"] > 0 else f"热量缺口在 {_fmt(deficit['low'])} 到 {_fmt(deficit['high'])} 千卡之间，可能吃得刚好。")
    asks = [f"「{m['food_name']}」：{m['ask']}" for m in est if m["ask"]]
    if asks:
        lines.append("回答这几句可以算得更准：" + "；".join(asks[:3]))
    return {"day": day, "intake": intake, "burn": burn, "deficit": deficit, "meals": len(meals), "text": "\n".join(lines)}


def weekly_report(store, account, end_day):
    end = datetime.strptime(end_day, "%Y-%m-%d")
    days = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    reports = [daily_report(store, account, d) for d in days]
    rows = ["日期 | 摄入（千卡） | 消耗 | 缺口"]
    for r in reports:
        rows.append(f"{r['day'][5:]} | {_range(r['intake']['low'], r['intake']['high']) if r['meals'] else '未记录'} | "
                    f"{_fmt(r['burn']['total']) if r['burn'] else '—'} | {_range(r['deficit']['low'], r['deficit']['high']) if r['deficit'] else '—'}")
    start_w = store.latest_metric(account, "weight_kg", before=day_bounds(days[0])[0] + 86400)
    end_w = store.latest_metric(account, "weight_kg", before=day_bounds(days[-1])[1])
    if start_w and end_w and start_w["id"] != end_w["id"]:
        rows.append(f"体重 {start_w['value']:.1f} → {end_w['value']:.1f} 公斤")
    return {"days": reports, "text": "\n".join(rows)}


def alert_lines(store, account, day, *, now=None):
    now = time.time() if now is None else now
    profile = store.health_profile(account) or {}
    end = datetime.strptime(day, "%Y-%m-%d")
    week = [daily_report(store, account, (end - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(7)]
    found = []
    complete = [r for r in week if r["deficit"]]
    # Only judge a week that is mostly recorded; a missing day is not a deficit.
    if len(complete) >= 5:
        avg = sum(r["deficit"]["low"] for r in complete) / len(complete)   # the conservative end of each range
        if avg > AVG_DEFICIT_LIMIT:
            found.append({"kind": "avg_deficit", "text": ALERT_TEXT["avg_deficit"].format(value=avg)})
    today = week[0]
    limit = LOW_INTAKE.get(profile.get("sex"))
    if limit and today["meals"] >= 2 and today["intake"]["high"] < limit:
        found.append({"kind": "low_intake", "text": ALERT_TEXT["low_intake"].format(day="今天", value=today["intake"]["high"], limit=limit)})
    end_ts = day_bounds(day)[1]
    points = [store.latest_metric(account, "weight_kg", before=end_ts - k * 7 * 86400) for k in (0, 1, 2)]
    if all(points) and len({p["id"] for p in points}) == 3:
        drops = [points[1]["value"] - points[0]["value"], points[2]["value"] - points[1]["value"]]
        if all(d > WEEKLY_LOSS_LIMIT for d in drops):
            found.append({"kind": "fast_loss", "text": ALERT_TEXT["fast_loss"].format(detail=f"{points[2]['value']:.1f} → {points[1]['value']:.1f} → {points[0]['value']:.1f} 公斤")})
    recent = {r["kind"] for r in store.all("SELECT kind FROM health_alerts WHERE account=? AND created>?", (account, now - ALERT_COOLDOWN))}
    return [a for a in found if a["kind"] not in recent]


# ---- tools ---------------------------------------------------------------------

LABEL_PROPS = {
    "food_name": {"type": "string", "description": "What it is, in the user's words or from the package."},
    "energy_value": {"type": "number", "description": "Energy exactly as printed, in the printed unit."},
    "energy_unit": {"type": "string", "enum": ["kJ", "kcal"]},
    "basis_amount": {"type": "number", "description": "The amount the column is per, e.g. 100 for 每100克, 1 for per serving."},
    "basis_unit": {"type": "string", "enum": ["g", "ml", "serving"]},
    "serving_grams": {"type": "number", "description": "Grams per serving when the label states it; omit otherwise."},
    "protein_g": {"type": "number"}, "fat_g": {"type": "number"}, "carb_g": {"type": "number"},
    "portion_amount": {"type": "number", "description": "How much the user ate, as they said it."},
    "portion_unit": {"type": "string", "enum": ["g", "ml", "serving"]},
    "portion_quote": {"type": "string", "description": "Copy verbatim the user's words stating the portion (e.g. '吃了半瓶，250毫升'). If they did not say, ask_user first."},
}
COMMON = {
    "eaten_at": {"type": "string", "description": "Local time YYYY-MM-DD HH:MM when eaten; omit for now."},
    "photo_source_id": {"type": "string", "description": "Source id of the photo this record is read from, if any."},
    "replaces_meal_id": {"type": "string", "description": "meal_id of an earlier record of the same food that this corrects (e.g. a label photo sent after an estimate)."},
}
ESTIMATE_PROPS = {
    "food_name": {"type": "string"},
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "amount": {"type": "string", "description": "Portion and how you judged it (plate ~20cm, about 20 pieces)."},
        "kcal_low": {"type": "number"}, "kcal_high": {"type": "number"}}, "required": ["name", "amount", "kcal_low", "kcal_high"]}},
    "basis": {"type": "string", "description": "One sentence: what the estimate rests on and the biggest uncertainty."},
    "ask": {"type": "string", "description": "The one question to the user that would narrow the range most (oil used, soup finished?); empty if none."},
    "portion_confirmed_quote": {"type": "string", "description": "Only when the user confirmed an unusually large portion: their verbatim words."},
}

TOOLS = [
    tool("record_meal_label", "Record food eaten from a nutrition label photo. Copy the printed numbers only; code converts units and multiplies by the portion. Never compute kcal yourself.",
         {**LABEL_PROPS, **COMMON}, ["food_name", "energy_value", "energy_unit", "basis_amount", "basis_unit", "portion_amount", "portion_unit", "portion_quote"]),
    tool("record_meal_estimate", "Record a meal from a food photo or description as an estimated kcal range per item, with the basis and one narrowing question. Recorded at once; the user's later answer can correct it via replaces_meal_id.",
         {**ESTIMATE_PROPS, **COMMON}, ["food_name", "items", "basis", "ask"]),
    tool("record_exercise", "Record exercise. Code computes kcal from a MET table and the latest weight, or uses the device reading the user gave.",
         {"activity": {"type": "string"}, "kind": {"type": "string", "enum": list(MET)}, "minutes": {"type": "number"},
          "done_at": {"type": "string", "description": "Local time YYYY-MM-DD HH:MM; omit for now."},
          "device_kcal": {"type": "number", "description": "Only a number the user read from a watch/app."},
          "quote": {"type": "string", "description": "Verbatim user words describing this exercise."}}, ["activity", "kind", "minutes", "quote"]),
    tool("record_body", "Record body metrics or profile facts the user stated (weight, waist, body fat, height, birth year, sex, target weight).",
         {"weight_kg": {"type": "number"}, "waist_cm": {"type": "number"}, "body_fat_pct": {"type": "number"},
          "height_cm": {"type": "number"}, "birth_year": {"type": "integer"}, "sex": {"type": "string", "enum": ["female", "male"]},
          "target_weight_kg": {"type": "number"}, "measured_at": {"type": "string", "description": "YYYY-MM-DD HH:MM; omit for now."},
          "quote": {"type": "string", "description": "Verbatim user words stating these values."}}, ["quote"]),
    tool("health_report", "Read the code-computed summary for a day or the last 7 days. Quote its numbers as they are; do not recompute.",
         {"day": {"type": "string", "description": "YYYY-MM-DD; omit for today."}, "range": {"type": "string", "enum": ["day", "week"]}}, []),
    tool("health_nightly", "Turn the nightly diet summary on (at HH:MM) or off, when the user asks for it.",
         {"action": {"type": "string", "enum": ["on", "off"]}, "at": {"type": "string", "description": "HH:MM local, for on."},
          "quote": {"type": "string", "description": "Verbatim user words asking for this."}}, ["action", "quote"]),
]

PROMPT = """Diet and exercise records (records stay on this computer):
- A nutrition label photo: use record_meal_label and copy the printed numbers. You only copy; code multiplies by the portion. The portion must be the user's own words; if they did not say how much they ate, ask_user.
- A meal photo without a label: use record_meal_estimate with a kcal range per item, how you judged the portion, the basis, and one question that would narrow it most. It is recorded at once; tell the user the range and ask that question. When they answer, record a corrected estimate with replaces_meal_id.
- A label sent later for a food you estimated: record_meal_label with replaces_meal_id.
- Weight, height and similar facts go to record_body; exercise to record_exercise.
- For any total, deficit or trend, call health_report and repeat its numbers and labels (读表/估算) exactly.
- Never give medical advice or a diagnosis. If something looks worrying, say it plainly and suggest seeing a doctor."""


def _user_words(call, quote, purpose):
    store, run = call.runtime.store, call.run
    task = store.task(run["task_id"])
    rows = store.all("SELECT id,content FROM messages WHERE task_id=? AND kind='user' AND source NOT IN ('runtime','route') ORDER BY rowid DESC LIMIT 20", (task["root_task_id"],))
    answers = [r["answer"] for r in store.all("SELECT answer FROM attentions WHERE task_id=? AND state='answered'", (task["id"],))]
    texts = [(r["id"], r["content"]) for r in rows] + [(None, a) for a in answers if isinstance(a, str)]
    texts += [(None, turn["content"]) for turn in task["spec"].get("conversation", []) if turn.get("speaker") == "user"]
    last = None
    for mid, text in texts:
        try:
            verify_user_words(text, quote, purpose)
            return mid
        except ValueError as exc:
            last = exc
    raise HealthError(str(last) if last else f"{purpose}：需要用户的原话")


def _account(call):
    return call.runtime.store.one("SELECT owner FROM rooms WHERE id=?", (call.runtime.store.task(call.run["task_id"])["room_id"],))["owner"]


def _private(runtime, run):
    room = runtime.store.one("SELECT r.kind FROM rooms r JOIN tasks t ON t.room_id=r.id WHERE t.id=?", (run["task_id"],))
    return bool(room and room["kind"] == "private")


def _photo(call, args):
    sid = args.get("photo_source_id")
    if not sid:
        return None, None
    read = next((o["result"] for o in reversed(call.runtime.store.operations(call.run["task_id"]))
                 if o["tool"] == "read_source" and o["status"] == "succeeded" and o["result"].get("source_id") == sid and o["result"].get("image")), None)
    if not read:
        raise HealthError("这张照片本任务还没有用 read_source 看过，不能当出处")
    return sid, read["sha256"]


def _when(text):
    return parse_local(text) if text else time.time()


def _meal_label(call, args):
    mid = _user_words(call, args["portion_quote"], "份量必须是用户自己说的")
    sid, sha = _photo(call, args)
    label = {k: args[k] for k in LABEL_PROPS if k in args and k not in {"food_name", "portion_quote"}}
    row = call.runtime.store.record_meal(_account(call), method="label", food_name=args["food_name"], eaten_at=_when(args.get("eaten_at")), label=label,
                                         portion_quote=args["portion_quote"], photo_source=sid, photo_sha256=sha, replaces=args.get("replaces_meal_id"),
                                         source_message=mid, operation_id=call.operation_id)
    return {"meal_id": row["id"], "method": "读表", "kcal": round(row["kcal"], 1), "day": row["day"]}


def _meal_estimate(call, args):
    confirmed = False
    if args.get("portion_confirmed_quote"):
        _user_words(call, args["portion_confirmed_quote"], "确认份量必须是用户自己说的")
        confirmed = True
    sid, sha = _photo(call, args)
    row = call.runtime.store.record_meal(_account(call), method="estimate", food_name=args["food_name"], eaten_at=_when(args.get("eaten_at")),
                                         items=args["items"], basis=args["basis"], ask=args.get("ask"), photo_source=sid, photo_sha256=sha,
                                         replaces=args.get("replaces_meal_id"), portion_confirmed=confirmed, operation_id=call.operation_id)
    return {"meal_id": row["id"], "method": "估算", "kcal_low": round(row["kcal_low"]), "kcal_high": round(row["kcal_high"]), "day": row["day"], "ask_user_next": row["ask"]}


def _exercise(call, args):
    mid = _user_words(call, args["quote"], "运动记录要依据用户的原话")
    row = call.runtime.store.record_exercise(_account(call), activity=args["activity"], kind=args["kind"], minutes=args["minutes"],
                                             done_at=_when(args.get("done_at")), device_kcal=args.get("device_kcal"), source_message=mid, operation_id=call.operation_id)
    return {"exercise_id": row["id"], "method": "设备读数" if row["method"] == "device" else "估算", "kcal": round(row["kcal"]), "day": row["day"]}


def _body(call, args):
    mid = _user_words(call, args["quote"], "身体数据要依据用户的原话")
    store, account = call.runtime.store, _account(call)
    at = _when(args.get("measured_at"))
    saved = {}
    with store.transaction():
        for kind in ("weight_kg", "waist_cm", "body_fat_pct"):
            if kind in args:
                saved[kind] = store.record_metric(account, kind=kind, value=args[kind], measured_at=at, source_message=mid, operation_id=call.operation_id)["value"]
        profile = {k: args[k] for k in ("sex", "birth_year", "height_cm", "target_weight_kg") if k in args}
        if profile:
            saved.update({k: v for k, v in store.set_health_profile(account, source_message=mid, **profile).items() if k in profile})
    if not saved:
        raise HealthError("没有可记录的数值")
    return {"saved": saved}


def _report(call, args):
    store, account = call.runtime.store, _account(call)
    day = args.get("day") or local_day(time.time())
    parse_local(day)
    if args.get("range") == "week":
        return {"text": weekly_report(store, account, day)["text"]}
    report = daily_report(store, account, day)
    return {"text": report["text"], "intake": report["intake"], "burn": report["burn"], "deficit": report["deficit"]}


def _nightly(call, args):
    _user_words(call, args["quote"], "开关每晚汇总要依据用户的原话")
    store, account = call.runtime.store, _account(call)
    if args["action"] == "off":
        row = store.disable_nightly(account)
        return {"state": "off", "was_on": bool(row)}
    task = store.task(call.run["task_id"])
    said = store.one("SELECT id FROM deliveries WHERE task_id=? ORDER BY created DESC LIMIT 1", (task["root_task_id"],))
    row = store.enable_nightly(account, task["room_id"], at=args.get("at") or "21:30", task_id=task["root_task_id"], quote=args["quote"],
                               said_delivery_id=said["id"] if said else None)
    return {"state": "on", "at": row["at"], "nightly_id": row["account"], "commitment_id": row["commitment_id"]}


def health_plugin():
    effects = {"health_report": "read"}
    handlers = {"record_meal_label": _meal_label, "record_meal_estimate": _meal_estimate, "record_exercise": _exercise,
                "record_body": _body, "health_report": _report, "health_nightly": _nightly}
    specs = [ToolSpec(t, handlers[t["function"]["name"]], effect=effects.get(t["function"]["name"], "local_write"), available=_private) for t in TOOLS]
    return Plugin("health", "1", specs, prompt=PROMPT,
                  describe=lambda runtime: ["Record meals (nutrition label or photo estimate), exercise and weight, and summarize daily intake, burn and calorie deficit."])
