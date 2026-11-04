/* Cache only the public, empty recorder shell. No API, photo or account data. */
const CACHE = 'noteshub-quick-v4-token-copy';
const SHELL = ['/quick','/quick?view=ocr','/static/app.css','/static/quick.css','/static/quick.js','/static/quick-ocr.js',
  '/static/quick.webmanifest','/static/quick-icon.svg'];
self.addEventListener('install',event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate',event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('noteshub-quick-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch',event => {
  const url = new URL(event.request.url);
  const cacheKey=url.pathname+(url.pathname==='/quick' && url.searchParams.get('view')==='ocr'?'?view=ocr':'');
  if (event.request.method !== 'GET' || url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && !response.redirected) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(cacheKey,copy)));
    }
    return response;
  }).catch(() => caches.match(cacheKey)));
});
