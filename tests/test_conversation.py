"""Conversation intent, ownership, interruption and durable routing contracts."""
import asyncio
import json
import time

import pytest

from personal_agent.contracts import ModelReply, TaskSpec, encode
from personal_agent.conversation import ConversationRouter
from personal_agent.runtime import Runtime
from personal_agent.service import Service
from conftest import ScriptedModel, tool_reply, submit_written_artifact


def incoming(store, text, *, nonce="auto", sources=None, task_id=None, room="local", actor_id=None, reply_to=None):
    return store.accept_conversation(TaskSpec(text, sources or [],max_calls=40,max_tools=100,max_seconds=900),text=text,nonce=nonce,room=room,actor_id=actor_id,task_id=task_id,reply_to=reply_to)


def claim_intake(store):
    run = store.claim_conversation()
    assert run and run['trigger'] == 'conversation'
    store.conversation_context(run)
    return run


def test_conversation_nonce_and_new_task_keep_one_original_message(store, source):
    received = incoming(store,"整理这份资料",sources=[source])
    assert store.claim() is None
    run = claim_intake(store)
    result = store.apply_conversation(run,{"action":"new_task"})
    tid = result['task_id']
    assert len(store.all('SELECT id FROM messages')) == 1
    assert store.one('SELECT source FROM messages')['source'] == 'web'
    assert store.task(tid)['spec']['sources'] == [source]
    repeated = incoming(store,"整理这份资料",sources=[source])
    assert repeated['message_id'] == received['message_id'] and repeated['task_id'] == tid and repeated['duplicate']
    assert len(store.all('SELECT id FROM tasks')) == 1
    assert store.apply_conversation(run,{"action":"new_task"})['duplicate']
    with pytest.raises(ValueError,match='nonce'):
        incoming(store,"不同内容",sources=[source])


@pytest.mark.parametrize("selection", ["private", "mention", "explicit"])
def test_intake_keeps_responding_bot_identity_without_private_scope(store, selection):
    bot = store.create_bot("资料助手", "用简洁的中文解释资料")
    room = bot["private_room"] if selection == "private" else store.create_group("资料协作", ["default", bot["id"]])["id"]
    incoming(store, "@资料助手 你好" if selection == "mention" else "你好", room=room,
             actor_id=bot["id"] if selection == "explicit" else None)
    context = store.conversation_context(claim_intake(store))
    assert context["responding_actor"] == {"id": bot["id"], "name": bot["name"], "role": bot["role"]}
    assert "private_room" not in json.dumps(context)
    assert "memory" not in context


@pytest.mark.asyncio
async def test_actual_pi_reply_has_no_artifact_task_and_accounts_request(store,tmp_path):
    incoming(store,"你好")
    model = ScriptedModel([tool_reply('resolve_conversation',{'action':'reply','text':'你好，今天想让我帮你处理什么？'})])
    runtime = Runtime(store,tmp_path/'data',model)
    await runtime.execute(claim_intake(store))
    assert not store.all('SELECT id FROM tasks')
    assert store.one('SELECT content FROM deliveries')['content'].startswith('你好')
    assert store.one('SELECT calls_used FROM conversation_inputs')['calls_used'] == 1
    assert store.one('SELECT state FROM model_requests')['state'] == 'completed'
    assert store.one('SELECT state FROM input_usage')['state'] == 'completed'
    assert store.one("SELECT data FROM steps WHERE kind='engine'")


def intake_context_seen(store, tmp_path, plugins, model_config=None):
    """Run real intake once and return the room context the model was shown."""
    seen = {}
    def reply(messages, tools):
        seen.update(json.loads(messages[1]["content"].split(": ", 1)[1]))
        seen["system"] = messages[0]["content"]
        return tool_reply('resolve_conversation', {'action': 'reply', 'text': '好的'})
    model = ScriptedModel([reply])
    if model_config:
        model.config = model_config
    runtime = Runtime(store, tmp_path/'data', model, plugins=plugins)
    return runtime, seen


@pytest.mark.asyncio
async def test_intake_is_told_what_execution_can_actually_do(store, tmp_path, monkeypatch):
    # Intake must not answer "I cannot get live information" when execution
    # has a working web capability; the list comes from installed, ready plugins.
    from personal_agent.web_tools import WebConfig, WebTools, web_plugin
    monkeypatch.setenv("WEB_TEST_KEY", "unit-test-only")
    incoming(store, "今天美元汇率多少")
    runtime, seen = intake_context_seen(store, tmp_path, [web_plugin(WebTools(WebConfig(api_key_env="WEB_TEST_KEY")))],
                                        type("C", (), {"vision": True})())
    await runtime.execute(claim_intake(store))
    capabilities = " ".join(seen["execution_capabilities"])
    assert "web" in capabilities.lower() and "page" in capabilities.lower() and "image" in capabilities.lower()
    assert "execution_capabilities" in seen["system"]


