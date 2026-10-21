/* Vista Panel: KPIs, tendencia de tamaño, duraciones y tabla de jobs.

   La tabla separa dos niveles de acción:
     - Barra superior: acciones MASIVAS sobre la selección múltiple. La selección
       es estilo DataTables: clic selecciona una fila, Ctrl/Cmd+clic alterna y
       Mayús+clic toma un rango; no hay casillas por fila.
     - Menú ⋮ por fila: acciones específicas de cada tarea.
   Las columnas Destino y Programación viven en una fila de detalle tras el signo +.
   Mientras una copia corre, el Estado muestra el % solo si es real (medido); si no,
   el tiempo transcurrido en el propio badge EN CURSO. */
import {
  $, el, pill, fmtGb, fmtDate, fmtDuration, relTime,
  relTimeFuture, scheduleText, toast, confirmDialog, statusClass, statusLabel,
} from '../ui.js';
import { sparkline, barList, emptyChart, diskGauge } from '../chart.js';

let ctxRef = null;
let tickTimer = null;

/* Etiquetas y criticidad de la ficha (campos nuevos): la lista viaja como texto
   separado por comas desde el API. */
function tagList(value) {
  if (Array.isArray(value)) return value;
  return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
}

function critClass(value) {
  const crit = String(value || 'media');
  return crit === 'alta' ? 'pill-err' : crit === 'baja' ? 'pill-unknown' : 'pill-warn';
}

function kpi(label, value, sub, extra) {
  return el('div', { class: 'col' }, [
    el('div', { class: 'card kpi h-100 ' + (extra || '') }, [
      el('div', { class: 'card-body' }, [
        el('div', { class: 'kpi-label' }, [el('span', { text: label })]),
        el('div', { class: 'kpi-value', text: value }),
        el('div', { class: 'kpi-sub', text: sub || '' }),
      ]),
    ]),
  ]);
}

function deltaLabel(growth, key) {
  const item = growth && growth[key];
  if (!item) return 'sin histórico';
  const sign = item.bytes > 0 ? '+' : '';
  const pct = item.percent === null || item.percent === undefined ? '' : ' (' + sign + item.percent + '%)';
  return sign + fmtGb(item.bytes) + pct + ' vs ' + key.replace('d', '') + 'd';
}

function renderKpis(summary) {
  const counts = summary.counts || {};
  const nas = summary.nas_stats;
  const growth = summary.growth || {};
  const bad = (counts.FALLO || 0) + (counts.NUNCA || 0);
  const row = $('kpi-row');
  if (!row) return;
  row.innerHTML = '';
  row.appendChild(kpi('GB almacenados', fmtGb(summary.bytes_total),
    (summary.files_total || 0).toLocaleString('es-CO') + ' archivos'));
  row.appendChild(kpi('Crecimiento 7d', deltaLabel(growth, 'd7'),
    deltaLabel(growth, 'd30'), (growth.d7 && growth.d7.bytes > 0) ? 'warn' : 'okv'));
  row.appendChild(kpi('Jobs OK', String(counts.OK || 0), 'de ' + (counts.total || 0) + ' · ' +
    (counts.habilitados || 0) + ' habilitadas', 'okv'));
  row.appendChild(kpi('Fallos / sin datos', String(bad),
    (counts.EN_CURSO || 0) + ' en curso · ' + (counts.TARDE || 0) + ' tarde · ' +
    (counts.PARCIAL || 0) + ' parcial · ' + (counts.OMITIDO || 0) + ' omitido',
    bad ? 'err' : ''));
  row.appendChild(kpi('Duración media', summary.avg_duration_s ? fmtDuration(summary.avg_duration_s) : '—',
    'última ejecución'));
  row.appendChild(kpi('NAS libre', nas ? fmtGb(nas.free) : '—',
    nas ? (nas.percent + '% usado de ' + fmtGb(nas.total)) : 'no montado',
    nas && nas.percent >= 80 ? 'warn' : ''));
}

/* ----------------------------- información NAS ----------------------------- */

/* El aviso de NAS no es una alerta de texto: es un disco con anillo de uso que
   vive siempre en el panel junto a los gráficos. `alert` puede ser null cuando
   el NAS está sano. */
