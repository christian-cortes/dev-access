/* UPS dashboard: live state, animated battery, history charts and events. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const svg = (body) => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const ICONS = {
    battery: svg('<rect x="2" y="7" width="16" height="10" rx="2"/><path d="M22 11v2"/><path d="M6 10v4M10 10v4"/>'),
    bolt: svg('<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>'),
    gauge: svg('<path d="M12 14 8 8"/><path d="M3.5 18a9 9 0 1 1 17 0"/>'),
    plug: svg('<path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0V8Z"/><path d="M12 17v5"/>'),
    server: svg('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
    database: svg('<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>'),
    shield: svg('<path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6l-7-3Z"/>'),
    alert: svg('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>'),
    clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    power: svg('<path d="M12 3v9"/><path d="M6.4 6.4a8 8 0 1 0 11.2 0"/>'),
    grid: svg('<path d="M9 2v20M15 2v20M3 8h18M3 16h18"/>'),
    usb: svg('<path d="M12 21V7"/><path d="m8 11 4-4 4 4"/><circle cx="12" cy="4" r="2"/>'),
    check: svg('<path d="M20 6 9 17l-5-5"/>'),
    x: svg('<path d="M18 6 6 18M6 6l12 12"/>'),
  };
  const icon = (name) => `<span class="ico" aria-hidden="true">${ICONS[name] || ""}</span>`;
  const onBattery = (status) => /(^|\s)(OB|DISCHRG|LB)(\s|$)/.test((status || "").toUpperCase());

  const LIVE_RANGES = new Set(["1h", "6h", "24h"]);
  const MAX_POINTS = 900;
  const DEFAULT_METRICS = ["battery_charge", "battery_runtime", "ups_load", "input_voltage", "battery_voltage"];
  const FALLBACK_COLORS = {
    battery_charge: "#34d399",
    battery_runtime: "#60a5fa",
    ups_load: "#fbbf24",
    input_voltage: "#a78bfa",
    battery_voltage: "#22d3ee",
  };
  const FALLBACK_RANGES = ["1h", "6h", "24h", "7d", "30d"];

  const state = {
    meta: null,
    summary: null,
    range: "24h",
    charts: {},
    nominalPower: 900,
    wsConnected: false,
    view: "dashboard",
    lastStatus: null,
    reconnectStep: 0,
    theme: localStorage.getItem("nut-theme") || "dark",
    metricKeys: DEFAULT_METRICS,
    colors: Object.assign({}, FALLBACK_COLORS),
    energy: { price: 0, currency: "COP" },
    monthCursor: null,
    earliestMonth: null,
  };

  function statusFromTokens(status) {
    const s = (status || "").toUpperCase();
    if (!s) return { label: "DESCONOCIDO", className: "muted", icon: "x" };
    const tokens = new Set(s.split());
    if (tokens.has("OB") || tokens.has("DISCHRG")) return { label: "EN BATERÍA", className: "warn", icon: "battery" };
    if (tokens.has("LB")) return { label: "BATERÍA BAJA", className: "bad", icon: "battery" };
    if (tokens.has("OFF") || tokens.has("FSD")) return { label: "SIN CONEXIÓN", className: "bad", icon: "power" };
    if (tokens.has("OVER") || tokens.has("ALARM")) return { label: "CRÍTICO", className: "bad", icon: "alert" };
    if (tokens.has("BOOST") || tokens.has("TRIM") || tokens.has("BYPASS")) return { label: "ADVERTENCIA", className: "warn", icon: "alert" };
    return { label: "EN LÍNEA", className: "ok", icon: "check" };
  }

  function fmtTime(ts, range) {
    const d = new Date(ts * 1000);
    if (range === "1h") return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (range === "6h" || range === "24h") return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleString([], { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  const fmtFull = (ts) => new Date(ts * 1000).toLocaleString();
  function relTime(ts) {
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 5) return "ahora";
    if (s < 60) return `hace ${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `hace ${m} min`;
    const h = Math.floor(m / 60);
    if (h < 24) return `hace ${h} h`;
    return `hace ${Math.floor(h / 24)} d`;
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("nut-theme", theme);
    state.theme = theme;
    Object.values(state.charts).forEach((c) => {
      if (!c || !c.options) return;
      c.options.scales.x.ticks.color = cssVar("--muted");
      c.options.scales.y.ticks.color = cssVar("--muted");
      c.options.scales.x.grid.color = cssVar("--line");
      c.options.scales.y.grid.color = cssVar("--line");
      c.options.plugins.legend.labels.color = cssVar("--muted");
      c.update("none");
    });
  }

  function updateBattery(s) {
    const charge = Number(s.battery_charge);
    const fill = $("battFill");
    if (Number.isFinite(charge)) {
      const low = s.charge_low ?? 10;
      const warn = s.charge_warning ?? 50;
      fill.style.height = clamp(charge, 0, 100) + "%";
      fill.classList.toggle("bad", charge <= low);
      fill.classList.toggle("warn", charge > low && charge <= warn);
      fill.classList.toggle("ok", charge > warn);
      $("battPct").textContent = Math.round(charge) + "%";
    }
    const onBattery = (s.status || "").includes("OB") || (s.status || "").includes("LB");
    $("batt").classList.toggle("pulse", onBattery || s.status_class === "bad");
    $("runtime").textContent = s.runtime_human || "--:--:--";
    $("runtimeSub").textContent = s.watts_est ? `Basado en consumo actual de ${Math.round(s.watts_est)} W` : "Basado en consumo actual.";
  }

  function renderMarks(s) {
    const marks = $("battMarks");
    marks.innerHTML = "";
    const thresholds = [
      [s.charge_low ?? 10, "baja"],
      [s.charge_warning ?? 50, "alerta"],
    ];
    thresholds.forEach(([value, label]) => {
      const el = document.createElement("div");
      el.className = "mark";
      const span = document.createElement("span");
      span.textContent = label;
      el.appendChild(span);
      el.style.bottom = clamp(value, 0, 100) + "%";
      marks.appendChild(el);
    });
  }

  const KPI_DEFS = [
    { key: "battery_charge", label: "Batería", unit: "%", decimals: 0, bar: "floor", accent: "battery_charge", icon: "battery", sub: (s) => (s.battery_charge != null ? `${s.battery_charge.toFixed(0)}% disponible` : "Estado de batería") },
    { key: "load", label: "Carga", unit: "%", decimals: 0, bar: "ceil", barWarn: 70, barBad: 90, accent: "ups_load", icon: "gauge", sub: (s) => (s.load != null ? `${s.load.toFixed(0)}% de capacidad` : "Carga actual") },
    { key: "watts_est", label: "Potencia", unit: "W", decimals: 0, accent: "ups_load", icon: "bolt", sub: (s) => (s.realpower_nominal ? `${s.realpower_nominal} W nominal` : "Consumo") },
    { key: "input_voltage", label: "Entrada", unit: "V", decimals: 1, accent: "input_voltage", icon: "plug", sub: (s) => (s.transfer_low && s.transfer_high ? `${s.transfer_low}–${s.transfer_high} V de rango` : "Voltaje de entrada") },
  ];

  function valueFor(def, s) {
    if (def.key === "load") return s.load;
    return s[def.key];
  }

  function renderKpis() {
    const host = $("kpis");
    host.innerHTML = "";
    KPI_DEFS.forEach((def, i) => {
      const card = document.createElement("div");
      card.className = "k";
      card.id = `kpi-${def.key}`;
      card.style.animationDelay = `${i * 35}ms`;

      const label = document.createElement("span");
      label.className = "k-label";
      if (def.icon) {
        const ic = document.createElement("span");
        ic.className = "k-ico";
        ic.innerHTML = ICONS[def.icon] || "";
        if (def.accent) ic.style.color = state.colors[def.accent] || cssVar("--accent");
        label.appendChild(ic);
      }
      label.appendChild(document.createTextNode(def.label));

      const wrap = document.createElement("div");
      wrap.className = "k-value-wrap";
      const val = document.createElement("span");
      val.dataset.val = "";
      val.textContent = "—";
      wrap.appendChild(val);
      if (def.unit) {
        const unit = document.createElement("span");
        unit.className = "k-unit";
        unit.textContent = def.unit;
        wrap.appendChild(unit);
      }

      const sub = document.createElement("span");
      sub.className = "k-sub";
      sub.dataset.sub = "";

      card.append(label, wrap, sub);
      if (def.bar) {
        const bar = document.createElement("div");
        bar.className = "bar";
        const fill = document.createElement("span");
        fill.dataset.bar = "";
        bar.appendChild(fill);
        card.appendChild(bar);
      }
      host.appendChild(card);
    });
  }

  function setNumber(el, value, decimals) {
    if (typeof value !== "number" || !isFinite(value)) return;
    const from = typeof el._val === "number" ? el._val : 0;
    const to = value;
    el._val = to;
    const start = performance.now();
    const tick = (now) => {
      const p = Math.min(1, (now - start) / 650);
      el.textContent = (from + (to - from) * p).toFixed(decimals || 0);
      if (p < 1) requestAnimationFrame(tick);
    };
    tick(start);
  }

  function updateKpis(s) {
    KPI_DEFS.forEach((def) => {
      const card = $("kpi-" + def.key);
      if (!card) return;
      const value = valueFor(def, s);
      const valEl = card.querySelector("[data-val]");
      if (typeof value === "number") setNumber(valEl, value, def.decimals);
      else valEl.textContent = "—";

      const subEl = card.querySelector("[data-sub]");
      if (subEl) subEl.textContent = typeof def.sub === "function" ? def.sub(s) : "";

      if (def.bar) {
        const fill = card.querySelector("[data-bar]");
        const pct = clamp(Number(value) || 0, 0, 100);
        let cls = "ok";
        if (pct >= (def.barBad ?? 90)) cls = "bad";
        else if (pct >= (def.barWarn ?? 70)) cls = "warn";
        fill.classList.remove("ok", "warn", "bad");
        fill.classList.add(cls);
        requestAnimationFrame(() => { fill.style.width = pct + "%"; });
      }
    });
  }

  function updateHeader(s) {
    $("model").textContent = s.model || "UPS";
    const bits = [s.mfr, s.firmware ? `fw ${s.firmware}` : null, s.serial].filter(Boolean);
    $("sub").textContent = bits.join(" · ") || "UPS";

    const status = statusFromTokens(s.status || s.status_label);
    const badge = $("statusBadge");
    badge.dataset.class = status.className;
    $("statusText").textContent = status.label;
    const statusIcon = $("statusIcon");
    if (statusIcon) statusIcon.innerHTML = ICONS[status.icon] || "";
    document.body.dataset.status = status.className;
    $("statusLabel").textContent = status.label;
    $("powerFlowState").textContent = status.label;
    $("lastSeen").textContent = s.ts ? relTime(s.ts) : "—";
    $("footMeta").textContent = s.model ? `${s.model} · ${status.label}` : "—";

    const usbState = s.ts && !s.stale ? "Conectado" : "Desconectado";
    $("usbState").textContent = usbState;
    $("nutState").textContent = s.ts && !s.stale ? "En marcha" : "Desconectado";
    const pveState = $("pveState");
    if (pveState) pveState.textContent = serviceState((s.services || {}).pve).text;
  }

  function updatePowerFlow(s) {
    const status = statusFromTokens(s.status || s.status_label);
    const gridValue = s.input_voltage != null ? `${Number(s.input_voltage).toFixed(0)} V` : "—";
    const batteryValue = s.battery_charge != null ? `${Number(s.battery_charge).toFixed(0)}%` : "—";
    const loadValue = s.watts_est != null ? `${Math.round(s.watts_est)} W` : (s.load != null ? `${Number(s.load).toFixed(0)}%` : "—");

    $("flowGrid").textContent = gridValue;
    $("flowBattery").textContent = batteryValue;
    $("flowLoad").textContent = loadValue;

    $("powerFlow").dataset.mode = status.className;
    const powerFlowState = $("powerFlowState");
    powerFlowState.textContent = status.label;
    powerFlowState.style.color = status.className === "bad" ? cssVar("--bad") : status.className === "warn" ? cssVar("--warn") : cssVar("--ok");
  }

  function serviceState(svc) {
    if (!svc || !svc.state || svc.state === "unknown") return { text: "No monitoreado", cls: "muted" };
    if (svc.state === "online" || svc.state === "auth") return { text: "En línea", cls: "ok" };
    return { text: "Caído", cls: "bad" };
  }

  function renderSystemStatus(s) {
    const status = statusFromTokens(s.status || s.status_label);
    const svc = s.services || {};
    const pve = serviceState(svc.pve);
    const pbs = serviceState(svc.pbs);
    const storage = serviceState(svc.storage);
    const nutOk = Boolean(s.ts && !s.stale);
    const items = [
      ["battery", "UPS", status.label, status.className],
      ["server", "NUT", nutOk ? "En marcha" : "Desconectado", nutOk ? "ok" : "bad"],
      ["server", "PVE", pve.text, pve.cls],
      ["database", "PBS", pbs.text, pbs.cls],
      ["shield", "Copias", storage.text, storage.cls],
    ];

    const host = $("systemStatus");
    host.innerHTML = items.map(([ic, name, value, cls]) =>
      `<li>${icon(ic)}<span class="ss-name">${name}</span><span class="state ${cls}">${value}</span></li>`
    ).join("");
  }

  function renderHealth(s) {
    const status = statusFromTokens(s.status || s.status_label);
    const wrap = $("healthState");
    const pill = wrap.querySelector(".health-pill");
    const summary = $("healthSummary");
    wrap.classList.remove("warning", "bad");

    const noData = !s.ts || s.stale;
    const runtimeLow = typeof s.runtime_s === "number" && typeof s.runtime_low === "number" && s.runtime_s <= s.runtime_low;
    const chargeLow = typeof s.battery_charge === "number" && s.charge_low != null && s.battery_charge <= s.charge_low;

    if (noData || status.className === "bad" || runtimeLow || chargeLow) {
      wrap.classList.add("bad");
      pill.textContent = "CRÍTICO";
      summary.textContent = noData
        ? "Sin comunicación con la UPS."
        : runtimeLow
          ? "Autonomía por debajo del mínimo configurado."
          : chargeLow
            ? "Batería en nivel crítico."
            : "La UPS está degradada o desconectada.";
    } else if (status.className === "warn" || (s.error && s.error.message)) {
      wrap.classList.add("warning");
      pill.textContent = "ADVERTENCIA";
      summary.textContent = "La UPS opera con batería o con carga elevada.";
    } else {
      pill.textContent = "SALUDABLE";
      summary.textContent = "La UPS funciona con normalidad.";
    }
  }

  function renderDiagnostics(s) {
    const host = $("diagnostics");
    const rows = [
      ["Modelo", s.model || "—"],
      ["Fabricante", s.mfr || "—"],
      ["N.º de serie", s.serial || "—"],
      ["Firmware", s.firmware || "—"],
      ["Voltaje de batería", s.battery_voltage != null ? `${Number(s.battery_voltage).toFixed(1)} V` : "—"],
      ["Voltaje nominal", s.input_voltage_nominal ? `${Number(s.input_voltage_nominal).toFixed(0)} V` : "—"],
      ["Voltaje de entrada", s.input_voltage != null ? `${Number(s.input_voltage).toFixed(0)} V` : "—"],
      ["Transferencia baja", s.transfer_low != null ? `${Number(s.transfer_low).toFixed(0)} V` : "—"],
      ["Transferencia alta", s.transfer_high != null ? `${Number(s.transfer_high).toFixed(0)} V` : "—"],
      ["Potencia nominal", s.realpower_nominal ? `${s.realpower_nominal} W` : "—"],
      ["Driver", s.driver_name ? `${s.driver_name}${s.driver_version ? ` ${s.driver_version}` : ""}` : "—"],
      ["USB vendor / product", s.usb_vendorid || s.usb_productid ? `${s.usb_vendorid || "—"} / ${s.usb_productid || "—"}` : "—"],
      ["Estado", s.status_label || "Desconocido"],
    ];
    host.innerHTML = rows.map(([label, value]) => `
      <div class="diag-item">
        <span>${label}</span>
        <strong>${value}</strong>
      </div>
    `).join("");
  }

  function toast(kind, title, msg) {
    const host = $("toasts");
    if (!host) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.setAttribute("role", "status");
    el.innerHTML = `${icon(kind === "ok" ? "check" : "alert")}<div class="toast-body"><strong>${title}</strong><span>${msg}</span></div>`;
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, 6000);
  }

  function detectTransitions(s) {
    const cur = (s.status || "").toUpperCase();
    const prev = state.lastStatus;
    if (prev !== null && cur && cur !== prev) {
      const nowBat = onBattery(cur);
      const wasBat = onBattery(prev);
      if (nowBat && !wasBat) toast("warn", "Corte de energía", "La UPS cambió a batería.");
      else if (!nowBat && wasBat) toast("ok", "Energía restaurada", "La UPS volvió a la red eléctrica.");
      if (/(^|\s)LB(\s|$)/.test(cur) && !/(^|\s)LB(\s|$)/.test(prev)) {
        toast("bad", "Batería baja", "Autonomía limitada.");
      }
    }
    if (cur) state.lastStatus = cur;
  }

  function updateSummary(s) {
    detectTransitions(s);
    state.summary = s;
    state.nominalPower = s.realpower_nominal || state.nominalPower;
    updateHeader(s);
    updateBattery(s);
    renderMarks(s);
    updateKpis(s);
    updatePowerFlow(s);
    renderSystemStatus(s);
    renderHealth(s);
    renderDiagnostics(s);

    const banner = $("banner");
    if (s.stale) {
      banner.className = "banner";
      banner.textContent = `Sin datos recientes${s.last_seen ? ` · última lectura ${fmtFull(s.last_seen)}` : ""}.`;
    } else if (s.error && s.error.message) {
      banner.className = "banner error";
      banner.textContent = `Error al leer la UPS: ${s.error.message}`;
    } else {
      banner.className = "banner hidden";
    }
  }

  const thresholdPlugin = {
    id: "thresholds",
    afterDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea, scales } = chart;
      [[opts.low, cssVar("--bad")], [opts.warn, cssVar("--warn")]].forEach(([value, color]) => {
        if (value == null || !scales.y) return;
        const y = scales.y.getPixelForValue(value);
        if (y < chartArea.top || y > chartArea.bottom) return;
        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(chartArea.left, y);
        ctx.lineTo(chartArea.right, y);
        ctx.stroke();
        ctx.restore();
      });
    },
  };
  Chart.register(thresholdPlugin);

  function baseOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 650, easing: "easeOutQuart" },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: cssVar("--muted"), boxWidth: 12, boxHeight: 12, usePointStyle: true } },
        tooltip: {
          backgroundColor: cssVar("--panel-strong"),
          borderColor: cssVar("--line"),
          borderWidth: 1,
          titleColor: cssVar("--txt"),
          bodyColor: cssVar("--txt"),
          padding: 10,
          cornerRadius: 8,
        },
        thresholds: { low: null, warn: null },
      },
      scales: {
        x: { ticks: { color: cssVar("--muted"), maxTicksLimit: 6, autoSkip: true }, grid: { color: cssVar("--line") }, border: { display: false } },
        y: { ticks: { color: cssVar("--muted") }, grid: { color: cssVar("--line") }, border: { display: false }, beginAtZero: false },
      },
    };
  }

  function makeChart(id, datasets) {
    return new Chart($(id).getContext("2d"), {
      type: "line",
      data: { labels: [], datasets },
      options: baseOptions(),
    });
  }

  function ds(label, color) {
    return { label, data: [], borderColor: color, backgroundColor: color + "26", fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2, spanGaps: true };
  }

  function buildCharts() {
    const c = state.colors;
    state.charts.charge = makeChart("chartCharge", [ds("Batería", c.battery_charge)]);
    state.charts.runtime = makeChart("chartRuntime", [ds("Autonomía", c.battery_runtime)]);
    state.charts.load = makeChart("chartLoad", [ds("Consumo", c.ups_load)]);
    state.charts.volt = makeChart("chartVolt", [ds("Entrada", c.input_voltage), ds("Batería", c.battery_voltage)]);
    state.charts.month = makeMonthChart("chartMonth", c.ups_load);
  }

  function makeMonthChart(id, color) {
    const options = baseOptions();
    options.scales.x.ticks.maxTicksLimit = 12;
    options.scales.y.beginAtZero = true;
    options.plugins.tooltip.callbacks = {
      label: (ctx) => {
        const kwh = (ctx.chart.$kwh || [])[ctx.dataIndex];
        const w = ctx.parsed.y;
        return kwh == null ? `${w} W` : [`${w} W`, `≈ ${kwh.toFixed(2)} kWh`];
      },
    };
    return new Chart($(id).getContext("2d"), {
      type: "bar",
      data: {
        labels: [],
        datasets: [{
          label: "Consumo",
          data: [],
          backgroundColor: color + "59",
          borderColor: color,
          borderWidth: 1,
          borderRadius: 4,
          maxBarThickness: 26,
        }],
      },
      options,
    });
  }

  function align(times, points, transform) {
    const map = new Map((points || []).map((p) => [p[0], p[1]]));
    return times.map((t) => {
      if (!map.has(t)) return null;
      const v = map.get(t);
      return transform ? transform(v) : v;
    });
  }

  function downsample(times, series, max) {
    const n = times.length;
    if (n <= max) return { times, series };
    const step = Math.ceil(n / max);
    const idx = [];
    for (let i = 0; i < n; i += step) idx.push(i);
    const out = {};
    Object.keys(series).forEach((k) => (out[k] = idx.map((i) => series[k][i])));
    return { times: idx.map((i) => times[i]), series: out };
  }

  async function loadHistory() {
    const range = state.range;
    const keys = state.metricKeys;
    const results = await Promise.all(
      keys.map((m) => fetch(`/api/history?metric=${m}&range=${range}`).then((r) => r.json()).catch(() => ({ points: [] })))
    );
    const data = {};
    keys.forEach((m, i) => (data[m] = (results[i] && results[i].points) || []));
    const base = keys.reduce((a, m) => (data[m].length > data[a].length ? m : a), keys[0]);
    const times = (data[base] || []).map((p) => p[0]);
    const power = state.nominalPower;
    const raw = {
      battery_charge: align(times, data.battery_charge),
      battery_runtime: align(times, data.battery_runtime, (v) => v / 60),
      ups_load: align(times, data.ups_load, (v) => (v * power) / 100),
      input_voltage: align(times, data.input_voltage),
      battery_voltage: align(times, data.battery_voltage),
    };
    const picked = downsample(times, raw, MAX_POINTS);
    const labels = picked.times.map((t) => fmtTime(t, range));
    const C = state.charts;

    C.charge.data.labels = labels.slice();
    C.charge.data.datasets[0].data = picked.series.battery_charge;
    C.charge.options.plugins.thresholds = {
      low: state.summary?.charge_low ?? state.meta?.thresholds?.charge_low ?? 10,
      warn: state.summary?.charge_warning ?? state.meta?.thresholds?.charge_warning ?? 50,
    };

    C.runtime.data.labels = labels.slice();
    C.runtime.data.datasets[0].data = picked.series.battery_runtime;

    C.load.data.labels = labels.slice();
    C.load.data.datasets[0].data = picked.series.ups_load;

    C.volt.data.labels = labels.slice();
    C.volt.data.datasets[0].data = picked.series.input_voltage;
    C.volt.data.datasets[1].data = picked.series.battery_voltage;

    Object.values(C).forEach((c) => c && c.update());
    renderConsumptionStats(picked.series.ups_load);
    const total = picked.times.length;
    const resolution = results[0] && results[0].resolution === "hourly" ? "por hora" : "brutos";
    $("chartHint").textContent = total ? `${total} puntos · datos ${resolution}` : "Recopilando datos…";
  }

  function pushLive(s) {
    if (!LIVE_RANGES.has(state.range) || !state.charts.charge) return;
    const label = fmtTime(s.ts, state.range);
    const power = state.nominalPower;
    const series = [
      [state.charts.charge, [s.battery_charge]],
      [state.charts.runtime, [typeof s.runtime_s === "number" ? s.runtime_s / 60 : null]],
      [state.charts.load, [typeof s.load === "number" ? (s.load * power) / 100 : null]],
      [state.charts.volt, [s.input_voltage, s.battery_voltage]],
    ];
    series.forEach(([chart, vals]) => {
      const labels = chart.data.labels;
      if (labels[labels.length - 1] === label) return;
      labels.push(label);
      chart.data.datasets.forEach((d, i) => d.data.push(vals[i]));
      while (labels.length > MAX_POINTS) {
        labels.shift();
        chart.data.datasets.forEach((d) => d.data.shift());
      }
      chart.update();
    });
  }

  function describeTransition(from, to) {
    const f = (from || "").toUpperCase().split(/\s+/);
    const t = (to || "").toUpperCase().split(/\s+/);
    const has = (arr, tok) => arr.includes(tok);
    const fromBat = has(f, "OB") || has(f, "DISCHRG");
    const toBat = has(t, "OB") || has(t, "DISCHRG");
    if (has(t, "LB") && !has(f, "LB")) return { text: "Batería baja", cls: "bad" };
    if (toBat && !fromBat) return { text: "Corte de energía detectado", cls: "warn" };
    if (!toBat && fromBat) return { text: "Energía restaurada", cls: "ok" };
    if (has(t, "OVER")) return { text: "Sobrecarga", cls: "bad" };
    if (has(t, "TRIM") || has(t, "BOOST")) return { text: "Regulación de voltaje", cls: "warn" };
    return { text: `Cambio de estado a ${to || "—"}`, cls: "muted" };
  }

  function renderEvents(rows) {
    const host = $("events");
    host.textContent = "";
    if (!rows || !rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Sin eventos en este periodo.";
      host.appendChild(empty);
      return;
    }

    rows.forEach((e) => {
      const d = describeTransition(e.from_status, e.to_status);
      const row = document.createElement("div");
      row.className = `event ${d.cls}`;
      row.innerHTML = `${icon(d.cls === "ok" ? "check" : d.cls === "muted" ? "clock" : "alert")}
        <div class="event-body">
          <strong>${d.text}</strong>
          <span class="event-sub">${e.from_label || e.from_status || "—"} &rarr; ${e.to_label || e.to_status || "—"}</span>
        </div>
        <time>${fmtFull(e.ts)}</time>`;
      host.appendChild(row);
    });
  }

  async function loadEvents() {
    try {
      const res = await fetch(`/api/events?range=${state.range}`).then((r) => r.json());
      renderEvents(res.events);
    } catch (_) {
      renderEvents([]);
    }
  }

  function fmtDuration(seconds) {
    if (seconds == null) return "—";
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h) return `${h} h ${m} min`;
    if (m) return `${m} min ${sec} s`;
    return `${sec} s`;
  }

  function renderPowerEvents(rows, power) {
    const host = $("powerEvents");
    if (!host) return;
    host.textContent = "";
    if (!rows || !rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Sin cortes registrados en este periodo.";
      host.appendChild(empty);
      return;
    }
    rows.forEach((ev) => {
      const batt = ev.battery_start != null || ev.battery_end != null
        ? `Batería ${ev.battery_start != null ? Math.round(ev.battery_start) + "%" : "—"} &rarr; ${ev.battery_end != null ? Math.round(ev.battery_end) + "%" : "—"}`
        : "Batería: —";
      const load = power && ev.load_avg_pct != null ? `${Math.round((ev.load_avg_pct * power) / 100)} W promedio` : "";
      const card = document.createElement("div");
      card.className = `power-event${ev.ongoing ? " ongoing" : ""}`;
      card.innerHTML = `${icon(ev.ongoing ? "alert" : "power")}
        <div class="event-body">
          <strong>${ev.ongoing ? "Corte en curso" : "Corte de energía"}</strong>
          <span class="event-sub">${fmtFull(ev.started_at)}${ev.ongoing ? "" : ` · ${fmtDuration(ev.duration_s)}`}</span>
          <span class="event-sub">${batt}${load ? ` · ${load}` : ""}</span>
        </div>`;
      host.appendChild(card);
    });
  }

  async function loadPowerEvents() {
    try {
      const res = await fetch(`/api/power-events?range=${state.range}`).then((r) => r.json());
      const realPower = state.summary && state.summary.realpower_nominal;
      renderPowerEvents(res.events, realPower);
    } catch (_) {
      renderPowerEvents([]);
    }
  }

  function renderConsumptionStats(series) {
    const host = $("consumptionStats");
    if (!host) return;
    const values = (series || []).filter((v) => typeof v === "number" && isFinite(v));
    if (!values.length) {
      host.innerHTML = "";
      return;
    }
    const current = values[values.length - 1];
    const avg = values.reduce((a, b) => a + b, 0) / values.length;
    const peak = Math.max(...values);
    const chip = (label, value) => `<div class="cons-chip"><span>${label}</span><strong>${Math.round(value)} W</strong></div>`;
    host.innerHTML = chip("Actual", current) + chip("Media", avg) + chip("Pico", peak);
  }

  const pctToWatts = (pct) => (pct * state.nominalPower) / 100;
  const pctHoursToKwh = (pctHours) => (pctHours * state.nominalPower) / 100 / 1000;
  const monthStart = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
  const sameMonth = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth();

  function fmtMoney(value) {
    const { currency } = state.energy;
    try {
      return new Intl.NumberFormat("es-CO", { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
    } catch (_) {
      return `${Math.round(value).toLocaleString()} ${currency}`;
    }
  }

  function monthlyStatsFromPoints(points) {
    const byDay = new Map();
    (points || []).forEach((p) => {
      const ts = p[0];
      const avg = p[1];
      if (avg == null) return;
      const mn = p[2];
      const mx = p[3];
      const n = p[4] || 1;
      const day = new Date(ts * 1000);
      day.setHours(0, 0, 0, 0);
      const key = day.getTime();
      let acc = byDay.get(key);
      if (!acc) {
        acc = { day, sumAvgN: 0, sumN: 0, max: null, min: null, hours: 0, energy: 0 };
        byDay.set(key, acc);
      }
      acc.sumAvgN += avg * n;
      acc.sumN += n;
      acc.energy += avg;
      acc.hours += 1;
      if (mx != null) acc.max = acc.max == null ? mx : Math.max(acc.max, mx);
      if (mn != null) acc.min = acc.min == null ? mn : Math.min(acc.min, mn);
    });
    return [...byDay.values()]
      .sort((a, b) => a.day - b.day)
      .map((d) => {
        const avg = d.sumAvgN / d.sumN;
        return { ...d, avg, max: d.max == null ? avg : d.max, min: d.min == null ? avg : d.min };
      });
  }

  function renderMonthStats(days, summary, isCurrent) {
    const host = $("monthStats");
    if (!host) return;
    if (!days.length || !summary) {
      host.innerHTML = "";
      return;
    }
    const chip = (label, value) => `<div class="cons-chip"><span>${label}</span><strong>${value}</strong></div>`;
    const kwh = pctHoursToKwh(summary.energy);
    const cost = state.energy.price > 0 ? fmtMoney(kwh * state.energy.price) : "";
    let todayChip = "";
    if (isCurrent) {
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const todayAcc = days.find((d) => d.day.getTime() === today.getTime());
      todayChip = chip("Hoy", todayAcc ? `${Math.round(pctToWatts(todayAcc.avg))} W` : "—");
    }
    host.innerHTML =
      todayChip +
      chip("Media mes", `${Math.round(pctToWatts(summary.avg))} W`) +
      chip("Pico mes", `${Math.round(pctToWatts(summary.peak))} W`) +
      chip("Energía mes", `${kwh.toFixed(1)} kWh`) +
      (cost ? chip("Costo mes", cost) : "");
  }

  function updateMonthNav() {
    const current = monthStart(new Date());
    const cursor = state.monthCursor || current;
    const prev = $("monthPrev");
    const next = $("monthNext");
    if (prev) prev.disabled = state.earliestMonth ? cursor <= state.earliestMonth : false;
    if (next) next.disabled = cursor >= current;
  }

  function shiftMonth(delta) {
    const current = monthStart(new Date());
    const cursor = state.monthCursor || current;
    let target = new Date(cursor.getFullYear(), cursor.getMonth() + delta, 1);
    if (target > current) target = current;
    if (state.earliestMonth && target < state.earliestMonth) target = state.earliestMonth;
    state.monthCursor = target;
    loadMonthlyConsumption();
  }

  async function loadMonthlyConsumption() {
    const chart = state.charts.month;
    if (!chart) return;
    const now = new Date();
    const current = monthStart(now);
    const cursor = state.monthCursor || current;
    state.monthCursor = cursor;
    const isCurrent = sameMonth(cursor, now);
    const since = Math.floor(cursor.getTime() / 1000);
    const monthEnd = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
    const until = Math.floor((isCurrent ? now : monthEnd).getTime() / 1000);
    let points = [];
    try {
      const res = await fetch(`/api/consumption/month?since=${since}&until=${until}`).then((r) => r.json());
      points = (res && res.points) || [];
    } catch (_) {
      points = [];
    }

    const title = $("monthTitle");
    if (title) title.textContent = `Consumo del mes · ${cursor.toLocaleDateString([], { month: "long", year: "numeric" })}`;
    const hint = $("monthHint");

    const days = monthlyStatsFromPoints(points);
    if (!days.length) {
      chart.data.labels = [];
      chart.data.datasets[0].data = [];
      chart.$kwh = [];
      chart.update();
      renderMonthStats([], null, isCurrent);
      if (hint) hint.textContent = isCurrent ? "Sin datos del mes en curso." : "Sin datos en este mes.";
      updateMonthNav();
      return;
    }

    const totalN = days.reduce((a, d) => a + d.sumN, 0);
    const summary = {
      avg: days.reduce((a, d) => a + d.sumAvgN, 0) / totalN,
      peak: Math.max(...days.map((d) => d.max)),
      min: Math.min(...days.map((d) => d.min)),
      energy: days.reduce((a, d) => a + d.energy, 0),
    };

    chart.data.labels = days.map((d) => String(d.day.getDate()).padStart(2, "0"));
    chart.data.datasets[0].data = days.map((d) => Math.round(pctToWatts(d.avg)));
    chart.$kwh = days.map((d) => pctHoursToKwh(d.energy));
    chart.update();

    renderMonthStats(days, summary, isCurrent);
    const priceTxt = state.energy.price > 0 ? ` · ${fmtMoney(state.energy.price)}/kWh` : "";
    if (hint) hint.textContent = `${days.length} días con datos · energía estimada con ${Math.round(state.nominalPower)} W nominales${priceTxt}`;
    updateMonthNav();
  }

  function renderRanges() {
    const host = $("ranges");
    host.textContent = "";
    (state.meta?.ranges || FALLBACK_RANGES).forEach((r) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = r;
      b.className = r === state.range ? "active" : "";
      b.addEventListener("click", () => {
        state.range = r;
        [...host.children].forEach((c) => c.classList.toggle("active", c.textContent === r));
        loadHistory();
        loadEvents();
        loadPowerEvents();
      });
      host.appendChild(b);
    });
  }

  function setMenu(open) {
    document.body.classList.toggle("menu-open", open);
    const btn = $("menuBtn");
    const scrim = $("menuScrim");
    if (btn) {
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "Cerrar menú" : "Abrir menú");
    }
    if (scrim) scrim.hidden = !open;
  }

  function showView(view) {
    setMenu(false);
    state.view = view;
    document.querySelectorAll("main [data-view]").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.view !== view);
    });
    document.querySelectorAll(".nav-link").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    if (view === "history") {
      Promise.all([loadHistory(), loadMonthlyConsumption()]).then(() =>
        Object.values(state.charts).forEach((c) => c && c.resize())
      );
    } else if (view === "events") {
      loadEvents();
      loadPowerEvents();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws;
    try {
      ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    } catch (_) {
      return;
    }
    ws.onopen = () => {
      state.wsConnected = true;
      state.reconnectStep = 0;
      $("nutState").textContent = "En marcha";
    };
    ws.onmessage = (ev) => {
      try {
        const s = JSON.parse(ev.data);
        updateSummary(s);
        pushLive(s);
      } catch (_) {}
    };
    ws.onclose = () => {
      state.wsConnected = false;
      $("nutState").textContent = "Desconectado";
      const delays = [2000, 5000, 10000, 30000];
      const delay = delays[Math.min(state.reconnectStep, delays.length - 1)];
      state.reconnectStep += 1;
      setTimeout(connect, delay);
    };
    ws.onerror = () => ws.close();
  }

  async function refreshSummary() {
    try {
      const s = await fetch("/api/summary").then((r) => r.json());
      if (s && s.ts) updateSummary(s);
      else {
        const b = $("banner");
        b.className = "banner";
        b.textContent = "Conectando con la UPS… esperando la primera lectura.";
      }
    } catch (_) {}
  }

  function tickLastSeen() {
    const seen = $("lastSeen");
    if (!state.summary || !state.summary.ts) return;
    seen.textContent = relTime(state.summary.ts);
  }

  async function init() {
    applyTheme(state.theme);
    try {
      state.meta = await fetch("/api/meta").then((r) => r.json());
    } catch (_) {
      state.meta = null;
    }
    if (state.meta?.metrics?.length) {
      state.metricKeys = state.meta.metrics.map((m) => m.key);
      state.meta.metrics.forEach((m) => {
        if (m.color) state.colors[m.key] = m.color;
      });
    } else {
      state.metricKeys = DEFAULT_METRICS;
    }
    if (state.meta?.energy) {
      const price = Number(state.meta.energy.price_per_kwh);
      if (Number.isFinite(price) && price > 0) state.energy.price = price;
      if (state.meta.energy.currency) state.energy.currency = state.meta.energy.currency;
    }
    state.monthCursor = monthStart(new Date());
    if (state.meta?.data_start) {
      state.earliestMonth = monthStart(new Date(state.meta.data_start * 1000));
    }

    renderKpis();
    buildCharts();
    renderRanges();
    document.querySelectorAll(".nav-link").forEach((b) => {
      b.addEventListener("click", () => showView(b.dataset.view));
    });
    const menuBtn = $("menuBtn");
    if (menuBtn) menuBtn.addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open")));
    const menuScrim = $("menuScrim");
    if (menuScrim) menuScrim.addEventListener("click", () => setMenu(false));
    const monthPrev = $("monthPrev");
    if (monthPrev) monthPrev.addEventListener("click", () => shiftMonth(-1));
    const monthNext = $("monthNext");
    if (monthNext) monthNext.addEventListener("click", () => shiftMonth(1));
    showView("dashboard");
    await Promise.all([refreshSummary(), loadHistory(), loadEvents(), loadPowerEvents(), loadMonthlyConsumption()]);
    connect();
    setInterval(() => { if (!state.wsConnected) refreshSummary(); }, 30000);
    setInterval(() => { loadHistory(); loadEvents(); loadMonthlyConsumption(); }, 300000);
    setInterval(tickLastSeen, 1000);
  }

  $("themeBtn").addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });
  document.addEventListener("DOMContentLoaded", init);
})();
