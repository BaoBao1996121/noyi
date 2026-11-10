// Memory galaxy: a read-only Canvas 2D map of the personal memory.
//   data   – GET /api/memory/galaxy (stars = memories + pending proposals, links = facts in the rows)
//   layout – deterministic: five clusters, golden-angle spiral inside each, wrecks beside their successor
//   view   – camera (pan/zoom), one render loop at ~30 fps while visible, O(n) hit test
// Nothing here writes; clicking a star only shows what the ledger already holds.
const KIND = {
  rule: {label: '规则', colour: '#ffd166', shape: 'cross'},
  preference: {label: '偏好', colour: '#ff8fab', shape: 'circle'},
  about_me: {label: '关于我', colour: '#7cc7ff', shape: 'diamond'},
  ongoing: {label: '进行中', colour: '#8ef0a5', shape: 'ring'},
  reference: {label: '常用信息', colour: '#c6a8ff', shape: 'square'},
};
const ORDER = Object.keys(KIND);
const ACTIONS = {create: '新建', correct: '修改', delete: '删除', restore: '明确恢复', renew: '续期'};
const SOON = 7 * 86400;          // an ongoing memory this close to expiry blinks
const CLUSTER_R = 430, SPIRAL = 30, GOLDEN = Math.PI * (3 - Math.sqrt(5));

function hash(text) {let h = 2166136261; for (const c of String(text)) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return (h >>> 0) / 4294967296;}
const rank = s => s.state === 'active' && !s.expired ? 0 : s.state === 'active' ? 1 : s.state === 'pending' ? 2 : 3;

export function layoutGalaxy(data) {
  const stars = data.stars.map(s => ({...s}));
  const byId = new Map(stars.map(s => [s.id, s]));
  const successor = new Map();
  for (const l of data.links) if (l.kind === 'supersedes') successor.set(l.to, l.from);
  // Clusters sit on a pentagon whose size follows the biggest cluster, so a
  // small library is not lost in empty space and a large one does not overlap.
  const biggest = Math.max(1, ...ORDER.map(kind => stars.filter(s => s.kind === kind).length));
  const ring = Math.max(CLUSTER_R * 0.35, SPIRAL * Math.sqrt(biggest) * 1.9 + 60);
  const clusters = ORDER.map((kind, i) => {
    const a = -Math.PI / 2 + i * Math.PI * 2 / ORDER.length;
    return {kind, x: Math.cos(a) * ring, y: Math.sin(a) * ring, count: 0, r: 0};
  });
  for (const c of clusters) {
    const members = stars.filter(s => s.kind === c.kind && !(s.state === 'deleted' && byId.has(successor.get(s.id))))
      .sort((a, b) => rank(a) - rank(b) || a.created - b.created || (a.id < b.id ? -1 : 1));
    const spin = hash(c.kind) * Math.PI * 2;
    members.forEach((s, k) => {
      const r = SPIRAL * Math.sqrt(k + 0.6), a = spin + k * GOLDEN;
      s.x = c.x + Math.cos(a) * r; s.y = c.y + Math.sin(a) * r; c.r = Math.max(c.r, r);
    });
    c.count = stars.filter(s => s.kind === c.kind).length;
  }
  // A superseded wreck drifts just beside the memory that replaced it.
  const place = (s, depth = 0) => {
    if (s.x !== undefined || depth > 8) return;
    const next = byId.get(successor.get(s.id));
    if (next) place(next, depth + 1);
    const a = hash(s.id) * Math.PI * 2, d = 26 + hash(s.id + 'd') * 14;
    s.x = (next?.x ?? 0) + Math.cos(a) * d; s.y = (next?.y ?? 0) + Math.sin(a) * d;
  };
  stars.forEach(s => place(s));
  stars.forEach(s => {
    s.size = 3.2 + Math.min(4, (s.version || 1)) * 0.9 + (s.state === 'active' ? 1 : 0);
    s.phase = hash(s.id + 'p') * Math.PI * 2;
  });
  const links = data.links.map(l => ({...l, a: byId.get(l.from), b: byId.get(l.to)})).filter(l => l.a && l.b);
  return {stars, links, clusters, byId, now: data.now};
}

