(function () {
  'use strict';

  var REFRESH_MS = 20000;
  var portal = null;
  var statuses = {};
  var timer = null;

  function fetchJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  function displayUrl(url) {
    var text = String(url).replace(/^https?:\/\//, '').replace(/\/$/, '');
    return text || url;
  }

  var PILLS = { online: 'ok', auth: 'login', offline: 'err', unknown: 'unknown' };

  function pillClass(state) {
    return PILLS[state] || 'unknown';
  }

  function pillText(st) {
    st = st || {};
    if (st.code) return String(st.code);
    return st.state === 'offline' ? 'off' : '\u2014';
  }

  function metaText(st) {
    st = st || {};
    var parts = [];
    if (st.latency_ms != null && st.state !== 'unknown') parts.push(st.latency_ms + 'ms');
    if (st.uptime_24h != null) parts.push(st.uptime_24h + '% 24h');
    return parts.join(' \u00b7 ');
  }

  function pill(st) {
    var el = document.createElement('span');
    el.className = 'badge rounded-pill pill-' + pillClass(st && st.state);
    el.textContent = pillText(st);
    if (st && st.error) el.title = st.error;
    return el;
  }

  function metaSpan(st) {
    var el = document.createElement('span');
    el.className = 'meta';
    el.textContent = metaText(st);
    return el;
  }

  function serviceRow(s) {
    var tr = document.createElement('tr');
    tr.setAttribute('data-slug', s.slug);

    var name = document.createElement('td');
    name.className = 'name';
    name.setAttribute('data-tech', (s.tech || []).join(','));
    if (s.icon) name.setAttribute('data-app', s.icon);
    if (s.color) name.setAttribute('data-color', s.color);
    if (!s.badge) name.setAttribute('data-badge', 'off');
    name.textContent = s.name;

    var url = document.createElement('td');
    url.className = 'url';
    var link = document.createElement('a');
    link.href = s.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = displayUrl(s.url);
    url.appendChild(link);

    var state = document.createElement('td');
    state.className = 'status';
    state.appendChild(pill(s.status));
    state.appendChild(metaSpan(s.status));

    var note = document.createElement('td');
    note.className = 'note';
    note.innerHTML = s.note || '\u2014';

    tr.appendChild(name);
    tr.appendChild(url);
    tr.appendChild(state);
    tr.appendChild(note);
    return tr;
  }

  function updateRow(tr, st) {
    var cell = tr.querySelector('td.status');
    if (!cell) return;
    cell.innerHTML = '';
    cell.appendChild(pill(st));
    cell.appendChild(metaSpan(st));
  }

  function cardIcon(s) {
    var img = document.createElement('img');
    img.className = 'app-icon card-app';
    img.src = '/favicons/' + s.icon;
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = function () {
      if (this.parentNode) this.parentNode.removeChild(this);
    };
    return img;
  }

  function headIcon(s) {
    if (s.icon) return cardIcon(s);
    var tech = (s.tech || [])[0];
    if (!tech) return null;
    var img = document.createElement('img');
    img.className = 'app-icon card-app';
    img.src = '/icons/' + tech + '.svg';
    img.alt = '';
    img.title = s.name;
    img.loading = 'lazy';
    img.onerror = function () {
      if (this.parentNode) this.parentNode.removeChild(this);
    };
    return img;
  }

  function card(s) {
    var col = document.createElement('div');
    col.className = 'col';

    var link = document.createElement('a');
    link.className = 'card h-100 text-body';
    link.href = s.url;
    link.target = '_blank';
    link.rel = 'noopener';

    var body = document.createElement('div');
    body.className = 'card-body';

    var head = document.createElement('div');
    head.className = 'card-head';
    var hi = headIcon(s);
    if (hi) head.appendChild(hi);

    var title = document.createElement('h3');
    title.className = 'h6 card-title';
    title.textContent = s.name;
    head.appendChild(title);
    body.appendChild(head);

    if (s.note) {
      var note = document.createElement('p');
      note.className = 'card-text small text-body-secondary mb-0';
      note.innerHTML = s.note;
      body.appendChild(note);
    }

    link.appendChild(body);
    col.appendChild(link);
    return col;
  }

  function byCategory(slug) {
    if (!portal) return [];
    return portal.services.filter(function (s) { return s.category === slug; });
  }

  function findCategory(slug) {
    if (!portal) return null;
    for (var i = 0; i < portal.categories.length; i++) {
      if (portal.categories[i].slug === slug) return portal.categories[i];
    }
    return null;
  }

  function renderTables() {
    document.querySelectorAll('table.links[data-category]').forEach(function (table) {
      var tbody = table.querySelector('tbody');
      if (!tbody) return;
      tbody.innerHTML = '';
      byCategory(table.getAttribute('data-category')).forEach(function (s) {
        tbody.appendChild(serviceRow(s));
      });
    });
  }

  function isExcluded(s, exclude) {
    var tech = s.tech || [];
    for (var i = 0; i < exclude.length; i++) {
      var token = exclude[i];
      if (!token) continue;
      if (s.slug === token || s.slug.indexOf(token + '-') === 0 || tech.indexOf(token) !== -1) {
        return true;
      }
    }
    return false;
  }

  function renderCardLists() {
    document.querySelectorAll('[data-cards]').forEach(function (box) {
      box.innerHTML = '';
      var exclude = (box.getAttribute('data-cards-exclude') || '')
        .split(',').map(function (t) { return t.trim(); }).filter(Boolean);
      var seen = {};
      box.getAttribute('data-cards').split(',').forEach(function (slug) {
        byCategory(slug.trim()).forEach(function (s) {
          if (seen[s.slug] || isExcluded(s, exclude)) return;
          seen[s.slug] = true;
          box.appendChild(card(s));
        });
      });
    });
  }

  function recomputeCategories() {
    if (!portal) return;
    portal.categories.forEach(function (cat) {
      var online = 0;
      var offline = 0;
      var unknown = 0;
      byCategory(cat.slug).forEach(function (s) {
        var st = statuses[s.slug] || s.status || {};
        if (st.state === 'online' || st.state === 'auth') online++;
        else if (st.state === 'offline') offline++;
        else unknown++;
      });
      cat.count = byCategory(cat.slug).length;
      cat.online = online;
      cat.offline = offline;
      cat.unknown = unknown;
      cat.state = offline === 0 ? 'ok' : (online > 0 ? 'degraded' : 'down');
    });
  }

  function renderCounts() {
    document.querySelectorAll('[data-count]').forEach(function (el) {
      el.textContent = byCategory(el.getAttribute('data-count')).length;
    });

    document.querySelectorAll('[data-category-card]').forEach(function (card) {
      var cat = findCategory(card.getAttribute('data-category-card'));
      var count = card.querySelector('.count');
      if (!cat || !count) return;
      count.textContent = cat.count + ' ' + cat.count_label + ' \u00b7 ' + cat.online + ' en l\u00ednea \u2192';
      count.setAttribute('data-state', cat.state);
    });
  }

  function renderBanner() {
    var wrap = document.querySelector('.wrap');
    if (!wrap) return null;
    var banner = document.getElementById('portal-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'portal-banner';
      banner.className = 'alert alert-warning banner';
      banner.hidden = true;
      wrap.insertBefore(banner, wrap.firstChild);
    }
    return banner;
  }

  function showError(message) {
    var banner = renderBanner();
    if (!banner) return;
    banner.textContent = message;
    banner.hidden = false;
  }

  function clearError() {
    var banner = document.getElementById('portal-banner');
    if (banner) banner.hidden = true;
  }

  function renderFreshness() {
    var footer = document.querySelector('footer.bottom');
    if (!footer) return;
    var el = document.getElementById('portal-freshness');
    if (!el) {
      el = document.createElement('span');
      el.id = 'portal-freshness';
      footer.appendChild(el);
    }
    el.textContent = 'estado actualizado ' + new Date().toLocaleTimeString();
  }

  function dispatchRendered() {
    document.dispatchEvent(new CustomEvent('portal:rendered'));
  }

  function applyStatuses(next) {
    statuses = next || {};
    if (portal) {
      portal.services.forEach(function (s) {
        if (statuses[s.slug]) s.status = statuses[s.slug];
      });
    }
    document.querySelectorAll('tr[data-slug]').forEach(function (tr) {
      var st = statuses[tr.getAttribute('data-slug')];
      if (st) updateRow(tr, st);
    });
    recomputeCategories();
    renderCounts();
    renderFreshness();
    dispatchRendered();
    document.dispatchEvent(new CustomEvent('portal:statuses', {
      detail: { statuses: statuses, services: portal ? portal.services : [] },
    }));
  }

  function loadStatuses(refresh) {
    return fetchJSON('/api/status' + (refresh ? '?refresh=1' : ''))
      .then(function (data) {
        clearError();
        applyStatuses(data.statuses);
      })
      .catch(function () {
        showError('No se pudo consultar el estado de los servicios (portal-api). Se reintenta\u2026');
      });
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      if (document.hidden) {
        schedule();
        return;
      }
      loadStatuses(false).then(schedule, schedule);
    }, REFRESH_MS);
  }

  function loadPortal() {
    return fetchJSON('/api/portal')
      .then(function (data) {
        portal = data;
        clearError();
        renderTables();
        renderCardLists();
        applyStatuses(statuses);
      })
      .catch(function () {
        showError('No se pudo cargar el cat\u00e1logo del portal (portal-api).');
      });
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) loadStatuses(false);
  });

  loadPortal().then(function () {
    return loadStatuses(true);
  }).then(schedule, schedule);
})();
