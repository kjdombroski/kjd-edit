// KJD Edit offline cache: network-first for the app page (gets updates when online), cache fallback offline.
const CACHE="kjd-edit-v0.8.1";
const FILES=["./","index.html","excerpts.html","manifest.webmanifest","icon-180.png","icon-192.png","icon-512.png","flac/libflac.min.wasm.js","flac/libflac.min.wasm.wasm","piano/A1.mp3","piano/A2.mp3","piano/A3.mp3","piano/A4.mp3","piano/A5.mp3","piano/A6.mp3","piano/A7.mp3","piano/C1.mp3","piano/C2.mp3","piano/C3.mp3","piano/C4.mp3","piano/C5.mp3","piano/C6.mp3","piano/C7.mp3","piano/C8.mp3","piano/Ds1.mp3","piano/Ds2.mp3","piano/Ds3.mp3","piano/Ds4.mp3","piano/Ds5.mp3","piano/Ds6.mp3","piano/Ds7.mp3","piano/Fs1.mp3","piano/Fs2.mp3","piano/Fs3.mp3","piano/Fs4.mp3","piano/Fs5.mp3","piano/Fs6.mp3","piano/Fs7.mp3"];
self.addEventListener("install",e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES)).then(()=>self.skipWaiting()))});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener("fetch",e=>{const r=e.request;if(r.method!=="GET"||new URL(r.url).origin!==location.origin)return;
  e.respondWith(fetch(r,{cache:"no-cache"}).then(res=>{if(res.ok){const cp=res.clone();caches.open(CACHE).then(c=>c.put(r,cp))}return res})
    .catch(()=>caches.match(r,{ignoreSearch:true}).then(m=>m||caches.match("index.html"))))});
