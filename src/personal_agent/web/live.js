// Live view of a task's browser tab during a login handoff: the user signs in
// themselves. Frames come over a WebSocket opened with a one-time ticket;
// taps and typed text go straight to the tab, never to the assistant.
let view = null;

async function ticket(questionId) {
  const boot = await (await fetch('/api/bootstrap')).json();
  const response = await fetch('/api/browser/live-ticket', {method: 'POST', headers: {'X-PA-Token': boot.token, 'Content-Type': 'application/json'},
    body: JSON.stringify({question_id: questionId})});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || '实时画面现在打不开');
  return (await response.json()).ticket;
}

function close() {
  if (!view) return;
  view.ws.close();
  URL.revokeObjectURL(view.url);
  view.root.remove();
  view = null;
}

async function open(questionId) {
  close();
  const root = document.createElement('div');
  root.className = 'live-view';
  root.innerHTML = `<header><span>实时画面：在这里亲手登录，Noyi 看不到你输入的内容</span><button class="button" data-live-close>关闭</button></header>
    <img alt="网页画面" draggable="false"><form class="live-input"><input placeholder="点上面的输入框后，在这里输入" autocomplete="off" autocapitalize="off" spellcheck="false">
    <button class="button" type="submit">发送</button><button class="button" type="button" data-key="Backspace">删除</button><button class="button" type="button" data-key="Enter">回车</button>
    <button class="button" type="button" data-key="Tab">下一项</button></form><p class="muted live-status">正在连接…</p>`;
  document.body.append(root);
  const img = root.querySelector('img'), status = root.querySelector('.live-status'), input = root.querySelector('input');
  let id;
  try { id = await ticket(questionId); } catch (e) { status.textContent = e.message; return; }
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/browser/live?ticket=${encodeURIComponent(id)}`);
  ws.binaryType = 'blob';
  view = {root, ws, url: ''};
  const send = event => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(event));
  ws.onopen = () => { status.textContent = '已连接。登好后关掉这里，点「我登好了」。'; };
  ws.onclose = () => { status.textContent = '实时画面已关闭（登录交接结束或超时）。'; };
  ws.onmessage = message => {
    if (!view) return;
    const previous = view.url;
    view.url = URL.createObjectURL(message.data);
    img.src = view.url;
    if (previous) URL.revokeObjectURL(previous);
  };
  img.addEventListener('click', e => {
    const box = img.getBoundingClientRect();
    send({type: 'click', x: (e.clientX - box.left) / box.width, y: (e.clientY - box.top) / box.height});
    input.focus();
  });
  img.addEventListener('wheel', e => { e.preventDefault(); send({type: 'scroll', dy: e.deltaY}); }, {passive: false});
  root.querySelector('form').addEventListener('submit', e => { e.preventDefault(); if (input.value) send({type: 'text', text: input.value}); input.value = ''; });
  root.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.key) send({type: 'key', key: b.dataset.key});
    if (b.hasAttribute('data-live-close')) close();
  });
}

document.addEventListener('click', e => {
  const b = e.target.closest('button[data-live]');
  if (b) open(b.dataset.live);
});