@pytest.mark.asyncio
async def test_intake_capabilities_omit_what_is_not_ready(store, tmp_path, monkeypatch):
    from personal_agent.web_tools import WebConfig, WebTools, web_plugin
    monkeypatch.delenv("WEB_TEST_KEY", raising=False)
    incoming(store, "某某股票现在多少钱")
    runtime, seen = intake_context_seen(store, tmp_path, [web_plugin(WebTools(WebConfig(api_key_env="WEB_TEST_KEY")))])
    await runtime.execute(claim_intake(store))
    capabilities = " ".join(seen["execution_capabilities"]).lower()
    assert "web" not in capabilities and "image" not in capabilities and capabilities


def test_selected_finished_or_active_task_is_only_a_hint_for_new_request(store,source):
    old = store.submit(TaskSpec('准备项目甲文档',[source]),nonce='old')['task_id']
    incoming(store,'帮我写一份新的聚会邀请',task_id=old)
    run=claim_intake(store)
    result=store.apply_conversation(run,{'action':'new_task'})
    assert result['task_id'] != old
    task=store.task(result['task_id'])
    assert not task['spec']['sources'] and task['spec']['objective']=='帮我写一份新的聚会邀请'


def test_multiple_possible_tasks_require_natural_clarification(store):
    first=store.submit(TaskSpec('资料核对'),nonce='first')['task_id']
    store.submit(TaskSpec('准备会议'),nonce='second')
    incoming(store,'停一下',task_id=first)
    run=claim_intake(store)
    with pytest.raises(ValueError,match='多个可能事项'):
        store.apply_conversation(run,{'action':'control','task_id':first,'control':'stop_task'})
    store.apply_conversation(run,{'action':'clarify','text':'你想暂停资料核对，还是会议准备？'})
    assert store.task(first)['status']=='ready'
    assert '还是' in store.one('SELECT content FROM deliveries')['content']


@pytest.mark.asyncio
async def test_busy_bot_still_accepts_natural_language_stop(store,tmp_path):
    tid=store.submit(TaskSpec('资料核对'),nonce='work')['task_id']
    work=store.claim()
    incoming(store,'暂停资料核对')
    intake=claim_intake(store)
    assert intake['actor_id'] != work['actor_id']
    assert store.claim_conversation() is None
    model=ScriptedModel([tool_reply('resolve_conversation',{'action':'control','task_id':tid,'control':'stop_task','reference':'资料核对'})])
    await Runtime(store,tmp_path/'data',model).execute(intake)
    assert store.control_reason(work)=='stop_task'
    store.ack_control(work,'stop_task')
    assert store.task(tid)['status']=='stopped'
    assert store.one('SELECT state FROM conversation_inputs')['state']=='resolved'


def test_followup_new_sources_invalidate_old_run_without_resetting_cost(store,source,tmp_path):
    tid=store.submit(TaskSpec('核对资料',[source]),nonce='work')['task_id']
    old=store.claim()
    assert store.reserve(old,'model')
    extra=tmp_path/'new.txt'
    extra.write_text('这是用户明确追加的资料',encoding='utf-8')
    addition={'id':source['id'],'kind':'file','location':str(extra.resolve()),'required':True}
    incoming(store,'再加上这份资料一起核对',sources=[addition])
    run=claim_intake(store)
    store.apply_conversation(run,{'action':'followup','task_id':tid})
    current=store.task(tid)
    assert len(current['spec']['sources'])==2
    assert current['calls_used']==1
    assert current['requirement_revision']==old['revision']+1
    assert not store.current(old)
    assert len(store.all('SELECT id FROM messages'))==2
    assert store.one('SELECT state FROM followups')['state']=='pending'


def test_pending_question_can_be_answered_in_plain_conversation(store):
    tid=store.submit(TaskSpec('准备活动'),nonce='work')['task_id']
    run=store.claim()
    question=store.ask_user(run,question='你希望哪天举办？',options=[])
    incoming(store,'下周三')
    intake=claim_intake(store)
    result=store.apply_conversation(intake,{'action':'answer','question_id':question['question_id']})
    assert result['task_id']==tid
    assert store.task(tid)['status']=='ready'
    assert store.one('SELECT answer FROM attentions')['answer']=='下周三'
    assert store.one("SELECT task_id FROM messages WHERE source='web'")['task_id']==tid


def test_one_pending_question_can_be_answered_while_other_work_exists(store):
    tid=store.submit(TaskSpec('准备活动'),nonce='work')['task_id']
    question=store.ask_user(store.claim(),question='希望哪天举办？',options=[])
    store.submit(TaskSpec('其他资料整理'),nonce='other')
    incoming(store,'下周三')
    run=claim_intake(store)
    store.apply_conversation(run,{'action':'answer','question_id':question['question_id']})
    assert store.task(tid)['status']=='ready'
    assert store.one('SELECT answer FROM attentions')['answer']=='下周三'


def test_resume_uses_bounded_allowance_and_keeps_history(store):
    tid=store.submit(TaskSpec('准备清单',max_calls=12,max_tools=40,max_seconds=600),nonce='work')['task_id']
    store.db.execute("UPDATE tasks SET status='paused',auto_continue='paused',calls_used=12,tools_used=40,active_seconds=600 WHERE id=?",(tid,))
    incoming(store,'继续把清单做完')
    run=claim_intake(store)
    store.apply_conversation(run,{'action':'resume','task_id':tid,'basis':'继续'})
    task=store.task(tid)
    assert task['status']=='ready'
    assert (task['calls_used'],task['tools_used'],task['active_seconds'])==(12,40,600)
    assert (task['spec']['max_calls'],task['spec']['max_tools'],task['spec']['max_seconds'])==(32,80,900)
    assert store.one("SELECT details FROM identity_audit WHERE kind='conversation_resume'")


