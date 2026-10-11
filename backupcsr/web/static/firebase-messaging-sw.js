/* Service worker de FCM (Firebase Cloud Messaging) para CortexDev.
 * No cachea recursos: solo inicializa Firebase para que el navegador muestre
 * las notificaciones en segundo plano (el servidor envía payload `notification`).
 *
 * El SDK se carga de forma SÍNCRONA (importScripts a nivel superior) y la config
 * se lee de /api/push/fcm-config, que es PÚBLICO: el SW no debe depender de un
 * endpoint con login (si falla, quedaría inerte y no mostraría nada). */
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });

var SDK = 'https://www.gstatic.com/firebasejs/10.12.2/';

try {
  importScripts(SDK + 'firebase-app-compat.js');
  importScripts(SDK + 'firebase-messaging-compat.js');
} catch (err) {
  console.error('[fcm-sw] no se pudo cargar el SDK de Firebase:', err);
}

fetch('/api/push/fcm-config', { credentials: 'same-origin' })
  .then(function (res) { return res.ok ? res.json() : null; })
  .then(function (cfg) {
    if (!cfg || !cfg.firebase || !cfg.firebase.apiKey) {
      console.error('[fcm-sw] configuración FCM no disponible:', cfg);
      return;
    }
    if (typeof firebase === 'undefined' || !firebase.apps) {
      console.error('[fcm-sw] SDK de Firebase no cargado');
      return;
    }
    if (!firebase.apps.length) firebase.initializeApp(cfg.firebase);
    firebase.messaging();
    console.info('[fcm-sw] Firebase Messaging inicializado');
  })
  .catch(function (err) {
    console.error('[fcm-sw] error inicializando FCM:', err);
  });
