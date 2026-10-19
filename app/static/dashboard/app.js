'use strict';
// MME Voice Console. Every value shown comes from the backend API; missing data is shown as an em dash,
// never invented. All text is inserted as DOM text nodes (no HTML-string APIs), so API data cannot inject markup.
const $ = (s) => document.querySelector(s);
let token = sessionStorage.getItem('mme_token');
let me = null;
let stopLive = null;
const ROLE = { admin: ['telephony:admin', 'platform:admin'], operator: ['telephony:user'] };

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const pill = (v) => h('span', { class: 'pill ' + String(v || '').replace(/[^a-z_]/g, '') }, v ?? '—');
const t = (iso) => (iso ? new Date(iso).toLocaleString() : '—');
const dur = (s) => (s == null ? '—' : s >= 60 ? Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's' : Math.round(s) + 's');
const isRole = (r) => me && (r === 'viewer' || (r === 'operator' && (me.operator || me.admin)) || (r === 'admin' && me.admin));

async function api(path, opts = {}) {
  const headers = { Accept: 'application/json', ...(opts.headers || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  if (opts.body && !(opts.body instanceof FormData)) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body); }
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 && token) { logout(); throw new Error('Session expired'); }
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) {
    const e = new Error((data && data.error && data.error.message) || r.statusText);
    e.code = data && data.error && data.error.code; e.status = r.status; throw e;
  }
  return data;
}
const safe = (p) => p.catch((e) => ({ __error: e.message || String(e), __status: e.status }));

function logout() { sessionStorage.removeItem('mme_token'); token = null; me = null; if (stopLive) stopLive(); show(); }

async function show() {
  const authed = !!token;
  $('#login').classList.toggle('hidden', authed);
  $('#app').classList.toggle('hidden', !authed);
  if (!authed) return;
  try {
    const m = await api('/v1/auth/me');
    const roles = m.roles;
    me = { sub: m.sub, tenant: m.tenant_id, admin: roles.some((r) => ROLE.admin.includes(r)), operator: roles.some((r) => ROLE.operator.includes(r)) };
    $('#who').textContent = `${me.sub} @ ${me.tenant} (${me.admin ? 'admin' : me.operator ? 'operator' : 'viewer'})`;
    document.querySelectorAll('nav a[data-role]').forEach((a) => a.classList.toggle('hidden', !isRole(a.dataset.role)));
    route();
  } catch (e) { logout(); }
}

$('#login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#login-err').textContent = '';
  try {
    const r = await api('/v1/auth/login', { method: 'POST', body: { username: $('#lu').value, password: $('#lp').value } });
    token = r.access_token; sessionStorage.setItem('mme_token', token); $('#lp').value = ''; show();
  } catch (e) { $('#login-err').textContent = e.message; }
});
$('#logout').addEventListener('click', logout);
window.addEventListener('hashchange', route);

const pages = { overview, live, call: callDetail, sessions, session: sessionDetail, controls, recordings, ai: aiPage, logs, users };
function route() {
  if (!me) return;
  if (stopLive) { stopLive(); stopLive = null; }
  const [, page = 'overview', arg] = location.hash.split('/');
  document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('on', a.getAttribute('href') === '#/' + page));
  const main = $('#main');
  main.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
  (pages[page] || overview)(main, arg && decodeURIComponent(arg)).catch((e) => main.replaceChildren(h('p', { class: 'err' }, e.message)));
}

function table(cols, rows, onRow) {
  const tb = h('tbody');
  rows.forEach((r) => { const tr = h('tr', onRow ? { class: 'click', onclick: () => onRow(r) } : {}, cols.map((c) => h('td', {}, c.f(r)))); tb.append(tr); });
  if (!rows.length) tb.append(h('tr', {}, h('td', { colspan: cols.length, class: 'muted' }, 'No data')));
  return h('table', {}, h('thead', {}, h('tr', {}, cols.map((c) => h('th', {}, c.h)))), tb);
}
const stat = (n, l) => h('div', { class: 'card stat' }, h('div', { class: 'n' }, n ?? '—'), h('div', { class: 'l' }, l));

