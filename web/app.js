'use strict';
/* Gate 3 digital twin – front end. Reads the JSON endpoints of dashboard.py; never writes anything. */

const $ = id => document.getElementById(id);
const fmt = n => Number(n).toLocaleString('en');
const pct = p => p == null ? '–' : (p < 0.001 ? '< 0.1 %' : (100 * p).toFixed(p < 0.1 ? 1 : 0) + ' %');
const signed = (v, d = 3) => (v > 0 ? '+' : '') + Number(v).toFixed(d);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// Colours: categorical slots of the validated reference palette; status colours are reserved for status.
const PAL = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];
const C = {ink2: '#52514e', grid: '#ebeae6', red: '#c0392b', green: '#1e8a4c', lock: '#6c3483', orange: '#e67e22', grey: '#9a9a96'};
const PCOL = {'3.5': PAL[0], '4': PAL[1], '4.5': PAL[2]};
const LVL = {H: PAL[0], M: PAL[1], L: PAL[2]};
const LVLNAME = {H: 'large', M: 'medium', L: 'small'};

/* ------------------------------------------------------------------ glossary + tooltips */
const GL = {
  depth: ['Insertion depth', 'How far the PVC tube is pushed into the bag port, measured by the camera after assembly. Target 15.0 mm.'],
  limits: ['Specification limits', 'A device is OK if its depth is between 13.0 and 17.0 mm (1.5 cm ± 0.2 cm). Outside = out of spec.'],
  expected: ['Expected depth', 'What the formula says the depth should be for this bag lot, tube reel and pressure if the machine is calibrated.'],
  residual: ['Difference', 'Measured minus expected depth. Around 0 on a healthy line; a steady trend means the line is drifting.'],
  drift: ['Drift (de-calibration)', 'The machine slowly pushes deeper or shallower over time, e.g. actuator wear or a camera moving.'],
  cusum: ['Drift alarm meter (CUSUM)', 'Adds up small differences that go the same way. Random noise cancels out; a real drift keeps growing. Above 5 = alarm.'],
  lock: ['Locked prediction', 'The device we predict will be the first out of spec. Saved once, with a timestamp and fingerprint, before that device is made. It can never be changed.'],
  early: ['Prediction result', 'HIT = exactly the right device. EARLY = the warning came before the real failure (safe side). LATE = after it. MISSED = the failure came before any prediction.'],
  falsealarm: ['False alarm', 'A prediction was locked but no device actually went out of spec.'],
  lead: ['Lead time (L)', 'L = X − Y: how many devices ahead of the last inspected device the locked prediction points. Jury metric, together with the error E = |Z − X|.'],
  jury: ['Jury score', 'X = predicted device, Y = last device inspected when the prediction was locked, Z = actual first failure. Error E = |Z − X| (smaller is better), lead time L = X − Y.'],
  rmse: ['Typical error (RMSE)', 'Root-mean-square difference between predicted and measured depth, in mm. Lower is better. About 0.17 mm is normal part-to-part noise.'],
  bias: ['Bias', 'Average of measured − predicted. Far from 0 means the model is systematically too high or too low, for example because the line drifted.'],
  r2: ['R²', 'Share of the depth variation the model explains. 1 = perfect, 0 = no better than always guessing the average.'],
  cpk: ['Cpk (process capability)', 'How many 3-sigma widths fit between the average depth and the nearest limit. 1.33 or more is usually required; 1.0 means about 0.13 % out of spec.'],
  ppm: ['ppm', 'Expected out-of-spec devices per million.'],
  ledger: ['Ledger', 'The tamper-proof database. Records can only be added, never changed or deleted.'],
  hash: ['Hash chain', 'Each record carries a SHA-256 fingerprint of itself and of the record before it. Changing anything breaks the chain and is detected.'],
  synthetic: ['Synthetic data', 'Real historical devices with an artificial drift added, used for testing because the real data never failed. Always labelled, never mixed with real records.'],
  setup: ['Setup change', 'A new bag lot, tube reel or pressure. The line pauses 60–130 s at each change.'],
  cv: ['Cross-validation', 'When training and test data are the same, the data is split into 5 parts and each part is predicted by a model trained on the other 4, so no device is predicted by a model that saw it.'],
  unseen: ['Unseen setting', 'A pressure, bag size or tube size left out of training, to test whether the model copes with something new. Real bags and pressures will not always match the training levels.'],
  followerr: ['Axis following error', 'Difference between where the insertion axis was told to stop and where it actually stopped.'],
  box: ['Explainability', 'White box: you can read exactly how it decides. Grey box: only partly. Black box: no human-readable logic, hard to validate under GxP.'],
};
const term = (key, text) => `<span class="term" data-term="${key}">${text ?? GL[key][0]}</span>`;
let MODEL = {};   // key -> {name, kind, box, desc}

