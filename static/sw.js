'use strict';
/* App-shell service worker. It exists so the app installs and opens to an honest "can't reach DinkIQ"
   page when the server is unreachable. It deliberately caches NOTHING from /api/: stale results
   served as if they were current is the failure the in-page offline banner exists to prevent. */
const CACHE = 'dinkiq-shell-v1';
const OFFLINE = new URL('offline', self.registration.scope).href;

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(c => c.add(new Request(OFFLINE, {credentials: 'same-origin'}))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET' || req.headers.has('range')) return;          // writes, and video seeking: straight to the network
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.includes('/api/')) return;                            // never cache data
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  // versioned (?v=hash) assets never change under the same URL: cache-first, and drop the older copies
  if (url.pathname.includes('/static/') && url.searchParams.has('v')) {
    event.respondWith(caches.open(CACHE).then(async cache => {
      const hit = await cache.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) {
        for (const old of await cache.keys()) if (new URL(old.url).pathname === url.pathname) await cache.delete(old);
        cache.put(req, res.clone());
      }
      return res;
    }));
  }
});
