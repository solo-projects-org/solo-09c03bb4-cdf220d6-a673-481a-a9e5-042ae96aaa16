/* Offline shell cache for the river sampling app.
 *
 * App shell: cache-first with background revalidation so a field worker can
 * reopen the page with no network.
 * API: never cached. Sampling records live in IndexedDB and uploads must hit
 * the warehouse (with idempotent retries), never a stale cached response.
 */
const VERSION = "river-sampling-v2";
const SHELL = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/static/manifest.webmanifest",
  "/static/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put("/", copy));
          return response;
        })
        .catch(() =>
          caches.match("/").then(
            (cached) =>
              cached ||
              new Response("离线缓存尚未就绪，请先在联网状态打开一次。", {
                status: 503,
                headers: { "Content-Type": "text/plain; charset=utf-8" },
              })
          )
        )
    );
    return;
  }

  // Assets: network-first so a redeploy is picked up, cache fallback so the
  // field worker can still open the app with no network.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request))
      .then(
        (response) =>
          response ||
          new Response("离线缓存中缺少该资源，请先联网打开一次。", {
            status: 503,
            headers: { "Content-Type": "text/plain; charset=utf-8" },
          })
      )
  );
});
