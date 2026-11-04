"""M5 evidence, interpretation, feedback and observation-window contracts."""
import asyncio
import json
import time

import pytest

from personal_agent.contracts import TaskSpec, encode
from personal_agent.orchestration_tools import OrchestrationTools
from personal_agent.runtime import Runtime
from personal_agent.service import Service
from personal_agent.store import Store
from conftest import ScriptedModel, submit_written_artifact, tool_reply


def new_goal(store):
    return store.create_goal(room="local", actor_id="default", title="资料持续改进", objective="有证据地维护资料清单")


def new_experiment(store, goal_id):
    now = time.time()
    return store.create_experiment(goal_id, title="更少检查是否仍然及时", hypothesis="调整频率可能减少成本且不漏过关键更新", metric="资料采纳率（尚未接入测量）", baseline="每分钟检查", variant="每两分钟检查，尚未应用", success_criteria="至少两次真实用户反馈并核对遗漏", window_start=now, window_end=now + 86400, next_check=now + 3600)


def review_args():
    return {"observed_facts": ["账本有记录但未连接业务效果数据"], "hypotheses": ["降低检查频率可能减少机械检查次数"], "uncertainties": ["没有采纳率、实际人工耗时和长期质量测量"], "decision": "insufficient", "rationale": "证据不足以证明提升，不自动修改计划", "evidence_refs": ["metrics"], "next_check": time.time() + 86400, "next_validation": "获得两次实际使用反馈后再核对遗漏与时间成本"}


def test_t33_missing_metrics_remain_unknown_and_unread_evidence_cannot_support_review(store, tmp_path):
    goal = new_goal(store)
    receipt = store.request_goal_review(goal["id"], nonce="review")
    run = store.claim()
    assert run["task_id"] == receipt["task_id"]
    context = store.review_context(run)
    schemas = Runtime(store, tmp_path / 'data', ScriptedModel([])).schemas(run)
    refs = next(s for s in schemas if s['function']['name'] == 'record_goal_review')['function']['parameters']['properties']['evidence_refs']
    assert refs['items']['enum'] == [item['id'] for item in context['evidence_catalog']]
    assert 'operation_id' in refs['description']
    assert context["metrics"]["domain_outcome"] is None
    assert context["metrics"]["reported_management_seconds"] is None
    assert context["metrics"]["invoice_currency_cost"] is None
    assert store.completion_errors(run)
    with pytest.raises(ValueError, match="实际读取"):
        store.record_goal_review(run, **review_args())
    store.read_review_evidence(run, evidence_id="metrics")
    with pytest.raises(ValueError, match="历史快照"):
        store.record_goal_review(run, **{**review_args(), "rationale": "观察期未满，实验成功尚不确定"})
    result = store.record_goal_review(run, **review_args())
    assert not result["policy_change_applied"] and result["next_check_is_proposed"]
    assert store.completion_errors(run) == []
    assert not store.all("SELECT * FROM routine_policies")
    review = store.one("SELECT * FROM goal_reviews")
    assert json.loads(review["uncertainties"]) and review["decision"] == "insufficient"


def test_experiment_versions_and_old_review_cannot_attach_to_new_version(store):
    goal = new_goal(store)
    experiment = new_experiment(store, goal["id"])
    store.request_goal_review(goal["id"], nonce="experiment-review", experiment_id=experiment["id"])
    run = store.claim()
    store.read_review_evidence(run, evidence_id="metrics")
    new = store.revise_experiment(experiment["id"], expected_version=1, reason="需要更多真实样本", next_check=time.time() + 172800)
    assert new["version"] == 2
    assert len(store.all("SELECT * FROM experiment_versions")) == 2
    with pytest.raises(ValueError, match="已改版"):
        store.record_goal_review(run, **review_args())
    with pytest.raises(ValueError):
        store.revise_experiment(experiment["id"], expected_version=1, reason="stale write", state="closed")


def test_review_evidence_is_scoped_to_one_goal_and_memory_floor(store):
    goal = new_goal(store)
    store.save_memory("local", key="preference", value="old", purpose="output")
    store.request_goal_review(goal["id"], nonce="scoped-review")
    run = store.claim()
    with pytest.raises(ValueError, match="范围"):
        store.read_review_evidence(run, evidence_id="delivery:foreign-private-id")
    store.save_memory("local", key="preference", value="new", purpose="output", expected_version=1)
    store.ack_control(run, store.control_reason(run))
    resumed = store.claim()
    assert store.review_context(resumed)["state"] == "stale_after_memory_correction"
    with pytest.raises(ValueError):
        store.read_review_evidence(resumed, evidence_id="metrics")


