export function installReviews({state,api,modal,toast,refresh,esc,timeLabel,onTask}){
  const $=q=>document.querySelector(q);
  const decisions={keep:'建议保留',stop:'建议停止',modify:'建议调整',insufficient:'证据还不够'};
  async function act(name,body,nonce=crypto.randomUUID()){const result=await api('/api/goals/'+name,{...body,nonce});await refresh(true);return result;}
  function card(g){
    const latest=g.reviews?.[0];
    if(!latest)return '';
    return `<div class="review-summary"><strong>最近一次回顾</strong><span class="pill">${decisions[latest.decision]||esc(latest.decision)}</span><p>助理的建议：${esc(latest.rationale)}</p><details><summary>依据与尚不确定的事</summary>${(latest.verified_facts||[]).map(x=>`<p>已记录：${esc(x)}</p>`).join('')}${latest.hypotheses.map(x=>`<p>假设：${esc(x)}</p>`).join('')}${latest.uncertainties.map(x=>`<p>尚不确定：${esc(x)}</p>`).join('')}<p>下次检验：${esc(latest.next_validation)}<br>建议时间：${esc(timeLabel(latest.next_check))}</p><p class="goal-compact-note">建议与已确认事实分开展示，原始证据仍可查看。</p></details><div class="task-buttons"><button class="button" data-review-report="${latest.id}">查看回顾报告</button></div></div>`;
  }
  function feedbackForm(taskId){modal('这份成果怎么样',`<form id="feedback-form" data-task="${taskId}" data-nonce="${crypto.randomUUID()}"><p class="modal-note">说说哪里有用，或哪里还需要调整。</p><label class="field">使用感受<select name="rating"><option value="useful">对我有用</option><option value="needs_fix">需要补充或调整</option><option value="incorrect">有事实错误</option></select></label><label class="field">具体说明<textarea name="note" rows="3" maxlength="3000" required></textarea></label><details class="disclosure"><summary>补充使用记录（可选）</summary><div class="goal-form-grid"><label class="field">这次你做了什么<select name="management_kind"><option value="feedback">检查与反馈</option><option value="reminder">催办</option><option value="addition">补充遗漏</option><option value="correction">纠错</option><option value="takeover">接管完成</option></select></label><label class="field">大约花了多少秒<input name="seconds" type="number" min="0" max="86400" placeholder="未填写"></label></div></details><div class="modal-actions"><button class="button primary">保存反馈</button></div></form>`);}
  document.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b||b.disabled)return;
    try{
      if(b.dataset.feedbackTask)feedbackForm(b.dataset.feedbackTask);
      if(b.dataset.reviewTask){$('#modal').close();onTask(b.dataset.reviewTask);}
      if(b.dataset.reviewReport){const report=await api('/api/reviews/'+b.dataset.reviewReport);modal('回顾报告',`<div class="artifact-preview">${report.text.split('\n').map(line=>line.startsWith('## ')?`<h3>${esc(line.slice(3))}</h3>`:line.startsWith('# ')?`<h2>${esc(line.slice(2))}</h2>`:`<p>${esc(line)}</p>`).join('')}</div><div class="modal-actions"><button class="button" data-review-task="${report.task_id}">查看原始记录</button><a class="download" href="/api/reviews/${report.id}?download=1" download>下载回顾报告</a></div>`);}
    }catch(error){toast(error.message);}finally{b.disabled=false;}
  });
  document.addEventListener('submit',async event=>{
    const form=event.target;if(form.id!=='feedback-form')return;event.preventDefault();const button=form.querySelector('button.primary');if(button.disabled)return;button.disabled=true;const f=new FormData(form);
    try{await act('feedback',{task_id:form.dataset.task,rating:f.get('rating'),note:f.get('note'),management_kind:f.get('management_kind'),management_seconds:f.get('seconds')===''?null:Number(f.get('seconds'))},form.dataset.nonce);toast('反馈已保存');$('#modal').close();}catch(error){toast(error.message);}finally{button.disabled=false;}
  });
  return {card};
}
