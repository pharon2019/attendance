/* Service worker: keeps the app shell available offline so employees can punch without a connection.
   API calls are never cached — offline punches are queued by the app itself (localStorage) and synced later. */
const VERSION = 'att-v3';
const SHELL = ['/', '/index.html', '/css/app.css', '/js/i18n.js', '/js/app.js', '/manifest.webmanifest',
  '/icons/icon.svg', '/icons/icon-192.png', '/icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin === location.origin && url.pathname.startsWith('/api/')) return;      // network only
  if (url.hostname.endsWith('tile.openstreetmap.org') || url.hostname.startsWith('nominatim')) return;

  if (url.origin === location.origin) {
    // network first so updates arrive immediately; fall back to cache when offline
    e.respondWith(fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then(r => r || (req.mode === 'navigate' ? caches.match('/index.html') : Response.error()))));
    return;
  }
  // third-party libraries (Leaflet): cache first
  e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
    return res;
  })));
});
