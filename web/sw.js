/* 基金台账 · Service Worker
 *
 * 只缓存界面外壳（HTML / CSS / JS / 图标），账本数据一律走网络、不进缓存：
 * 数据在本机 SQLite 里，缓存一份旧数据反而容易让人看错数。
 *
 * 改完 web/ 下的文件后，把 VERSION 加 1，旧缓存会在下次打开时清掉。
 */

const VERSION = 'fund-ledger-v5';
const SHELL = [
  '/',
  '/web/style.css',
  '/web/app.js',
  '/web/manifest.webmanifest',
  '/web/icon.svg',
  '/web/icon-192.png',
  '/web/icon-512.png',
  '/web/icon-maskable-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // 账本数据永远请求服务端，避免展示过期数字
  if (url.pathname.startsWith('/api/')) return;

  // 页面：先网络，失败时回退到上一次缓存的界面
  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const res = await fetch(request);
        if (res && res.ok) {
          const cache = await caches.open(VERSION);
          cache.put('/', res.clone());
        }
        return res;
      } catch {
        return (await caches.match(request)) || (await caches.match('/')) || Response.error();
      }
    })());
    return;
  }

  // 静态资源：联网时优先拿最新的，断网再用缓存
  if (url.pathname.startsWith('/web/') || url.pathname === '/manifest.webmanifest') {
    event.respondWith((async () => {
      try {
        const res = await fetch(request);
        if (res && res.ok) {
          const cache = await caches.open(VERSION);
          cache.put(request, res.clone());
        }
        return res;
      } catch {
        const cached = await caches.match(request);
        return cached || Response.error();
      }
    })());
  }
});
