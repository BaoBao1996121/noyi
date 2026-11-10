// Pixel factory skin for the ledger replay: a miniature assembly line.
// Every sprite below is a character grid plus the palette PAL, written in this
// file and owned by the project (same licence as the code). No image files,
// no third-party art, no dependencies. Grids are baked once into 1-pixel
// offscreen canvases and drawn at an integer scale with smoothing off.
const PAL = {
  k: '#2b2b3a', d: '#555b6a', g: '#8a8f9e', G: '#c5c9d3', w: '#f4f1e8', r: '#d64545', R: '#f0a399', o: '#e8873a', y: '#f2c84b',
  b: '#4f7fd8', B: '#9cc3f5', n: '#8b5a2b', N: '#c08a4f', e: '#2f9e6b', E: '#8fd6a8', s: '#f1c27d', h: '#3a2a1e',
};
const SPRITES = {
  // 1 回复层判断: a clerk at the front desk reading the note
  desk: ['................', '......hhhh......', '.....hhhhhh.....', '.....hsssss.....', '.....skssks.....', '.....ssssss.....', '......ssss......', '....bbbbbbbb....',
    '...bbbbbbbbbb...', '..kkkkkkkkkkkk..', '..kNNNNNNNNNNk..', '..kNwwwwNNNNNk..', '..kNNNNNNNNNNk..', '..knnnnnnnnnnk..', '..kn........nk..', '..kk........kk..'],
  // 2 登记承诺: the open ledger book on its stand
  book: ['................', '................', '................', '..kkkkkkkkkkkk..', '..kwwwwkkwwwwk..', '..kwddwkkwddwk..', '..kwwwwkkwwwwk..', '..kwddwkkwdddk..',
    '..kwwwwkkwwwwk..', '..kwddwkkwrrwk..', '..kkkkkkkkkkkk..', '.......kk.......', '.......kk.......', '......kddk......', '.....kddddk.....', '....kkkkkkkk....'],
  // 3 开工: the punch clock, a work card in the slot
  clock: ['................', '....kkkkkkkk....', '...kGGGGGGGGk...', '...kGkkkkkkGk...', '...kGkwwwwkGk...', '...kGkwkwwkGk...', '...kGkwkkwkGk...', '...kGkwwwwkGk...',
    '...kGkkkkkkGk...', '...kGGGGGGGGk...', '...kGGkkkkGGk...', '...kGGGyyGGGk...', '...kGGGyyGGGk...', '...kGGGGGGGGk...', '...kkkkkkkkkk...', '....kk....kk....'],
  // 4 模型请求: the thinking furnace with a chimney
  furnace: ['...........kk...', '..........kddk..', '..........kddk..', '.kkkkkkkkkkkkkk.', '.kddddddddddddk.', '.kdggggggggggdk.', '.kdgkkkkkkkkgdk.', '.kdgkooyyookgdk.',
    '.kdgkoyyyyokgdk.', '.kdgkoyrryokgdk.', '.kdgkookookgdk.', '.kdgkkkkkkkkgdk.', '.kdggggggggggdk.', '.kddddddddddddk.', '.kkkkkkkkkkkkkk.', '.kk..........kk.'],
  // 5 工具调用: a robot arm bringing back a card (web page or file)
  arm: ['................', '.........kkkkkk.', '.........kwwwwk.', '.........kbbwwk.', '.........kkkkkk.', '..........k..k..', '..........kGGk..', '.........kGGk...',
    '........kGGk....', '.......kGGk.....', '......kooook....', '......kooook....', '.......kGGk.....', '.....kkkkkkkk...', '....kddddddddk..', '....kkkkkkkkkk..'],
  // 6 交付: the shipping dock, shutter half open, a parcel inside
  dock: ['................', '..kkkkkkkkkkkk..', '..kbbbbbbbbbbk..', '..kkkkkkkkkkkk..', '..kGdGdGdGdGdk..', '..kdGdGdGdGdGk..', '..kGdGdGdGdGdk..', '..kdGdGdGdGdGk..',
    '..kddddddddddk..', '..kddNNNNddddk..', '..kddNnnNddddk..', '..kddNNNNddddk..', '..kddddddddddk..', '..kkkkkkkkkkkk..', '..kk........kk..', '................'],
  // 7 承诺兑现: the ledger wall, one line per promise, a tick box each
  board: ['................', '.kkkkkkkkkkkkkk.', '.knnnnnnnnnnnnk.', '.knwwwwwwwwwwnk.', '.knwdddddwGGwnk.', '.knwwwwwwwwwwnk.', '.knwdddddwGGwnk.', '.knwwwwwwwwwwnk.',
    '.knwdddddwGGwnk.', '.knwwwwwwwwwwnk.', '.knwdddddwGGwnk.', '.knwwwwwwwwwwnk.', '.knnnnnnnnnnnnk.', '.kkkkkkkkkkkkkk.', '.......kk.......', '.....kkkkkk.....'],
  // the task: a cardboard box
  box: ['.kkkkkkkk.', 'kNNNnnNNNk', 'kNNNnnNNNk', 'knnnnnnnnk', 'kNNNnnNNNk', 'kNNNnnNNNk', 'kNNNNNNNNk', '.kkkkkkkk.'],
  stamp: ['..kk..', '.kddk.', '.kddk.', '..kk..', '.kkkk.', 'kddddk', 'krrrrk', 'kkkkkk'],
  seal: ['rrrr', 'r..r', 'r..r', 'rrrr'],
  check: ['......e', '.....ee', 'e...ee.', 'ee.ee..', '.eee...', '..e....'],
  cross: ['r...r', '.r.r.', '..r..', '.r.r.', 'r...r'],
  alarm: ['.rrrrr.', 'rwwkwwr', 'rwwkwwr', 'rwwkkwr', 'rwwwwwr', 'rwwwwwr', '.rrrrr.'],
  bubble: ['.kkkkkkk.', 'kwwwwwwwk', 'kwrwwwrwk', 'kwwrwrwwk', 'kwwwrwwwk', 'kwwrwrwwk', 'kwrwwwrwk', 'kwwwwwwwk', '.kkkkkkk.', '..kk.....', '.k.......'],
  hazard: ['kkkkkkkkkk', 'kykkyykkyk', 'kyykkyykkk', 'kkyykkyykk', 'kkkkkkkkkk', '....kk....', '....kk....'],
  tag: ['kkkkk', 'kyyyk', 'kyyyk', 'kyyyk', 'kkkkk'],
  paper: ['kkkkkk', 'kwwwwk', 'kwddwk', 'kwwwwk', 'kkkkkk'],
};
const MACHINE = {intake: 'desk', commit: 'book', start: 'clock', model: 'furnace', tool: 'arm', deliver: 'dock', fulfil: 'board'};
const ORDER = ['intake', 'commit', 'start', 'model', 'tool', 'deliver', 'fulfil'];
const GREY = {r: 'g', R: 'G', o: 'g', y: 'G', b: 'g', B: 'G', e: 'g', E: 'G', n: 'd', N: 'g', s: 'G', h: 'd'};
const ON_DARK = {ok: '#8fd6a8', warn: '#f2c84b', bad: '#f0a399', info: '#9cc3f5'};
const LAMP = {ok: 'e', warn: 'y', bad: 'r', info: 'b'};
const FONT = '"Microsoft YaHei",sans-serif';

