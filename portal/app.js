(function () {
  'use strict';

  function initials(text) {
    var words = text.trim().split(/\s+/).filter(function (w) { return w.length > 2; });
    if (words.length === 0) words = text.trim().split(/\s+/);
    if (words.length === 1) {
      return words[0].replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase();
    }
    return words.slice(0, 2).map(function (w) { return w.charAt(0); }).join('').toUpperCase();
  }

  function badge(text, color) {
    var el = document.createElement('span');
    el.className = 'app-badge';
    el.style.setProperty('--c', color || '#334155');
    el.textContent = initials(text);
    return el;
  }

  function techImg(name, cls, title) {
    var img = document.createElement('img');
    img.src = '/icons/' + name + '.svg';
    img.alt = title || name;
    img.title = title || name;
    img.className = cls;
    img.loading = 'lazy';
    return img;
  }

  function appImg(file, label, color) {
    var img = document.createElement('img');
    img.className = 'app-icon';
    img.src = '/favicons/' + file;
    img.alt = label;
    img.title = label;
    img.loading = 'lazy';
    img.onerror = function () {
      var b = badge(label, color);
      if (this.parentNode) this.parentNode.replaceChild(b, this);
    };
    return img;
  }

  var names = {
    laravel: 'Laravel', codeigniter: 'CodeIgniter', php: 'PHP', angular: 'Angular',
    react: 'React', python: 'Python', mysql: 'MySQL', docker: 'Docker',
    proxmox: 'Proxmox', pihole: 'Pi-hole', netdata: 'Netdata', nginx: 'Nginx',
    phpmyadmin: 'phpMyAdmin', powerbi: 'Power BI',
    certificate: 'Certificado local',
    server: 'Infraestructura', linux: 'Linux', grafana: 'Grafana'
  };

  var BELL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>';
  var USER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';

  function currentTheme() {
    var root = document.documentElement;
    var theme = root.getAttribute('data-bs-theme') || root.getAttribute('data-theme');
    return theme === 'light' ? 'light' : 'dark';
  }

  function setTheme(next) {
    var root = document.documentElement;
    root.setAttribute('data-theme', next);
    root.setAttribute('data-bs-theme', next);
    try { localStorage.setItem('cortexdev-theme', next); } catch (e) {}
  }

  function toggleTheme() {
    setTheme(currentTheme() === 'light' ? 'dark' : 'light');
  }

  /* Etiquetas para el layout tipo tarjeta en movil (se repite tras cada render) */
  function addTableLabels() {
    document.querySelectorAll('table.links').forEach(function (table) {
      var ths = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) {
        return th.textContent.trim();
      });
      table.querySelectorAll('tbody tr').forEach(function (tr) {
        Array.prototype.forEach.call(tr.children, function (td, i) {
          if (ths[i]) td.setAttribute('data-th', ths[i]);
        });
      });
    });
  }

  function addCardIcons() {
    document.querySelectorAll('.card[data-tech]').forEach(function (card) {
      if (card.querySelector('.card-icons')) return;
      var box = document.createElement('div');
      box.className = 'card-icons';
      card.dataset.tech.split(',').forEach(function (t) {
        t = t.trim();
        if (t) box.appendChild(techImg(t, 'card-icon', names[t] || t));
      });
      var host = card.querySelector('.card-body') || card;
      host.insertBefore(box, host.firstChild);
    });
  }

  function addNameIcons() {
    document.querySelectorAll('td.name[data-tech]').forEach(function (td) {
      if (td.dataset.icons === 'done') return;
      td.dataset.icons = 'done';

      var label = td.dataset.label || td.textContent;
      var noBadge = td.dataset.badge === 'off';

      if (!noBadge && !td.querySelector('.app-badge, .app-icon')) {
        td.insertBefore(
          td.dataset.app ? appImg(td.dataset.app, label, td.dataset.color)
                         : badge(label, td.dataset.color),
          td.firstChild
        );
      }

      td.dataset.tech.split(',').forEach(function (t) {
        t = t.trim();
        if (!t) return;
        var img = techImg(t, noBadge ? 'tech-lg' : 'tech', names[t] || t);
        if (noBadge) {
          td.insertBefore(img, td.firstChild);
        } else {
          td.appendChild(img);
        }
      });
    });
  }

  /* --------------------------- barra del header ------------------------- */
  /* Agrupa la navegación y los controles (tema, campana, usuario, salir) en una
     misma barra a la derecha, en vez de quedar sueltos en el header. */
  function headerTools() {
    var header = document.querySelector('header.top');
    if (!header) return null;
    var tools = header.querySelector('.header-tools');
    if (!tools) {
      tools = document.createElement('div');
      tools.className = 'header-tools d-flex flex-wrap align-items-center gap-2';
      var nav = header.querySelector('nav.crumbs');
      if (nav) tools.appendChild(nav);
      header.appendChild(tools);
    }
    return tools;
  }

  /* Ajustes de notificaciones: el menú de usuario abre un modal con el
     componente #push-settings (el mismo que usa la página dedicada). */
  function loadPushClient() {
    if (document.querySelector('script[data-push-client]')) return;
    var s = document.createElement('script');
    s.src = '/push-client.js?v=5';
    s.defer = true;
    s.setAttribute('data-push-client', '1');
    document.head.appendChild(s);
  }

  function ensureNotifModal() {
    if (document.getElementById('notifModal')) return;
    var modal = document.createElement('div');
    modal.className = 'modal fade';
    modal.id = 'notifModal';
    modal.tabIndex = -1;
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML =
      '<div class="modal-dialog modal-dialog-centered">' +
      '<div class="modal-content">' +
      '<div class="modal-header"><h5 class="modal-title">Notificaciones</h5>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cerrar"></button></div>' +
      '<div class="modal-body">' +
      '<p class="small text-body-secondary mb-2">Activa este dispositivo y elige qué avisos del portal recibir.</p>' +
      '<div id="push-settings" data-area="index" data-scopes-url="/api/scopes" data-scopes-label="proyecto"></div>' +
      '</div></div></div>';
    document.body.appendChild(modal);
  }

  function openNotifModal() {
    // En la página dedicada ya existe #push-settings (sin modal inyectado): no hay nada que abrir.
    if (document.getElementById('push-settings') && !document.getElementById('notifModal')) return;
    ensureNotifModal();
    if (!window.bootstrap || !window.bootstrap.Modal) {
      location.href = '/notificaciones.html';
      return;
    }
    loadPushClient();
    window.bootstrap.Modal.getOrCreateInstance(document.getElementById('notifModal')).show();
  }

  /* --------------------------- menú de usuario --------------------------- */
  /* El login es único en el índice. El icono de usuario abre un desplegable con
     el nombre, el cambio de tema, los ajustes de notificaciones y el cierre de
     la sesión compartida (.cortexdev.win). */

  function addUserMenu() {
    var tools = headerTools();
    if (!tools || tools.querySelector('.user-menu')) return;
    var wrap = document.createElement('div');
    wrap.className = 'dropdown user-menu';
    wrap.innerHTML =
      '<button class="btn btn-outline-secondary rounded-circle user-toggle" type="button" id="user-toggle"' +
      ' data-bs-toggle="dropdown" aria-expanded="false" aria-label="Menú de usuario" title="Cuenta">' + USER + '</button>' +
      '<ul class="dropdown-menu dropdown-menu-end" aria-labelledby="user-toggle">' +
      '<li><span class="dropdown-item-text user-menu-name" id="user-name">Cuenta</span></li>' +
      '<li><hr class="dropdown-divider"></li>' +
      '<li><button class="dropdown-item" type="button" id="user-theme">Cambiar tema</button></li>' +
      '<li><button class="dropdown-item" type="button" id="user-settings">Ajustes</button></li>' +
      '<li><hr class="dropdown-divider"></li>' +
      '<li><button class="dropdown-item" type="button" id="user-logout">Cerrar sesión</button></li>' +
      '</ul>';
    tools.appendChild(wrap);

    wrap.querySelector('#user-theme').addEventListener('click', toggleTheme);
    wrap.querySelector('#user-settings').addEventListener('click', openNotifModal);
    var out = wrap.querySelector('#user-logout');
    out.addEventListener('click', function () {
      out.disabled = true;
      fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
        .catch(function () {})
        .then(function () { location.href = '/login.html'; });
    });

    fetch('/api/auth/me', { credentials: 'same-origin' })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (user) {
        var name = wrap.querySelector('#user-name');
        if (user && user.username) name.textContent = user.username;
      })
      .catch(function () {});
  }

  /* --------------------------- notificaciones --------------------------- */
  /* Problemas activos del catálogo (servicios caídos). El estado llega por el
     evento 'portal:statuses' que dispara render.js tras cada consulta. */

  var NOTIF_ACK_KEY = 'portal.notif.ack.v1';
  var notifActive = [];

  function notifAcked() {
    try { return new Set(JSON.parse(localStorage.getItem(NOTIF_ACK_KEY) || '[]')); }
    catch (e) { return new Set(); }
  }

  function notifSave(set) {
    try { localStorage.setItem(NOTIF_ACK_KEY, JSON.stringify(Array.from(set))); } catch (e) {}
  }

  function shortUrl(url) {
    return String(url).replace(/^https?:\/\//, '').replace(/\/$/, '') || url;
  }

  function fmtNotifTime(value) {
    var d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  function collectNotifications(services, statuses) {
    var out = [];
    (services || []).forEach(function (s) {
      var st = (statuses && statuses[s.slug]) || s.status || {};
      if (st.state !== 'offline') return;
      var details = [{ label: 'URL', value: shortUrl(s.url) }];
      if (st.code != null) details.push({ label: 'Código', value: String(st.code) });
      if (st.latency_ms != null) details.push({ label: 'Latencia', value: st.latency_ms + ' ms' });
      if (st.checked_at) details.push({ label: 'Último chequeo', value: new Date(st.checked_at).toLocaleString() });
      if (st.error) details.push({ label: 'Error', value: st.error });
      if (st.uptime_24h != null) details.push({ label: 'Uptime 24h', value: st.uptime_24h + '%' });
      if (s.category) details.push({ label: 'Categoría', value: s.category });
      out.push({
        id: 'index:service:' + s.slug + ':offline',
        severity: 'critical',
        title: s.name,
        summary: 'El servicio no responde',
        ts: st.checked_at || null,
        details: details,
        action: { label: 'Abrir servicio', url: s.url },
      });
    });
    return out;
  }

  function notifEl(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function notifDismiss(id) {
    var acked = notifAcked();
    acked.add(id);
    notifSave(acked);
    renderNotifications();
  }

  function notifRow(n) {
    var danger = n.severity === 'critical';
    var row = notifEl('div', 'notif ' + (danger ? 'notif-danger' : 'notif-warn'));

    var ico = notifEl('span', 'notif-ico');
    ico.innerHTML = BELL;
    row.appendChild(ico);

    var body = notifEl('div', 'notif-body');
    var head = notifEl('div', 'notif-head');
    head.appendChild(notifEl('div', 'notif-title', n.title));
    if (n.ts) head.appendChild(notifEl('span', 'notif-time', fmtNotifTime(n.ts)));
    var close = notifEl('button', 'notif-dismiss');
    close.type = 'button';
    close.title = 'Descartar';
    close.setAttribute('aria-label', 'Descartar aviso');
    close.innerHTML = '&times;';
    close.addEventListener('click', function () { notifDismiss(n.id); });
    head.appendChild(close);
    body.appendChild(head);
    body.appendChild(notifEl('div', 'notif-text', n.summary));

    if (n.details && n.details.length) {
      var box = notifEl('div', 'notif-detail');
      box.hidden = true;
      n.details.forEach(function (d) {
        var r = notifEl('div', 'notif-detail-row');
        r.appendChild(notifEl('span', 'notif-detail-label', d.label));
        r.appendChild(notifEl('span', 'notif-detail-value', d.value));
        box.appendChild(r);
      });
      var more = notifEl('button', 'notif-more', 'Ver detalle');
      more.type = 'button';
      more.addEventListener('click', function () {
        box.hidden = !box.hidden;
        more.textContent = box.hidden ? 'Ver detalle' : 'Ocultar detalle';
      });
      body.appendChild(more);
      body.appendChild(box);
    }

    if (n.action && n.action.url) {
      var link = notifEl('a', 'notif-action', n.action.label || 'Abrir');
      link.href = n.action.url;
      link.target = '_blank';
      link.rel = 'noopener';
      body.appendChild(link);
    }

    row.appendChild(body);
    row.appendChild(notifEl('span', 'notif-level', danger ? 'Crítico' : 'Aviso'));
    return row;
  }

  function renderNotifications() {
    var list = document.getElementById('notif-list');
    if (!list) return;
    var badge = document.getElementById('notif-count');
    var clearBtn = document.getElementById('notif-clear');

    var acked = notifAcked();
    var ids = new Set(notifActive.map(function (n) { return n.id; }));
    var pruned = false;
    Array.from(acked).forEach(function (id) { if (!ids.has(id)) { acked.delete(id); pruned = true; } });
    if (pruned) notifSave(acked);

    var shown = notifActive.filter(function (n) { return !acked.has(n.id); });
    var danger = shown.some(function (n) { return n.severity === 'critical'; });

    list.innerHTML = '';
    if (!shown.length) {
      list.appendChild(notifEl('div', 'notif-empty',
        notifActive.length ? 'Sin notificaciones pendientes.' : 'Sin notificaciones.'));
    } else {
      shown.forEach(function (n) { list.appendChild(notifRow(n)); });
    }

    if (badge) {
      badge.hidden = shown.length === 0;
      badge.textContent = shown.length > 99 ? '99+' : String(shown.length);
      badge.classList.toggle('is-danger', danger);
    }
    if (clearBtn) clearBtn.hidden = shown.length === 0;
  }

  function addNotificationsBell() {
    var tools = headerTools();
    if (!tools || document.getElementById('notif-toggle')) return;
    var wrap = document.createElement('div');
    wrap.className = 'dropdown notif-dropdown';
    wrap.innerHTML =
      '<button class="btn btn-outline-secondary rounded-circle theme-toggle position-relative" id="notif-toggle"' +
      ' type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false" aria-label="Ver notificaciones">' +
      BELL + '<span class="notif-badge" id="notif-count" hidden>0</span></button>' +
      '<div class="dropdown-menu dropdown-menu-end notif-menu" aria-labelledby="notif-toggle">' +
      '<div class="notif-menu-head"><span>Notificaciones</span>' +
      '<button type="button" class="notif-clear" id="notif-clear" hidden>Limpiar todo</button></div>' +
      '<div class="notif-menu-body" id="notif-list"></div></div>';
    tools.appendChild(wrap);
    var clearBtn = document.getElementById('notif-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        var acked = notifAcked();
        notifActive.forEach(function (n) { acked.add(n.id); });
        notifSave(acked);
        renderNotifications();
      });
    }
    renderNotifications();
  }

  function enhance() {
    addTableLabels();
    addCardIcons();
    addNameIcons();
    headerTools();
    addNotificationsBell();
    addUserMenu();
  }

  document.addEventListener('portal:rendered', enhance);
  document.addEventListener('portal:statuses', function (ev) {
    var detail = ev.detail || {};
    notifActive = collectNotifications(detail.services, detail.statuses);
    renderNotifications();
  });
  enhance();
})();
