/* RentalAdmin service worker
   - API requests: never cached (financial data must be live)
   - Local pages/static: network-first with cache fallback (offline shell)
   - CDN assets (Tailwind/Alpine/FontAwesome): stale-while-revalidate
*/
const VERSION = 'v28-20260828';
const PAGE_CACHE = `ra-page-${VERSION}`;
const STATIC_CACHE = `ra-static-${VERSION}`;
const CDN_HOSTS = ['cdn.tailwindcss.com', 'cdn.jsdelivr.net', 'cdnjs.cloudflare.com'];

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(PAGE_CACHE).then((c) => c.add('/')).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((k) => k !== PAGE_CACHE && k !== STATIC_CACHE).map((k) => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (e) => {
    const req = e.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);

    if (url.origin === self.location.origin) {
        // Live data only — no caching for API/docs
        if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/docs') || url.pathname.startsWith('/openapi.json')) {
            e.respondWith(fetch(req));
            return;
        }
        // App shell & local static: network-first, cache fallback
        e.respondWith(
            fetch(req).then((res) => {
                if (res && res.ok) {
                    const copy = res.clone();
                    caches.open(PAGE_CACHE).then((c) => c.put(req, copy));
                }
                return res;
            }).catch(() =>
                caches.match(req).then((hit) =>
                    hit || (req.mode === 'navigate' ? caches.match('/') : Response.error())
                )
            )
        );
        return;
    }

    if (CDN_HOSTS.includes(url.hostname)) {
        // Stale-while-revalidate for CDN libs/fonts
        e.respondWith(
            caches.open(STATIC_CACHE).then(async (cache) => {
                const hit = await cache.match(req);
                const net = fetch(req).then((res) => {
                    if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone());
                    return res;
                }).catch(() => null);
                return hit || net.then((r) => r || Response.error());
            })
        );
    }
    // other cross-origin: browser default
});
