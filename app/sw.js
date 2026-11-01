// Offline-Unterstützung: App-Hülle aus dem Cache, Artikelliste immer frisch (mit Offline-Fallback).
const SHELL = "surplus-shell-v1";
const DATA = "surplus-data-v1";
const FILES = ["./", "index.html", "manifest.webmanifest", "icons/icon-180.png", "icons/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.endsWith("/data/articles.json")) {
    e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(DATA).then(x => x.put("articles", c)); return r; })
      .catch(() => caches.open(DATA).then(x => x.match("articles"))));
    return;
  }
  // App-Hülle: erst Netz (damit Updates ankommen), sonst Cache
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(x => x.put(e.request, c)); return r; })
    .catch(() => caches.match(e.request).then(m => m || caches.match("index.html"))));
});
