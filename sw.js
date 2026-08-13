// sw.js — 오프라인 캐시. 한 번 열어본 뒤로는 망이 없어도 그대로 뜹니다.
//
// 파일을 고쳤다면 VERSION 을 올리세요. 올리지 않아도 아래의 갱신 규칙(캐시로
// 먼저 응답하고 뒤에서 새 파일을 받아두기) 덕분에 다음번 방문 때 새것으로
// 바뀌지만, VERSION 을 올리면 그 자리에서 통째로 갈립니다.
const VERSION = "kinescope-v1";

const ASSETS = [
  "./",
  "./index.html",
  "./app.js",
  "./manifest.webmanifest",
  "./icon.png",
  "./lib/worker.js",
  "./lib/lt.js",
  "./vendor/fflate.js",
  "./vendor/zxing/share.js",
  "./vendor/zxing/reader/index.js",
  "./vendor/zxing/reader/zxing_reader.wasm",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => c.addAll(ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// 캐시로 먼저 응답하고, 뒤에서 조용히 새 파일을 받아둡니다.
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  e.respondWith((async () => {
    const cache = await caches.open(VERSION);
    const hit = await cache.match(req, { ignoreSearch: true });

    const fresh = fetch(req).then((res) => {
      if (res && res.ok && res.type === "basic") cache.put(req, res.clone());
      return res;
    }).catch(() => null);

    if (hit) return hit;

    const res = await fresh;
    if (res) return res;

    // 망도 캐시도 없을 때, 화면 이동이라면 첫 페이지라도 돌려줍니다.
    if (req.mode === "navigate") {
      const index = await cache.match("./index.html");
      if (index) return index;
    }
    return Response.error();
  })());
});