const tip = $('tip');
document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-term],[data-model],[data-tip]');
  if (!el) { tip.style.display = 'none'; return; }
  let html = '';
  if (el.dataset.term && GL[el.dataset.term]) html = `<b>${GL[el.dataset.term][0]}</b><br>${GL[el.dataset.term][1]}`;
  else if (el.dataset.model && MODEL[el.dataset.model]) {
    const m = MODEL[el.dataset.model];
    html = `<b>${el.dataset.model} · ${esc(m.name)}</b><br>${esc(m.desc)}<br><i>${m.kind} · ${m.box} box</i>`;
  } else if (el.dataset.tip) html = el.dataset.tip;
  if (!html) return;
  tip.innerHTML = html; tip.style.display = 'block';
});
document.addEventListener('mousemove', e => {
  if (tip.style.display !== 'block') return;
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = e.clientY + 16 + tip.offsetHeight > window.innerHeight ? e.clientY - tip.offsetHeight - 10 : e.clientY + 16;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
const modelTag = (k, extra = '') => `<span data-model="${k}" class="term">${k} ${esc((MODEL[k] || {}).name || '')}</span>${extra}`;

/* ------------------------------------------------------------------ charts */
const charts = {};
function chart(id, cfg) {
  const el = $(id); if (!el || typeof Chart === 'undefined') return;
  if (charts[id]) { charts[id].data = cfg.data; charts[id].options = cfg.options; charts[id].update('none'); return; }
  charts[id] = new Chart(el, cfg);
}
function opts({x = '', y = '', legend = true, label = null, xmin, xmax, ymin, ymax, fixed = false} = {}) {
  return {
    animation: false, responsive: true, maintainAspectRatio: !fixed,
    plugins: {
      legend: {display: legend, labels: {boxWidth: 10, font: {size: 11}, filter: i => !!i.text}},
      tooltip: {callbacks: {label: label || (c => c.raw.u ? `${c.raw.u}: ${c.raw.y} mm` : `${c.dataset.label || ''} ${c.raw.y}`)}},
    },
    scales: {
      x: {type: 'linear', min: xmin, max: xmax, title: {display: !!x, text: x, font: {size: 11}}, grid: {color: C.grid}, ticks: {font: {size: 10}}},
      y: {min: ymin, max: ymax, title: {display: !!y, text: y, font: {size: 11}}, grid: {color: C.grid}, ticks: {font: {size: 10}}},
    },
    elements: {point: {radius: 1.6}},
  };
}
const hline = (y, x0, x1, color, label = '', dash = []) => ({label, data: [{x: x0, y}, {x: x1, y}], borderColor: color, borderWidth: 1.3, borderDash: dash, pointRadius: 0, pointHitRadius: 0, showLine: true});
const vline = (x, y0, y1, color, label = '', dash = [], w = 2) => ({label, data: [{x, y: y0}, {x, y: y1}], borderColor: color, borderWidth: w, borderDash: dash, pointRadius: 0, pointHitRadius: 0, showLine: true});
const pts = (xs, ys, extra = {}) => xs.map((x, i) => Object.assign({x, y: ys[i]}, typeof extra === 'function' ? extra(i) : extra));

/* ------------------------------------------------------------------ tabs */
const TAB_INTRO = {
  how: 'Start here: what the system does, the one measurement that matters, and the words used on this site.',
  live: 'What is happening on each assembly line right now, and which device is expected to be the first one out of spec.',
  data: 'Everything received from the simulator, how good the data is, and where the line came closest to failing.',
  lab: 'Train models on one dataset, test them on another, and see where each model predicts the first failure.',
  cmp: 'Results of comparing 14 prediction models on the real data, and why the simple formula stays in production.',
  audit: 'For the jury: trace any unit end to end, browse every stored record, re-verify the fingerprints and download everything.',
};
let cur = 'live';
function show(t, anchor) {
  if (!TAB_INTRO[t]) t = 'live';
  cur = t;
  document.querySelectorAll('main > section').forEach(s => s.classList.toggle('on', s.id === 'tab-' + t));
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  $('tabintro').textContent = TAB_INTRO[t];
  history.replaceState(null, '', '#' + t + (anchor ? '/' + anchor : ''));
  return Promise.resolve(({how: renderHow, live: renderLive, data: loadData, lab: initLab, cmp: loadCmp, audit: loadAudit})[t]());
}

/* "Show me" links: switch tab, wait for it to render, scroll to the element and highlight it. */
async function goto(tab, id, unit) {
  await show(tab, id);
  if (unit) await traceUnit(unit);
  await new Promise(r => setTimeout(r, 120));
  const el = $(id);
  if (!el) return;
  const target = ['CANVAS', 'TABLE', 'TBODY'].includes(el.tagName) || !el.offsetHeight ? (el.closest('.card') || el) : el;
  target.scrollIntoView({block: 'center'});
  target.classList.remove('flash'); void target.offsetWidth; target.classList.add('flash');
}
document.addEventListener('click', e => {
  const a = e.target.closest('[data-goto]');
  if (!a) return;
  e.preventDefault();
  const [tab, id, unit] = a.dataset.goto.split('|');
  goto(tab, id, unit);
});
const showme = (label, tab, id, unit = '') => `<a data-goto="${tab}|${id}|${unit}" href="#${tab}/${id}">→ ${label}</a>`;
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => show(b.dataset.tab));

/* ------------------------------------------------------------------ header */
const MODE_TXT = {
  HISTORICAL: 'Showing the real historical data downloaded from the simulator API.',
  SIM: 'Showing the synthetic drift runs (real devices + artificial drift), used for testing.',
  REALTIME: 'Showing live data received during the jury run.',
};
function renderHeader(s) {
  if (s.waiting) { $('hdr-pills').innerHTML = `<span class="pill" data-tip="Start monitor.py; this page fills in automatically.">Data: ${s.mode} · waiting for the first records…</span>`; return; }
  const ok = s.integrity.ok;
  $('hdr-pills').innerHTML =
    `<span class="pill" data-tip="${MODE_TXT[s.mode] || ''}">Data: ${s.mode}</span>` +
    `<span class="pill ${ok ? '' : 'bad'}" data-tip="${ok ? 'Every record was re-checked against its SHA-256 fingerprint and the chain is unbroken: nothing was changed or deleted.' : 'A record was changed outside the app: ' + esc(s.integrity.problem)}">` +
    `${ok ? '✓ Records intact' : '✖ INTEGRITY FAILURE'} · ${fmt(s.integrity.entries)} records</span>` +
    `<span class="pill" data-tip="Version of the frozen prediction formula. It is a fingerprint of the formula, so any change would give a new version.">Model ${s.model.version || '–'}</span>`;
}

/* ------------------------------------------------------------------ HOW IT WORKS */
async function renderHow() {
  $('glossary').innerHTML = Object.values(GL).map(([t, d]) => `<tr><th>${t}</th><td>${d}</td></tr>`).join('');
  $('urs-rerun').onclick = renderHow;
  $('urs-time').textContent = 'checking…';
  const [U, Q0] = await Promise.all([(await fetch('/api/urs')).json(), (await fetch('/api/quickunits')).json()]);
  if (U.error) { $('urs').innerHTML = `<tr><td class="note">${esc(U.error)}</td></tr>`; $('urs-time').textContent = ''; return; }
  const Q = Array.isArray(Q0) ? Q0 : [];
  const pick = k => (Q.find(q => q.kind === k) || {}).unit_id;
  const anyUnit = pick('PREDICTION') || pick('EVALUATION') || pick('EXTREME') || pick('FIRST');
  const lockLine = (Q.find(q => q.kind === 'PREDICTION') || {line: 'ASM-01'}).line;
  const hasPred = !!pick('PREDICTION');
  const HOW = {
    'URS-DI-001': ['Every stored record gets the unique ID <code>MODE/LINE/event number</code> (e.g. HISTORICAL/ASM-01/001179) plus its line and Unit ID. The database refuses a second record with the same ID.',
      [showme('Audit trail › one unit with its record ID', 'audit', 'trace-rec', anyUnit), showme('Audit trail › ledger browser', 'audit', 'ledger-card'),
       showme('Data explorer › traceability and sequence checks', 'data', 'd-checks')]],
    'URS-DI-002': ['Every API response is stored word for word before it is read, then each record separately. Records can only be added; a SHA-256 hash chain proves nothing changed. Source timestamps are kept as sent.',
      [showme('Audit trail › the raw API response of a unit', 'audit', 'trace-raw', anyUnit), showme('Audit trail › re-verify all fingerprints', 'audit', 'verify-card'),
       showme('Header › “Records intact” badge', 'live', 'hdr-pills'), showme('Data explorer › timestamps per source', 'data', 'd-timing')]],
    'URS-DI-003': ['A drift alarm meter (CUSUM) and a trend line watch the difference between measured and expected depth. On a drift, the first out-of-spec Unit ID is forecast and locked once, with line, predicted unit, current unit and UTC time.',
      [showme('Live monitor › drift alarm meter', 'live', 'c-' + lockLine), hasPred ? showme('Live monitor › locked prediction', 'live', 'lk-' + lockLine) : '',
       showme('Live monitor › system messages (alarms)', 'live', 'alerts'), U.mode !== 'SIM' ? showme('Data explorer › synthetic failure runs', 'data', 'd-sim-card') : '',
       showme('Model lab › where each model predicts the failure', 'lab', 'l-run')]],
    'URS-DI-004': ['Each unit record names its bag lot and tube reel, which link to their BAG-01 / PVC-01 measurements; predictions and inspection results are stored with line and Unit ID.',
      [showme('Audit trail › full chain for a unit', 'audit', 'trace-chain', anyUnit), showme('Data explorer › traceability checks', 'data', 'd-checks')]],
    'URS-DI-005': ['One append-only PostgreSQL ledger whose data volume survives restarts; the monitor resumes from it. This site connects with a read-only role and offers lookup, browsing, re-verification and a full export.',
      [showme('Audit trail › ledger browser', 'audit', 'ledger-card'), showme('Audit trail › verify and download export', 'audit', 'verify-card'),
       showme('Header › number of stored records', 'live', 'hdr-pills')]],
  };
  const pill = s => s === 'PASS' ? '<span class="pill ok">✓ PASS</span>' : s === 'FAIL' ? '<span class="pill bad">✖ FAIL</span>' : `<span class="pill nt">${s}</span>`;
  $('urs').innerHTML = '<tr><th>URS</th><th>requirement (verbatim)</th><th>ALCOA+</th><th>how the system meets it</th><th>live evidence</th><th>show me</th></tr>' +
    U.urs.map(u => `<tr><td><b>${u.id}</b><br>${pill(u.status)}</td><td class="req">${esc(u.text)}</td><td>${u.alcoa}</td>
      <td>${HOW[u.id][0]}</td><td><ul>${u.evidence.map(e => `<li>${esc(e)}</li>`).join('')}</ul></td>
      <td class="showme">${HOW[u.id][1].join('')}</td></tr>`).join('');
  const all = U.urs.every(u => u.status === 'PASS');
  $('urs-time').innerHTML = `${all ? '<span class="pill ok">all 5 PASS</span>' : ''} checked ${U.checked_utc.replace('T', ' ').slice(0, 19)} UTC on the ${U.mode} ledger`;
}

/* ------------------------------------------------------------------ LIVE */
let lastTip = -1, live = null, liveWin = 300;
async function tick(force) {
  if (document.hidden && !force) return;
  try {
    const s = await (await fetch('/api/state?tip=' + (force ? -1 : lastTip))).json();
    if (s.unchanged) return;
    lastTip = s.tip; live = s; renderHeader(s);
    if (cur === 'live') renderLive();
  } catch (e) { /* server restarting: try again on the next tick */ }
}
setInterval(tick, 2000);
document.querySelectorAll('[data-win]').forEach(b => b.onclick = () => {
  liveWin = +b.dataset.win;
  document.querySelectorAll('[data-win]').forEach(x => x.classList.toggle('on', x === b));
  renderLive();
});

const STATUS = {
  idle: ['idle', '○', 'Waiting'], ok: ['ok', '●', 'In control'], warn: ['warn', '▲', 'Drift detected'],
  lock: ['lock', '🔒', 'Prediction locked'], bad: ['bad', '✖', 'Failed'],
};
function resultText(e) {
  if (!e.predicted_unit_id) return 'No prediction had been locked before it (MISSED).';
  const jury = e.E_abs_error_units != null ? ` ${term('jury', 'Jury score')}: E = ${e.E_abs_error_units}, L = ${e.L_lead_units}.` : '';
  return resultText0(e) + jury;
}
function resultText0(e) {
  if (e.status === 'HIT') return `We had predicted <b>${e.predicted_unit_id}</b>: exactly right (HIT).`;
  if (e.error_units > 0) return `We had predicted <b>${e.predicted_unit_id}</b>, ${e.error_units} devices earlier: the warning came early, on the safe side (EARLY).`;
  return `We had predicted <b>${e.predicted_unit_id}</b>, ${-e.error_units} devices too late (LATE).`;
}
// Commit rule from model.json (holt_next: lock when P(next device fails) >= p_next_commit, predict lead_units ahead)
const rule = () => { const d = (live && live.model && live.model.detector) || {};
  return {holt: d.estimator === 'holt_next', p: d.p_next_commit ?? 0.1, lead: d.lead_units ?? 3}; };
function lineStatus(L) {
  const u = L.units, f = L.forecast, p = L.prediction, e = L.evaluation;
  if (!u.length) return {s: 'idle', text: 'No devices received from this line yet.'};
  if (e) return {s: 'bad', label: 'Failed · ' + (e.status || '').split(' ')[0],
    text: `First out-of-spec device: <b>${e.actual_first_failure_unit_id}</b> at ${e.actual_depth_mm} mm. ${resultText(e)}`};
  if (p) return {s: 'lock', text: `Predicted first out-of-spec device: <b>${p.predicted_unit_id}</b> (${p.predicted_failure_mode === 'TOO_DEEP' ? 'too deep' : 'too shallow'}), ` +
    `locked while ${p.current_unit_id} was being made. Waiting for the camera to confirm.`};
  if (f && f.drift_alarm) return {s: 'warn', text: `Depth is drifting <b>${f.slope_mm_per_unit > 0 ? 'deeper' : 'shallower'}</b> by about ` +
    `${Math.abs(f.slope_mm_per_unit * 100).toFixed(1)} mm per 100 devices. ` +
    (f.k_median ? `First out-of-spec device expected in about <b>${f.k_median}</b> devices.` : 'No out-of-spec device expected within 300 devices yet.')};
  return {s: 'ok', text: f ? `Depth is stable and inside the 13–17 mm limits. Chance that the next device is out of spec: <b>${pct(f.p_next)}</b>` +
    (rule().holt ? ` (prediction is locked at ${pct(rule().p)}).` : '.')
    : 'Collecting the first devices before the drift check can start…'};
}
function stat(v, l, t) { return `<div class="stat" data-tip="${t}"><div class="v">${v}</div><div class="l">${l}</div></div>`; }

function renderLive() {
  if (!live) return;
  if (!$('status-legend').innerHTML)
    $('status-legend').innerHTML = Object.entries({ok: 'stable, inside limits', warn: 'the alarm meter crossed 5', lock: 'first failure predicted and saved',
      bad: 'a device went out of spec', idle: 'no data yet'}).map(([k, d]) => `<span class="pill ${k}">${STATUS[k][1]} ${STATUS[k][2]}</span> <span class="note">${d}</span>`).join(' ');
  const host = $('lines');
  for (const line of ['ASM-01', 'ASM-02', 'ASM-03']) {
    const L = live.lines[line];
    if (!$('card-' + line)) host.insertAdjacentHTML('beforeend', `<div class="card linecard" id="card-${line}">
      <h2>${line} <span id="st-${line}"></span></h2><div class="summary" id="sum-${line}"></div><div class="stats" id="stats-${line}"></div>
      <canvas id="d-${line}" height="200"></canvas><div class="mes" id="mes-${line}"></div>
      <div><h3 data-term="cusum" class="term">Drift alarm meter</h3></div><canvas id="c-${line}" height="70"></canvas>
      <div id="lk-${line}"></div></div>`);
    if (!$('card-' + line).dataset.hover) {
      $('card-' + line).dataset.hover = 1;
      ['d-', 'c-'].forEach(p => $(p + line).addEventListener('mouseleave', () => clearHover(line)));
    }
    const st = lineStatus(L), [cls, icon, lab] = STATUS[st.s];
    $('st-' + line).innerHTML = `<span class="pill ${cls}">${icon} ${st.label || lab}</span>`;
    $('sum-' + line).innerHTML = st.text;
    const u = liveWin ? L.units.slice(-liveWin) : L.units, f = L.forecast, last = L.units[L.units.length - 1] || {};
    const ev = L.evaluation;
    $('stats-' + line).innerHTML = ev ?
      stat(ev.actual_first_failure_unit_id, 'first device out of spec', `Measured ${ev.actual_depth_mm} mm, outside the 13–17 mm limits.`) +
      stat(ev.predicted_unit_id || '–', 'our locked prediction', 'The device we had predicted, saved before it was made.') +
      stat(ev.error_units != null ? (ev.error_units === 0 ? 'exact' : Math.abs(ev.error_units) + (ev.error_units > 0 ? ' early' : ' late')) : 'missed', 'warning vs reality', 'How many devices before (early) or after (late) the real failure our prediction was.') +
      stat(ev.actual_depth_mm + ' mm', 'depth of that device', 'Measured insertion depth of the first out-of-spec device.')
      : stat(last.depth != null ? last.depth.toFixed(2) + ' mm' : '–', 'last device depth', 'Measured insertion depth of the most recent device. OK range 13.0–17.0 mm.') +
      stat(f ? signed(f.slope_mm_per_unit * 100, 1) + ' mm' : '–', 'drift per 100 devices', 'How fast the difference between measured and expected depth is changing. Near 0 = no drift.') +
      stat(f ? pct(f.p_next) : '–', 'chance next device fails', rule().holt
        ? `Probability that the next device will be outside 13–17 mm: expected depth for its bag, tube and pressure plus the smoothed recent difference (level and trend) and normal noise. At ${pct(rule().p)} the prediction is locked on the device ${rule().lead} ahead.`
        : 'Probability that the next device will be outside 13–17 mm, from the trend and the normal noise.') +
      stat(!f ? '–' : f.drift_alarm ? (f.k_median || '> 300') : 'none expected', 'devices until first failure',
        'Only estimated once drift is detected: the median of the drift forecast for the first out-of-spec device. For information; the lock uses the chance above.');
    $('card-' + line).querySelectorAll('canvas, h3').forEach(el => el.style.display = u.length ? '' : 'none');
    if (!u.length) continue;
    const x0 = u[0].event_no, xe = u[u.length - 1].event_no, fc = f && f.path ? f.path.slice(0, 40) : [], x1 = xe + (fc.length || 1);
    const p = L.prediction;
    const ds = [
      {label: 'device (OK)', data: u.filter(r => !r.nok).map(r => ({x: r.event_no, y: r.depth, u: r.unit_id})), backgroundColor: PAL[0], pointRadius: 1.8},
      {label: 'out of spec', data: u.filter(r => r.nok).map(r => ({x: r.event_no, y: r.depth, u: r.unit_id})), backgroundColor: C.red, pointRadius: 5},
      {label: 'expected depth', data: u.map(r => ({x: r.event_no, y: r.baseline})), borderColor: 'rgba(110,110,110,.55)', borderWidth: 1, pointRadius: 0, showLine: true},
      {label: 'forecast', data: fc.map(q => ({x: xe + q.k, y: q.mu})), borderColor: C.orange, borderDash: [5, 3], borderWidth: 2, pointRadius: 0, showLine: true},
      {label: 'forecast range', data: fc.map(q => ({x: xe + q.k, y: q.hi})), borderColor: 'rgba(230,126,34,.35)', borderWidth: 1, pointRadius: 0, showLine: true},
      {label: '', data: fc.map(q => ({x: xe + q.k, y: q.lo})), borderColor: 'rgba(230,126,34,.35)', borderWidth: 1, pointRadius: 0, showLine: true},
      hline(17, x0, x1, C.red, 'limits 13 / 17 mm'), hline(13, x0, x1, C.red),
    ];
    if (p) ds.push(vline(p.current_event_no + p.units_ahead, 12.5, 17.5, C.lock, 'locked prediction', [5, 3]));
    chart('d-' + line, {type: 'scatter', data: {datasets: ds}, options: hoverable(line, opts({x: 'device number', y: 'depth (mm)'}))});
    chart('c-' + line, {type: 'scatter', data: {datasets: [
      {label: 'pushing deeper', data: u.map(r => ({x: r.event_no, y: r.cusum_pos})), borderColor: C.red, borderWidth: 1.3, pointRadius: 0, showLine: true},
      {label: 'pushing shallower', data: u.map(r => ({x: r.event_no, y: r.cusum_neg})), borderColor: PAL[0], borderWidth: 1.3, pointRadius: 0, showLine: true},
      hline(5, x0, xe, C.grey, 'alarm level 5', [3, 3])]}, options: hoverable(line, opts({y: 'meter', ymin: 0, ymax: Math.min(20, Math.max(8, ...u.map(r => Math.max(r.cusum_pos, r.cusum_neg))))}))});
    $('lk-' + line).innerHTML = p ? `<div class="lockbox">🔒 <b>Locked prediction: ${p.predicted_unit_id}</b> will be the first device out of spec (${p.predicted_failure_mode === 'TOO_DEEP' ? 'too deep' : 'too shallow'}).<br>
      Saved at ${p.prediction_time_utc} while ${p.current_unit_id} was being made, ${p.units_ahead} devices ahead (${term('lead', 'lead time')} L = ${p.units_ahead}).<br>
      <span class="hash" data-tip="SHA-256 fingerprint of this prediction record. If anyone changed it, the fingerprint would no longer match.">fingerprint ${p.entry_hash}</span>
      ${L.evaluation ? `<br><b>Result:</b> ${resultText(L.evaluation)}` : ''}</div>` : '';
  }
  renderProduction();
  const M = live.materials;
  const bar = (id, obj, lo, hi, c, lab) => chart(id, {type: 'bar', data: {labels: Object.keys(obj), datasets: [{label: lab, data: Object.values(obj), backgroundColor: c, borderRadius: 3}]},
    options: {animation: false, plugins: {legend: {display: false}, title: {display: true, text: lab, font: {size: 12}}}, scales: {y: {min: lo, max: hi, grid: {color: C.grid}}, x: {ticks: {font: {size: 9}}}}}});
  bar('live-bag', M.bag, 0.173, 0.176, PAL[0], 'bag port size per lot (in) · axis = spec range');
  bar('live-pvc', M.pvc, 0.1675, 0.1725, PAL[2], 'tube size per reel (in) · axis = spec range');
  const AL = {DECALIBRATION_SUSPECTED: ['warn', 'Drift detected'], MATERIAL_UNKNOWN: ['warn', 'Unknown bag lot or tube reel'],
    UNIT_SEQUENCE_GAP: ['warn', 'Gap in device numbers'], TIME_NOT_MONOTONIC: ['bad', 'Time went backwards'],
    DATA_INCOMPLETE: ['bad', 'Missing data fields'], LINE_MISMATCH: ['bad', 'Record from another line']};
  $('alerts').innerHTML = live.alerts.length ? '<tr><th>line</th><th>device #</th><th>what</th><th>details</th></tr>' + live.alerts.slice().reverse().map(a => {
    const [c, t] = AL[a.code] || ['idle', a.code];
    let txt = esc(a.text);
    const m = a.code === 'DECALIBRATION_SUSPECTED' && a.text.match(/slope ([-+\d.]+).*CUSUM\+ ([\d.]+) \/ CUSUM- ([\d.]+)/);
    if (m) txt = `Depth started drifting <b>${+m[1] > 0 ? 'deeper' : 'shallower'}</b>, about ${Math.abs(m[1] * 100).toFixed(1)} mm per 100 devices. ` +
      `Alarm meter at ${Math.max(+m[2], +m[3]).toFixed(1)} (alarm above 5).`;
    return `<tr><td>${a.line}</td><td>${a.event_no}</td><td><span class="pill ${c}">${t}</span></td><td class="note" data-tip="${esc(a.text)}">${txt}</td></tr>`;
  }).join('') : '<tr><td class="note">No messages: data complete and no drift detected.</td></tr>';
}

/* ------------------------------------------------------------------ MES: production state per line */
// Hovering a line's chart greys out everything after the hovered device and shows the line's state at that moment.
const HOVER = {}, PROD = {};
const shadePlugin = {
  id: 'shade',
  afterDatasetsDraw(chart) {
    const line = chart.options.plugins.shade && chart.options.plugins.shade.line, xv = line && HOVER[line];
    if (xv == null) return;
    const {ctx, chartArea: a, scales: {x}} = chart, px = x.getPixelForValue(xv);
    if (px < a.left || px > a.right) return;
    ctx.save();
    ctx.fillStyle = 'rgba(115,118,125,0.22)';
    ctx.fillRect(px, a.top, a.right - px, a.bottom - a.top);
    ctx.strokeStyle = '#1f3a5f'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
    ctx.restore();
  },
};
if (typeof Chart !== 'undefined') Chart.register(shadePlugin);
function hoverable(line, o) {
  o.plugins.shade = {line};
  o.onHover = (evt, els, chart) => {
    const P = PROD[line], a = chart.chartArea;
    if (!P || !P.units.length || evt.x < a.left || evt.x > a.right) return clearHover(line);
    const i = idxAt(P.units, chart.scales.x.getValueForPixel(evt.x));
    if (HOVER[line] === P.units[i].event_no) return;
    HOVER[line] = P.units[i].event_no;
    ['d-', 'c-'].forEach(p => charts[p + line] && charts[p + line].draw());
    $('mes-' + line).innerHTML = mesHTML(line, i, true);
  };
  return o;
}
function idxAt(units, xv) {           // last device made at or before x
  let lo = 0, hi = units.length - 1;
  if (xv <= units[0].event_no) return 0;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (units[mid].event_no <= xv) lo = mid; else hi = mid - 1; }
  return lo;
}
function clearHover(line) {
  if (HOVER[line] == null) return;
  HOVER[line] = null;
  ['d-', 'c-'].forEach(p => charts[p + line] && charts[p + line].draw());
  const P = PROD[line];
  if (P && P.units.length) $('mes-' + line).innerHTML = mesHTML(line, P.units.length - 1, false);
}
function mesHTML(line, i, hovering) {
  const P = PROD[line], u = P.units[i], M = live.materials;
  const cell = (l, v, sub = '', t = '') => `<div class="mc"${t ? ` data-tip="${t}"` : ''}><div class="ml">${l}</div><div class="mv">${v}</div>${sub ? `<div class="ms">${sub}</div>` : ''}</div>`;
  const nok = P.nokCum[i];
  return `<div class="mes-head">${hovering ? `◀ STATE AT DEVICE #${u.event_no} <span>· everything to the right of the line is not produced yet · move away for live</span>`
      : '● LIVE · current state of the line <span>· hover the chart to go back in time</span>'}</div><div class="mes-row">` +
    cell('PRODUCED', fmt(i + 1), 'feeding devices', 'Devices assembled on this line up to this moment.') +
    cell('OK', fmt(i + 1 - nok), pct((i + 1 - nok) / (i + 1)) + ' yield', 'Devices with depth inside 13–17 mm.') +
    cell('OUT OF SPEC', nok, nok ? 'first: ' + P.firstNok : 'none so far', 'Devices outside 13–17 mm.') +
    cell('DEVICE', u.unit_id, u.time.replace('T', ' ').replace('Z', ' UTC'), 'Unit ID and completion time from the line.') +
    cell('BAG LOT', u.bag_lot, `device ${P.bagN[i]} from this lot · port ${M.bag[u.bag_lot] != null ? M.bag[u.bag_lot].toFixed(4) + '″' : '?'}`, 'Bag lot used for this device (BAG-01) and how many devices this line has made from it so far.') +
    cell('TUBE REEL', u.reel, `device ${P.reelN[i]} from this reel · OD ${M.pvc[u.reel] != null ? M.pvc[u.reel].toFixed(4) + '″' : '?'}`, 'Tube reel used for this device (PVC-01) and how many devices this line has made from it so far.') +
    cell('PRESSURE', u.pressure + ' bar') +
    cell('DEPTH', u.depth.toFixed(2) + ' mm', u.nok ? '<b class="red">out of spec</b>' : 'in spec') + '</div>';
}
function renderProduction() {
  const lines = ['ASM-01', 'ASM-02', 'ASM-03'], bags = {}, reels = {};
  for (const line of lines) {
    const units = live.lines[line].units, nokCum = [], bagN = [], reelN = [], bc = {}, rc = {};
    let n = 0, firstNok = null;
    units.forEach((u, i) => {
      if (u.nok) { n++; firstNok = firstNok || u.unit_id; }
      nokCum.push(n); bc[u.bag_lot] = (bc[u.bag_lot] || 0) + 1; rc[u.reel] = (rc[u.reel] || 0) + 1;
      bagN.push(bc[u.bag_lot]); reelN.push(rc[u.reel]);
      for (const [T, k] of [[bags, u.bag_lot], [reels, u.reel]]) {
        const e = T[k] = T[k] || {n: 0, lines: {}, first: u.unit_id, last: null, inUse: []};
        e.n++; e.lines[line] = (e.lines[line] || 0) + 1; e.last = u.unit_id;
      }
    });
    PROD[line] = {units, nokCum, bagN, reelN, firstNok};
    if (units.length) {
      const last = units[units.length - 1];
      bags[last.bag_lot].inUse.push(line); reels[last.reel].inUse.push(line);
      if (HOVER[line] == null) $('mes-' + line).innerHTML = mesHTML(line, units.length - 1, false);
    }
  }
  const M = live.materials;
  $('prod-lines').innerHTML = '<tr><th>line</th><th>produced</th><th>OK</th><th>out of spec</th><th>bag lot now</th><th>tube reel now</th><th>last device</th></tr>' +
    lines.map(l => { const P = PROD[l], u = P.units[P.units.length - 1];
      return u ? `<tr><td><b>${l}</b></td><td>${fmt(P.units.length)}</td><td>${fmt(P.units.length - P.nokCum[P.units.length - 1])}</td><td>${P.nokCum[P.units.length - 1]}</td>
        <td>${u.bag_lot}</td><td>${u.reel}</td><td>${u.unit_id}<br><span class="note">${u.time.replace('T', ' ').replace('Z', '')}</span></td></tr>`
        : `<tr><td><b>${l}</b></td><td colspan="6" class="note">no devices yet</td></tr>`; }).join('');
  const mat = (T, means, unit) => '<tr><th></th><th>devices</th><th>per line (ASM-0x)</th><th>first → last device</th><th>' + unit + '</th></tr>' +
    Object.entries(T).sort((a, b) => a[0].localeCompare(b[0])).map(([k, e]) => `<tr class="${e.inUse.length ? 'hl' : ''}"><td><b>${k}</b>${e.inUse.length ? `<br><span class="pill ok">in use: ${e.inUse.join(', ')}</span>` : ''}</td>
      <td>${fmt(e.n)}</td><td class="note">${Object.entries(e.lines).map(([l, c]) => `<span class="nw">${l.slice(4)}: ${c}</span>`).join(' · ')}</td><td class="note"><span class="nw">${e.first}</span> → <span class="nw">${e.last}</span></td>
      <td>${means[k] != null ? means[k].toFixed(4) + '″' : '<span class="FAIL">no data</span>'}</td></tr>`).join('');
  $('prod-bags').innerHTML = mat(bags, M.bag, 'port ID');
  $('prod-reels').innerHTML = mat(reels, M.pvc, 'tube OD');
  $('prod-note').innerHTML = `${fmt(lines.reduce((s, l) => s + PROD[l].units.length, 0))} feeding devices produced from ${Object.keys(bags).length} bag lots and ${Object.keys(reels).length} tube reels.
    Highlighted rows are in use right now.`;
}

/* ------------------------------------------------------------------ DATA EXPLORER */
let ana = null;
async function loadData() {
  if (!ana) {
    $('kpis').innerHTML = '<div class="card note">Loading the analysis…</div>';
    const r = await (await fetch('/api/analysis')).json();
    if (r.error) { $('kpis').innerHTML = `<div class="card note">${esc(r.error)}</div>`; return; }
    ana = r;
  }
  const sel = $('d-line'), avail = Object.keys(ana.lines).filter(l => ana.lines[l]);
  if (!sel.options.length) { sel.innerHTML = avail.map(l => `<option>${l}</option>`).join(''); sel.onchange = renderData; }
  renderData();
}
function renderData() {
  const l = $('d-line').value, L = ana.lines[l];
  if (!L) { $('kpis').innerHTML = '<div class="card note">No assembly data in this dataset yet.</div>'; return; }
  const S = L.series, n = S.depth.length, x0 = S.event_no[0], x1 = S.event_no[n - 1];
  const margin = Math.min(L.closest_high[0].margin_mm, L.closest_low[0].margin_mm);
  const tile = (v, lab, t) => `<div class="card tile" data-tip="${t}"><div class="v">${v}</div><div class="l">${lab}</div></div>`;
  $('kpis').innerHTML =
    tile(fmt(ana.total_timestamps), 'timestamps received (all sources)', 'Every record from BAG-01, PVC-01 and the assembly lines has its own timestamp. None are duplicated.') +
    tile(fmt(L.units), `devices on ${l}`, `From ${L.first_unit} to ${L.last_unit}.`) +
    tile(L.n_nok, 'devices out of spec', 'Devices whose measured depth was outside 13.0–17.0 mm.') +
    tile(L.capability.mean.toFixed(2) + ' mm', 'average depth (target 15.00)', 'Average measured insertion depth. The target is 15.0 mm.') +
    tile(L.capability.cpk, 'Cpk', GL.cpk[1]) +
    (margin >= 0 ? tile(margin.toFixed(3) + ' mm', 'closest approach to a limit', 'The smallest distance any device came to 13.0 or 17.0 mm.')
      : tile(Math.abs(margin).toFixed(3) + ' mm', 'worst device: this far outside a limit', 'How far the worst device went beyond 13.0 or 17.0 mm.'));
  // whole run
  const ds = [];
  for (const p of [...new Set(S.pressure)].sort()) {
    const idx = S.pressure.map((v, i) => v === p ? i : -1).filter(i => i >= 0);
    ds.push({label: p + ' bar', data: idx.map(i => ({x: S.event_no[i], y: S.depth[i], u: S.unit_id[i] + ' · ' + S.time[i]})), backgroundColor: PCOL[String(p)] || C.grey, pointRadius: 1.6});
  }
  const bad = S.nok.map((v, i) => v ? i : -1).filter(i => i >= 0);
  if (bad.length) ds.push({label: 'out of spec', data: bad.map(i => ({x: S.event_no[i], y: S.depth[i], u: S.unit_id[i]})), backgroundColor: C.red, pointRadius: 5});
  const cl = [...L.closest_high.slice(0, 3), ...L.closest_low.slice(0, 3)];
  ds.push({label: 'closest to a limit', data: cl.map(c => ({x: c.event_no, y: c.depth, u: c.unit_id})), backgroundColor: '#111', pointStyle: 'triangle', pointRadius: 6});
  ds.push({label: 'expected depth', data: pts(S.event_no, S.baseline), borderColor: 'rgba(110,110,110,.45)', borderWidth: 1, pointRadius: 0, showLine: true});
  ds.push(hline(17, x0, x1, C.red, 'limits'), hline(13, x0, x1, C.red), hline(15, x0, x1, C.green, 'target 15 mm', [6, 4]));
  chart('d-depth', {type: 'scatter', data: {datasets: ds}, options: opts({x: 'device number', y: 'depth (mm)'})});
  chart('d-cusum', {type: 'scatter', data: {datasets: [
    {label: 'pushing deeper', data: pts(S.event_no, S.cusum_pos), borderColor: C.red, borderWidth: 1, pointRadius: 0, showLine: true},
    {label: 'pushing shallower', data: pts(S.event_no, S.cusum_neg), borderColor: PAL[0], borderWidth: 1, pointRadius: 0, showLine: true},
    {label: 'axis following error (mm)', data: pts(S.event_no, S.follow_err), borderColor: 'rgba(30,138,76,.6)', borderWidth: 1, pointRadius: 0, showLine: true},
    hline(5, x0, x1, C.grey, 'alarm level 5', [3, 3])]}, options: opts({y: 'meter / mm', ymax: Math.min(20, Math.max(8, ...S.cusum_pos, ...S.cusum_neg))})});
  // failure
  const H = L.closest_high[0], Lo = L.closest_low[0], meanOff = L.capability.mean - 15;
  const row = c => `<tr><td>${c.unit_id}</td><td>${c.depth}</td><td>${c.margin_mm}</td><td>${c.bag_lot}</td><td>${c.reel}</td><td>${c.pressure}</td></tr>`;
  const th = '<tr><th>device</th><th>depth mm</th><th>distance to limit</th><th>bag lot</th><th>tube reel</th><th>bar</th></tr>';
  let head;
  if (L.first_failure) {
    const f = L.first_failure;
    head = `<p><b>The first out-of-spec device was ${f.unit_id}</b> (device #${f.event_no}, ${f.time}) at ${f.depth} mm.
      The formula expected ${f.baseline} mm, so it was ${signed(f.resid)} mm off. ${L.n_nok} devices were out of spec in total.</p>` +
      (L.prediction ? `<p>Locked prediction: <b>${L.prediction.predicted_unit_id}</b>, made while ${L.prediction.current_unit_id} was produced → <b>${L.evaluation ? L.evaluation.status : 'waiting'}</b>.</p>` : '<p>No prediction had been locked before it.</p>');
  } else head = `<p><b>No device failed: all ${fmt(L.units)} are inside 13–17 mm.</b> The closest call was <b>${H.unit_id}</b> at ${H.depth} mm, only
      <b>${H.margin_mm} mm</b> below the 17 mm limit. It was made with a large bag port (${H.bag_lot}), a small tube (${H.reel}) and the highest pressure
      (${H.pressure} bar), which is exactly the combination the formula predicts to go deepest.</p>
      <p><b>If this line fails, it will be on the “too deep” side.</b> The average depth is ${L.capability.mean.toFixed(2)} mm instead of 15.00 mm
      (${signed(meanOff, 2)} mm too deep: the actuator over-travels by about ${(-S.follow_err.reduce((a, b) => a + b, 0) / n).toFixed(2)} mm and the camera reads about 0.41 mm deeper).
      That leaves ${Lo.margin_mm} mm of room on the shallow side but only ${H.margin_mm} mm on the deep side.
      With the worst bag + tube + pressure combination, just <b>${L.drift_to_fail_mm.to_USL_from_worst} mm</b> of extra drift would push the average device over 17 mm.
      ${term('cpk')} = ${L.capability.cpk}, i.e. about ${fmt(L.capability.ppm_expected)} ${term('ppm')} expected too deep even without drift.</p>`;
  $('d-fail').innerHTML = head + `<div class="grid3 tight"><div><h3>Closest to 17 mm (too deep)</h3><table>${th}${L.closest_high.map(row).join('')}</table></div>
    <div><h3>Closest to 13 mm (too shallow)</h3><table>${th}${L.closest_low.map(row).join('')}</table></div>
    <div><h3>Deepest and shallowest set-ups (formula)</h3><table><tr><th>bag lot</th><th>tube reel</th><th>bar</th><th>expected mm</th></tr>${L.worst_case.map(w => `<tr><td>${w.bag_lot}</td><td>${w.reel}</td><td>${w.pressure}</td><td>${w.pred_mm}</td></tr>`).join('')}</table></div></div>`;
  // timing
  $('d-timing').innerHTML = '<tr><th>source</th><th>records</th><th>unique timestamps</th><th>duplicates</th><th>first → last (UTC)</th><th>time span</th><th>usual step</th></tr>' +
    ana.timing.map(t => `<tr><td>${t.source}</td><td>${fmt(t.records)}</td><td>${fmt(t.timestamps_unique)}</td><td>${t.duplicate_timestamps}</td><td>${t.first.slice(0, 19).replace('T', ' ')} → ${t.last.slice(0, 19).replace('T', ' ')}</td><td>${t.span_h} h</td><td>${t.typical_interval_s} s</td></tr>`).join('') +
    `<tr><th>total</th><th>${fmt(ana.total_timestamps)}</th><th colspan="5"></th></tr>`;
  $('d-timing-note').innerHTML = ana.raw_responses ? `${ana.raw_responses} API responses are stored word for word. The bag and tube files list their lots out of time order; every timestamp is kept exactly as received.`
    : 'This is synthetic data generated on this computer, so there are no API responses to store. Every timestamp is kept exactly as generated.';
  const iv = L.timing.intervals_s, bins = {};
  for (const [s, c] of Object.entries(iv)) { const b = +s <= 10 ? `${+s} s` : `${Math.floor(s / 20) * 20}–${Math.floor(s / 20) * 20 + 19} s`; bins[b] = (bins[b] || 0) + c; }
  const bk = Object.keys(bins).sort((a, b) => parseInt(a) - parseInt(b));
  chart('d-dt', {type: 'bar', data: {labels: bk, datasets: [{label: 'pairs of devices', data: bk.map(b => bins[b]), backgroundColor: PAL[0], borderRadius: 3}]},
    options: {animation: false, plugins: {legend: {display: false}}, scales: {y: {title: {display: true, text: 'how often'}, grid: {color: C.grid}}, x: {title: {display: true, text: 'time between two devices'}}}}});
  $('d-dt-note').innerHTML = `Most devices come <b>3 s</b> apart. Every one of the ${L.timing.pauses_gt_10s} longer pauses (60–130 s) happens exactly at a
    ${term('setup', 'setup change')} (${L.timing.setup_changes} in total). Average ${L.timing.units_per_hour} devices per hour.`;
  $('d-checks').innerHTML = '<tr><th>check</th><th>result</th><th>details</th></tr>' + ana.checks.map(c => `<tr><td>${c[0]}</td><td class="${c[1]}">${c[1]}</td><td class="note">${esc(c[2])}</td></tr>`).join('');
  // materials
  const lvlScatter = (id, xs, ys, groups, lo, hi, ylab) => {
    const g = {}; xs.forEach((x, i) => { const k = groups[i]; (g[k] = g[k] || []).push({x, y: ys[i]}); });
    chart(id, {type: 'scatter', data: {datasets: [...Object.keys(g).sort().map(k => ({label: LVLNAME[k] || k, data: g[k], backgroundColor: LVL[k] || C.grey, pointRadius: .8})),
      hline(hi, xs[0], xs[xs.length - 1], C.red, 'spec limits'), hline(lo, xs[0], xs[xs.length - 1], C.red)]}, options: opts({x: 'reading number', y: ylab, label: c => `${c.raw.y}`})});
  };
  if (ana.pvc) { const P = ana.pvc.series; lvlScatter('d-pvc', P.od.map((_, i) => i + 1), P.od, P.reel.map(r => r.split('_').pop()[0]), 0.1675, 0.1725, 'tube size (in)'); }
  if (ana.bag) { const B = ana.bag.series; lvlScatter('d-bag', B.port.map((_, i) => i + 1), B.port, B.lot.map(r => r.split('_').pop()), 0.173, 0.176, 'port size (in)'); }
  const E = L.effects, labs = [], vals = [], cols = [];
  const ord = o => Object.entries(o).sort((a, b) => 'LMH'.indexOf(a[0][0]) - 'LMH'.indexOf(b[0][0]));
  for (const [k, v] of ord(E.bag_level)) { labs.push(`bag port ${LVLNAME[k] || k}`); vals.push(v); cols.push(PAL[0]); }
  for (const [k, v] of ord(E.reel_level)) { labs.push(`tube ${k}`); vals.push(v); cols.push(PAL[2]); }
  for (const [k, v] of Object.entries(E.pressure)) { labs.push(`${k} bar`); vals.push(v); cols.push(PAL[1]); }
  chart('d-eff', {type: 'bar', data: {labels: labs, datasets: [{data: vals, backgroundColor: cols, borderRadius: 3}]},
    options: {animation: false, plugins: {legend: {display: false}, tooltip: {callbacks: {label: c => c.raw + ' mm'}}}, scales: {y: {min: 15.4, max: 16.4, title: {display: true, text: 'average depth (mm)'}, grid: {color: C.grid}}, x: {ticks: {font: {size: 9}}}}}});
  // synthetic failure runs
  if (!ana.sim) { $('d-sim-card').style.display = 'none'; return; }
  $('d-sim-card').style.display = '';
  const notes = [];
  for (const [ln, s] of Object.entries(ana.sim)) {
    const e = s.evaluation, N = s.depth.length, num = id => id ? parseInt(id.match(/\d+$/)[0]) : null;
    const pi = num(s.prediction_unit), mi = num(s.prediction_made_at);
    const d = [{label: 'device depth', data: s.depth.map((y, i) => ({x: i + 1, y})), backgroundColor: PAL[0], pointRadius: 1.2}, hline(17, 1, N, C.red), hline(13, 1, N, C.red)];
    if (s.first_alarm_event) d.push(vline(s.first_alarm_event, 12.5, 17.5, C.orange, 'drift alarm'));
    if (mi) d.push(vline(mi, 12.5, 17.5, C.lock, 'prediction locked'));
    if (pi) d.push(vline(pi, 12.5, 17.5, C.lock, 'predicted failure', [4, 3]));
    d.push({label: 'actual failure', data: [{x: e.actual_event_no, y: e.actual_depth_mm}], backgroundColor: C.red, borderColor: C.red, pointStyle: 'crossRot', pointRadius: 8, borderWidth: 3});
    chart('s-' + ln, {type: 'scatter', data: {datasets: d}, options: opts({x: ln + ' · device number', y: 'depth (mm)'})});
    notes.push(`<b>${ln}</b>: alarm at device #${s.first_alarm_event}, prediction ${s.prediction_unit || 'none'} locked at ${s.prediction_made_at || '–'},
      actual first failure ${e.actual_first_failure_unit_id} (${e.actual_depth_mm} mm) → <b>${e.status}</b>${e.error_units != null ? ` (${e.error_units} devices early)` : ''}`);
  }
  $('d-sim-note').innerHTML = 'Drift added: ASM-01 actuator +0.010 mm per device from #150 · ASM-02 camera −0.015 mm per device from #100 · ASM-03 actuator +0.006 mm per device from #250.<br>' + notes.join('<br>');
}

/* ------------------------------------------------------------------ MODEL LAB */
let labMeta = null, labTrain = null, labTest = null, labRes = null, labZoom = 'all';
let labSel = ['M03F', 'M03', 'M09', 'M13'];
async function meta() {
  if (!labMeta) { labMeta = await (await fetch('/api/lab/datasets')).json(); MODEL = Object.fromEntries(labMeta.models.map(m => [m.key, m])); }
  return labMeta;
}
async function initLab() {
  await meta();
  if (labTrain) return;
  labTrain = (labMeta.datasets.find(d => d.id.startsWith('HISTORICAL')) || labMeta.datasets[0]).id;
  labTest = (labMeta.datasets.find(d => d.id.startsWith('SIM:')) || labMeta.datasets[0]).id;
  renderLabControls();
  document.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => {
    const P = {rec: ['M03F', 'M03', 'M09', 'M12', 'M13'], none: [],
      formula: labMeta.models.filter(m => m.kind === 'Simple formula').map(m => m.key),
      ml: labMeta.models.filter(m => m.kind === 'Machine learning').map(m => m.key)};
    labSel = P[b.dataset.preset].slice(0, 8); renderLabControls();
  });
  $('l-run').onclick = runLab;
  // shareable links: ?train=...&test=...&models=M03F,M09&run=1 (plus fake settings fk, fs, fst, fn, fnoise, fseed)
  const q = new URLSearchParams(location.search);
  if (q.get('train')) labTrain = q.get('train');
  if (q.get('test')) labTest = q.get('test');
  if (q.get('models')) labSel = q.get('models').split(',').filter(k => MODEL[k]).slice(0, 8);
  for (const k of ['fk', 'fs', 'fst', 'fn', 'fnoise', 'fseed']) if (q.get(k) != null) $('l-' + k).value = q.get(k);
  renderLabControls();
  if (q.get('run')) runLab();
  document.querySelectorAll('[data-zoom]').forEach(b => b.onclick = () => {
    labZoom = b.dataset.zoom; document.querySelectorAll('[data-zoom]').forEach(x => x.classList.toggle('on', x === b)); if (labRes) renderLabCharts(labRes);
  });
}
function renderLabControls() {
  const radios = (host, val, set) => {
    $(host).innerHTML = labMeta.datasets.map(d => `<label class="${d.id === val ? 'on' : ''}"><input type="radio" name="${host}" value="${d.id}" ${d.id === val ? 'checked' : ''}>
      <b>${d.label}</b> ${d.synthetic ? '<span class="pill warn">synthetic</span>' : '<span class="pill ok">real</span>'}<small>${d.desc}</small></label>`).join('');
    $(host).querySelectorAll('input').forEach(i => i.onchange = () => { set(i.value); renderLabControls(); });
  };
  radios('l-train', labTrain, v => labTrain = v);
  radios('l-test', labTest, v => labTest = v);
  $('l-fake').style.display = labTrain === 'FAKE' || labTest === 'FAKE' ? '' : 'none';
  const groups = ['Simple formula', 'Table', 'Machine learning', 'Reference'];
  $('l-models').innerHTML = groups.map(g => `<div class="group">${g}</div>` + labMeta.models.filter(m => m.kind === g).map(m => {
    const i = labSel.indexOf(m.key);
    return `<label class="chip ${i >= 0 ? 'on' : ''}" data-model="${m.key}"><input type="checkbox" value="${m.key}" ${i >= 0 ? 'checked' : ''}>
      <span class="sw" style="background:${i >= 0 ? PAL[i] : '#ddd'}"></span>${m.key} ${esc(m.name)} <span class="box ${m.box}">${m.box}</span></label>`;
  }).join('')).join('');
  $('l-models').querySelectorAll('input').forEach(i => i.onchange = () => {
    if (i.checked) { if (labSel.length >= 8) { i.checked = false; alert('Pick at most 8 models, so each keeps its own colour.'); return; } labSel.push(i.value); }
    else labSel = labSel.filter(k => k !== i.value);
    renderLabControls();
  });
}
async function runLab() {
  if (!labSel.length) { alert('Choose at least one model.'); return; }
  const g = id => $(id).value;
  const q = new URLSearchParams({train: labTrain, test: labTest, models: labSel.join(','), fk: g('l-fk'), fs: g('l-fs'), fst: g('l-fst'),
    fn: g('l-fn'), fnoise: g('l-fnoise'), fseed: g('l-fseed')});
  const btn = $('l-run'); btn.disabled = true;
  $('l-status').textContent = `Training ${labSel.length} model(s) and replaying the digital twin… usually 2–10 s (the Gaussian process is the slowest).`;
  const t0 = performance.now();
  try {
    const R = await (await fetch('/api/lab?' + q)).json();
    if (R.error) { $('l-status').innerHTML = `<span class="FAIL">${esc(R.error)}</span>`; return; }
    labRes = R;
    $('l-status').innerHTML = `Done in ${((performance.now() - t0) / 1000).toFixed(1)} s · ${R.same_data ? term('cv', 'cross-validation') + ' (same data for training and test)' : 'trained on one dataset, tested on the other'} · ${fmt(R.n_train)} training devices, ${fmt(R.n_test)} test devices.`;
    renderLab(R);
  } finally { btn.disabled = false; }
}
function outcome(t) {
  const s = t.status || '';
  if (s === 'HIT') return '<span class="PASS">HIT · exactly right</span>';
  if (s === 'EARLY') return `<span class="WARN">EARLY · ${t.error_units} devices before the failure</span>`;
  if (s === 'LATE') return `<span class="FAIL">LATE · ${-t.error_units} devices after the failure</span>`;
  if (s === 'MISSED') return '<span class="FAIL">MISSED · failure came first</span>';
  if (s.startsWith('FALSE')) return '<span class="FAIL">FALSE ALARM · nothing failed</span>';
  return '<span class="PASS">no failure, no alarm</span>';
}
function renderLab(R) {
  $('l-out').style.display = '';
  const ds = id => labMeta.datasets.find(d => d.id === id) || {label: id};
  const syn = [R.train, R.test].some(id => (labMeta.datasets.find(d => d.id === id) || {}).synthetic);
  $('l-badge').innerHTML = syn ? '<span class="pill warn">uses synthetic data</span>' : '<span class="pill ok">real data only</span>';
  // verdict
  const ms = R.models, best = ms.reduce((a, b) => a.rmse <= b.rmse ? a : b);
  const fails = ms.some(m => m.twin.actual_failure);
  let v = `<h2>What this run shows</h2><p>Trained on <b>${ds(R.train).label}</b>, tested on <b>${ds(R.test).label}</b>.
    Most accurate depth prediction: ${modelTag(best.key)} with a typical error of <b>${best.rmse.toFixed(3)} mm</b>.</p>`;
  if (fails) {
    const af = ms.find(m => m.twin.actual_failure).twin;
    const good = ms.filter(m => ['EARLY', 'HIT'].includes(m.twin.status)).sort((a, b) => a.twin.error_units - b.twin.error_units);
    const missed = ms.filter(m => m.twin.status === 'MISSED' || m.twin.status === 'LATE');
    v += `<p>The first device actually out of spec was <b>${af.actual_failure}</b> (device #${af.actual_idx} of the test data).
      ${good.length ? `${good.length} of ${ms.length} models warned in time. Closest warning: ${modelTag(good[0].key)}, ${good[0].twin.error_units ? good[0].twin.error_units + ' devices early' : 'exactly right'}.` : 'No model warned in time.'}
      ${missed.length ? ` Too late or missed: ${missed.map(m => modelTag(m.key)).join(', ')}.` : ''}</p>`;
  } else {
    const fa = ms.filter(m => (m.twin.status || '').startsWith('FALSE'));
    v += `<p>No test device went out of spec, so the right answer is <b>no prediction at all</b>.
      ${fa.length ? `False alarms from: ${fa.map(m => modelTag(m.key)).join(', ')}.` : 'All models stayed quiet. Correct.'}</p>`;
  }
  const biased = ms.filter(m => Math.abs(m.bias) > 0.5);
  if (biased.length) v += `<p class="note">⚠ ${biased.map(m => modelTag(m.key)).join(', ')} ${biased.length > 1 ? 'are' : 'is'} off by more than 0.5 mm on average (${term('bias', 'bias')}).
    Usually this means the training data contained a drift, or the test data does. A model trained on a drifting line learns the drift as “normal”.</p>`;
  $('l-verdict').innerHTML = v;
  // table
  const hdr = (t, k) => `<th>${k ? term(k, t) : t}</th>`;
  $('l-table').innerHTML = '<tr><th></th>' + hdr('model') + hdr('typical error', 'rmse') + hdr('bias', 'bias') + hdr('R²', 'r2') + hdr('first drift alarm', 'cusum') +
    hdr('predicted first failure', 'lock') + hdr('actual first failure') + hdr('result', 'early') + '</tr>' +
    ms.map((m, i) => `<tr><td><span class="sw" style="background:${PAL[i]}"></span></td><td>${modelTag(m.key)}</td>
      <td style="${m === best ? 'font-weight:700' : ''}">${m.rmse.toFixed(3)} mm</td><td>${signed(m.bias)}</td><td>${m.r2.toFixed(2)}</td>
      <td>${m.twin.first_alarm || '–'}</td><td>${m.twin.predicted_unit ? `<b>${m.twin.predicted_unit}</b><br><span class="note">locked at ${m.twin.predicted_at}</span>` : '–'}</td>
      <td>${m.twin.actual_failure || 'none'}</td><td>${outcome(m.twin)}</td></tr>`).join('');
  $('l-note').innerHTML = `${modelTag('M03F')} is the frozen production formula: it ignores the training data you picked.
    “Result” comes from plugging each model into the digital twin as its expected-depth formula and replaying the test devices with the production alarm rules.
    Everything here runs in memory: nothing is written to the ledger and the production model never changes.`;
  renderLabCharts(R);
}
function renderLabCharts(R) {
  const ms = R.models, n = R.actual.length, rows = ms.length;
  // ---- timeline: one row per model
  const marks = ms.flatMap(m => [m.twin.first_alarm_idx, m.twin.predicted_at_idx, m.twin.predicted_idx, m.twin.actual_idx]).filter(x => x != null);
  const lo = marks.length ? Math.max(1, Math.min(...marks) - 15) : 1, hi = marks.length ? Math.min(n + 10, Math.max(...marks) + 15) : n;
  const tl = [];
  ms.forEach((m, i) => { const t = m.twin; if (t.predicted_at_idx && t.predicted_idx) tl.push({label: '', data: [{x: t.predicted_at_idx, y: i}, {x: t.predicted_idx, y: i}], borderColor: PAL[i], borderWidth: 3, pointRadius: 0, showLine: true}); });
  tl.push({label: '▲ drift alarm', data: ms.map((m, i) => m.twin.first_alarm_idx ? {x: m.twin.first_alarm_idx, y: i, t: `${m.key}: drift alarm at ${m.twin.first_alarm}`} : null).filter(Boolean),
    backgroundColor: C.ink2, pointStyle: 'triangle', pointRadius: 7});
  tl.push({label: '● prediction locked', data: ms.map((m, i) => m.twin.predicted_at_idx ? {x: m.twin.predicted_at_idx, y: i, t: `${m.key}: prediction locked while ${m.twin.predicted_at} was made`} : null).filter(Boolean),
    backgroundColor: '#fff', borderColor: C.ink2, borderWidth: 2, pointRadius: 6});
  ms.forEach((m, i) => { if (m.twin.predicted_idx) tl.push({label: `◆ ${m.key} predicted failure`, data: [{x: m.twin.predicted_idx, y: i, t: `${m.key} ${MODEL[m.key] ? MODEL[m.key].name : ''}: predicted first failure ${m.twin.predicted_unit} → ${m.twin.status}`}],
    backgroundColor: PAL[i], borderColor: '#fff', borderWidth: 1.5, pointStyle: 'rectRot', pointRadius: 9}); });
  const af = ms.find(m => m.twin.actual_idx);
  if (af) tl.push(vline(af.twin.actual_idx, -0.6, rows - 0.4, C.red, `actual first failure ${af.twin.actual_failure}`, [], 2.5));
  $('l-tl-box').style.height = (90 + 34 * rows) + 'px';
  const to = opts({x: 'device number in the test data', legend: true, xmin: lo, xmax: hi, fixed: true, label: c => c.raw.t || ''});
  to.scales.y = {min: -0.6, max: rows - 0.4, reverse: true, grid: {color: C.grid}, ticks: {stepSize: 1, font: {size: 11},
    callback: v => Number.isInteger(v) && ms[v] ? `${ms[v].key} ${(MODEL[ms[v].key] || {}).name || ''}`.slice(0, 34) : ''}};
  if (charts['l-timeline']) { charts['l-timeline'].destroy(); delete charts['l-timeline']; }
  chart('l-timeline', {type: 'scatter', data: {datasets: tl}, options: to});
  // ---- measured vs predicted
  const d = [{label: 'measured', data: R.actual.map((y, i) => ({x: i + 1, y, u: R.unit_id[i]})), backgroundColor: 'rgba(120,120,120,.45)', pointRadius: 1.3}];
  ms.forEach((m, i) => d.push({label: m.key, data: m.pred.map((y, j) => ({x: j + 1, y, u: `${m.key} expects at ${R.unit_id[j]}`})), borderColor: PAL[i], borderWidth: 1.5, pointRadius: 0, showLine: true}));
  const ymin = Math.min(12.5, ...R.actual), ymax = Math.max(17.5, ...R.actual);
  ms.forEach((m, i) => { if (m.twin.predicted_idx) d.push(vline(m.twin.predicted_idx, ymin, ymax, PAL[i], '', [5, 4])); });
  if (af) d.push(vline(af.twin.actual_idx, ymin, ymax, C.red, 'actual first failure'));
  d.push(hline(17, 1, n, C.red, 'limits'), hline(13, 1, n, C.red));
  const zoom = labZoom === 'fail' && marks.length;
  chart('l-pred', {type: 'scatter', data: {datasets: d}, options: opts({x: 'device number in the test data', y: 'depth (mm)', xmin: zoom ? lo : 1, xmax: zoom ? hi : n})});
  // ---- rolling difference
  const roll = (a, w) => a.map((_, i) => { const s = a.slice(Math.max(0, i - w + 1), i + 1); return s.reduce((p, c) => p + c, 0) / s.length; });
  chart('l-res', {type: 'scatter', data: {datasets: [...ms.map((m, i) => ({label: m.key, data: roll(R.actual.map((y, j) => y - m.pred[j]), 25).map((y, j) => ({x: j + 1, y: +y.toFixed(3), u: R.unit_id[j]})),
    borderColor: PAL[i], borderWidth: 1.5, pointRadius: 0, showLine: true})), hline(0, 1, n, C.grey, '', [3, 3])]}, options: opts({x: 'device number', y: 'measured − predicted (mm)'})});
}

/* ------------------------------------------------------------------ MODEL COMPARISON */
let mods = null;
async function loadCmp() {
  await meta();
  if (!mods) { const r = await fetch('/api/models'); if (!r.ok) { $('m-verdict').innerHTML = 'Run <code>python3 models.py</code> first.'; return; } mods = await r.json(); }
  const R = mods, best = k => Math.min(...R.models.map(m => m.tests[k].rmse));
  const f = (v, b) => v >= 1 ? `<b class="FAIL" data-tip="Broke down: off by more than 1 mm.">${v.toFixed(1)} ✖</b>` : `<span style="${v === b ? 'font-weight:700' : ''}">${v.toFixed(3)}</span>`;
  const BOX = {white: 'white box', grey: 'grey box', black: 'black box'};
  $('m-table').innerHTML = `<tr><th>model</th><th>type</th><th>${term('box', 'explainability')}</th><th>random split</th><th>new lots</th><th>${term('unseen', 'unseen setting')}</th><th>future</th>
    <th>numbers inside</th><th>twin: ${term('jury', 'E ≤ 2')}</th><th>twin: locked before failure</th><th>twin: ${term('falsealarm', 'false alarm')}</th></tr>` +
    R.models.map(m => {
      const t = R.twin[m.key], info = MODEL[m.key] || {};
      return `<tr class="${m.key === 'M03' ? 'hl' : ''}"><td>${modelTag(m.key)}</td><td>${info.kind || m.family}</td><td><span class="box ${m.box}">${BOX[m.box]}</span></td>
        ${['random', 'new_lots', 'unseen', 'future'].map(k => `<td>${f(m.tests[k].rmse, best(k))}</td>`).join('')}
        <td>${m.n_parameters != null ? fmt(m.n_parameters) : 1}</td><td>${t && t['E<=2_%'] != null ? t['E<=2_%'] + ' %' : '–'}</td><td>${t ? t['caught_in_advance_%'] + ' %' : '–'}</td>
        <td>${t ? (t.false_prediction_on_stable_line ? '<b class="FAIL">yes</b>' : 'no') : '–'}</td></tr>`;
    }).join('');
  const P = R.permutation_importance, cols = Object.keys(Object.values(P)[0]);
  const CN = {port_mean_thou: 'bag port size', od_mean_thou: 'tube size', pressure_bar: 'pressure', port_sd_thou: 'bag unevenness', od_sd_thou: 'tube unevenness'};
  $('m-imp').innerHTML = '<tr><th>model</th>' + cols.map(c => `<th>${CN[c] || c}</th>`).join('') + '</tr>' +
    Object.entries(P).map(([k, v]) => `<tr><td><span class="term" data-model="${k}">${k}</span></td>${cols.map(c => `<td>${v[c].toFixed(3)}</td>`).join('')}</tr>`).join('');
  $('m-leak').innerHTML = `The actuator's end position explains ${(100 * R.leakage.r2_with_actuator).toFixed(0)} % of the depth on its own. But it is recorded <b>at the same moment</b> as the depth of the same device,
    so it cannot tell us anything <i>before</i> the device is made. Using it would be cheating (“target leakage”). It is only used for monitoring.`;
  const p = R.models.find(m => m.key === 'M03'), c = p.detail.coef;
  $('m-prod').innerHTML = `<b>depth (mm) = ${p.detail.intercept} + ${c.port_mean_thou} × bag port − ${Math.abs(c.od_mean_thou)} × tube + ${c.pressure_bar} × pressure</b>
    <br>(bag port and tube in thousandths of an inch, pressure in bar)<br><br>Every extra 0.001″ of bag port → ${(c.port_mean_thou).toFixed(2)} mm deeper.
    Every extra 0.001″ of tube → ${Math.abs(c.od_mean_thou).toFixed(2)} mm shallower. Every extra bar → ${c.pressure_bar.toFixed(2)} mm deeper.
    <br>Typical error ${p.tests.new_lots.rmse.toFixed(3)} mm on new lots and ${p.tests.unseen.rmse.toFixed(3)} mm on unseen settings.`;
  const g = R.models.find(m => m.key === 'M09'), nn = R.models.find(m => m.key === 'M13');
  $('m-verdict').innerHTML = `<h2>Verdict: keep the simple formula (${modelTag('M03')}) in production</h2>
    <p><b>1. Machine learning is barely more accurate.</b> On settings it has seen, the best ML model (${modelTag('M09')}) is only
      ${(p.tests.random.rmse - g.tests.random.rmse).toFixed(3)} mm better (${g.tests.random.rmse.toFixed(3)} vs ${p.tests.random.rmse.toFixed(3)} mm). The rest is normal part-to-part noise no model can predict.</p>
    <p><b>2. On a new setting, most models fall apart.</b> When a pressure, bag size or tube size was ${term('unseen', 'never seen in training')}, tree models get about 60 % worse,
      the lookup table can only guess the average, and the neural network is off by ${nn.tests.unseen.rmse.toFixed(1)} mm. The formula stays at ${p.tests.unseen.rmse.toFixed(3)} mm.</p>
    <p><b>3. Inside the twin, the choice hardly matters.</b> Every model that uses bag port, tube size and pressure predicts about the same share of failures
      within 2 devices (${term('jury', 'E ≤ 2')}: ${modelTag('M03')} ${R.twin.M03 ? R.twin.M03['E<=2_%'] : '–'} %, best ${Math.max(...Object.values(R.twin).map(t => t['E<=2_%'] || 0))} % on ${R.twin.M03 ? R.twin.M03.scenarios : '–'} scenarios).
      Without them (${modelTag('M00')}) the twin is less accurate and raises a false alarm.</p>
    <p><b>4. The formula is easy to validate.</b> 4 numbers with a physical meaning, checkable by hand. ML needs more validation (GAMP 5 category 5), monitoring for model drift,
      and cannot be explained to an inspector. The ${modelTag('M12')} is the only credible alternative.</p>`;
}

/* ------------------------------------------------------------------ AUDIT TRAIL */
let auditInit = false, ledgerBefore = null;
async function loadAudit() {
  if (auditInit) return;
  auditInit = true;
  $('unit-go').onclick = () => traceUnit($('unit-in').value);
  $('unit-in').onkeydown = e => { if (e.key === 'Enter') traceUnit($('unit-in').value); };
  const Q0 = await (await fetch('/api/quickunits')).json(), Q = Array.isArray(Q0) ? Q0 : [];
  if (Q0.error) { auditInit = false; $('trace').innerHTML = `<p class="note">${esc(Q0.error)}</p>`; return; }
  const LBL = {PREDICTION: 'predicted failure', EVALUATION: 'actual first failure', EXTREME: 'furthest from target', FIRST: 'first unit'};
  const seen = new Set();
  $('quick').innerHTML = 'Try: ' + Q.filter(q => !seen.has(q.unit_id) && seen.add(q.unit_id)).slice(0, 8)
    .map(q => `<button class="small" data-unit="${q.unit_id}">${q.unit_id} <span class="note">${q.line} · ${LBL[q.kind]}</span></button>`).join(' ');
  $('quick').querySelectorAll('[data-unit]').forEach(b => b.onclick = () => traceUnit(b.dataset.unit));
  $('verify-go').onclick = async () => {
    $('verify-out').textContent = 'checking every fingerprint…';
    const v = await (await fetch('/api/verify')).json();
    $('verify-out').innerHTML = v.error ? esc(v.error) : v.ok ? `<span class="pill ok">✓ intact</span> ${fmt(v.entries)} records verified at ${v.checked_utc.replace('T', ' ').slice(0, 19)} UTC`
      : `<span class="pill bad">✖ BROKEN</span> ${esc(v.problem)}`;
  };
  $('ledger-kind').onchange = () => { ledgerBefore = null; $('ledger').innerHTML = ''; loadLedger(); };
  $('ledger-more').onclick = loadLedger;
  await loadLedger();
}
async function loadLedger() {
  const k = $('ledger-kind').value || '';
  const L = await (await fetch(`/api/ledger?limit=40&kind=${k}` + (ledgerBefore ? `&before=${ledgerBefore}` : ''))).json();
  if (L.error) { $('ledger').innerHTML = `<tr><td class="note">${esc(L.error)}</td></tr>`; $('ledger-more').style.display = 'none'; return; }
  if (!$('ledger-kind').options.length)
    $('ledger-kind').innerHTML = '<option value="">all kinds</option>' + Object.entries(L.kinds).map(([kk, c]) => `<option value="${kk}">${kk} (${fmt(c)})</option>`).join('');
  if (!$('ledger').innerHTML) $('ledger').innerHTML = '<tr><th>#</th><th>kind</th><th>record ID</th><th>unit</th><th>source time</th><th>received (UTC)</th><th>fingerprint</th></tr>';
  $('ledger').insertAdjacentHTML('beforeend', L.rows.map(r => `<tr class="${r.unit_id && r.kind === 'ASM' ? 'click' : ''}" data-unit="${r.kind === 'ASM' ? r.unit_id || '' : ''}">
    <td>${r.seq}</td><td>${r.kind}</td><td class="mono">${esc(r.record_uid)}</td><td>${r.unit_id || ''}</td><td>${r.source_time || ''}</td>
    <td>${r.received_utc}</td><td class="mono" data-tip="entry fingerprint ${r.entry_hash}">${r.entry_hash.slice(0, 12)}…</td></tr>`).join(''));
  $('ledger').querySelectorAll('tr.click').forEach(tr => tr.onclick = () => { traceUnit(tr.dataset.unit); $('trace-card').scrollIntoView({behavior: 'smooth'}); });
  if (L.rows.length) ledgerBefore = L.rows[L.rows.length - 1].seq;
  $('ledger-more').style.display = L.rows.length < 40 ? 'none' : '';
}
async function traceUnit(id) {
  id = (id || '').trim();
  if (!id) return;
  if (!auditInit) await loadAudit();
  $('unit-in').value = id;
  $('trace').innerHTML = '<p class="note">tracing…</p>';
  const T = await (await fetch('/api/unit?id=' + encodeURIComponent(id))).json();
  if (T.error) {
    $('trace').innerHTML = `<p class="FAIL">${esc(T.error)}</p>` + ((T.suggestions || []).length ? '<p>Did you mean: ' +
      T.suggestions.map(s => `<button class="small" data-unit="${s}">${s}</button>`).join(' ') + '</p>' : '');
    $('trace').querySelectorAll('[data-unit]').forEach(b => b.onclick = () => traceUnit(b.dataset.unit));
    return;
  }
  const r = T.raw_response, b = T.bag, p = T.pvc, P = T.prediction, E = T.evaluation, I = T.inspection, L = T.ledger;
  const kv = o => '<table class="kv">' + Object.entries(o).map(([k, v]) => `<tr><th>${k}</th><td>${esc(v)}</td></tr>`).join('') + '</table>';
  const linkbox = (id, cls, title, html) => `<div class="link ${cls}" id="${id}"><h3>${title}</h3>${html}</div>`;
  $('trace').innerHTML = `
    <h3>${T.unit_id} · ${T.line_id} ${I.conforming ? '<span class="pill ok">inside 13–17 mm</span>' : '<span class="pill bad">OUT OF SPEC</span>'}
      ${T.role.map(x => `<span class="pill lock">${esc(x)}</span>`).join(' ')}</h3>
    <div class="chain" id="trace-chain">
      ${r ? linkbox('trace-raw', 'ok', '1 · Raw API response', `received ${r.received_utc}<br>request <code>${esc(r.request)}</code> (HTTP ${r.http_status})<br>
         record ${r.position} of ${r.records_in_response}<br><span class="mono">${r.record_uid}<br>sha256 ${r.payload_sha256.slice(0, 16)}…</span><br>
         ${r.matches_stored_record ? '<b class="PASS">✓ identical to the stored record</b>' : '<b class="FAIL">✖ differs from the stored record</b>'}`)
        : linkbox('trace-raw', 'none', '1 · Raw API response', 'Synthetic data: generated on this computer, no API response.')}
      ${linkbox('trace-rec', 'ok', '2 · Stored record', `<span class="mono">${L.record_uid}</span><br>event no. ${L.event_no}<br>source time ${L.source_time}<br>
         received ${L.received_utc}<br><span class="mono" data-tip="entry fingerprint ${L.entry_hash}">fingerprint ${L.entry_hash.slice(0, 16)}…</span>`)}
      ${b ? linkbox('trace-bag', 'ok', '3 · Bag lot ' + esc(T.record.bag_lot_id), `${b.records} port measurements (BAG-01)<br>average ${b.mean.toFixed(5)} in
         (${b.min}–${b.max})<br>measured ${b.first_time} → ${b.last_time}<br><span class="mono">${b.first_uid} … ${b.last_uid.split('/').pop()}</span>`)
        : linkbox('trace-bag', 'bad', '3 · Bag lot', 'No BAG-01 data for this lot.')}
      ${p ? linkbox('trace-pvc', 'ok', '4 · Tube reel ' + esc(T.record.reel_id), `${p.records} OD readings (PVC-01)<br>average ${p.mean.toFixed(5)} in
         (${p.min}–${p.max})<br>extruded ${p.first_time} → ${p.last_time}<br><span class="mono">${p.first_uid} … ${p.last_uid.split('/').pop()}</span>`)
        : linkbox('trace-pvc', 'bad', '4 · Tube reel', 'No PVC-01 data for this reel.')}
      ${P ? linkbox('trace-pred', 'lockc', '5 · Prediction for ' + T.line_id, `predicted first failure <b>${P.predicted_unit_id}</b><br>while processing ${P.current_unit_id}<br>
         locked ${P.prediction_time_utc}<br>${term('lead', 'lead time')} L = X − Y = <b>${P.units_ahead}</b><br><span class="mono">${P.record_uid}<br>fingerprint ${P.entry_hash.slice(0, 16)}…</span>`)
        : linkbox('trace-pred', 'none', '5 · Prediction for ' + T.line_id, 'No prediction on this line: no de-calibration was detected.')}
      ${linkbox('trace-insp', I.conforming ? 'ok' : 'bad', '6 · Inspection result', `camera depth <b>${I.depth_mm} mm</b><br>limits ${I.limits_mm.join('–')} mm →
         ${I.conforming ? 'conforming' : '<b class="FAIL">nonconforming</b>'}` + (E ? `<br>line's first failure: <b>${E.actual_first_failure_unit_id}</b><br>prediction result: <b>${E.status}</b>` +
           (E.E_abs_error_units != null ? `<br>${term('jury', 'jury error')} E = |Z − X| = <b>${E.E_abs_error_units}</b>` : '') : ''))}
    </div>
    <div class="grid2 tight"><div><h3>Original record, exactly as received</h3>${kv(T.record)}</div>
      <div><h3>Ledger entry</h3>${kv({'record ID': L.record_uid, 'ledger position': L.seq, 'event number': L.event_no, 'source time': L.source_time,
        'received (UTC)': L.received_utc, 'stored by': L.actor, 'content fingerprint': L.payload_sha256, 'previous entry fingerprint': L.prev_hash, 'entry fingerprint': L.entry_hash})}</div></div>`;
  history.replaceState(null, '', '#audit/trace');
}

/* ------------------------------------------------------------------ boot */
meta().catch(() => {});
tick(true).then(() => {
  const unit = new URLSearchParams(location.search).get('unit');      // shareable: /?unit=EFD_A1179
  if (unit) return goto('audit', 'trace-chain', unit);
  const [t, id] = (location.hash.slice(1) || 'live').split('/');
  return id ? goto(t, id) : show(t);
});
