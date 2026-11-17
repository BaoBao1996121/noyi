"""Weekly self-review of tool-call chains: which call patterns recur often
enough that a fixed composite tool (a template) would save rounds.

Division of labour (user decision 2026-11-17):
- code reads the ledger, builds every task's chain round by round, counts the
  recurring segments and what they cost (rounds, seconds, tokens), and marks
  the ones existing templates already cover;
- the model only turns those numbers into a short note for the user.

Noyi only proposes. Nothing here creates, enables or edits a template, the
code or the configuration; a development session does that after the user
agrees. The statistics carry tool names and numbers only, never arguments,
page text or the user's words.

Thresholds, window and schedule live in tool_review.toml (packaged default),
overridden by tool-review.toml in the data directory.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import time

DEFAULT_CONFIG = Path(__file__).with_name("tool_review.toml")
CONFIG_NAME = "tool-review.toml"


@dataclass
class ReviewConfig:
    enabled: bool
    schedule: dict
    window_days: float
    max_pattern_rounds: int
    min_tasks: int
    min_occurrences: int
    min_saved_rounds: int
    max_candidates: int
    ignore_tools: list[str]
    templates: list[dict] = field(default_factory=list)
    calls_limit: int = 1000
    tools_limit: int = 3000

    @classmethod
    def load(cls, data=None):
        import tomllib
        values = tomllib.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))["tool_review"]
        override = Path(data) / CONFIG_NAME if data else None
        if override and override.is_file():
            extra = tomllib.loads(override.read_text(encoding="utf-8-sig")).get("tool_review", {})
            if not isinstance(extra, dict) or set(extra) - set(cls.__dataclass_fields__):
                raise ValueError("unknown [tool_review] configuration fields")
            values.update(extra)
        config = cls(**values)
        config.validate()
        return config

    def validate(self):
        ints = {"max_pattern_rounds": (1, 8), "min_tasks": (1, 1000), "min_occurrences": (1, 10000),
                "min_saved_rounds": (0, 100000), "max_candidates": (1, 20), "calls_limit": (1, 10000), "tools_limit": (1, 30000)}
        for name, (low, high) in ints.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"[tool_review] {name} must be an integer from {low} to {high}")
        if type(self.enabled) is not bool or type(self.window_days) not in (int, float) or not 1 <= self.window_days <= 366:
            raise ValueError("[tool_review] enabled must be true/false and window_days 1-366")
        if not isinstance(self.ignore_tools, list) or any(not isinstance(t, str) for t in self.ignore_tools):
            raise ValueError("[tool_review] ignore_tools must be a list of tool names")
        for template in self.templates:
            if (not isinstance(template, dict) or set(template) != {"name", "pattern"} or not isinstance(template["name"], str)
                    or not isinstance(template["pattern"], list) or not template["pattern"]
                    or any(not isinstance(p, str) or not p for p in template["pattern"])):
                raise ValueError("[[tool_review.templates]] needs a name and a non-empty pattern list")


def _round_key(names):
    # One round = the distinct tools the model called together; how many of
    # each does not change which template would replace it.
    return "+".join(sorted(set(names)))


def task_chains(store, *, since, until, ignore=(), exclude_goal=None):
    """Each task's rounds in order: (key, calls, seconds, input, output).

    A round is one model reply and the tool calls it made; a call belongs to
    the latest reply of the same run recorded before it. Rounds whose calls
    are all ignored tools, and replies that called nothing, are left out.
    """
    tasks = store.all("SELECT DISTINCT o.task_id FROM operations o JOIN tasks t ON t.id=o.task_id JOIN tasks r ON r.id=t.root_task_id "
                      "WHERE o.created>=? AND o.created<? AND (? IS NULL OR r.goal_id IS NULL OR r.goal_id!=?) ORDER BY o.task_id",
                      (since, until, exclude_goal, exclude_goal))
    chains = {}
    for row in tasks:
        task_id = row["task_id"]
        steps = store.all("SELECT run_id,data,created FROM steps WHERE task_id=? AND kind='model' ORDER BY created,id", (task_id,))
        ops = store.all("SELECT run_id,tool,created,ended FROM operations WHERE task_id=? ORDER BY created,id", (task_id,))
        rounds = []
        for step in steps:
            data = json.loads(step["data"])
            rounds.append({"run": step["run_id"], "at": step["created"], "start": step["created"] - (data.get("seconds") or 0),
                           "input": data.get("input_tokens") or 0, "output": data.get("output_tokens") or 0, "calls": []})
        for op in ops:
            owner = None
            for r in rounds:
                if r["run"] == op["run_id"] and r["at"] <= op["created"]:
                    owner = r
            if owner is not None:
                owner["calls"].append(op)
        chain = []
        for r in rounds:
            names = [c["tool"] for c in r["calls"] if c["tool"] not in ignore]
            if not names:
                continue
            end = max([c["ended"] or c["created"] for c in r["calls"]] + [r["at"]])
            chain.append((_round_key(names), len(names), end - r["start"], r["input"], r["output"]))
        if chain:
            chains[task_id] = chain
    return chains


def _units(chain):
    """Consecutive rounds with the same tools form one unit (e.g. paging)."""
    units = []
    for key, calls, seconds, tokens_in, tokens_out in chain:
        if units and units[-1]["key"] == key:
            u = units[-1]
        else:
            u = {"key": key, "rounds": 0, "calls": 0, "seconds": 0.0, "input": 0, "output": 0}
            units.append(u)
        u["rounds"] += 1
        u["calls"] += calls
        u["seconds"] += seconds
        u["input"] += tokens_in
        u["output"] += tokens_out
    return units


def _covered(pattern, templates):
    """Templates whose tool sequence is exactly this pattern's units."""
    return [t["name"] for t in templates if [k.rstrip("+") for k in t["pattern"]] == list(pattern)]