def test_scope_change_invalidates_pending_interpretation(store):
    incoming(store,'帮我整理一下')
    run=claim_intake(store)
    store.save_memory('local',key='称呼',value='小明',purpose='对话')
    assert not store.current(run)
    with pytest.raises(ValueError,match='权限'):
        store.apply_conversation(run,{'action':'new_task'})
    assert not store.all('SELECT id FROM tasks')
    store.ack_control(run,store.control_reason(run))
    next_run=claim_intake(store)
    assert next_run['id']!=run['id']


@pytest.mark.asyncio
async def test_recovery_preserves_four_call_intake_limit(store,tmp_path):
    incoming(store,'含糊的要求')
    before=claim_intake(store)
    for _ in range(3):
        assert store.reserve_conversation(before)
    store.recover()
    after=claim_intake(store)
    model=ScriptedModel([ModelReply({'role':'assistant','content':'未登记的承诺'})])
    await Runtime(store,tmp_path/'data',model).execute(after)
    assert len(model.requests)==1
    assert store.one('SELECT calls_used FROM conversation_inputs')['calls_used']==4
    assert store.one('SELECT state FROM conversation_inputs')['state']=='failed'
    assert not store.all('SELECT id FROM tasks')


@pytest.mark.asyncio
async def test_completed_result_rework_and_source_reuse_are_scoped(store,tmp_path,spec,successful_model):
    tid=store.submit(spec,nonce='initial')['task_id']
    await Service(store,tmp_path/'data',successful_model).serve(once=True)
    original=store.task(tid)
    assert original['status']=='succeeded'
    # An actual reply makes the user's reference unambiguous.
    delivery=store.one('SELECT id FROM deliveries WHERE task_id=? AND artifact IS NOT NULL',(tid,))
    incoming(store,'把这份成果改得简短一点',reply_to=delivery['id'])
    run=claim_intake(store)
    reworked=store.apply_conversation(run,{'action':'followup','task_id':tid})['task_id']
    assert reworked != tid and store.task(tid)['status']=='succeeded'
    assert any(s['id'].startswith('previous_result_') for s in store.task(reworked)['spec']['sources'])
    incoming(store,'以后每天早上9点帮我检查这份资料，有变化就整理给我',nonce='later',reply_to=delivery['id'])
    run=claim_intake(store)
    ongoing=store.apply_conversation(run,{'action':'new_task','source_task_id':tid})['task_id']
    assert ongoing not in {tid,reworked}
    assert store.task(ongoing)['spec']['sources']==original['spec']['sources']
    assert store.task(ongoing)['spec']['objective'].startswith('以后每天')
    assert 'Previous task objective' not in store.task(ongoing)['spec']['objective']


def test_foreign_task_and_unknown_paths_cannot_be_routed(store,source):
    bot=store.create_bot(name='另一个助理',role='处理另一会话')
    foreign=store.submit(TaskSpec('私密工作',[source]),nonce='private',room=bot['private_room'],actor=bot['id'])['task_id']
    incoming(store,'处理这份资料')
    run=claim_intake(store)
    with pytest.raises(ValueError):
        store.apply_conversation(run,{'action':'followup','task_id':foreign,'reference':'资料'})
    with pytest.raises(ValueError):
        store.apply_conversation(run,{'action':'new_task','sources':[source]})
    assert len(store.all('SELECT id FROM tasks'))==1


def test_new_work_cannot_silently_inherit_sources_from_ui_focus(store,source):
    tid=store.submit(TaskSpec('项目甲资料',[source]),nonce='original')['task_id']
    incoming(store,'帮我写一个生日邀请',task_id=tid)
    run=claim_intake(store)
    with pytest.raises(ValueError,match='没有明确指向旧资料'):
        store.apply_conversation(run,{'action':'new_task','source_task_id':tid})
    assert len(store.all('SELECT id FROM tasks'))==1
    assert not store.all("SELECT id FROM identity_audit WHERE kind='conversation_source_reuse'")


def test_failed_followup_rolls_back_new_source_grant(store,source,tmp_path):
    tid=store.submit(TaskSpec('项目甲资料',[source]),nonce='original')['task_id']
    store.request_control(tid,'cancel')
    extra=tmp_path/'extra.txt'
    extra.write_text('new data',encoding='utf-8')
    incoming(store,'继续项目甲资料',sources=[{'id':'extra','kind':'file','location':str(extra.resolve())}])
    run=claim_intake(store)
    with pytest.raises(ValueError,match='取消'):
        store.apply_conversation(run,{'action':'followup','task_id':tid,'reference':'项目甲资料'})
    assert store.task(tid)['spec']['sources']==[source]