// ---------------------------------------------------------------- overview
async function overview(main) {
  const o = await api('/v1/dashboard/overview');
  const hc = o.health;
  const comp = (name, c) => h('tr', {}, h('td', {}, name), h('td', {}, pill(c.status)), h('td', { class: 'muted' }, c.detail || ''));
  const fe = hc.freeswitch_events || {}, ai = hc.ai || {};
  main.replaceChildren(
    h('h1', {}, 'Overview'),
    h('p', { class: 'muted' }, `As of ${t(o.as_of)} · last healthy DB ${t(hc.last_ok_at.database)} · last healthy FreeSWITCH ${t(hc.last_ok_at.freeswitch)}`),
    h('div', { class: 'grid' }, stat(o.active_calls, 'Active calls'), stat(o.calls_today, 'Calls today (UTC)'), stat(o.answered_today, 'Answered'),
      stat(o.completed_today, 'Completed'), stat(o.missed_today, 'Missed (busy/no answer)'), stat(o.failed_today, 'Failed'),
      stat(o.avg_connected_seconds == null ? null : dur(o.avg_connected_seconds), 'Avg connected duration'),
      stat(o.active_sessions, 'Active sessions'), stat(o.active_ai_sessions, 'Active AI sessions')),
    h('h2', {}, 'Services'),
    h('table', {}, h('tbody', {},
      comp('API', { status: 'up' }), comp('Database', hc.database),
      comp('FreeSWITCH event listener (ESL)', { ...fe, detail: fe.reconnects != null ? `reconnects ${fe.reconnects}, queue ${fe.queue_depth}, dropped ${fe.dropped}` : '' }),
      comp('FreeSWITCH command channel', hc.freeswitch_commands || { status: 'not_applicable' }),
      comp('AI (global switch)', { status: ai.status, detail: ai.media_bridge ? `media bridge: ${ai.media_bridge.name} — ${ai.media_bridge.ready ? 'ready' : 'NOT ready: ' + ai.media_bridge.reason}` : '' }),
      comp('Background workers', { status: hc.workers.webhook_worker ? 'up' : 'down', detail: `controller loops ${hc.workers.controller_loops}` }))),
    h('h2', {}, 'Recent errors'),
    isRole('admin') ? table([{ h: 'Time', f: (r) => r.ts }, { h: 'Level', f: (r) => pill(r.level) }, { h: 'Source', f: (r) => r.logger }, { h: 'Message', f: (r) => r.msg }], o.recent_errors)
      : h('p', { class: 'muted' }, 'Visible to administrators.'));
}

