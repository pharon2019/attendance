/* نظام الحضور والانصراف — Attendance System front end (vanilla JS, no build step). */
'use strict';

// ------------------------------------------------------------------ state & utilities
const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};
const S = { me: null, lang: ls.get('att_lang', 'ar'), lookups: null, offset: 180, timers: [], page: null };
const STAFF = ['admin', 'hr', 'manager'];
const ADMIN_HR = ['admin', 'hr'];
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const pad = n => String(n).padStart(2, '0');
const esc = v => v == null ? '' : String(v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function t(key, p) {
  let s = I18N[S.lang][key];
  if (s == null) s = I18N.ar[key];
  if (s == null) s = key;
  if (p) for (const k of Object.keys(p)) s = s.split('{' + k + '}').join(p[k]);
  return s;
}
const has = key => I18N[S.lang][key] != null;
function nm(o, base = 'name') {
  if (!o) return '';
  const other = S.lang === 'ar' ? 'en' : 'ar';
  return o[base + '_' + S.lang] || o[base + '_' + other] || '';
}
const role = () => S.me && S.me.user.role;
const isRole = list => list.includes(role());
const hasEmp = () => !!(S.me && S.me.employee);

// times are shown in the company time zone (settings.utc_offset_min), not the device's
const toCompany = isoStr => new Date(new Date(isoStr).getTime() + S.offset * 60000);
function fmtT(isoStr) { if (!isoStr) return '—'; const d = toCompany(isoStr); return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()); }
function fmtDT(isoStr) { if (!isoStr) return '—'; return toCompany(isoStr).toISOString().slice(0, 16).replace('T', ' '); }
const companyNow = () => new Date(Date.now() + S.offset * 60000);
const companyToday = () => companyNow().toISOString().slice(0, 10);
function addDays(ds, n) { const d = new Date(ds + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
function fmtDur(m) { if (!m) return '—'; return Math.floor(m / 60) + ':' + pad(m % 60); }
const fmtMin = m => m ? `${m} ${t('c.min')}` : '—';
const fmtH = m => (m / 60).toFixed(1);
function rowTime(ts, day) {  // report times are 'YYYY-MM-DD HH:MM' already in company time
  if (!ts) return '—';
  return esc(ts.slice(11)) + (ts.slice(0, 10) !== day ? ` <small class="muted">${t('r.next_day')}</small>` : '');
}
function longDate(d) {
  const loc = S.lang === 'ar' ? 'ar-SA-u-nu-latn-ca-gregory' : 'en-GB';
  return new Intl.DateTimeFormat(loc, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' }).format(d);
}
function uuid4() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16); crypto.getRandomValues(b); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
  const h = [...b].map(x => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
function haversine(lat1, lng1, lat2, lng2) {
  const r = 6371000, rad = Math.PI / 180;
  const dp = (lat2 - lat1) * rad, dl = (lng2 - lng1) * rad;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(a));
}
// All sites an employee may punch at (main location first). Each: { id, lat, lng, r, name }.
function empSites(emp) {
  if (!emp) return [];
  if (emp.sites) return emp.sites.map(l => ({ id: l.id, lat: l.lat, lng: l.lng, r: l.radius_m, name: nm(l) }));
  return emp.location_id ? [{ id: emp.location_id, lat: emp.loc_lat, lng: emp.loc_lng, r: emp.radius_m, name: nm(emp, 'loc') }] : [];
}
// Nearest site, preferring one whose radius contains the point.
function nearestSite(sites, lat, lng) {
  let best = null;
  for (const s of sites) {
    const d = haversine(lat, lng, s.lat, s.lng);
    if (!best || (d <= s.r) > (best.d <= best.site.r) || ((d <= s.r) === (best.d <= best.site.r) && d < best.d)) best = { site: s, d };
  }
  return best;
}
function debounce(fn, ms = 300) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }
function every(fn, ms) { S.timers.push(setInterval(fn, ms)); }

// ------------------------------------------------------------------ API
async function api(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'att' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    const e = new Error('network'); e.code = 'network'; throw e;
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const e = new Error((data && data.error) || 'server_error');
    e.code = (data && data.error) || (res.status >= 500 ? 'server_error' : 'network');
    e.params = (data && data.params) || {};
    e.status = res.status;
    if (res.status === 401 && url !== '/api/login') onUnauthorized();
    throw e;
  }
  return data;
}
function errText(e) {
  if (!e || !e.code) return (e && e.message) || t('err.server_error');
  const p = Object.assign({}, e.params || {});
  if (p.field) p.field = has('f.' + p.field) ? t('f.' + p.field) : (has('s.' + p.field) ? t('s.' + p.field) : p.field);
  return has('err.' + e.code) ? t('err.' + e.code, p) : t('err.server_error');
}
function onUnauthorized() {
  if (!S.me) return;
  S.me = null; ls.del('att_me');
  toast(t('err.unauthorized'), 'err');
  renderApp();
}
async function ensureLookups(force) {
  if (!S.lookups || force) S.lookups = await api('GET', '/api/lookups');
  return S.lookups;
}
const optDepts = () => S.lookups.departments.map(d => [d.id, nm(d)]);
const optShifts = () => S.lookups.shifts.map(d => [d.id, nm(d) + ` (${d.start_time}–${d.end_time})`]);
const optLocs = () => S.lookups.locations.map(d => [d.id, nm(d)]);
const optEmps = (exclude) => S.lookups.employees.filter(e => e.id !== exclude).map(e => [e.id, `${e.emp_no} — ${nm(e)}`]);
const optList = (prefix, keys) => keys.map(k => [k, t(prefix + k)]);

// ------------------------------------------------------------------ UI helpers
function toast(msg, type = 'ok', ms = 4500) {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.setAttribute('role', type === 'err' ? 'alert' : 'status');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.classList.add('hide'), ms);
  setTimeout(() => el.remove(), ms + 400);
}

function modal({ title, body, submit, onSubmit, onOpen, wide }) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'modal-wrap';
    wrap.innerHTML = `<div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
      <div class="modal-h"><h3>${esc(title)}</h3><button type="button" class="icon-btn" data-x aria-label="${t('c.close')}">✕</button></div>
      <form class="modal-b" novalidate>${body}<div class="form-err" role="alert" hidden></div>
      <div class="modal-f">${onSubmit ? `<button type="submit" class="btn primary">${esc(submit || t('c.save'))}</button>` : ''}
      <button type="button" class="btn" data-x>${onSubmit ? t('c.cancel') : t('c.close')}</button></div></form></div>`;
    document.body.appendChild(wrap);
    const form = $('form', wrap);
    const onKey = e => { if (e.key === 'Escape') close(null); };
    const close = v => { document.removeEventListener('keydown', onKey); wrap.remove(); resolve(v); };
    document.addEventListener('keydown', onKey);
    $$('[data-x]', wrap).forEach(b => { b.onclick = () => close(null); });
    wrap.addEventListener('mousedown', e => { if (e.target === wrap) close(null); });
    form.onsubmit = async e => {
      e.preventDefault();
      if (!onSubmit) return;
      const btn = $('[type=submit]', form), errEl = $('.form-err', form);
      btn.disabled = true; errEl.hidden = true;
      try {
        const r = await onSubmit(form);
        close(r === undefined ? true : r);
      } catch (err) {
        errEl.textContent = errText(err); errEl.hidden = false;
      } finally { btn.disabled = false; }
    };
    if (onOpen) onOpen(wrap, form);
    const first = $('input:not([type=checkbox]),select,textarea', form);
    if (first) first.focus();
  });
}
const confirmDialog = msg => modal({ title: t('c.confirm'), body: `<p>${esc(msg)}</p>`, submit: t('c.confirm'), onSubmit: async () => true });
const reasonDialog = (title, extra = '') => modal({
  title, submit: t('c.confirm'),
  body: `${extra}<div class="field full"><label for="f_reason">${t('c.reason')} <span class="req">*</span></label>
         <textarea id="f_reason" name="reason" rows="3" required placeholder="${esc(t('c.reason_hint'))}"></textarea></div>`,
  onSubmit: async form => {
    const v = form.elements.reason.value.trim();
    if (v.length < 3) { const e = new Error(); e.code = 'reason_required'; throw e; }
    return v;
  },
});

function fieldHtml(f, v) {
  const id = 'f_' + f.key;
  const lab = esc(f.label || t('f.' + f.key));
  const req = f.required ? ' required' : '';
  const label = `<label for="${id}">${lab}${f.required ? ' <span class="req">*</span>' : ''}</label>`;
  const hint = f.hint ? `<small class="hint">${esc(f.hint)}</small>` : '';
  switch (f.type) {
    case 'check':
      return `<div class="field check full"><label><input type="checkbox" name="${f.key}" ${v ? 'checked' : ''}> ${lab}</label>${hint}</div>`;
    case 'select':
      return `<div class="field ${f.full ? 'full' : ''}">${label}<select id="${id}" name="${f.key}"${req}>
        ${f.blank === false ? '' : `<option value="">${f.required ? '' : t('c.none')}</option>`}
        ${f.options().map(([val, l]) => `<option value="${esc(val)}" ${String(v) === String(val) ? 'selected' : ''}>${esc(l)}</option>`).join('')}
        </select>${hint}</div>`;
    case 'days': {
      const set = new Set(String(v ?? '').split(',').filter(Boolean));
      return `<div class="field full"><span class="lbl">${lab}${f.required ? ' <span class="req">*</span>' : ''}</span><div class="days">${[0, 1, 2, 3, 4, 5, 6].map(d =>
        `<label class="day"><input type="checkbox" name="${f.key}" value="${d}" ${set.has(String(d)) ? 'checked' : ''}><span>${t('days.short.' + d)}</span></label>`).join('')}</div></div>`;
    }
    case 'multi': {
      const set = new Set(String(v ?? '').split(',').filter(Boolean));
      return `<div class="field full"><span class="lbl">${lab}</span><div class="days">${f.options().map(([val, l]) =>
        `<label class="day"><input type="checkbox" name="${f.key}" value="${esc(val)}" ${set.has(String(val)) ? 'checked' : ''}><span>${esc(l)}</span></label>`).join('')}</div>${hint}</div>`;
    }
    case 'textarea':
      return `<div class="field full">${label}<textarea id="${id}" name="${f.key}" rows="3"${req}>${esc(v ?? '')}</textarea>${hint}</div>`;
    case 'map':
      return `<div class="field full"><div class="map small" id="pickmap"></div><small class="hint">${t('f.map_hint')}</small></div>`;
    default:
      return `<div class="field ${f.full ? 'full' : ''}">${label}<input id="${id}" name="${f.key}" type="${f.type || 'text'}" value="${esc(v ?? '')}"
        ${f.step ? `step="${f.step}"` : ''} ${f.dir ? `dir="${f.dir}"` : ''} autocomplete="${f.autocomplete || 'off'}"${req}>${hint}</div>`;
  }
}
function readForm(form, fields) {
  const o = {};
  for (const f of fields) {
    if (f.type === 'map') continue;
    if (f.type === 'check') o[f.key] = form.elements[f.key].checked;
    else if (f.type === 'days' || f.type === 'multi') o[f.key] = $$(`input[name="${f.key}"]:checked`, form).map(x => +x.value);
    else {
      const v = form.elements[f.key].value.trim();
      if (f.omitEmpty && v === '') continue;
      o[f.key] = v === '' ? null : v;
    }
  }
  return o;
}