function nasCard(alert, stats, copies) {
  const parsed = alert ? parseFloat((String(alert.text).match(/([\d.]+)\s*%/) || [])[1]) : NaN;
  const percent = stats ? Number(stats.percent) || 0 : (isNaN(parsed) ? 0 : parsed);
  const level = !stats ? 'off'
    : (alert && alert.level === 'danger') ? 'danger'
      : (alert || percent >= 80) ? 'warn'
        : 'ok';

  /* Copias: parte del uso ocupada por las tareas (bytes medidos por el backend). */
  const bytes = copies && copies.bytes ? Math.min(Number(copies.bytes), Number(stats.total) || 0) : 0;
  const copiesPercent = stats && Number(stats.total) ? (bytes / Number(stats.total)) * 100 : 0;
  const otherBytes = copies ? Math.max(0, (Number(stats.used) || 0) - bytes) : 0;

  const ring = el('div', { class: 'disk-ring' });
  ring.innerHTML = diskGauge(percent, level, copiesPercent);
  ring.appendChild(el('div', { class: 'disk-center' }, [
    el('span', { class: 'disk-percent', text: stats ? percent.toFixed(1) + '%' : '—' }),
    el('span', { class: 'disk-caption', text: stats ? 'usado' : 'sin datos' }),
  ]));

  const bars = [];
  if (copies && bytes) {
    bars.push(el('div', {
      class: 'progress-bar disk-bar-copies',
      style: 'width:' + Math.max(0, Math.min(100, copiesPercent)).toFixed(2) + '%',
      title: 'Copias de seguridad: ' + fmtGb(bytes) + (copies.partial ? ' (medición parcial)' : ''),
    }));
  }
  bars.push(el('div', {
    class: 'progress-bar ' + (level === 'danger' ? 'bg-danger' : level === 'warn' ? 'bg-warning' : level === 'off' ? 'bg-secondary' : 'bg-success'),
    style: 'width:' + Math.max(0, Math.min(100, percent - (copies && bytes ? copiesPercent : 0))).toFixed(2) + '%',
    title: copies && bytes ? 'Otro uso del NAS' : 'Uso del NAS',
  }));
  const bar = el('div', { class: 'progress disk-bar', role: 'progressbar', 'aria-valuenow': String(percent), 'aria-valuemin': '0', 'aria-valuemax': '100' }, bars);

  let legend = null;
  if (stats) {
    const keys = [];
    if (copies) {
      keys.push(el('span', {
        class: 'disk-key disk-key-copies',
        title: (copies.partial ? 'Medición parcial: alguna tarea no tiene tamaño calculado. ' : '') +
          (copies.files ? copies.files.toLocaleString('es-CO') + ' archivos en las tareas' : 'Bytes medidos por las tareas'),
      }, [
        el('i'), el('b', { text: (copies.partial ? '≥ ' : '') + fmtGb(bytes) }),
        document.createTextNode(' en copias'),
      ]));
      keys.push(el('span', { class: 'disk-key disk-key-other' }, [
        el('i'), el('b', { text: fmtGb(otherBytes) }), document.createTextNode(' otro uso'),
      ]));
    } else {
      keys.push(el('span', {}, [el('b', { text: fmtGb(stats.used) }), document.createTextNode(' usados')]));
    }
    keys.push(el('span', { class: 'disk-key disk-key-free' }, [
      el('i'), el('b', { text: fmtGb(stats.free) }), document.createTextNode(' libres'),
    ]));
    keys.push(el('span', { class: 'disk-key disk-key-total' }, [
      el('b', { text: fmtGb(stats.total) }), document.createTextNode(' total'),
    ]));
    legend = el('div', { class: 'disk-legend' }, keys);
  }

  let note = 'NAS no disponible';
  if (stats) {
    if (alert) {
      note = (level === 'danger' ? 'Espacio crítico: ' : level === 'warn' ? 'Aviso: ' : '') + alert.text;
    } else {
      note = 'NAS operativo: ' + percent.toFixed(1) + '% usado';
    }
  } else if (alert) {
    note = alert.text;
  }

  return el('div', { class: 'disk-card disk-' + level }, [
    ring,
    el('div', { class: 'disk-body' }, [
      el('div', { class: 'disk-title' }, [
        el('i', { class: 'fa-solid fa-hard-drive' }),
        el('span', { text: 'Almacenamiento NAS' }),
      ]),
      bar,
      legend,
      el('div', { class: 'disk-note' }, [
        el('i', { class: level === 'off' ? 'fa-solid fa-plug-circle-xmark' : (level === 'ok' ? 'fa-solid fa-circle-check' : 'fa-solid fa-triangle-exclamation') }),
        el('span', { text: note }),
      ]),
    ]),
  ]);
}

/* Tamaño de las copias: lo que el backend ya midió para las tareas. */
function copiesInfo(summary, state) {
  if (summary.bytes_total === null || summary.bytes_total === undefined) return null;
  const bytes = Number(summary.bytes_total) || 0;
  if (!bytes) return null;  /* sin mediciones: leyenda clásica de usados/libres */
  const jobs = (state && state.jobs) || [];
  const measured = jobs.filter((job) => job.size && job.size.bytes !== null && job.size.bytes !== undefined);
  return { bytes, files: Number(summary.files_total) || 0, partial: jobs.length > 0 && measured.length < jobs.length };
}

/* El disco NAS se muestra siempre, tenga o no un aviso asociado. */
function renderNas(summary, state) {
  const box = $('nas-info');
  if (!box) return;
  const alerts = summary.alerts || [];
  const alert = alerts.filter((item) => item.kind === 'nas')[0] || null;
  const stats = summary.nas_stats || null;
  const copies = stats ? copiesInfo(summary, state) : null;
  box.replaceChildren(nasCard(alert, stats, copies));
}

function seriesPoints(series, slug) {
  const byStamp = {};
  series.forEach((point) => {
    if (slug && point.job_slug !== slug) return;
    const key = point.taken_at;
    if (!key) return;
    byStamp[key] = (byStamp[key] || 0) + Number(point.bytes || 0);
  });
  return Object.keys(byStamp).sort().map((key) => ({ t: key, v: byStamp[key] }));
}

