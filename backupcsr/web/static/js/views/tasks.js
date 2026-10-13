/* Vista de creación/edición de tareas desde el portal (modal #newJobModal).
   Permite clonar una tarea existente o partir de cero, elegir credenciales
   (reutilizar grupo o crear uno), editar rutas origen→destino, ver el script que
   se instalará y guardar/eliminar. Solo visible con admin + BACKUP_MANAGE_JOBS=1. */
import { $, el, fmtDate, toast, confirmDialog, openModal, hideModal } from '../ui.js';

let ctxRef = null;

function splitList(value) {
  return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
}

function credMode() {
  return $('newjob-cred-new') && $('newjob-cred-new').checked ? 'new' : 'reuse';
}

function setTab(which) {
  const target = 'newjob-' + which;
  const button = $(target + '-btn');
  if (button && window.bootstrap && window.bootstrap.Tab) {
    window.bootstrap.Tab.getOrCreateInstance(button).show();
    return;
  }
  ['newjob-general', 'newjob-rutas', 'newjob-cred', 'newjob-prog'].forEach((id) => {
    const pane = $(id);
    if (pane) { pane.classList.toggle('show', id === target); pane.classList.toggle('active', id === target); }
    const btn = $(id + '-btn');
    if (btn) { btn.classList.toggle('active', id === target); btn.setAttribute('aria-selected', id === target ? 'true' : 'false'); }
  });
}

function syncCredFields() {
  const isNew = credMode() === 'new';
  const reuseBox = $('newjob-cred-reuse-box');
  const newBox = $('newjob-cred-new-box');
  if (reuseBox) reuseBox.hidden = isNew;
  if (newBox) newBox.hidden = !isNew;
  const type = $('newjob-origin-type').value;
  const passRow = $('newjob-cred-pass-row');
  const keyRow = $('newjob-cred-key-row');
  if (keyRow) keyRow.hidden = type !== 'sftp';
  if (passRow) passRow.hidden = type === 'sftp';
}

function routeRow(source, dest) {
  const wrap = el('div', { class: 'row g-2 align-items-end newjob-route mb-2' });
  const src = el('input', { class: 'form-control form-control-sm mono newjob-src', autocomplete: 'off', 'aria-label': 'Origen remoto' });
  const dst = el('input', { class: 'form-control form-control-sm mono newjob-dest', autocomplete: 'off', 'aria-label': 'Destino relativo al NAS' });
  src.value = source || '';
  dst.value = dest || '';
  wrap.appendChild(el('div', { class: 'col-12 col-md-5' }, [
    el('label', { class: 'form-label small mb-1', text: 'Origen (remoto)' }), src,
  ]));
  wrap.appendChild(el('div', { class: 'col-12 col-md-5' }, [
    el('label', { class: 'form-label small mb-1', text: 'Destino (relativo al NAS)' }), dst,
  ]));
  const remove = el('button', {
    type: 'button', class: 'btn btn-sm btn-outline-danger newjob-del-route', title: 'Quitar ruta',
  }, [el('i', { class: 'fa-solid fa-trash' })]);
  remove.addEventListener('click', () => {
    if (document.querySelectorAll('#newjob-routes .newjob-route').length <= 1) {
      toast('Debe quedar al menos una ruta', 'warn');
      return;
    }
    wrap.remove();
  });
  wrap.appendChild(el('div', { class: 'col-12 col-md-2' }, [remove]));
  return wrap;
}

function setRoutes(routes) {
  const box = $('newjob-routes');
  if (!box) return;
  box.innerHTML = '';
  const list = routes && routes.length ? routes : [{ source: '', dest_rel: '' }];
  list.forEach((route) => box.appendChild(routeRow(route.source, route.dest_rel)));
}

function collect(existingSlug) {
  const routes = Array.from(document.querySelectorAll('#newjob-routes .newjob-route')).map((row) => ({
    source: row.querySelector('.newjob-src').value.trim(),
    dest_rel: row.querySelector('.newjob-dest').value.trim(),
  })).filter((route) => route.source || route.dest_rel);
  const payload = {
    slug: existingSlug || $('newjob-slug').value.trim().toLowerCase(),
    name: $('newjob-name').value.trim(),
    description: $('newjob-description').value.trim(),
    origin_type: $('newjob-origin-type').value,
    cred_prefix: (credMode() === 'new' ? $('newjob-cred-prefix').value : $('newjob-cred-group').value).trim().toUpperCase(),
    cred_mode: credMode(),
    host: $('newjob-cred-host').value.trim(),
    port: $('newjob-cred-port').value.trim(),
    user: $('newjob-cred-user').value.trim(),
    password: $('newjob-cred-pass').value || null,
    key_name: $('newjob-cred-key').value.trim(),
    routes: routes,
    exclusions: splitList($('newjob-exclusions').value),
    gate_wait: $('newjob-gatewait').value ? Number($('newjob-gatewait').value) : null,
    sort: Number($('newjob-sort').value || 100),
    criticality: $('newjob-criticality').value,
    owner: $('newjob-owner').value.trim(),
    retention_days: $('newjob-retention').value === '' ? null : Number($('newjob-retention').value),
    tags: $('newjob-tags').value.trim(),
    size_exclude: $('newjob-size-exclude').value.trim(),
    notes: $('newjob-notes').value,
    enabled: $('newjob-enabled').checked,
    lockfile: $('newjob-lockfile').value.trim(),
    cron_minute: $('newjob-minute').value.trim(),
    cron_hour: $('newjob-hour').value.trim(),
    cron_dom: $('newjob-dom').value.trim(),
    cron_month: $('newjob-month').value.trim(),
    cron_dow: $('newjob-dow').value.trim(),
  };
  return payload;
}