def _summary(pattern, occurrences, templates):
    tasks = {o["task"] for o in occurrences}
    rounds = sum(o["rounds"] for o in occurrences)
    tokens_in = sum(o["input"] for o in occurrences)
    saved = rounds - len(occurrences)  # each occurrence would become one round
    return {"pattern": list(pattern), "label": " → ".join(pattern), "tasks": len(tasks), "occurrences": len(occurrences),
            "rounds": rounds, "calls": sum(o["calls"] for o in occurrences),
            "seconds": round(sum(o["seconds"] for o in occurrences), 1), "input_tokens": tokens_in,
            "output_tokens": sum(o["output"] for o in occurrences),
            # Estimate: the merged rounds' model input disappears in
            # proportion; tool time stays, since the same calls still run.
            "est_saved_rounds": saved, "est_saved_input_tokens": round(tokens_in * saved / rounds) if rounds else 0,
            "covered_by": _covered(pattern, templates)}


def chain_stats(store, config: ReviewConfig, *, now=None, exclude_goal=None):
    now = time.time() if now is None else now
    since = now - config.window_days * 86400
    chains = task_chains(store, since=since, until=now, ignore=set(config.ignore_tools), exclude_goal=exclude_goal)
    found = defaultdict(list)
    total = {"tasks": len(chains), "rounds": 0, "calls": 0, "seconds": 0.0, "input_tokens": 0, "output_tokens": 0}
    for task_id, chain in chains.items():
        for _, calls, seconds, tokens_in, tokens_out in chain:
            total["rounds"] += 1
            total["calls"] += calls
            total["seconds"] += seconds
            total["input_tokens"] += tokens_in
            total["output_tokens"] += tokens_out
        units = _units(chain)
        for size in range(1, config.max_pattern_rounds + 1):
            for start in range(len(units) - size + 1):
                window = units[start:start + size]
                rounds = sum(u["rounds"] for u in window)
                if rounds < 2:
                    continue  # already one round: nothing to merge
                found[tuple(u["key"] for u in window)].append(
                    {"task": task_id, "span": (start, start + size), "rounds": rounds,
                     **{k: sum(u[k] for u in window) for k in ("calls", "seconds", "input", "output")}})
    passes = lambda p: (p["tasks"] >= config.min_tasks and p["occurrences"] >= config.min_occurrences
                        and p["est_saved_rounds"] >= config.min_saved_rounds)
    patterns = sorted((_summary(k, v, config.templates) for k, v in found.items()),
                      key=lambda p: (-p["est_saved_rounds"], -p["tasks"], p["label"]))
    # Greedy, without counting a round twice: once a pattern is chosen, its
    # rounds are claimed, and every other pattern is re-measured on the
    # occurrences that remain. Covered patterns claim rounds too.
    claimed, candidates, covered = defaultdict(list), [], []
    free = lambda o: all(o["span"][1] <= a or o["span"][0] >= b for a, b in claimed[o["task"]])
    while len(candidates) < config.max_candidates:
        best = None
        for key, occurrences in found.items():
            rest = [o for o in occurrences if free(o)]
            if not rest:
                continue
            summary = _summary(key, rest, config.templates)
            if passes(summary) and (best is None or (summary["est_saved_rounds"], summary["tasks"]) > (best[1]["est_saved_rounds"], best[1]["tasks"])):
                best = (rest, summary)
        if best is None:
            break
        rest, summary = best
        for o in rest:
            claimed[o["task"]].append(o["span"])
        (covered if summary["covered_by"] else candidates).append(summary)
    total["seconds"] = round(total["seconds"], 1)
    chosen = {tuple(p["pattern"]) for p in candidates + covered}
    return {"window_days": config.window_days, "since": since, "until": now, "totals": total,
            "thresholds": {"min_tasks": config.min_tasks, "min_occurrences": config.min_occurrences, "min_saved_rounds": config.min_saved_rounds},
            "templates": [t["name"] for t in config.templates], "covered": covered,
            "candidates": candidates, "no_candidates": not candidates,
            "near_misses": [p for p in patterns if tuple(p["pattern"]) not in chosen and not passes(p)][:3],
            "scope": "tool names and counts from the ledger only; no arguments, page text or user words"}