function renderCharts(state) {
  const series = state.series || [];
  const select = $('chart-job');
  const jobs = state.jobs || [];
  if (select && select.options.length === 0) {
    select.appendChild(el('option', { value: '', text: 'Total' }));
    jobs.forEach((job) => select.appendChild(el('option', { value: job.slug, text: job.name })));
  }
  const slug = select ? select.value : '';
  const sizeBox = $('chart-size');
  if (sizeBox) {
    sizeBox.innerHTML = series.length
      ? sparkline(seriesPoints(series, slug))
      : emptyChart('aún no hay snapshots de tamaño');
  }

  const durations = jobs
    .filter((job) => job.last_run && job.last_run.duration_s)
    .map((job) => ({ label: job.name, value: job.last_run.duration_s }))
    .sort((a, b) => b.value - a.value);
  const durationBox = $('chart-duration');
  if (durationBox) {
    durationBox.innerHTML = durations.length
      ? barList(durations, fmtDuration)
      : emptyChart('sin ejecuciones recientes');
  }
}

/* KPIs, disco NAS y gráficos: se pueden repintar sin tocar la tabla de jobs. */
function renderSummary(state) {
  const summary = state.summary || { counts: {} };
  renderKpis(summary);
  renderNas(summary, state);
  renderCharts(state);
}

/* ------------------------------ tabla de jobs ------------------------------ */

const COLSPAN = 7;

/* Barra de progreso de una copia en curso (dentro de la celda Estado).
   Solo se dibuja si el porcentaje es real; si no, el tiempo va en el badge. */
function progressBar(job) {
  const p = job.progress || {};
  const value = Math.max(0, Math.min(100, Number(p.percent) || 0));
  const fill = el('div', {
    class: 'progress-bar',
    style: 'width:' + value + '%',
    text: value + '%',
  });
  const bar = el('div', {
    class: 'progress progress-cell', role: 'progressbar',
    'aria-valuenow': String(value), 'aria-valuemin': '0', 'aria-valuemax': '100',
    title: p.detail || 'progreso medido',
    'data-live': '1',
  }, [fill]);
  return el('div', { class: 'progress-wrap' }, [bar]);
}

/* Tiempo transcurrido de una copia en curso: el del servidor o, si falta, el que se
   deduce del arranque de la última corrida (para poder refrescarlo con el reloj). */
function elapsedLabel(job) {
  const p = job.progress || {};
  if (p.elapsed_s !== null && p.elapsed_s !== undefined) return fmtDuration(p.elapsed_s);
  const started = job.last_run && job.last_run.started_at;
  if (!started) return '';
  const seconds = Math.round((Date.now() - new Date(started).getTime()) / 1000);
  return isFinite(seconds) && seconds >= 0 ? fmtDuration(seconds) : '';
}

/* Badge EN CURSO: añade el % solo si es real; si no, el tiempo transcurrido; con
   ninguno de los dos queda el estado a secas. */
function runningBadge(job) {
  const p = job.progress || {};
  const hasPercent = p.real === true && p.percent !== null && p.percent !== undefined;
  const suffix = hasPercent ? ' · ' + Math.round(Number(p.percent)) + '%'
    : (elapsedLabel(job) ? ' · ' + elapsedLabel(job) : '');
  const started = job.last_run ? (job.last_run.started_at || '') : '';
  const badge = el('span', {
    class: 'badge rounded-pill pill-live pill-' + statusClass(job.status),
    text: statusLabel(job.status) + suffix,
    'data-started': started,
    'data-real': hasPercent ? '1' : '0',
  });
  if (job.status_detail) badge.title = job.status_detail;
  return badge;
}

function statusCell(job) {
  const p = job.progress || {};
  const running = p.live || job.status === 'EN_CURSO';
  const cell = el('td', {}, [running ? runningBadge(job) : pill(job.status, job.status_detail)]);
  if (running && p.real === true && p.percent !== null && p.percent !== undefined) {
    cell.appendChild(progressBar(job));
  }
  return cell;
}

function expandCell(job, expanded) {
  return el('td', { class: 'col-expand' }, [
    el('button', {
      type: 'button', class: 'btn btn-sm btn-link js-expand p-0', 'data-slug': job.slug,
      'aria-expanded': expanded ? 'true' : 'false',
      'aria-label': expanded ? 'Ocultar detalle' : 'Mostrar detalle',
      title: expanded ? 'Ocultar destino y programación' : 'Ver destino y programación',
    }, [el('i', { class: 'fa-solid fa-circle-' + (expanded ? 'minus' : 'plus') })]),
  ]);
}

function menuItem(action, icon, label, enabled, title) {
  const item = el('button', {
    type: 'button',
    class: 'dropdown-item' + (enabled ? '' : ' disabled'),
    'data-action': action,
    ...(enabled ? {} : { disabled: 'disabled' }),
  }, [el('i', { class: icon + ' me-2' }), el('span', { text: label })]);
  if (title) item.title = title;
  return item;
}