// ---------------------------------------------------------------- live calls (SSE with polling fallback)
async function live(main) {
  let calls = [], mode = 'connecting';
  const q = h('input', { placeholder: 'Search number / call id', oninput: draw });
  const st = h('select', { onchange: draw }, ['', 'queued', 'initiating', 'ringing', 'answered', 'bridged', 'transferring', 'completed', 'failed', 'busy', 'no_answer', 'cancelled'].map((s) => h('option', { value: s }, s || 'All states')));
  const box = h('div'), badge = h('span', { class: 'pill' });
  main.replaceChildren(h('h1', {}, 'Live calls'), h('div', { class: 'row' }, q, st, badge), box);
  function draw() {
    badge.textContent = mode; badge.className = 'pill ' + (mode === 'live' ? 'up' : 'ringing');
    const s = q.value.trim().toLowerCase();
    const rows = calls.filter((c) => (!st.value || c.state === st.value) && (!s || [c.id, c.from_number, c.to_number].join(' ').toLowerCase().includes(s)));
    box.replaceChildren(table([
      { h: 'Call', f: (c) => h('span', { class: 'mono' }, c.id) }, { h: 'Caller', f: (c) => c.from_number }, { h: 'Destination', f: (c) => c.to_number },
      { h: 'Dir', f: (c) => c.direction }, { h: 'Status', f: (c) => pill(c.state) }, { h: 'Started', f: (c) => t(c.initiated_at || c.created_at) },
      { h: 'Answered', f: (c) => t(c.answered_at) },
      { h: 'Duration', f: (c) => dur(c.connected_duration_s != null ? c.connected_duration_s : (c.answered_at && !c.ended_at ? (Date.now() - new Date(c.answered_at)) / 1000 : null)) },
      { h: 'Session', f: (c) => c.session_id ? h('a', { href: '#/session/' + c.session_id }, c.session_id.slice(0, 8)) : '—' },
      { h: 'AI', f: (c) => c.ai_enabled ? 'requested' : 'off' }, { h: 'Hangup cause', f: (c) => c.hangup_cause || '—' },
    ], rows, (c) => { location.hash = '#/call/' + c.id; }));
  }
  const ctl = new AbortController(); let poll = null, tick = setInterval(draw, 1000);
  stopLive = () => { ctl.abort(); clearInterval(poll); clearInterval(tick); };
  const startPolling = () => { mode = 'polling'; const go = async () => { try { calls = (await api('/v1/calls?limit=100')).items; draw(); } catch (e) { mode = 'error: ' + e.message; draw(); } }; go(); poll = setInterval(go, 3000); };
  try {
    const r = await fetch('/v1/stream/events', { headers: { Authorization: 'Bearer ' + token }, signal: ctl.signal });
    if (!r.ok || !r.body) throw new Error('stream unavailable');
    mode = 'live'; draw();
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    (async () => {
      try {
        for (;;) {
          const { value, done } = await rd.read(); if (done) break;
          buf += dec.decode(value, { stream: true });
          let i; while ((i = buf.indexOf('\n\n')) >= 0) {
            const blk = buf.slice(0, i); buf = buf.slice(i + 2);
            if (blk.startsWith('event: calls')) { calls = JSON.parse(blk.split('\ndata: ')[1]); draw(); }
          }
        }
      } catch (e) { /* aborted or dropped */ }
      if (!ctl.signal.aborted) startPolling();
    })();
  } catch (e) { if (!ctl.signal.aborted) startPolling(); }
}