function showError(message) {
  const box = $('newjob-error');
  if (!box) return;
  box.textContent = message || '';
  box.hidden = !message;
}

async function runPreview() {
  try {
    const data = await ctxRef.api.post('/jobs/preview', collect($('newjob-mode').value === 'edit' ? $('newjob-slug').value : ''));
    $('newjob-script').textContent = data.script || '';
    const list = $('newjob-preview');
    list.innerHTML = '';
    (data.next || []).forEach((when) => list.appendChild(el('li', { text: fmtDate(when) })));
    if (!(data.next || []).length) list.appendChild(el('li', { class: 'muted', text: 'sin ocurrencias' }));
    if (data.valid === false) showError('El script no compila: ' + (data.error || ''));
    else showError('');
  } catch (err) {
    showError(err.message);
  }
}

async function loadGroups() {
  const select = $('newjob-cred-group');
  if (!select) return;
  try {
    const data = await ctxRef.api.get('/credentials/groups');
    select.innerHTML = '';
    (data.groups || []).forEach((group) => {
      select.appendChild(el('option', { value: group.prefix, text: group.prefix }));
    });
    if (!(data.groups || []).length) {
      select.appendChild(el('option', { value: '', text: '(sin grupos configurados)' }));
    }
  } catch (err) {
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: '(no se pudieron leer)' }));
  }
}

function fillBaseOptions(selected) {
  const select = $('newjob-base');
  if (!select) return;
  select.innerHTML = '';
  select.appendChild(el('option', { value: '', text: '— Desde cero —' }));
  ((ctxRef.state.jobs) || []).forEach((job) => {
    select.appendChild(el('option', { value: job.slug, text: job.name + ' (' + job.slug + ')' }));
  });
  select.value = selected || '';
}

async function applyBase(slug) {
  if (!slug) return;
  try {
    const data = await ctxRef.api.get('/jobs/' + encodeURIComponent(slug) + '/definition');
    const job = data.job || {};
    $('newjob-origin-type').value = job.origin_type || 'ftp';
    // Precarga el grupo de credenciales en el selector de reutilización (el modo
    // por defecto es 'reuse'); collect() lee cred_prefix de ahí, no del campo nuevo.
    $('newjob-cred-group').value = data.cred_prefix || '';
    $('newjob-cred-prefix').value = data.cred_prefix || '';
    $('newjob-cred-key').value = data.key_name || '';
    setRoutes(data.routes || []);
    syncCredFields();
    toast('Precargado desde ' + slug, 'info');
  } catch (err) {
    showError('No se pudo clonar: ' + err.message);
  }
}

function resetForm() {
  const mode = $('newjob-mode');
  mode.value = 'create';
  $('newjob-title').textContent = 'Nueva tarea';
  $('newjob-base-row').hidden = false;
  fillBaseOptions('');
  ['newjob-slug', 'newjob-name', 'newjob-description', 'newjob-owner', 'newjob-tags',
    'newjob-size-exclude', 'newjob-notes', 'newjob-lockfile'].forEach((id) => { $(id).value = ''; });
  $('newjob-slug').disabled = false;
  $('newjob-origin-type').value = 'ftp';
  $('newjob-sort').value = '100';
  $('newjob-criticality').value = 'media';
  $('newjob-retention').value = '';
  $('newjob-minute').value = '20';
  $('newjob-hour').value = '6-19';
  $('newjob-dom').value = '*';
  $('newjob-month').value = '*';
  $('newjob-dow').value = '*';
  $('newjob-enabled').checked = false;
  $('newjob-exclusions').value = '';
  $('newjob-gatewait').value = '';
  $('newjob-cred-prefix').value = '';
  $('newjob-cred-host').value = '';
  $('newjob-cred-port').value = '22';
  $('newjob-cred-user').value = '';
  $('newjob-cred-pass').value = '';
  $('newjob-cred-key').value = '';
  $('newjob-cred-reuse').checked = true;
  $('newjob-delete').hidden = true;
  $('newjob-script').textContent = '—';
  $('newjob-preview').innerHTML = '';
  setRoutes([]);
  syncCredFields();
  showError('');
}

