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

// 订阅可能被系统静默轮换（pushsubscriptionchange）。轮换发生时页面不一定开着，
// 所以开启通知时页面会把「公钥 + API Token」写进 IndexedDB，由 SW 自己重订阅并上报。
// 代价是同源 IndexedDB 里多存一份 Token，和 localStorage 处于同一信任级别。
var PUSH_DB = 'xiaoma-push';
var PUSH_STORE = 'kv';
var PUSH_CONFIG_KEY = 'config';

function pushDb() {
  return new Promise(function(resolve, reject) {
    var req = indexedDB.open(PUSH_DB, 1);
    req.onupgradeneeded = function() {
      var db = req.result;
      if (!db.objectStoreNames.contains(PUSH_STORE)) db.createObjectStore(PUSH_STORE);
    };
    req.onsuccess = function() { resolve(req.result); };
    req.onerror = function() { reject(req.error); };
  });
}

function pushDbGet(key) {
  return pushDb().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(PUSH_STORE, 'readonly');
      var req = tx.objectStore(PUSH_STORE).get(key);
      req.onsuccess = function() { resolve(req.result); };
      req.onerror = function() { reject(req.error); };
    });
  });
}

function pushDbSet(key, value) {
  return pushDb().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(PUSH_STORE, 'readwrite');
      tx.objectStore(PUSH_STORE).put(value, key);
      tx.oncomplete = function() { resolve(true); };
      tx.onerror = function() { reject(tx.error); };
    });
  });
}

function pushDbDelete(key) {
  return pushDb().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction(PUSH_STORE, 'readwrite');
      tx.objectStore(PUSH_STORE).delete(key);
      tx.oncomplete = function() { resolve(true); };
      tx.onerror = function() { reject(tx.error); };
    });
  });
}

function base64ToUint8Array(base64String) {
  var padding = '='.repeat((4 - base64String.length % 4) % 4);
  var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  var raw = atob(base64);
  var bytes = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function postSubscription(config, subscription, oldEndpoint) {
  var headers = {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + config.token
  };
  var json = subscription.toJSON();
  return fetch('/api/push/subscribe', {
    method: 'POST',
    headers: headers,
    body: JSON.stringify({
      endpoint: json.endpoint,
      keys: json.keys,
      user_agent: 'service-worker'
    })
  }).then(function() {
    // 新订阅生效后再删旧的，避免中间空窗
    if (!oldEndpoint || oldEndpoint === json.endpoint) return null;
    return fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({ endpoint: oldEndpoint })
    }).catch(function() { /* 旧订阅清不掉不影响新订阅 */ });
  });
}

self.addEventListener('pushsubscriptionchange', function(event) {
  event.waitUntil(
    pushDbGet(PUSH_CONFIG_KEY).then(function(config) {
      if (!config || !config.publicKey || !config.token) return null;
      var oldEndpoint = event.oldSubscription
        ? event.oldSubscription.endpoint
        : null;
      var next = event.newSubscription
        ? Promise.resolve(event.newSubscription)
        : self.registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: base64ToUint8Array(config.publicKey)
          });
      return next.then(function(subscription) {
        return postSubscription(config, subscription, oldEndpoint);
      });
    }).catch(function() { /* 没有配置或权限被撤，交给用户重新开启 */ })
  );
});

self.addEventListener('push', function(event) {
  var payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (e) {}

  var title = payload.title || '小马提醒';
  var options = {
    body: payload.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    data: {
      url: payload.url || '/',
      taskId: payload.task_id || 0,
      kind: payload.kind || ''
    },
    tag: payload.tag || 'xiaoma-reminder',
    renotify: true
  };
  // 正文存一份到 IndexedDB：点通知时页面靠它还原提醒内容，
  // taskId/kind 用来把下一轮对话绑定到这条任务上
  event.waitUntil(Promise.all([
    pushDbSet('last', {
      title: title,
      body: options.body,
      tag: options.tag,
      taskId: options.data.taskId,
      taskTitle: payload.task_title || '',
      kind: options.data.kind,
      at: Date.now()
    }).catch(function() { /* 存不了也能弹通知，只是点开看不到原文 */ }),
    self.registration.showNotification(title, options)
  ]));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  var data = event.notification.data || {};
  var target = data.url || '/';
  var taskId = data.taskId || 0;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(self.location.origin) === 0) {
          // 页面已经开着：直接把提醒内容（和任务）交给它渲染，不再新开窗口
          list[i].postMessage({ type: 'open-reminder', taskId: taskId });
          return list[i].focus();
        }
      }
      var sep = target.indexOf('?') === -1 ? '?' : '&';
      var extra = 'reminder=1' + (taskId ? ('&task=' + encodeURIComponent(taskId)) : '');
      return self.clients.openWindow(target + sep + extra);
    })
  );
});
