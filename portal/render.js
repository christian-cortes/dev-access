(function () {
  'use strict';

  var DEFAULT_REFRESH_MS = 30000;
  var staticMode = document.documentElement.getAttribute('data-portal-mode') === 'static';
  var portal = null;
  var statuses = {};
  var timer = null;
  var nf = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 1 });

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
    if (st.state === 'online') return 'on';
    if (st.state === 'auth') return 'login';
    if (st.state === 'offline') return 'off';
    return '\u2014';
  }

  function fmtPct(value) {
    return value == null ? '\u2014' : nf.format(value) + '%';
  }

  function metaText(st) {
    st = st || {};
    var parts = [];
    if (st.latency_ms != null && st.state !== 'unknown') {
      parts.push(nf.format(st.latency_ms) + 'ms');
    }
    if (st.uptime_24h_pct != null) parts.push(fmtPct(st.uptime_24h_pct) + ' 24h');
    return parts.join(' \u00b7 ');
  }

  function statusTitle(st) {
    st = st || {};
    var parts = [];
    if (st.last_state) parts.push('último: ' + st.last_state);
    if (st.checked_at) parts.push('check: ' + st.checked_at);
    if (st.code != null) parts.push('código: ' + st.code);
    if (st.error) parts.push(st.error);
    return parts.join(' \u00b7 ');
  }

  function pill(st) {
    var el = document.createElement('span');
    el.className = 'badge rounded-pill pill-' + pillClass(st && st.state);
    el.textContent = pillText(st);
    var title = statusTitle(st);
    if (title) el.title = title;
    return el;
  }

  function metaSpan(st) {
    var el = document.createElement('span');
    el.className = 'meta';
    el.textContent = metaText(st);
    return el;
  }

  function renderNote(el, html) {
    el.textContent = '';
    if (!html) {
      el.textContent = '\u2014';
      return;
    }
    var doc = new DOMParser().parseFromString(String(html), 'text/html');
    var allowed = { A: true, CODE: true, STRONG: true, EM: true, BR: true };
    Array.prototype.forEach.call(doc.body.childNodes, function copy(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        el.appendChild(document.createTextNode(node.nodeValue));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || !allowed[node.tagName]) return;
      if (node.tagName === 'BR') {
        el.appendChild(document.createElement('br'));
        return;
      }
      var clone = document.createElement(node.tagName.toLowerCase());
      if (node.tagName === 'A') {
        var href = node.getAttribute('href') || '';
        if (!/^(https?:|mailto:)/i.test(href)) return;
        clone.setAttribute('href', href);
        clone.setAttribute('target', '_blank');
        clone.setAttribute('rel', 'noopener noreferrer');
      }
      Array.prototype.forEach.call(node.childNodes, function (child) {
        if (child.nodeType === Node.TEXT_NODE) {
          clone.appendChild(document.createTextNode(child.nodeValue));
        } else if (child.nodeType === Node.ELEMENT_NODE && allowed[child.tagName]) {
          var sub = document.createElement(child.tagName.toLowerCase());
          sub.textContent = child.textContent;
          clone.appendChild(sub);
        }
      });
      el.appendChild(clone);
    });
    if (!el.childNodes.length) el.textContent = '\u2014';
  }

  function serviceRow(s) {
    var tr = document.createElement('tr');
    tr.setAttribute('data-slug', s.slug);
    tr.setAttribute('data-category', s.category);

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
    renderNote(note, s.note);

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
    img.decoding = 'async';
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
    img.decoding = 'async';
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
      renderNote(note, s.note);
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

  function ensureCategory(slug) {
    if (!portal) return null;
    for (var i = 0; i < portal.categories.length; i++) {
      if (portal.categories[i].slug === slug) return portal.categories[i];
    }
    var created = { slug: slug, title: slug, count_label: 'servicios', count: 0 };
    portal.categories.push(created);
    return created;
  }

  function findCategory(slug) {
    if (!portal) return null;
    for (var i = 0; i < portal.categories.length; i++) {
      if (portal.categories[i].slug === slug) return portal.categories[i];
    }
    return ensureCategory(slug);
  }

  function renderTables() {
    document.querySelectorAll('table.links[data-category]').forEach(function (table) {
      var tbody = table.querySelector('tbody');
      if (!tbody) return;
      var labels = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) {
        return (th.textContent || '').trim();
      });
      tbody.innerHTML = '';
      byCategory(table.getAttribute('data-category')).forEach(function (s) {
        var tr = serviceRow(s);
        Array.prototype.forEach.call(tr.children, function (td, i) {
          if (labels[i]) td.setAttribute('data-th', labels[i]);
        });
        tbody.appendChild(tr);
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

  function hydrateFromDom() {
    var services = [];
    var categories = [];
    document.querySelectorAll('tr[data-slug], .card[data-slug]').forEach(function (item) {
      var slug = item.getAttribute('data-slug');
      var nameCell = item.querySelector('td.name');
      var link = item.querySelector('td.url a');
      var tech = (item.getAttribute('data-tech') || (nameCell && nameCell.getAttribute('data-tech')) || '')
        .split(',').filter(Boolean);
      services.push({
        slug: slug,
        name: item.getAttribute('data-name') || (nameCell ? nameCell.textContent.trim() : slug),
        category: item.getAttribute('data-category') || '',
        url: item.getAttribute('data-url') || (link ? link.href : item.href || '#'),
        tech: tech,
        icon: item.getAttribute('data-icon') || null,
        color: item.getAttribute('data-color') || null,
        badge: item.getAttribute('data-badge') !== 'off',
        status: {},
      });
    });
    document.querySelectorAll('[data-category-card]').forEach(function (cardEl) {
      var slug = cardEl.getAttribute('data-category-card');
      if (!findIn(categories, slug)) {
        categories.push({
          slug: slug,
          title: cardEl.getAttribute('data-category-title') || slug,
          count_label: cardEl.getAttribute('data-count-label') || 'servicios',
          count: 0,
        });
      }
    });
    services.forEach(function (s) {
      if (s.category && !findIn(categories, s.category)) {
        categories.push({ slug: s.category, title: s.category, count_label: 'servicios', count: 0 });
      }
    });
    portal = { services: services, categories: categories };
  }

  function findIn(list, slug) {
    for (var i = 0; i < list.length; i += 1) if (list[i].slug === slug) return list[i];
    return null;
  }

  function recomputeCategories() {
    if (!portal || !Object.keys(statuses).length) return;
    var counts = {};
    Object.keys(statuses).forEach(function (slug) {
      var st = statuses[slug] || {};
      var catSlug = st.category;
      if (!catSlug) return;
      if (!counts[catSlug]) counts[catSlug] = { count: 0, online: 0, offline: 0, unknown: 0 };
      counts[catSlug].count += 1;
      if (st.state === 'online' || st.state === 'auth') counts[catSlug].online += 1;
      else if (st.state === 'offline') counts[catSlug].offline += 1;
      else counts[catSlug].unknown += 1;
    });
    portal.categories.forEach(function (cat) {
      var data = counts[cat.slug];
      if (!data) {
        data = { count: byCategory(cat.slug).length, online: 0, offline: 0, unknown: 0 };
      }
      cat.count = data.count;
      cat.online = data.online;
      cat.offline = data.offline;
      cat.unknown = data.unknown;
      cat.state = cat.offline === 0 ? 'ok' : (cat.online > 0 ? 'degraded' : 'down');
    });
  }

  function renderCounts() {
    document.querySelectorAll('[data-count]').forEach(function (el) {
      el.textContent = byCategory(el.getAttribute('data-count')).length;
    });

    document.querySelectorAll('[data-category-card]').forEach(function (cardEl) {
      var cat = findCategory(cardEl.getAttribute('data-category-card'));
      var count = cardEl.querySelector('.count');
      if (!cat || !count) return;
      count.textContent = cat.count + ' ' + cat.count_label + ' \u00b7 ' + cat.online + ' en línea →';
      count.setAttribute('data-state', cat.state);
    });
  }

  function formatDuration(seconds) {
    if (seconds == null) return '\u2014';
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm';
    return nf.format(Math.round(seconds / 3600)) + 'h';
  }

  function setStat(name, value) {
    document.querySelectorAll('[data-stat="' + name + '"]').forEach(function (el) {
      el.textContent = value;
    });
  }

  function renderSparkline(series) {
    var box = document.getElementById('portal-sparkline');
    if (!box || !series || !series.length) return;
    box.textContent = '';
    var ns = 'http://www.w3.org/2000/svg';
    var width = 240;
    var height = 38;
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Disponibilidad de las últimas 24 horas');
    var bar = Math.max(2, Math.floor(width / series.length) - 2);
    series.forEach(function (point, index) {
      var rect = document.createElementNS(ns, 'rect');
      var pct = point.availability_pct;
      var h = pct == null ? 3 : Math.max(3, Math.round((pct / 100) * (height - 4)));
      rect.setAttribute('x', String(index * (bar + 2)));
      rect.setAttribute('y', String(height - h));
      rect.setAttribute('width', String(bar));
      rect.setAttribute('height', String(h));
      rect.setAttribute('rx', '1');
      rect.setAttribute('class', pct == null ? 'spark-empty' : (pct < 95 ? 'spark-warn' : 'spark-ok'));
      if (pct != null) rect.appendChild(document.createElementNS(ns, 'title')).textContent = pct + '%';
      svg.appendChild(rect);
    });
    box.appendChild(svg);
  }

  function renderStats(summary, series, windowEffective) {
    var section = document.getElementById('portal-stats');
    if (section) section.setAttribute('aria-busy', 'false');
    if (!summary) return;
    setStat('availability', fmtPct(summary.availability_pct));
    setStat('services', (summary.services_up || 0) + '/' + (summary.services_total || 0));
    setStat('latency', summary.avg_latency_ms == null ? '\u2014' : nf.format(summary.avg_latency_ms) + 'ms');
    setStat('incidents', summary.incidents_24h == null ? '\u2014' : String(summary.incidents_24h));
    var detail = document.getElementById('portal-stats-detail');
    if (detail) {
      var parts = [];
      if (windowEffective && windowEffective !== '24h') parts.push('datos: ' + windowEffective);
      parts.push('cobertura ' + fmtPct(summary.coverage_pct));
      parts.push('p95 ' + (summary.p95_latency_ms == null ? '\u2014' : nf.format(summary.p95_latency_ms) + 'ms'));
      parts.push('MTTR ' + formatDuration(summary.mttr_s));
      if (summary.last_check_at) parts.push('último check ' + summary.last_check_at);
      detail.textContent = parts.join(' \u00b7 ');
    }
    renderSparkline(series);
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

  function renderFreshness(lastRun) {
    var footer = document.querySelector('footer.bottom');
    if (!footer) return;
    var el = document.getElementById('portal-freshness');
    if (!el) {
      el = document.createElement('span');
      el.id = 'portal-freshness';
      footer.appendChild(el);
    }
    var stamp = lastRun ? new Date(lastRun) : new Date();
    el.textContent = 'estado actualizado ' + (isNaN(stamp.getTime()) ? new Date().toLocaleTimeString() : stamp.toLocaleTimeString());
  }

  function dispatchRendered() {
    document.dispatchEvent(new CustomEvent('portal:rendered'));
  }

  function applyStatuses(next, summary, series, lastRun, windowEffective) {
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
    renderStats(summary, series, windowEffective);
    renderFreshness(lastRun);
    dispatchRendered();
    var known = {};
    if (portal) {
      portal.services.forEach(function (s) { known[s.slug] = s; });
    }
    Object.keys(statuses).forEach(function (slug) {
      var st = statuses[slug] || {};
      var s = known[slug];
      if (!s) {
        s = known[slug] = {
          slug: slug,
          name: st.name || slug,
          url: st.url || '',
          category: st.category || '',
          tech: [],
        };
      }
      if (!s.name && st.name) s.name = st.name;
      if (!s.url && st.url) s.url = st.url;
      s.status = st;
    });
    document.dispatchEvent(new CustomEvent('portal:statuses', {
      detail: { statuses: statuses, services: Object.keys(known).map(function (slug) { return known[slug]; }) },
    }));
  }

  function loadStatuses() {
    return fetchJSON('/api/status')
      .then(function (data) {
        clearError();
        applyStatuses(data.statuses, data.summary, data.series, data.last_run, data.window_effective);
        return data;
      })
      .catch(function () {
        var section = document.getElementById('portal-stats');
        if (section) section.setAttribute('aria-busy', 'false');
        showError('No se pudo consultar el estado de los servicios (portal-api). Se reintenta…');
        return null;
      });
  }

  function schedule(delay) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      if (document.hidden) {
        schedule(delay);
        return;
      }
      loadStatuses().then(function (data) {
        schedule((data && data.refresh_ms) || delay || DEFAULT_REFRESH_MS);
      });
    }, (delay || DEFAULT_REFRESH_MS) + Math.floor(Math.random() * 3000));
  }

  function loadPortal() {
    if (staticMode) {
      hydrateFromDom();
      return Promise.resolve();
    }
    return fetchJSON('/api/catalog')
      .then(function (data) {
        portal = { services: data.services || [], categories: data.categories || [] };
        portal.categories.forEach(function (cat) {
          cat.count = portal.services.filter(function (s) { return s.category === cat.slug; }).length;
          cat.count_label = cat.count_label || 'servicios';
        });
        clearError();
        renderTables();
        renderCardLists();
        applyStatuses(statuses, null, null, null);
      })
      .catch(function () {
        showError('No se pudo cargar el catálogo del portal (portal-api).');
      });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (timer) clearTimeout(timer);
    loadStatuses().then(function (data) {
      schedule((data && data.refresh_ms) || DEFAULT_REFRESH_MS);
    });
  });

  loadPortal().then(function () {
    return loadStatuses();
  }).then(function (data) {
    schedule((data && data.refresh_ms) || DEFAULT_REFRESH_MS);
  });
})();
