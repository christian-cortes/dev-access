/* Shell del portal: app-shell, routing, estado compartido y modales comunes. */
import { api, LOGIN_URL } from './api.js';
import { applyDataIcons } from './icons.js';
import {
  $, el, icon, pill, fmtDate, fmtDuration, fmtGb, relTime, toast, confirmDialog,
  openModal, hideModal,
} from './ui.js';
import { panel } from './views/panel.js';
import { history } from './views/history.js';
import { files } from './views/files.js';
import { schedule, openEditor } from './views/schedule.js';
import { users } from './views/users.js';
import { audit } from './views/audit.js';

const VIEWS = [panel, history, files, schedule, users, audit];

const state = {
  user: null,
  jobs: [],
  summary: null,
  manageCron: false,
  series: [],
  seriesLoaded: false,
  tab: 'panel',
  histJob: '',
  filesJob: '',
  checked: new Set(),
  expanded: new Set(),
  anchor: '',
  batch: null,
  jobsSearch: '',
  // Diagnóstico de carga: la tabla nunca se vacía por un fallo de API.
  loading: true,
  loaded: false,
  jobsSig: '',
  summarySig: '',
  apiErrors: [],
  renderError: '',
  authError: '',
};

const mounted = {};
let pendingView = null;
let refreshTimer = null;

const ctx = {
  api,
  state,
  refresh: loadAll,
  go,
  toast,
  confirmDialog,
  openLog,
  openRun,
  openSchedule: (job, tab) => {
    ensureMounted(schedule);
    const isAdmin = !!(state.user && state.user.role === 'admin');
    openEditor(job, {
      isAdmin: isAdmin,
      canManage: isAdmin && !!state.manageCron,
      tab: tab || 'programacion',
    });
  },
  openPassword,
};

/* --------------------------------- banner --------------------------------- */

const banner = { kind: '', text: '', action: null, actionLabel: '' };

function renderBanner() {
  const box = $('banner');
  if (!box) return;
  if (!banner.text) {
    box.hidden = true;
    box.textContent = '';
    return;
  }
  box.className = 'alert alert-' + (banner.kind || 'warning') + ' banner d-flex align-items-center gap-2 flex-wrap';
  box.hidden = false;
  box.innerHTML = '';
  box.appendChild(el('i', {
    class: banner.kind === 'danger' ? 'fa-solid fa-triangle-exclamation' : 'fa-solid fa-circle-info',
  }));
  box.appendChild(el('span', { class: 'flex-grow-1', text: banner.text }));
  if (banner.action) {
    const button = el('button', {
      type: 'button',
      class: 'btn btn-sm btn-outline-' + (banner.kind || 'warning'),
      text: banner.actionLabel || 'Reintentar',
    });
    button.addEventListener('click', banner.action);
    box.appendChild(button);
  }
}

function setBanner(kind, text, action, actionLabel) {
  banner.kind = kind;
  banner.text = text;
  banner.action = action || null;
  banner.actionLabel = actionLabel || '';
  renderBanner();
}

function clearBanner() {
  banner.text = '';
  banner.action = null;
  renderBanner();
}

function describeError(err) {
  if (!err) return 'error desconocido';
  if (err.status) return 'HTTP ' + err.status + (err.message ? ' (' + err.message + ')' : '');
  return err.message || String(err);
}

/* --------------------------------- datos --------------------------------- */

function summarySignature(summary) {
  if (!summary) return '';
  return JSON.stringify([
    summary.counts || null, summary.bytes_total, summary.files_total, summary.avg_duration_s,
    summary.db, summary.nas, summary.nas_stats || null, summary.growth || null,
    (summary.alerts || []).map((alert) => alert.level + ':' + alert.text),
  ]);
}

function jobsSignature(jobs) {
  return jobs.map((job) => [
    job.slug, job.status, job.status_detail, job.running ? 1 : 0, job.enabled ? 1 : 0,
    job.last_run && job.last_run.started_at, job.last_run && job.last_run.duration_s,
    job.next_run, job.size && job.size.bytes, job.size && job.size.pending ? 1 : 0,
    job.progress && job.progress.live ? 1 : 0, job.progress && job.progress.state,
  ].join(':')).join('|');
}