// ---------------------------------------------------------------- call details
async function callDetail(main, id) {
  const [c, ev, rec] = await Promise.all([api('/v1/calls/' + id), safe(api(`/v1/calls/${id}/events`)), safe(api(`/v1/calls/${id}/recording`))]);
  const sess = c.session_id ? await safe(api('/v1/sessions/' + c.session_id)) : null;
  const aiS = c.session_id ? await safe(api('/v1/ai/sessions/' + c.session_id)) : null;
  const kv = (o) => h('div', { class: 'kv' }, Object.entries(o).flatMap(([k, v]) => [h('div', {}, k), h('div', {}, v == null || v === '' ? '—' : v)]));
  const actions = h('div', { class: 'row' });
  const live = !['completed', 'failed', 'cancelled', 'busy', 'no_answer'].includes(c.state);
  if (isRole('operator') && live) {
    actions.append(h('button', { class: 'danger', onclick: async () => { if (confirm('Hang up this call?')) { await doAct(() => api(`/v1/calls/${id}/hangup`, { method: 'POST' })); } } }, 'Hang up'));
    if (['answered', 'bridged'].includes(c.state)) actions.append(h('button', { class: 'ghost', onclick: async () => { const d = prompt('Transfer to (E.164, must be an allowed destination):'); if (d && confirm('Transfer call to ' + d + '?')) await doAct(() => api(`/v1/calls/${id}/transfer`, { method: 'POST', body: { destination: d } })); } }, 'Transfer'));
  }
  if (isRole('operator') && c.session_id && ['answered', 'bridged'].includes(c.state)) {
    actions.append(h('button', { class: 'ghost', onclick: () => doAct(() => api(`/v1/ai/sessions/${c.session_id}/start`, { method: 'POST', body: {} })) }, 'Start AI'),
      h('button', { class: 'ghost', onclick: () => doAct(() => api(`/v1/ai/sessions/${c.session_id}/stop`, { method: 'POST' })) }, 'Stop AI'));
  }
  const msg = h('p', { class: 'err' });
  async function doAct(fn) { msg.textContent = ''; try { await fn(); route(); } catch (e) { msg.textContent = e.message; } }
  main.replaceChildren(h('h1', {}, 'Call ' + c.id), actions, msg,
    h('div', { class: 'card' }, kv({ State: pill(c.state), Direction: c.direction, Caller: c.from_number, Destination: c.to_number, Provider: c.provider,
      'FreeSWITCH UUID': c.fs_uuid, 'Provider call id': c.provider_call_id, Created: t(c.created_at), Answered: t(c.answered_at), Ended: t(c.ended_at),
      'Ring duration': dur(c.ring_duration_s), 'Connected duration': dur(c.connected_duration_s), 'Hangup cause': c.hangup_cause, 'Failure reason': c.failure_reason,
      Language: c.language, 'AI requested': c.ai_enabled ? 'yes' : 'no', Session: c.session_id ? h('a', { href: '#/session/' + c.session_id }, c.session_id) : '—' })),
    h('h2', {}, 'AI pipeline'),
    aiS && !aiS.__error ? h('div', { class: 'card' }, kv({ State: pill(aiS.state), Language: aiS.language, Turns: aiS.turns, 'Last timings (ms)': JSON.stringify(aiS.timings_ms || {}),
      'Media bridge': `${aiS.media.bridge} — ${aiS.media.bridge_ready ? 'ready' : 'not ready: ' + aiS.media.bridge_reason}`, Error: aiS.error })) : h('p', { class: 'muted' }, aiS ? aiS.__error : 'No session'),
    h('h2', {}, 'Lifecycle timeline'),
    ev.__error ? h('p', { class: 'err' }, ev.__error) : h('div', { class: 'card tl' }, ev.items.length ? ev.items.map((e) => h('div', {}, `${t(e.ts)} · `, h('b', {}, e.type), ` · ${e.source}`, e.payload && Object.keys(e.payload).length ? ' · ' + JSON.stringify(e.payload) : '')) : 'No events'),
    h('h2', {}, 'Recording'), rec.__error ? h('p', { class: 'muted' }, rec.__error) : table([{ h: 'Id', f: (r) => r.id }, { h: 'Status', f: (r) => pill(r.status) }, { h: 'Duration', f: (r) => dur(r.duration_s) }, { h: 'Retention until', f: (r) => t(r.retention_until) },
      { h: '', f: (r) => isRole('operator') && r.status === 'available' ? h('button', { class: 'ghost', onclick: async () => { try { const u = await api(r.download); window.open(u.url, '_blank', 'noopener'); } catch (e) { alert(e.message); } } }, 'Play / download') : '' }], rec.items),
    h('h2', {}, 'Transcript & summary'), h('p', { class: 'muted' }, 'Not available: transcripts and AI summaries are not stored by this release.'));
}