const baked = new Map();
function bake(name, swap) {
  const key = name + (swap ? JSON.stringify(swap) : '');
  let canvas = baked.get(key);
  if (canvas) return canvas;
  const rows = SPRITES[name];
  canvas = document.createElement('canvas');
  canvas.width = Math.max(...rows.map(r => r.length)); canvas.height = rows.length;
  const g = canvas.getContext('2d');
  rows.forEach((row, y) => [...row].forEach((ch, x) => {
    const colour = PAL[swap?.[ch] ?? ch];
    if (colour) {g.fillStyle = colour; g.fillRect(x, y, 1, 1);}
  }));
  baked.set(key, canvas);
  return canvas;
}
const size = name => [Math.max(...SPRITES[name].map(r => r.length)), SPRITES[name].length];
function put(ctx, name, x, y, S, {swap = null, alpha = 1} = {}) {
  const canvas = bake(name, swap);
  ctx.imageSmoothingEnabled = false; ctx.globalAlpha = alpha;
  ctx.drawImage(canvas, Math.round(x), Math.round(y), canvas.width * S, canvas.height * S);
  ctx.globalAlpha = 1;
}
// A sign plate: dark frame, flat fill, hard pixel shadow. Text stays a system font (CJK).
function plate(ctx, view, text, x, y, {fg = '#fff', bg = '#2b2b3a', frame = '#2b2b3a', align = 'center', px = 12, max = 260} = {}) {
  ctx.font = `600 ${px}px ${FONT}`;
  let shown = String(text);
  while (shown.length > 1 && ctx.measureText(shown).width > max) shown = shown.slice(0, -2) + '…';
  const w = Math.ceil(ctx.measureText(shown).width) + 14, h = px + 10;
  let left = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
  left = Math.round(Math.max(4, Math.min(view.width - w - 6, left)));
  const top = Math.round(Math.max(4, Math.min(view.height - h - 6, y - h / 2)));
  ctx.fillStyle = 'rgba(43,43,58,.25)'; ctx.fillRect(left + 3, top + 3, w, h);
  ctx.fillStyle = frame; ctx.fillRect(left, top, w, h);
  ctx.fillStyle = bg; ctx.fillRect(left + 2, top + 2, w - 4, h - 4);
  ctx.fillStyle = fg; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(shown, left + 7, top + h / 2 + 1);
  return {left, top, w, h};
}
const blink = (view, ms = 260) => Math.floor((view.now || 0) / ms) % 2 === 0;
const kinds = view => new Set((view.history || []).map(e => e.kind));
// Where the promise's proof came from, as recorded in the ledger event's facts.
function provenance(event) {
  const fact = name => event.facts.find(([k]) => k === name)?.[1];
  if (fact('成果校验值')) return `成果 sha256 ${fact('成果校验值').slice(0, 12)}…`;
  if (fact('读过的网页')) return `读过的网页 ${fact('读过的网页')}`;
  if (fact('成功操作')) return `成功操作 ${fact('成功操作')}`;
  if (fact('告知你的消息')) return `告诉你：${fact('告知你的消息')}`;
  return '账本里有兑现记录';
}

