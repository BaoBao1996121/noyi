// Noyi service worker: an offline shell plus system notifications.
// API responses are never cached, so diet and health data never sit in the phone's cache.
const SHELL = 'noyi-shell-v1';
const FILES = ['/assets/style.css', '/assets/modules.css', '/assets/mobile.css', '/assets/icon-192.png'];

self.addEventListener('install', e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).catch(() => {})); self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/r/')) return;
  if (FILES.includes(url.pathname)) e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
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
