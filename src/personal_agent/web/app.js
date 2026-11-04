const $ = (q, root=document) => root.querySelector(q);
const $$ = (q, root=document) => [...root.querySelectorAll(q)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {ready:'排队中',running:'正在处理',waiting_children:'后台协作中',waiting_dependencies:'等待前置成果复核',waiting_user:'等你回答',paused:'已暂停',stopped:'已停止',cancelled:'已取消',succeeded:'已完成',failed:'遇到问题',review:'待检查'};
const state = {token:'', room:'local', task:null, tab:'details', data:null, online:false, uploads:[], urls:[], sending:false, composing:false, answers:{}, draftKey:'pa.draft.local', signature:'', detailSignature:'', retry:null};
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
    if(boot.token!==state.token){state.token=boot.token;return api(path,body,options,false);}
  }
  let data;try{data=await res.json();}catch{throw new Error(res.status===403?'页面凭证已更新，请刷新后重试':'服务返回了无法读取的内容');}
  if(!res.ok)throw new Error(data.error||'操作未完成');
  return data;
}
function rich(text){
  return String(text).split(/```[^\n]*\n?/).map((part,i)=>i%2?`<pre><code>${esc(part)}</code></pre>`:esc(part).split('\n').map(line=>{
    let value=line.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
    if(value.startsWith('### '))return `<h3>${value.slice(4)}</h3>`;
    if(value.startsWith('## '))return `<h2>${value.slice(3)}</h2>`;
    if(value.startsWith('# '))return `<h2>${value.slice(2)}</h2>`;
    if(/^[-*] /.test(value))return `<div class="list-line">· ${value.slice(2)}</div>`;
    return value?`<p>${value}</p>`:'<br>';
  }).join('')).join('');
}
function selected(){return state.data?.tasks.find(t=>t.id===state.task);}
function roots(){return state.data?.tasks.filter(t=>!t.parent_task_id)||[];}
function artifactCard(d){return `<button class="artifact-card" data-artifact="${esc(d.id)}"><span class="file-symbol">▤</span><span>查看交付成果<small>${esc(timeLabel(d.created))} · 已保存</small></span><span class="arrow">↗</span></button>`;}
function roomView(){
  const sideSignature=JSON.stringify({rooms:state.data.rooms,tasks:roots().map(t=>[t.id,t.spec.objective,t.status,t.created]),search:$('#search').value,task:state.task,online:state.online,model:state.data.model,ready:state.data.ready,error:state.data.service_error});
  if(state.sideSignature===sideSignature)return;
  state.sideSignature=sideSignature;
  $('#rooms').innerHTML=state.data.rooms.map(r=>`<button class="room-card" data-room="${esc(r.id)}"><span class="avatar">✳</span><span class="room-copy"><strong>${esc(r.name)}</strong><small>${esc(r.description)}</small></span><span class="dot"></span></button>`).join('');
  const search=$('#search').value.toLowerCase();
  const tasks=roots().filter(t=>t.spec.objective.toLowerCase().includes(search));
  $('#task-count').textContent=roots().length||'';
  $('#task-list').innerHTML=tasks.map(t=>`<button class="task-link ${state.task===t.id?'active':''}" data-task="${t.id}" title="${esc(t.spec.objective)}"><strong>${esc(t.spec.objective)}</strong><small><span>${esc(labels[t.status])}</span><span>${esc(timeLabel(t.created))}</span></small></button>`).join('')||'<p class="empty-detail">交办的事情会留在这里</p>';
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
  const visible=t=>!t||!data.tasks.some(x=>x.id===t&&x.parent_task_id);
  const messages=data.messages.filter(m=>visible(m.task_id)&&(!state.task||m.task_id===state.task));
  const deliveries=data.deliveries.filter(d=>visible(d.task_id)&&d.kind!=='question'&&(!state.task||d.task_id===state.task));
  const questions=data.questions.filter(q=>!state.task||q.task_id===state.task||data.tasks.some(t=>t.id===q.task_id&&t.root_task_id===state.task));
  const sig=JSON.stringify({messages,deliveries,questions,task:task?.status,unassigned:data.unassigned,selected:state.task});
  if(!force&&sig===state.signature)return;
  state.signature=sig;
  const box=$('#messages'), bottom=box.scrollHeight-box.scrollTop-box.clientHeight<90, oldScroll=box.scrollTop;
  const items=[...messages.map(m=>({sort:m.created||0,html:`<article class="message user"><div class="message-label">你 <time>${esc(timeLabel(m.created))}</time></div><div class="bubble">${esc(m.content)}</div></article>`})),...deliveries.map(d=>({sort:d.created||0,html:`<article class="message ${d.kind==='system'||d.kind==='clarification'?'system':'assistant'}"><div class="message-label">${d.kind==='result'?'个人助理':''} <time>${esc(timeLabel(d.created))}</time></div><div class="bubble rich">${rich(d.content)}</div>${d.artifact?artifactCard(d):''}</article>`}))].sort((a,b)=>a.sort-b.sort);
  if(!items.length&&!questions.length){
    box.innerHTML='<div class="empty"><div class="hero-mark">✳</div><h2>把事情交给我，慢慢腾出手。</h2><p>整理资料、拆解任务、跟进进度。<br>你可以随时补充、暂停，或查看每一份成果。</p><div class="suggestions"><button class="suggestion" data-suggestion="帮我把这些资料整理成一份行动清单，标明优先级和待确认事项。"><span>▤</span>从一份资料开始<br><span class="muted">整理重点和下一步行动</span></button><button class="suggestion" data-suggestion="帮我规划接下来一周的个人工作，先问我需要了解的关键信息。"><span>☷</span>理清手头的事情<br><span class="muted">把想法变成可执行的计划</span></button></div></div>';
  }else{
    box.innerHTML=items.map(i=>i.html).join('')+questions.map(questionCard).join('');
    if(task&&['ready','running','waiting_children'].includes(task.status))box.insertAdjacentHTML('beforeend',`<article class="message system"><div class="bubble">${esc(labels[task.status])} <i class="spinner-dot"></i><i class="spinner-dot"></i><i class="spinner-dot"></i></div></article>`);
  }
  if(bottom||force)box.scrollTop=box.scrollHeight;else box.scrollTop=oldScroll;
  $('#task-focus').classList.toggle('hidden',!task);
  $('#task-focus').innerHTML=task?`<span>当前任务：${esc(task.spec.objective)}</span><button data-clear-task title="查看全部对话">×</button>`:'';
  $('#message-mode option[value="followup"]').disabled=!task;
}
async function detailView(force=false){
  const task=selected(), data=state.data, artifacts=data.deliveries.filter(d=>d.artifact);
  $('#artifact-count').textContent=artifacts.length||'';
  $$('.detail-tabs button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===state.tab)));
  const signature=JSON.stringify({tab:state.tab,task,artifacts,controls:data.controls});
  if(!force&&signature===state.detailSignature)return;
  state.detailSignature=signature;
  const box=$('#detail-content');
  if(state.tab==='library'){
    box.innerHTML='<div class="detail-heading">已交付的成果</div>'+ (artifacts.slice().reverse().map(artifactCard).join('')||'<div class="empty-detail">完成的清单、报告和资料<br>都会收在这里。</div>');return;
  }
  let html='<div class="profile"><div class="avatar">✳</div><h2>个人助理</h2><p>把资料变成行动，把进度留住。</p></div>';
  if(!task){box.innerHTML=html+'<div class="detail-heading">任务与进展</div><div class="detail-card muted">选择左侧任务，可以查看进展、后台协作和交付成果。<br><br>新任务开始后，你仍然可以在这里暂停，或补充新的要求。</div>';return;}
  const cancelled=task.cancel_state==='cancelled', done=task.status==='succeeded';
  const children=data.tasks.filter(t=>t.parent_task_id===task.id);
  const stopping=data.controls.some(c=>c.task_id===task.id&&c.state==='requested');
  html+=`<div class="detail-heading">当前任务</div><div class="detail-card">${stopping?'<span class="pill">正在停止…</span>':pill(task.status)}<h3>${esc(task.spec.objective)}</h3><div class="detail-row"><span>开始时间</span><strong>${esc(timeLabel(task.created))}</strong></div><div class="detail-row"><span>资料</span><strong>${task.spec.sources.length} 份</strong></div><div class="detail-row"><span>成果检查</span><strong>${task.acceptance==='passed'?'已通过':task.acceptance==='needs_review'?'需要复核':'尚未通过'}</strong></div><div class="task-buttons">`;
  if(!done&&!cancelled){
    html+='<button class="button" data-compose-followup>补充要求</button>';
    if(['paused','failed','review','stopped'].includes(task.status))html+=`<button class="button" data-action="resume" data-tid="${task.id}">继续任务</button>`;
    else html+=`<button class="button" data-action="stop_reply" data-tid="${task.id}">暂停回复</button>`;
    html+=`<button class="button danger" data-action="cancel" data-tid="${task.id}">取消委托</button>`;
  }else if(done)html+='<button class="button" data-compose-followup>基于成果追加任务</button>';
  html+='</div></div>';
  if(children.length)html+='<div class="detail-heading">后台协作</div>'+children.map(c=>`<div class="worker"><strong>${esc(c.spec.objective)}</strong>${pill(c.status)} ${!['succeeded','cancelled','stopped'].includes(c.status)?`<button class="button" data-action="stop_task" data-tid="${c.id}">停止这项</button>`:''}</div>`).join('');
  if(task.spec.sources.length)html+='<div class="detail-heading">本任务资料</div>'+task.spec.sources.map(s=>`<div class="detail-row"><span>${esc(s.description||s.id)}</span><strong>${s.required===false?'可选':'必读'}</strong></div>`).join('');
  html+=`<details class="disclosure"><summary>执行记录与预算</summary><div class="detail-row"><span>模型调用（含后台）</span><strong>${task.calls_used} / ${task.spec.max_calls}</strong></div><div class="detail-row"><span>工具操作（含后台）</span><strong>${task.tools_used} / ${task.spec.max_tools}</strong></div><div id="operation-list"></div></details>`;
  box.innerHTML=html;
  try{const detail=await api('/api/tasks/'+task.id);if(state.task!==task.id||state.tab!=='details')return;$('#operation-list').innerHTML=detail.operations.map(o=>`<div class="log-row"><span>${esc(o.tool)}</span><span>${esc(o.status)}</span></div>`).join('')||'<p class="muted">还没有工具操作记录</p>';}catch(e){toast(e.message);}
}
async function refresh(force=false){
  try{state.data=await api('/api/state?room='+encodeURIComponent(state.room));state.online=true;if(state.task&&!selected())state.task=null;roomView();messageView(force);await detailView(force);}
  catch(e){state.online=false;$('#connection').textContent='连接中断 · 草稿已保留';$('#banner').textContent='连接暂时中断。已输入的内容会保留，恢复后可以继续。';$('#banner').classList.remove('hidden');}
}
function saveDraft(){localStorage.setItem(state.draftKey,JSON.stringify({text:$('#message-input').value,uploads:state.uploads,urls:state.urls,mode:$('#message-mode').value,task:state.task,retry:state.retry}));}
function restoreDraft(){try{const saved=JSON.parse(localStorage.getItem(state.draftKey)||'{}');$('#message-input').value=saved.text||'';state.uploads=saved.uploads||[];state.urls=saved.urls||[];state.task=saved.task||null;state.retry=saved.retry||null;$('#message-mode').value=saved.mode||'task';}catch{}renderAttachments();}
function renderAttachments(){ $('#attachment-list').innerHTML=[...state.uploads.map((u,i)=>`<div class="attachment"><span>▤ ${esc(u.name)}</span><button type="button" data-remove-upload="${i}" aria-label="移除资料">×</button></div>`),...state.urls.map((u,i)=>`<div class="attachment"><span>↗ ${esc(u)}</span><button type="button" data-remove-url="${i}" aria-label="移除网址">×</button></div>`)].join('');}
function chooseTask(id){state.task=id;$('#message-mode').value=id?'followup':'task';state.signature='';state.detailSignature='';saveDraft();roomView();messageView(true);detailView(true);$('#app').classList.remove('show-nav');}
async function send(event){
  event.preventDefault();if(state.sending||state.composing)return;
  const text=$('#message-input').value.trim();if(!text)return;
  const mode=$('#message-mode').value;if(mode==='followup'&&!state.task){toast('先选择一项任务，再补充要求');return;}
  if(!state.data?.ready){openSettings();return;}
  if(mode!=='task'&&(state.uploads.length||state.urls.length)){toast('新增资料请使用“交办任务”，以建立明确的资料范围');return;}
  const payload={text,mode,room:state.room,task_id:state.task,uploads:state.uploads.map(u=>u.id),urls:state.urls};
  const fingerprint=JSON.stringify(payload);
  const nonce=state.retry?.fingerprint===fingerprint?state.retry.nonce:crypto.randomUUID();
  state.retry={fingerprint,nonce};saveDraft();state.sending=true;$('#send').disabled=true;
  try{const receipt=await api('/api/messages',{...payload,nonce});$('#message-input').value='';state.uploads=[];state.urls=[];state.retry=null;renderAttachments();if(receipt.task_id){state.task=receipt.task_id;$('#message-mode').value='followup';}saveDraft();await refresh(true);}
  catch(e){toast(e.message+'；内容已保留，可以重试');}
  finally{state.sending=false;$('#send').disabled=false;$('#message-input').focus();}
}
function modal(title, html){$('#modal-title').textContent=title;$('#modal-content').innerHTML=html;$('#modal').showModal();}
function openSettings(){const model=state.data?.model||{name:'qwen3.7-plus',base_url:'https://maas.qianwenaiapi.com/compatible-mode/v1'};modal('模型设置',`<p class="modal-note">使用你自己的 Qwen 官网账号。密钥保存在本机数据目录，不会显示在聊天或发送给其他网站。</p><form id="settings-form"><label class="field">模型型号<input name="name" value="${esc(model.name)}" required></label><label class="field">官网接口地址<input name="base_url" value="${esc(model.base_url)}" required><small>请使用与你的账号地域和服务匹配的官方接口。</small></label><label class="field">API Key<input name="key" type="password" autocomplete="new-password" placeholder="${state.data?.ready?'已配置；留空保持原密钥':'粘贴你的 Qwen 官网 API Key'}"><small>不会回显已保存的密钥。配置保存不代表连通测试通过。</small></label><div class="modal-actions"><button class="button primary" type="submit">保存配置</button></div></form>`);}
async function performAction(name,taskId,extra={}){try{const result=await api('/api/actions/'+name,{task_id:taskId,...extra});toast(result.state==='stopping'?'正在停止执行，已有进度会保留':'操作已记录');$('#modal').close();await refresh();}catch(e){if(name==='resume'&&e.message.includes('budget'))openBudget(taskId);else toast(e.message);}}
function openBudget(taskId){modal('增加任务预算',`<p class="modal-note">原进度和消耗会保留。只增加你选择的额度，模型调用可能产生费用。</p><form id="budget-form" data-tid="${taskId}"><label class="field">增加模型调用次数<input name="extra_calls" type="number" min="0" max="60" value="10"></label><label class="field">增加工具操作次数<input name="extra_tools" type="number" min="0" max="100" value="20"></label><label class="field">增加执行时间（秒）<input name="extra_seconds" type="number" min="0" max="2700" value="300"></label><div class="modal-actions"><button class="button primary">增加并继续</button></div></form>`);}
async function answer(qid,option){const q=state.data.questions.find(q=>q.id===qid);const text=option!==undefined?q.options[Number(option)]:state.answers[qid]?.trim();if(!text)return;try{const result=await api('/api/actions/answer',{question_id:qid,text,nonce:crypto.randomUUID()});if(result.state==='ignored_stale_answer')toast('这个问题已经失效，请查看当前任务');await refresh();}catch(e){toast(e.message);}}
$('#composer-form').addEventListener('submit',send);
$('#message-input').addEventListener('compositionstart',()=>state.composing=true);
$('#message-input').addEventListener('compositionend',()=>{state.composing=false;saveDraft();});
$('#message-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!state.composing&&e.keyCode!==229){e.preventDefault();$('#composer-form').requestSubmit();}});
$('#message-input').addEventListener('input',()=>{state.retry=null;saveDraft();});
$('#message-mode').addEventListener('change',saveDraft);
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
  if(b.dataset.room){chooseTask(null);}
  if(b.dataset.tab){state.tab=b.dataset.tab;await detailView(true);}
  if(b.dataset.suggestion){$('#message-input').value=b.dataset.suggestion;saveDraft();$('#message-input').focus();}
  if(b.hasAttribute('data-clear-task'))chooseTask(null);
  if(b.hasAttribute('data-compose-followup')){$('#message-mode').value='followup';saveDraft();$('#message-input').focus();}
  if(b.hasAttribute('data-open-settings'))openSettings();
  if(b.dataset.removeUpload!==undefined){state.uploads.splice(Number(b.dataset.removeUpload),1);renderAttachments();saveDraft();}
  if(b.dataset.removeUrl!==undefined){state.urls.splice(Number(b.dataset.removeUrl),1);renderAttachments();saveDraft();}
  if(b.dataset.answer)await answer(b.dataset.answer,b.dataset.option);
  if(b.dataset.action){if(b.dataset.action==='cancel')modal('取消这项委托？',`<p class="modal-note">这会停止当前任务及其后台协作。已有记录和产物会保留，普通消息不会重新启动这项已取消的任务。</p><div class="modal-actions"><button class="button danger" data-confirm-cancel="${b.dataset.tid}">取消委托</button></div>`);else await performAction(b.dataset.action,b.dataset.tid);}
  if(b.dataset.confirmCancel)await performAction('cancel',b.dataset.confirmCancel);
  if(b.dataset.artifact){try{const data=await api('/api/artifacts/'+b.dataset.artifact);modal(data.name,`<div class="artifact-preview rich">${rich(data.text)}</div><div class="modal-actions"><a class="button download" href="/api/artifacts/${b.dataset.artifact}?download=1" download="result.md">下载成果</a></div>`);}catch(error){toast(error.message);}}
});
document.addEventListener('submit',async e=>{
  if(e.target.id==='settings-form'){e.preventDefault();const form=new FormData(e.target),body={name:form.get('name'),base_url:form.get('base_url')};if(form.get('key'))body.key=form.get('key');try{await api('/api/settings',body);$('#modal').close();toast('配置已保存');await refresh();}catch(error){toast(error.message);}}
  if(e.target.id==='url-form'){e.preventDefault();state.urls.push(new FormData(e.target).get('url'));renderAttachments();saveDraft();$('#modal').close();}
  if(e.target.id==='budget-form'){e.preventDefault();const extra=Object.fromEntries([...new FormData(e.target)].map(([k,v])=>[k,Number(v)]));await performAction('resume',e.target.dataset.tid,extra);}
});
$$('.resizer').forEach(r=>r.addEventListener('pointerdown',e=>{r.setPointerCapture(e.pointerId);const move=e=>{const width=r.dataset.panel==='left'?e.clientX:innerWidth-e.clientX;document.documentElement.style.setProperty(r.dataset.panel==='left'?'--left':'--right',Math.max(220,Math.min(440,width))+'px');};r.addEventListener('pointermove',move);r.addEventListener('pointerup',()=>r.removeEventListener('pointermove',move),{once:true});}));
async function poll(){await refresh();setTimeout(poll,document.hidden?5000:1500);}
try{const boot=await api('/api/bootstrap');state.token=boot.token;restoreDraft();await refresh(true);setTimeout(poll,1500);}catch(e){toast('无法连接本地服务，请确认启动窗口仍在运行。');}
