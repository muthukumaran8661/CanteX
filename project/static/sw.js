/* ====================================================================
   Smart Canteen – Service Worker
   Provides robust PWA offline shell caching, safe cache versioning,
   and automatic stale cache invalidation upon deployment.
   
   CACHE STRATEGY:
   - HTML / Navigations: Network-First with Offline Cache Fallback.
     Ensures live online users never see stale HTML shells.
   - JS / CSS: Network-First with Cache Fallback.
     Guarantees updated styles and scripts load immediately.
   - Static Media (Images/Icons): Cache-First with Network Fallback.
     Loads instantly; pruned and re-cached on new SW version.
   
   DOES NOT cache:
   - /api/* (orders, payments, tokens, auth, recommendations, queue — always live)
   - /events (real-time SSE streams)
   - /admin, /admin/login, /staff (management portals)
   - Any non-GET requests
   ==================================================================== */

const CACHE_VERSION = 'v2.1.0';
const CACHE_NAME = `smart-canteen-${CACHE_VERSION}`;

// Pre-cached static assets shell
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable.png',
  '/static/images/app_logo.svg',
  '/static/images/gpay_logo.svg',
  '/static/images/phonepe_logo.svg',
  '/static/images/upi_logo.svg',
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
];

// URLs that must NEVER be cached (always live from server)
const NEVER_CACHE = [
  '/api/',
  '/events',
  '/admin',
  '/admin/login',
  '/staff',
];

// ── Install: Pre-cache Static Shell ──────────────────────────────────────
self.addEventListener('install', (event) => {
  console.log(`[SW] Installing Smart Canteen service worker (${CACHE_VERSION})`);
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return Promise.allSettled(
        STATIC_ASSETS.map((url) => {
          // Use cache: 'reload' so pre-caching fetches latest deployed files from server
          const req = new Request(url, { cache: 'reload' });
          return fetch(req).then((response) => {
            if (response && response.status === 200) {
              return cache.put(url, response);
            }
          }).catch((err) => {
            console.warn('[SW] Pre-cache asset skipped:', url, err);
          });
        })
      );
    })
  );
  // NOTE: We deliberately do NOT call self.skipWaiting() here on updates,
  // so existing tabs can display "New version available" and prompt the user.
  // When user clicks "Update" (or SKIP_WAITING is sent), it will activate.
});

// ── Activate: Clean Up Outdated Caches Safely ────────────────────────────
self.addEventListener('activate', (event) => {
  console.log(`[SW] Activating Smart Canteen service worker (${CACHE_VERSION})`);
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((name) => {
          // Only delete outdated Smart Canteen caches; never touch unrelated caches
          if (name.startsWith('smart-canteen-') && name !== CACHE_NAME) {
            console.log('[SW] Purging outdated cache:', name);
            return caches.delete(name);
          }
        })
      );
    }).then(() => {
      console.log(`[SW] Clients claimed by ${CACHE_VERSION}`);
      return self.clients.claim();
    })
  );
});

// ── Message: Communication with Client Pages ─────────────────────────────
self.addEventListener('message', (event) => {
  if (!event.data) return;

  if (event.data.type === 'SKIP_WAITING') {
    console.log('[SW] Received SKIP_WAITING command. Activating immediately...');
    self.skipWaiting();
  } else if (event.data.type === 'GET_VERSION' && event.ports && event.ports[0]) {
    event.ports[0].postMessage({
      version: CACHE_VERSION,
      cacheName: CACHE_NAME,
    });
  }
});

// ── Fetch: Strategic Caching & Live Data Protection ───────────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 1. Only intercept GET requests
  if (event.request.method !== 'GET') {
    return; // Pass through to browser
  }

  // 2. Never cache dynamic APIs, SSE streams, auth, or admin/staff dashboards
  const isNeverCache = NEVER_CACHE.some((pattern) => url.pathname.startsWith(pattern)) ||
                       url.pathname.includes('/events');
  if (isNeverCache) {
    return; // Browser network only
  }

  // 3. HTML / Navigation requests: Network-First with Offline Fallback
  // Guarantees online users always get the freshest deployment shell.
  if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html' || event.request.destination === 'document') {
    event.respondWith(
      fetch(event.request)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const copy = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return networkResponse;
        })
        .catch(async () => {
          // Offline fallback
          const cache = await caches.open(CACHE_NAME);
          const cached = await cache.match(event.request) ||
                         await cache.match('/') ||
                         await cache.match('/index.html');
          return cached || new Response('Offline - Smart Canteen is currently unavailable.', {
            status: 503,
            headers: { 'Content-Type': 'text/plain' },
          });
        })
    );
    return;
  }

  // 4. JavaScript and CSS: Network-First with Cache Fallback
  // Ensures updated code is received immediately, falling back to cache if offline.
  if (url.pathname.endsWith('.js') || url.pathname.endsWith('.css')) {
    event.respondWith(
      fetch(event.request)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const copy = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return networkResponse;
        })
        .catch(async () => {
          const cache = await caches.open(CACHE_NAME);
          return await cache.match(event.request, { ignoreSearch: true });
        })
    );
    return;
  }

  // 5. Static Images & Icons: Cache-First with Network Fallback
  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      const cached = await cache.match(event.request, { ignoreSearch: true });
      if (cached) {
        return cached;
      }
      try {
        const networkResponse = await fetch(event.request);
        if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
          cache.put(event.request, networkResponse.clone());
        }
        return networkResponse;
      } catch (err) {
        return cached;
      }
    })
  );
});
