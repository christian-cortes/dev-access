/* Cliente de notificaciones push (FCM web) para las apps de CortexDev.
 *
 * Se activa donde exista <div id="push-settings" data-area="copias|index|ups">.
 * Opcionalmente acepta data-scopes-url (lista ligera de proyectos/tareas) y
 * data-scopes-label (proyecto|tarea) para configurar el on/off de cada uno.
 * Flujo: pedir permiso, registrar el service worker, obtener el token FCM,
 * enviarlo a /api/push/subscribe y configurar eventos/severidad/silencio del
 * área y, si aplica, los avisos por proyecto/tarea. Requiere sesión (la cookie
 * SSO viaja al proxy).
 */
(function () {
  'use strict';

  var FIREBASE_SDK = 'https://www.gstatic.com/firebasejs/10.12.2/';
  var host = document.getElementById('push-settings');
  if (!host) return;

  var area = host.getAttribute('data-area') || '';
  var scopesUrl = host.getAttribute('data-scopes-url') || '';
  var scopeLabel = host.getAttribute('data-scopes-label') || 'proyecto';
  var TOKEN_KEY = 'cortexpush.token.' + area;

  var state = {
    config: null, messaging: null, swReg: null, token: null, prefs: null,
    scopes: null, scopePrefs: {}, scopeFilter: '',
  };

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function api(path, options) {
    return fetch('/api/push' + path, Object.assign({ credentials: 'same-origin' }, options || {}));
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.indexOf('on') === 0) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    });
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function render() {
    host.textContent = '';
    if (!state.config) {
      host.appendChild(el('p', { class: 'push-note', text: 'No se pudieron leer las notificaciones (revisa la sesión).' }));
      return;
    }
    var supported = 'Notification' in window && 'serviceWorker' in navigator && 'PushManager' in window;
    var note = el('p', { class: 'push-note' });
    var actions = el('div', { class: 'push-actions' });

    if (!supported) {
      note.textContent = 'Este navegador no soporta notificaciones push.';
      host.appendChild(note);
      return;
    }

    var perm = Notification.permission;
    note.textContent = state.token
      ? 'Dispositivo activado para recibir avisos.'
      : (perm === 'denied'
        ? 'Permiso de notificaciones denegado. Restablécelo en los ajustes del sitio (icono del candado → Notificaciones → Permitir) para poder activarlas.'
        : 'Activa las notificaciones para recibir avisos en este dispositivo.');

    if (!state.token && perm !== 'denied') {
      actions.appendChild(el('button', {
        class: 'push-btn primary', type: 'button', text: 'Activar notificaciones',
        onclick: enable,
      }));
    }
    if (state.token) {
      actions.appendChild(el('button', {
        class: 'push-btn', type: 'button', text: 'Enviar prueba', onclick: test,
      }));
      actions.appendChild(el('button', {
        class: 'push-btn', type: 'button', text: 'Desactivar en este dispositivo', onclick: disable,
      }));
    }
    host.appendChild(note);
    host.appendChild(actions);

    if (state.prefs) host.appendChild(prefsForm(state.prefs));
    var meta = areaInfo();
    if (state.scopes && state.scopes.length && meta && meta.scoped === true) {
      host.appendChild(scopeForm());
    }
  }

  function prefsForm(prefs) {
    var box = el('div', { class: 'push-prefs' });
    var meta = areaInfo();
    if (!meta) return box;

    box.appendChild(el('label', { class: 'push-row switch' }, [
      el('input', {
        type: 'checkbox', checked: prefs.enabled !== false,
        onchange: function (e) { prefs.enabled = e.target.checked; savePrefs(); },
      }),
      el('span', { text: 'Recibir avisos de ' + meta.title }),
    ]));

    var list = el('div', { class: 'push-events' });
    meta.events.forEach(function (ev) {
      var checked = prefs.events && prefs.events[ev.key] !== false;
      list.appendChild(el('label', { class: 'push-row switch' }, [
        el('input', {
          type: 'checkbox', checked: checked,
          onchange: function (e) { prefs.events = prefs.events || {}; prefs.events[ev.key] = e.target.checked; savePrefs(); },
        }),
        el('span', { text: ev.title }),
      ]));
    });
    box.appendChild(list);

    var levels = ['info', 'warning', 'critical'];
    var select = el('select', {
      class: 'push-select',
      onchange: function (e) { prefs.min_level = e.target.value; savePrefs(); },
    });
    levels.forEach(function (lv) {
      select.appendChild(el('option', { value: lv, text: lv, selected: prefs.min_level === lv }));
    });
    box.appendChild(el('div', { class: 'push-row' }, [
      el('span', { text: 'Severidad mínima' }), select,
    ]));

    var quiet = prefs.quiet || {};
    box.appendChild(el('label', { class: 'push-row switch' }, [
      el('input', {
        type: 'checkbox', checked: !!quiet.enabled,
        onchange: function (e) { prefs.quiet = prefs.quiet || {}; prefs.quiet.enabled = e.target.checked; savePrefs(); },
      }),
      el('span', { text: 'No molestar de noche' }),
    ]));

    return box;
  }

  function scopeForm() {
    var box = el('div', { class: 'push-scopes' });
    box.appendChild(el('h3', { class: 'push-scopes-title', text: 'Por ' + scopeLabel }));
    box.appendChild(el('p', {
      class: 'push-note',
      text: 'Elige qué ' + scopeLabel + 's quieres recibir. Los demás ajustes se heredan del área.',
    }));

    var tools = el('div', { class: 'push-scopes-tools' });
    tools.appendChild(el('input', {
      class: 'push-search', type: 'search', value: state.scopeFilter,
      placeholder: 'Buscar ' + scopeLabel + '…',
      oninput: function (e) { state.scopeFilter = e.target.value.toLowerCase(); renderScopeList(listBox); },
    }));
    tools.appendChild(el('button', { class: 'push-btn', type: 'button', text: 'Todos', onclick: function () { setAllScopes(true); } }));
    tools.appendChild(el('button', { class: 'push-btn', type: 'button', text: 'Ninguno', onclick: function () { setAllScopes(false); } }));
    box.appendChild(tools);

    var listBox = el('div', { class: 'push-scopes-list' });
    box.appendChild(listBox);
    renderScopeList(listBox);
    return box;
  }

  function renderScopeList(listBox) {
    listBox.textContent = '';
    var filter = state.scopeFilter;
    var shown = 0;
    var lastGroup = null;
    (state.scopes || []).forEach(function (item) {
      var label = item.name || item.slug;
      if (filter && (item.slug + ' ' + label).toLowerCase().indexOf(filter) < 0) return;
      if (item.group_title && item.group !== lastGroup) {
        listBox.appendChild(el('div', { class: 'push-scopes-group', text: item.group_title }));
        lastGroup = item.group;
      }
      listBox.appendChild(el('label', { class: 'push-row switch' }, [
        el('input', {
          type: 'checkbox', checked: state.scopePrefs[item.slug] !== false,
          onchange: function (e) {
            var previous = state.scopePrefs[item.slug];
            state.scopePrefs[item.slug] = e.target.checked;
            saveScopes(item.slug, e.target.checked, previous);
          },
        }),
        el('span', { text: label }),
      ]));
      shown += 1;
    });
    if (!shown) listBox.appendChild(el('p', { class: 'push-note', text: 'Sin resultados.' }));
  }

  function setAllScopes(value) {
    var snapshot = Object.assign({}, state.scopePrefs);
    var batch = [];
    (state.scopes || []).forEach(function (item) {
      state.scopePrefs[item.slug] = value;
      batch.push({ scope: item.slug, enabled: value });
    });
    render();
    saveScopesBatch(batch, function () { state.scopePrefs = snapshot; render(); });
  }

  function saveScopes(scope, enabled, previous) {
    return saveScopesBatch([{ scope: scope, enabled: enabled }], function () {
      state.scopePrefs[scope] = previous;
      render();
    });
  }

  async function saveScopesBatch(batch, onError) {
    if (!batch.length) return;
    try {
      var res = await api('/scopes', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ area: area, scopes: batch }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
    } catch (err) {
      console.error('[push] no se pudieron guardar los ' + scopeLabel + 's:', err);
      if (onError) onError();
      window.alert('No se pudieron guardar los cambios de ' + scopeLabel + 's.');
    }
  }

  function areaInfo() {
    if (!state.config) return null;
    for (var i = 0; i < state.config.areas.length; i++) {
      if (state.config.areas[i].slug === area) return state.config.areas[i];
    }
    return null;
  }

  function setBusy(on) {
    host.querySelectorAll('button').forEach(function (b) { b.disabled = on; });
  }

  async function enable() {
    setBusy(true);
    try {
      if (!state.config.firebase || !state.config.firebase.apiKey) {
        throw new Error('FCM no está configurado en el servidor.');
      }
      var perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error('permiso no concedido');
      state.swReg = await navigator.serviceWorker.register('/firebase-messaging-sw.js', { scope: '/' });
      if (!window.firebase) {
        await loadScript(FIREBASE_SDK + 'firebase-app-compat.js');
        await loadScript(FIREBASE_SDK + 'firebase-messaging-compat.js');
      }
      if (!window.firebase.apps.length) window.firebase.initializeApp(state.config.firebase);
      var messaging = window.firebase.messaging();
      var token = await messaging.getToken({
        vapidKey: state.config.vapid_key,
        serviceWorkerRegistration: state.swReg,
      });
      if (!token) throw new Error('no se obtuvo el token FCM');
      var res = await api('/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ area: area, token: token, platform: navigator.platform || '' }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      state.token = token;
      try { localStorage.setItem(TOKEN_KEY, JSON.stringify({ token: token, id: data.id })); } catch (e) {}
      render();
    } catch (err) {
      console.error('[push] no se pudieron activar las notificaciones:', err);
      window.alert('No se pudieron activar las notificaciones: ' + err.message);
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    try {
      var stored = readStored();
      if (state.token) {
        await window.firebase.messaging().deleteToken();
      }
      if (stored && stored.id) {
        await api('/unsubscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: stored.id }),
        });
      }
      state.token = null;
      try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
      render();
    } catch (err) {
      window.alert('No se pudieron desactivar: ' + err.message);
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    try {
      var res = await api('/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ area: area }),
      });
      var data = await res.json();
      if (!data.sent) window.alert(data.fcm_ready ? 'No hay dispositivos suscritos.' : 'FCM no está configurado en el servidor.');
    } catch (err) {
      window.alert('No se pudo enviar la prueba: ' + err.message);
    } finally {
      setBusy(false);
    }
  }

  async function savePrefs() {
    try {
      await api('/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ area: area, prefs: state.prefs }),
      });
    } catch (err) {
      /* el siguiente render recarga del servidor */
    }
  }

  function readStored() {
    try { return JSON.parse(localStorage.getItem(TOKEN_KEY) || 'null'); }
    catch (e) { return null; }
  }

  async function boot() {
    try {
      var configRes = await api('/config');
      if (configRes.status === 401) { render(); return; }
      state.config = await configRes.json();
    } catch (err) {
      console.error('[push] no se pudo leer la configuración:', err);
      host.appendChild(el('p', { class: 'push-note', text: 'No se pudo contactar el servicio de notificaciones.' }));
      return;
    }
    var stored = readStored();
    if (stored && stored.token) state.token = stored.token;
    try {
      var prefsRes = await api('/preferences');
      if (prefsRes.ok) {
        var prefsData = await prefsRes.json();
        state.prefs = prefsData.prefs[area] || null;
        state.scopePrefs = (prefsData.scopes && prefsData.scopes[area]) || {};
      }
    } catch (err) { /* sin prefs */ }
    if (scopesUrl) {
      try {
        var scopesRes = await fetch(scopesUrl, { credentials: 'same-origin' });
        if (scopesRes.ok) {
          var scopesData = await scopesRes.json();
          state.scopes = scopesData.scopes || [];
        }
      } catch (err) {
        console.error('[push] no se pudo leer la lista de ' + scopeLabel + 's:', err);
      }
    }
    render();
  }

  boot();
})();