/* Menú ⋮ por fila con las acciones específicas de la tarea. */
function actionsCell(job, opts) {
  const idle = !job.running;
  const items = [
    menuItem('run', 'fa-solid fa-play', 'Ejecutar ahora', idle && opts.isAdmin,
      idle && opts.isAdmin ? 'mirror con --delete' : 'Requiere rol admin y tarea detenida'),
    menuItem('dry', 'fa-solid fa-flask', 'Dry-run', idle && opts.isAdmin,
      idle && opts.isAdmin ? 'ejecución de prueba (sin borrar)' : 'Requiere rol admin'),
  ];
  if (job.status === 'FALLO') {
    items.push(menuItem('retry', 'fa-solid fa-rotate-right', 'Reintentar', idle && opts.isAdmin,
      idle && opts.isAdmin ? 'reintenta la copia fallida' : 'Requiere rol admin'));
  }
  items.push(el('li', {}, [el('hr', { class: 'dropdown-divider' })]));
  items.push(menuItem('log', 'fa-solid fa-file-lines', 'Ver log', true));
  items.push(menuItem('history', 'fa-solid fa-clock-rotate-left', 'Historial', true));
  items.push(menuItem('files', 'fa-solid fa-folder-open', 'Archivos', true));
  const portalManaged = job.managed_by === 'portal' && opts.manageJobs;
  if (portalManaged) {
    // Un único editor autoritativo para las tareas del portal: evita que Ficha/Horario
    // (PATCH parcial) y Editar definición (PUT) desincronicen overlay, script y cron.
    items.push(el('li', {}, [el('hr', { class: 'dropdown-divider' })]));
    items.push(menuItem('edit-def', 'fa-solid fa-sliders', 'Editar definición', idle,
      idle ? 'Rutas, credenciales y script' : 'Requiere tarea detenida'));
    items.push(menuItem('delete', 'fa-solid fa-trash', 'Eliminar tarea', idle,
      idle ? 'Borra script, catálogo y cron (conserva histórico)' : 'Requiere tarea detenida'));
  } else {
    items.push(menuItem('ficha', 'fa-solid fa-id-card', 'Ficha', true));
    items.push(menuItem('schedule', 'fa-solid fa-calendar-days', 'Horario', opts.canManage,
      opts.canManage ? 'Editar horario' : 'Requiere admin y BACKUP_MANAGE_CRON=1'));
  }
  items.push(menuItem('toggle', job.enabled ? 'fa-solid fa-toggle-off' : 'fa-solid fa-toggle-on',
    job.enabled ? 'Deshabilitar' : 'Habilitar', opts.canManage && idle,
    opts.canManage ? 'Habilitar o deshabilitar la tarea' : 'Requiere admin y BACKUP_MANAGE_CRON=1'));

  return el('td', { class: 'text-end' }, [
    el('div', { class: 'dropdown jobs-actions' }, [
      el('button', {
        type: 'button', class: 'btn btn-sm btn-outline-secondary', 'data-bs-toggle': 'dropdown',
        'aria-expanded': 'false', 'aria-label': 'Acciones de ' + job.name,
      }, [el('i', { class: 'fa-solid fa-ellipsis-vertical' })]),
      el('ul', { class: 'dropdown-menu dropdown-menu-end' }, items),
    ]),
  ]);
}

function detailValue(label, children) {
  return el('div', { class: 'detail-field' }, [
    el('div', { class: 'muted small', text: label }),
    el('div', {}, children),
  ]);
}

/* Fila de detalle (tras el +): destino, programación y ficha secundaria. */
function jobDetailRow(job, expanded, opts) {
  const fields = [
    detailValue('Destino', [el('span', { class: 'mono', text: '/mnt/nas/' + (job.dest_rel || '') })]),
    detailValue('Programación', [
      el('code', { text: scheduleText(job) }),
      el('span', {
        class: 'badge rounded-pill ms-1 ' + (job.enabled ? 'pill-ok' : 'pill-unknown'),
        text: job.enabled ? 'habilitada' : 'deshabilitada',
      }),
    ]),
    detailValue('Origen', [el('span', { class: 'mono', text: (job.source || '—') + ' · ' + (job.origin_type || '') })]),
    detailValue('Responsable', [el('span', { text: job.owner || '—' })]),
    detailValue('Etiquetas', [el('span', { text: tagList(job.tags).join(', ') || '—' })]),
    detailValue('Retención', [el('span', { text: job.retention_days ? job.retention_days + ' días' : '—' })]),
  ];
  if (job.schedule_drift) {
    fields.push(el('div', { class: 'detail-field detail-warn' }, [
      el('i', { class: 'fa-solid fa-triangle-exclamation me-1' }),
      el('span', { text: 'La programación difiere del cron instalado' }),
    ]));
  }
  if (opts.isAdmin) {
    fields.push(el('div', { class: 'detail-field' }, [
      el('button', {
        type: 'button', class: 'btn btn-sm btn-outline-secondary', 'data-action': 'size',
        'data-slug': job.slug,
      }, [el('i', { class: 'fa-solid fa-ruler-combined me-1' }), el('span', { text: 'Medir tamaño' })]),
    ]));
  }
  return el('tr', {
    class: 'detail-row', 'data-detail': job.slug,
    ...(expanded ? {} : { hidden: 'hidden' }),
  }, [el('td', { colspan: String(COLSPAN) }, [el('div', { class: 'detail-grid' }, fields)])]);
}

