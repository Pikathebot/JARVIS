/* The app shell, so the installed app opens (and says it can't reach the PC) while the laptop is
   asleep. Network first, so a new build shows on the next open; the API is never cached. */
const SHELL = "jarvis-m-v2";
const FILES = ["./", "index.html", "app.css", "app.js", "manifest.webmanifest", "icons/jarvis-mark.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
// Notifications (PLAN 4.19 phase 2): what Jarvis said while you were away from the PC
// (app/remote_push.py). Each push must show a notification -- browsers require it.
self.addEventListener("push", (e) => {
  let msg = {};
  try { msg = e.data ? e.data.json() : {}; } catch { msg = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(msg.title || "Jarvis", {
    body: msg.body || "",
    tag: msg.tag || "jarvis",           // a newer one of the same kind replaces the old
    renotify: true,
    icon: "icons/icon-192.png",
    badge: "icons/icon-192.png",
    data: { url: "./" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const open = wins.find((w) => new URL(w.url).pathname.startsWith("/m/"));
    return open ? open.focus() : self.clients.openWindow("./");
  }));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/m/")) return;
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }).then((hit) => hit || caches.match("index.html")))
  );
});