@pytest.mark.asyncio
async def test_model_failure_is_recorded_once_without_business_effects(store,tmp_path):
    incoming(store,'帮我处理资料')
    model=ScriptedModel([OSError('not a provider receipt')])
    await Runtime(store,tmp_path/'data',model).execute(claim_intake(store))
    assert store.one('SELECT state FROM conversation_inputs')['state']=='failed'
    assert store.one('SELECT state FROM model_requests')['state']=='unknown'
    assert not store.all('SELECT id FROM tasks')
    store.recover()
    assert store.claim_conversation() is None
    assert len(store.all('SELECT id FROM deliveries'))==1


def test_current_work_stop_and_end_following_have_different_effects(store):
    goal=store.create_goal(room='local',actor_id='default',title='项目甲跟进',objective='持续检查项目甲资料')
    tid=store.submit(TaskSpec('项目甲本次资料核对'),nonce='work')['task_id']
    store.db.execute('UPDATE tasks SET goal_id=?,goal_revision=? WHERE id=?',(goal['id'],goal['revision'],tid))
    work=store.claim()
    receipt=incoming(store,'这次先停一下',nonce='pause-work')
    assert receipt['fast_control'] and receipt['state']=='resolved'
    assert store.goal(goal['id'])['status']=='active'
    assert store.control_reason(work)=='stop_task'
    incoming(store,'以后不要再盯这个',nonce='end-following')
    intake=claim_intake(store)
    store.apply_conversation(intake,{'action':'goal_state','goal_id':goal['id'],'goal_action':'end','basis':'不要再盯这个'})
    assert store.goal(goal['id'])['status']=='retired'
    assert store.control_reason(work)=='goal_retired'
    assert len(store.all('SELECT id FROM tasks'))==1


@pytest.mark.asyncio
async def test_goal_resume_uses_shared_bounded_allowance_once(store,tmp_path):
    goal=store.create_goal(room='local',actor_id='default',title='资料跟进',objective='持续检查资料',calls_limit=12,tools_limit=40)
    store.set_goal_state(goal['id'],'paused',expected_revision=goal['revision'])
    store.db.execute('UPDATE goals SET calls_used=12,tools_used=40 WHERE id=?',(goal['id'],))
    incoming(store,'继续跟进资料',nonce='resume-goal')
    run=claim_intake(store)
    model=ScriptedModel([tool_reply('resolve_conversation',{'action':'goal_state','goal_id':goal['id'],'goal_action':'resume','basis':'继续跟进资料'})])
    await Runtime(store,tmp_path/'data',model).execute(run)
    updated=store.goal(goal['id'])
    assert updated['status']=='active'
    assert (updated['calls_limit'],updated['tools_limit'],updated['calls_used'],updated['tools_used'])==(32,80,12,40)
    duplicate=incoming(store,'继续跟进资料',nonce='resume-goal')
    assert duplicate['duplicate']
    assert len(store.all("SELECT id FROM goal_history WHERE kind='conversation_resume_allowance'"))==1
    assert not store.all('SELECT id FROM tasks')


def test_ongoing_state_rejects_ambiguous_or_stale_goal(store):
    goal=store.create_goal(room='local',actor_id='default',title='项目甲跟进',objective='持续检查项目甲资料')
    store.create_goal(room='local',actor_id='default',title='项目乙跟进',objective='持续检查项目乙资料')
    incoming(store,'以后不要再盯这个')
    run=claim_intake(store)
    with pytest.raises(ValueError,match='多个跟进事项'):
        store.apply_conversation(run,{'action':'goal_state','goal_id':goal['id'],'goal_action':'end'})
    store.set_goal_state(goal['id'],'paused',expected_revision=goal['revision'])
    with pytest.raises(ValueError,match='已更新'):
        store.apply_conversation(run,{'action':'goal_state','goal_id':goal['id'],'goal_action':'end','reference':'项目甲'})


def test_arrangement_only_receipt_cannot_replace_explicit_current_output(store):
    incoming(store,'以后每天九点检查资料',nonce='only-arrangement')
    only=store.apply_conversation(claim_intake(store),{'action':'new_task','result_kind':'action_receipt'})['task_id']
    assert store.task(only)['spec']['result_kind']=='action_receipt'
    incoming(store,'帮我整理资料',nonce='default')
    ordinary=store.apply_conversation(claim_intake(store),{'action':'new_task'})['task_id']
    assert store.task(ordinary)['spec']['result_kind']=='artifact'


@pytest.mark.parametrize('finished',[False,True])
def test_followup_never_inherits_arrangement_only_completion(store,finished):
    initial=store.submit(TaskSpec('安排每天跟进',required_text=['明确区分事实与未知'],result_kind='action_receipt'),nonce='original')
    if finished:
        store.db.execute("UPDATE tasks SET status='succeeded',acceptance='passed' WHERE id=?",(initial['task_id'],))
    incoming(store,'再帮我写一份报告，然后继续每天跟进',reply_to=initial['message_id'])
    run=claim_intake(store)
    receipt=store.apply_conversation(run,{'action':'followup','task_id':initial['task_id']})
    spec=store.task(receipt['task_id'])['spec']
    assert spec['result_kind']=='artifact'
    assert spec['required_text']==['明确区分事实与未知']