/* Carga tolerante a fallos: si un endpoint cae, el otro se conserva y la tabla
   mantiene la última información válida en lugar de quedar vacía. */
async function loadAll(silent) {
  const [summaryRes, jobsRes] = await Promise.allSettled([api.get('/summary'), api.get('/jobs')]);
  const errors = [];

  if (summaryRes.status === 'fulfilled') {
    state.summary = summaryRes.value || {};
  } else if (!(summaryRes.reason && summaryRes.reason.status === 401)) {
    errors.push({ endpoint: '/api/summary', message: describeError(summaryRes.reason) });
  }

  let jobsChanged = false;
  if (jobsRes.status === 'fulfilled') {
    const data = jobsRes.value || {};
    if (Array.isArray(data.jobs)) {
      const signature = jobsSignature(data.jobs);
      jobsChanged = signature !== state.jobsSig;
      state.jobs = data.jobs;
      state.jobsSig = signature;
      state.loaded = true;
      state.manageCron = !!data.manage_cron;
    } else {
      errors.push({ endpoint: '/api/jobs', message: 'la respuesta no trae la lista de tareas' });
    }
  } else if (!(jobsRes.reason && jobsRes.reason.status === 401)) {
    errors.push({ endpoint: '/api/jobs', message: describeError(jobsRes.reason) });
  }

  const summaryChanged = summarySignature(state.summary) !== state.summarySig;
  state.summarySig = summarySignature(state.summary);
  state.apiErrors = errors;
  state.loading = false;
  updateStatus();

  // Solo repintamos si hay datos nuevos (o si el usuario pidió actualizar): el
  // refresco automático no reconstruye la tabla mientras se está interactuando.
  if (jobsChanged || !silent) {
    activate(state.tab);
    return;
  }
  if (summaryChanged && state.tab === 'panel') panel.renderSummary(state);
  refreshBanner();
}

async function loadSeries() {
  try {
    const data = await api.get('/sizes?days=30');
    state.series = data.series || [];
    state.seriesLoaded = true;
  } catch (e) {
    state.seriesLoaded = true;
  }
}

/* Cola de acciones masivas (en memoria del backend): se consulta en cada refresco
   para mostrar el avance aunque el lote lo haya lanzado otra pestaña o sesión. */