// ---------------------------------------------------------------- sessions
async function sessions(main) {
  const r = await api('/v1/sessions?limit=100');
  const q = h('input', { placeholder: 'Search id / number', onchange: async () => { const x = await api('/v1/sessions?limit=100&q=' + encodeURIComponent(q.value)); box.replaceChildren(grid(x.items)); } });
  const grid = (rows) => table([{ h: 'Session', f: (s) => h('span', { class: 'mono' }, s.session_id) }, { h: 'Call', f: (s) => s.call_id || '—' }, { h: 'Tenant', f: (s) => s.tenant_id },
    { h: 'Caller', f: (s) => s.caller || '—' }, { h: 'Language', f: (s) => s.language }, { h: 'Status', f: (s) => pill(s.status) }, { h: 'Created', f: (s) => t(s.created_at) },
    { h: 'Answered', f: (s) => t(s.answered_at) }, { h: 'Ended', f: (s) => t(s.ended_at) }, { h: 'End reason', f: (s) => s.end_reason || '—' }], rows, (s) => { location.hash = '#/session/' + s.session_id; });
  const box = h('div', {}, grid(r.items));
  main.replaceChildren(h('h1', {}, 'Counselling sessions'), h('div', { class: 'row' }, q), box);
}
async function sessionDetail(main, id) {
  const s = await api('/v1/sessions/' + id), a = await safe(api('/v1/ai/sessions/' + id));
  main.replaceChildren(h('h1', {}, 'Session ' + id), h('div', { class: 'card kv' }, Object.entries({ Status: pill(s.status), Call: s.call_id ? h('a', { href: '#/call/' + s.call_id }, s.call_id) : '—', Tenant: s.tenant_id,
    Caller: s.caller, Language: s.language, Created: t(s.created_at), Answered: t(s.answered_at), Ended: t(s.ended_at), 'End reason': s.end_reason, 'AI state': a.__error ? a.__error : a.state }).flatMap(([k, v]) => [h('div', {}, k), h('div', {}, v == null || v === '' ? '—' : v)])),
    h('h2', {}, 'History'), h('div', { class: 'card tl' }, s.history.map((x) => h('div', {}, `${t(x.ts)} · ${x.state} · ${x.source}`))));
}

// ---------------------------------------------------------------- call controls
async function controls(main) {
  if (!isRole('operator')) return main.replaceChildren(h('p', { class: 'err' }, 'Operator role required.'));
  const f = { to: h('input', { placeholder: '+91…', required: true }), from: h('input', { placeholder: 'approved caller id', required: true }),
    lang: h('select', {}, ['auto', 'hi', 'en', 'hinglish'].map((x) => h('option', { value: x }, x))), ai: h('input', { type: 'checkbox' }), purpose: h('input', { placeholder: 'purpose (metadata)' }) };
  const out = h('p'), key = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
  const form = h('form', { class: 'card', onsubmit: async (ev) => {
    ev.preventDefault(); out.className = ''; out.textContent = '';
    if (!confirm(`Place an outbound call to ${f.to.value}? This dials a real number if real calls are enabled.`)) return;
    try {
      const body = { to: f.to.value, from: f.from.value, ai_enabled: f.ai.checked, language: f.lang.value, metadata: f.purpose.value ? { purpose: f.purpose.value } : undefined };
      const c = await api('/v1/calls', { method: 'POST', headers: { 'Idempotency-Key': key() }, body });
      out.replaceChildren('Accepted: ', h('a', { href: '#/call/' + c.id }, c.id), ' (state ' + c.state + ')');
    } catch (e) { out.className = 'err'; out.textContent = `${e.code || e.status}: ${e.message}`; }
  } }, h('div', { class: 'row' }, h('label', {}, 'To', f.to), h('label', {}, 'From (caller ID)', f.from), h('label', {}, 'Language', f.lang), h('label', {}, 'Request AI', f.ai), h('label', {}, 'Purpose', f.purpose)),
    h('button', { type: 'submit' }, 'Place call'), out);
  main.replaceChildren(h('h1', {}, 'Call controls'), h('h2', {}, 'Outbound call'), form,
    h('h2', {}, 'Inbound calls'), h('p', { class: 'muted' }, 'Inbound calls are answered by the routing policy configured for the number (park / bridge / hangup); they cannot be answered manually from this console. Hang up, transfer and AI start/stop are on each call\'s detail page.'));
}