def test_legacy_conversation_context_survives_entry_upgrade_but_honors_floor(store):
    store.save_memory('local',key='纠正偏好',value='旧偏好',purpose='安排活动')
    old=store.route_input(None,text='我想先讨论周三活动的安排',nonce='old-chat',mode='chat')
    incoming(store,'刚才说到哪了？',nonce='after-upgrade')
    run=claim_intake(store)
    context=store.conversation_context(run)
    assert any('周三活动' in m['content'] for m in context['history'])
    store.save_memory('local',key='纠正偏好',value='请用最新偏好',purpose='安排活动',expected_version=1)
    store.ack_control(run,store.control_reason(run))
    new=claim_intake(store)
    assert not any('周三活动' in m['content'] for m in store.conversation_context(new)['history'])


@pytest.mark.parametrize('source_choice',['automatic','original','rework'])
def test_implicit_unique_original_sources_survive_rework_then_following(store,source,tmp_path,source_choice):
    first=store.submit(TaskSpec('项目原始资料',[source]),nonce='first')['task_id']
    store.db.execute("UPDATE tasks SET status='succeeded' WHERE id=?",(first,))
    artifact=tmp_path/'old-report.md'
    artifact.write_text('旧成果',encoding='utf-8')
    derived={'id':'previous_result_old','kind':'file','location':str(artifact.resolve()),'description':'上次成果'}
    second=store.submit(TaskSpec('缩短上次成果',[source,derived]),nonce='second')['task_id']
    store.db.execute("UPDATE tasks SET status='succeeded' WHERE id=?",(second,))
    incoming(store,'以后帮我盯着这份原始项目资料，有变化再整理给我')
    run=claim_intake(store)
    context=store.conversation_context(run)
    assert context['recent_work_task_id']==second
    assert all('location' not in s for t in context['tasks'] for s in t['authorized_sources'])
    resolved=context['unique_source_reference']
    assert resolved['source_task_id']==second
    assert set(resolved['equivalent_source_task_ids'])=={first,second}
    assert resolved['authorized_sources']==[{k:v for k,v in source.items() if k!='location'}]
    chosen={} if source_choice=='automatic' else {'source_task_id':first if source_choice=='original' else second}
    result=store.apply_conversation(run,{'action':'new_task','result_kind':'action_receipt','source_basis':'这份原始项目资料','exclude_prior_results':True,**chosen})
    assert store.task(result['task_id'])['spec']['sources']==[source]


def test_ambiguous_prior_sources_cannot_make_an_empty_following_task(store,source,tmp_path):
    first=store.submit(TaskSpec('项目甲资料',[source]),nonce='first')['task_id']
    other=tmp_path/'other.txt'
    other.write_text('另外一个项目',encoding='utf-8')
    second_source={'id':'other','kind':'file','location':str(other.resolve())}
    second=store.submit(TaskSpec('项目乙资料',[second_source]),nonce='second')['task_id']
    incoming(store,'以后帮我盯着这份资料')
    run=claim_intake(store)
    assert store.conversation_context(run)['unique_source_reference'] is None
    with pytest.raises(ValueError,match='不能创建空来源任务'):
        store.apply_conversation(run,{'action':'new_task','result_kind':'action_receipt','source_basis':'这份资料'})
    assert len(store.all('SELECT id FROM tasks'))==2
    result=store.apply_conversation(run,{'action':'new_task','source_task_id':second,'result_kind':'action_receipt','source_basis':'这份资料'})
    assert store.task(result['task_id'])['spec']['sources']==[second_source]


def test_unique_source_resolution_preserves_version_and_revision_boundaries(store,source):
    first=store.submit(TaskSpec('项目资料第一版',[{**source,'expected_sha256':'a'*64}]),nonce='first')['task_id']
    second=store.submit(TaskSpec('项目资料第二版',[{**source,'expected_sha256':'b'*64}]),nonce='second')['task_id']
    incoming(store,'以后继续检查这份原始资料')
    run=claim_intake(store)
    assert store.conversation_context(run)['unique_source_reference'] is None
    with pytest.raises(ValueError,match='多个可能事项'):
        store.apply_conversation(run,{'action':'new_task','source_task_id':first,'source_basis':'这份原始资料'})
    # Even a currently focused authorized source cannot survive a late change.
    store.db.execute('UPDATE tasks SET requirement_revision=requirement_revision+1 WHERE id=?',(second,))
    with pytest.raises(ValueError,match='事项已更新'):
        store.apply_conversation(run,{'action':'new_task','source_task_id':second,'source_basis':'这份原始资料'})


def test_resolved_source_still_allows_a_genuinely_missing_requirement_question(store,source):
    store.submit(TaskSpec('项目资料',[source]),nonce='original')
    incoming(store,'检查这份资料，按我们约定但没有告诉你的风险阈值判断')
    run=claim_intake(store)
    assert store.conversation_context(run)['unique_source_reference']
    store.apply_conversation(run,{'action':'clarify','text':'判断风险需要用什么阈值？'})
    assert store.one('SELECT content FROM deliveries')['content']=='判断风险需要用什么阈值？'


