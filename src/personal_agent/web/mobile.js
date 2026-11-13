// Phone extras: camera capture, "这里不对" on long press, seen receipts, Web Push, pairing.
// Everything goes through window.noyiApp.api, the same endpoints the desktop page uses.
const $ = (q, root = document) => root.querySelector(q);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const app = () => window.noyiApp;
const boot = () => window.noyiBoot || {};
const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;

// ---- camera: shrink to 1600px JPEG before upload (fits the 4 MB limit, fewer image tokens) ----
async function shrink(file) {
  if (!file.type.startsWith('image/')) return file;
  const bitmap = await createImageBitmap(file).catch(() => null);
  if (!bitmap) return file;
  const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size < 1_500_000) return file;
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.85));
  return blob ? new File([blob], (file.name || 'photo').replace(/\.\w+$/, '') + '.jpg', {type: 'image/jpeg'}) : file;
}
async function uploadPhoto(file) {
  const small = await shrink(file);
  const body = new FormData(); body.append('file', small);
  const uploaded = await app().api('/api/uploads?room=' + encodeURIComponent(app().room), body);
  app().addUpload(uploaded);
  const input = $('#message-input');
  if (!input.value.trim()) input.placeholder = '说一句这是什么、吃了多少，比如「午饭，吃了一半」';
  input.focus();
}
$('#camera')?.addEventListener('click', () => $('#camera-input').click());
$('#camera-input')?.addEventListener('change', async e => {
  for (const file of e.target.files) { try { await uploadPhoto(file); } catch (err) { app().toast(err.message); } }
  e.target.value = '';
});

// ---- 「这里不对」: long press on phones, right click on desktop ---------------------------
const CATEGORIES = [['wrong_fact', '事实错了'], ['wrong_math', '算错了'], ['misunderstood', '没听懂我的意思'], ['verbose', '太啰嗦'], ['missed', '漏了东西'], ['other', '其他']];
function openFeedback(article) {
  const id = article.dataset.delivery;
  app().modal('这里不对', `<form id="feedback-form" data-delivery="${esc(id)}">
    <div class="feedback-cats">${CATEGORIES.map(([v, l], i) => `<label><input type="radio" name="category" value="${v}" ${i ? '' : 'required'}> ${l}</label>`).join('')}</div>
    <label class="field">哪里不对（可选，<b>这句会随运行日志回传到 noyi-ops</b>，供改进用）<textarea name="shared" maxlength="300" rows="2" placeholder="例如：鸡蛋算成了两个"></textarea></label>
    <label class="field">只留在家里的备注（可选，不回传）<textarea name="private" maxlength="2000" rows="2"></textarea></label>
    <p class="modal-note" id="feedback-preview">将回传：问题分类。回复原文、照片和你的备注都不会回传。</p>
    <div class="modal-actions"><button class="button primary" type="submit">标记</button></div></form>`);
  const form = $('#feedback-form');
  const preview = () => {
    const shared = form.shared.value.trim(), cat = CATEGORIES.find(c => c[0] === form.category.value)?.[1] || '问题分类';
    $('#feedback-preview').textContent = '将回传：' + cat + (shared ? '，以及「' + shared + '」' : '') + '。回复原文、照片和只留在家里的备注都不会回传。';
  };
  form.addEventListener('input', preview);
}
document.addEventListener('submit', async e => {
  if (e.target.id !== 'feedback-form') return;
  e.preventDefault();
  const f = e.target, shared = f.shared.value.trim();
  try {
    await app().api('/api/feedback', {delivery_id: f.dataset.delivery, category: f.category.value, note_shared: shared, note_private: f.private.value.trim(), share: !!shared});
    $('#modal').close(); app().toast('已标记，谢谢');
  } catch (err) { app().toast(err.message); }
});
let pressTimer = null, pressStart = null;
document.addEventListener('pointerdown', e => {
  const article = e.target.closest?.('.message[data-delivery]');
  if (!article || e.pointerType === 'mouse') return;
  pressStart = [e.clientX, e.clientY];
  pressTimer = setTimeout(() => { pressTimer = null; navigator.vibrate?.(15); openFeedback(article); }, 550);
});
const cancelPress = e => {
  if (!pressTimer) return;
  if (e.type === 'pointermove' && pressStart && Math.hypot(e.clientX - pressStart[0], e.clientY - pressStart[1]) < 10) return;
  clearTimeout(pressTimer); pressTimer = null;
};
['pointerup', 'pointercancel', 'pointermove'].forEach(t => document.addEventListener(t, cancelPress));
document.addEventListener('contextmenu', e => {
  const article = e.target.closest?.('.message[data-delivery]');
  if (!article) return;
  e.preventDefault(); openFeedback(article);
});

