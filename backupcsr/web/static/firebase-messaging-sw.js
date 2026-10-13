/* Service worker de notificaciones push de CortexDev (FCM web).
 *
 * Construye la notificación a partir del payload que envía el servicio notify
 * (title/body/icon/url en `data`, con `notification` como respaldo). No usa el
 * SDK de Firebase en segundo plano: así el título y el favicon son los del
 * sitio, en vez de que el auto-display del SDK use el origen como título.
 * El token FCM lo sigue obteniendo la página (push-client.js).
 */
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener('push', function (event) {
  var payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (err) { payload = {}; }
  var data = payload.data || {};
  var note = payload.notification || {};
  var title = data.title || note.title || 'CortexDev';
  var options = {
    body: data.body || note.body || '',
    data: { url: data.url || (payload.fcmOptions && payload.fcmOptions.link) || '/' }
  };
  var icon = data.icon || note.icon || '';
  if (icon) options.icon = icon;
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].url === url && 'focus' in list[i]) return list[i].focus();
    }
    return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
  }));
});