function jobRow(job, opts) {
  const expanded = opts.expanded.has(job.slug);
  const checked = opts.checked.has(job.slug);
  const tr = el('tr', {
    class: 'clickable' + (checked ? ' selected-row' : '') + (expanded ? ' expanded-row' : ''),
    'data-slug': job.slug, tabindex: '0', 'aria-selected': checked ? 'true' : 'false',
    'aria-expanded': expanded ? 'true' : 'false',
  });

  tr.appendChild(expandCell(job, expanded));

  const tags = tagList(job.tags);
  const first = el('td', {}, [
    el('div', { class: 'fw-semibold' }, [
      el('span', { text: job.name }),
      el('span', {
        class: 'badge rounded-pill ms-1 ' + critClass(job.criticality),
        title: 'Criticidad: ' + (job.criticality || 'media'),
        text: job.criticality || 'media',
      }),
    ]),
    el('div', { class: 'muted mono', text: job.slug + ' · ' + (job.origin_type || '') }),
  ]);
  if (tags.length) first.appendChild(el('div', { class: 'muted small', text: tags.join(', ') }));
  tr.appendChild(first);

  tr.appendChild(statusCell(job));

  const size = job.size || {};
  tr.appendChild(el('td', {}, [
    el('div', { text: size.bytes === null || size.bytes === undefined ? '—' : fmtGb(size.bytes) }),
    el('div', { class: 'muted small', text: size.disabled ? 'sin medir (deshabilitada)' :
      (size.pending ? 'midiendo…' : (size.error ? size.error : (size.files || 0) + ' archivos')) }),
  ]));

  const last = job.last_run;
  tr.appendChild(el('td', {}, [
    el('div', { text: last ? relTime(last.started_at) : '—', title: last ? fmtDate(last.started_at) : '' }),
    el('div', { class: 'muted small', text: last ? fmtDuration(last.duration_s) + ' · ' +
      (last.files_transferred || 0) + ' arch.' : '' }),
  ]));
  tr.appendChild(el('td', {}, [
    el('div', { text: job.next_run ? relTimeFuture(job.next_run) : '—' }),
    el('div', { class: 'muted small', text: job.next_run ? fmtDate(job.next_run) : '' }),
  ]));

  tr.appendChild(actionsCell(job, opts));
  return tr;
}

/* --------------------------- selección y acciones -------------------------- */

/* El Panel (dashboard) solo muestra las copias activas: las deshabilitadas siguen
   visibles y gestionables en las demás vistas (Programación, Historial, Archivos). */
function panelJobs(state) {
  const jobs = Array.isArray(state.jobs) ? state.jobs : [];
  return jobs.filter((job) => !!job.enabled);
}

function filteredRows(state) {
  const jobs = panelJobs(state);
  const term = String(state.jobsSearch || '').trim().toLowerCase();
  return term
    ? jobs.filter((job) => [job.name, job.slug, job.tags, job.owner, job.notes]
      .join(' ').toLowerCase().includes(term))
    : jobs;
}

function applySelection() {
  if (!ctxRef) return;
  const { checked, expanded } = ctxRef.state;
  document.querySelectorAll('#jobs-body tr[data-slug]').forEach((tr) => {
    const slug = tr.getAttribute('data-slug');
    tr.classList.toggle('selected-row', checked.has(slug));
    tr.classList.toggle('expanded-row', expanded.has(slug));
    tr.setAttribute('aria-selected', checked.has(slug) ? 'true' : 'false');
    tr.setAttribute('aria-expanded', expanded.has(slug) ? 'true' : 'false');
  });
  document.querySelectorAll('#jobs-body tr.detail-row[data-detail]').forEach((tr) => {
    tr.hidden = !expanded.has(tr.getAttribute('data-detail'));
  });
}

function updateBulkBar() {
  if (!ctxRef) return;
  const state = ctxRef.state;
  const isAdmin = !!(state.user && state.user.role === 'admin');
  const canManage = isAdmin && !!state.manageCron;
  const chosen = state.checked.size;
  const filtered = filteredRows(state);

  const newButton = $('jobs-new');
  if (newButton) newButton.hidden = !(isAdmin && state.manageJobs);

  const label = $('jobs-selected');
  if (label) {
    label.innerHTML = '';
    label.appendChild(el('i', { class: 'fa-solid fa-hand-pointer me-1' }));
    label.appendChild(el('span', {
      text: chosen
        ? chosen + ' seleccionada' + (chosen === 1 ? '' : 's')
        : 'Sin selección · clic en una fila, Ctrl/Mayús para varias',
    }));
  }

  const allowed = {
    run: isAdmin && chosen > 0,
    retry: isAdmin && chosen > 0,
    dry: isAdmin && chosen > 0,
    size: isAdmin && chosen > 0,
    toggle: canManage && chosen > 0,
    'export-csv': chosen > 0 || filtered.length > 0,
  };
  document.querySelectorAll('#jobs-actionbar button[data-bulk]').forEach((button) => {
    const action = button.getAttribute('data-bulk');
    if (action === 'all' || action === 'none') {
      button.disabled = filtered.length === 0;
      return;
    }
    button.disabled = !allowed[action];
  });
}

/* Estado del lote serializado (cola del backend). */
function renderBatch(state) {
  const box = $('jobs-batch');
  if (!box) return;
  const b = state.batch;
  if (!b || !b.active) {
    box.hidden = true;
    box.textContent = '';
    return;
  }
  const pending = (b.pending || []).length;
  const done = (b.done || []).length;
  const total = pending + done + (b.running ? 1 : 0);
  const parts = [];
  if (b.running) parts.push('ejecutando ' + b.running);
  if (pending) parts.push(pending + ' en cola');
  parts.push(done + '/' + total + ' completadas');
  box.hidden = false;
  box.textContent = 'Lote ' + (b.action || '') + ': ' + parts.join(' · ');
}

