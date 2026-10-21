'use strict';
let token = '', actor = null, providers = [], configuration = null, latest = null, discovered = [], discoveryNext = null;
const el = id => document.getElementById(id);
const admin = () => actor?.role === 'admin';
const message = text => { el('message').textContent = text; };
const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
function node(tag, text = '', cls) {
  const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n;
}
function button(text, action, cls = 'secondary') {
  const b = node('button', text, cls); b.type = 'button';
  b.addEventListener('click', () => Promise.resolve().then(action).catch(showError)); return b;
}
function showError(error) { message(error.message); if (error.status === 401) clearSession(); }
async function request(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) { const err = new Error(result.error || 'Anfrage fehlgeschlagen'); err.status = response.status; throw err; }
  return result;
}
function field(parent, label, value, cls, attributes = {}) {
  const l = node('label', label), input = document.createElement('input');
  Object.assign(input, attributes); input.className = cls;
  if (input.type === 'checkbox') { input.checked = !!value; l.className = 'check'; } else input.value = value ?? '';
  l.append(input); parent.append(l); return input;
}
function select(parent, label, values, value, cls) {
  const l = node('label', label), input = document.createElement('select'); input.className = cls;
  for (const [v, text] of values) { const option = node('option', text); option.value = v; input.append(option); }
  input.value = value; l.append(input); parent.append(l); return input;
}
function providerOptions(optional = true) {
  return [...(optional ? [['', 'Ohne Provider / SSH']] : []), ...providers.map(p => [p.id, p.name])];
}
function navigate(page) {
  document.querySelectorAll('.page').forEach(p => { p.hidden = p.id !== 'page-' + page; });
  document.querySelectorAll('[data-page]').forEach(b => b.classList.toggle('selected', b.dataset.page === page));
}
function applyRole() {
  document.querySelectorAll('[data-admin]').forEach(n => { n.hidden = !admin(); });
  el('identity').textContent = actor.name + ' · ' + (admin() ? 'Administrator' : 'Nur lesen');
}
function resetProviderForm() {
  el('provider-form').reset();
  // Hidden input values also change their default value; reset() alone retains the edit ID.
  el('provider-id').value = '';
  el('provider-form-title').textContent = 'Provider anlegen';
  el('provider-save').textContent = 'Provider hinzufügen';
}
function clearSession() {
  token = ''; actor = null; providers = []; configuration = null; latest = null; discovered = []; discoveryNext = null;
  for (const id of ['user-form', 'login', 'bootstrap']) el(id).reset();
  resetProviderForm();
  el('workspace').hidden = true; el('auth').hidden = false; el('logout').hidden = true; el('identity').textContent = '';
  for (const id of ['editors', 'repos', 'recent', 'jobs', 'provider-list', 'user-list', 'discovery-results']) el(id).replaceChildren();
}
async function connect(value) {
  token = value;
  actor = await request('/api/me');
  await loadProviders(); await loadSettings();
  if (admin()) await loadUsers();
  el('workspace').hidden = false; el('auth').hidden = true; el('logout').hidden = false;
  applyRole(); navigate('dashboard'); await refresh();
  message('Verbunden. ' + (actor.name === 'bootstrap' ? 'Unter Benutzer einen persönlichen Administrator anlegen.' : 'Willkommen, ' + actor.name + '.'));
}
function remoteEditor(parent, name = '', url = '', provider = '') {
  const row = node('div', '', 'remote-row');
  const nameField = field(row, 'Remote-Name', name, 'remote-name', {required: true, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,39}'});
  field(row, 'Git-URL (HTTPS/SSH)', url, 'remote-url', {required: true, type: 'url'});
  select(row, 'Zugangsdaten von Provider', providerOptions(), provider, 'remote-provider');
  row.append(button('Remote entfernen', () => { const card = row.closest('.repo-editor'); row.remove(); updateSource(card); }));
  nameField.addEventListener('input', () => updateSource(row.closest('.repo-editor')));
  parent.append(row);
}
function updateSource(card) {
  const source = card.querySelector('.repo-source'), previous = source.value;
  source.replaceChildren();
  card.querySelectorAll('.remote-name').forEach(n => { const o = node('option', n.value); o.value = n.value; source.append(o); });
  if ([...source.options].some(o => o.value === previous)) source.value = previous;
}
function repoEditor(repo = {name: '', enabled: false, remotes: {source: '', target: ''}}) {
  const card = node('fieldset', '', 'repo-editor');
  card.append(node('legend', repo.name || 'Neues Profil'));
  const grid = node('div', '', 'grid');
  field(grid, 'Profilname', repo.name, 'repo-name', {required: true, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,79}'});
  field(grid, 'Aktiv', repo.enabled !== false, 'repo-enabled', {type: 'checkbox'});
  const direction = select(grid, 'Richtung', [['bidirectional', 'Bidirektional'], ['one-way', 'Einweg: Quelle → Ziele']], repo.direction || 'bidirectional', 'repo-direction');
  const source = select(grid, 'Quell-/Start-Remote', [], '', 'repo-source');
  const policy = select(grid, 'Konfliktregel', [['ff-only', 'Fast-forward; Konflikte überspringen'], ['reject', 'Bei Konflikt gesamtes Profil überspringen'], ['force-with-lease', 'Erzwungen mit Zielstand-Prüfung (Einweg)']], repo.policy || 'ff-only', 'repo-policy');
  field(grid, 'Branch-Filter (durch Komma getrennt; leer = alle)', (repo.branches || []).join(', '), 'repo-branches', {placeholder: 'main, release/*'});
  field(grid, 'Tags synchronisieren', repo.tags !== false, 'repo-tags', {type: 'checkbox'});
  field(grid, 'Intervall in Sekunden (leer = Standard; 0 = nur manuell)', repo.interval ?? '', 'repo-interval', {type: 'number', min: 0, max: 86400});
  field(grid, 'Webhook-Secret: Umgebungsvariable', repo.webhook_secret_env || 'GITMESH_WEBHOOK_SECRET', 'repo-secret');
  const create = field(grid, 'Fehlende Ziel-Repositories automatisch anlegen', !!repo.create_missing, 'repo-create', {type: 'checkbox'});
  const createPrivate = field(grid, 'Neue Ziele privat anlegen (deaktiviert = öffentlich)', repo.create_private !== false, 'repo-create-private', {type: 'checkbox'});
  const force = field(grid, 'Ich bestätige: Zielhistorie darf im Einwegmodus überschrieben werden.', repo.force_confirmed, 'repo-force', {type: 'checkbox'});
  const updatePolicy = () => { source.disabled = direction.value !== 'one-way' && !create.checked; createPrivate.parentElement.hidden = !create.checked; force.parentElement.hidden = policy.value !== 'force-with-lease'; force.required = policy.value === 'force-with-lease'; };
  create.addEventListener('change', updatePolicy);
  policy.addEventListener('change', updatePolicy); direction.addEventListener('change', updatePolicy); updatePolicy();
  card.append(grid);
  const remotes = node('div', '', 'remote-list'); card.append(remotes);
  for (const [name, url] of Object.entries(repo.remotes)) remoteEditor(remotes, name, url, repo.remote_providers?.[name] || '');
  updateSource(card); source.value = repo.source || Object.keys(repo.remotes)[0];
  card.append(button('Remote hinzufügen', () => { remoteEditor(remotes); updateSource(card); }), button('Profil entfernen', () => card.remove(), 'danger'));
  el('editors').append(card);
}
async function loadSettings() {
  configuration = await request('/api/settings');
  el('interval').value = configuration.interval || 300;
  const w = configuration.worker || {};
  el('parallelism').value = w.parallelism ?? 2; el('min-free').value = w.min_free_mb ?? 128;
  el('min-memory').value = w.min_memory_mb ?? 0; el('max-load').value = w.max_load ?? 0;
  el('editors').replaceChildren(); configuration.repositories.forEach(repoEditor);
}
function collectSettings() {
  return {interval: Number(el('interval').value), worker: configuration.worker || {parallelism: 2, min_free_mb: 128, min_memory_mb: 0, max_load: 0}, repositories: [...el('editors').children].map(card => {
    const remotes = Object.create(null), remote_providers = Object.create(null);
    for (const row of card.querySelectorAll('.remote-row')) {
      const name = row.querySelector('.remote-name').value.trim();
      if (Object.hasOwn(remotes, name)) throw new Error('Remote-Namen müssen pro Profil eindeutig sein.');
      remotes[name] = row.querySelector('.remote-url').value.trim();
      const provider = row.querySelector('.remote-provider').value; if (provider) remote_providers[name] = provider;
    }
    const repo = {name: card.querySelector('.repo-name').value.trim(), enabled: card.querySelector('.repo-enabled').checked,
      direction: card.querySelector('.repo-direction').value, source: card.querySelector('.repo-source').value,
      create_missing: card.querySelector('.repo-create').checked, create_private: card.querySelector('.repo-create-private').checked,
      policy: card.querySelector('.repo-policy').value, force_confirmed: card.querySelector('.repo-force').checked,
      tags: card.querySelector('.repo-tags').checked, branches: card.querySelector('.repo-branches').value.split(',').map(x => x.trim()).filter(Boolean),
      webhook_secret_env: card.querySelector('.repo-secret').value.trim(), remotes, remote_providers};
    const interval = card.querySelector('.repo-interval').value;
    if (interval !== '') repo.interval = Number(interval);
    return repo;
  })};
}
async function saveSettings() {
  const config = collectSettings();
  await request('/api/settings', 'POST', config); configuration = config;
  message('Einstellungen gespeichert.'); await refresh();
}
async function loadProviders() {
  providers = (await request('/api/providers')).providers;
  document.querySelectorAll('.provider-select').forEach(s => {
    const previous = s.value; s.replaceChildren();
    for (const p of providers) { const o = node('option', p.name); o.value = p.id; s.append(o); }
    if (providers.some(p => p.id === previous)) s.value = previous;
  });
  document.querySelectorAll('.remote-provider').forEach(s => {
    const previous = s.value; s.replaceChildren();
    providerOptions().forEach(([v, text]) => { const o = node('option', text); o.value = v; s.append(o); }); s.value = previous;
  });
  el('provider-list').replaceChildren(...providers.map(p => {
    const card = node('article', '', 'card ' + p.health);
    card.append(node('h2', p.name), node('p', p.kind + ' · ' + p.url), node('small', 'Verbindung: ' + p.health + ' · ' + date(p.checked)), node('p', p.has_token ? 'Token gespeichert' : 'Kein Token'));
    if (admin()) card.append(button('Bearbeiten', () => {
      el('provider-form-title').textContent = 'Provider bearbeiten: ' + p.name;
      el('provider-save').textContent = 'Änderungen speichern';
      el('provider-id').value = p.id; el('provider-name').value = p.name; el('provider-kind').value = p.kind;
      el('provider-url').value = p.url; el('provider-username').value = p.username; el('provider-token').value = ''; el('provider-clear').checked = false;
      el('provider-form').scrollIntoView({behavior: 'smooth'});
    }), button('Verbindung testen', async () => { try { const r = await request('/api/providers/test', 'POST', {id: p.id}); message('Verbunden als ' + r.account); } finally { await loadProviders(); } }),
    button('Entfernen', async () => { await request('/api/providers/delete', 'POST', {id: p.id}); await loadProviders(); message('Provider entfernt.'); }, 'danger'));
    return card;
  }));
}
async function loadUsers() {
  const users = (await request('/api/users')).users;
  el('user-list').replaceChildren(...users.map(u => {
    const card = node('article', '', 'card'); card.append(node('h2', u.name), node('p', u.role + ' · ' + (u.enabled ? 'Aktiv' : 'Deaktiviert')),
      button('Bearbeiten', () => { el('user-name').value = u.name; el('user-role').value = u.role; el('user-enabled').checked = u.enabled; el('user-password').value = ''; }));
    return card;
  }));
}
function profileCard(repo) {
  const job = latest.jobs.find(j => j.profile === repo.name);
  const card = node('article', '', 'card ' + (job?.status || ''));
  card.append(node('h2', repo.name), node('p', repo.remotes.join(repo.direction === 'one-way' ? ' → ' : ' ↔ ')),
    node('small', (repo.enabled ? 'Aktiv' : 'Deaktiviert') + ' · Nächster Lauf: ' + date(latest.schedules[repo.name])),
    node('p', job ? job.status + ' · ' + date(job.requested) : 'Noch kein Job'));
  const mapping = node('details'); mapping.append(node('summary', 'Repository-Zuordnung & Regeln'));
  for (const [name, url] of Object.entries(repo.remote_urls || {})) mapping.append(node('p', name + ': ' + url));
  mapping.append(node('p', 'Konfliktregel: ' + repo.policy + ' | Branches: ' + (repo.branches?.join(', ') || 'alle') + ' | Intervall: ' + repo.interval + ' s'));
  card.append(mapping);
  if (admin()) {
    const sync = button('Jetzt synchronisieren', async () => {
      await request('/api/sync', 'POST', {profile: repo.name}); message('Job angefordert: ' + repo.name); await refresh();
    });
    sync.disabled = !repo.enabled;
    sync.title = repo.enabled ? 'Profil in die Warteschlange aufnehmen' : 'Profil zuerst aktivieren und speichern';
    card.append(sync);
  }
  return card;
}
function jobCard(job) {
  const card = node('article', '', 'card ' + job.status);
  const head = node('div', '', 'heading'); head.append(node('strong', '#' + job.id + ' · ' + job.profile), node('span', job.status, 'badge'));
  const details = node('details'); details.append(node('summary', 'Details / Protokoll'), node('pre', JSON.stringify(job.details, null, 2)));
  card.append(head, node('p', date(job.requested) + ' · ' + job.origin + ' · Versuche: ' + job.attempts), details);
  if (job.status === 'queued' && admin()) card.append(button('Abbrechen', async () => { await request('/api/jobs/cancel', 'POST', {id: job.id}); await refresh(); }, 'danger'));
  return card;
}
function renderJobs() {
  const filter = el('job-filter').value;
  el('jobs').replaceChildren(...latest.jobs.filter(j => !filter || j.status === filter).map(jobCard));
}
async function refresh() {
  if (!token) return;
  try {
    latest = await request('/api/status');
    const stats = [['Profile', latest.repositories.length], ['Aktive Jobs', Object.keys(latest.worker.active).length],
      ['Warteschlange', latest.jobs.filter(j => j.status === 'queued').length], ['Konflikte / Fehler', latest.jobs.filter(j => ['conflict', 'error'].includes(j.status)).length]];
    el('stats').replaceChildren(...stats.map(([label, value]) => { const c = node('article', '', 'stat'); c.append(node('strong', String(value)), node('span', label)); return c; }));
    el('repos').replaceChildren(...latest.repositories.map(profileCard)); el('profile-summary').replaceChildren(...latest.repositories.map(profileCard));
    el('recent').replaceChildren(...latest.jobs.slice(0, 5).map(jobCard)); renderJobs();
    const w = latest.worker, r = w.resources;
    el('worker-status').replaceChildren(node('h2', 'Worker: ' + w.mode), node('p', 'Heartbeat: ' + date(w.heartbeat)),
      node('p', 'Aktive Profile: ' + (Object.values(w.active).join(', ') || 'keine')),
      node('p', 'Freier Speicherplatz: ' + r.disk_free_mb + ' MB · RAM verfügbar: ' + (r.memory_available_mb ?? 'unbekannt') + ' MB · Systemlast: ' + (r.load?.toFixed(2) ?? 'unbekannt')),
      node('p', w.notice || 'Bereit'));
  } catch (error) { showError(error); }
}
async function discover(page = 1) {
  const query = new URLSearchParams({provider: el('discovery-provider').value, owner: el('discovery-owner').value.trim(), scope: el('discovery-scope').value, page});
  const result = await request('/api/discover?' + query);
  if (page === 1) discovered = [];
  discovered.push(...result.repositories.map(r => ({...r, provider: el('discovery-provider').value})));
  discoveryNext = result.next_page; el('discovery-more').hidden = !discoveryNext;
  el('discovery-results').replaceChildren(...discovered.map((r, index) => {
    const row = node('article', '', 'card'); field(row, r.full_name + (r.private ? ' · privat' : ''), false, 'discovered', {type: 'checkbox'});
    row.querySelector('input').dataset.index = index; row.append(node('small', r.clone_url)); return row;
  }));
  message(discovered.length + ' Repositories geladen.');
}
function importDiscovery() {
  const target = providers.find(p => p.id === el('discovery-target').value), namespace = el('discovery-namespace').value.trim();
  if (!target || !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(namespace) || namespace.split('/').some(p => p === '.' || p === '..')) throw new Error('Ziel-Provider und gültigen Namespace angeben.');
  let count = 0;
  for (const checkbox of el('discovery-results').querySelectorAll('input:checked')) {
    const repo = discovered[Number(checkbox.dataset.index)], leaf = repo.full_name.split('/').at(-1);
    const base = ('sync-' + repo.full_name.replace(/[^A-Za-z0-9_-]/g, '-')).slice(0, 70);
    let name = base, suffix = 1;
    const used = [...document.querySelectorAll('.repo-name')].map(x => x.value);
    while (used.includes(name)) name = base + '-' + suffix++;
    const path = [...namespace.split('/'), leaf].map(encodeURIComponent).join('/');
    repoEditor({name, enabled: false, remotes: {source: repo.clone_url, target: target.url + '/' + path + '.git'},
      remote_providers: {source: repo.provider, target: target.id}, direction: 'one-way', source: 'source', policy: 'ff-only'});
    count++;
  }
  if (!count) throw new Error('Mindestens ein Repository auswählen.');
  navigate('profiles'); message(count + ' Profile als Entwurf übernommen. Prüfen, aktivieren und Einstellungen speichern.');
}
document.querySelectorAll('[data-page]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.page)));
el('login').addEventListener('submit', async e => {
  e.preventDefault();
  try { const r = await request('/api/login', 'POST', {username: el('username').value, password: el('password').value}); el('password').value = ''; await connect(r.token); }
  catch (error) { el('password').value = ''; clearSession(); showError(error); }
});
el('bootstrap').addEventListener('submit', async e => {
  e.preventDefault(); const value = el('token').value; el('token').value = '';
  try { await connect(value); } catch (error) { clearSession(); showError(error); }
});
el('logout').addEventListener('click', async () => { try { await request('/api/logout', 'POST'); } finally { clearSession(); message('Abgemeldet.'); } });
el('sync-all').addEventListener('click', () => request('/api/sync', 'POST').then(() => { message('Jobs angefordert.'); return refresh(); }).catch(showError));
el('add-repo').addEventListener('click', () => repoEditor());
el('reload').addEventListener('click', () => loadSettings().then(() => message('Gespeicherten Stand geladen.')).catch(showError));
el('settings-form').addEventListener('submit', e => { e.preventDefault(); saveSettings().catch(showError); });
el('worker-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    const saved = await request('/api/settings');
    saved.worker = {parallelism: Number(el('parallelism').value), min_free_mb: Number(el('min-free').value),
      min_memory_mb: Number(el('min-memory').value), max_load: Number(el('max-load').value)};
    await request('/api/settings', 'POST', saved);
    configuration.worker = saved.worker;
    message('Worker-Einstellungen gespeichert.'); await refresh();
  } catch (error) { showError(error); }
});
el('provider-new').addEventListener('click', resetProviderForm);
el('provider-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await request('/api/providers', 'POST', {id: el('provider-id').value || undefined, name: el('provider-name').value.trim(), kind: el('provider-kind').value,
      url: el('provider-url').value.trim(), username: el('provider-username').value.trim(), token: el('provider-token').value, clear_token: el('provider-clear').checked});
    resetProviderForm(); await loadProviders(); message('Provider gespeichert.');
  } catch (error) { el('provider-token').value = ''; showError(error); }
});
el('user-form').addEventListener('submit', async e => {
  e.preventDefault(); const name = el('user-name').value;
  try {
    await request('/api/users', 'POST', {name, role: el('user-role').value, password: el('user-password').value, enabled: el('user-enabled').checked});
    el('user-password').value = '';
    if (actor.name === name) { clearSession(); message('Konto aktualisiert. Bitte erneut anmelden.'); }
    else { el('user-form').reset(); await loadUsers(); message('Benutzer gespeichert.'); }
  } catch (error) { el('user-password').value = ''; showError(error); }
});
document.querySelectorAll('[data-worker]').forEach(b => b.addEventListener('click', () => request('/api/worker', 'POST', {action: b.dataset.worker}).then(refresh).catch(showError)));
el('job-filter').addEventListener('change', renderJobs);
el('discovery-form').addEventListener('submit', e => { e.preventDefault(); discover().catch(showError); });
el('discovery-more').addEventListener('click', () => discover(discoveryNext).catch(showError));
el('discovery-import').addEventListener('click', () => { try { importDiscovery(); } catch (error) { showError(error); } });
setInterval(refresh, 5000);
