/* Minimal service worker for the Kassim Stock Move PWA.
 *
 * Its main job is to satisfy Chrome/Android's installability criteria so the
 * app installs as a real (WebAPK) app that launches standalone — without the
 * browser address bar. It also gives a small offline app-shell cache.
 *
 * NOTE: Android Chrome only registers a service worker over a *trusted* HTTPS
 * origin. A self-signed certificate accepted with a warning is NOT a secure
 * context, so registration (and therefore standalone install) will silently
 * fail until the certificate is trusted on the device. See README / cert steps.
 */
const CACHE = "zstm-shell-v1";
const CORE = [
  "./",
  "index.html",
  "manifest.webmanifest",
  "img/kassim-logo.png",
  "img/icons/icon-192.png",
  "img/icons/icon-512.png",
  "img/icons/apple-touch-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(CORE)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                 // never touch API POSTs
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return;      // API is always live

  // Network-first so the app and UI5 resources stay fresh; fall back to the
  // cache (and the app shell for navigations) when offline.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then(
          (hit) => hit || (req.mode === "navigate" ? caches.match("index.html") : undefined)
        )
      )
  );
});