// ---------------------------------------------------------------- recordings
async function recordings(main) {
  const r = await api('/v1/recordings?limit=100');
  main.replaceChildren(h('h1', {}, 'Recordings'), h('p', { class: 'muted' }, 'Recording runs only when enabled globally, by tenant policy, with a consent mode and storage configured. Transcripts are not stored by this release.'),
    table([{ h: 'Recording', f: (x) => h('span', { class: 'mono' }, x.id) }, { h: 'Call', f: (x) => h('a', { href: '#/call/' + x.call_id }, x.call_id) }, { h: 'Status', f: (x) => pill(x.status) },
      { h: 'Duration', f: (x) => dur(x.duration_s) }, { h: 'Created', f: (x) => t(x.created_at) }, { h: 'Retention until', f: (x) => t(x.retention_until) }, { h: 'Consent', f: (x) => x.consent_mode || '—' }], r.items));
}

// ---------------------------------------------------------------- AI configuration
async function aiPage(main) {
  const [cfg, hl] = await Promise.all([api('/v1/ai/config'), api('/v1/ai/health')]);
  const admin = isRole('admin');
  const inp = (k, ph) => h('input', { value: cfg[k] || '', placeholder: ph, disabled: !admin, class: 'wide' });
  const f = { stt: inp('stt_url', 'http://127.0.0.1:PORT'), brain: inp('brain_url', ''), tts: inp('tts_url', ''), rag: inp('rag_url', ''),
    en: h('input', { type: 'checkbox', disabled: !admin }), lang: h('select', { disabled: !admin }, ['auto', 'hi', 'en', 'hinglish'].map((x) => h('option', { value: x }, x))) };
  f.en.checked = !!cfg.enabled; f.lang.value = cfg.default_language;
  const msg = h('p');
  const save = async () => { msg.className = ''; try { await api('/v1/ai/config', { method: 'PUT', body: { stt_url: f.stt.value, brain_url: f.brain.value, tts_url: f.tts.value, rag_url: f.rag.value, enabled: f.en.checked, default_language: f.lang.value } }); msg.textContent = 'Saved (audited).'; route(); } catch (e) { msg.className = 'err'; msg.textContent = `${e.code || ''} ${e.message}`; } };
  const mb = hl.media_bridge;
  main.replaceChildren(h('h1', {}, 'AI configuration'),
    !mb.ready ? h('div', { class: 'banner' }, 'Media bridge not ready: ' + mb.reason + '. AI sessions cannot start.') : null,
    h('p', { class: 'muted' }, `Global AI switch: ${hl.ai_enabled_global ? 'on' : 'off'} · tenant: ${hl.ai_enabled_tenant ? 'enabled' : 'disabled'} · checked ${t(hl.checked_at)}`),
    table([{ h: 'Service', f: (r) => r[0] }, { h: 'Model', f: (r) => r[1] }, { h: 'URL', f: (r) => r[2] }, { h: 'Health', f: (r) => pill(r[3].status) }, { h: 'Latency', f: (r) => (r[3].latency_ms != null ? r[3].latency_ms + ' ms' : '—') }],
      [['STT', cfg.models.stt, cfg.stt_url, hl.services.stt], ['Brain', cfg.models.brain, cfg.brain_url, hl.services.brain], ['TTS', cfg.models.tts, cfg.tts_url, hl.services.tts], ['RAG', '—', cfg.rag_url, hl.services.rag]]),
    admin ? h('div', { class: 'card' }, h('h2', {}, 'Edit'), h('div', { class: 'row' }, h('label', {}, 'STT URL', f.stt), h('label', {}, 'Brain URL', f.brain), h('label', {}, 'TTS URL', f.tts), h('label', {}, 'RAG URL', f.rag),
      h('label', {}, 'Default language', f.lang), h('label', {}, 'Enable AI for tenant', f.en)), h('p', { class: 'muted' }, 'URLs must use hosts listed in AI_ALLOWED_HOSTS. API keys are server-side environment settings and are never shown here.'), h('button', { onclick: save }, 'Save'), msg) : h('p', { class: 'muted' }, 'Only administrators can change AI settings.'));
}

