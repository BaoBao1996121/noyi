// Noyi service worker: the app shell (every script and style of this build) plus system notifications.
// API responses are never cached, so diet and health data never sit in the phone's cache,
// and the conversation on screen always comes from the ledger, never from here.
// The server fills in BUILD and SHELL (web_assets.py): each URL carries ?v=<build>, so a
// release has new URLs, a new worker and a new cache; the old cache is deleted on activate.
const BUILD = /*BUILD*/'dev';
const SHELL = /*SHELL*/[];
const CACHE = 'noyi-shell-' + BUILD;
const shell = new Set(SHELL);

self.addEventListener('install', e => {
  // Files the page already loaded come from the HTTP cache, not the network.
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/r/')) return;
  if (shell.has(url.pathname + url.search)) {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return res;
    })));
  }
  // Pages are not handled here: they always come from home (a 304 when unchanged), so the
  // page a phone opens always names the current build.
});

// The push carries only a short line and a delivery id: no meal names or numbers
// pass through Apple's push service. Opening it shows the full message in the app.
self.addEventListener('push', e => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = {}; }
  const title = data.title || 'Noyi';
  e.waitUntil(self.registration.showNotification(title, {
    body: data.body || '有一条新消息，点开查看',
    tag: data.tag || 'noyi',
    icon: '/assets/icon-192.png',
    badge: '/assets/icon-192.png',
    data: {delivery: data.delivery || null, url: '/'}
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const delivery = e.notification.data && e.notification.data.delivery;
  const target = '/' + (delivery ? '?open=' + encodeURIComponent(delivery) : '');
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) { if ('focus' in c) { c.postMessage({type: 'open-delivery', delivery}); return c.focus(); } }
    return self.clients.openWindow(target);
  }));
});
