import {installReviews} from './reviews.js';

export function installGoals({state,api,modal,toast,refresh,esc,timeLabel,chooseTask,compose,request}){
  const $=q=>document.querySelector(q);
  let visible=false, signature='';
  const states={active:'正在跟进',paused:'已暂停',needs_attention:'需要你回应',achieved:'已完成',retired:'已结束'};
  const occurrenceStates={observing:'正在查看资料',queued:'等待前一项工作',dispatched:'正在处理',completed:'已有成果',failed:'这次检查没有完成',unchanged:'资料没有变化',skipped:'已跳过过期检查',ignored:'原安排已失效',coalesced:'已合并最新变化'};
  const goals=()=>state.data?.goals||[];
  const goal=id=>goals().find(g=>g.id===id);
  const reviewsUI=installReviews({state,api,modal,toast,refresh,esc,timeLabel,onTask:id=>{show(false);chooseTask(id);}});
  function show(on=true){visible=on;signature='';$('.main').classList.toggle('goals-open',on);$('#goal-panel').classList.toggle('hidden',!on);$('#open-goals').classList.toggle('active',on);$('#app').classList.remove('show-nav');render();}
  async function act(name,body,nonce=crypto.randomUUID()){const result=await api('/api/goals/'+name,{...body,nonce});await refresh(true);return result;}
  function scheduleText(p){
    const s=p.schedule||{},seconds=s.interval_seconds||60;
    const period=seconds%86400===0?seconds/86400+'天':seconds%3600===0?seconds/3600+'小时':seconds%60===0?seconds/60+'分钟':seconds+'秒';
    if(s.kind==='watch')return `每${period}查看资料，仅在变化时处理`;
    if(s.kind==='interval')return `每${period}处理一次`;
    if(s.kind==='calendar'){const days=s.weekdays||[0,1,2,3,4,5,6];return `${days.length===7?'每天':days.map(d=>['周一','周二','周三','周四','周五','周六','周日'][d]).join('、')} ${s.time}（${s.timezone==='Asia/Shanghai'?'北京时间':s.timezone||'当地时间'}）处理`;}
    return '安排尚未明确';
  }
  function card(g){
    const policies=g.policies||[],failed=policies.flatMap(p=>p.health||[]).filter(h=>h.state==='failed');
    const action=g.next_action||'暂时没有新的安排。';
    const statusText=g.current_state||'已记下这件事，后续进展会保留在对话里。';
    const controllable=['active','paused','needs_attention'].includes(g.status);
    const ended=['retired','achieved'].includes(g.status);
    const currentStatus=ended?'已结束，不再自动检查。':g.status==='paused'?'已暂停，暂不自动检查。':g.status==='needs_attention'?'需要处理后继续。':policies.length?'正在按以下安排跟进。':'尚未登记自动检查。';
    const arrangements=policies.map(p=>{const running=g.status==='active'&&p.state==='active';const label=ended?'已停用':g.status==='paused'||p.state==='paused'?'已暂停':running?'已生效':p.state==='active'?'等待继续':'已停用';return `<small>${esc(scheduleText(p))} · ${label}${running&&p.next_due?'<br>下次查看：'+esc(timeLabel(p.next_due)):''}</small>`;}).join('');
    const history=g.assessment_at?`<div class="goal-next" data-following-history><small>上次处理 · ${esc(timeLabel(g.assessment_at))}</small>${esc(statusText)}<small>当时记录的下一步：${esc(action)}</small></div>`:`<p class="goal-compact-note">${esc(statusText)}</p>`;
    return `<article class="goal-card"><div class="goal-card-head"><h3>${esc(g.title)}</h3><span class="pill goal-status ${g.status}">${states[g.status]||esc(g.status)}</span></div><div class="goal-owner">由 ${esc(state.data.bots.find(b=>b.id===g.actor_id)?.name||'已归档的助理')} 跟进</div>
      <div class="goal-next" data-following-arrangement><strong>当前安排</strong><div>${esc(currentStatus)}</div>${arrangements}${g.status==='needs_attention'&&g.wait_reason?'<small>'+esc(g.wait_reason)+'</small>':''}</div>${history}
      ${failed.length?'<p class="goal-health bad">有资料暂时无法读取，相关工作不会被当作已经完成。</p>':''}<div class="task-buttons"><button class="button" data-discuss-following="${g.id}">继续沟通</button>${controllable?`<button class="button" data-goal-state="${g.id}" data-status="${g.status==='active'?'paused':'active'}" data-rev="${g.revision}">${g.status==='active'?'暂停跟进':'继续跟进'}</button><button class="button" data-goal-end="${g.id}">结束这件事</button>`:''}</div>${reviewsUI.card(g)}
      <details data-goal-details="${g.id}"><summary>查看跟进记录</summary><p class="goal-compact-note">事项记录：${esc(g.objective)}</p>${[...(g.gaps||[]).map(x=>'还需完成：'+x),...(g.unknowns||[]).map(x=>'尚待了解：'+x)].map(x=>`<p class="goal-compact-note">${esc(x)}</p>`).join('')}${(g.occurrences||[]).map(o=>`<div class="goal-log"><span>${esc(occurrenceStates[o.status]||o.status)}<br>${esc(timeLabel(o.scheduled_at))}</span>${o.task_id?`<button class="small-link" data-goal-task="${o.task_id}">查看进展</button>`:''}</div>`).join('')}${!(g.occurrences||[]).length?'<p class="goal-compact-note">后续处理记录会显示在这里。</p>':''}</details><details data-goal-details="technical-${g.id}" class="disclosure"><summary>技术详情</summary><div class="detail-row"><span>模型调用</span><strong>${g.calls_used} / ${g.calls_limit}</strong></div><div class="detail-row"><span>工具操作</span><strong>${g.tools_used} / ${g.tools_limit}</strong></div>${policies.map(p=>`<div class="goal-log"><span>${esc(p.template.objective)}</span><span>${p.state==='active'?'已登记':p.state==='paused'?'已暂停':'已结束'}</span></div>`).join('')}</details></article>`;
  }
  function render(){
    if(!state.data)return;
    $('#goal-count').textContent=goals().filter(g=>g.status==='active').length||'';
    if(!visible)return;
    const sig=JSON.stringify({room:state.room,goals:goals(),bots:state.data.bots});if(sig===signature)return;signature=sig;
    const box=$('#goal-panel'),expanded=[...box.querySelectorAll('details[open]')].map(d=>d.dataset.goalDetails);
    box.innerHTML=`<div class="goal-page-head"><div><h2>正在跟进的事情</h2><p>你交代过的事情、最新进展和接下来的安排。</p></div><button class="button" data-goal-back>返回对话</button></div>`+(goals().length?goals().map(card).join(''):'<div class="goal-empty"><div class="hero-mark">◷</div><h3>有需要持续留意的事，直接告诉我。</h3><p>例如：“以后帮我留意这份项目资料，<br>有变化就更新行动清单。”</p><button class="button" data-start-following>回到对话</button></div>')+'<p class="goal-compact-note">本机服务运行时会继续处理。需要调整安排或停止跟进，直接在对话里说就好。</p>';
    expanded.forEach(id=>{const d=box.querySelector(`[data-goal-details="${id}"]`);if(d)d.open=true;});
  }
  $('#open-goals').addEventListener('click',()=>show(!visible));
  document.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b||b.disabled)return;
    try{
      if(b.hasAttribute('data-goal-back')||b.hasAttribute('data-start-following')){show(false);$('#message-input').focus();}
      if(b.dataset.discussFollowing){const g=goal(b.dataset.discussFollowing);compose(`关于「${g.title}」：`,g.actor_id);}
      if(b.dataset.goalTask){show(false);chooseTask(b.dataset.goalTask);}
      if(b.dataset.goalEnd){const g=goal(b.dataset.goalEnd);modal('结束这件事？',`<p class="modal-note">结束后不再继续跟进或处理未完成的工作。已有记录和成果保留。</p><div class="modal-actions"><button class="button" data-goal-state="${g.id}" data-status="retired" data-rev="${g.revision}">不再跟进</button><button class="button primary" data-goal-state="${g.id}" data-status="achieved" data-rev="${g.revision}">这件事已完成</button></div>`);}
      if(b.dataset.goalState){b.disabled=true;if(b.dataset.status==='active'){const g=goal(b.dataset.goalState);await request(`请继续跟进「${g.title}」，沿用已确定的安排和资料。`,{actorId:g.actor_id,key:'following:'+g.id});}else{await act('state',{id:b.dataset.goalState,status:b.dataset.status,revision:Number(b.dataset.rev)});$('#modal').close();toast('跟进状态已更新');}}
    }catch(error){toast(error.message);}finally{b.disabled=false;}
  });
  return {render,show};
}