function table(cols, rows, empty) {
  if (!rows.length) return `<div class="empty">${esc(empty || t('c.no_data'))}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${cols.map(c => `<th class="${c.cls || ''}">${esc(c.label)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr class="${r._cls || ''}">${cols.map(c => `<td class="${c.cls || ''}" data-label="${esc(c.label)}">${c.html(r)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function pager(el, data, go) {
  const n = Math.max(1, Math.ceil(data.total / data.size));
  el.innerHTML = `<div class="pager"><button class="btn sm" ${data.page <= 1 ? 'disabled' : ''} data-p="${data.page - 1}">${t('c.prev')}</button>
    <span>${t('c.page_of', { p: data.page, n, t: data.total })}</span>
    <button class="btn sm" ${data.page >= n ? 'disabled' : ''} data-p="${data.page + 1}">${t('c.next')}</button></div>`;
  $$('[data-p]', el).forEach(b => { b.onclick = () => go(+b.dataset.p); });
}
const badge = (status) => `<span class="badge st-${esc(status)}">${esc(t('st.' + status))}</span>`;
function flagBadges(r) {
  return r.flags.filter(f => f !== r.status && !(f === 'late_absent'))
    .map(f => `<span class="badge fl fl-${esc(f)}">${esc(t('fl.' + f))}</span>`).join(' ');
}
const yesNo = v => v ? `<span class="ok-mark">${t('c.yes')}</span>` : `<span class="muted">${t('c.no')}</span>`;
const loading = () => `<div class="loading">${t('c.loading')}</div>`;
function selectHtml(id, options, value, allLabel) {
  return `<select id="${id}">${allLabel ? `<option value="">${esc(allLabel)}</option>` : ''}${options.map(([v, l]) =>
    `<option value="${esc(v)}" ${String(v) === String(value ?? '') ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
}
function bars(items, max, cls = '') {
  if (!items.length) return `<div class="empty">${t('c.no_data')}</div>`;
  return `<div class="bars ${cls}">${items.map(i => {
    const pct = max ? Math.max(0, Math.min(100, (i.value / max) * 100)) : 0;
    return `<div class="bar-row"><span class="bar-label">${esc(i.label)}</span><span class="bar-track"><span class="bar-fill ${i.cls || ''}" style="width:${pct}%"></span></span><span class="bar-val">${esc(i.text ?? i.value)}</span></div>`;
  }).join('')}</div>`;
}
function printHead(title, sub) {
  const company = S.me ? (S.lang === 'ar' ? S.me.settings.company_ar : S.me.settings.company_en) : '';
  return `<div class="print-head"><strong>${esc(company)}</strong><span>${esc(title)}</span><span>${esc(sub || '')}</span>
    <small>${t('r.generated', { t: fmtDT(new Date().toISOString()) })}</small></div>`;
}

// ------------------------------------------------------------------ maps (Leaflet, online only)
function makeMap(el, lat, lng, zoom = 16) {
  if (!window.L || !navigator.onLine) {
    el.innerHTML = `<div class="map-fallback">${t('map.unavailable')}<br><span dir="ltr">${(+lat).toFixed(6)}, ${(+lng).toFixed(6)}</span></div>`;
    return null;
  }
  const m = L.map(el, { attributionControl: true }).setView([lat, lng], zoom);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(m);
  return m;
}
const gmapLink = (lat, lng) => `https://www.google.com/maps?q=${lat},${lng}`;
function gpsModal(title, points, fence) {
  points = points.filter(p => p && p.lat != null);
  if (!points.length) return;
  const fences = [].concat(fence || []);
  modal({
    title, wide: true,
    body: `<div class="map big" id="gmap"></div><div class="gps-list">${points.map(p => `<div class="gps-item">
      <b>${esc(p.label)}</b> <span dir="ltr">${p.lat.toFixed(6)}, ${p.lng.toFixed(6)}</span>
      ${p.accuracy != null ? ` • ${t('p.accuracy', { m: Math.round(p.accuracy) })}` : ''}
      ${p.distance_m != null && fences.length ? ` • ${t('rec.distance')}: ${Math.round(p.distance_m)} m` : ''}
      ${p.address ? `<br><small>${esc(p.address)}</small>` : ''}
      <br><a href="${gmapLink(p.lat, p.lng)}" target="_blank" rel="noopener">${t('map.open_google')}</a></div>`).join('')}</div>`,
    onOpen: w => setTimeout(() => {
      const m = makeMap($('#gmap', w), points[0].lat, points[0].lng, 16);
      if (!m) return;
      const pts = [];
      points.forEach(p => {
        L.marker([p.lat, p.lng]).addTo(m).bindTooltip(p.label, { permanent: true, direction: 'top', offset: [0, -30] });
        if (p.accuracy) L.circle([p.lat, p.lng], { radius: p.accuracy, color: '#2563eb', weight: 1, fillOpacity: 0.1 }).addTo(m);
        pts.push([p.lat, p.lng]);
      });
      fences.forEach(f => {
        L.circle([f.lat, f.lng], { radius: f.r, color: '#0f766e', dashArray: '6', fillOpacity: 0.05 }).addTo(m)
          .bindTooltip(t('map.fence') + ': ' + f.name);
        pts.push([f.lat, f.lng]);
      });
      if (pts.length > 1) m.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
    }, 60),
  });
}
function fenceForEmployee(empId) {
  if (!S.lookups) return null;
  const e = S.lookups.employees.find(x => x.id === empId);
  if (!e) return null;
  const ids = [e.location_id, ...String(e.extra_location_ids || '').split(',').map(Number)].filter(Boolean);
  return S.lookups.locations.filter(l => ids.includes(l.id)).map(l => ({ lat: l.lat, lng: l.lng, r: l.radius_m, name: nm(l) }));
}

// ------------------------------------------------------------------ offline punch queue
const Q = {
  all: () => ls.get('att_queue', []),
  save: q => ls.set('att_queue', q),
  mine() { return S.me ? this.all().filter(p => p.user_id === S.me.user.id) : []; },
  add(p) { const q = this.all(); q.push(p); this.save(q); },
  remove(id) { this.save(this.all().filter(p => p.uuid !== id)); },
};
let flushing = false;
async function flushQueue(manual) {
  if (flushing || !S.me || !navigator.onLine) return;
  const items = Q.mine();
  if (!items.length) return;
  flushing = true;
  let ok = 0;
  const log = ls.get('att_sync_log', []);
  try {
    for (const p of items) {
      const { user_id, ...body } = p;
      try {
        const res = await api('POST', '/api/punch', body);
        ok++; Q.remove(p.uuid);
        if (res.today) S.me.today = res.today;
      } catch (e) {
        if (e.code === 'network' || e.status === 401 || e.status >= 500) break;
        Q.remove(p.uuid);
        const msg = t('p.sync_rejected', { type: t('p.op_' + p.type), t: fmtDT(p.client_time), reason: errText(e) });
        log.unshift({ at: new Date().toISOString(), msg });
        toast(msg, 'err', 9000);
      }
    }
  } finally {
    flushing = false;
    ls.set('att_sync_log', log.slice(0, 10));
    if (S.me) ls.set('att_me', S.me);
  }
  if (ok) toast(t('p.synced_ok', { n: ok }));
  else if (manual && Q.mine().length) toast(t('err.network'), 'err');
  if (S.page === 'punch') updatePunchUI();
}

// ------------------------------------------------------------------ pages registry & router
const ICON = {
  punch: 'M12 2a10 10 0 100 20 10 10 0 000-20zm1 5v5.4l3.5 2.1-.8 1.3L11 13V7h2z',
  history: 'M4 4h16v2H4zm0 5h16v2H4zm0 5h10v2H4zm0 5h10v2H4z',
  dashboard: 'M3 3h8v8H3zm10 0h8v5h-8zM3 13h8v8H3zm10-3h8v11h-8z',
  employees: 'M16 11a4 4 0 10-4-4 4 4 0 004 4zM8 11a3 3 0 10-3-3 3 3 0 003 3zm8 2c-2.7 0-8 1.3-8 4v3h16v-3c0-2.7-5.3-4-8-4zM8 13c-.3 0-.7 0-1.1.1A4.4 4.4 0 019 17v3H1v-3c0-2.3 4.7-4 7-4z',
  shifts: 'M7 2v2H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V6a2 2 0 00-2-2h-2V2h-2v2H9V2zm-2 8h14v10H5z',
  locations: 'M12 2a7 7 0 00-7 7c0 5.3 7 13 7 13s7-7.7 7-13a7 7 0 00-7-7zm0 9.5A2.5 2.5 0 1114.5 9 2.5 2.5 0 0112 11.5z',
  departments: 'M3 21V7l9-4 9 4v14h-7v-6h-4v6z',
  leaves: 'M19 3h-1V1h-2v2H8V1H6v2H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V5a2 2 0 00-2-2zm0 16H5V8h14zm-9-3l-3-3 1.4-1.4 1.6 1.6 4.6-4.6L16 10z',
  punches: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zm-1 7V3.5L18.5 9zM8 13h8v2H8zm0 4h8v2H8z',
  daily: 'M3 13h2v8H3zm4-5h2v13H7zm4 3h2v10h-2zm4-6h2v16h-2zm4 9h2v7h-2z',
  monthly: 'M5 9.2h3V19H5zM10.6 5h2.8v14h-2.8zm5.6 8H19v6h-2.8z',
  users: 'M12 12a5 5 0 10-5-5 5 5 0 005 5zm0 2c-3.3 0-10 1.7-10 5v3h20v-3c0-3.3-6.7-5-10-5z',
  settings: 'M19.4 13a7.8 7.8 0 000-2l2.1-1.6-2-3.4-2.5 1a7.3 7.3 0 00-1.7-1L15 3.3h-4L10.6 6a7.3 7.3 0 00-1.7 1l-2.5-1-2 3.4L6.6 11a7.8 7.8 0 000 2l-2.2 1.6 2 3.4 2.5-1a7.3 7.3 0 001.7 1l.4 2.7h4l.4-2.7a7.3 7.3 0 001.7-1l2.5 1 2-3.4zM13 15.5A3.5 3.5 0 1116.5 12 3.5 3.5 0 0113 15.5z',
  audit: 'M12 1L3 5v6c0 5.6 3.8 10.7 9 12 5.2-1.3 9-6.4 9-12V5zm-2 16l-4-4 1.4-1.4 2.6 2.6 6.6-6.6L18 9z',
  profile: 'M12 2a10 10 0 100 20 10 10 0 000-20zm0 4a3 3 0 11-3 3 3 3 0 013-3zm0 14.2a7.2 7.2 0 01-6-3.2c0-2 4-3.1 6-3.1s6 1.1 6 3.1a7.2 7.2 0 01-6 3.2z',
  logout: 'M10 17l1.4-1.4-2.6-2.6H20v-2H8.8l2.6-2.6L10 7l-5 5zM4 5h8V3H4a2 2 0 00-2 2v14a2 2 0 002 2h8v-2H4z',
};
const icon = k => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${ICON[k] || ''}"/></svg>`;

const PAGES = {
  punch: { group: 'self', ok: () => hasEmp(), render: pagePunch },
  history: { group: 'self', ok: () => hasEmp(), render: pageHistory },
  dashboard: { group: 'main', ok: () => isRole(STAFF), render: pageDashboard },
  punches: { group: 'main', ok: () => isRole(STAFF), render: pagePunches },
  daily: { group: 'reports', ok: () => isRole(STAFF), render: pageDaily },
  monthly: { group: 'reports', ok: () => isRole(STAFF), render: pageMonthly },
  employees: { group: 'setup', ok: () => isRole(STAFF), render: crudPage('employees') },
  leaves: { group: 'setup', ok: () => isRole(STAFF), render: crudPage('leaves') },
  shifts: { group: 'setup', ok: () => isRole(ADMIN_HR), render: crudPage('shifts') },
  locations: { group: 'setup', ok: () => isRole(ADMIN_HR), render: crudPage('locations') },
  departments: { group: 'setup', ok: () => isRole(ADMIN_HR), render: crudPage('departments') },
  users: { group: 'setup', ok: () => isRole(ADMIN_HR), render: crudPage('users') },
  settings: { group: 'setup', ok: () => isRole(['admin']), render: pageSettings },
  audit: { group: 'setup', ok: () => isRole(ADMIN_HR), render: pageAudit },
  profile: { group: null, ok: () => true, render: pageProfile },
};
const defaultPage = () => (isRole(STAFF) ? 'dashboard' : 'punch');
const currentPage = () => location.hash.replace(/^#\/?/, '').split('?')[0];

function applyLang() {
  document.documentElement.lang = S.lang;
  document.documentElement.dir = S.lang === 'ar' ? 'rtl' : 'ltr';
  document.title = t('app.title');
}
async function switchLang() {
  S.lang = S.lang === 'ar' ? 'en' : 'ar';
  ls.set('att_lang', S.lang);
  applyLang();
  if (S.me) { S.me.user.lang = S.lang; api('PUT', '/api/me/lang', { lang: S.lang }).catch(() => {}); }
  renderApp(true);
}

function renderApp(forceShell) {
  S.timers.forEach(clearInterval); S.timers = [];
  const root = $('#app');
  if (!S.me) { S.page = 'login'; root.innerHTML = ''; pageLogin(root); return; }
  let name = currentPage();
  if (!PAGES[name] || !PAGES[name].ok()) {
    name = defaultPage();
    if (currentPage() !== name) { history.replaceState(null, '', '#/' + name); }
  }
  S.page = name;
  if (forceShell || !$('#view')) renderShell();
  $$('.nav a').forEach(a => a.classList.toggle('active', a.dataset.page === name));
  $('#page-title').textContent = t('nav.' + name);
  document.body.classList.remove('nav-open');
  const view = $('#view');
  view.innerHTML = loading();
  view.scrollTop = 0;
  Promise.resolve(PAGES[name].render(view)).catch(e => {
    if (S.page === name) view.innerHTML = `<div class="banner err">${esc(errText(e))}</div>`;
  });
}
window.addEventListener('hashchange', () => renderApp());

function renderShell() {
  const u = S.me.user;
  const groups = ['self', 'main', 'reports', 'setup'];
  const nav = groups.map(g => {
    const items = Object.entries(PAGES).filter(([, p]) => p.group === g && p.ok());
    if (!items.length) return '';
    return `<div class="nav-group"><div class="nav-gt">${t('nav.g.' + g)}</div>${items.map(([k]) =>
      `<a href="#/${k}" data-page="${k}">${icon(k)}<span>${t('nav.' + k)}</span></a>`).join('')}</div>`;
  }).join('');
  const who = S.me.employee ? nm(S.me.employee) : u.username;
  const company = S.lang === 'ar' ? S.me.settings.company_ar : S.me.settings.company_en;
  $('#app').innerHTML = `
    <header class="topbar">
      <button class="icon-btn burger" id="burger" aria-label="menu">☰</button>
      <div class="brand"><img src="/icons/icon.svg" alt="" width="28" height="28"><div><b>${t('app.title')}</b><small>${esc(company)}</small></div></div>
      <h1 id="page-title" class="page-title"></h1>
      <span id="net" class="net ${navigator.onLine ? 'on' : 'off'}" title="online"></span>
      <button class="btn sm ghost" id="lang">${t('lang.switch')}</button>
    </header>
    <aside class="sidebar">
      <div class="who">${icon('profile')}<div><b>${esc(who)}</b><small>${t('role.' + u.role)}</small></div></div>
      <nav class="nav">${nav}<div class="nav-group"><a href="#/profile" data-page="profile">${icon('profile')}<span>${t('nav.profile')}</span></a>
      <a href="#" id="logout">${icon('logout')}<span>${t('nav.logout')}</span></a></div></nav>
    </aside>
    <div class="scrim" id="scrim"></div>
    <main id="view" class="view" tabindex="-1"></main>`;
  $('#lang').onclick = switchLang;
  $('#burger').onclick = () => document.body.classList.toggle('nav-open');
  $('#scrim').onclick = () => document.body.classList.remove('nav-open');
  $('#logout').onclick = async e => {
    e.preventDefault();
    try { await api('POST', '/api/logout', {}); } catch { /* offline logout still clears local state */ }
    S.me = null; S.lookups = null; ls.del('att_me');
    location.hash = '';
    renderApp();
  };
}
function setNetIndicator() {
  const n = $('#net'); if (n) n.className = 'net ' + (navigator.onLine ? 'on' : 'off');
  const b = $('#offline-banner'); if (b) b.hidden = navigator.onLine;
}

// ------------------------------------------------------------------ login
function pageLogin(root) {
  root.innerHTML = `<div class="login-page">
    <div class="login-card card">
      <div class="login-top"><img src="/icons/icon.svg" alt="" width="56" height="56"><button class="btn sm ghost" id="lang">${t('lang.switch')}</button></div>
      <h1>${t('app.title')}</h1><p class="muted">${t('login.subtitle')}</p>
      <form id="login-form" novalidate>
        <div class="field"><label for="u">${t('f.username')}</label><input id="u" name="username" autocomplete="username" dir="ltr" required></div>
        <div class="field"><label for="pw">${t('f.password')}</label><input id="pw" name="password" type="password" autocomplete="current-password" dir="ltr" required></div>
        <div class="form-err" role="alert" hidden></div>
        <button class="btn primary block" type="submit">${t('login.submit')}</button>
      </form>
      <details class="demo"><summary>${t('login.demo')}</summary>
        <div dir="ltr" class="demo-list"><code>admin / Admin@123</code><code>hr / Hr@12345</code><code>manager / Manager@123</code><code>${esc(t('login.demo_emp'))}</code></div>
      </details>
    </div></div>`;
  $('#lang', root).onclick = switchLang;
  $('#login-form').onsubmit = async e => {
    e.preventDefault();
    const f = e.target, err = $('.form-err', f), btn = $('button[type=submit]', f);
    err.hidden = true; btn.disabled = true;
    try {
      await api('POST', '/api/login', { username: f.username.value.trim(), password: f.password.value });
      await loadMe();
      if (S.me.user.lang && S.me.user.lang !== S.lang) { S.lang = S.me.user.lang; ls.set('att_lang', S.lang); applyLang(); }
      location.hash = '#/' + defaultPage();
      renderApp(true);
      flushQueue();
    } catch (ex) {
      err.textContent = errText(ex); err.hidden = false;
    } finally { btn.disabled = false; }
  };
  $('#u').focus();
}
async function loadMe() {
  S.me = await api('GET', '/api/me');
  S.offset = +S.me.settings.utc_offset_min;
  ls.set('att_me', S.me);
}

// ------------------------------------------------------------------ punch (mobile)
let lastFix = null;
function effectiveToday() {
  const td = S.me.today;
  if (!td) return null;
  // when offline the cached state may belong to a finished earlier day
  const today = companyToday();
  if (td.work_date < today && (td.out || !td.in) && !navigator.onLine) {
    return { work_date: today, in: null, out: null, scheduled: true, leave: null };
  }
  return td;
}
async function pagePunch(view) {
  if (navigator.onLine) {
    try { await loadMe(); } catch (e) { if (e.code !== 'network') throw e; }
  }
  const emp = S.me.employee;
  if (!emp) { view.innerHTML = `<div class="banner warn">${t('p.no_employee')}</div>`; return; }
  const overnight = emp.end_time && emp.end_time <= emp.start_time;
  const permAck = ls.get('att_perm_ack', false);
  view.innerHTML = `<div class="punch-page">
    <div id="offline-banner" class="banner warn" ${navigator.onLine ? 'hidden' : ''}>${t('p.offline_banner')}</div>
    <section class="card hello">
      <div><h2>${t('p.hello', { name: esc(nm(emp)) })}</h2>
      <div class="muted">${esc(emp.emp_no)} • ${esc(nm(emp, 'dept'))}</div></div>
      <div class="clock"><b id="clock-t" dir="ltr"></b><small id="clock-d"></small></div>
      ${emp.shift_id ? `<div class="shift-line">${icon('shifts')}<span>${t('p.your_shift')}: <b>${esc(nm(emp, 'shift'))}</b>
        <span dir="ltr">${esc(emp.start_time)} – ${esc(emp.end_time)}</span>${overnight ? ` <small class="tag">${t('overnight')}</small>` : ''}</span></div>` : ''}
      ${empSites(emp).length ? `<div class="shift-line">${icon('locations')}<span>${t('r.workplace')}: <b>${empSites(emp).map(s => esc(s.name)).join('، ')}</b></span></div>` : ''}
      <div id="day-notes"></div>
    </section>
    ${permAck ? '' : `<section class="card perm" id="perm"><h3>${t('p.perm_title')}</h3><p>${t('p.perm_body')}</p>
      <button class="btn sm" id="perm-ok">${t('p.perm_ok')}</button></section>`}
    <div id="perm-denied" class="banner warn" hidden>${t('p.perm_denied_hint')}</div>
    <section class="punch-buttons">
      <button class="btn-punch in" id="btn-in"><span class="big">${t('p.check_in')}</span><small id="in-sub"></small></button>
      <button class="btn-punch out" id="btn-out"><span class="big">${t('p.check_out')}</span><small id="out-sub"></small></button>
    </section>
    <section class="card"><h3>${t('p.loc_title')}</h3>
      <div id="loc-status" class="loc-status idle">${t('p.loc.idle')}</div>
      <div id="punch-map" class="map" hidden></div></section>
    <section class="card"><h3>${t('p.today_ops')}</h3><div id="ops"></div><div id="queue-box"></div></section>
  </div>`;
  const tick = () => {
    const d = companyNow();
    const ct = $('#clock-t'); if (!ct) return;
    ct.textContent = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
    $('#clock-d').textContent = longDate(d);
  };
  tick(); every(tick, 1000);
  if ($('#perm-ok')) $('#perm-ok').onclick = () => { ls.set('att_perm_ack', true); $('#perm').remove(); };
  if (navigator.permissions && navigator.permissions.query) {
    navigator.permissions.query({ name: 'geolocation' }).then(p => {
      const upd = () => { const el = $('#perm-denied'); if (el) el.hidden = p.state !== 'denied'; };
      upd(); p.onchange = upd;
    }).catch(() => {});
  }
  $('#btn-in').onclick = () => doPunch('in');
  $('#btn-out').onclick = () => doPunch('out');
  updatePunchUI();
  if (lastFix) showFix(lastFix);
}

function updatePunchUI() {
  if (S.page !== 'punch' || !$('#btn-in')) return;
  const emp = S.me.employee;
  const td = effectiveToday();
  const recent = Q.mine().filter(p => Date.now() - new Date(p.client_time).getTime() < 20 * 3600e3);
  const qIn = recent.find(p => p.type === 'in'), qOut = recent.find(p => p.type === 'out');
  const hasIn = !!(td && td.in) || !!qIn, hasOut = !!(td && td.out) || !!qOut;
  const noShift = !emp.shift_id;
  $('#btn-in').disabled = noShift || hasIn;
  $('#btn-out').disabled = noShift || !hasIn || hasOut;
  $('#in-sub').textContent = td && td.in ? t('p.in_at', { t: fmtT(td.in.punch_time) }) : (qIn ? t('p.pending') : '');
  $('#out-sub').textContent = td && td.out ? t('p.out_at', { t: fmtT(td.out.punch_time) }) : (qOut ? t('p.pending') : '');

  const notes = [];
  if (noShift) notes.push(`<div class="banner warn">${t('p.no_shift')}</div>`);
  else if (td) {
    notes.push(`<div class="muted small">${t('p.work_date', { d: esc(td.work_date) })}</div>`);
    if (td.leave) notes.push(`<div class="banner info">${t('p.on_leave', { type: t('lt.' + td.leave.type) })}${td.leave.note ? ' — ' + esc(td.leave.note) : ''}</div>`);
    else if (!td.scheduled) notes.push(`<div class="banner info">${t('p.offday')}</div>`);
    if (hasIn && hasOut) notes.push(`<div class="banner ok">${t('p.done_today')}</div>`);
  }
  $('#day-notes').innerHTML = notes.join('');

  const ops = [];
  const opRow = (p, pending) => `<div class="op ${p.type}">
      <span class="op-type">${t('p.op_' + p.type)}</span>
      <b dir="ltr">${fmtT(p.punch_time || p.client_time)}</b>
      <span class="op-meta">
        ${pending ? `<span class="badge st-not_yet">${t('p.pending')}</span>` : ''}
        ${!pending && p.is_offline ? `<span class="badge fl">${t('p.offline_flag')}</span><br><small>${t('p.recorded', { t: fmtDT(p.punch_time) })} • ${t('p.synced', { t: fmtDT(p.synced_at) })}</small>` : ''}
        ${!pending && p.source !== 'mobile' ? `<span class="badge fl">${t('rec.src.' + p.source)}</span>` : ''}
        ${p.accuracy != null ? `<small>${t('p.accuracy', { m: Math.round(p.accuracy) })}</small>` : ''}
        ${p.distance_m != null ? `<small>${p.in_fence ? '✓ ' + t('p.inside') : t('p.outside')} (${Math.round(p.distance_m)} m)</small>` : ''}
      </span>
      ${p.lat != null ? `<button class="btn sm ghost" data-map="${esc(p.uuid)}">${t('c.view_map')}</button>` : ''}</div>`;
  if (td && td.in) ops.push(opRow(td.in));
  if (td && td.out) ops.push(opRow(td.out));
  recent.forEach(p => ops.push(opRow(p, true)));
  $('#ops').innerHTML = ops.length ? ops.join('') : `<div class="empty">${t('p.no_ops')}</div>`;
  const all = [td && td.in, td && td.out, ...recent].filter(Boolean);
  $$('[data-map]', $('#ops')).forEach(b => {
    b.onclick = () => {
      const p = all.find(x => x.uuid === b.dataset.map);
      gpsModal(t('p.op_' + p.type), [{ label: t('p.op_' + p.type), lat: p.lat, lng: p.lng, accuracy: p.accuracy, address: p.address, distance_m: p.distance_m }], empSites(emp));
    };
  });
  const n = Q.mine().length;
  const log = ls.get('att_sync_log', []);
  $('#queue-box').innerHTML = (n ? `<div class="queue"><span>${t('p.queue', { n })}</span><button class="btn sm" id="sync-now">${t('p.sync_now')}</button></div>` : '') +
    (log.length ? `<details class="sync-log"><summary>${t('p.sync_log')}</summary>${log.map(l => `<div class="small">${esc(l.msg)}</div>`).join('')}</details>` : '');
  if ($('#sync-now')) $('#sync-now').onclick = () => flushQueue(true);
}

function setLoc(state, html) {
  const el = $('#loc-status');
  if (!el) return;
  el.className = 'loc-status ' + state;
  el.innerHTML = html;
}
function getPosition() {
  return new Promise((resolve, reject) => {
    if (!window.isSecureContext) return reject({ code: 'insecure' });
    if (!('geolocation' in navigator)) return reject({ code: 'unsupported' });
    navigator.geolocation.getCurrentPosition(resolve,
      e => reject({ code: e.code === 1 ? 'denied' : e.code === 3 ? 'timeout' : 'unavailable' }),
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
  });
}
async function reverseGeocode(lat, lng) {
  if (!navigator.onLine) return null;
  const ctl = new AbortController();
  const h = setTimeout(() => ctl.abort(), 3500);
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18&lat=${lat}&lon=${lng}&accept-language=${S.lang}`,
      { signal: ctl.signal, referrerPolicy: 'strict-origin-when-cross-origin' });
    const j = await r.json();
    return j.display_name || null;
  } catch { return null; } finally { clearTimeout(h); }
}
function showFix(fix) {
  const emp = S.me.employee;
  const lines = [`<b>${t('p.loc.ok')}</b>`,
    `${t('p.coords')}: <span dir="ltr">${fix.lat.toFixed(6)}, ${fix.lng.toFixed(6)}</span>`,
    t('p.accuracy', { m: Math.round(fix.accuracy) })];
  if (fix.address) lines.push(`${t('p.address')}: ${esc(fix.address)}`);
  if (fix.distance != null) {
    lines.push(t('p.distance', { loc: esc(fix.site.name), m: Math.round(fix.distance), r: fix.site.r }) +
      ` — <b class="${fix.inside ? 'ok-mark' : 'bad-mark'}">${fix.inside ? t('p.inside') : t('p.outside')}</b>`);
  }
  setLoc(fix.inside === false ? 'warn' : 'ok', lines.join('<br>'));
  const mapEl = $('#punch-map');
  if (!mapEl) return;
  mapEl.hidden = false;
  mapEl.innerHTML = '';
  if (mapEl._leaflet_id) mapEl._leaflet_id = null;
  const m = makeMap(mapEl, fix.lat, fix.lng, 17);
  if (!m) return;
  L.marker([fix.lat, fix.lng]).addTo(m);
  L.circle([fix.lat, fix.lng], { radius: fix.accuracy, color: '#2563eb', weight: 1, fillOpacity: 0.12 }).addTo(m);
  empSites(emp).forEach(s => {
    L.circle([s.lat, s.lng], { radius: s.r, color: '#0f766e', dashArray: '6', fillOpacity: 0.05 }).addTo(m).bindTooltip(s.name);
  });
  if (fix.site) m.fitBounds([[fix.lat, fix.lng], [fix.site.lat, fix.site.lng]], { padding: [40, 40], maxZoom: 17 });
}

let punching = false;
async function doPunch(type) {
  if (punching) return;
  punching = true;
  const emp = S.me.employee;
  const btns = [$('#btn-in'), $('#btn-out')];
  btns.forEach(b => { b.disabled = true; });
  btns[type === 'in' ? 0 : 1].classList.add('busy');
  try {
    setLoc('busy', `<span class="spinner"></span> ${t('p.loc.locating')}`);
    let pos = null, locErr = null;
    try { pos = await getPosition(); } catch (e) { locErr = e.code; }
    const sites = empSites(emp);
    const fence = !!emp.geofence_required && sites.length > 0;
    const needGps = fence || S.me.settings.require_gps === '1';
    let fix = null;
    if (pos) {
      fix = { lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy };
      const near = nearestSite(sites, fix.lat, fix.lng);
      if (near) {
        fix.site = near.site;
        fix.distance = near.d;
        fix.inside = near.d <= near.site.r;
      }
      lastFix = fix;
      showFix(fix);
    } else {
      setLoc('err', `<b>${t('p.loc.failed')}</b><br>${t('p.loc.' + locErr)}`);
      if (needGps) { toast(t('p.gps_needed') + ' ' + t('p.loc.' + locErr), 'err', 7000); return; }
    }
    // client-side pre-check (the server re-checks everything)
    if (fix && fence) {
      const maxAcc = +S.me.settings.max_accuracy_m;
      if (fix.accuracy > maxAcc) { toast(t('p.precheck_accuracy', { m: Math.round(fix.accuracy), max: maxAcc }), 'err', 8000); return; }
      if (!fix.inside) { toast(t('p.precheck_outside', { m: Math.round(fix.distance), loc: fix.site.name, r: fix.site.r }), 'err', 8000); return; }
    }
    if (fix) {
      fix.address = await reverseGeocode(fix.lat, fix.lng);
      if (fix.address) showFix(fix);
    }
    const p = {
      uuid: uuid4(), type, client_time: new Date().toISOString(),
      lat: fix ? fix.lat : null, lng: fix ? fix.lng : null, accuracy: fix ? fix.accuracy : null,
      address: fix ? fix.address : null, loc_error: locErr,
    };
    const queueIt = () => {
      Q.add({ ...p, offline: true, user_id: S.me.user.id });
      toast(t('p.saved_offline'), 'warn', 8000);
    };
    if (!navigator.onLine) { queueIt(); return; }
    try {
      const res = await api('POST', '/api/punch', p);
      S.me.today = res.today;
      ls.set('att_me', S.me);
      toast(t(type === 'in' ? 'p.ok_in' : 'p.ok_out', { t: fmtT(res.punch.punch_time) }), 'ok', 6000);
    } catch (e) {
      if (e.code === 'network') queueIt();
      else toast(errText(e), 'err', 8000);
    }
  } finally {
    punching = false;
    btns.forEach(b => b && b.classList.remove('busy'));
    updatePunchUI();
  }
}

// ------------------------------------------------------------------ my history
async function pageHistory(view) {
  const data = await api('GET', '/api/my/punches');
  const emp = S.me.employee;
  const fence = empSites(emp);
  view.innerHTML = `<div class="page-h"><h2>${t('h.title')}</h2></div><div class="card" id="h-tbl"></div>`;
  $('#h-tbl').innerHTML = table([
    { label: t('c.date'), html: r => `<span dir="ltr">${esc(r.date)}</span><br><small class="muted">${t('day.' + new Date(r.date + 'T00:00:00Z').getUTCDay())}</small>` },
    { label: t('f.status'), html: r => badge(r.status) + ' ' + flagBadges(r) },
    { label: t('r.in'), html: r => rowTime(r.in_time, r.date) },
    { label: t('r.out'), html: r => rowTime(r.out_time, r.date) },
    { label: t('r.late'), html: r => fmtMin(r.late_min) },
    { label: t('r.work'), html: r => fmtDur(r.work_min) },
    { label: t('r.gps'), html: r => (r.in_gps || r.out_gps) ? `<button class="btn sm ghost" data-g="${esc(r.date)}">${t('c.view_map')}</button>` : '—' },
  ], data.items);
  $$('[data-g]', view).forEach(b => {
    b.onclick = () => {
      const r = data.items.find(x => x.date === b.dataset.g);
      gpsModal(r.date, [r.in_gps && { label: t('r.in'), ...r.in_gps }, r.out_gps && { label: t('r.out'), ...r.out_gps }], fence);
    };
  });
}

// ------------------------------------------------------------------ dashboard
async function pageDashboard(view) {
  const load = async () => {
    const d = await api('GET', '/api/dashboard');
    if (S.page !== 'dashboard') return;
    const c = d.counts;
    const onLeave = (c.leave || 0) + (c.mission || 0) + (c.exempt || 0);
    const card = (k, v, cls, href) => `<a class="stat ${cls}" ${href ? `href="${href}"` : ''}><span>${t(k)}</span><b>${v || 0}</b></a>`;
    const list = (items, fmt) => items.length ? `<ul class="alert-list">${items.map(fmt).join('')}</ul>` : `<div class="empty small">${t('d.no_alerts')}</div>`;
    const who = r => `<b>${esc(nm(r))}</b> <small class="muted">${esc(r.emp_no)} • ${esc(nm(r, 'dept'))} • ${esc(nm(r, 'shift'))}</small>`;
    const rate = c.scheduled ? Math.round(100 * Math.min(c.present, c.scheduled - onLeave) / Math.max(1, c.scheduled - onLeave)) : 0;
    view.innerHTML = `
      <div class="page-h"><h2>${t('d.title')} <small class="muted" dir="ltr">${esc(d.date)}</small></h2>
        <span class="muted small">${t('c.updated_at', { t: fmtT(new Date().toISOString()) })}</span></div>
      <div class="stats">
        ${card('d.scheduled', c.scheduled, 'neutral', '#/daily')}${card('d.present', c.present, 'good', '#/daily')}
        ${card('d.late', c.late_flag, 'warn', '#/daily')}${card('d.absent', c.absent, 'bad', '#/daily')}
        ${card('d.on_leave', onLeave, 'info', '#/leaves')}${card('d.working', c.working, 'neutral', '#/daily')}
        ${card('d.not_yet', c.not_yet, 'neutral', '#/daily')}${card('d.no_checkout', c.no_checkout, 'warn', '#/daily')}
      </div>
      <div class="grid-2">
        <section class="card"><h3>${t('d.by_dept')}</h3>
          ${bars(d.by_department.map(g => ({ label: nm(g), value: g.present, text: `${g.present} / ${g.scheduled}` })), Math.max(1, ...d.by_department.map(g => g.scheduled)))}
          <div class="ring-wrap"><div class="ring" style="--p:${rate}"><b>${rate}%</b></div><span class="muted">${t('r.rate')}</span></div>
        </section>
        <section class="card"><h3>${t('d.trend')}</h3>
          <div class="vbars">${d.trend.map(x => `<div class="vbar"><span class="vbar-v">${x.rate == null ? '—' : Math.round(x.rate) + '%'}</span>
            <span class="vbar-track"><span class="vbar-fill" style="height:${x.rate || 0}%"></span></span>
            <small dir="ltr">${x.date.slice(5)}</small><small>${t('days.short.' + new Date(x.date + 'T00:00:00Z').getUTCDay())}</small></div>`).join('')}</div>
        </section>
      </div>
      <h3 class="section-t">${t('d.alerts')}</h3>
      <div class="grid-2">
        <section class="card alert bad"><h4>${t('d.al_absent')} <span class="count">${d.alerts.absent.length}</span></h4>${list(d.alerts.absent, r => `<li>${who(r)}</li>`)}</section>
        <section class="card alert warn"><h4>${t('d.al_no_checkout')} <span class="count">${d.alerts.no_checkout.length}</span></h4>
          ${list(d.alerts.no_checkout, r => `<li>${who(r)} <small dir="ltr">${esc(r.date)} • ${esc((r.in_time || '').slice(11))}</small></li>`)}</section>
        <section class="card alert warn"><h4>${t('d.al_late')} <span class="count">${d.alerts.late.length}</span></h4>
          ${list(d.alerts.late, r => `<li>${who(r)} <span class="badge st-late">${t('d.late_by', { m: r.late_min })}</span></li>`)}</section>
        <section class="card alert info"><h4>${t('d.al_rejected')} <span class="count">${d.alerts.rejected.length}</span></h4>
          ${list(d.alerts.rejected, r => `<li><b>${esc(nm(r) || '#' + r.entity_id)}</b> <small dir="ltr">${fmtDT(r.at)}</small><br><small>${esc(errText({ code: r.details.code, params: r.details }))}</small></li>`)}</section>
      </div>`;
  };
  await load();
  every(() => { if (navigator.onLine) load().catch(() => {}); }, 60000);
}

// ------------------------------------------------------------------ daily report
const dailyState = { date: null, shift_id: '', department_id: '', status: '', q: '' };
async function pageDaily(view) {
  await ensureLookups();
  if (!dailyState.date) dailyState.date = companyToday();
  const stOpts = ['present', 'late', 'early_leave', 'absent', 'leave', 'mission', 'exempt', 'no_checkout', 'working', 'not_yet', 'offday'].map(k => [k, t('st.' + k)]);
  view.innerHTML = `<div class="page-h no-print"><h2>${t('r.daily')}</h2><div class="btns">
      <a class="btn" id="xlsx">${t('c.export_xlsx')}</a><button class="btn" id="print">${t('c.print_pdf')}</button></div></div>
    <div class="toolbar card no-print">
      <input type="date" id="d-date" value="${esc(dailyState.date)}">
      ${selectHtml('d-shift', optShifts(), dailyState.shift_id, `${t('f.shift')}: ${t('c.all')}`)}
      ${selectHtml('d-dept', optDepts(), dailyState.department_id, `${t('f.dept')}: ${t('c.all')}`)}
      ${selectHtml('d-status', stOpts, dailyState.status, `${t('f.status')}: ${t('c.all')}`)}
      <input type="search" id="d-q" placeholder="${t('c.search')}" value="${esc(dailyState.q)}">
    </div><div id="d-out">${loading()}</div>`;
  const params = () => new URLSearchParams(Object.entries(dailyState).filter(([, v]) => v !== '' && v != null).map(([k, v]) => [k, v]));
  const load = async () => {
    $('#xlsx').href = '/api/reports/daily.xlsx?' + params() + '&lang=' + S.lang;
    const data = await api('GET', '/api/reports/daily?' + params());
    if (S.page !== 'daily') return;
    const sm = data.summary;
    const chips = ['present', 'late', 'early_leave', 'absent', 'leave', 'mission', 'exempt', 'no_checkout', 'working', 'not_yet', 'offday']
      .filter(k => sm[k] || sm['flag_' + k]).map(k => `<span class="chip st-${k}">${t('st.' + k)}: <b>${k === 'late' || k === 'early_leave' ? (sm['flag_' + k] || 0) : sm[k]}</b></span>`).join('');
    const groups = {};
    data.rows.forEach(r => { (groups[r.shift_id || 0] = groups[r.shift_id || 0] || []).push(r); });
    const showGps = r => (r.in_gps || r.out_gps) ? `<button class="btn sm ghost" data-g="${r.employee_id}">${t('c.view_map')}</button>` : '—';
    const cols = [
      { label: t('f.emp_no'), html: r => esc(r.emp_no) },
      { label: t('f.name'), html: r => `<b>${esc(nm(r))}</b>` },
      { label: t('f.dept'), html: r => esc(nm(r, 'dept')) },
      { label: t('r.in'), html: r => rowTime(r.in_time, r.date) + (r.in_offline ? ' <span class="badge fl">⚡</span>' : '') },
      { label: t('r.out'), html: r => rowTime(r.out_time, r.date) + (r.out_offline ? ' <span class="badge fl">⚡</span>' : '') },
      { label: t('f.status'), html: r => badge(r.status) + ' ' + flagBadges(r) + (r.leave_note ? `<br><small class="muted">${esc(r.leave_note)}</small>` : '') },
      { label: t('r.late'), html: r => fmtMin(r.late_min) },
      { label: t('r.early'), html: r => fmtMin(r.early_min) },
      { label: t('r.work'), html: r => fmtDur(r.work_min) },
      { label: t('r.ot'), html: r => fmtDur(r.overtime_min) },
      { label: t('r.gps'), cls: 'no-print', html: showGps },
    ];
    const sec = Object.values(groups).map(rows => {
      const r0 = rows[0];
      const sch = rows.filter(r => r.scheduled).length;
      return `<section class="card shift-sec"><div class="shift-h"><h3>${esc(nm(r0, 'shift') || t('c.none'))}</h3>
        <span dir="ltr" class="muted">${esc(r0.shift_start || '')} – ${esc(r0.shift_end || '')}</span>
        <span class="chip">${t('r.scheduled_n', { n: sch })}</span></div>${table(cols, rows)}</section>`;
    }).join('');
    const absent = data.rows.filter(r => r.status === 'absent');
    view.querySelector('#d-out').innerHTML = `${printHead(t('r.daily'), data.date)}
      <div class="chips">${chips || ''}<span class="chip">${t('c.total')}: <b>${sm.total}</b></span></div>
      ${sec || `<div class="card empty">${t('c.no_data')}</div>`}
      <section class="card absent-sec"><h3>${t('r.absentees')} <span class="count">${absent.length}</span></h3>
      ${table([
        { label: t('f.name'), html: r => `<b>${esc(nm(r))}</b>` }, { label: t('f.emp_no'), html: r => esc(r.emp_no) },
        { label: t('f.dept'), html: r => esc(nm(r, 'dept')) }, { label: t('f.shift'), html: r => esc(nm(r, 'shift')) },
        { label: t('c.date'), html: r => `<span dir="ltr">${esc(r.date)}</span>` },
      ], absent, t('r.no_absent'))}</section>`;
    $$('[data-g]', view).forEach(b => {
      b.onclick = () => {
        const r = data.rows.find(x => String(x.employee_id) === b.dataset.g);
        gpsModal(`${nm(r)} — ${r.date}`, [r.in_gps && { label: t('r.in') + ' ' + (r.in_time || '').slice(11), ...r.in_gps },
          r.out_gps && { label: t('r.out') + ' ' + (r.out_time || '').slice(11), ...r.out_gps }], fenceForEmployee(r.employee_id));
      };
    });
  };
  const bind = (id, key, ev = 'change') => { $(id).addEventListener(ev, e => { dailyState[key] = e.target.value; load().catch(err => toast(errText(err), 'err')); }); };
  bind('#d-date', 'date'); bind('#d-shift', 'shift_id'); bind('#d-dept', 'department_id'); bind('#d-status', 'status');
  $('#d-q').addEventListener('input', debounce(e => { dailyState.q = e.target.value.trim(); load().catch(err => toast(errText(err), 'err')); }));
  $('#print').onclick = () => window.print();
  await load();
}

// ------------------------------------------------------------------ monthly report
const monthlyState = { from: null, to: null, department_id: '', shift_id: '', employee_id: '', q: '' };
async function pageMonthly(view) {
  await ensureLookups();
  if (!monthlyState.from) { const td = companyToday(); monthlyState.from = td.slice(0, 8) + '01'; monthlyState.to = td; }
  view.innerHTML = `<div class="page-h no-print"><h2>${t('r.monthly')}</h2><div class="btns">
      <a class="btn" id="xlsx">${t('c.export_xlsx')}</a><button class="btn" id="print">${t('c.print_pdf')}</button></div></div>
    <div class="toolbar card no-print">
      <label class="inline">${t('c.month')} <input type="month" id="m-month" value="${esc(monthlyState.from.slice(0, 7))}"></label>
      <label class="inline">${t('c.from')} <input type="date" id="m-from" value="${esc(monthlyState.from)}"></label>
      <label class="inline">${t('c.to')} <input type="date" id="m-to" value="${esc(monthlyState.to)}"></label>
      ${selectHtml('m-dept', optDepts(), monthlyState.department_id, `${t('f.dept')}: ${t('c.all')}`)}
      ${selectHtml('m-shift', optShifts(), monthlyState.shift_id, `${t('f.shift')}: ${t('c.all')}`)}
      ${selectHtml('m-emp', optEmps(), monthlyState.employee_id, `${t('f.employee_id')}: ${t('c.all')}`)}
      <input type="search" id="m-q" placeholder="${t('c.search')}" value="${esc(monthlyState.q)}">
    </div><div id="m-out">${loading()}</div>`;
  const params = () => new URLSearchParams(Object.entries(monthlyState).filter(([, v]) => v !== '' && v != null));
  const numCols = [
    { label: t('r.scheduled_days'), html: r => r.scheduled_days },
    { label: t('r.present_days'), html: r => r.present_days },
    { label: t('r.absent_days'), html: r => r.absent_days ? `<b class="bad-mark">${r.absent_days}</b>` : 0 },
    { label: t('r.late_days'), html: r => r.late_days },
    { label: t('r.late_total'), html: r => r.late_min },
    { label: t('r.early_days'), html: r => r.early_days },
    { label: t('r.leave_days'), html: r => r.leave_days },
    { label: t('r.mission_days'), html: r => r.mission_days },
    { label: t('r.no_checkout_days'), html: r => r.no_checkout_days },
    { label: t('r.work_hours'), html: r => fmtH(r.work_min) },
    { label: t('r.ot_hours'), html: r => fmtH(r.overtime_min) },
    { label: t('r.rate'), html: r => r.attendance_rate == null ? '—' : `<span class="rate" style="--p:${r.attendance_rate}">${r.attendance_rate}%</span>` },
  ];
  const load = async () => {
    $('#xlsx').href = '/api/reports/monthly.xlsx?' + params() + '&lang=' + S.lang;
    const d = await api('GET', '/api/reports/monthly?' + params());
    if (S.page !== 'monthly') return;
    const tot = d.total;
    const kpi = (k, v) => `<div class="stat neutral"><span>${t(k)}</span><b>${v}</b></div>`;
    $('#m-out').innerHTML = `${printHead(t('r.monthly'), t('r.period', { a: d.from, b: d.to }))}
      <div class="muted small no-print">${t('r.period', { a: `<span dir="ltr">${esc(d.from)}</span>`, b: `<span dir="ltr">${esc(d.to)}</span>` })}</div>
      <div class="stats">${kpi('r.employees', tot.employees)}${kpi('r.present_days', tot.present_days)}${kpi('r.absent_days', tot.absent_days)}
        ${kpi('r.late_days', tot.late_days)}${kpi('r.early_days', tot.early_days)}${kpi('r.leave_days', tot.leave_days + tot.mission_days)}
        ${kpi('r.work_hours', fmtH(tot.work_min))}${kpi('r.rate', tot.attendance_rate == null ? '—' : tot.attendance_rate + '%')}</div>
      <section class="card"><h3>${t('r.rate_chart')}</h3>${bars(d.departments.map(g => ({ label: nm(g), value: g.attendance_rate || 0, text: (g.attendance_rate ?? '—') + '%' })), 100)}</section>
      <section class="card"><h3>${t('r.by_emp')}</h3>${table([
        { label: t('f.emp_no'), html: r => esc(r.emp_no) }, { label: t('f.name'), html: r => `<b>${esc(nm(r))}</b>` },
        { label: t('f.dept'), html: r => esc(nm(r, 'dept')) }, { label: t('f.shift'), html: r => esc(nm(r, 'shift')) }, ...numCols,
      ], d.employees.concat(d.employees.length ? [{ ...tot, _cls: 'total-row', emp_no: '', name_ar: t('c.total'), name_en: t('c.total') }] : []))}</section>
      <div class="grid-2">
        <section class="card"><h3>${t('r.by_dept')}</h3>${table([{ label: t('f.dept'), html: r => `<b>${esc(nm(r))}</b>` }, { label: t('r.employees'), html: r => r.employees }, ...numCols], d.departments)}</section>
        <section class="card"><h3>${t('r.by_shift')}</h3>${table([{ label: t('f.shift'), html: r => `<b>${esc(nm(r))}</b>` }, { label: t('r.employees'), html: r => r.employees }, ...numCols], d.shifts)}</section>
      </div>`;
  };
  const reload = () => load().catch(err => toast(errText(err), 'err'));
  $('#m-month').onchange = e => {
    const v = e.target.value; if (!v) return;
    const [y, m] = v.split('-').map(Number);
    monthlyState.from = `${v}-01`;
    const last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    monthlyState.to = last > companyToday() ? companyToday() : last;
    $('#m-from').value = monthlyState.from; $('#m-to').value = monthlyState.to;
    reload();
  };
  $('#m-from').onchange = e => { monthlyState.from = e.target.value; reload(); };
  $('#m-to').onchange = e => { monthlyState.to = e.target.value; reload(); };
  $('#m-dept').onchange = e => { monthlyState.department_id = e.target.value; reload(); };
  $('#m-shift').onchange = e => { monthlyState.shift_id = e.target.value; reload(); };
  $('#m-emp').onchange = e => { monthlyState.employee_id = e.target.value; reload(); };
  $('#m-q').addEventListener('input', debounce(e => { monthlyState.q = e.target.value.trim(); reload(); }));
  $('#print').onclick = () => window.print();
  await load();
}

// ------------------------------------------------------------------ punch records (review / correct)
const recState = { from: null, to: null, employee_id: '', department_id: '', source: '', q: '', page: 1 };
async function pagePunches(view) {
  await ensureLookups();
  if (!recState.from) { recState.to = companyToday(); recState.from = addDays(recState.to, -7); }
  const canEdit = isRole(ADMIN_HR);
  view.innerHTML = `<div class="page-h"><h2>${t('rec.title')}</h2>${canEdit ? `<button class="btn primary" id="add">+ ${t('rec.add')}</button>` : ''}</div>
    <div class="toolbar card">
      <label class="inline">${t('c.from')} <input type="date" id="r-from" value="${esc(recState.from)}"></label>
      <label class="inline">${t('c.to')} <input type="date" id="r-to" value="${esc(recState.to)}"></label>
      ${selectHtml('r-emp', optEmps(), recState.employee_id, `${t('f.employee_id')}: ${t('c.all')}`)}
      ${selectHtml('r-dept', optDepts(), recState.department_id, `${t('f.dept')}: ${t('c.all')}`)}
      ${selectHtml('r-src', [['mobile', t('rec.src.mobile')], ['manual', t('rec.src.manual')], ['edited', t('rec.src.edited')], ['offline', t('rec.offline')]], recState.source, `${t('f.source')}: ${t('c.all')}`)}
      <input type="search" id="r-q" placeholder="${t('c.search')}" value="${esc(recState.q)}">
    </div><div class="card"><div id="r-tbl">${loading()}</div><div id="r-pager"></div></div>`;
  const load = async () => {
    const { page, ...f } = recState;
    const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v !== '' && v != null));
    qs.set('page', page);
    const data = await api('GET', '/api/punches?' + qs);
    if (S.page !== 'punches') return;
    $('#r-tbl').innerHTML = table([
      { label: t('f.work_date'), html: r => `<span dir="ltr">${esc(r.work_date)}</span>` },
      { label: t('f.employee_id'), html: r => `<b>${esc(nm(r, 'emp'))}</b><br><small class="muted">${esc(r.emp_no)}</small>` },
      { label: t('f.type'), html: r => `<span class="badge ${r.type === 'in' ? 'st-present' : 'st-working'}">${t('rec.type.' + r.type)}</span>` },
      { label: t('f.time'), html: r => `<span dir="ltr">${fmtDT(r.punch_time)}</span>` },
      { label: t('f.source'), html: r => `${t('rec.src.' + r.source)}${r.is_offline ? ` <span class="badge fl">${t('rec.offline')}</span><br><small class="muted">${t('p.synced', { t: fmtDT(r.synced_at) })}</small>` : ''}${r.note ? `<br><small class="muted">${esc(r.note)}</small>` : ''}` },
      { label: t('rec.accuracy'), html: r => r.accuracy != null ? `±${Math.round(r.accuracy)} m` : '—' },
      { label: t('rec.distance'), html: r => r.distance_m != null ? `${Math.round(r.distance_m)} m ${r.in_fence ? '✓' : '✗'}` : '—' },
      { label: t('c.actions'), cls: 'actions', html: r => `${r.lat != null ? `<button class="btn sm ghost" data-map="${r.id}">${t('c.view_map')}</button>` : ''}
        ${canEdit ? `<button class="btn sm" data-edit="${r.id}">${t('c.edit')}</button> <button class="btn sm danger" data-del="${r.id}">${t('c.delete')}</button>` : ''}` },
    ], data.items);
    pager($('#r-pager'), data, p => { recState.page = p; load(); });
    const find = id => data.items.find(x => String(x.id) === id);
    $$('[data-map]', view).forEach(b => {
      b.onclick = () => { const r = find(b.dataset.map); gpsModal(`${nm(r, 'emp')} — ${t('rec.type.' + r.type)}`, [{ label: t('rec.type.' + r.type) + ' ' + fmtT(r.punch_time), ...r }], fenceForEmployee(r.employee_id)); };
    });
    $$('[data-edit]', view).forEach(b => {
      b.onclick = async () => {
        const r = find(b.dataset.edit);
        const local = toCompany(r.punch_time).toISOString().slice(0, 16);
        const ok = await modal({
          title: t('rec.edit'),
          body: `<p><b>${esc(nm(r, 'emp'))}</b> — ${t('rec.type.' + r.type)} — <span dir="ltr">${esc(r.work_date)}</span></p>
            ${fieldHtml({ key: 'time', type: 'datetime-local', required: true, hint: t('rec.time_hint') }, local)}
            ${fieldHtml({ key: 'reason', label: t('c.reason'), type: 'textarea', required: true, hint: t('c.reason_hint') }, '')}`,
          onSubmit: f => api('PUT', `/api/punches/${r.id}`, { time: f.elements.time.value, reason: f.elements.reason.value }),
        });
        if (ok) { toast(t('c.saved')); load(); }
      };
    });
    $$('[data-del]', view).forEach(b => {
      b.onclick = async () => {
        const r = find(b.dataset.del);
        const reason = await reasonDialog(t('rec.delete'), `<p>${t('c.confirm_delete')}</p><p><b>${esc(nm(r, 'emp'))}</b> — ${t('rec.type.' + r.type)} <span dir="ltr">${fmtDT(r.punch_time)}</span></p>`);
        if (!reason) return;
        try { await api('DELETE', `/api/punches/${r.id}`, { reason }); toast(t('c.deleted')); load(); } catch (e) { toast(errText(e), 'err'); }
      };
    });
  };
  const reload = () => { recState.page = 1; load().catch(err => toast(errText(err), 'err')); };
  $('#r-from').onchange = e => { recState.from = e.target.value; reload(); };
  $('#r-to').onchange = e => { recState.to = e.target.value; reload(); };
  $('#r-emp').onchange = e => { recState.employee_id = e.target.value; reload(); };
  $('#r-dept').onchange = e => { recState.department_id = e.target.value; reload(); };
  $('#r-src').onchange = e => { recState.source = e.target.value; reload(); };
  $('#r-q').addEventListener('input', debounce(e => { recState.q = e.target.value.trim(); reload(); }));
  if ($('#add')) {
    $('#add').onclick = async () => {
      const fields = [
        { key: 'employee_id', type: 'select', required: true, options: () => optEmps(), full: true },
        { key: 'type', type: 'select', required: true, blank: false, options: () => [['in', t('rec.type.in')], ['out', t('rec.type.out')]] },
        { key: 'work_date', type: 'date', required: true },
        { key: 'time', type: 'datetime-local', required: true, hint: t('rec.time_hint') },
        { key: 'reason', label: t('c.reason'), type: 'textarea', required: true, hint: t('c.reason_hint') },
      ];
      const now = companyNow().toISOString().slice(0, 16);
      const ok = await modal({
        title: t('rec.add'),
        body: `<div class="grid-form">${fields.map(f => fieldHtml(f, { work_date: now.slice(0, 10), time: now }[f.key])).join('')}</div>`,
        onSubmit: f => api('POST', '/api/punches', readForm(f, fields)),
      });
      if (ok) { toast(t('c.saved')); load(); }
    };
  }
  await load();
}

// ------------------------------------------------------------------ generic CRUD pages
const CRUD = {
  departments: {
    lookup: true,
    columns: () => [{ label: t('f.name_ar'), html: r => esc(r.name_ar) }, { label: t('f.name_en'), html: r => esc(r.name_en) }],
    fields: () => [{ key: 'name_ar', required: true }, { key: 'name_en', required: true, dir: 'ltr' }],
  },
  locations: {
    lookup: true, wide: true,
    columns: () => [
      { label: t('f.name'), html: r => `<b>${esc(nm(r))}</b>` },
      { label: t('f.location'), html: r => `<span dir="ltr">${(+r.lat).toFixed(5)}, ${(+r.lng).toFixed(5)}</span>` },
      { label: t('f.radius_m'), html: r => r.radius_m },
      { label: t('c.view_map'), html: r => `<button class="btn sm ghost" data-loc="${r.id}">${t('c.view_map')}</button>` },
    ],
    fields: () => [{ key: 'name_ar', required: true }, { key: 'name_en', required: true, dir: 'ltr' },
      { key: 'lat', type: 'number', step: 'any', required: true, dir: 'ltr' }, { key: 'lng', type: 'number', step: 'any', required: true, dir: 'ltr' },
      { key: 'radius_m', type: 'number', required: true }, { key: 'map', type: 'map' }],
    defaults: { lat: 24.7136, lng: 46.6753, radius_m: 200 },
    onOpen(w, form) {
      setTimeout(() => {
        const g = k => parseFloat(form.elements[k].value);
        const m = makeMap($('#pickmap', w), g('lat') || 24.7136, g('lng') || 46.6753, 15);
        if (!m) return;
        const mk = L.marker([g('lat'), g('lng')], { draggable: true }).addTo(m);
        const c = L.circle([g('lat'), g('lng')], { radius: g('radius_m') || 200, color: '#0f766e' }).addTo(m);
        const set = ll => { form.elements.lat.value = ll.lat.toFixed(6); form.elements.lng.value = ll.lng.toFixed(6); mk.setLatLng(ll); c.setLatLng(ll); };
        m.on('click', e => set(e.latlng));
        mk.on('dragend', () => set(mk.getLatLng()));
        const sync = () => { if (!isNaN(g('lat')) && !isNaN(g('lng'))) { mk.setLatLng([g('lat'), g('lng')]); c.setLatLng([g('lat'), g('lng')]); } c.setRadius(g('radius_m') || 0); };
        ['lat', 'lng', 'radius_m'].forEach(k => form.elements[k].addEventListener('input', sync));
      }, 60);
    },
    after(view, items) {
      $$('[data-loc]', view).forEach(b => {
        b.onclick = () => { const l = items.find(x => String(x.id) === b.dataset.loc); gpsModal(nm(l), [{ label: nm(l), lat: l.lat, lng: l.lng }], { lat: l.lat, lng: l.lng, r: l.radius_m, name: nm(l) }); };
      });
    },
  },
  shifts: {
    lookup: true, wide: true,
    columns: () => [
      { label: t('f.name'), html: r => `<b>${esc(nm(r))}</b>` },
      { label: t('r.shift_time'), html: r => `<span dir="ltr">${esc(r.start_time)} – ${esc(r.end_time)}</span>${r.end_time <= r.start_time ? ` <small class="tag">${t('overnight')}</small>` : ''}` },
      { label: t('f.work_days'), html: r => r.work_days.split(',').map(d => t('days.short.' + d)).join('، ') },
      { label: t('f.grace_in_min'), html: r => r.grace_in_min }, { label: t('f.grace_out_min'), html: r => r.grace_out_min },
      { label: t('f.break_min'), html: r => r.break_min }, { label: t('f.overtime_after_min'), html: r => r.overtime_after_min },
    ],
    fields: () => [{ key: 'name_ar', required: true }, { key: 'name_en', required: true, dir: 'ltr' },
      { key: 'start_time', type: 'time', required: true }, { key: 'end_time', type: 'time', required: true, hint: t('overnight') + ': 22:00 → 06:00' },
      { key: 'work_days', type: 'days', required: true },
      { key: 'grace_in_min', type: 'number', required: true }, { key: 'grace_out_min', type: 'number', required: true },
      { key: 'break_min', type: 'number', required: true }, { key: 'overtime_after_min', type: 'number', required: true }],
    defaults: { start_time: '08:00', end_time: '16:00', work_days: '0,1,2,3,4', grace_in_min: 15, grace_out_min: 10, break_min: 60, overtime_after_min: 30 },
  },
  employees: {
    lookup: true, wide: true,
    filters: () => [{ key: 'department_id', label: t('f.dept'), options: optDepts }, { key: 'shift_id', label: t('f.shift'), options: optShifts },
      { key: 'status', label: t('f.status'), options: () => optList('emp.', ['active', 'inactive', 'suspended']) }],
    columns: () => [
      { label: t('f.emp_no'), html: r => esc(r.emp_no) },
      { label: t('f.name'), html: r => `<b>${esc(nm(r))}</b><br><small class="muted">${esc(r.job_title || '')}</small>` },
      { label: t('f.dept'), html: r => esc(nm(r, 'dept')) },
      { label: t('f.shift'), html: r => esc(nm(r, 'shift')) },
      { label: t('f.location_id'), html: r => esc(nm(r, 'loc')) + (r.extra_location_ids ? ` <small class="muted">+${r.extra_location_ids.split(',').length}</small>` : '') +
        ((r.location_id || r.extra_location_ids) && r.geofence_required ? ' <span class="badge fl">GPS</span>' : '') },
      { label: t('f.manager_id'), html: r => esc(nm(r, 'mgr')) },
      { label: t('f.phone'), html: r => `<span dir="ltr">${esc(r.phone || '')}</span>` },
      { label: t('f.status'), html: r => `<span class="badge emp-${esc(r.status)}">${t('emp.' + r.status)}</span>${r.exempt_rules ? ` <span class="badge fl">${t('st.exempt')}</span>` : ''}` },
    ],
    fields: row => [{ key: 'emp_no', required: true, dir: 'ltr' }, { key: 'status', type: 'select', required: true, blank: false, options: () => optList('emp.', ['active', 'inactive', 'suspended']) },
      { key: 'name_ar', required: true }, { key: 'name_en', required: true, dir: 'ltr' },
      { key: 'department_id', type: 'select', options: optDepts }, { key: 'job_title' },
      { key: 'phone', type: 'tel', dir: 'ltr' }, { key: 'email', type: 'email', dir: 'ltr' },
      { key: 'shift_id', type: 'select', options: optShifts }, { key: 'location_id', type: 'select', options: optLocs },
      { key: 'extra_location_ids', type: 'multi', options: optLocs, hint: t('f.extra_location_hint') },
      { key: 'manager_id', type: 'select', options: () => optEmps(row && row.id) }, { key: 'hire_date', type: 'date' },
      { key: 'geofence_required', type: 'check' }, { key: 'exempt_rules', type: 'check' }],
    defaults: { status: 'active', geofence_required: 1 },
  },
  leaves: {
    write: STAFF,
    filters: () => [{ key: 'type', label: t('f.type'), options: () => optList('lt.', ['leave', 'mission', 'exempt']) },
      { key: 'employee_id', label: t('f.employee_id'), options: () => optEmps() }],
    columns: () => [
      { label: t('f.employee_id'), html: r => `<b>${esc(nm(r, 'emp'))}</b><br><small class="muted">${esc(r.emp_no)}</small>` },
      { label: t('f.type'), html: r => `<span class="badge st-${esc(r.type)}">${t('lt.' + r.type)}</span>` },
      { label: t('f.start_date'), html: r => `<span dir="ltr">${esc(r.start_date)}</span>` },
      { label: t('f.end_date'), html: r => `<span dir="ltr">${esc(r.end_date)}</span>` },
      { label: t('c.days_n', { n: '' }).trim(), html: r => Math.round((new Date(r.end_date) - new Date(r.start_date)) / 864e5) + 1 },
      { label: t('f.note'), html: r => esc(r.note || '') },
    ],
    fields: () => [{ key: 'employee_id', type: 'select', required: true, options: () => optEmps(), full: true },
      { key: 'type', type: 'select', required: true, blank: false, options: () => optList('lt.', ['leave', 'mission', 'exempt']) },
      { key: 'start_date', type: 'date', required: true }, { key: 'end_date', type: 'date', required: true },
      { key: 'note', type: 'textarea' }],
    defaults: () => ({ type: 'leave', start_date: companyToday(), end_date: companyToday() }),
  },
  users: {
    help: () => t('u.roles_help'),
    filters: () => [{ key: 'role', label: t('f.role'), options: () => optList('role.', ['admin', 'hr', 'manager', 'employee']) }],
    columns: () => [
      { label: t('f.username'), html: r => `<b dir="ltr">${esc(r.username)}</b>` },
      { label: t('f.role'), html: r => `<span class="badge role-${esc(r.role)}">${t('role.' + r.role)}</span>` },
      { label: t('u.linked'), html: r => r.employee_id ? `${esc(nm(r, 'emp'))} <small class="muted">${esc(r.emp_no)}</small>` : '—' },
      { label: t('f.active'), html: r => yesNo(r.active) + (r.locked_until && r.locked_until > new Date().toISOString() ? ` <span class="badge st-absent">${t('u.locked')}</span>` : '') },
      { label: t('f.lang'), html: r => r.lang === 'en' ? 'English' : 'العربية' },
    ],
    fields: row => [{ key: 'username', required: !row, dir: 'ltr', omitEmpty: !!row, autocomplete: 'off' },
      { key: 'role', type: 'select', required: true, blank: false, options: () => optList('role.', role() === 'admin' ? ['admin', 'hr', 'manager', 'employee'] : ['manager', 'employee']) },
      { key: 'employee_id', type: 'select', options: () => optEmps(), full: true },
      { key: 'password', type: 'password', required: !row, omitEmpty: true, autocomplete: 'new-password', dir: 'ltr', hint: row ? t('f.password_edit_hint') : t('pr.pw_rules') },
      { key: 'active', type: 'check' }],
    defaults: { role: 'employee', active: 1 },
    canEditRow: r => role() === 'admin' || !['admin', 'hr'].includes(r.role),
  },
};

function crudPage(entity) {
  return async function (view) {
    const cfg = CRUD[entity];
    await ensureLookups();
    const canWrite = isRole(cfg.write || ADMIN_HR);
    const st = { page: 1, q: '', f: {} };
    const filters = cfg.filters ? cfg.filters() : [];
    view.innerHTML = `<div class="page-h"><h2>${t('nav.' + entity)}</h2>${canWrite ? `<button class="btn primary" id="add">+ ${t('c.add')}</button>` : ''}</div>
      ${cfg.help ? `<div class="banner info small">${esc(cfg.help())}</div>` : ''}
      <div class="toolbar card"><input type="search" id="q" placeholder="${t('c.search')}">
      ${filters.map(f => `<select data-f="${f.key}"><option value="">${esc(f.label)}: ${t('c.all')}</option>${f.options().map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('')}</select>`).join('')}</div>
      <div class="card"><div id="tbl">${loading()}</div><div id="pager"></div></div>`;
    const load = async () => {
      const qs = new URLSearchParams({ page: st.page, size: 25, ...st.f });
      if (st.q) qs.set('q', st.q);
      const data = await api('GET', `/api/${entity}?${qs}`);
      if (S.page !== entity) return;
      const cols = cfg.columns();
      if (canWrite) {
        cols.push({
          label: t('c.actions'), cls: 'actions',
          html: r => (cfg.canEditRow && !cfg.canEditRow(r)) ? '' : `<button class="btn sm" data-edit="${r.id}">${t('c.edit')}</button> <button class="btn sm danger" data-del="${r.id}">${t('c.delete')}</button>`,
        });
      }
      $('#tbl', view).innerHTML = table(cols, data.items);
      pager($('#pager', view), data, p => { st.page = p; load(); });
      $$('[data-edit]', view).forEach(b => { b.onclick = () => edit(data.items.find(x => String(x.id) === b.dataset.edit)); });
      $$('[data-del]', view).forEach(b => { b.onclick = () => del(+b.dataset.del); });
      if (cfg.after) cfg.after(view, data.items);
    };
    const edit = async row => {
      const fields = cfg.fields(row);
      const defaults = typeof cfg.defaults === 'function' ? cfg.defaults() : (cfg.defaults || {});
      const ok = await modal({
        title: `${t(row ? 'c.edit' : 'c.add')} — ${t('nav.' + entity)}`, wide: cfg.wide,
        body: `<div class="grid-form">${fields.map(f => fieldHtml(f, row ? row[f.key] : defaults[f.key])).join('')}</div>`,
        onOpen: cfg.onOpen ? (w, form) => cfg.onOpen(w, form, row) : null,
        onSubmit: async form => {
          const body = readForm(form, fields);
          if (row) await api('PUT', `/api/${entity}/${row.id}`, body);
          else await api('POST', `/api/${entity}`, body);
        },
      });
      if (ok) { toast(t('c.saved')); if (cfg.lookup) await ensureLookups(true); load(); }
    };
    const del = async id => {
      if (!(await confirmDialog(t('c.confirm_delete')))) return;
      try { await api('DELETE', `/api/${entity}/${id}`); toast(t('c.deleted')); if (cfg.lookup) await ensureLookups(true); load(); } catch (e) { toast(errText(e), 'err', 7000); }
    };
    $('#q', view).addEventListener('input', debounce(e => { st.q = e.target.value.trim(); st.page = 1; load().catch(err => toast(errText(err), 'err')); }));
    $$('[data-f]', view).forEach(s => {
      s.onchange = () => { if (s.value) st.f[s.dataset.f] = s.value; else delete st.f[s.dataset.f]; st.page = 1; load().catch(err => toast(errText(err), 'err')); };
    });
    if ($('#add', view)) $('#add', view).onclick = () => edit(null);
    await load();
  };
}

// ------------------------------------------------------------------ settings
async function pageSettings(view) {
  const { settings: s } = await api('GET', '/api/settings');
  const groups = [
    ['s.company', [['company_ar', 'text'], ['company_en', 'text'], ['utc_offset_min', 'number']]],
    ['s.rules', [['late_enabled', 'check'], ['early_enabled', 'check'], ['overtime_enabled', 'check'], ['absent_if_late_over_min', 'number'], ['no_checkout_after_min', 'number']]],
    ['s.gps', [['require_gps', 'check'], ['max_accuracy_m', 'number'], ['max_offline_hours', 'number']]],
  ];
  const fields = groups.flatMap(([, fs]) => fs.map(([key, type]) => ({ key, type, label: t('s.' + key), dir: key === 'company_en' ? 'ltr' : undefined })));
  fields.push({ key: 'backup_keep', type: 'number', label: t('s.backup_keep') });
  view.innerHTML = `<div class="page-h"><h2>${t('s.title')}</h2></div>
    <form id="s-form" class="settings">
      ${groups.map(([g, fs]) => `<section class="card"><h3>${t(g)}</h3><div class="grid-form">${fs.map(([key, type]) =>
        fieldHtml({ key, type, label: t('s.' + key), dir: key === 'company_en' ? 'ltr' : undefined }, type === 'check' ? s[key] === '1' : s[key])).join('')}</div></section>`).join('')}
      <section class="card"><h3>${t('s.backup')}</h3><p class="muted small" id="bk-note">${t('s.backup_auto')}</p>
        <div class="grid-form">${fieldHtml({ key: 'backup_keep', type: 'number', label: t('s.backup_keep') }, s.backup_keep)}</div>
        <button type="button" class="btn" id="bk-now">${t('s.backup_now')}</button><div id="bk-list"></div></section>
      <div class="sticky-save"><button class="btn primary" type="submit">${t('c.save')}</button></div>
    </form>`;
  const loadBackups = async () => {
    const { items, managed } = await api('GET', '/api/backups');
    if (managed) { // PostgreSQL (Neon): restore points are managed by the database service
      $('#bk-note').textContent = t('s.backup_managed');
      $('#bk-now').hidden = true;
      return;
    }
    $('#bk-list').innerHTML = table([
      { label: t('f.name'), html: r => `<span dir="ltr">${esc(r.name)}</span>` },
      { label: t('a.at'), html: r => `<span dir="ltr">${fmtDT(r.at)}</span>` },
      { label: t('s.size'), html: r => (r.size / 1024).toFixed(0) + ' KB' },
      { label: '', html: r => `<a class="btn sm" href="/api/backups/${encodeURIComponent(r.name)}">${t('s.download')}</a>` },
    ], items);
  };
  $('#s-form').onsubmit = async e => {
    e.preventDefault();
    const body = readForm(e.target, fields);
    try {
      await api('PUT', '/api/settings', body);
      await loadMe();
      toast(t('c.saved'));
      renderApp(true);
    } catch (err) { toast(errText(err), 'err'); }
  };
  $('#bk-now').onclick = async () => {
    try { const r = await api('POST', '/api/backups', {}); toast(t('s.backup_done', { n: r.name })); loadBackups(); } catch (err) { toast(errText(err), 'err'); }
  };
  await loadBackups();
}

// ------------------------------------------------------------------ audit log
const auditState = { page: 1, q: '', action: '', entity: '', from: '', to: '' };
async function pageAudit(view) {
  const actions = ['login', 'logout', 'login_failed', 'create', 'update', 'delete', 'punch_in', 'punch_out', 'punch_rejected', 'export', 'backup', 'backup_download', 'password_changed'];
  const ents = ['employees', 'employee', 'shifts', 'locations', 'departments', 'leaves', 'users', 'user', 'punches', 'punch', 'settings', 'backup', 'report_daily', 'report_monthly'];
  view.innerHTML = `<div class="page-h"><h2>${t('a.title')}</h2></div>
    <div class="toolbar card">
      ${selectHtml('a-action', actions.map(a => [a, t('act.' + a)]), auditState.action, `${t('a.action')}: ${t('c.all')}`)}
      ${selectHtml('a-entity', ents.map(a => [a, t('ent.' + a)]), auditState.entity, `${t('a.entity')}: ${t('c.all')}`)}
      <label class="inline">${t('c.from')} <input type="date" id="a-from" value="${esc(auditState.from)}"></label>
      <label class="inline">${t('c.to')} <input type="date" id="a-to" value="${esc(auditState.to)}"></label>
      <input type="search" id="a-q" placeholder="${t('c.search')}" value="${esc(auditState.q)}">
    </div><div class="card"><div id="a-tbl">${loading()}</div><div id="a-pager"></div></div>`;
  const details = r => {
    if (!r.details) return '';
    let d; try { d = JSON.parse(r.details); } catch { return esc(r.details); }
    if (r.action === 'punch_rejected' && d.code) return esc(errText({ code: d.code, params: d }));
    return `<code class="details">${esc(Object.entries(d).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(' → ') : (typeof v === 'object' && v ? JSON.stringify(v) : v)}`).join(' | ')).slice(0, 400)}</code>`;
  };
  const load = async () => {
    const { page, ...f } = auditState;
    const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v));
    qs.set('page', page);
    const data = await api('GET', '/api/audit?' + qs);
    if (S.page !== 'audit') return;
    $('#a-tbl').innerHTML = table([
      { label: t('a.at'), html: r => `<span dir="ltr">${fmtDT(r.at)}</span>` },
      { label: t('a.user'), html: r => esc(r.username || t('a.system')) },
      { label: t('a.action'), html: r => `<span class="badge act-${esc(r.action)}">${esc(has('act.' + r.action) ? t('act.' + r.action) : r.action)}</span>` },
      { label: t('a.entity'), html: r => r.entity ? `${esc(has('ent.' + r.entity) ? t('ent.' + r.entity) : r.entity)}${r.entity_id ? ' #' + r.entity_id : ''}` : '—' },
      { label: t('c.details'), html: details },
      { label: t('c.reason'), html: r => esc(r.reason || '') },
      { label: t('a.ip'), html: r => `<small dir="ltr">${esc(r.ip || '')}</small>` },
    ], data.items);
    pager($('#a-pager'), data, p => { auditState.page = p; load(); });
  };
  const reload = () => { auditState.page = 1; load().catch(err => toast(errText(err), 'err')); };
  $('#a-action').onchange = e => { auditState.action = e.target.value; reload(); };
  $('#a-entity').onchange = e => { auditState.entity = e.target.value; reload(); };
  $('#a-from').onchange = e => { auditState.from = e.target.value; reload(); };
  $('#a-to').onchange = e => { auditState.to = e.target.value; reload(); };
  $('#a-q').addEventListener('input', debounce(e => { auditState.q = e.target.value.trim(); reload(); }));
  await load();
}

// ------------------------------------------------------------------ profile
async function pageProfile(view) {
  const u = S.me.user, e = S.me.employee;
  view.innerHTML = `<div class="page-h"><h2>${t('pr.title')}</h2></div>
    <div class="grid-2">
      <section class="card"><dl class="kv">
        <dt>${t('f.username')}</dt><dd dir="ltr">${esc(u.username)}</dd>
        <dt>${t('f.role')}</dt><dd>${t('role.' + u.role)}</dd>
        ${e ? `<dt>${t('f.name')}</dt><dd>${esc(nm(e))}</dd><dt>${t('f.emp_no')}</dt><dd>${esc(e.emp_no)}</dd>
        <dt>${t('f.dept')}</dt><dd>${esc(nm(e, 'dept'))}</dd><dt>${t('f.shift')}</dt><dd>${esc(nm(e, 'shift'))}</dd>` : ''}
        <dt>${t('pr.lang')}</dt><dd><button class="btn sm" id="pl">${t('lang.switch')}</button></dd></dl></section>
      <section class="card"><h3>${t('pr.change_pw')}</h3>
        <form id="pw-form" novalidate>
          <div class="field"><label for="op">${t('pr.old')}</label><input id="op" type="password" autocomplete="current-password" dir="ltr" required></div>
          <div class="field"><label for="np">${t('pr.new')}</label><input id="np" type="password" autocomplete="new-password" dir="ltr" required><small class="hint">${t('pr.pw_rules')}</small></div>
          <div class="field"><label for="cp">${t('pr.confirm')}</label><input id="cp" type="password" autocomplete="new-password" dir="ltr" required></div>
          <div class="form-err" role="alert" hidden></div>
          <button class="btn primary" type="submit">${t('c.save')}</button></form></section></div>`;
  $('#pl').onclick = switchLang;
  $('#pw-form').onsubmit = async ev => {
    ev.preventDefault();
    const err = $('.form-err', ev.target);
    err.hidden = true;
    if ($('#np').value !== $('#cp').value) { err.textContent = t('pr.mismatch'); err.hidden = false; return; }
    try {
      await api('PUT', '/api/me/password', { old_password: $('#op').value, new_password: $('#np').value });
      toast(t('c.saved')); ev.target.reset();
    } catch (ex) { err.textContent = errText(ex); err.hidden = false; }
  };
}

// ------------------------------------------------------------------ boot
async function boot() {
  applyLang();
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  window.addEventListener('online', () => { setNetIndicator(); flushQueue(); });
  window.addEventListener('offline', setNetIndicator);
  try {
    await loadMe();
    if (S.me.user.lang && S.me.user.lang !== S.lang) { S.lang = S.me.user.lang; ls.set('att_lang', S.lang); applyLang(); }
  } catch (e) {
    if (e.code === 'network') {
      S.me = ls.get('att_me', null);  // offline: keep working from the cached profile
      if (S.me) S.offset = +S.me.settings.utc_offset_min;
    } else S.me = null;
  }
  renderApp(true);
  flushQueue();
  setInterval(() => flushQueue(), 30000);
}
document.addEventListener('DOMContentLoaded', boot);