export const pixelSkin = {
  name: 'pixel',
  label: '像素工厂',
  animated: true,
  at: {},
  scale(view) {
    if (view.vertical) return 2;
    const gap = (view.width - 140) / ((this.count || 7) - 1);
    return Math.max(2, Math.min(5, Math.floor(gap / 22), Math.floor(view.height * 0.3 / 16)));
  },
  layout(width, height, count, vertical) {
    this.count = count; this.at = {};
    return Array.from({length: count}, (_, i) => vertical
      ? {x: 82, y: 92 + i * (height - 128) / (count - 1)}
      : {x: 70 + i * (width - 140) / (count - 1), y: Math.round(height * 0.62)});
  },
  background(ctx, view) {
    const S = this.scale(view), {width: w, height: h} = view, line = Math.max(1, S >> 1);
    const floor = view.vertical ? h : Math.round(h * 0.62) + 4 * S;
    ctx.fillStyle = '#ece6d6'; ctx.fillRect(0, 0, w, floor);
    ctx.fillStyle = '#ddd5c1';
    for (let y = 0, row = 0; y < floor; y += 4 * S, row++) {
      ctx.fillRect(0, y, w, line);
      for (let x = (row % 2) * 6 * S; x < w; x += 12 * S) ctx.fillRect(x, y, line, 4 * S);
    }
    if (!view.vertical) {
      ctx.fillStyle = '#cbc1aa'; ctx.fillRect(0, floor, w, h - floor);
      ctx.fillStyle = '#bfb49b';
      for (let x = 0; x < w; x += 10 * S) ctx.fillRect(x, floor, line, h - floor);
      for (let y = floor; y < h; y += 6 * S) ctx.fillRect(0, y, w, line);
      if (w > 640) plate(ctx, view, '说到做到 · 账本工厂', w - 16, 22, {align: 'right', bg: '#8b5a2b', frame: '#3a2a1e', fg: '#f4f1e8', px: 12});
    }
  },
  track(ctx, view, a, b, done) {
    const S = this.scale(view), shift = done ? Math.floor((view.now || 0) / 90) % 4 * S : 0;
    ctx.globalAlpha = done ? 1 : 0.5;
    if (view.vertical) {
      const x = a.x - 2 * S, top = Math.min(a.y, b.y), len = Math.abs(b.y - a.y);
      ctx.fillStyle = PAL.k; ctx.fillRect(x, top, 4 * S, len);
      ctx.fillStyle = '#6b6f7c'; ctx.fillRect(x + S, top, 2 * S, len);
      ctx.fillStyle = PAL.G; for (let y = top + shift; y < top + len; y += 4 * S) ctx.fillRect(x + S, y, 2 * S, S);
    } else {
      const left = Math.min(a.x, b.x), len = Math.abs(b.x - a.x), y = a.y;
      ctx.fillStyle = PAL.k; ctx.fillRect(left, y, len, 4 * S);
      ctx.fillStyle = '#6b6f7c'; ctx.fillRect(left, y + S, len, 2 * S);
      ctx.fillStyle = PAL.G; for (let x = left + shift; x < left + len; x += 4 * S) ctx.fillRect(x, y + S, S, 2 * S);
      ctx.fillStyle = PAL.k; for (let x = left + 6 * S; x < left + len; x += 16 * S) ctx.fillRect(x, y + 4 * S, S, 3 * S);
    }
    ctx.globalAlpha = 1;
  },
  machine(view, s) {
    const S = this.scale(view), name = MACHINE[s.id] || MACHINE[ORDER[s.index]], [w, h] = size(name);
    const x = view.vertical ? s.pos.x - 2 * S - 6 - w * S : s.pos.x - w * S / 2, y = view.vertical ? s.pos.y - h * S / 2 : s.pos.y - h * S;
    return {name, x, y, w: w * S, h: h * S, S};
  },
  station(ctx, view, s) {
    const m = this.machine(view, s), {S} = m, seen = new Set(s.kinds || []);
    this.at[s.id || ORDER[s.index]] = {...s.pos, m};
    if (s.active) {ctx.fillStyle = 'rgba(242,200,75,.35)'; ctx.fillRect(m.x - 2 * S, m.y - 2 * S, m.w + 4 * S, m.h + 4 * S);}
    const flame = s.active && blink(view, 160) ? {y: 'o', o: 'y'} : null;
    put(ctx, m.name, m.x, m.y, S, {swap: s.visited ? flame : GREY, alpha: s.visited ? 1 : 0.6});
    // Status lamp on the machine's corner; a bad event leaves it blinking red.
    if (s.visited && s.tone) {
      const colour = s.bad ? (blink(view) ? PAL.r : PAL.R) : PAL[LAMP[s.tone] || 'b'];
      ctx.fillStyle = PAL.k; ctx.fillRect(m.x + m.w - 3 * S, m.y - S, 3 * S, 3 * S);
      ctx.fillStyle = colour; ctx.fillRect(m.x + m.w - 2 * S, m.y, S, S);
    }
    // Lasting marks: what this station has seen so far stays visible.
    if (seen.has('fulfilled')) put(ctx, 'check', m.x + m.w / 2 - 3.5 * S, m.y + 5 * S, S);
    if (seen.has('unable')) put(ctx, 'cross', m.x + m.w / 2 - 2.5 * S, m.y + 5 * S, S);
    if (seen.has('released')) {ctx.fillStyle = PAL.d; ctx.fillRect(m.x + 4 * S, m.y + 7 * S, 8 * S, S);}
    if (seen.has('overdue_told')) put(ctx, 'alarm', m.x + m.w - 4 * S, m.y - 6 * S, S);
    if (seen.has('superseded')) put(ctx, 'tag', m.x + S, m.y + 2 * S, S);
    if (seen.has('unregistered_refused')) put(ctx, 'bubble', m.x - 3 * S, m.y - 8 * S, Math.max(2, S - 1));
    if (seen.has('blocked') || seen.has('blocked_told')) put(ctx, 'hazard', m.x + m.w - 6 * S, m.y - 5 * S, Math.max(2, S - 1));
    const label = `${s.index + 1} ${s.label}${s.count > 1 ? ` ×${s.count}` : ''}`;
    const style = {bg: s.active ? '#f2c84b' : s.visited ? '#2b2b3a' : '#8a8f9e', fg: s.active ? '#2b2b3a' : '#fff', px: view.vertical ? 12 : S >= 4 ? 13 : 11};
    if (view.vertical) plate(ctx, view, label, s.pos.x + 2 * S + 12, s.pos.y, {...style, align: 'left', max: view.width - s.pos.x - 40});
    else plate(ctx, view, label, s.pos.x, s.pos.y + 4 * S + 18, {...style, max: (view.width - 140) / 6 + 10});
  },
  lamp() {},
  box(view, pos) {
    const S = this.scale(view), [w, h] = size('box');
    return {x: pos.x - w * S / 2, y: view.vertical ? pos.y - h * S / 2 : pos.y - h * S + S, w: w * S, h: h * S, S};
  },
  token(ctx, view, pos) {
    const b = this.box(view, pos), {S} = b, seen = kinds(view), event = view.event;
    const swap = seen.has('unable') || seen.has('released') ? {N: 'G', n: 'g'} : seen.has('overdue_told') ? {N: 'R', n: 'r'} : null;
    put(ctx, 'box', b.x, b.y, S, {swap});
    const stamping = event?.kind === 'fulfilled' && (view.progress || 0) < 0.4;
    if (seen.has('fulfilled') && !stamping) put(ctx, 'seal', b.x + b.w - 5 * S, b.y + 2 * S, S);
    if (seen.has('superseded')) put(ctx, 'tag', b.x - 2 * S, b.y + 2 * S, Math.max(1, S - 1));
    if (seen.has('unable')) put(ctx, 'cross', b.x + 2.5 * S, b.y + 1.5 * S, S);
    if (seen.has('overdue_told')) put(ctx, 'alarm', b.x + b.w / 2 - 3.5 * S, b.y - 8 * S, S);
    this.proof(ctx, view);
  },
  // The proof slip next to the ledger wall once a promise is kept.
  proof(ctx, view) {
    const kept = [...(view.history || [])].reverse().find(e => e.kind === 'fulfilled'), wall = this.at.fulfil;
    if (!kept || !wall) return;
    const rising = view.event === kept ? Math.min(1, Math.max(0, ((view.progress || 0) - 0.35) / 0.3)) : 1;
    if (rising <= 0) return;
    const {m} = wall, text = '出处 · ' + provenance(kept);
    ctx.globalAlpha = rising;
    if (view.vertical) plate(ctx, view, text, wall.x + 2 * m.S + 12, wall.y + 30, {align: 'left', bg: '#f4f1e8', fg: '#2b2b3a', px: 11, max: view.width - wall.x - 50});
    // On the floor under the ledger wall's sign, so it never covers the stamping.
    else plate(ctx, view, text, wall.x + 40, wall.y + 4 * m.S + 50 + (1 - rising) * 10, {align: 'right', bg: '#f4f1e8', fg: '#2b2b3a', px: m.S >= 4 ? 12 : 11, max: Math.min(420, view.width * 0.5)});
    ctx.globalAlpha = 1;
  },
  burst(ctx, view, pos, tone, progress) {
    const event = view.event, b = this.box(view, pos), {S} = b, m = this.machine(view, {id: event?.station, pos, index: 0});
    const above = view.vertical ? {x: pos.x + 2 * S + 12, y: pos.y - 22, align: 'left'} : {x: pos.x, y: m.y - 14, align: 'center'};
    const sign = (text, bg, fg = '#fff') => plate(ctx, view, text, above.x, above.y, {align: above.align, bg, fg, px: 12, max: 240});
    switch (event?.kind) {
      case 'fulfilled': {
        const t = progress < 0.4 ? progress / 0.4 : progress < 0.6 ? 1 : 1 - (progress - 0.6) / 0.4;
        put(ctx, 'stamp', b.x + b.w - 6 * S, b.y - 8 * S - (1 - t) * 12 * S, S);
        if (progress >= 0.4 && progress < 0.75) {
          ctx.fillStyle = PAL.r; for (let i = 0; i < 6; i++) {const a = i / 6 * Math.PI * 2, r = (progress - 0.4) * 30 * S; ctx.fillRect(b.x + b.w - 3 * S + Math.cos(a) * r, b.y + Math.sin(a) * r * 0.6, S, S);}
          if (!view.vertical) sign('啪！盖章 · 已兑现', '#2f9e6b');
        }
        break;
      }
      case 'overdue_told':
        put(ctx, 'alarm', b.x + b.w / 2 - 3.5 * S, b.y - 9 * S - Math.abs(Math.sin(progress * 12)) * 3 * S, S);
        if (blink(view, 220) || progress > 0.7) sign('逾期 · 已如实提醒', '#d64545');
        break;
      case 'unable':
        put(ctx, 'cross', b.x + 2.5 * S, b.y - (1 - Math.min(1, progress * 2)) * 10 * S, S);
        sign('做不到 · 已如实告知', '#e8873a');
        break;
      case 'released': sign('不再继续', '#8a8f9e'); break;
      case 'superseded': {
        // The old label flies off; a new tag is stuck on.
        ctx.globalAlpha = 1 - progress; put(ctx, 'paper', b.x - progress * 14 * S, b.y - progress * 16 * S, S); ctx.globalAlpha = 1;
        sign('改约 · 换成新的约定', '#f2c84b', '#2b2b3a');
        break;
      }
      case 'unregistered_refused':
        put(ctx, 'bubble', m.x - 3 * S, m.y - 10 * S, Math.max(2, S - 1));
        sign('口头许诺 · 被账本拦下', '#d64545');
        break;
      case 'blocked': case 'blocked_told':
        put(ctx, 'hazard', b.x, b.y - 8 * S, Math.max(2, S - 1));
        sign(event.kind === 'blocked' ? '卡住了 · 进度已保存' : '卡住超过一天 · 已提醒', '#f2c84b', '#2b2b3a');
        break;
      case 'request': {
        // Smoke from the chimney, a puff per ~600 input tokens (at most five).
        const tokens = Number(event.facts.find(([k]) => k === '输入 token')?.[1]) || 600, puffs = Math.max(1, Math.min(5, Math.round(tokens / 600)));
        const cx = view.vertical ? m.x + 11.5 * S : m.x + 11.5 * S, top = m.y;
        for (let i = 0; i < puffs; i++) {
          const p = (progress + i / puffs) % 1, r = (1 + p * 2) * S;
          ctx.globalAlpha = (1 - p) * 0.8; ctx.fillStyle = PAL.G; ctx.fillRect(cx - r + Math.sin(p * 6 + i) * 2 * S, top - p * 14 * S - r, r * 2, r * 2);
        }
        ctx.globalAlpha = 1;
        break;
      }
      default: {
        ctx.fillStyle = PAL[LAMP[tone] || 'b'];
        for (let i = 0; i < 8; i++) {
          const a = i / 8 * Math.PI * 2, r = (2 + progress * 10) * S;
          ctx.globalAlpha = 1 - progress; ctx.fillRect(b.x + b.w / 2 + Math.cos(a) * r - S / 2, b.y + b.h / 2 + Math.sin(a) * r - S / 2, S, S);
        }
        ctx.globalAlpha = 1;
      }
    }
  },
  caption(ctx, view, text, tone) {
    plate(ctx, view, text, 12, 22, {align: 'left', fg: ON_DARK[tone] || '#fff', px: 13, max: view.width - (view.width > 640 && !view.vertical ? 240 : 40)});
  },
};