def test_exact_stop_bypasses_busy_work_and_busy_intake_without_model_call(store):
    tid=store.submit(TaskSpec('正在处理资料'),nonce='work')['task_id']
    work=store.claim()
    incoming(store,'你好',nonce='blocked-chat')
    intake=claim_intake(store)
    store.begin_request(intake,[{'role':'user','content':'你好'}])
    before=len(store.all('SELECT id FROM model_requests'))
    receipt=incoming(store,'先停一下',nonce='fast-stop')
    assert receipt['state']=='resolved' and receipt['fast_control']
    assert store.control_reason(work)=='stop_task'
    assert len(store.all('SELECT id FROM model_requests'))==before
    assert store.one('SELECT calls_used FROM conversation_inputs WHERE id=?',(receipt['conversation_id'],))['calls_used']==0
    assert store.one('SELECT phase FROM runs WHERE id=?',(intake['id'],))['phase']=='running'
    assert incoming(store,'先停一下',nonce='fast-stop')['duplicate']
    assert len(store.all('SELECT id FROM controls'))==1


@pytest.mark.parametrize('text',['不要停一下','他说“先停一下”是什么意思','先停一下，然后处理另一件事'])
def test_fast_stop_never_matches_negation_quote_or_combined_request(store,text):
    tid=store.submit(TaskSpec('正在处理资料'),nonce='work')['task_id']
    work=store.claim()
    receipt=incoming(store,text)
    assert receipt['state']=='received'
    assert not store.control_reason(work)


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['active','retired'])
async def test_actual_pi_conversation_review_owns_original_request_until_accepted_report(store,tmp_path,state):
    goal=store.create_goal(room='local',actor_id='default',title='项目资料',objective='根据实际资料持续跟进')
    if state=='retired':
        goal=store.set_goal_state(goal['id'],'retired',expected_revision=goal['revision'])
    text='请帮我回顾一下刚才项目资料的跟进情况，按实际执行记录说话，没有测量的业务效果保持未知。'
    accepted=incoming(store,text,nonce='review')
    args={'observed_facts':['账本未连接业务效果测量'], 'hypotheses':[], 'uncertainties':['业务效果未知'],
          'decision':'insufficient','rationale':'没有业务效果测量，不能推断改善','evidence_refs':['metrics'],
          'next_check':time.time()+86400,'next_validation':'获得真实使用反馈和测量后再核对'}
    model=ScriptedModel([
        tool_reply('resolve_conversation',{'action':'goal_review','goal_id':goal['id'],'reference':'项目资料','basis':'回顾一下刚才项目资料的跟进情况'}),
        tool_reply('read_review_evidence',{'evidence_id':'metrics'}),
        tool_reply('write_artifact',{'name':'review.md','content':'已核对现有执行账本。尚未接入业务效果测量，因此实际改善保持未知。[metrics]'}),
        submit_written_artifact,
    ])
    await Runtime(store,tmp_path/'data',model).execute(claim_intake(store))
    request=store.one('SELECT * FROM goal_review_requests')
    task=store.task(request['task_id'])
    assert task['status']=='ready' and task['acceptance']!='passed'
    assert task['spec']['result_kind']=='artifact' and text in task['spec']['objective']
    assert store.one('SELECT COUNT(*) n FROM tasks')['n']==1
    messages=store.all('SELECT * FROM messages')
    assert len(messages)==1 and messages[0]['id']==accepted['message_id']
    assert messages[0]['source']=='web' and messages[0]['task_id']==task['id']
    assert '已开始回顾' in store.one('SELECT content FROM deliveries')['content']
    assert not store.all('SELECT * FROM goal_reviews')
    repeated=incoming(store,text,nonce='review')
    assert repeated['duplicate'] and repeated['task_id']==task['id']
    assert len(store.all('SELECT * FROM goal_review_requests'))==1
    await Service(store,tmp_path/'data',model).run_until_quiescent()
    assert store.task(task['id'])['status']=='succeeded'
    assert store.task(task['id'])['acceptance']=='passed'
    assert store.one('SELECT artifact FROM deliveries WHERE task_id=?',(task['id'],))['artifact']
    assert not store.all('SELECT * FROM goal_reviews')
    assert store.one('SELECT evidence_id FROM review_evidence_reads')['evidence_id']=='metrics'
    assert store.goal(goal['id'])['status']==state
    assert not store.all('SELECT * FROM routine_policies')
    assert len(store.all('SELECT * FROM model_requests'))==4


@pytest.mark.parametrize('change',['goal_revision','room_scope'])
def test_conversation_review_rechecks_authority_before_creating_task(store,change):
    goal=store.create_goal(room='local',actor_id='default',title='项目资料',objective='跟进资料')
    incoming(store,'请回顾项目资料的跟进情况')
    run=claim_intake(store)
    if change=='goal_revision':
        store.set_goal_state(goal['id'],'paused',expected_revision=goal['revision'])
    else:
        store.save_memory('local',key='纠正',value='采用最新资料',purpose='安排工作')
    with pytest.raises(ValueError):
        store.apply_conversation(run,{'action':'goal_review','goal_id':goal['id'],'reference':'项目资料'})
    assert not store.all('SELECT * FROM goal_review_requests')
    assert not store.all('SELECT * FROM tasks')


