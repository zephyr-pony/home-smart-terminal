/* 小马 PWA Service Worker
 *
 * 目标：
 * - 让页面可安装到主屏幕（安卓 Chrome / iOS Safari）
 * - 断网或服务端重启时，至少能打开界面（API 仍会失败并提示）
 * - 为后续 Web Push 留好 push / notificationclick 入口
 *
 * 策略：页面导航用「网络优先，失败回退缓存」；静态资源缓存优先；
 * /api/ 一律走网络，不缓存（数据实时性优先）。
 */
var CACHE_NAME = 'xiaoma-shell-v3';
var SHELL = [
  '/',
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function(cache) { return cache.addAll(SHELL); })
      .then(function() { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys()
      .then(function(keys) {
        return Promise.all(keys.map(function(key) {
          if (key !== CACHE_NAME) return caches.delete(key);
        }));
      })
      .then(function() { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function(event) {
  var request = event.request;
  if (request.method !== 'GET') return;

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/api/') === 0) return;

  // 页面导航：优先拿最新的，离线时回退到缓存的壳
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then(function(response) {
          var copy = response.clone();
          caches.open(CACHE_NAME).then(function(cache) { cache.put('/', copy); });
          return response;
        })
        .catch(function() { return caches.match('/'); })
    );
    return;
  }

  // 其它静态资源：缓存优先，未命中再请求并写回
  event.respondWith(
    caches.match(request).then(function(hit) {
      if (hit) return hit;
      return fetch(request).then(function(response) {
        if (response && response.ok) {
          var copy = response.clone();
          caches.open(CACHE_NAME).then(function(cache) { cache.put(request, copy); });
        }
        return response;
      });
    })
  );
});

// ---- Web Push（后续接入：服务端推送提醒）----

self.addEventListener('push', function(event) {
  var payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (e) {}

  var title = payload.title || '小马提醒';
  var options = {
    body: payload.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    data: { url: payload.url || '/' },
    tag: payload.tag || 'xiaoma-reminder',
    renotify: true
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(self.location.origin) === 0) return list[i].focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