/* El estado vacío dice POR QUÉ está vacío: cargando, error de API, catálogo
   vacío o filtro sin coincidencias (con botón para quitarlo). */
function reasonCell(message, actionLabel, action) {
  const box = el('div', { class: 'empty-state' }, [el('span', { text: message })]);
  if (action) {
    const button = el('button', {
      type: 'button', class: 'btn btn-sm btn-outline-secondary', text: actionLabel,
    });
    button.addEventListener('click', action);
    box.appendChild(button);
  }
  return el('td', { colspan: String(COLSPAN), class: 'empty' }, [box]);
}

function clearFilter() {
  if (!ctxRef) return;
  ctxRef.state.jobsSearch = '';
  const box = $('jobs-search');
  if (box) box.value = '';
  renderJobs(ctxRef.state);
}

function renderJobs(state) {
  const body = $('jobs-body');
  if (!body) return;
  const count = $('jobs-count');
  const allJobs = Array.isArray(state.jobs) ? state.jobs : [];
  const jobs = panelJobs(state);
  const rows = filteredRows(state);

  // La selección y el detalle solo se conservan si la tarea sigue existiendo.
  const slugs = new Set(jobs.map((job) => job.slug));
  [...state.checked].forEach((slug) => { if (!slugs.has(slug)) state.checked.delete(slug); });
  [...state.expanded].forEach((slug) => { if (!slugs.has(slug)) state.expanded.delete(slug); });
  if (state.anchor && !slugs.has(state.anchor)) state.anchor = '';

  const fragment = document.createDocumentFragment();
  const filtered = state.loaded && jobs.length > 0 && rows.length === 0;
  let message = '';
  if (state.loading) {
    message = 'Cargando tareas…';
  } else if (!state.loaded) {
    const failures = (state.apiErrors || []).map((item) => item.endpoint + ' → ' + item.message).join('; ');
    message = failures
      ? 'No se pudieron cargar las tareas (' + failures + ').'
      : 'No se pudieron cargar las tareas: el servidor no devolvió la lista.';
  } else if (!allJobs.length) {
    message = 'El catálogo no tiene tareas (/etc/cron.d/backupcsr + jobs.yml).';
  } else if (!jobs.length) {
    message = 'No hay copias activas. Las deshabilitadas están en Programación.';
  } else if (filtered) {
    message = 'Sin coincidencias para «' + String(state.jobsSearch || '').trim() + '».';
  }

  if (message) {
    fragment.appendChild(el('tr', {}, [
      reasonCell(message, filtered ? 'Quitar filtro' : '', filtered ? clearFilter : null),
    ]));
  } else {
    const opts = {
      isAdmin: !!(state.user && state.user.role === 'admin'),
      canManage: !!(state.user && state.user.role === 'admin') && !!state.manageCron,
      manageJobs: !!(state.user && state.user.role === 'admin') && !!state.manageJobs,
      checked: state.checked,
      expanded: state.expanded,
    };
    rows.forEach((job) => {
      try {
        fragment.appendChild(jobRow(job, opts));
        fragment.appendChild(jobDetailRow(job, opts.expanded.has(job.slug), opts));
      } catch (err) {
        console.error('jobRow', job && job.slug, err);
        fragment.appendChild(el('tr', {}, [
          reasonCell('No se pudo dibujar la tarea "' + (job && job.slug ? job.slug : '?') + '".'),
        ]));
      }
    });
  }

  // Sustitución atómica: la tabla nunca queda a medias.
  body.replaceChildren(fragment);
  if (count) count.textContent = state.loaded ? String(rows.length) : '—';
  applySelection();
  updateBulkBar();
  renderBatch(state);
}

function toggleExpanded(slug) {
  const set = ctxRef.state.expanded;
  if (set.has(slug)) set.delete(slug); else set.add(slug);
  applySelection();
}

/* Selección estilo DataTables: clic simple, Ctrl/Cmd+clic alterna, Mayús+clic rango. */
function selectRow(slug, event) {
  const state = ctxRef.state;
  const rows = filteredRows(state);
  const index = rows.findIndex((job) => job.slug === slug);
  const additive = !!(event && (event.ctrlKey || event.metaKey));
  const range = !!(event && event.shiftKey);
  const anchorIndex = state.anchor ? rows.findIndex((job) => job.slug === state.anchor) : -1;
  const set = state.checked;
  if (range && anchorIndex >= 0 && index >= 0) {
    const [from, to] = anchorIndex <= index ? [anchorIndex, index] : [index, anchorIndex];
    set.clear();
    for (let i = from; i <= to; i += 1) set.add(rows[i].slug);
  } else if (additive) {
    if (set.has(slug)) set.delete(slug); else set.add(slug);
    state.anchor = slug;
  } else {
    set.clear();
    set.add(slug);
    state.anchor = slug;
  }
  applySelection();
  updateBulkBar();
}