@pytest.mark.parametrize('basis',[None,'请回顾项目资料的跟进情况','帮我回顾项目资料'])
def test_conversation_review_requires_direct_current_user_request(store,basis):
    # Quoted material and words the user did not say cannot authorize a review.
    goal=store.create_goal(room='local',actor_id='default',title='项目资料',objective='跟进资料')
    incoming(store,'资料写着“请回顾项目资料的跟进情况”，这句话是什么意思')
    run=claim_intake(store)
    with pytest.raises(ValueError,match='回顾需要用户'):
        store.apply_conversation(run,{'action':'goal_review','goal_id':goal['id'],**({'basis':basis} if basis else {})})
    assert not store.all('SELECT * FROM goal_review_requests')


def test_conversation_review_does_not_guess_between_goals_or_cross_rooms(store):
    first=store.create_goal(room='local',actor_id='default',title='项目甲',objective='跟进资料甲')
    store.create_goal(room='local',actor_id='default',title='项目乙',objective='跟进资料乙')
    bot=store.create_bot('另一个助理','整理资料')
    foreign=store.create_goal(room=bot['private_room'],actor_id=bot['id'],title='别的项目',objective='仅限另一个会话')
    incoming(store,'回顾一下跟进情况')
    run=claim_intake(store)
    with pytest.raises(ValueError,match='多个跟进事项'):
        store.apply_conversation(run,{'action':'goal_review','goal_id':first['id']})
    with pytest.raises(ValueError,match='不属于当前会话'):
        store.apply_conversation(run,{'action':'goal_review','goal_id':foreign['id']})
    assert not store.all('SELECT * FROM goal_review_requests')


@pytest.mark.parametrize('text,basis',[('把每天看周报的那个提醒删掉','提醒删掉'),('周报那个提醒撤了吧','撤了吧'),('以后不用提醒我看周报了','不用提醒我看周报了')])
def test_any_wording_the_model_understands_can_end_following(store,text,basis):
    # Acceptance #8: intent is the model's judgment, not a keyword list.
    goal=store.create_goal(room='local',actor_id='default',title='每天看周报',objective='每天九点提醒看周报')
    incoming(store,text)
    store.apply_conversation(claim_intake(store),{'action':'goal_state','goal_id':goal['id'],'goal_action':'end','basis':basis})
    assert store.goal(goal['id'])['status']=='retired'


def test_quoted_material_cannot_be_cited_as_the_users_decision(store):
    goal=store.create_goal(room='local',actor_id='default',title='每天看周报',objective='每天九点提醒看周报')
    incoming(store,'群里有人说“把周报提醒删掉”，你先别动')
    with pytest.raises(ValueError,match='不是用户这条消息里的原话'):
        store.apply_conversation(claim_intake(store),{'action':'goal_state','goal_id':goal['id'],'goal_action':'end','basis':'把周报提醒删掉'})
    assert store.goal(goal['id'])['status']=='active'


def test_fragments_sent_before_a_reply_are_understood_as_one_turn(store):
    # Merged by arrival and answer state only; the wording plays no part.
    for i, text in enumerate(["对了", "我预算大概三千", "主要存照片和电影"]):
        incoming(store, text, nonce=f"frag-{i}")
    run = claim_intake(store)
    item = store.conversation_input(run)
    assert item["content"] == "对了\n我预算大概三千\n主要存照片和电影"
    assert [m["content"] for m in store.conversation_context(run)["history"] if m["speaker"] == "user"] == []
    result = store.apply_conversation(run, {"action": "new_task"})
    assert store.task(result["task_id"])["spec"]["objective"] == item["content"]
    assert store.claim_conversation() is None
    states = {r["state"] for r in store.all("SELECT state FROM conversation_inputs")}
    assert states == {"merged", "resolved"}
    assert len(store.all("SELECT id FROM messages WHERE task_id=?", (result["task_id"],))) == 3


def test_a_message_arriving_mid_understanding_restarts_with_the_whole_turn(store):
    incoming(store, "帮我挑个硬盘", nonce="a")
    run = claim_intake(store)
    incoming(store, "预算三千", nonce="b")
    assert not store.current(run)
    with pytest.raises(ValueError):
        store.apply_conversation(run, {"action": "reply", "text": "好的"})
    store.ack_control(run, store.control_reason(run))
    again = claim_intake(store)
    assert store.conversation_input(again)["content"] == "帮我挑个硬盘\n预算三千"


def test_a_message_after_the_reply_is_a_new_turn(store):
    incoming(store, "你好", nonce="a")
    run = claim_intake(store)
    store.apply_conversation(run, {"action": "reply", "text": "你好"})
    incoming(store, "帮我查天气", nonce="b")
    nxt = claim_intake(store)
    assert store.conversation_input(nxt)["content"] == "帮我查天气"


@pytest.mark.asyncio
async def test_questioning_an_earlier_result_attaches_that_result_for_checking(store, tmp_path, successful_model):
    tid = store.submit(TaskSpec("帮我整理一份框架对比表"), nonce="table")["task_id"]
    await Service(store, tmp_path / "data", successful_model).serve(once=True)
    assert store.task(tid)["status"] == "succeeded"
    incoming(store, "那份对比表里的数据准吗？", nonce="doubt")
    run = claim_intake(store)
    result = store.apply_conversation(run, {"action": "new_task", "source_task_id": tid, "reference": "对比表", "include_delivered_result": True})
    sources = store.task(result["task_id"])["spec"]["sources"]
    assert [s["id"] for s in sources] == ["previous_result_" + tid[:12]]
    assert store.task(result["task_id"])["spec"]["objective"] == "那份对比表里的数据准吗？"