def test_user_feedback_records_management_cost_without_inventing_duration(store):
    goal = new_goal(store)
    first = store.request_goal_review(goal["id"], nonce="feedback")
    store.save_feedback(first["task_id"], rating="needs_fix", note="要补充日期对照", management_kind="correction")
    store.save_feedback(first["task_id"], rating="useful", note="已核对日期", management_kind="feedback", management_seconds=90)
    run = store.claim()
    # Snapshot was frozen before these reports, so it must not change in place.
    assert store.review_context(run)["metrics"]["user_feedback_count"] == 0
    store.request_control(first["task_id"], "cancel")
    store.ack_control(run, store.control_reason(run))
    store.request_goal_review(goal["id"], nonce="new-feedback")
    new_run = store.claim()
    metrics = store.review_context(new_run)["metrics"]
    assert metrics["user_feedback_count"] == 2 and metrics["reported_management_seconds"] == 90
    assert metrics["domain_outcome"] is None
    with pytest.raises(ValueError):
        store.save_feedback(first["task_id"], rating="useful", note="invalid", management_seconds=-1)


def test_experiment_reminder_survives_reopen_and_paused_goal_stays_quiet(store, tmp_path):
    goal = new_goal(store)
    experiment = new_experiment(store, goal["id"])
    now = experiment["next_check"] + 1
    store.set_goal_state(goal["id"], "paused", expected_revision=1)
    store.tick_review_reminders(now=now)
    assert not store.all("SELECT * FROM experiment_reminders")
    store.set_goal_state(goal["id"], "active", expected_revision=2)
    store.tick_review_reminders(now=now)
    assert len(store.all("SELECT * FROM experiment_reminders")) == 1
    assert not store.all("SELECT * FROM tasks") and not store.all("SELECT * FROM model_requests")
    store.close()
    reopened = Store(tmp_path / "data/state.sqlite")
    try:
        before = len(reopened.all("SELECT * FROM deliveries"))
        reopened.tick_review_reminders(now=now + 3600)
        assert len(reopened.all("SELECT * FROM deliveries")) == before
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_actual_pi_review_requires_recorded_assessment_before_delivery(store, tmp_path):
    goal = new_goal(store)
    receipt = store.request_goal_review(goal["id"], nonce="protocol-review")
    model = ScriptedModel([tool_reply("read_review_evidence", {"evidence_id": "metrics"}), tool_reply("record_goal_review", review_args()), tool_reply("write_artifact", {"name": "review.md", "content": "资料采纳率与实际人工耗时均未测量。证据不足以判断改善；下一次应收集真实反馈。[metrics]"}), submit_written_artifact])
    await Service(store, tmp_path / "data", model).serve(once=True)
    assert store.task(receipt["task_id"])["status"] == "succeeded"
    assert store.goal(goal["id"])["calls_used"] == 4
    assert len(store.all("SELECT * FROM goal_reviews")) == 1
    assert len(store.all("SELECT * FROM routine_policies")) == 0


def test_review_task_pause_resume_preserves_snapshot_and_cumulative_budget(store):
    goal = new_goal(store)
    receipt = store.request_goal_review(goal["id"], nonce="paused-review")
    run = store.claim()
    store.reserve(run, "model")
    store.set_goal_state(goal["id"], "paused", expected_revision=1)
    store.ack_control(run, store.control_reason(run))
    store.set_goal_state(goal["id"], "active", expected_revision=2)
    resumed = store.claim()
    assert resumed["task_id"] == receipt["task_id"]
    assert store.review_context(resumed)["snapshot_id"] == receipt["snapshot_id"]
    assert store.goal(goal["id"])["calls_used"] == 1


