import {installGoals} from './goals.js';
const $ = (q, root=document) => root.querySelector(q);
const $$ = (q, root=document) => [...root.querySelectorAll(q)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {ready:'排队中',running:'正在处理',waiting_children:'后台协作中',waiting_dependencies:'等待前置成果复核',waiting_user:'等你回答',paused:'已暂停',stopped:'已停止',cancelled:'已取消',succeeded:'已完成',failed:'遇到问题',review:'待检查'};
const state = {token:'', room:'local', workspace:'', storagePrefix:'', recipient:'', replyTo:null, task:null, tab:'details', data:null, online:false, uploads:[], urls:[], sending:false, composing:false, answers:{}, draftKey:'pa.draft.local', signature:'', detailSignature:'', retry:null};
let toastTimer;
const timeLabel = seconds => seconds ? new Date(seconds*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}) : '';
const pill = status => `<span class="pill ${esc(status)}">${esc(labels[status] || status)}</span>`;
function toast(text){$('#toast').textContent=text;$('#toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),5000);}
async function api(path, body, options={}, retryToken=true){
  const headers = {'X-PA-Token':state.token};
  if(body && !(body instanceof FormData)) headers['Content-Type']='application/json';
  const res=await fetch(path,{method:body?'POST':'GET',headers,body:body instanceof FormData?body:body?JSON.stringify(body):undefined,...options});
  if(res.status===403&&body&&retryToken){
    const boot=await api('/api/bootstrap');
    if(boot.workspace_id!==state.workspace){switchWorkspace(boot);await refresh(true);throw new Error('资料库已切换，请在当前会话重新确认操作');}
    if(boot.token!==state.token){state.token=boot.token;return api(path,body,options,false);}
  }
  let data;try{data=await res.json();}catch{throw new Error(res.status===403?'页面凭证已更新，请刷新后重试':'服务返回了无法读取的内容');}
  if(!res.ok)throw new Error(data.error||'操作未完成');
  return data;
}
function rich(text){
  const inline=line=>esc(line).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
  const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
  return String(text).split(/```[^\n]*\n?/).map((part,partIndex)=>{
    if(partIndex%2)return `<pre><code>${esc(part)}</code></pre>`;
    const lines=part.split('\n'),out=[];
    for(let i=0;i<lines.length;i++){
      const line=lines[i],value=inline(line);
      if(line.includes('|')&&i+1<lines.length&&cells(lines[i+1]).length>1&&cells(lines[i+1]).every(x=>/^:?-{3,}:?$/.test(x))){
        const head=cells(line);i+=2;const rows=[];
        while(i<lines.length&&lines[i].includes('|')){rows.push(cells(lines[i]));i++;}i--;
        out.push(`<div class="table-scroll"><table><thead><tr>${head.map(c=>`<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${head.map((_,j)=>`<td>${inline(row[j]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      }else if(value.startsWith('### '))out.push(`<h3>${value.slice(4)}</h3>`);
      else if(value.startsWith('## '))out.push(`<h2>${value.slice(3)}</h2>`);
      else if(value.startsWith('# '))out.push(`<h2>${value.slice(2)}</h2>`);
      else if(/^[-*] /.test(value))out.push(`<div class="list-line">· ${value.slice(2)}</div>`);
      else out.push(value?`<p>${value}</p>`:'<br>');
    }
    return out.join('');
  }).join('');
}
function selected(){return state.data?.tasks.find(t=>t.id===state.task);}
function currentRoom(){return state.data?.rooms.find(r=>r.id===state.room);}
function botName(id){return state.data?.bots.find(b=>b.id===id)?.name||'协作者';}
function taskTitle(task){const objective=task.spec.objective;const title=objective.split(/[。\n]/)[0];return title.length>80?title.slice(0,80)+"…":title;}
function roots(){return state.data?.tasks.filter(t=>!t.parent_task_id)||[];}
function artifactCard(d){return `<button class="artifact-card" data-artifact="${esc(d.id)}"><span class="file-symbol">▤</span><span>${state.data?.tasks.find(t=>t.id===d.task_id)?.review_report_id?'查看模型复盘草稿':'查看交付成果'}<small>${esc(timeLabel(d.created))} · 已保存</small></span><span class="arrow">↗</span></button>`;}
function roomView(){
  const sideSignature=JSON.stringify({rooms:state.data.rooms,room:state.room,recipient:state.recipient,replyTo:state.replyTo,tasks:roots().map(t=>[t.id,t.spec.objective,t.status,t.created]),search:$('#search').value,task:state.task,online:state.online,model:state.data.model,ready:state.data.ready,error:state.data.service_error});
  if(state.sideSignature===sideSignature)return;
  state.sideSignature=sideSignature;
  const search=$('#search').value.toLowerCase();
  $('#rooms').innerHTML=state.data.rooms.filter(r=>(r.members.length||r.id===state.room)&&(!search||r.name.toLowerCase().includes(search))).map(r=>`<button class="room-card ${r.id===state.room?'selected-room':''}" data-room="${esc(r.id)}"><span class="avatar">${r.kind==='group'?'◎':'✳'}</span><span class="room-copy"><strong>${esc(r.name)}</strong><small>${r.kind==='group'?r.members.length+' 位成员 · 群共享范围':'独立私聊 · 私有记忆'}</small></span><span class="dot"></span></button>`).join('');
  const room=currentRoom();$('#room-name').textContent=room?.name||'会话';
  const task=selected();
  $('#recipient').innerHTML=(room?.kind==='group'?'<option value="">指定成员或 @姓名</option>':'')+(room?.members||[]).map(b=>`<option value="${b.id}">${esc(b.name)}</option>`).join('');
  $('#recipient').value=state.recipient||room?.kind==='private'&&room?.members[0]?.id||'';
  $('#recipient').disabled=false;$('#recipient-row').classList.toggle('private-recipient',room?.kind==='private'&&!state.replyTo);
  $('#audience-note').textContent=state.replyTo?'正在回复选中的消息':room?.kind==='group'?'群成员共享这里的消息与记忆':'只在此私聊使用资料与记忆';
  $('#clear-reply').classList.toggle('hidden',!state.replyTo);
  const tasks=roots().filter(t=>t.spec.objective.toLowerCase().includes(search));
  $('#task-count').textContent=roots().length||'';
  $('#task-list').innerHTML=tasks.map(t=>`<button class="task-link ${state.task===t.id?'active':''}" data-task="${t.id}" title="${esc(taskTitle(t))}"><strong>${esc(taskTitle(t))}</strong><small><span>${esc(labels[t.status])}</span><span>${esc(timeLabel(t.created))}</span></small></button>`).join('')||'<p class="empty-detail">交办的事情会留在这里</p>';
  $('#model-label').textContent=`${state.data.model.name} · Qwen 官网`;
  const active=roots().filter(t=>['running','waiting_children'].includes(t.status)).length;
  $('#header-status').textContent=active?`${active} 项任务正在处理`:'帮你把事情持续管好';
  $('#connection').textContent=state.online?'已连接 · 运行在此电脑':'连接中断 · 草稿已保留';
  const banner=$('#banner');
  banner.classList.toggle('hidden',state.data.ready&&!state.data.service_error&&state.online);
  if(!state.online)banner.textContent='连接暂时中断。草稿会保留，恢复后可以继续。';
  else if(state.data.service_error)banner.textContent='后台服务需要检查，请查看任务状态后重新启动。已接收的任务会保留。';
  else if(!state.data.ready)banner.innerHTML='先配置你的 Qwen 官网 API Key，即可开始处理任务。 <button data-open-settings>去设置</button>';
}
function questionCard(q){
  const pending=q.state==='pending';
  const options=q.options.map((o,i)=>`<button class="button" data-answer="${q.id}" data-option="${i}" ${pending?'':'disabled'}>${esc(o)}</button>`).join('');
  return `<article class="question"><div class="question-label">${pending?'需要你补充':'问题记录'}</div><p>${esc(q.question)}</p><div class="question-options">${options}</div>${pending?`<div class="question-input"><input data-answer-input="${q.id}" placeholder="也可以直接输入你的回答" value="${esc(state.answers[q.id]||'')}"><button class="button" data-answer="${q.id}">回答</button></div>`:`<p class="muted">${q.state==='answered'?`已回答：${esc(q.answer)}`:'此问题已失效，无需回答'}</p>`}</article>`;
}
function messageView(force=false){
  const data=state.data, task=selected();
  const visible=id=>!id||!data.tasks.some(x=>x.id===id&&x.parent_task_id&&!data.bots.some(b=>b.id===x.actor_id));
  const focused=id=>!state.task||id===state.task||data.tasks.some(t=>t.id===id&&t.root_task_id===state.task);
  // Only explicit user ingress belongs in a user bubble. Scheduled work and
  // internal assignments stay in the ledger; their progress/results render below.
  const messages=data.messages.filter(m=>['web','cli','conversation'].includes(m.source)&&visible(m.task_id)&&focused(m.task_id));
  const deliveries=data.deliveries.filter(d=>visible(d.task_id)&&!['question','clarification'].includes(d.kind)&&focused(d.task_id));
  const questions=data.questions.filter(q=>!state.task||q.task_id===state.task||data.tasks.some(t=>t.id===q.task_id&&t.root_task_id===state.task));
  const sig=JSON.stringify({messages,deliveries,questions,task:task?.status,unassigned:data.unassigned,routes:data.pending_routes,selected:state.task,room:state.room});
  if(!force&&sig===state.signature)return;
  state.signature=sig;
  const box=$('#messages'), bottom=box.scrollHeight-box.scrollTop-box.clientHeight<90, oldScroll=box.scrollTop;
  const items=[...messages.map(m=>({sort:m.created||0,html:`<article class="message user"><div class="message-label">你 ${m.actor_id!=='unassigned'?'→ '+esc(botName(m.actor_id)):''}<time>${esc(timeLabel(m.created))}</time><button class="small-link" data-reply="${m.id}" data-tid="${m.task_id||''}" data-actor="${m.actor_id}">回复</button></div><div class="bubble">${esc(m.content)}</div></article>`})),...deliveries.map(d=>({sort:d.created||0,html:`<article class="message ${d.kind==='system'||d.kind==='clarification'?'system':'assistant'}"><div class="message-label">${d.kind==='result'?esc(botName(d.actor_id)):''} <time>${esc(timeLabel(d.created))}</time>${d.actor_id?`<button class="small-link" data-reply="${d.id}" data-tid="${d.task_id||''}" data-actor="${d.actor_id}">回复</button>`:''}</div><div class="bubble rich">${rich(d.content)}</div>${d.artifact?artifactCard(d):''}</article>`}))].sort((a,b)=>a.sort-b.sort);
  if(!items.length&&!questions.length){
    box.innerHTML='<div class="empty"><div class="hero-mark">✳</div><h2>把事情交给我，慢慢腾出手。</h2><p>整理资料、拆解任务、跟进进度。<br>你可以随时补充、暂停，或查看每一份成果。</p><div class="suggestions"><button class="suggestion" data-suggestion="帮我把这些资料整理成一份行动清单，标明优先级和待确认事项。"><span>▤</span>从一份资料开始<br><span class="muted">整理重点和下一步行动</span></button><button class="suggestion" data-suggestion="帮我规划接下来一周的个人工作，先问我需要了解的关键信息。"><span>☷</span>理清手头的事情<br><span class="muted">把想法变成可执行的计划</span></button></div></div>';
  }else{
    box.innerHTML=items.map(i=>i.html).join('')+questions.map(questionCard).join('')+(data.pending_routes||[]).map(p=>`<article class="question"><div class="question-label">选择接手对象</div><p>${esc(p.content)}</p><button class="button" data-route="${p.id}">分配这条消息</button></article>`).join('')+(data.unassigned||[]).map(f=>`<article class="question"><div class="question-label">待分配的补充</div><p>${esc(f.content)}</p><button class="button" data-followup-route="${f.id}">选择对应任务</button></article>`).join('');
    if(task&&['ready','running','waiting_children'].includes(task.status))box.insertAdjacentHTML('beforeend',`<article class="message system"><div class="bubble">${esc(labels[task.status])} <i class="spinner-dot"></i><i class="spinner-dot"></i><i class="spinner-dot"></i></div></article>`);
  }
  if(bottom||force)box.scrollTop=box.scrollHeight;else box.scrollTop=oldScroll;
  if(!task){const ongoing=roots().filter(t=>['ready','running','waiting_children'].includes(t.status));if(ongoing.length)box.insertAdjacentHTML('beforeend',`<div class="conversation-progress">${ongoing.slice(0,3).map(t=>`<button data-task="${t.id}">${esc(labels[t.status])}：${esc(taskTitle(t))}</button>`).join('')}</div>`);}
  $('#task-focus').classList.toggle('hidden',!task);
  $('#task-focus').innerHTML=task?`<span>正在查看：${esc(taskTitle(task))}</span><button data-clear-task title="查看全部对话">×</button>`:'';
}
async function detailView(force=false){
  const task=selected(), data=state.data, artifacts=data.deliveries.filter(d=>d.artifact);
  $('#artifact-count').textContent=artifacts.length||'';
  $$('.detail-tabs button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===state.tab)));
  const signature=JSON.stringify({tab:state.tab,task,artifacts,controls:data.controls,room:currentRoom(),memories:data.memories,proposals:data.memory_proposals});
  if(!force&&signature===state.detailSignature)return;
  state.detailSignature=signature;
  const box=$('#detail-content');
  if(state.tab==='memory'){memoryView(box);return;}
  if(state.tab==='library'){
    box.innerHTML='<div class="detail-heading">已交付的成果</div>'+ (artifacts.slice().reverse().map(artifactCard).join('')||'<div class="empty-detail">完成的清单、报告和资料<br>都会收在这里。</div>');return;
  }
  const room=currentRoom();
  let html=`<div class="profile"><div class="avatar">${room.kind==='group'?'◎':'✳'}</div><h2>${esc(room.name)}</h2><p>${room.kind==='group'?room.members.map(b=>esc(b.name)).join(' · '):esc(room.members[0]?.role||'已归档')}</p><button class="button" data-edit-room>管理${room.kind==='group'?'群成员':'角色'}</button></div>`;
  if(!task){box.innerHTML=html+'<div class="detail-heading">事情与进展</div><div class="detail-card muted">直接在对话里交代需求，或从左侧查看事情的进展和成果。<br><br>需要调整时，继续说就好；也可以随时暂停或取消。</div>';return;}
  const cancelled=task.cancel_state==='cancelled', done=task.status==='succeeded';
  const children=data.tasks.filter(t=>t.parent_task_id===task.id);
  const stopping=data.controls.some(c=>c.task_id===task.id&&c.state==='requested');
  html+=`<div class="detail-heading">这件事的进展</div><div class="detail-card">${stopping?'<span class="pill">正在停止…</span>':pill(task.status)}<h3>${esc(taskTitle(task))}</h3><div class="detail-row"><span>开始时间</span><strong>${esc(timeLabel(task.created))}</strong></div><div class="detail-row"><span>资料</span><strong>${task.spec.sources.length} 份</strong></div><div class="task-buttons">`;
  if(!done&&!cancelled){
    html+='<button class="button" data-compose-followup>补充要求</button>';
    if(['paused','failed','review','stopped'].includes(task.status))html+=`<button class="button" data-action="resume" data-tid="${task.id}">继续处理</button>`;
    else html+=`<button class="button" data-action="stop_task" data-tid="${task.id}">暂停这件事</button>`;
    html+=`<button class="button danger" data-action="cancel" data-tid="${task.id}">取消委托</button>`;
  }else if(done)html+='<button class="button" data-compose-followup>继续说说这件事</button>';
  html+='</div></div>';
  if(task.reason==='member_unavailable')html+=`<div class="detail-card"><p>原成员已不可用。任务、补充要求和预算均已保留。</p><button class="button" data-reassign="${task.id}">选择成员接手</button></div>`;
  if(children.length)html+='<div class="detail-heading">后台协作</div>'+children.map(c=>`<div class="worker"><strong>${esc(taskTitle(c))}</strong>${pill(c.status)} ${!['succeeded','cancelled','stopped'].includes(c.status)?`<button class="button" data-action="stop_task" data-tid="${c.id}">停止这项</button>`:''}</div>`).join('');
  if(task.spec.sources.length)html+='<div class="detail-heading">本任务资料</div>'+task.spec.sources.map(s=>`<div class="detail-row"><span>${esc(s.description||s.id)}</span><strong>${s.required===false?'可选':'必读'}</strong></div>`).join('');
  html+=`<details class="disclosure"><summary>技术详情</summary><div class="detail-row"><span>结构检查</span><strong>${task.acceptance==='passed'?'已通过（不等于内容无误）':task.acceptance==='needs_review'?'需要复核':'尚未通过'}</strong></div><div class="detail-row"><span>模型调用（含后台）</span><strong>${task.calls_used} / ${task.spec.max_calls}</strong></div><div class="detail-row"><span>工具操作（含后台）</span><strong>${task.tools_used} / ${task.spec.max_tools}</strong></div><div id="operation-list"></div></details>`;
  box.innerHTML=html;
  try{const detail=await api('/api/tasks/'+task.id);if(state.task!==task.id||state.tab!=='details')return;$('#operation-list').innerHTML=detail.operations.map(o=>`<div class="log-row"><span>${esc(o.tool)}</span><span>${esc(o.status)}</span></div>`).join('')||'<p class="muted">还没有工具操作记录</p>';}catch(e){toast(e.message);}
}
async function refresh(force=false){
  const requestedRoom=state.room;
  try{const data=await api('/api/state?room='+encodeURIComponent(requestedRoom));if(state.room!==requestedRoom)return;if(data.workspace_id!==state.workspace){switchWorkspace(await api('/api/bootstrap'));await refresh(true);return;}state.data=data;state.online=true;if(state.task&&!selected())state.task=null;roomView();messageView(force);await detailView(force);goalsUI.render();}
  catch(e){try{const boot=await api('/api/bootstrap');if(boot.workspace_id!==state.workspace){switchWorkspace(boot);await refresh(true);return;}}catch{}state.online=false;$('#connection').textContent='连接中断 · 草稿已保留';$('#banner').textContent='连接暂时中断。已输入的内容会保留，恢复后可以继续。';$('#banner').classList.remove('hidden');}
}
function saveDraft(){localStorage.setItem(state.draftKey,JSON.stringify({text:$('#message-input').value,uploads:state.uploads,urls:state.urls,task:state.task,retry:state.retry,recipient:state.recipient,replyTo:state.replyTo}));}
function restoreDraft(){state.draftKey=state.storagePrefix+'draft.'+state.room;try{const saved=JSON.parse(localStorage.getItem(state.draftKey)||'{}');$('#message-input').value=saved.text||'';state.uploads=saved.uploads||[];state.urls=saved.urls||[];state.task=saved.task||null;state.retry=saved.retry||null;state.recipient=saved.recipient||'';state.replyTo=saved.replyTo||null;}catch{}renderAttachments();}
function renderAttachments(){ $('#attachment-list').innerHTML=[...state.uploads.map((u,i)=>`<div class="attachment"><span>▤ ${esc(u.name)}</span><button type="button" data-remove-upload="${i}" aria-label="移除资料">×</button></div>`),...state.urls.map((u,i)=>`<div class="attachment"><span>↗ ${esc(u)}</span><button type="button" data-remove-url="${i}" aria-label="移除网址">×</button></div>`)].join('');}
function chooseTask(id){goalsUI.show(false);state.task=id;state.replyTo=null;state.recipient=id?selected()?.actor_id||'':state.recipient;state.signature='';state.detailSignature='';saveDraft();roomView();messageView(true);detailView(true);$('#app').classList.remove('show-nav');}
function composeAbout(text,actorId=null){
  goalsUI.show(false);state.task=null;state.replyTo=null;state.recipient=actorId||state.recipient;state.signature='';state.detailSignature='';state.retry=null;
  const input=$('#message-input');if(!input.value.trim())input.value=text;else toast('已有草稿已保留，你可以接着补充');saveDraft();roomView();messageView(true);detailView(true);input.focus();
}
async function chooseRoom(id){if(state.sending)return;goalsUI.show(false);saveDraft();state.room=id;localStorage.setItem(state.storagePrefix+'room',id);state.signature='';state.detailSignature='';state.sideSignature='';restoreDraft();await refresh(true);$('#app').classList.remove('show-nav');}
async function send(event){
  event.preventDefault();if(state.sending||state.composing)return;
  const text=$('#message-input').value.trim();if(!text)return;
  const mode='auto';
  if(!state.data?.ready){openSettings();return;}
  const payload={text,mode,room:state.room,actor_id:state.recipient||null,reply_to:state.replyTo,task_id:state.task,uploads:state.uploads.map(u=>u.id),urls:state.urls};
  const fingerprint=JSON.stringify(payload);
  const nonce=state.retry?.fingerprint===fingerprint?state.retry.nonce:crypto.randomUUID();
  state.retry={fingerprint,nonce};saveDraft();state.sending=true;$('#send').disabled=true;
  try{const receipt=await api('/api/messages',{...payload,nonce});$('#message-input').value='';state.uploads=[];state.urls=[];state.retry=null;state.replyTo=null;renderAttachments();state.task=null;state.signature='';state.detailSignature='';if(receipt.state==='context')toast('已记入群聊，没有启动新任务');saveDraft();await refresh(true);}
  catch(e){toast(e.message+'；内容已保留，可以重试');}
  finally{state.sending=false;$('#send').disabled=false;$('#message-input').focus();}
}
function modal(title, html){$('#modal-title').textContent=title;$('#modal-content').innerHTML=html;$('#modal').showModal();}
function botForm(bot=null){modal(bot?'编辑角色':'创建助理',`<p class="modal-note">每位助理都有独立私聊和记忆。角色说明决定它如何帮你，不能扩大资料权限。</p><form id="bot-form" data-id="${bot?.id||''}" data-revision="${bot?.revision||0}"><label class="field">名称<input name="name" maxlength="40" value="${esc(bot?.name||'')}" required></label><label class="field">角色说明<textarea name="role" rows="5" maxlength="4000" required>${esc(bot?.role||'')}</textarea></label><div class="modal-actions"><button class="button primary">保存</button></div></form>`);}
function manageBots(){modal('管理助理',`<p class="modal-note">复制只带走角色配置。私聊、记忆、密钥和持续任务均不会复制。</p><div class="manager-list">${state.data.bots.map(b=>`<article class="detail-card"><strong>${esc(b.name)}</strong><p class="muted">${b.archived?'已归档':esc(b.role.slice(0,100))}</p><div class="task-buttons"><button class="button" data-edit-bot="${b.id}">编辑</button><button class="button" data-copy-bot="${b.id}">复制</button><button class="button" data-archive-bot="${b.id}" data-archived="${b.archived?'false':'true'}">${b.archived?'恢复':'归档'}</button></div></article>`).join('')}</div><div class="modal-actions"><button class="button primary" data-new-bot>新建助理</button></div>`);}
function groupForm(room=null){modal(room?'管理群成员':'创建群聊',`<p class="modal-note">群内消息与记忆由这些成员共享，成员可以接收群内分配的工作。退出成员的未完成任务会暂停，等待你指定接手对象。</p><form id="group-form" data-id="${room?.id||''}" data-revision="${room?.revision||0}"><label class="field">群名称<input name="name" maxlength="80" value="${esc(room?.name||'')}" required></label><div class="member-options">${state.data.bots.filter(b=>!b.archived).map(b=>`<label><input type="checkbox" name="members" value="${b.id}" ${room?.members.some(m=>m.id===b.id)?'checked':''}> <span>${esc(b.name)}</span></label>`).join('')}</div><div class="modal-actions">${room?'<button type="button" class="button" data-save-team>保存为团队模板</button>':''}<button class="button primary">保存群聊</button></div></form>`);}
function newRoomMenu(){modal('添加到工作台',`<div class="new-options"><button class="button" data-new-bot>创建一位助理</button><button class="button" data-new-group>建立群聊</button></div>${state.data.team_templates.length?'<h3>使用团队模板</h3>'+state.data.team_templates.map(t=>`<div class="detail-row"><span>${esc(t.name)}</span><button class="button" data-use-team="${t.id}">创建新团队</button></div>`).join(''):''}<p class="modal-note">团队模板保存角色组合。使用模板会创建全新的成员与群聊，不带入原会话内容。</p>`);}
function memoryView(box){
  const group=currentRoom().kind==='group';
  box.innerHTML=`<div class="detail-heading">${group?'群共享记忆':'私聊记忆'} <button class="small-link" data-new-memory>＋ 添加</button></div><p class="memory-note">${group?'仅在这个群内使用，不读取成员的私聊记忆。':'只在这个私聊使用，加入群聊不会自动带过去。'}只有你保存或确认的内容才会长期记住。</p>`;
  box.innerHTML+=state.data.memories.map(m=>`<article class="detail-card memory-card"><strong>${esc(m.key)}</strong><span class="memory-version">版本 ${m.version}</span><p>${m.state==='deleted'?'<span class="muted">已删除 · 已停止加载旧内容</span>':esc(m.value)}</p><small class="muted">用途：${esc(m.purpose)}</small><div class="task-buttons"><button class="button" data-edit-memory="${m.id}">${m.state==='deleted'?'明确恢复':'修改'}</button>${m.state==='active'?`<button class="button" data-delete-memory="${m.id}">删除</button>`:''}</div><details class="disclosure"><summary>来源与范围</summary><p>${m.source_type==='user_explicit'?'用户明确保存':'用户确认建议'} · ${esc(m.speaker)}<br>${group?'此群':'此私聊'} · ${esc(timeLabel(m.updated))}</p></details></article>`).join('')||'<div class="empty-detail">暂时没有保存记忆。<br>你可以记录经常需要的偏好和背景。</div>';
  box.innerHTML+=state.data.memory_proposals.map(p=>`<article class="question"><div class="question-label">待你确认的记忆建议</div><strong>${esc(p.key)}</strong><p>${esc(p.value)}</p><p class="muted">用途：${esc(p.purpose)}</p><div class="task-buttons"><button class="button primary" data-memory-decision="${p.id}" data-accept="true">保存为记忆</button><button class="button" data-memory-decision="${p.id}" data-accept="false">不保存</button></div></article>`).join('');
}
function memoryForm(memory=null){modal(memory?.state==='deleted'?'明确恢复这条记忆':memory?'修正记忆':'添加长期记忆',`<p class="modal-note">保存到「${esc(currentRoom().name)}」。修改或删除后，正在执行的旧上下文会停止并重新加载。既有聊天记录与产物仍会保留，便于追溯。</p><form id="memory-form" data-id="${memory?.id||''}" data-version="${memory?.version||0}" data-restore="${memory?.state==='deleted'}"><label class="field">标题<input name="key" maxlength="100" value="${esc(memory?.key||'')}" ${memory?'readonly':''} required></label><label class="field">内容<textarea name="value" rows="5" maxlength="4000" required>${esc(memory?.value||'')}</textarea></label><label class="field">用途<input name="purpose" maxlength="200" value="${esc(memory?.purpose||'')}" placeholder="例如：安排日程时考虑我的作息" required></label><div class="modal-actions"><button class="button primary">${memory?.state==='deleted'?'确认恢复':'保存记忆'}</button></div></form>`);}
async function manageAction(name,body){const result=await api('/api/manage/'+name,body);$('#modal').close();await refresh(true);return result;}
function assignmentForm(routeId=null,taskId=null,followupId=null){const route=state.data.pending_routes.find(p=>p.id===routeId);const followup=Boolean(followupId)||route?.mode==='followup';modal(taskId?'选择接手成员':'分配已保存的消息',`<form id="assignment-form" data-route="${routeId||''}" data-tid="${taskId||''}" data-followup-id="${followupId||''}" data-followup="${Boolean(followup)}"><p class="modal-note">${taskId?'接手会保留原任务、预算和未处理的补充要求。':'这条消息不会丢失，也不会因为重复点击创建多份任务。'}</p><label class="field">${followup?'对应任务':'接手成员'}<select name="target" required>${followup?state.data.tasks.filter(t=>t.cancel_state==='active'&&t.status!=='succeeded').map(t=>`<option value="${t.id}">${esc(botName(t.actor_id))}：${esc(t.spec.objective.slice(0,60))}</option>`).join(''):currentRoom().members.map(b=>`<option value="${b.id}">${esc(b.name)}</option>`).join('')}</select></label><div class="modal-actions"><button class="button primary">确认分配</button></div></form>`);}
function openSettings(){const model=state.data?.model||{name:'qwen3.7-plus',base_url:'https://maas.qianwenaiapi.com/compatible-mode/v1'};modal('模型设置',`<p class="modal-note">使用你自己的 Qwen 官网账号。密钥保存在本机数据目录，不会显示在聊天或发送给其他网站。</p><form id="settings-form"><label class="field">模型型号<input name="name" value="${esc(model.name)}" required></label><label class="field">官网接口地址<input name="base_url" value="${esc(model.base_url)}" required><small>请使用与你的账号地域和服务匹配的官方接口。</small></label><label class="field">API Key<input name="key" type="password" autocomplete="new-password" placeholder="${state.data?.ready?'已配置；留空保持原密钥':'粘贴你的 Qwen 官网 API Key'}"><small>不会回显已保存的密钥。配置保存不代表连通测试通过。</small></label><div class="modal-actions"><button class="button primary" type="submit">保存配置</button></div></form><hr><h3>资料备份</h3><p class="modal-note">保存对话、记忆、成果与跟进记录。不包含模型密钥和数据目录外的原始文件。恢复时使用新目录，未完成工作保持暂停。</p><button class="button" data-download-backup>下载备份包</button>`);}
const continuationNonces=new Map();
async function sendIntent(text,{actorId=null,taskId=null,key=text}={}){
  const nonce=continuationNonces.get(key)||crypto.randomUUID();continuationNonces.set(key,nonce);
  await api('/api/messages',{text,mode:'auto',room:state.room,actor_id:actorId,task_id:taskId,reply_to:null,uploads:[],urls:[],nonce});
  continuationNonces.delete(key);$('#modal').close();chooseTask(null);await refresh(true);
}
async function performAction(name,taskId,extra={}){
  if(name==='resume'){
    const task=state.data.tasks.find(t=>t.id===taskId);if(!task)return;
    try{await sendIntent(`请继续处理「${taskTitle(task)}」，保留已经完成的进度。`,{actorId:task.actor_id,taskId,key:taskId});}catch(error){toast(error.message);}
    return;
  }
  try{const result=await api('/api/actions/'+name,{task_id:taskId,...extra});toast(result.state==='stopping'?'正在停止执行，已有进度会保留':'操作已记录');$('#modal').close();await refresh();}catch(error){toast(error.message);}
}
async function answer(qid,option){const q=state.data.questions.find(q=>q.id===qid);const text=option!==undefined?q.options[Number(option)]:state.answers[qid]?.trim();if(!text)return;try{const result=await api('/api/actions/answer',{question_id:qid,text,nonce:crypto.randomUUID()});if(result.state==='ignored_stale_answer')toast('这个问题已经失效，请查看当前任务');await refresh();}catch(e){toast(e.message);}}
$('#composer-form').addEventListener('submit',send);
$('#message-input').addEventListener('compositionstart',()=>state.composing=true);
$('#message-input').addEventListener('compositionend',()=>{state.composing=false;saveDraft();});
$('#message-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!state.composing&&e.keyCode!==229){e.preventDefault();$('#composer-form').requestSubmit();}});
$('#message-input').addEventListener('input',()=>{state.retry=null;saveDraft();});
$('#recipient').addEventListener('change',()=>{state.recipient=$('#recipient').value;state.replyTo=null;state.retry=null;saveDraft();roomView();});
$('#clear-reply').onclick=()=>{state.replyTo=null;saveDraft();roomView();};
$('#manage-bots').onclick=manageBots;
$('#new-room').onclick=newRoomMenu;
$('#search').addEventListener('input',()=>state.data&&roomView());
$('#new-task').onclick=()=>{chooseTask(null);$('#message-input').focus();};
$('#settings').onclick=openSettings;
$('#close-modal').onclick=()=>$('#modal').close();
$('#attach').onclick=()=>$('#file-input').click();
$('#add-url').onclick=()=>modal('添加资料网址','<p class="modal-note">只读取你提供的这个地址，不自动访问其他页面。文本页面支持良好；需要登录、JavaScript 或超大页面时可能无法读取。</p><form id="url-form"><label class="field">资料地址<input name="url" type="url" placeholder="https://…" required></label><div class="modal-actions"><button class="button primary">添加</button></div></form>');
$('#file-input').onchange=async e=>{for(const file of e.target.files){try{if(file.size>128000)throw new Error(file.name+' 超过 128 KB');const body=new FormData();body.append('file',file);const uploaded=await api('/api/uploads?room='+state.room,body);state.uploads.push(uploaded);renderAttachments();saveDraft();}catch(error){toast(error.message);}}e.target.value='';};
$('#show-details').onclick=()=>{if(innerWidth<=900)$('#app').classList.toggle('show-details');else $('#app').classList.toggle('no-details');};
$('#mobile-nav').onclick=()=>$('#app').classList.toggle('show-nav');
$('#jump-bottom').onclick=()=>{$('#messages').scrollTop=$('#messages').scrollHeight;};
$('#messages').onscroll=()=>{const b=$('#messages');$('#jump-bottom').classList.toggle('hidden',b.scrollHeight-b.scrollTop-b.clientHeight<150);};
document.addEventListener('input',e=>{if(e.target.dataset.answerInput)state.answers[e.target.dataset.answerInput]=e.target.value;});
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.task)chooseTask(b.dataset.task);
  if(b.dataset.room)await chooseRoom(b.dataset.room);
  if(b.dataset.reply){state.replyTo=b.dataset.reply;state.task=b.dataset.tid||null;state.recipient=b.dataset.actor==='unassigned'?'':b.dataset.actor;saveDraft();roomView();messageView();await detailView();$('#message-input').focus();}
  if(b.hasAttribute('data-new-bot'))botForm();
  if(b.hasAttribute('data-new-group'))groupForm();
  if(b.dataset.editBot)botForm(state.data.bots.find(x=>x.id===b.dataset.editBot));
  if(b.hasAttribute('data-edit-room')){const room=currentRoom();if(room.kind==='group')groupForm(room);else botForm(state.data.bots.find(x=>x.private_room===room.id));}
  if(b.hasAttribute('data-new-memory'))memoryForm();
  if(b.dataset.editMemory)memoryForm(state.data.memories.find(x=>x.id===b.dataset.editMemory));
  if(b.dataset.route)assignmentForm(b.dataset.route);
  if(b.dataset.followupRoute)assignmentForm(null,null,b.dataset.followupRoute);
  if(b.dataset.reassign)assignmentForm(null,b.dataset.reassign);
  try{
    if(b.dataset.copyBot){const copied=await manageAction('copy_bot',{id:b.dataset.copyBot});await chooseRoom(copied.private_room);toast('已复制角色，私聊与记忆从空白开始');}
    if(b.dataset.archiveBot){await manageAction('archive_bot',{id:b.dataset.archiveBot,archived:b.dataset.archived==='true'});toast('状态已更新，未完成的工作会保留');}
    if(b.dataset.deleteMemory){const m=state.data.memories.find(x=>x.id===b.dataset.deleteMemory);await manageAction('delete_memory',{room:state.room,id:m.id,version:m.version});toast('记忆已删除，旧内容不会再自动加载');}
    if(b.dataset.memoryDecision)await manageAction('decide_memory',{id:b.dataset.memoryDecision,accept:b.dataset.accept==='true'});
    if(b.hasAttribute('data-save-team')){await manageAction('save_team',{room:state.room,name:currentRoom().name+' 模板'});toast('已保存角色组合，可从新建入口使用');}
    if(b.dataset.useTeam){const template=state.data.team_templates.find(t=>t.id===b.dataset.useTeam);const group=await manageAction('instantiate_team',{id:template.id,name:template.name+' 新会话'});await chooseRoom(group.id);}
  }catch(error){toast(error.message);}
  if(b.dataset.tab){state.tab=b.dataset.tab;await detailView(true);}
  if(b.dataset.suggestion){$('#message-input').value=b.dataset.suggestion;saveDraft();$('#message-input').focus();}
  if(b.hasAttribute('data-clear-task'))chooseTask(null);
  if(b.hasAttribute('data-compose-followup')){saveDraft();$('#message-input').focus();}
  if(b.hasAttribute('data-open-settings'))openSettings();
  if(b.hasAttribute('data-download-backup')){b.disabled=true;try{await downloadBackup();}catch(error){toast(error.message);}finally{b.disabled=false;}}
  if(b.dataset.removeUpload!==undefined){state.uploads.splice(Number(b.dataset.removeUpload),1);renderAttachments();saveDraft();}
  if(b.dataset.removeUrl!==undefined){state.urls.splice(Number(b.dataset.removeUrl),1);renderAttachments();saveDraft();}
  if(b.dataset.answer)await answer(b.dataset.answer,b.dataset.option);
  if(b.dataset.action){if(b.dataset.action==='cancel')modal('取消这项委托？',`<p class="modal-note">这会停止当前任务及其后台协作。已有记录和产物会保留，普通消息不会重新启动这项已取消的任务。</p><div class="modal-actions"><button class="button danger" data-confirm-cancel="${b.dataset.tid}">取消委托</button></div>`);else {b.disabled=true;try{await performAction(b.dataset.action,b.dataset.tid);}finally{b.disabled=false;}}}
  if(b.dataset.confirmCancel)await performAction('cancel',b.dataset.confirmCancel);
  if(b.dataset.artifact){try{const data=await api('/api/artifacts/'+b.dataset.artifact);modal(data.name,`<div class="artifact-preview rich">${rich(data.text)}</div><div class="modal-actions"><button class="button" data-feedback-task="${esc(state.data.deliveries.find(d=>d.id===b.dataset.artifact)?.task_id||"")}">这份成果怎么样</button><a class="button" href="/r/${esc(state.data.deliveries.find(d=>d.id===b.dataset.artifact)?.task_id||"")}" target="_blank" rel="noopener">打开交付页</a><a class="button download" href="/api/artifacts/${b.dataset.artifact}?download=1" download="result.md">下载成果</a></div>`);}catch(error){toast(error.message);}}
});
document.addEventListener('submit',async e=>{
  if(['bot-form','group-form','memory-form','assignment-form'].includes(e.target.id)){
    e.preventDefault();const form=e.target,f=new FormData(form),id=form.dataset.id;const submit=form.querySelector('button[type="submit"],button.primary');if(submit?.disabled)return;if(submit)submit.disabled=true;
    try{
      if(form.id==='bot-form'){const bot=await manageAction(id?'update_bot':'create_bot',{id,name:f.get('name'),role:f.get('role'),revision:Number(form.dataset.revision)});await chooseRoom(bot.private_room);}
      if(form.id==='group-form'){const group=await manageAction(id?'update_group':'create_group',{id,name:f.get('name'),members:f.getAll('members'),revision:Number(form.dataset.revision)});await chooseRoom(group.id);}
      if(form.id==='memory-form')await manageAction('save_memory',{room:state.room,key:f.get('key'),value:f.get('value'),purpose:f.get('purpose'),version:Number(form.dataset.version),restore:form.dataset.restore==='true'});
      if(form.id==='assignment-form'){const body=form.dataset.tid?{task_id:form.dataset.tid,actor_id:f.get('target')}:form.dataset.followupId?{followup_id:form.dataset.followupId,task_id:f.get('target')}:{route_id:form.dataset.route,[form.dataset.followup==='true'?'task_id':'actor_id']:f.get('target')};await api('/api/actions/'+(form.dataset.tid?'reassign':form.dataset.followupId?'assign':'assign_route'),body);$('#modal').close();await refresh(true);}
    }catch(error){toast(error.message);}finally{if(submit)submit.disabled=false;}
  }
  if(e.target.id==='settings-form'){e.preventDefault();const form=new FormData(e.target),body={name:form.get('name'),base_url:form.get('base_url')};if(form.get('key'))body.key=form.get('key');try{await api('/api/settings',body);$('#modal').close();toast('配置已保存');await refresh();}catch(error){toast(error.message);}}
  if(e.target.id==='url-form'){e.preventDefault();state.urls.push(new FormData(e.target).get('url'));renderAttachments();saveDraft();$('#modal').close();}
});
$$('.resizer').forEach(r=>r.addEventListener('pointerdown',e=>{r.setPointerCapture(e.pointerId);const move=e=>{const width=r.dataset.panel==='left'?e.clientX:innerWidth-e.clientX;document.documentElement.style.setProperty(r.dataset.panel==='left'?'--left':'--right',Math.max(220,Math.min(440,width))+'px');};r.addEventListener('pointermove',move);r.addEventListener('pointerup',()=>r.removeEventListener('pointermove',move),{once:true});}));
const goalsUI=installGoals({state,api,modal,toast,refresh,esc,timeLabel,chooseTask,compose:composeAbout,request:sendIntent});
async function poll(){await refresh();setTimeout(poll,document.hidden?5000:1500);}
try{const boot=await api('/api/bootstrap');switchWorkspace(boot);await refresh(true);setTimeout(poll,1500);}catch(e){toast('无法连接本地服务，请确认启动窗口仍在运行。');}

async function downloadBackup(){
  let response=await fetch('/api/backup',{method:'POST',headers:{'X-PA-Token':state.token}});
  if(response.status===403){const boot=await api('/api/bootstrap');if(boot.workspace_id!==state.workspace){switchWorkspace(boot);await refresh(true);throw new Error('资料库已切换，请重新选择备份');}state.token=boot.token;response=await fetch('/api/backup',{method:'POST',headers:{'X-PA-Token':state.token}});}
  if(!response.ok){let error;try{error=await response.json();}catch{}throw new Error(error?.error||'备份未完成，请稍后重试');}
  const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||'personal-agent-backup.zip';
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);toast('备份包已生成，正在下载');
}

function switchWorkspace(boot){
  if(state.workspace)saveDraft();
  state.workspace=boot.workspace_id;state.storagePrefix='pa.'+state.workspace+'.';state.token=boot.token;
  state.room=localStorage.getItem(state.storagePrefix+'room')||'local';state.data=null;state.task=null;state.answers={};continuationNonces.clear();
  state.signature='';state.detailSignature='';state.sideSignature='';goalsUI.show(false);restoreDraft();
}