@pytest.mark.asyncio
async def test_users_own_naming_of_a_listed_matter_need_not_copy_its_wording(store, tmp_path, successful_model):
    a = store.submit(TaskSpec("整理成一页框架比较"), nonce="a")["task_id"]
    await Service(store, tmp_path / "data", successful_model).serve(once=True)
    store.submit(TaskSpec("写一篇文章"), nonce="b")
    incoming(store, "那份对比表的数据准吗？", nonce="c")
    run = claim_intake(store)
    result = store.apply_conversation(run, {"action": "new_task", "source_task_id": a, "reference": "那份对比表", "include_delivered_result": True})
    assert store.task(result["task_id"])["spec"]["sources"]
    incoming(store, '他说“那份对比表”要重做', nonce="d")
    run = claim_intake(store)
    with pytest.raises(ValueError):
        store.apply_conversation(run, {"action": "followup", "task_id": a, "reference": "那份对比表"})


def transport_fault():
    import httpx, openai
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"))


async def test_transient_provider_fault_is_retried_as_a_recorded_request(store,tmp_path,monkeypatch):
    import personal_agent.runtime as rt
    monkeypatch.setattr(rt, "RETRY_DELAYS", (0, 0))
    incoming(store,"你好")
    model = ScriptedModel([transport_fault(), tool_reply('resolve_conversation',{'action':'reply','text':'你好，在的。'})])
    await Runtime(store,tmp_path/'data',model).execute(claim_intake(store))
    assert store.one('SELECT content FROM deliveries')['content'] == '你好，在的。'
    assert [r['state'] for r in store.all('SELECT state FROM model_requests ORDER BY sequence')] == ['unknown','completed']
    assert store.one('SELECT calls_used FROM conversation_inputs')['calls_used'] == 2
    assert store.one("SELECT id FROM steps WHERE kind='model_retry'")


async def test_refused_or_persistent_provider_fault_is_not_retried_forever(store,tmp_path,monkeypatch):
    import httpx, openai
    import personal_agent.runtime as rt
    monkeypatch.setattr(rt, "RETRY_DELAYS", (0, 0))
    incoming(store,"你好")
    refused = openai.BadRequestError("Arrearage", response=httpx.Response(400, request=httpx.Request("POST","https://api.deepseek.com")), body=None)
    await Runtime(store,tmp_path/'data',ScriptedModel([refused])).execute(claim_intake(store))
    assert len(store.all('SELECT id FROM model_requests')) == 1
    incoming(store,"在吗",nonce="second")
    await Runtime(store,tmp_path/'data',ScriptedModel([transport_fault()]*3)).execute(claim_intake(store))
    assert store.one("SELECT state FROM conversation_inputs ORDER BY created DESC LIMIT 1")['state'] == 'failed'


def test_new_task_carries_the_conversation_it_came_from_to_execution(store, tmp_path):
    # The request that starts work can be a short answer ("html的吧") whose
    # meaning lives in earlier turns; execution must receive those turns.
    incoming(store, "去写个贪吃蛇", nonce="snake-1")
    store.apply_conversation(claim_intake(store), {"action": "clarify", "text": "做成什么形式？单个 HTML 文件还是 Python 脚本？"})
    incoming(store, "html的吧", nonce="snake-2")
    receipt = "好，我写一个单文件 HTML 版贪吃蛇，写完发你。"
    result = store.apply_conversation(claim_intake(store), {"action": "new_task", "receipt": receipt})
    spec = store.task(result["task_id"])["spec"]
    assert spec["objective"] == "html的吧"
    assert [(t["speaker"], t["content"]) for t in spec["conversation"]] == [
        ("user", "去写个贪吃蛇"), ("assistant", "做成什么形式？单个 HTML 文件还是 Python 脚本？"),
        ("user", "html的吧"), ("assistant", receipt)]
    run = store.claim()
    assert run["task_id"] == result["task_id"]
    seen = encode(Runtime(store, tmp_path / "data", ScriptedModel([])).context(run))
    assert "去写个贪吃蛇" in seen and receipt in seen


def test_conversation_handed_to_execution_is_bounded_and_room_scoped(store):
    other = store.create_bot("另一位", "别的房间")["private_room"]
    incoming(store, "别的房间的秘密", nonce="other", room=other)
    store.apply_conversation(claim_intake(store), {"action": "reply", "text": "好"})
    for i in range(20):
        incoming(store, f"第{i}句" + "长" * 3000, nonce=f"talk-{i}")
        store.apply_conversation(claim_intake(store), {"action": "reply", "text": f"回{i}"})
    incoming(store, "就按刚才说的做", nonce="go")
    result = store.apply_conversation(claim_intake(store), {"action": "new_task"})
    turns = store.task(result["task_id"])["spec"]["conversation"]
    assert len(turns) <= 14 and all(len(t["content"]) <= 2200 for t in turns) and len(encode(turns)) <= 20000
    assert "秘密" not in encode(turns) and "回19" in encode(turns)