// ---- seen: a message counts as delivered when this page has shown it ---------------------
const reported = new Set();
let seenTimer = null;
const observer = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
  for (const entry of entries) if (entry.isIntersecting && document.visibilityState === 'visible') queueSeen(entry.target.dataset.delivery);
}, {threshold: 0.5}) : null;
function queueSeen(id) {
  if (!id || reported.has(id)) return;
  reported.add(id);
  clearTimeout(seenTimer); seenTimer = setTimeout(flushSeen, 400);
}
async function flushSeen() {
  const ids = [...document.querySelectorAll('.message[data-delivery]')].map(a => a.dataset.delivery)
    .filter(id => reported.has(id) && document.querySelector(`.message[data-delivery="${CSS.escape(id)}"]`)?.dataset.deliveryState !== 'accepted');
  try { await app().api('/api/seen', {ids: ids.slice(-300)}); } catch { ids.forEach(id => reported.delete(id)); }
}
function missedBanner(data) {
  // Pushes can be late or dropped; whatever the phone never showed is listed here.
  const missed = (data?.deliveries || []).filter(d => d.state === 'pushed' && ['health', 'health_alert', 'commitment'].includes(d.kind));
  let bar = $('#missed');
  if (!missed.length) { bar?.remove(); return; }
  if (!bar) { bar = document.createElement('div'); bar.id = 'missed'; bar.className = 'missed'; $('#messages').before(bar); }
  bar.innerHTML = `<span>有 ${missed.length} 条提醒推送到手机后还没看到</span><button type="button">看一下</button>`;
  bar.querySelector('button').onclick = () => { document.querySelector(`.message[data-delivery="${CSS.escape(missed[0].id)}"]`)?.scrollIntoView({block: 'center'}); };
}
let presence = 0;
function afterRefresh(data) {
  if (observer) document.querySelectorAll('.message[data-delivery]').forEach(a => { if (!a.dataset.observed) { a.dataset.observed = '1'; observer.observe(a); } });
  missedBanner(data);
  // Tell the service the page is open now, so it shows messages here instead of pushing.
  if (document.visibilityState === 'visible' && Date.now() - presence > 20000) { presence = Date.now(); app().api('/api/seen', {ids: []}).catch(() => {}); }
  const open = new URLSearchParams(location.search).get('open');
  if (open) { const a = document.querySelector(`.message[data-delivery="${CSS.escape(open)}"]`); if (a) { a.scrollIntoView({block: 'center'}); queueSeen(open); history.replaceState(null, '', '/'); } }
  pushOffer();
}
navigator.serviceWorker?.addEventListener('message', e => { if (e.data?.type === 'open-delivery') history.replaceState(null, '', '/?open=' + encodeURIComponent(e.data.delivery || '')); });

