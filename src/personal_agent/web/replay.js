// Ledger replay tab. Three layers that do not know about each other's insides:
//   data   – GET /api/tasks/{id}/timeline (read-only ledger rows, unknown times kept unknown)
//   player – a position measured in beats; one ledger event per beat, real time only labelled
//   skin   – every pixel goes through the skin object; a pixel skin implements the same methods
const TONES = {ok: '#2f9e6b', warn: '#d39518', bad: '#d64545', info: '#5b7fd6'};
const BEAT_MS = 1400;            // one event per beat at 1×
const TRAVEL = 0.42;             // share of a beat the token spends moving
const OUTCOMES = {fulfilled: ['已兑现', 'ok'], overdue: ['已逾期，已如实提醒', 'bad'], unable: ['做不到，已如实告知', 'warn'], released: ['不再继续', 'warn'],
  superseded: ['已改约', 'warn'], blocked: ['卡住了', 'warn'], open: ['还在做', 'info'], none: ['没有登记承诺', 'info']};

// ---- skin: geometric (phase 1). Pixel skin = another object with these methods. ----
export const geometricSkin = {
  name: 'geometry',
  layout(width, height, count, vertical) {
    const pad = vertical ? {x: 70, y: 44} : {x: 70, y: 0};
    return Array.from({length: count}, (_, i) => vertical
      ? {x: pad.x, y: pad.y + i * (height - pad.y * 2) / (count - 1)}
      : {x: pad.x + i * (width - pad.x * 2) / (count - 1), y: height * 0.5});
  },
  background(ctx, view) {ctx.fillStyle = '#fbfbf9'; ctx.fillRect(0, 0, view.width, view.height);},
  track(ctx, view, a, b, done) {
    ctx.strokeStyle = done ? '#9fd0b7' : '#e3e3de'; ctx.lineWidth = done ? 4 : 3; ctx.setLineDash(done ? [] : [6, 6]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); ctx.setLineDash([]);
  },
  station(ctx, view, s) {
    const r = view.vertical ? 15 : 20, colour = s.tone ? TONES[s.tone] : '#cfcfca';
    ctx.fillStyle = s.visited ? colour : '#ffffff'; ctx.strokeStyle = colour; ctx.lineWidth = s.active ? 4 : 2;
    ctx.beginPath(); ctx.roundRect(s.pos.x - r, s.pos.y - r, r * 2, r * 2, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = s.visited ? '#fff' : '#9a9a95'; ctx.font = `600 ${view.vertical ? 12 : 14}px "Microsoft YaHei",sans-serif`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(String(s.index + 1), s.pos.x, s.pos.y + 1);
    ctx.fillStyle = s.visited ? '#232320' : '#9a9a95'; ctx.font = `${s.active ? 600 : 400} ${view.vertical ? 13 : 14}px "Microsoft YaHei",sans-serif`;
    const label = s.label + (s.count > 1 ? ` ×${s.count}` : '');
    if (view.vertical) {ctx.textAlign = 'left'; ctx.fillText(label, s.pos.x + r + 12, s.pos.y);}
    else {ctx.textAlign = 'center'; ctx.fillText(label, s.pos.x, s.pos.y + r + 20);}
    // A red lamp stays once any event at this station went wrong (e.g. a refused promise).
    if (s.bad && s.visited) this.lamp(ctx, view, s.pos, r);
  },
  lamp(ctx, view, pos, r) {
    const x = view.vertical ? pos.x - r - 16 : pos.x + r + 4, y = view.vertical ? pos.y : pos.y - r - 4;
    ctx.fillStyle = TONES.bad; ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
  },
  token(ctx, view, pos, tone) {
    ctx.fillStyle = TONES[tone] || TONES.info; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(pos.x, pos.y, view.vertical ? 7 : 9, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  },
  burst(ctx, view, pos, tone, progress) {
    ctx.strokeStyle = TONES[tone] || TONES.info; ctx.globalAlpha = 1 - progress; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(pos.x, pos.y, (view.vertical ? 18 : 24) + progress * 22, 0, Math.PI * 2); ctx.stroke(); ctx.globalAlpha = 1;
  },
  caption(ctx, view, text, tone) {
    ctx.fillStyle = TONES[tone] || '#55554f'; ctx.font = '600 13px "Microsoft YaHei",sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    let shown = text;
    while (shown.length > 1 && ctx.measureText(shown).width > view.width - 28) shown = shown.slice(0, -2) + '…';
    ctx.fillText(shown, 14, 12);
  },
};
geometricSkin.label = '简洁';
const SKINS = {geometry: geometricSkin};
const skinListeners = new Set();
export function registerSkin(skin) {SKINS[skin.name] = skin; skinListeners.forEach(f => f());}
const SKIN_KEY = 'pa.replay.skin';

// ---- player: pure state over the event list ----
function createPlayer(onChange) {
  const p = {events: [], position: 0, playing: false, speed: 1, last: 0};
  p.load = events => {p.events = events; p.position = 0; p.playing = false; onChange();};
  p.length = () => p.events.length;
  p.seek = value => {p.position = Math.max(0, Math.min(p.length(), value)); onChange();};
  p.play = () => {if (!p.length()) return; if (p.position >= p.length()) p.position = 0; p.playing = true; p.last = performance.now(); onChange();};
  p.pause = () => {p.playing = false; onChange();};
  p.tick = now => {
    if (!p.playing) return;
    p.position += (now - p.last) / BEAT_MS * p.speed; p.last = now;
    if (p.position >= p.length()) {p.position = p.length(); p.playing = false;}
    onChange();
  };
  // Event shown now and how far into its beat we are.
  p.current = () => {
    if (!p.length()) return {index: -1, fraction: 0};
    const index = Math.min(p.length() - 1, Math.floor(p.position));
    return {index, fraction: p.position >= p.length() ? 1 : p.position - index};
  };
  return p;
}

export function installReplay({state, api, esc, timeLabel}) {
  const root = document.createElement('div');
  root.className = 'replay';
  root.innerHTML = `<div class="replay-bar"><select class="replay-task" aria-label="选一件事"></select><select class="replay-skin" aria-label="画风"></select><button class="small-link" data-replay-reload>刷新</button><button class="small-link" data-replay-full>全屏回放</button></div>
    <div class="replay-outcome"></div><canvas class="replay-canvas" aria-label="账本回放画面"></canvas>
    <div class="replay-controls"><button class="button replay-play" data-replay-play aria-label="播放">▶ 播放</button><button class="icon" data-replay-step="-1" aria-label="上一步">‹</button><input class="replay-seek" type="range" min="0" max="0" step="0.01" value="0" aria-label="回放进度"><button class="icon" data-replay-step="1" aria-label="下一步">›</button><select class="replay-speed" aria-label="速度"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option><option value="4">4×</option></select></div>
    <div class="replay-step-label muted"></div><article class="replay-card"></article><details class="replay-log"><summary>全部经过</summary><ol></ol></details>`;
  const $ = q => root.querySelector(q);
  const canvas = $('.replay-canvas'), ctx = canvas.getContext('2d');
  let timeline = null, taskId = null, followed = null, skin = geometricSkin, frame = 0, shownCard = -2;
  const player = createPlayer(() => {draw(); controls();});

  function skinOptions() {
    const select = $('.replay-skin');
    select.innerHTML = Object.values(SKINS).map(s => `<option value="${esc(s.name)}">${esc(s.label || s.name)}</option>`).join('');
    select.value = skin.name;
  }
  function setSkin(name) {
    skin = SKINS[name] || geometricSkin; root.dataset.skin = skin.name;
    try {localStorage.setItem(SKIN_KEY, skin.name);} catch {}
    skinOptions(); draw(); idle();
  }
  // Animated skins keep moving (conveyor, blinking lamps) while paused, at a low frame rate.
  let idleFrame = 0, idleLast = 0;
  function idle() {
    if (idleFrame || !skin.animated) return;
    const step = now => {
      idleFrame = 0;
      if (!root.isConnected || !skin.animated) return;
      if (!player.playing && now - idleLast > 66) {idleLast = now; draw();}
      idleFrame = requestAnimationFrame(step);
    };
    idleFrame = requestAnimationFrame(step);
  }
  function when(e) {return e.at ? timeLabel(e.at) : '时间未知';}
  function card(e) {
    if (!e) return '<p class="muted">按 ▶ 开始，或拖动进度条。</p>';
    const facts = e.facts.map(([k, v]) => `<div class="detail-row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
    const links = e.links.map(l => `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}</a>`).join(' ');
    const station = timeline.stations.find(s => s.id === e.station)?.label || '';
    const end = e.end_time === 'unknown' ? ' · 结束时间未知' : e.end_time === 'running' ? ' · 还没结束' : '';
    return `<div class="replay-card-head"><span class="replay-tone ${esc(e.tone)}"></span><strong>${esc(e.title)}</strong><span class="muted">${esc(station)}</span></div>
      <time class="muted">${esc(when(e))}${end}${e.order === 'inferred' ? '（按账本顺序排列）' : ''}</time>${e.text ? `<p>${esc(e.text.slice(0, 600))}</p>` : ''}${facts}${links ? `<div class="replay-links">${links}</div>` : ''}`;
  }
  function controls() {
    const n = player.length(), index = player.position > 0 ? player.current().index : -1;
    root.dataset.playing = String(player.playing); root.dataset.position = player.position.toFixed(2);
    $('.replay-play').textContent = player.playing ? '❚❚ 暂停' : player.position >= n && n ? '↺ 重播' : '▶ 播放';
    $('.replay-play').setAttribute('aria-label', player.playing ? '暂停' : '播放');
    const seek = $('.replay-seek'); seek.max = String(n); if (document.activeElement !== seek) seek.value = String(player.position);
    $('.replay-step-label').textContent = n ? `第 ${index + 1} / ${n} 步${timeline.unknown_times ? ` · ${timeline.unknown_times} 处时间账本里没有记录，标为未知` : ''}` : '';
    if (shownCard !== index) {
      shownCard = index; $('.replay-card').innerHTML = card(timeline?.events[index]);
      $$log().forEach((li, i) => li.classList.toggle('current', i === index));
    }
  }
  const $$log = () => [...root.querySelectorAll('.replay-log li')];

  function resize() {
    const box = canvas.getBoundingClientRect(), ratio = devicePixelRatio || 1;
    const width = Math.max(240, box.width), vertical = width < 560;
    const height = vertical ? 470 : Math.max(240, Math.min(innerHeight * 0.42, 360));
    canvas.style.height = height + 'px';
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);}
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return {width, height, vertical};
  }
  function draw() {
    if (!root.isConnected) return;
    const view = resize();
    view.now = performance.now();
    skin.background(ctx, view);
    if (!timeline) {skin.caption(ctx, view, '选一件事开始回放', 'info'); return;}
    const stations = timeline.stations, index_of = id => stations.findIndex(s => s.id === id);
    const pos = skin.layout(view.width, view.height, stations.length, view.vertical);
    const {index, fraction} = player.current(), arrived = fraction >= TRAVEL || player.position >= player.length();
    const seen = stations.map(() => ({count: 0, tone: null, bad: false, kinds: []}));
    // Skins may read what has happened so far (events already arrived) and the event in hand.
    view.history = [];
    timeline.events.forEach((e, i) => {
      if (i < index || (i === index && arrived && player.position > 0)) {const s = seen[index_of(e.station)]; s.count++; s.tone = e.tone; s.bad = s.bad || e.tone === 'bad'; s.kinds.push(e.kind); view.history.push(e);}
    });
    const reached = Math.max(-1, ...seen.map((s, i) => s.count ? i : -1));
    for (let i = 0; i < stations.length - 1; i++) skin.track(ctx, view, pos[i], pos[i + 1], i < reached);
    const current = timeline.events[index];
    view.event = player.position > 0 ? current : null;
    view.progress = arrived ? Math.min(1, (fraction - TRAVEL) / (1 - TRAVEL)) : 0;
    stations.forEach((s, i) => skin.station(ctx, view, {index: i, id: s.id, label: s.label, pos: pos[i], visited: seen[i].count > 0, count: seen[i].count, tone: seen[i].tone, bad: seen[i].bad, kinds: seen[i].kinds,
      active: current && index_of(current.station) === i && arrived && player.position > 0}));
    if (current && player.position > 0) {
      const to = pos[index_of(current.station)], prev = index > 0 ? pos[index_of(timeline.events[index - 1].station)] : to;
      const t = Math.min(1, fraction / TRAVEL), ease = t * t * (3 - 2 * t);
      skin.token(ctx, view, {x: prev.x + (to.x - prev.x) * ease, y: prev.y + (to.y - prev.y) * ease}, current.tone);
      if (arrived) skin.burst(ctx, view, to, current.tone, view.progress);
      skin.caption(ctx, view, `${current.title} · ${when(current)}`, current.tone);
    } else skin.caption(ctx, view, timeline.task.title, 'info');
  }
  // Animation frames drive playback; a slow timer keeps it moving where the
  // browser withholds frames (a background tab), so pause/seek stay truthful.
  let timer = 0;
  function loop() {
    frame = 0;
    if (!root.isConnected) {player.pause(); return;}
    player.tick(performance.now()); draw();
    if (player.playing) frame = requestAnimationFrame(loop);
  }
  function start() {
    player.play();
    if (!frame) frame = requestAnimationFrame(loop);
    if (!timer) timer = setInterval(() => {
      if (!player.playing) {clearInterval(timer); timer = 0; return;}
      if (performance.now() - player.last > 120) {player.tick(performance.now()); draw();}
    }, 100);
  }

  async function load(id) {
    taskId = id; timeline = null; shownCard = -2;
    $('.replay-outcome').className = 'replay-outcome info'; $('.replay-outcome').textContent = '正在读账本…';
    try {
      const data = await api('/api/tasks/' + encodeURIComponent(id) + '/timeline');
      if (taskId !== id) return;
      timeline = data;
      const [text, t] = OUTCOMES[data.outcome] || [data.outcome, 'info'];
      $('.replay-outcome').className = `replay-outcome ${t}`;
      $('.replay-outcome').textContent = `结果：${text}`;
      $('.replay-log ol').innerHTML = data.events.map((e, i) => `<li><button data-replay-seek="${i}"><span class="replay-tone ${esc(e.tone)}"></span>${esc(e.title)}<small>${esc(when(e))}</small></button></li>`).join('');
      player.load(data.events);
    } catch (error) {$('.replay-outcome').className = 'replay-outcome bad'; $('.replay-outcome').textContent = error.message; player.load([]);}
  }
  function options() {
    const tasks = (state.data?.tasks || []).filter(t => !t.parent_task_id);
    const select = $('.replay-task');
    const html = tasks.map(t => `<option value="${esc(t.id)}">${esc(t.spec.objective.split(/[。\n]/)[0].slice(0, 40))}</option>`).join('');
    if (select.dataset.html !== html) {select.innerHTML = html || '<option value="">还没有交代过事情</option>'; select.dataset.html = html;}
    if (taskId) select.value = taskId;
    return tasks;
  }
  function mount(box) {
    const tasks = options();
    if (root.classList.contains('full')) box.innerHTML = '<div class="empty-detail">正在全屏回放，按 Esc 退出。</div>';
    else if (root.parentElement !== box) {box.innerHTML = ''; box.append(root);}
    // Follow the task chosen in the sidebar; otherwise keep what is loaded.
    const wanted = state.task && state.task !== followed ? state.task : taskId || tasks[0]?.id;
    followed = state.task;
    if (wanted && wanted !== taskId) load(wanted); else draw();
    idle();
  }
  function toggleFull(on) {
    root.classList.toggle('full', on);
    if (on) document.body.append(root); else {root.remove(); state.detailSignature = ''; document.dispatchEvent(new CustomEvent('replay:closed'));}
    $('[data-replay-full]').textContent = on ? '退出全屏' : '全屏回放';
    draw(); idle();
  }

  root.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.hasAttribute('data-replay-play')) player.playing ? player.pause() : start();
    if (b.dataset.replayStep) {
      // A step lands the token on the next (or previous) event's station.
      const {index} = player.current(), at = player.position > 0 ? index : -1;
      player.pause(); player.seek(Math.max(0, Math.min(player.length() - 1, at + Number(b.dataset.replayStep))) + TRAVEL);
    }
    if (b.dataset.replaySeek !== undefined) {player.pause(); player.seek(Number(b.dataset.replaySeek) + TRAVEL);}
    if (b.hasAttribute('data-replay-reload') && taskId) load(taskId);
    if (b.hasAttribute('data-replay-full')) toggleFull(!root.classList.contains('full'));
  });
  $('.replay-seek').addEventListener('input', e => {const value = Number(e.target.value); player.pause(); player.seek(value);});
  $('.replay-speed').addEventListener('change', e => {player.speed = Number(e.target.value);});
  $('.replay-skin').addEventListener('change', e => setSkin(e.target.value));
  skinListeners.add(() => {skinOptions(); let saved = null; try {saved = localStorage.getItem(SKIN_KEY);} catch {} if (saved && saved !== skin.name && SKINS[saved]) setSkin(saved);});
  let saved = null; try {saved = localStorage.getItem(SKIN_KEY);} catch {}
  setSkin(saved && SKINS[saved] ? saved : skin.name);
  $('.replay-task').addEventListener('change', e => {if (e.target.value) {player.pause(); load(e.target.value);}});
  document.addEventListener('keydown', e => {if (e.key === 'Escape' && root.classList.contains('full')) toggleFull(false);});
  new ResizeObserver(() => draw()).observe(canvas);
  return {mount, setSkin, registerSkin, player};
}