// ---------------------------------------------------------------- logs & system
async function logs(main) {
  const [sys, qs] = await Promise.all([api('/v1/system/status'), api('/v1/system/queues')]);
  const lv = h('select', { onchange: reload }, ['', 'ERROR', 'WARNING'].map((x) => h('option', { value: x }, x || 'All levels'))), qq = h('input', { placeholder: 'filter text', onchange: reload }), box = h('div');
  async function reload() { if (!isRole('admin')) return box.replaceChildren(h('p', { class: 'muted' }, 'Application logs are visible to administrators.')); const r = await api(`/v1/logs/errors?limit=200&level=${lv.value}&q=${encodeURIComponent(qq.value)}`); box.replaceChildren(table([{ h: 'Time', f: (x) => x.ts }, { h: 'Level', f: (x) => pill(x.level) }, { h: 'Source', f: (x) => x.logger }, { h: 'Message', f: (x) => x.msg }, { h: 'Request', f: (x) => x.request_id || '—' }], r.items)); }
  main.replaceChildren(h('h1', {}, 'Logs & system'),
    h('div', { class: 'card kv' }, Object.entries({ Service: sys.service, Version: sys.version, Environment: sys.env, Provider: sys.provider, 'Real calls enabled': String(sys.real_calls_enabled), 'Recording enabled': String(sys.recording_enabled),
      'Max active / queued': `${sys.limits.max_active_global} / ${sys.limits.max_queued}`, 'Provider limits': `${sys.limits.provider_max_concurrent} concurrent, ${sys.limits.provider_max_cps} cps` }).flatMap(([k, v]) => [h('div', {}, k), h('div', {}, v)])),
    h('h2', {}, 'Retry & queue status'), h('div', { class: 'card kv' }, Object.entries({ 'Queued calls': qs.queued_calls, 'Active calls': qs.active_calls, 'Uncertain originations': qs.uncertain_originations, 'ESL event queue': qs.esl_event_queue, 'Webhook deliveries': JSON.stringify(qs.webhook_deliveries) }).flatMap(([k, v]) => [h('div', {}, k), h('div', {}, v ?? '—')])),
    h('h2', {}, 'Recent warnings & errors (this process, redacted)'), h('div', { class: 'row' }, lv, qq), box);
  reload();
}

// ---------------------------------------------------------------- users (admin)
async function users(main) {
  if (!isRole('admin')) return main.replaceChildren(h('p', { class: 'err' }, 'Administrator role required.'));
  const r = await api('/v1/admin/users');
  const f = { u: h('input', { placeholder: 'username' }), p: h('input', { type: 'password', placeholder: '≥12 chars' }), r: h('select', {}, ['viewer', 'operator', 'admin'].map((x) => h('option', { value: x }, x))) }, msg = h('p');
  main.replaceChildren(h('h1', {}, 'Users'), table([{ h: 'User', f: (x) => x.username }, { h: 'Role', f: (x) => x.role }, { h: 'Disabled', f: (x) => String(x.disabled) }, { h: 'Last login', f: (x) => t(x.last_login_at) },
    { h: '', f: (x) => h('button', { class: 'ghost', onclick: async () => { if (confirm('Disable ' + x.username + '?')) { await api(`/v1/admin/users/${encodeURIComponent(x.username)}/disable`, { method: 'POST' }); route(); } } }, 'Disable') }], r.items),
    h('h2', {}, 'Add user'), h('div', { class: 'row' }, f.u, f.p, f.r, h('button', { onclick: async () => { try { await api('/v1/admin/users', { method: 'POST', body: { username: f.u.value, password: f.p.value, role: f.r.value } }); route(); } catch (e) { msg.className = 'err'; msg.textContent = e.message; } } }, 'Create')), msg);
}

show();
