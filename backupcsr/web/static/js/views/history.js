/* Vista Historial: corridas filtrables, detalle y exportación. */
import { $, el, pill, fmtDate, fmtDuration, fmtGb, toast } from '../ui.js';
import { goToLogin } from '../api.js';

let ctxRef = null;

function filters() {
  const job = $('hist-job').value;
  const status = $('hist-status').value;
  const desde = $('hist-desde').value;
  const hasta = $('hist-hasta').value;
  const params = new URLSearchParams();
  if (job) params.set('job', job);
  if (status) params.set('status', status);
  if (desde) params.set('desde', desde);
  if (hasta) params.set('hasta', hasta);
  return params;
}

function ensureOptions(state) {
  const select = $('hist-job');
  if (select.options.length <= 1) {
    (state.jobs || []).forEach((job) => select.appendChild(el('option', { value: job.slug, text: job.name })));
  }
  if (state.histJob) {
    select.value = state.histJob;
    state.histJob = '';
  }
}

async function load() {
  const params = filters();
  params.set('limit', '200');
  try {
    const data = await ctxRef.api.get('/runs?' + params.toString());
    const body = $('hist-body');
    body.innerHTML = '';
    if (!data.runs.length) {
      body.appendChild(el('tr', {}, [el('td', { colspan: '8', class: 'empty', text: 'Sin ejecuciones.' })]));
      return;
    }
    data.runs.forEach((run) => {
      const tr = el('tr', { class: 'clickable' });
      tr.appendChild(el('td', { class: 'mono', text: run.job_slug || '—' }));
      tr.appendChild(el('td', { text: fmtDate(run.started_at) }));
      tr.appendChild(el('td', { text: fmtDate(run.finished_at) }));
      tr.appendChild(el('td', {}, [pill(run.status, run.error_text)]));
      tr.appendChild(el('td', { text: fmtDuration(run.duration_s) }));
      tr.appendChild(el('td', { text: run.size_bytes === null || run.size_bytes === undefined
        ? '—' : fmtGb(run.size_bytes) }));
      tr.appendChild(el('td', { text: String(run.files_transferred || 0) + ' / ' + String(run.files_removed || 0) }));
      tr.appendChild(el('td', { text: run.dry_run ? 'dry-run' : 'real' }));
      if (run.id) tr.addEventListener('click', () => ctxRef.openRun(run.id));
      body.appendChild(tr);
    });
  } catch (err) {
    toast('No se pudo cargar el historial: ' + err.message, 'err');
  }
}

// Descarga vía blob: si el endpoint no existe (backend viejo) o falla, se avisa
// con un toast en lugar de navegar a una página 404 y perder el portal.
async function exportRuns(format) {
  const params = filters();
  params.set('format', format);
  params.set('limit', '20000');
  try {
    const res = await fetch('/api/runs/export?' + params.toString(), { credentials: 'same-origin' });
    if (res.status === 401) {
      goToLogin();
      return;
    }
    if (!res.ok) {
      let detail = 'HTTP ' + res.status;
      try {
        const data = await res.json();
        if (data && typeof data.detail === 'string') detail = data.detail;
      } catch (e) { /* respuesta sin JSON */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = el('a', {
      href: url,
      download: 'runs-' + new Date().toISOString().slice(0, 10) + '.' + (format === 'csv' ? 'csv' : 'json'),
    });
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (err) {
    toast('No se pudo exportar (' + String(format).toUpperCase() + '): ' + err.message, 'err');
  }
}

export const history = {
  id: 'historial',
  admin: false,
  mount(ctx) {
    ctxRef = ctx;
    $('hist-filter').addEventListener('submit', (event) => { event.preventDefault(); load(); });
    $('hist-refresh').addEventListener('click', load);
    $('hist-export-csv').addEventListener('click', () => exportRuns('csv'));
    $('hist-export-json').addEventListener('click', () => exportRuns('json'));
  },
  render(ctx) {
    ctxRef = ctx;
    ensureOptions(ctx.state);
    load();
  },
  load,
};
