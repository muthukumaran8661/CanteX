/* ====================================================================
   Smart Canteen – Service Worker
   Provides PWA installation support and offline caching of safe
   static assets only.
   
   DOES NOT cache:
   - /api/* (payment, orders, tokens, auth — always live)
   - /static/data/* or any dynamic responses
   ==================================================================== */

const CACHE_NAME = 'smart-canteen-v2';
const CACHE_VERSION = 2;

// Only cache safe static assets
const STATIC_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.json',
  '/static/images/gpay_logo.svg',
  '/static/images/phonepe_logo.svg',
  '/static/images/upi_logo.svg',
  '/static/images/app_logo.svg',
  '/static/images/idli.jpg',
  '/static/images/masala_dosa.jpg',
  '/static/images/pongal.jpg',
  '/static/images/vada.jpg',
  '/static/images/poori_masala.jpg',
  '/static/images/sambar_rice.jpg',
  '/static/images/lemon_rice.jpg',
  '/static/images/curd_rice.jpg',
  '/static/images/tomato_rice.jpg',
  '/static/images/parotta.jpg',
  '/static/images/biriyani.jpg',
  '/static/images/veg_meals.jpg',
  '/static/images/chicken_65.jpg',
  '/static/images/tea.jpg',
  '/static/images/filter_coffee.jpg',
  '/static/images/lime_juice.jpg',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

// URLs that must NEVER be cached (always live from server)
const NEVER_CACHE = [
  '/api/',
  '/admin',
  '/admin/login',
  '/staff',
];

// ── Install: pre-cache static shell ──────────────────────────────────────
self.addEventListener('install', (event) => {
  console.log('[SW] Installing Smart Canteen service worker v' + CACHE_VERSION);
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // Cache one by one so a single failure doesn't break everything
      return Promise.allSettled(
        STATIC_ASSETS.map((url) =>
          cache.add(url).catch((err) =>
            console.warn('[SW] Failed to cache:', url, err)
          )
        )
      );
    }).then(() => self.skipWaiting())
  );
});

// ── Activate: clean up old caches ────────────────────────────────────────
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating Smart Canteen service worker');
  event.waitUntil(
    caches.keys().then((cacheNames) =>
      Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => {
            console.log('[SW] Deleting old cache:', name);
            return caches.delete(name);
          })
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: Network-first for API, Cache-first for static ─────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache these – always go to network
  const isNeverCache = NEVER_CACHE.some((path) =>
    url.pathname.startsWith(path)
  );

  // Only handle GET requests
  if (event.request.method !== 'GET' || isNeverCache) {
    return; // Let the browser handle it normally
  }

  // For API calls – network only, no cache fallback
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // For SSE streams – always network
  if (url.pathname.includes('/events')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // For static assets – cache first, then network, update cache
  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      const cached = await cache.match(event.request);
      const networkFetch = fetch(event.request)
        .then((response) => {
          // Only cache successful, same-origin responses
          if (
            response &&
            response.status === 200 &&
            response.type === 'basic'
          ) {
            cache.put(event.request, response.clone());
          }
          return response;
        })
        .catch(() => cached); // Offline fallback to cache

      // Return cached immediately if available, update in background
      return cached || networkFetch;
    })
  );
});

// ── Background sync: notify clients of updates ───────────────────────────
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