# ---- presentation: the exact numbers the report must carry -----------------

NO_CANDIDATES = "本周没有新候选"


def render_table(stats):
    t = stats["totals"]
    days = stats["window_days"]
    lines = ["| 统计范围 | 任务 | 轮次 | 工具调用 | 秒数 | 输入 token | 输出 token |", "|---|---|---|---|---|---|---|",
             f"| 近 {days:g} 天 | {t['tasks']} | {t['rounds']} | {t['calls']} | {t['seconds']:.1f} | {t['input_tokens']} | {t['output_tokens']} |"]
    rows = [(p, "候选") for p in stats["candidates"]] + [(p, "已有模版 " + "、".join(p["covered_by"])) for p in stats["covered"]]
    if rows:
        lines += ["", "| 调用模式 | 状态 | 涉及任务 | 出现次数 | 占用轮次 | 秒数 | 输入 token | 估计可省轮次 | 估计可省输入 token |",
                  "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {p['label']} | {state} | {p['tasks']} | {p['occurrences']} | {p['rounds']} | {p['seconds']:.1f} | "
                  f"{p['input_tokens']} | {p['est_saved_rounds']} | {p['est_saved_input_tokens']} |" for p, state in rows]
    return "\n".join(lines)


# ---- the weekly arrangement on the existing goal scheduler ------------------

TITLE = "每周工具调用复盘"
QUOTE = "每周复盘一次工具调用链，把值得做成模版的调用模式告诉你"
OBJECTIVE = f"""每周工具调用复盘。先调用 tool_chain_stats，取得程序从账本算出的统计；不要自己数，也不要读任何网页或资料。
然后写一份简短的中文报告（write_artifact）并提交（submit_result）：
- 报告必须原样包含统计结果里的 table，一个字符也不改；
- 有候选时，在表格后逐条说明：哪种调用模式、出现几次、做成固定组合工具（模版）大约能省多少轮次和输入 token、建议这个工具一次做完哪些步骤。估计值要写明是估计；
- 没有候选时，表格后只写一句「{NO_CANDIDATES}」；
- 你只提建议：不要说已经创建、启用或修改了模版、代码或配置。做不做由用户决定，用户同意后由开发者实现并测试；
- 只写工具名和统计数字，不写任何任务内容。
summary 第一行：有候选写「本周发现 N 个值得做成模版的调用模式」，没有就写「{NO_CANDIDATES}」。"""
METADATA = "tool_review_goal"