function shape(ctx, kind, x, y, r) {
  ctx.beginPath();
  switch (KIND[kind]?.shape) {
    case 'cross': for (let i = 0; i < 8; i++) {const a = i * Math.PI / 4 - Math.PI / 2, rr = i % 2 ? r * 0.38 : r * 1.25; ctx[i ? 'lineTo' : 'moveTo'](x + Math.cos(a) * rr, y + Math.sin(a) * rr);} ctx.closePath(); break;
    case 'diamond': ctx.moveTo(x, y - r * 1.2); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r * 1.2); ctx.lineTo(x - r, y); ctx.closePath(); break;
    case 'square': ctx.rect(x - r * 0.85, y - r * 0.85, r * 1.7, r * 1.7); break;
    case 'ring': ctx.arc(x, y, r, 0, Math.PI * 2); ctx.moveTo(x + r * 1.7, y); ctx.ellipse(x, y, r * 1.7, r * 0.55, -0.4, 0, Math.PI * 2); break;
    default: ctx.arc(x, y, r, 0, Math.PI * 2);
  }
}

export function installGalaxy({state, api, esc, timeLabel}) {
  const root = document.createElement('div');
  root.className = 'galaxy';
  root.innerHTML = `<div class="galaxy-bar"><span class="galaxy-count muted"></span><button class="small-link" data-galaxy-fit>回到全景</button><button class="small-link" data-galaxy-full>全屏</button></div>
    <canvas class="galaxy-canvas" aria-label="记忆星系，拖动平移，滚轮缩放，点击星星看详情"></canvas>
    <div class="galaxy-legend">${ORDER.map(k => `<span><i style="color:${KIND[k].colour}">${{cross: '✦', circle: '●', diamond: '◆', ring: '◎', square: '■'}[KIND[k].shape]}</i>${KIND[k].label}</span>`).join('')}<span><i class="dim">○</i>待确认</span><span><i class="dim">✕</i>被取代的残骸</span><span><i class="blink">●</i>快到期</span></div>
    <article class="galaxy-card"><p class="muted">点一颗星，看它的原话、出处和版本历史。这里只读，修改请回到列表。</p></article>`;
  const $ = q => root.querySelector(q);
  const canvas = $('.galaxy-canvas'), ctx = canvas.getContext('2d');
  let world = null, signature = '', loading = null, selected = null, hover = null, cam = {x: 0, y: 0, k: 0.5}, fitted = false, frame = 0, last = 0, sky = null, view = {w: 0, h: 0};
  root.galaxyFrames = 0;

  function resize() {
    const box = canvas.getBoundingClientRect(), ratio = devicePixelRatio || 1;
    const w = Math.max(240, box.width), h = root.classList.contains('full') ? Math.max(320, innerHeight - 110) : Math.max(320, Math.min(520, w * 1.15));
    canvas.style.height = h + 'px';
    if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio); sky = null;}
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    view = {w, h};
  }
  function starfield() {
    // Background dust, drawn once per canvas size.
    sky = document.createElement('canvas'); sky.width = canvas.width; sky.height = canvas.height;
    const g = sky.getContext('2d'), ratio = devicePixelRatio || 1;
    const grad = g.createRadialGradient(sky.width / 2, sky.height / 2, 0, sky.width / 2, sky.height / 2, Math.max(sky.width, sky.height) * 0.7);
    grad.addColorStop(0, '#1b1f3a'); grad.addColorStop(1, '#07080f'); g.fillStyle = grad; g.fillRect(0, 0, sky.width, sky.height);
    let seed = 20261110;
    const rand = () => {seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296;};
    g.fillStyle = '#fff';
    for (let i = 0; i < 260; i++) {const big = rand() > 0.9, d = ratio * (big ? 1.5 : 0.8); g.globalAlpha = 0.15 + rand() * 0.5; g.fillRect(rand() * sky.width, rand() * sky.height, d, d);}
    g.globalAlpha = 1;
  }
  const toScreen = (x, y) => [(x - cam.x) * cam.k + view.w / 2, (y - cam.y) * cam.k + view.h / 2];
  const toWorld = (sx, sy) => [(sx - view.w / 2) / cam.k + cam.x, (sy - view.h / 2) / cam.k + cam.y];
  function fit() {
    if (!world?.stars.length) return;
    const xs = world.clusters.flatMap(c => [c.x - c.r - 60, c.x + c.r + 60]), ys = world.clusters.flatMap(c => [c.y - c.r - 80, c.y + c.r + 60]);
    cam.x = (Math.min(...xs) + Math.max(...xs)) / 2; cam.y = (Math.min(...ys) + Math.max(...ys)) / 2;
    cam.k = Math.min(view.w / (Math.max(...xs) - Math.min(...xs)), view.h / (Math.max(...ys) - Math.min(...ys))) * 0.95;
  }

  function draw(now) {
    resize();
    if (!sky) starfield();
    ctx.drawImage(sky, 0, 0, view.w, view.h);
    if (!world) {ctx.fillStyle = '#aab'; ctx.font = '13px "Microsoft YaHei",sans-serif'; ctx.textAlign = 'center'; ctx.fillText(loading ? '正在读记忆…' : '还没有记忆', view.w / 2, view.h / 2); return;}
    if (!world.stars.length) {ctx.fillStyle = '#aab'; ctx.font = '13px "Microsoft YaHei",sans-serif'; ctx.textAlign = 'center'; ctx.fillText('还没有记忆，星系是空的', view.w / 2, view.h / 2); return;}
    const t = now / 1000, k = cam.k, focus = selected || hover;
    const near = focus ? new Set([focus.id, ...world.links.filter(l => l.a === focus || l.b === focus).flatMap(l => [l.a.id, l.b.id])]) : null;
    // Cluster haze and names.
    for (const c of world.clusters) {
      const [x, y] = toScreen(c.x, c.y), r = (c.r + 50) * k, colour = KIND[c.kind].colour;
      const grad = ctx.createRadialGradient(x, y, 0, x, y, r); grad.addColorStop(0, colour + '2a'); grad.addColorStop(1, colour + '00');
      ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = colour; ctx.font = `600 ${Math.max(11, Math.min(16, 15 * k + 4))}px "Microsoft YaHei",sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      ctx.fillText(`${KIND[c.kind].label} · ${c.count}`, x, y - (c.r + 22) * k);
    }
    // Links: same source (white), same keyword (kind colour), supersedes (grey arrow wreck → successor).
    ctx.lineCap = 'round';
    for (const l of world.links) {
      const [x1, y1] = toScreen(l.a.x, l.a.y), [x2, y2] = toScreen(l.b.x, l.b.y);
      const lit = near && near.has(l.a.id) && near.has(l.b.id) && (l.a === focus || l.b === focus);
      if (l.kind === 'supersedes') {
        // Draw from the old memory (b) to the new one (a).
        ctx.strokeStyle = lit ? '#e6e6f0' : 'rgba(170,170,190,.45)'; ctx.lineWidth = lit ? 1.6 : 1; ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x1, y1); ctx.stroke(); ctx.setLineDash([]);
        const a = Math.atan2(y1 - y2, x1 - x2), ex = x1 - Math.cos(a) * (l.a.size * k + 4), ey = y1 - Math.sin(a) * (l.a.size * k + 4);
        ctx.fillStyle = ctx.strokeStyle; ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(ex - Math.cos(a - 0.45) * 7, ey - Math.sin(a - 0.45) * 7); ctx.lineTo(ex - Math.cos(a + 0.45) * 7, ey - Math.sin(a + 0.45) * 7); ctx.fill();
      } else {
        const colour = l.kind === 'source' ? '255,255,255' : hexRgb(KIND[l.a.kind]?.colour || '#ffffff');
        ctx.strokeStyle = `rgba(${colour},${lit ? 0.85 : near ? 0.06 : 0.16})`; ctx.lineWidth = lit ? 1.5 : 0.8;
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      }
    }
    // Stars.
    let drawn = 0;
    for (const s of world.stars) {
      const [x, y] = toScreen(s.x, s.y);
      if (x < -30 || y < -30 || x > view.w + 30 || y > view.h + 30) continue;
      drawn++;
      const colour = KIND[s.kind]?.colour || '#fff', r = Math.max(1.6, s.size * Math.min(1.6, Math.max(0.55, k * 1.4)));
      const dimmed = near && !near.has(s.id);
      const soon = s.kind === 'ongoing' && s.state === 'active' && !s.expired && s.expires_at && s.expires_at - world.now < SOON;
      const twinkle = 0.85 + 0.15 * Math.sin(t * 1.7 + s.phase);
      ctx.globalAlpha = dimmed ? 0.25 : 1;
      if (s.state === 'deleted') {
        // Wreck: grey shards, no glow.
        ctx.strokeStyle = '#7d7f8c'; ctx.lineWidth = 1.2;
        for (let i = 0; i < 3; i++) {const a = s.phase + i * 2.1, d = r * 0.9; ctx.beginPath(); ctx.moveTo(x + Math.cos(a) * d * 0.3, y + Math.sin(a) * d * 0.3); ctx.lineTo(x + Math.cos(a + 0.5) * d, y + Math.sin(a + 0.5) * d); ctx.stroke();}
        ctx.fillStyle = '#5c5e6a'; ctx.fillRect(x - 1, y - 1, 2, 2);
      } else if (s.state === 'pending') {
        ctx.strokeStyle = colour; ctx.globalAlpha *= 0.65; ctx.lineWidth = 1.1; ctx.setLineDash([2.5, 2.5]);
        shape(ctx, s.kind, x, y, r); ctx.stroke(); ctx.setLineDash([]);
      } else {
        const blink = soon ? 0.35 + 0.65 * Math.abs(Math.sin(t * 3.2 + s.phase)) : twinkle;
        const faded = s.expired ? 0.35 : 1;
        ctx.globalAlpha = (dimmed ? 0.25 : 1) * faded * (soon ? blink : 1);
        const glow = ctx.createRadialGradient(x, y, 0, x, y, r * 4); glow.addColorStop(0, colour + 'aa'); glow.addColorStop(1, colour + '00');
        ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(x, y, r * 4 * (soon ? blink : twinkle), 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = colour; ctx.strokeStyle = colour; ctx.lineWidth = 1.4;
        shape(ctx, s.kind, x, y, r);
        KIND[s.kind]?.shape === 'ring' ? (ctx.stroke(), ctx.beginPath(), ctx.arc(x, y, r * 0.8, 0, Math.PI * 2), ctx.fill()) : ctx.fill();
        ctx.fillStyle = '#fff'; ctx.globalAlpha *= 0.9; ctx.beginPath(); ctx.arc(x, y, Math.max(0.8, r * 0.3), 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (s === selected || s === hover) {ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(x, y, r + 7, 0, Math.PI * 2); ctx.stroke();}
      if (k > 1.15 || s === selected || s === hover || (near && near.has(s.id) && k > 0.6)) {
        ctx.font = `${s === selected ? 600 : 400} 11px "Microsoft YaHei",sans-serif`; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillStyle = s.state === 'deleted' ? '#8b8d99' : s.state === 'pending' ? 'rgba(230,230,240,.7)' : '#f2f2f7';
        ctx.fillText(s.key + (s.state === 'deleted' ? '（已被取代）' : s.state === 'pending' ? '（待确认）' : soon ? '（快到期）' : ''), x + r + 6, y);
      }
    }
    root.dataset.drawn = String(drawn);
  }
  const hexRgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)).join(',');

  function loop(now) {
    frame = 0;
    if (!root.isConnected) return;
    if (now - last > 33) {last = now; timed(now);}
    frame = requestAnimationFrame(loop);
  }
  function kick() {if (!frame) frame = requestAnimationFrame(loop);}
  // Draw cost is recorded so smoothness can be checked at a few hundred stars.
  function timed(now) {const t0 = performance.now(); draw(now); const ms = performance.now() - t0; root.galaxyFrames++; root.dataset.drawMs = ms.toFixed(2); root.galaxyWorst = Math.max(root.galaxyWorst || 0, ms);}

  function card(s) {
    if (!s) return '<p class="muted">点一颗星，看它的原话、出处和版本历史。这里只读，修改请回到列表。</p>';
    const kind = KIND[s.kind]?.label || s.kind;
    const status = s.state === 'deleted' ? '已被取代或删除，不再加载' : s.state === 'pending' ? '待确认，还没生效' : s.expired ? `已于 ${new Date(s.expires_at * 1000).toLocaleDateString('zh-CN')} 到期，已停用`
      : s.kind === 'ongoing' && s.expires_at ? `生效中，到 ${new Date(s.expires_at * 1000).toLocaleDateString('zh-CN')}` : '生效中';
    const origin = s.origin || (s.source_type === 'user_explicit' ? '你明确保存' : s.source_type === 'user_confirmed_proposal' ? '你确认的建议' : s.source_type === 'proposal' ? '整理时提出的建议' : '');
    const quotes = (s.evidence || []).filter(e => e.quote).map(e => `<blockquote>「${esc(e.quote)}」${e.created ? `<small>${esc(timeLabel(e.created))}</small>` : ''}</blockquote>`).join('') || (s.quote ? `<blockquote>「${esc(s.quote)}」</blockquote>` : '<p class="muted">账本里没有原话记录。</p>');
    const related = world.links.filter(l => l.a === s || l.b === s).map(l => {
      const other = l.a === s ? l.b : l.a;
      const why = l.kind === 'supersedes' ? (l.a === s ? '取代了' : '被它取代') : l.kind === 'source' ? '同一句话里说的' : `同一个关键词「${l.label}」`;
      return `<button class="small-link" data-galaxy-star="${esc(other.id)}">${esc(why)}：${esc(other.key)}</button>`;
    }).join('');
    const history = s.history.length ? `<ol class="galaxy-history">${s.history.map(h => `<li>版本 ${h.version} · ${esc(ACTIONS[h.action] || h.action)} · ${esc(timeLabel(h.at))}</li>`).join('')}</ol>` : '<p class="muted">还没有版本记录（确认后才有）。</p>';
    return `<div class="galaxy-card-head"><span class="galaxy-dot" style="background:${KIND[s.kind]?.colour}"></span><strong>${esc(s.key)}</strong><span class="muted">${esc(kind)} · ${esc(status)}</span></div>
      ${s.value ? `<p>${esc(s.value)}</p>` : ''}<h4>原话</h4>${quotes}<h4>出处</h4><p>${esc(origin)}${s.reason ? ` · ${esc(s.reason)}` : ''}${s.last_used ? ` · 最近用过 ${esc(timeLabel(s.last_used))}` : ''}</p>
      <h4>版本历史</h4>${history}${related ? `<h4>连线</h4><div class="galaxy-related">${related}</div>` : ''}`;
  }
  function select(star) {selected = star; $('.galaxy-card').innerHTML = card(star); root.dataset.selected = star?.id || '';}
  function hit(sx, sy) {
    if (!world) return null;
    let best = null, bestD = 14 * 14;
    for (const s of world.stars) {const [x, y] = toScreen(s.x, s.y), d = (x - sx) ** 2 + (y - sy) ** 2; if (d < bestD) {bestD = d; best = s;}}
    return best;
  }

  // Pointer: drag pans, wheel zooms at the cursor, a click without movement selects.
  let drag = null;
  const point = e => {const b = canvas.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top];};
  canvas.addEventListener('pointerdown', e => {canvas.setPointerCapture(e.pointerId); const [x, y] = point(e); drag = {x, y, cx: cam.x, cy: cam.y, moved: 0};});
  canvas.addEventListener('pointermove', e => {
    const [x, y] = point(e);
    if (drag) {drag.moved = Math.max(drag.moved, Math.hypot(x - drag.x, y - drag.y)); cam.x = drag.cx - (x - drag.x) / cam.k; cam.y = drag.cy - (y - drag.y) / cam.k; canvas.style.cursor = 'grabbing';}
    else {hover = hit(x, y); canvas.style.cursor = hover ? 'pointer' : 'grab';}
  });
  canvas.addEventListener('pointerup', e => {const [x, y] = point(e); if (drag && drag.moved < 4) select(hit(x, y)); drag = null; canvas.style.cursor = 'grab';});
  canvas.addEventListener('pointerleave', () => {hover = null;});
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const [sx, sy] = point(e), [wx, wy] = toWorld(sx, sy);
    cam.k = Math.max(0.15, Math.min(6, cam.k * Math.exp(-e.deltaY * 0.0015)));
    cam.x = wx - (sx - view.w / 2) / cam.k; cam.y = wy - (sy - view.h / 2) / cam.k;
  }, {passive: false});
  root.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.galaxyStar) {const s = world.byId.get(b.dataset.galaxyStar); if (s) {select(s); cam.x = s.x; cam.y = s.y; cam.k = Math.max(cam.k, 1.4);}}
    if (b.hasAttribute('data-galaxy-fit')) fit();
    if (b.hasAttribute('data-galaxy-full')) toggleFull(!root.classList.contains('full'));
  });
  document.addEventListener('keydown', e => {if (e.key === 'Escape' && root.classList.contains('full')) toggleFull(false);});
  function toggleFull(on) {
    const holder = root.parentElement;
    root.classList.toggle('full', on);
    if (on) {root.placeholder = holder; document.body.append(root);} else {root.remove(); state.detailSignature = ''; document.dispatchEvent(new CustomEvent('galaxy:closed'));}
    $('[data-galaxy-full]').textContent = on ? '退出全屏' : '全屏';
    // Timer, not an animation frame: a background tab may withhold frames.
    sky = null; setTimeout(() => {resize(); fit(); draw(performance.now()); kick();}, 30);
  }

  async function load(room) {
    loading = room;
    try {
      const data = await api('/api/memory/galaxy?room=' + encodeURIComponent(room));
      if (loading !== room) return;
      const keep = selected?.id;
      world = layoutGalaxy(data);
      const live = world.stars.filter(s => s.state === 'active' && !s.expired).length, pending = world.stars.filter(s => s.state === 'pending').length;
      $('.galaxy-count').textContent = `${live} 颗亮星 · ${pending} 颗待确认 · ${world.stars.length - live - pending} 颗暗星或残骸 · ${world.links.length} 条连线`;
      if (!fitted) {resize(); fit(); fitted = true;}
      select(keep ? world.byId.get(keep) || null : null);
      timed(performance.now());
    } catch (error) {$('.galaxy-count').textContent = error.message;}
    finally {if (loading === room) loading = null;}
  }
  function mount(box, room, sig) {
    if (root.classList.contains('full')) {box.insertAdjacentHTML('beforeend', '<div class="empty-detail">星系正在全屏显示，按 Esc 退出。</div>'); return;}
    box.append(root);
    if (sig !== signature) {signature = sig; load(room);}
    kick();
  }
  new ResizeObserver(() => {sky = null;}).observe(canvas);
  return {mount, root, load, draw: timed, fit, select: id => select(world?.byId.get(id) || null), camera: cam, world: () => world};
}
