"""UTC occurrence identity with explicit local calendar/DST semantics."""
from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_schedule(config):
    if not isinstance(config, dict) or set(config) - {"kind", "timezone", "interval_seconds", "time", "weekdays", "missed", "grace_seconds"}:
        raise ValueError("无效检查计划字段")
    kind = config.get("kind")
    if kind not in {"interval", "calendar", "watch"}:
        raise ValueError("检查计划必须是间隔、日历或资料变化")
    try:
        ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("无效时区；例如 Asia/Shanghai") from None
    if config.get("missed", "coalesce") not in {"coalesce", "skip", "catch_up_one"}:
        raise ValueError("无效漏触发策略")
    grace = config.get("grace_seconds", 60)
    if type(grace) not in (int, float) or not math.isfinite(grace) or not 0 <= grace <= 86400:
        raise ValueError("漏触发宽限范围为 0–86400 秒")
    if kind in {"interval", "watch"}:
        seconds = config.get("interval_seconds", 60)
        minimum = 1 if kind == "watch" else 60
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not minimum <= seconds <= 86400 * 31:
            raise ValueError(f"检查间隔必须为 {minimum} 秒至 31 天")
    else:
        if not isinstance(config.get("time"), str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config["time"]):
            raise ValueError("请使用 HH:MM 时间")
        days = config.get("weekdays", list(range(7)))
        if not isinstance(days, list) or not days or len(set(days)) != len(days) or any(type(d) is not int or not 0 <= d <= 6 for d in days):
            raise ValueError("星期需要 0–6（周一至周日）")
    return {**config, "timezone": config.get("timezone", "Asia/Shanghai"), "missed": config.get("missed", "coalesce"), "grace_seconds": grace}


def calendar_candidates(config, start, count_days=16):
    zone = ZoneInfo(config["timezone"])
    day = datetime.fromtimestamp(start, zone).date()
    hour, minute = map(int, config["time"].split(":"))
    for offset in range(count_days):
        date = day + timedelta(days=offset)
        if date.weekday() not in config.get("weekdays", range(7)):
            continue
        local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone, fold=0)
        stamp = local.timestamp()
        # A nonexistent spring-forward wall time is skipped. Repeated fall
        # times run only at fold=0; occurrence keys still use UTC instants.
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local.replace(tzinfo=None):
            yield stamp


def next_due(config, after, *, anchor=None):
    if config["kind"] in {"interval", "watch"}:
        seconds = config.get("interval_seconds", 60)
        if anchor is None:
            return after + seconds
        return anchor + (math.floor((after - anchor) / seconds) + 1) * seconds
    for stamp in calendar_candidates(config, after):
        if stamp > after:
            return stamp
    raise ValueError("无法计算下一次检查时间")


def due_window(config, due, now):
    """One durable decision for a missed range; never burst-replay every slot."""
    if due > now:
        return None
    if config["kind"] in {"interval", "watch"}:
        seconds = config.get("interval_seconds", 60)
        missed = int(math.floor((now - due) / seconds))
        latest = due + missed * seconds
    else:
        # Search near now, not every day since an offline machine stopped.
        zone = ZoneInfo(config["timezone"])
        start = (datetime.fromtimestamp(now, zone) - timedelta(days=8)).timestamp()
        candidates = [s for s in calendar_candidates(config, start, 10) if due <= s <= now]
        latest = max(candidates, default=due)
        # The exact old count is not invented when downtime spans many weeks.
        missed = "multiple_calendar_slots" if latest != due else 0
    strategy = config.get("missed", "coalesce")
    scheduled = due if strategy == "catch_up_one" else latest
    skipped = strategy == "skip" and now - due > config.get("grace_seconds", 60)
    return {"scheduled": scheduled, "first_due": due, "latest_due": latest, "missed_slots": missed,
            "decision": "skipped" if skipped else strategy, "next_due": next_due(config, now, anchor=due),
            "key": datetime.fromtimestamp(scheduled, timezone.utc).isoformat(timespec="microseconds")}