// ---- Web Push (iOS 16.4+, only after 添加到主屏幕) ----------------------------------------
function pushOffer() {
  const b = boot();
  if (!b.remote || !b.push_public_key || !('serviceWorker' in navigator) || !('PushManager' in window)) return;
  if (!standalone()) return addToHomeHint();
  if (Notification.permission !== 'default' || $('#push-offer') || localStorage.getItem('noyi.push.dismissed')) return;
  const bar = document.createElement('div'); bar.id = 'push-offer'; bar.className = 'missed';
  bar.innerHTML = '<span>开启通知，每晚汇总和提醒会推到这台 iPhone</span><button type="button" data-yes>开启</button><button type="button" data-no class="small-link">以后再说</button>';
  $('#messages').before(bar);
  bar.querySelector('[data-no]').onclick = () => { localStorage.setItem('noyi.push.dismissed', '1'); bar.remove(); };
  bar.querySelector('[data-yes]').onclick = async () => {   // permission must come from a tap
    try {
      if (await Notification.requestPermission() !== 'granted') throw new Error('没有开启通知，可以以后在 iPhone 设置里打开');
      const reg = await navigator.serviceWorker.ready;
      const key = Uint8Array.from(atob(b.push_public_key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - b.push_public_key.length % 4) % 4)), c => c.charCodeAt(0));
      const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
      await app().api('/api/push/subscribe', sub.toJSON());
      bar.remove(); app().toast('通知已开启');
    } catch (err) { app().toast(err.message); }
  };
}
function addToHomeHint() {
  if ($('#home-hint') || localStorage.getItem('noyi.home.dismissed') || !/iPhone|iPad/.test(navigator.userAgent)) return;
  const bar = document.createElement('div'); bar.id = 'home-hint'; bar.className = 'missed';
  bar.innerHTML = '<span>想收到通知：点 Safari 底部的「分享」→「添加到主屏幕」，再从主屏幕打开</span><button type="button" class="small-link">知道了</button>';
  $('#messages').before(bar);
  bar.querySelector('button').onclick = () => { localStorage.setItem('noyi.home.dismissed', '1'); bar.remove(); };
}
if ('serviceWorker' in navigator && boot().remote !== undefined) navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {});
else if ('serviceWorker' in navigator) addEventListener('load', () => navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {}));

// ---- 添加手机 (desktop, this machine only) --------------------------------------------------
async function openPairing() {
  try {
    const list = await app().api('/api/devices');
    const devices = list.devices.filter(d => !d.revoked);
    const rows = devices.map(d => `<div class="device-row"><span>${esc(d.name)}</span><small>${d.last_seen ? '最近用过 ' + new Date(d.last_seen * 1000).toLocaleString('zh-CN', {hour12: false}) : ''}</small><button class="small-link" data-revoke="${esc(d.id)}">移除</button></div>`).join('') || '<p class="muted">还没有配对的设备。</p>';
    const ready = list.hosts.length;
    app().modal('添加手机', `${ready ? '' : '<p class="modal-note">还没配置外网地址：先在这台电脑上按说明运行 <code>tailscale serve</code>，并设置 NOYI_REMOTE_HOSTS 后重启 Noyi。</p>'}
      <form id="pairing-form"><label class="field">给手机起个名字<input name="name" value="我的 iPhone" maxlength="40" required></label>
      <div class="modal-actions"><button class="button primary" type="submit" ${ready ? '' : 'disabled'}>生成配对码</button></div></form>
      <div id="pairing-code"></div><h3>已配对的设备</h3>${rows}`);
  } catch (err) { app().toast(err.message); }
}
document.addEventListener('submit', async e => {
  if (e.target.id !== 'pairing-form') return;
  e.preventDefault();
  try {
    const code = await app().api('/api/pairing', {name: e.target.name.value});
    $('#pairing-code').innerHTML = `<div class="pair-code"><p>手机连上 Tailscale 后，打开</p><code>${esc(code.url.split('/?')[0])}</code><p>输入配对码（5 分钟内有效）</p><strong>${esc(code.code)}</strong><p class="muted">也可以直接在手机上打开这个链接：<br><code>${esc(code.url)}</code></p></div>`;
  } catch (err) { app().toast(err.message); }
});
document.addEventListener('click', async e => {
  const revoke = e.target.closest?.('[data-revoke]');
  if (revoke) { try { await app().api('/api/devices/revoke', {id: revoke.dataset.revoke}); app().toast('已移除'); openPairing(); } catch (err) { app().toast(err.message); } }
  if (e.target.closest?.('[data-open-pairing]')) openPairing();
});
// The settings dialog gets an "添加手机" entry when opened on this machine.
new MutationObserver(() => {
  const form = $('#settings-form');
  if (form && !boot().remote && !$('#open-pairing')) form.insertAdjacentHTML('afterend', '<hr><h3>手机</h3><p class="modal-note">在外面用手机访问家里的 Noyi（经 Tailscale，只有配对过的设备能打开）。</p><button id="open-pairing" class="button" data-open-pairing>添加手机</button>');
}).observe(document.body, {childList: true, subtree: true});

window.noyiMobile = {afterRefresh};