@pytest.mark.asyncio
async def test_review_artifact_requires_complete_verified_pagination(store, tmp_path):
    from personal_agent.tools import Toolset
    goal = new_goal(store)
    tid = store.submit(TaskSpec("Large report"), nonce="large-report")["task_id"]
    store.db.execute("UPDATE tasks SET goal_id=?,goal_revision=1 WHERE id=?", (goal["id"], tid))
    # An explicit review request is not required for this fixture task; keep its
    # completion independent, then associate its saved delivery with the goal.
    store.db.execute("UPDATE tasks SET goal_id=NULL,goal_revision=NULL WHERE id=?", (tid,))
    await Service(store, tmp_path / "data", ScriptedModel([tool_reply("write_artifact", {"name": "large.md", "content": "背景" * 4500 + "文末关键限制"}), submit_written_artifact])).serve(once=True)
    store.db.execute("UPDATE tasks SET goal_id=?,goal_revision=1 WHERE id=?", (goal["id"], tid))
    delivery = store.one("SELECT id FROM deliveries WHERE task_id=? AND artifact IS NOT NULL", (tid,))
    receipt = store.request_goal_review(goal["id"], nonce="paged-review")
    run = store.claim()
    runtime = Runtime(store, tmp_path / "data", ScriptedModel([]))
    tools = Toolset(tmp_path / "data", store.task(receipt["task_id"]))
    evidence_id = "delivery:" + delivery["id"]
    args = {**review_args(), "evidence_refs": [evidence_id]}
    first = await runtime.execute_tool(run, tools, tool_reply("read_review_evidence", {"evidence_id": evidence_id}).message["tool_calls"][0], 1)
    assert not first["result"]["artifact_page"]["eof"]
    assert store.task(receipt["task_id"])["stagnant_tools"] == 0
    await runtime.execute_tool(run, tools, tool_reply("read_review_evidence", {"evidence_id": evidence_id}).message["tool_calls"][0], 1)
    assert store.task(receipt["task_id"])["stagnant_tools"] == 1
    with pytest.raises(ValueError, match="所有分页"):
        store.record_goal_review(run, **args)
    await runtime.execute_tool(run, tools, tool_reply("read_review_evidence", {"evidence_id": evidence_id, "offset": 8000}).message["tool_calls"][0], 1)
    assert store.task(receipt["task_id"])["stagnant_tools"] == 0
    assert store.record_goal_review(run, **args)["state"] == "assessment_recorded"


def test_review_new_snapshot_does_not_reintroduce_pre_correction_assessment(store):
    goal = new_goal(store)
    store.db.execute("UPDATE goals SET current_state='old-private-derived-fact',assessment_at=? WHERE id=?", (time.time(), goal["id"]))
    store.save_memory("local", key="correction", value="new", purpose="correct record")
    store.save_memory("local", key="correction", value="corrected", purpose="correct record", expected_version=1)
    store.request_goal_review(goal["id"], nonce="after-correction")
    run = store.claim()
    state = store.read_review_evidence(run, evidence_id="goal_state")
    assert 'old-private-derived-fact' not in encode(state)


def test_explicit_resume_reopens_consecutive_failure_window_not_lifetime_budgets(store):
    tid = store.submit(TaskSpec('Recover a stalled investigation', max_calls=30), nonce='recover-stagnation')['task_id']
    run = store.claim()
    store.reserve(run, 'model')
    store.db.execute('UPDATE tasks SET stagnant_tools=20,failure_streak=8 WHERE id=?', (tid,))
    store.finish(run, status='paused', reason='stagnation_budget', content='Stalled')
    store.resume(tid)
    task = store.task(tid)
    assert task['stagnant_tools'] == 0 and task['failure_streak'] == 0
    assert task['calls_used'] == 1 and task['spec']['max_calls'] == 30


def test_review_report_facts_come_from_frozen_ledger_not_model_restatements(store):
    goal = new_goal(store)
    store.request_goal_review(goal['id'], nonce='grounded-report')
    run = store.claim()
    store.read_review_evidence(run, evidence_id='metrics')
    record = store.record_goal_review(run, **{**review_args(), 'observed_facts': ['模型误写：已有1000份成果']})
    report = store.review_report(record['review_id'])
    assert all('1000' not in fact for fact in report['facts'])
    assert '1000' not in report['text']
    assert '没有关联实验' in report['text']
    assert '文档回执 0 份' in report['text']
    assert report['fact_source'] == 'frozen_ledger_projection'
    store.reserve(run, 'model')
    assert store.review_report(record['review_id'])['sha256'] == report['sha256']
