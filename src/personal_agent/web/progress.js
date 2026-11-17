// Live progress under "正在处理": the step list the model wrote (display only)
// and the last real tool call from the ledger, with how long ago it happened.
// Times are rendered with data-ago so tick() can refresh them every second
// without asking the server for anything.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const VERBS = {web_search:'搜索', fetch_page:'读取网页', read_source:'读取资料', write_artifact:'写成果', read_artifact:'查看已写的成果',
  submit_result:'提交核对', spawn_worker:'安排协作者', wait_workers:'等协作者', read_worker_result:'查看协作者结果', adopt_worker_result:'核对协作者结果',
  request_rework:'让协作者返工', read_dependency:'读取前置成果', ask_user:'向你提问', read_memory:'查看记忆', propose_memory:'提议记一条记忆',
  start_following:'安排跟进', manage_following:'调整跟进', review_following:'复盘跟进', complete_action:'确认安排', report_unable:'说明做不到',
  deliver_answer:'发出答复', resolve_followup:'处理补充要求', handback_followup:'交回补充要求', yield_task:'接收下一批补充'};

export function ago(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
}
const agoTag = (at, now, suffix) => `<time data-ago="${at}" data-suffix="${esc(suffix)}">${ago(now - at)}${esc(suffix)}</time>`;

function current(plan) {
  const index = plan.findIndex(s => s.state === 'doing');
  return index >= 0 ? index : plan.findIndex(s => s.state !== 'done');
}

export function actionLine(action, now) {
  if (!action) return '';
  const verb = VERBS[action.tool] || '调用工具';
  const what = action.target ? `${verb}「${esc(String(action.target).slice(0, 60))}」` : verb;
  if (action.status === 'running') return `正在${what} · 已 ${agoTag(action.at, now, '')}`;
  const end = action.ended || action.at;
  return `${what}${action.status === 'succeeded' ? '' : '（没成功）'} · ${agoTag(end, now, '前')}`;
}

// The one line shown while collapsed.
export function progressSummary(p, now) {
  const plan = p?.plan || [];
  const i = current(plan);
  const step = plan.length ? (i >= 0 ? `第 ${i + 1}/${plan.length} 步：${esc(plan[i].text)}` : `${plan.length} 步都已做完，正在收尾`) : '';
  return step || (p?.action ? actionLine(p.action, now) : '');
}

// The expanded body: the whole list, then what actually happened last.
export function progressPanel(p, now) {
  const plan = p?.plan || [];
  const marks = {done: '✓', doing: '▸', pending: '○'};
  const list = plan.length ? `<ol class="progress-steps">${plan.map(s => `<li class="${esc(s.state)}"><span>${marks[s.state] || '○'}</span>${esc(s.text)}</li>`).join('')}</ol>` : '<p class="progress-empty">还没有列步骤</p>';
  const last = p?.action ? `<div class="progress-action">最近一次实际动作：${actionLine(p.action, now)}</div>` : '<div class="progress-action">还没有实际动作</div>';
  const thinking = p?.thinking_since ? `<div class="progress-action">正在想下一步，已等 ${agoTag(p.thinking_since, now, '')}</div>` : '';
  return list + last + thinking;
}

export function tick(root, now) {
  for (const el of root.querySelectorAll('time[data-ago]')) el.textContent = ago(now - Number(el.dataset.ago)) + (el.dataset.suffix || '');
}