def _metadata(store):
    store.db.execute("CREATE TABLE IF NOT EXISTS local_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")


def review_goal(store):
    if not store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='local_metadata'"):
        return None
    row = store.one("SELECT value FROM local_metadata WHERE key=?", (METADATA,))
    return row["value"] if row else None


def ensure_tool_review(store, data):
    """Register the weekly review once, as an ordinary ongoing goal with a
    calendar policy and a standing commitment; follow enabled/schedule from
    configuration. A goal the user ended stays ended."""
    from .contracts import TaskSpec
    config = ReviewConfig.load(data)
    with store.transaction():
        _metadata(store)
        goal_id = review_goal(store)
        if goal_id is None:
            if not config.enabled:
                return None
            goal = store.create_goal(room="local", actor_id="default", title=TITLE, objective=OBJECTIVE,
                                     calls_limit=config.calls_limit, tools_limit=config.tools_limit)
            spec = TaskSpec(OBJECTIVE, max_calls=8, max_tools=12, max_seconds=600, max_workers=0, max_continuations=2)
            policy = store.create_policy(goal["id"], schedule=config.schedule, template=spec)
            store.register_commitment(room="local", account="local-user", quote=QUOTE, said_delivery_id=None,
                                      goal_id=goal["id"], policy_id=policy["id"], kind="standing")
            store.db.execute("INSERT INTO local_metadata VALUES(?,?)", (METADATA, goal["id"]))
            return goal["id"]
        goal = store.goal(goal_id)
        if goal["status"] in {"retired", "achieved"}:
            return goal_id
        if not config.enabled:
            store.set_goal_state(goal_id, "retired", expected_revision=goal["revision"])
            return goal_id
        policy = store.one("SELECT id FROM routine_policies WHERE goal_id=? AND state!='retired'", (goal_id,))
        if policy:
            current = store.policy(policy["id"])
            from .schedule import validate_schedule
            if current["schedule"] != validate_schedule(config.schedule):
                store.update_policy(current["id"], schedule=config.schedule, template=current["template"],
                                    expected_revision=current["revision"], state=current["state"])
    return goal_id


# ---- the one tool the review task gets, and its acceptance check -----------

STATS_TOOL = {"type": "function", "function": {"name": "tool_chain_stats",
              "description": "Weekly review only: the recurring tool-call patterns of recent tasks, counted by code from the ledger "
                             "(tool names and numbers only). Copy its table into the report unchanged.",
              "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}


def _is_review(store, run):
    goal_id = review_goal(store)
    if not goal_id or not run.get("task_id"):
        return False
    task = store.task(run["task_id"])
    return store.task(task["root_task_id"])["goal_id"] == goal_id


def _stats(call, args):
    store = call.runtime.store
    stats = chain_stats(store, ReviewConfig.load(call.runtime.data), exclude_goal=review_goal(store))
    return {**stats, "table": render_table(stats)}


def artifact_errors(store, run, artifact_ref):
    """A review report must carry the code's table verbatim and say no
    candidate exactly when there is none."""
    if not _is_review(store, run):
        return []
    ops = store.operations(run["task_id"])
    stats = [o for o in ops if o["tool"] == "tool_chain_stats" and o["status"] == "succeeded"]
    writes = [o for o in ops if o["tool"] == "write_artifact" and o["status"] == "succeeded" and (o["result"] or {}).get("artifact_ref") == artifact_ref]
    if not stats:
        return ["weekly review needs tool_chain_stats before submitting"]
    if not writes:
        return []
    text, result = writes[-1]["arguments"]["content"], stats[-1]["result"]
    errors = []
    if result["table"] not in text:
        errors.append("report must contain the tool_chain_stats table unchanged; copy it again without editing any number")
    if result["no_candidates"] != (NO_CANDIDATES in text):
        errors.append(f"say「{NO_CANDIDATES}」exactly when tool_chain_stats has no candidates")
    return errors


def tool_review_plugin():
    from .plugins import Plugin, ToolSpec
    return Plugin("core.tool_review", "1", [ToolSpec(STATS_TOOL, _stats, effect="read",
                                                       available=lambda runtime, run: _is_review(runtime.store, run))])