export function openNewJob(ctx, base) {
  ctxRef = ctx;
  resetForm();
  loadGroups().then(() => { if (base) applyBase(base); });
  setTab('general');
  openModal('newJobModal');
}

export async function openEditJob(ctx, job) {
  ctxRef = ctx;
  resetForm();
  $('newjob-mode').value = 'edit';
  $('newjob-title').textContent = 'Editar tarea · ' + job.name;
  $('newjob-base-row').hidden = true;
  $('newjob-slug').value = job.slug;
  $('newjob-slug').disabled = true;
  $('newjob-name').value = job.name || '';
  $('newjob-description').value = job.description || '';
  $('newjob-origin-type').value = job.origin_type || 'ftp';
  $('newjob-sort').value = job.sort === null || job.sort === undefined ? 100 : job.sort;
  $('newjob-criticality').value = job.criticality || 'media';
  $('newjob-owner').value = job.owner || '';
  $('newjob-retention').value = job.retention_days === null || job.retention_days === undefined ? '' : job.retention_days;
  $('newjob-tags').value = job.tags || '';
  $('newjob-size-exclude').value = Array.isArray(job.size_exclude) ? job.size_exclude.join(', ') : (job.size_exclude || '');
  $('newjob-notes').value = job.notes || '';
  $('newjob-enabled').checked = !!job.enabled;
  $('newjob-lockfile').value = job.lockfile || '';
  $('newjob-minute').value = job.cron_minute || '20';
  $('newjob-hour').value = job.cron_hour || '6-19';
  $('newjob-dom').value = job.cron_dom || '*';
  $('newjob-month').value = job.cron_month || '*';
  $('newjob-dow').value = job.cron_dow || '*';
  await loadGroups();
  try {
    const data = await ctx.api.get('/jobs/' + encodeURIComponent(job.slug) + '/definition');
    if (data.cred_prefix) $('newjob-cred-group').value = data.cred_prefix;
    if (data.key_name) $('newjob-cred-key').value = data.key_name;
    $('newjob-exclusions').value = (data.exclusions || []).join(', ');
    if (data.gate_wait) $('newjob-gatewait').value = data.gate_wait;
    setRoutes(data.routes || []);
  } catch (err) {
    showError('No se pudo leer la definición: ' + err.message);
  }
  $('newjob-delete').hidden = false;
  syncCredFields();
  setTab('general');
  openModal('newJobModal');
}

async function save() {
  const editing = $('newjob-mode').value === 'edit';
  const slug = $('newjob-slug').value.trim().toLowerCase();
  const payload = collect(editing ? slug : '');
  if (!slug) { showError('El slug es obligatorio'); return; }
  if (!payload.name) { showError('El nombre es obligatorio'); return; }
  try {
    if (editing) {
      await ctxRef.api.request('/jobs/' + encodeURIComponent(slug), {
        method: 'PUT', body: JSON.stringify(payload),
      });
      toast('Tarea actualizada: ' + slug, 'ok');
    } else {
      const res = await ctxRef.api.post('/jobs', payload);
      toast(res.note || ('Tarea creada: ' + slug), res.note ? 'warn' : 'ok');
    }
    hideModal('newJobModal');
    await ctxRef.refresh(true);
  } catch (err) {
    showError(err.message);
  }
}

async function remove() {
  const slug = $('newjob-slug').value.trim().toLowerCase();
  if (!slug) return;
  confirmDialog('Eliminar tarea',
    'Eliminar "' + slug + '"? Se borra el script, el catálogo y su línea de cron; el '
    + 'histórico de ejecuciones se conserva.', async () => {
      try {
        await ctxRef.api.del('/jobs/' + encodeURIComponent(slug));
        hideModal('newJobModal');
        toast('Tarea eliminada: ' + slug, 'ok');
        await ctxRef.refresh(true);
      } catch (err) {
        showError(err.message);
      }
    }, 'Eliminar', 'btn-danger');
}

export const tasks = {
  id: 'tareas',
  admin: true,
  mount(ctx) {
    ctxRef = ctx;
    $('newjob-add-route').addEventListener('click', () => {
      $('newjob-routes').appendChild(routeRow('', ''));
    });
    $('newjob-base').addEventListener('change', (event) => applyBase(event.target.value));
    $('newjob-origin-type').addEventListener('change', syncCredFields);
    document.querySelectorAll('input[name="newjob-cred-mode"]').forEach((radio) => {
      radio.addEventListener('change', syncCredFields);
    });
    $('newjob-preview-btn').addEventListener('click', runPreview);
    $('newjob-save').addEventListener('click', save);
    $('newjob-delete').addEventListener('click', remove);
  },
  render() {},
};