function setAllChecked(on) {
  const rows = filteredRows(ctxRef.state);
  const set = ctxRef.state.checked;
  set.clear();
  if (on) rows.forEach((job) => set.add(job.slug));
  ctxRef.state.anchor = on && rows.length ? rows[0].slug : '';
  applySelection();
  updateBulkBar();
}

/* Los valores que Excel/LibreOffice evalúan como fórmula se neutralizan con "'". */
const CSV_RISKY = /^[=+\-@\t\r]/;

function csvCell(value) {
  let text = String(value === null || value === undefined ? '' : value);
  if (CSV_RISKY.test(text)) text = "'" + text;
  return /[",\n;]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

function exportCsv(jobs) {
  const head = ['slug', 'nombre', 'estado', 'progreso', 'bytes', 'ultima', 'proxima', 'destino', 'habilitada'];
  const lines = [head.join(',')];
  jobs.forEach((job) => {
    const p = job.progress || {};
    const progress = (p.real && p.percent !== null && p.percent !== undefined)
      ? p.percent + '%' : (elapsedLabel(job) || '');
    lines.push([
      job.slug, job.name, job.status,
      progress,
      job.size_bytes === null || job.size_bytes === undefined ? '' : job.size_bytes,
      job.last_run ? job.last_run.started_at : '',
      job.next_run || '', job.dest_rel || '', job.enabled ? '1' : '0',
    ].map(csvCell).join(','));
  });
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: 'copias-' + new Date().toISOString().slice(0, 10) + '.csv' });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function handleAction(action, slug) {
  const job = (ctxRef.state.jobs || []).filter((item) => item.slug === slug)[0];
  if (!job) {
    toast('Tarea no encontrada', 'warn');
    return;
  }
  try {
    if (action === 'run' || action === 'dry' || action === 'retry') {
      const dry = action === 'dry';
      const retry = action === 'retry';
      const message = retry
        ? 'Reintentar "' + job.name + '" AHORA? El mirror usa --delete.'
        : 'Ejecutar "' + job.name + '" AHORA? El mirror usa --delete y puede borrar en el NAS.';
      const launch = async () => {
        await ctxRef.api.post('/jobs/' + slug + '/run', { dry_run: dry, retry });
        toast('Ejecución lanzada: ' + job.name + (dry ? ' (dry-run)' : ''), 'info');
        setTimeout(() => ctxRef.refresh(true), 1500);
      };
      if (dry) await launch();
      else confirmDialog(retry ? 'Reintentar copia' : 'Ejecutar copia', message, launch,
        retry ? 'Reintentar' : 'Ejecutar', 'btn-warning');
    } else if (action === 'size') {
      await ctxRef.api.post('/jobs/' + slug + '/size/refresh', {});
      toast('Midiendo ' + job.name + '…', 'info');
      setTimeout(() => ctxRef.refresh(true), 1500);
    } else if (action === 'toggle') {
      await ctxRef.api.patch('/jobs/' + slug, { enabled: !job.enabled });
      toast((job.enabled ? 'Deshabilitada: ' : 'Habilitada: ') + job.name, 'ok');
      await ctxRef.refresh(true);
    } else if (action === 'log') {
      await ctxRef.openLog(slug, job.name);
    } else if (action === 'history') {
      ctxRef.state.histJob = slug;
      ctxRef.go('historial');
    } else if (action === 'files') {
      ctxRef.state.filesJob = slug;
      ctxRef.go('archivos');
    } else if (action === 'ficha') {
      ctxRef.openSchedule(job, 'general');
    } else if (action === 'schedule') {
      ctxRef.openSchedule(job, 'programacion');
    } else if (action === 'edit-def') {
      ctxRef.openEditJob(job);
    } else if (action === 'delete') {
      confirmDialog('Eliminar tarea',
        'Eliminar "' + job.name + '"? Se borra el script, el catálogo y su línea de cron; '
        + 'el histórico de ejecuciones se conserva.', async () => {
          try {
            await ctxRef.api.del('/jobs/' + slug);
            toast('Tarea eliminada: ' + job.name, 'ok');
            await ctxRef.refresh(true);
          } catch (err) {
            toast('Error: ' + err.message, 'err');
          }
        }, 'Eliminar', 'btn-danger');
    }
  } catch (err) {
    toast('Error: ' + err.message, 'err');
  }
}

async function handleBulk(action) {
  const state = ctxRef.state;
  const isAdmin = !!(state.user && state.user.role === 'admin');
  const canManage = isAdmin && !!state.manageCron;
  const rows = filteredRows(state);

  if (action === 'all') { setAllChecked(true); return; }
  if (action === 'none') { setAllChecked(false); return; }

  const selected = rows.filter((job) => state.checked.has(job.slug));
  if (action === 'export-csv') {
    exportCsv(selected.length ? selected : rows);
    return;
  }
  if (!selected.length) {
    toast('Selecciona al menos una tarea', 'warn');
    return;
  }
  const names = selected.length + ' tarea' + (selected.length === 1 ? '' : 's');

  try {
    if (action === 'run' || action === 'dry' || action === 'retry') {
      if (!isAdmin) { toast('Requiere rol admin', 'err'); return; }
      if (action === 'retry' && !selected.some((job) => job.status === 'FALLO')) {
        toast('No hay tareas fallidas en la selección', 'warn');
        return;
      }
      const launch = async () => {
        const res = await ctxRef.api.post('/jobs/batch', {
          action: action, slugs: selected.map((job) => job.slug),
        });
        const queued = (res.queued || []).length;
        const skipped = (res.skipped || []).length;
        toast(queued + ' en cola' + (skipped ? ' · ' + skipped + ' omitidas' : ''), 'info');
        state.batch = await ctxRef.api.get('/jobs/batch');
        renderBatch(state);
      };
      if (action === 'dry') { await launch(); return; }
      const title = action === 'retry' ? 'Reintentar copias' : 'Ejecutar copias';
      const verb = action === 'retry' ? 'Reintentar' : 'Ejecutar';
      confirmDialog(title, verb + ' ' + names + '? El mirror usa --delete y puede borrar en el NAS.',
        launch, verb, 'btn-warning');
      return;
    }
    if (action === 'size') {
      if (!isAdmin) { toast('Requiere rol admin', 'err'); return; }
      const res = await ctxRef.api.post('/jobs/batch', {
        action: 'size', slugs: selected.map((job) => job.slug),
      });
      toast((res.queued || []).length + ' mediciones en cola', 'info');
      state.batch = await ctxRef.api.get('/jobs/batch');
      renderBatch(state);
      return;
    }
    if (action === 'toggle') {
      if (!canManage) { toast('Requiere admin y BACKUP_MANAGE_CRON=1', 'err'); return; }
      const enable = selected.some((job) => !job.enabled);
      const label = enable ? 'Habilitar' : 'Deshabilitar';
      confirmDialog(label + ' tareas', label + ' ' + names + '?', async () => {
        let ok = 0;
        let fail = 0;
        for (const job of selected) {
          try {
            await ctxRef.api.patch('/jobs/' + job.slug, { enabled: enable });
            ok += 1;
          } catch (err) {
            fail += 1;
          }
        }
        toast((enable ? 'Habilitadas: ' : 'Deshabilitadas: ') + ok + (fail ? ' · fallos: ' + fail : ''),
          fail ? 'warn' : 'ok');
        await ctxRef.refresh(true);
      }, label, enable ? 'btn-success' : 'btn-warning');
    }
  } catch (err) {
    toast('Error: ' + err.message, 'err');
  }
}

/* El badge EN CURSO refresca el tiempo transcurrido con el reloj local entre
   refrescos del servidor (el % solo se muestra si es real y llega medido). */
function startTicker() {
  if (tickTimer) return;
  tickTimer = setInterval(() => {
    document.querySelectorAll('#jobs-body .pill-live[data-started]').forEach((badge) => {
      if (badge.getAttribute('data-real') === '1') return;
      const started = badge.getAttribute('data-started');
      if (!started) return;
      const elapsed = Math.round((Date.now() - new Date(started).getTime()) / 1000);
      if (!isFinite(elapsed) || elapsed < 0) return;
      badge.textContent = statusLabel('EN_CURSO') + ' · ' + fmtDuration(elapsed);
    });
  }, 2000);
}

export const panel = {
  id: 'panel',
  admin: false,
  mount(ctx) {
    ctxRef = ctx;
    startTicker();
    const body = $('jobs-body');
    if (body) {
      body.addEventListener('click', (event) => {
        const expandButton = event.target.closest('button.js-expand');
        if (expandButton) {
          event.preventDefault();
          const row = expandButton.closest('tr[data-slug]');
          toggleExpanded(expandButton.getAttribute('data-slug') ||
            (row && row.getAttribute('data-slug')));
          return;
        }
        const actionButton = event.target.closest('button[data-action]');
        if (actionButton) {
          if (actionButton.disabled) return;
          event.preventDefault();
          const row = actionButton.closest('tr');
          const slug = actionButton.getAttribute('data-slug') ||
            (row && row.getAttribute('data-slug'));
          handleAction(actionButton.getAttribute('data-action'), slug);
          return;
        }
        if (event.target.closest('button, a, input, .dropdown-menu, .dropdown-toggle')) return;
        const row = event.target.closest('tr[data-slug]');
        if (row) selectRow(row.getAttribute('data-slug'), event);
      });
      body.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        if (event.target.closest('button, a, input, .dropdown-menu')) return;
        const row = event.target.closest('tr[data-slug]');
        if (!row) return;
        event.preventDefault();
        if (event.key === 'Enter') toggleExpanded(row.getAttribute('data-slug'));
        else selectRow(row.getAttribute('data-slug'), event);
      });
    }
    const bar = $('jobs-actionbar');
    if (bar) {
      bar.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-bulk]');
        if (button && !button.disabled) handleBulk(button.getAttribute('data-bulk'));
      });
    }
    // El filtro de #jobs-search lo gobierna shell.js (setupFilters), que además
    // descarta el autofill del navegador.
    const selectJob = $('chart-job');
    if (selectJob) selectJob.addEventListener('change', () => renderCharts(ctxRef.state));
    const newButton = $('jobs-new');
    if (newButton) newButton.addEventListener('click', () => ctxRef.openNewJob());
  },
  render(ctx) {
    ctxRef = ctx;
    renderSummary(ctx.state);
    renderJobs(ctx.state);
  },
  renderSummary(state) { renderSummary(state); },
  renderCharts(state) { renderCharts(state); },
  renderJobs(state) { renderJobs(state); },
  renderBatch(state) { renderBatch(state); },
};
