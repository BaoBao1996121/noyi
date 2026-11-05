"""Built-in capabilities, expressed through the same plugin contract as extensions.

Their guidance still lives in runtime.SYSTEM; moving it into per-plugin prompt
fragments is a separate change because it alters the exact model request.
"""
from __future__ import annotations

from .conversation_goals import CONVERSATION_GOAL_SCHEMAS, validate_conversation_goal_arguments
from .orchestration_tools import ORCHESTRATION_SCHEMAS, SUSPENDING_TOOLS, validate_orchestration_arguments
from .plugins import Plugin, ToolSpec
from .tools import TOOL_SCHEMAS, tool, validate_arguments, verify


ACTION_COMPLETION = tool('complete_action', 'Finish an arrangement-only request using actual effect operation IDs. This accepts registration, a change, or starting a review/observation; a started review is not a finished report, an observation plan proves no improvement. For a requested document use submit_result.',
    {'summary': {'type':'string'}, 'effect_refs': {'type':'array','items':{'type':'string'},'minItems':1,'maxItems':8}}, ['summary','effect_refs'])

FOLLOWING_EFFECT_TOOLS = ('start_following', 'manage_following', 'review_following', 'observe_following')


def _schema(name):
    return next(s for s in TOOL_SCHEMAS if s["function"]["name"] == name)


async def _read_source(call, args):
    return await call.toolset.read_source(**args)


def _write_artifact(call, args):
    store = call.runtime.store
    store.check_artifact_version(call.run, call.operation_id, args)
    result = call.toolset.write_artifact(call.operation_id, **args)
    return store.record_artifact_version(call.run, call.operation_id, args, result)


def _read_artifact(call, args):
    return call.toolset.read_artifact(operations=call.runtime.store.operations(call.run["task_id"]), **args)


def _submit_result(call, args):
    store, run = call.runtime.store, call.run
    result = verify(call.toolset, store.operations(run["task_id"]), args)
    errors = store.completion_errors(run) + store.artifact_version_errors(run, args["artifact_ref"]) + store.review_artifact_errors(run, args["artifact_ref"])
    if errors:
        result["passed"] = False
        result.setdefault("errors", []).extend(errors)
    return result


def artifacts_plugin():
    def validator(name):
        return lambda args: validate_arguments(name, args)
    return Plugin("core.artifacts", "1", [
        ToolSpec(_schema("read_source"), _read_source, validator("read_source")),
        ToolSpec(_schema("write_artifact"), _write_artifact, validator("write_artifact"), effect="local_write"),
        ToolSpec(_schema("read_artifact"), _read_artifact, validator("read_artifact")),
        ToolSpec(_schema("submit_result"), _submit_result, validator("submit_result"), exclusive=True),
    ])


def _orchestration_available(name):
    goal_only = {"record_goal_progress", "propose_policy_adjustment", "review_policy_proposal"}
    review_only = {"read_review_evidence", "record_goal_review"}

    def available(runtime, run):
        store = runtime.store
        root = store.task(store.task(run["task_id"])["root_task_id"])
        reviewing = store.one("SELECT task_id FROM goal_review_requests WHERE task_id=?", (root["id"],))
        if name in goal_only and not root["goal_id"]:
            return False
        if name in review_only and not reviewing:
            return False
        return not (name == "record_goal_progress" and reviewing)
    return available


def orchestration_plugin():
    def spec(schema):
        name = schema["function"]["name"]
        return ToolSpec(schema, lambda call, args: call.runtime.orchestration.execute(call.run, name, args),
                        lambda args: validate_orchestration_arguments(name, args),
                        effect="local_write", exclusive=name in SUSPENDING_TOOLS,
                        available=_orchestration_available(name))
    return Plugin("core.orchestration", "1", [spec(s) for s in ORCHESTRATION_SCHEMAS])


def _validate_complete_action(args):
    if (not isinstance(args, dict) or set(args) != {'summary','effect_refs'} or
            not isinstance(args['summary'], str) or not 1 <= len(args['summary'].strip()) <= 3000 or
            not isinstance(args['effect_refs'], list) or not 1 <= len(args['effect_refs']) <= 8 or
            any(not isinstance(ref, str) for ref in args['effect_refs'])):
        raise ValueError('需要简洁结果和本次真实操作回执')


def _complete_action(call, args):
    store, run = call.runtime.store, call.run
    task = store.task(run['task_id'])
    if task['parent_task_id'] or task['goal_id'] or task['spec']['required_text'] or task['spec'].get('result_kind', 'artifact') != 'action_receipt':
        raise ValueError('此任务需要交付并验收成果，不能只报告安排完成')
    effects = store.all("SELECT o.id FROM operations o JOIN runs r ON r.id=o.run_id WHERE o.task_id=? AND r.revision=? AND o.status='succeeded' AND o.tool IN (%s)" % ",".join("?" * len(FOLLOWING_EFFECT_TOOLS)), (task['id'], run['revision'], *FOLLOWING_EFFECT_TOOLS))
    if any(ref not in {effect['id'] for effect in effects} for ref in args['effect_refs']):
        raise ValueError('安排尚未实际完成，请引用本次成功的操作回执')
    errors = store.completion_errors(run)
    return {'passed': not errors, 'errors': errors, 'scope': 'registered following effects only; not a document or business outcome', 'effect_refs': args['effect_refs']}


def following_plugin():
    def available(runtime, run):
        return runtime.following.available(run)

    def spec(schema):
        name = schema["function"]["name"]
        return ToolSpec(schema, lambda call, args: call.runtime.following.execute(call.run, name, args),
                        lambda args: validate_conversation_goal_arguments(name, args),
                        effect="local_write", available=available)
    return Plugin("core.following", "1", [spec(s) for s in CONVERSATION_GOAL_SCHEMAS] + [
        ToolSpec(ACTION_COMPLETION, _complete_action, _validate_complete_action, exclusive=True, available=available)])


def builtin_plugins():
    return [artifacts_plugin(), orchestration_plugin(), following_plugin()]