async function loadBatch() {
  try {
    state.batch = await api.get('/jobs/batch');
    if (state.tab === 'panel') panel.renderBatch(state);
  } catch (e) {
    /* endpoint no disponible o sin sesión: se reintenta en el próximo ciclo */
  }
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

/* ------------------------------- alertas ---------------------------------- */

/* Notificaciones de la campana: solo avisos de texto (jobs, MySQL y reloj).
   El estado del NAS se muestra siempre en el panel (ver views/panel.js). */
const NOTIF_META = {
  job: { icon: 'server', title: 'Tarea' },
  db: { icon: 'database', title: 'Base de datos' },
  clock: { icon: 'clock', title: 'Reloj del portal' },
  schedule: { icon: 'clock', title: 'Horario' },
};

/* Etiqueta corta de la clase de error (ver logs.py) para el cuerpo del aviso. */
const CLASS_META = {
  fatal: 'Crítico',
  transitorio: 'Red',
  contencion: 'Cola',
  parcial: 'Parcial',
};

/* Descarte por navegador: ids de avisos que el usuario limpió. */
const NOTIF_ACK_KEY = 'backupcsr.notif.ack.v1';

function notifId(alert) {
  if (alert.id) return alert.id;
  const arm = alert.slug ? alert.slug + ':' + (alert.class || '') + ':' : '';
  return 'copias:' + alert.kind + ':' + arm + alert.level;
}

function ackedSet() {
  try {
    const raw = localStorage.getItem(NOTIF_ACK_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch (e) {
    return new Set();
  }
}

function saveAcked(set) {
  try { localStorage.setItem(NOTIF_ACK_KEY, JSON.stringify(Array.from(set))); } catch (e) {}
}

function closeAlertsMenu() {
  const toggle = $('alerts-toggle');
  if (!toggle || !window.bootstrap || !bootstrap.Dropdown) return;
  const inst = bootstrap.Dropdown.getInstance(toggle);
  if (inst) inst.hide();
}

function dismissAlert(id) {
  const set = ackedSet();
  set.add(id);
  saveAcked(set);
  renderAlerts();
}

function clearAlerts() {
  const set = ackedSet();
  ((state.summary && state.summary.alerts) || [])
    .filter((alert) => alert.kind !== 'nas')
    .forEach((alert) => set.add(notifId(alert)));
  saveAcked(set);
  renderAlerts();
}

function alertDanger(alert) {
  return alert.severity ? alert.severity === 'critical' : alert.level === 'danger';
}

function detailBox(alert) {
  const details = Array.isArray(alert.details) ? alert.details : [];
  if (!details.length) return null;
  const box = el('div', { class: 'notif-detail' });
  box.hidden = true;
  details.forEach((row) => {
    box.appendChild(el('div', { class: 'notif-detail-row' }, [
      el('span', { class: 'notif-detail-label', text: row.label }),
      el('span', { class: 'notif-detail-value', text: row.value }),
    ]));
  });
  return box;
}

function notifRow(alert) {
  const meta = NOTIF_META[alert.kind] || { icon: 'alert', title: 'Aviso' };
  const danger = alertDanger(alert);
  let title = meta.title;
  let text = alert.text;
  if (alert.kind === 'job') {
    const cut = String(text).indexOf(': ');
    if (cut > 0) {
      title = String(text).slice(0, cut);
      text = String(text).slice(cut + 2);
    }
  }
  if (alert.class && CLASS_META[alert.class]) {
    text = '[' + CLASS_META[alert.class] + '] ' + text;
  }

  const body = el('div', { class: 'notif-body' }, [
    el('div', { class: 'notif-head' }, [
      el('div', { class: 'notif-title', text: title }),
      alert.ts ? el('span', { class: 'notif-time', text: alert.ts }) : null,
      el('button', {
        type: 'button', class: 'notif-dismiss', title: 'Descartar', 'aria-label': 'Descartar aviso',
        onclick: () => dismissAlert(notifId(alert)),
      }, [icon('x')]),
    ]),
    el('div', { class: 'notif-text', text: text }),
  ]);

  const box = detailBox(alert);
  if (box) {
    const toggle = el('button', {
      type: 'button', class: 'notif-more', text: 'Ver detalle',
      onclick: () => {
        box.hidden = !box.hidden;
        toggle.textContent = box.hidden ? 'Ver detalle' : 'Ocultar detalle';
      },
    });
    body.appendChild(toggle);
    body.appendChild(box);
  }

  if (alert.action && alert.action.slug) {
    body.appendChild(el('button', {
      type: 'button', class: 'notif-action', text: alert.action.label || 'Ver tarea',
      onclick: () => {
        closeAlertsMenu();
        state.histJob = alert.action.slug;
        go('historial');
      },
    }));
  }

  return el('div', { class: 'notif ' + (danger ? 'notif-danger' : 'notif-warn') }, [
    el('div', { class: 'notif-icon' }, [icon(meta.icon)]),
    body,
    el('span', { class: 'notif-level', text: danger ? 'Crítico' : 'Aviso' }),
  ]);
}

/* Las notificaciones ya no viven dentro del panel: se consultan desde el botón
   de la barra superior, con un contador en el badge de la campana. No incluyen
   el estado del NAS: ese se muestra siempre en el panel. */
function renderAlerts() {
  const list = $('alerts-list');
  const badge = $('alerts-count');
  const countPill = $('alerts-menu-count');
  const toggle = $('alerts-toggle');
  const clearBtn = $('alerts-clear');
  const summary = state.summary || {};
  const active = (summary.alerts || []).filter((alert) => alert.kind !== 'nas');

  const acked = ackedSet();
  const activeIds = new Set(active.map(notifId));
  let pruned = false;
  Array.from(acked).forEach((id) => {
    if (!activeIds.has(id)) { acked.delete(id); pruned = true; }
  });
  if (pruned) saveAcked(acked);

  const alerts = active.filter((alert) => !acked.has(notifId(alert)));
  const danger = alerts.some(alertDanger);
  const loaded = !!state.summary;

  if (list) {
    list.innerHTML = '';
    if (!alerts.length) {
      list.appendChild(el('div', { class: 'alerts-empty' }, [
        el('i', { class: loaded ? 'fa-solid fa-circle-check' : 'fa-regular fa-bell' }),
        el('span', {
          text: !loaded ? 'Cargando notificaciones…'
            : (active.length ? 'Sin notificaciones pendientes.' : 'Sin notificaciones.'),
        }),
      ]));
    } else {
      alerts.forEach((alert) => list.appendChild(notifRow(alert)));
    }
  }

  if (clearBtn) {
    clearBtn.hidden = alerts.length === 0;
    clearBtn.textContent = 'Limpiar todo';
  }

  if (badge) {
    badge.hidden = alerts.length === 0;
    badge.textContent = alerts.length > 99 ? '99+' : String(alerts.length);
    badge.classList.toggle('is-danger', danger);
  }
  if (countPill) {
    countPill.textContent = String(alerts.length);
    countPill.className = 'badge rounded-pill ' + (danger ? 'pill-err' : alerts.length ? 'pill-warn' : 'pill-ok');
  }
  if (toggle) {
    toggle.classList.toggle('has-alerts', alerts.length > 0 && !danger);
    toggle.classList.toggle('has-danger', danger);
    toggle.setAttribute('aria-label',
      alerts.length ? 'Ver notificaciones (' + alerts.length + ')' : 'Ver notificaciones');
  }
}

function updateStatus() {
  const summary = state.summary || {};
  const dbOk = !!summary.db;
  const nasOk = !!summary.nas;
  const nas = summary.nas_stats;

  ['conn-db', 'conn-nas'].forEach((id, index) => {
    const node = $(id);
    if (!node) return;
    const ok = index === 0 ? dbOk : nasOk;
    node.className = 'conn-pill hide-sm ' + (ok ? 'ok' : 'off');
  });

  const sbDb = $('sb-db');
  if (sbDb) sbDb.className = 'dot ' + (dbOk ? 'ok' : 'off');
  setText('sb-db-text', dbOk ? 'conectada' : 'sin conexión');
  const sbNas = $('sb-nas');
  if (sbNas) sbNas.className = 'dot ' + (nasOk ? 'ok' : 'off');
  setText('sb-nas-text', nas ? (nas.percent + '%') : (nasOk ? 'montado' : 'no montado'));
  setText('sb-updated', summary.generated_at ? 'actualizado ' + relTime(summary.generated_at) : '—');
  renderAlerts();
}

/* Un solo banner con prioridad: error de pintado > endpoints caídos > MySQL. */
function refreshBanner() {
  if (state.renderError) {
    setBanner('danger', state.renderError, () => activate(state.tab), 'Reintentar');
    return;
  }
  if (state.authError) {
    setBanner('danger', state.authError, bootstrap, 'Reintentar');
    return;
  }
  if (state.apiErrors.length) {
    const detail = state.apiErrors.map((item) => item.endpoint + ' → ' + item.message).join('; ');
    setBanner('danger',
      'No se pudieron leer datos del servidor (' + detail + '). Se muestra la última información válida.',
      () => loadAll(false));
    return;
  }
  const summary = state.summary;
  if (summary && summary.db === false) {
    setBanner('warning', 'MySQL no disponible: modo lectura (sin edición de horarios ni historial).');
    return;
  }
  clearBanner();
}

/* ------------------------------- navegación ------------------------------- */

const TITLES = {
  panel: 'Panel', historial: 'Historial', archivos: 'Archivos',
  programacion: 'Programación', usuarios: 'Usuarios', auditoria: 'Auditoría',
};

function renderBreadcrumb(view) {
  const box = $('breadcrumbs');
  if (!box) return;
  box.innerHTML = '';
  box.appendChild(el('span', { text: 'Copias' }));
  box.appendChild(el('span', { text: '/' }));
  box.appendChild(el('strong', { text: TITLES[view] || view }));
}

function safeRender(view) {
  try {
    view.render(ctx);
    state.renderError = '';
  } catch (err) {
    console.error('render ' + view.id, err);
    state.renderError = 'No se pudo dibujar "' + (TITLES[view.id] || view.id) + '": ' + err.message;
  }
}

function ensureMounted(view) {
  if (mounted[view.id]) return;
  try {
    view.mount(ctx);
  } catch (err) {
    console.error('mount ' + view.id, err);
  }
  mounted[view.id] = true;
}

function activate(name) {
  const view = VIEWS.filter((v) => v.id === name)[0] || panel;
  if (view.admin && (!state.user || state.user.role !== 'admin')) return activate('panel');
  state.tab = view.id;
  VIEWS.forEach((v) => {
    const section = $('view-' + v.id);
    if (section) section.hidden = v.id !== view.id;
  });
  document.querySelectorAll('#sidebar-nav .nav-item').forEach((link) => {
    link.classList.toggle('active', link.getAttribute('data-view') === view.id);
  });
  renderBreadcrumb(view.id);
  ensureMounted(view);
  safeRender(view);
  refreshBanner();
  if (view.id === 'panel' && !state.seriesLoaded) {
    loadSeries().then(() => {
      if (state.tab === 'panel') safeRender(view);
    });
  }
}

function go(name) {
  const target = '#/' + name;
  if (location.hash !== target) {
    pendingView = name;
    location.hash = target;
  }
  activate(name);
}

function currentView() {
  return (location.hash || '').replace(/^#\/?/, '') || 'panel';
}

/* --------------------------------- sidebar -------------------------------- */

function setupSidebar() {
  const sidebar = $('app-sidebar');
  const backdrop = $('sidebar-backdrop');
  const close = () => { sidebar.classList.remove('open'); backdrop.hidden = true; };
  const toggle = $('sidebar-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      sidebar.classList.toggle('open');
      backdrop.hidden = !sidebar.classList.contains('open');
    });
  }
  if (backdrop) backdrop.addEventListener('click', close);
  // Navegación explícita: no dependemos de que el hash cambie para renderizar.
  document.querySelectorAll('#sidebar-nav .nav-item').forEach((link) => {
    link.addEventListener('click', (event) => {
      const target = link.getAttribute('data-view');
      if (!target) return;
      event.preventDefault();
      if (window.innerWidth < 992) close();
      go(target);
    });
  });
}

/* ---------------------------------- tema ---------------------------------- */

function currentTheme() {
  const root = document.documentElement;
  return (root.getAttribute('data-bs-theme') || root.getAttribute('data-theme')) === 'dark' ? 'dark' : 'light';
}

function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  const root = document.documentElement;
  root.setAttribute('data-theme', next);
  root.setAttribute('data-bs-theme', next);
  try { localStorage.setItem('cortexdev-theme', next); } catch (e) {}
}

/* --------------------------------- modales -------------------------------- */

async function openLog(slug, name) {
  try {
    const data = await api.get('/jobs/' + slug + '?tail=500');
    $('log-title').textContent = 'Log · ' + (name || slug);
    $('log-pre').textContent = data.log || '(vacío)';
    openModal('logModal');
  } catch (err) {
    toast('No se pudo leer el log: ' + err.message, 'err');
  }
}

async function openRun(runId) {
  try {
    const data = await api.get('/runs/' + runId);
    $('run-title').textContent = 'Ejecución #' + runId + ' · ' + (data.run.job_slug || '');
    const body = $('run-body');
    body.innerHTML = '';
    const pairs = [
      ['Estado', data.run.status],
      ['Inicio', fmtDate(data.run.started_at)],
      ['Fin', fmtDate(data.run.finished_at)],
      ['Duración', fmtDuration(data.run.duration_s)],
      ['Tamaño', data.run.size_bytes === null || data.run.size_bytes === undefined ? '—' : fmtGb(data.run.size_bytes)],
      ['Transferidos', String(data.run.files_transferred || 0)],
      ['Eliminados', String(data.run.files_removed || 0)],
    ];
    const info = el('div', { class: 'row g-2 mb-3' });
    pairs.forEach((pair) => info.appendChild(el('div', { class: 'col-6 col-md-4' }, [
      el('div', { class: 'muted small', text: pair[0] }),
      el('div', { text: pair[1] }),
    ])));
    body.appendChild(info);
    if (data.run.error_text || (data.run.error_lines || []).length) {
      const box = el('div', {
        class: 'alert ' + (data.run.error_class === 'fatal' ? 'alert-danger' : 'alert-warning'),
      });
      if (data.run.error_text) box.appendChild(el('div', { text: data.run.error_text }));
      (data.run.error_lines || []).forEach((line) => {
        box.appendChild(el('div', { class: 'mono small', text: line }));
      });
      body.appendChild(box);
    }
    body.appendChild(el('div', { class: 'section-title', text: 'Archivos (' + data.files.length + ')' }));
    const list = el('ul', { class: 'mono', style: 'max-height:320px;overflow:auto' });
    data.files.slice(0, 2000).forEach((file) => {
      list.appendChild(el('li', { text: (file.action === 'remove' ? '- ' : '+ ') + file.path }));
    });
    if (!data.files.length) list.appendChild(el('li', { class: 'muted', text: 'sin detalle de archivos' }));
    body.appendChild(list);
    const logButton = el('button', {
      type: 'button', class: 'btn btn-sm btn-outline-secondary mt-2', text: 'Ver log',
    });
    logButton.addEventListener('click', () => openRunLog(runId));
    body.appendChild(logButton);
    openModal('runModal');
  } catch (err) {
    toast('No se pudo abrir la ejecución: ' + err.message, 'err');
  }
}

async function openRunLog(runId) {
  try {
    const data = await api.get('/runs/' + runId + '/log?tail=800');
    $('log-title').textContent = 'Log · ejecución #' + runId;
    $('log-pre').textContent = data.log || '(vacío)';
    openModal('logModal');
  } catch (err) {
    toast('No se pudo leer el log: ' + err.message, 'err');
  }
}

function openPassword(username, self) {
  $('password-username').value = username;
  $('password-title').textContent = self ? 'Cambiar mi contraseña' : 'Contraseña de ' + username;
  $('password-current-row').hidden = !self;
  $('password-current').value = '';
  $('password-new').value = '';
  $('password-error').hidden = true;
  $('passwordModal').dataset.self = self ? '1' : '0';
  openModal('passwordModal');
}

async function savePassword() {
  const username = $('password-username').value;
  const self = $('passwordModal').dataset.self === '1';
  const box = $('password-error');
  try {
    if (self) {
      await api.post('/auth/password', {
        current: $('password-current').value,
        new: $('password-new').value,
      });
    } else {
      await api.post('/users/' + encodeURIComponent(username) + '/password', {
        password: $('password-new').value,
      });
    }
    hideModal('passwordModal');
    toast(self ? 'Contraseña actualizada' : 'Contraseña restablecida', 'ok');
  } catch (err) {
    box.textContent = err.message;
    box.hidden = false;
  }
}

/* ---------------------------------- init ---------------------------------- */

/* El filtro de la tabla (jobs-search) se rellena solo con el usuario guardado en
   Firefox (wfd-id) y Chrome porque la página tiene un modal con contraseñas.
   Dos capas:
     1) `readonly` (viene en el HTML): los navegadores no rellenan campos de
        solo lectura. Se libera en cuanto hay intención real (clic/tecla/foco).
     2) Solo una pulsación o pegado real marca el campo como escrito por la
        persona; el `input` del autofill —que puede llegar *después* del clic—
        se descarta y se limpia el valor, para que no deje la tabla filtrada. */
function setupFilters() {
  const input = $('jobs-search');
  if (!input) return;
  const apply = (value) => {
    state.jobsSearch = value;
    if (state.tab === 'panel') panel.renderJobs(state);
  };
  const unlock = () => input.removeAttribute('readonly');
  ['pointerdown', 'touchstart', 'focusin', 'keydown'].forEach((event) => {
    input.addEventListener(event, unlock);
  });
  ['keydown', 'paste', 'compositionstart'].forEach((event) => {
    input.addEventListener(event, () => { input.dataset.userTyped = '1'; });
  });
  input.addEventListener('focusin', () => {
    if (input.dataset.userTyped !== '1' && input.value) {
      input.value = '';
      apply('');
    }
  });
  input.addEventListener('input', () => {
    if (input.dataset.userTyped !== '1') {   // autofill del navegador
      input.value = '';
      apply('');
      return;
    }
    apply(input.value);
  });
  const dropAutofill = () => {
    if (input.dataset.userTyped === '1') return;
    input.value = '';
    if (state.jobsSearch) apply('');
  };
  setTimeout(dropAutofill, 300);
  window.addEventListener('load', () => setTimeout(dropAutofill, 50));
}

function setupTopbar() {
  setupFilters();
  const clearAlertsBtn = $('alerts-clear');
  if (clearAlertsBtn) clearAlertsBtn.addEventListener('click', clearAlerts);
  const userTheme = $('user-theme');
  if (userTheme) userTheme.addEventListener('click', () => { closeAlertsMenu(); toggleTheme(); });
  const userSettings = $('user-settings');
  if (userSettings) userSettings.addEventListener('click', () => { closeAlertsMenu(); openModal('pushModal'); });
  $('menu-password').addEventListener('click', () => openPassword(state.user.username, true));
  $('password-save').addEventListener('click', savePassword);
  $('logout-btn').addEventListener('click', async () => {
    try { await api.post('/auth/logout'); } catch (e) {}
    location.href = LOGIN_URL;
  });
}

function setupDataIcons() {
  applyDataIcons(document);
  const toggle = $('sidebar-toggle');
  if (toggle) toggle.innerHTML = '<i class="fa-solid fa-bars"></i>';
}

function startRefresh() {
  if (refreshTimer) return;
  refreshTimer = setInterval(async () => {
    if (document.hidden) return;
    // No reconstruimos la tabla con un modal o un menú abiertos: evita perder el
    // contexto de una acción en curso.
    if (document.querySelector('.modal.show')) return;
    if (document.querySelector('.dropdown-menu.show')) return;
    await loadAll(true);
    await loadBatch();
  }, 30000);
}

window.addEventListener('hashchange', () => {
  const name = currentView();
  if (pendingView === name) {
    pendingView = null;
    return;
  }
  activate(name);
});

async function bootstrap() {
  try {
    state.user = await api.get('/auth/me');
  } catch (err) {
    // 401 ya redirige al login central desde api.js; cualquier otro fallo se
    // avisa (antes se salía en silencio y el portal quedaba muerto y sin datos).
    if (!err || err.status !== 401) {
      state.loading = false;
      state.authError = 'No se pudo validar la sesión: ' + describeError(err) +
        '. El portal no puede cargar datos hasta reintentar.';
      activate(state.tab);
    }
    return;
  }
  state.authError = '';
  $('user-name').textContent = state.user.username + ' (' + state.user.role + ')';
  document.querySelectorAll('#sidebar-nav [data-admin]').forEach((node) => {
    if (state.user.role !== 'admin') node.hidden = true;
  });
  $('user-name').closest('.dropdown').classList.toggle('d-none', false);
  await loadAll(false);
  activate(currentView());
  startRefresh();
}

/* Hook de diagnóstico desde la consola del navegador: __backupcsr.state */
window.__backupcsr = {
  state,
  reload: () => loadAll(false),
  bootstrap,
  version: '5',
};

(async function init() {
  setupDataIcons();
  setupSidebar();
  setupTopbar();
  renderAlerts();
  await bootstrap();
})();
