'use strict';
let conflictTableRows = [];
let token = '', actor = null, providers = [], configuration = null, latest = null, discovered = [], discoveryNext = null;
const openedJobDetails = new Set(), openedMappings = new Set(), openedConflictValues = new Set();
let activeSave = null, saveNoticeTimer = null, authorshipLoadSequence = 0;
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
  if (activeSave) {
    activeSave.nextPage = page;
    el('save-progress-text').textContent = 'Wird gespeichert. Seitenwechsel erfolgt nach erfolgreichem Abschluss.';
    document.querySelectorAll('[data-page]').forEach(b => b.classList.toggle('pending-page', b.dataset.page === page));
    return;
  }
  if (page === 'conflicts') { setupConflictForm(); loadConflictCenter().catch(showError); }
  if (page === 'discovery' && discovered.length) renderDiscovery();
  document.querySelectorAll('.page').forEach(p => { p.hidden = p.id !== 'page-' + page; });
  document.querySelectorAll('[data-page]').forEach(b => b.classList.toggle('selected', b.dataset.page === page));
}
function saveForm(id, work) {
  el(id).addEventListener('submit', event => {
    event.preventDefault();
    runSave(el(id), work).catch(showError);
  });
}
async function runSave(form, work) {
  if (activeSave) return activeSave.promise;
  const state = {nextPage: null}, content = document.querySelector('#workspace .content');
  const submit = form?.querySelector('button:not([type]), button[type="submit"]');
  const submitText = submit?.textContent, submitDisabled = submit?.disabled, logoutDisabled = el('logout').disabled;
  clearTimeout(saveNoticeTimer);
  activeSave = state;
  el('save-progress').hidden = false; el('save-progress').dataset.state = 'saving';
  el('save-progress-text').textContent = 'Wird gespeichert und geprueft. Bitte warten ...';
  content.inert = true; content.setAttribute('aria-busy', 'true'); el('logout').disabled = true;
  if (submit) { submit.disabled = true; submit.textContent = 'Wird gespeichert ...'; }
  let success = false;
  state.promise = Promise.resolve().then(work);
  try {
    const value = await state.promise;
    success = true;
    return value;
  } finally {
    activeSave = null;
    content.inert = false; content.removeAttribute('aria-busy'); el('logout').disabled = logoutDisabled;
    if (submit?.isConnected) { submit.disabled = submitDisabled; if (submit.textContent === 'Wird gespeichert ...') submit.textContent = submitText; }
    document.querySelectorAll('[data-page]').forEach(b => b.classList.remove('pending-page'));
    el('save-progress').dataset.state = success ? 'saved' : 'error';
    el('save-progress-text').textContent = success ? 'Gespeichert.' : 'Speichern fehlgeschlagen. Bitte die Meldung pruefen und erneut speichern.';
    if (!token) el('save-progress').hidden = true;
    else if (success) {
      saveNoticeTimer = setTimeout(() => { if (!activeSave) el('save-progress').hidden = true; }, 5000);
      if (state.nextPage) navigate(state.nextPage);
    }
  }
}
window.addEventListener('beforeunload', event => {
  if (activeSave) { event.preventDefault(); event.returnValue = ''; }
});
function applyRole() {
  el('profile-summary').hidden = admin();
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
  clearTimeout(saveNoticeTimer); el('save-progress').hidden = true;
  openedJobDetails.clear(); openedMappings.clear(); openedConflictValues.clear();
  el('conflict-list').replaceChildren(); el('conflict-other').replaceChildren(); el('conflict-comparison').replaceChildren(); el('conflict-inventory-list').replaceChildren();
  mappingPaused = true; mappingReport = null; mappingPair = null; decisionSelections.clear(); el('problem-table').replaceChildren(); el('mapping-table').replaceChildren(); el('decision-list').replaceChildren(); mappingBatchRemaining = []; mappingBatchReports = []; el('pair-confirm').disabled = true; el('pair-values').textContent = ''; el('mapping-results').replaceChildren(); el('mapping-progress').textContent = '';
  toolComparison = null; el('tool-output').replaceChildren(); el('tool-inventory').replaceChildren(); el('tool-comparison').replaceChildren();
  token = ''; actor = null; providers = []; configuration = null; latest = null; discovered = []; discoveryNext = null;
  for (const id of ['user-form', 'login', 'bootstrap', 'transport-form', 'authorship-form', 'shared-form', 'group-form']) el(id).reset();
  resetProviderForm(); authorshipLoadSequence++; el('authorship-info').textContent = '';
  el('workspace').hidden = true; el('auth').hidden = false; el('logout').hidden = true; el('identity').textContent = '';
  for (const id of ['editors', 'repos', 'recent', 'jobs', 'provider-list', 'user-list', 'discovery-results', 'shared-list', 'group-list']) el(id).replaceChildren();
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
function suggestedRemoteUrl(row, provider) {
  const base = provider.url.replace(/\/+$/, '');
  const candidates = [row, ...[...row.closest('.repo-editor').querySelectorAll('.remote-row')].filter(other => other !== row)];
  for (const other of candidates) {
    const value = other.querySelector('.remote-url').value.trim();
    if (!value) continue;
    try {
      const parsed = new URL(value);
      if (!['https:', 'http:', 'ssh:'].includes(parsed.protocol) || parsed.password) continue;
      let path = parsed.pathname.replace(/\/+$/, '');
      const owner = providers.find(p => {
        try { const base = new URL(p.url); return base.hostname === parsed.hostname && path.startsWith(base.pathname.replace(/\/+$/, '') + '/'); } catch (_) { return false; }
      });
      if (owner) {
        const prefix = new URL(owner.url).pathname.replace(/\/+$/, '');
        if (prefix && path.startsWith(prefix + '/')) path = path.slice(prefix.length);
      }
      const parts = path.replace(/^\/+/, '').replace(/\.git$/, '').split('/');
      if (parts.length < 2 || parts.some(part => !part || part === '.' || part === '..')) continue;
      if (provider.kind !== 'gitlab' && parts.length !== 2) continue;
      return base + '/' + parts.join('/') + '.git';
    } catch (_) { /* An unfinished URL is not a repository-path suggestion. */ }
  }
  return base + '/';
}
function remoteEditor(parent, name = '', url = '', provider = '') {
  const row = node('div', '', 'remote-row');
  const nameField = field(row, 'Remote-Name', name, 'remote-name', {required: true, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,39}'});
  const urlField = field(row, 'Git-URL (HTTPS/SSH)', url, 'remote-url', {required: true, type: 'url'});
  const providerField = select(row, 'Zugangsdaten von Provider', providerOptions(), provider, 'remote-provider');
  const hint = node('small', '', 'remote-url-hint'); hint.hidden = true;
  const suggest = button('URL vom Provider uebernehmen', () => fillUrl(true));
  suggest.classList.add('remote-url-suggest');
  suggest.hidden = !provider;
  let generated = '';
  function fillUrl(force = false) {
    const selected = providers.find(p => p.id === providerField.value);
    suggest.hidden = !selected;
    hint.textContent = ''; hint.hidden = true;
    if (!selected) return;
    if (!force && urlField.value.trim() && urlField.value !== generated) {
      hint.hidden = false;
      hint.textContent = 'Vorhandene URL bleibt erhalten. Bei Bedarf URL vom Provider uebernehmen.';
      return;
    }
    generated = suggestedRemoteUrl(row, selected); urlField.value = generated;
    const incomplete = generated === selected.url.replace(/\/+$/, '') + '/';
    urlField.setCustomValidity(incomplete ? 'Bitte Namespace und Repository zur Provider-Adresse ergaenzen.' : '');
    hint.hidden = false;
    hint.textContent = incomplete ? 'Namespace/Repository.git bitte ergaenzen.' : 'Repository-Pfad vorgeschlagen. Namespace und Repository bitte pruefen.';
    urlField.dispatchEvent(new Event('input', {bubbles: true}));
    if (incomplete) urlField.setCustomValidity('Bitte Namespace und Repository zur Provider-Adresse ergaenzen.');
  }
  urlField.addEventListener('input', () => urlField.setCustomValidity(''));
  providerField.addEventListener('change', () => fillUrl());
  row.append(button('Remote entfernen', () => { const card = row.closest('.repo-editor'); row.remove(); updateSource(card); updateEditorSummary(card); }));
  nameField.addEventListener('input', () => updateSource(row.closest('.repo-editor')));
  row.append(suggest, hint);
  parent.append(row);
}
function updateSource(card) {
  const source = card.querySelector('.repo-source'), previous = source.value;
  source.replaceChildren();
  card.querySelectorAll('.remote-name').forEach(n => { const o = node('option', n.value); o.value = n.value; source.append(o); });
  if ([...source.options].some(o => o.value === previous)) source.value = previous;
}
const syncOptions = {sync_branches: 'Branches', labels: 'Labels', milestones: 'Meilensteine', issues: 'Issues', comments: 'Kommentare zu Issues / PRs', pull_requests: 'Pull Requests / Merge Requests (eingeschraenkt)', wiki: 'Wiki', lfs: 'Git LFS', repository_details: 'Repository-Beschreibung / Website / Themen'};
function repoEditor(repo = {name: '', enabled: false, remotes: {source: '', target: ''}}, {open = true} = {}) {
  repo = {...repo, ...(configuration?.profiles?.[repo.profile] || {}), ...(repo.sync_overrides || {})};
  const card = node('details', '', 'repo-editor'); card.open = open;
  card.dataset.profileName = repo.name;
  card._syncOverrides = {...repo.sync_overrides};
  const summary = node('summary', '', 'repo-editor-summary');
  const heading = node('span', '', 'repo-editor-heading');
  heading.append(node('strong', repo.name || 'Neues Profil', 'repo-editor-title'), node('small', '', 'repo-editor-meta'));
  const toggleLabel = node('span', open ? 'Einklappen' : 'Bearbeiten', 'repo-editor-toggle');
  summary.append(heading, toggleLabel);
  card.addEventListener('toggle', () => { toggleLabel.textContent = card.open ? 'Einklappen' : 'Bearbeiten'; });
  const body = node('div', '', 'repo-editor-body'); card.append(summary, body);
  const grid = node('div', '', 'grid');
  const shared = select(grid, 'Gemeinsames Profil', [['', 'Eigene Regeln'], ...Object.keys(configuration?.profiles || {}).map(name => [name, name])], repo.profile || '', 'repo-shared');
  const workerSet = select(grid, 'Worker-Gruppe', [['default', 'Standard'], ...Object.keys(configuration?.worker_sets || {}).map(name => [name, name])], repo.worker_set || 'default', 'repo-worker-set');
  field(grid, 'Profilname', repo.name, 'repo-name', {required: true, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,79}'});
  field(grid, 'Aktiv', repo.enabled !== false, 'repo-enabled', {type: 'checkbox'});
  const direction = select(grid, 'Richtung', [['bidirectional', 'Bidirektional'], ['one-way', 'Einweg: Quelle → Ziele']], repo.direction || 'bidirectional', 'repo-direction');
  const source = select(grid, 'Quell-/Start-Remote', [], '', 'repo-source');
  const policy = select(grid, 'Konfliktregel', [['ff-only', 'Fast-forward; Konflikte überspringen'], ['reject', 'Bei Konflikt gesamtes Profil überspringen'], ['force-with-lease', 'Erzwungen mit Zielstand-Prüfung (Einweg)']], repo.policy || 'ff-only', 'repo-policy');
  field(grid, 'Branch-Filter (durch Komma getrennt; leer = alle)', (repo.branches || []).join(', '), 'repo-branches', {placeholder: 'main, release/*'});
  const components = node('fieldset', '', 'sync-components'); components.append(node('legend', 'Was synchronisieren?'));
  const choices = node('div', '', 'grid'); components.append(choices);
  for (const [key, label] of Object.entries(syncOptions)) field(choices, label, repo[key] ?? key === 'sync_branches', 'repo-' + key, {type: 'checkbox'});
  components.append(node('p', 'Diese Auswahl gilt pro Sync. Abweichungen vom gemeinsamen Profil werden als eigene Auswahl gespeichert.'));
  components.append(node('p', 'Meilensteine und Kommentare folgen der Richtung; Kommentare zu bereits zugeordneten Issues/PRs. Uebrige Metadaten und Wiki: Quelle → Ziele. Kommentare benoetigen Issues oder PRs. PRs nur mit passenden Zielbranches; keine automatischen Merges. Wiki muss auf den Providern verfuegbar sein.'));
  field(choices, 'Tags synchronisieren', repo.tags !== false, 'repo-tags', {type: 'checkbox'});
  field(grid, 'Intervall in Sekunden (leer = Standard; 0 = nur manuell)', repo.interval ?? '', 'repo-interval', {type: 'number', min: 0, max: 86400});
  field(grid, 'Webhook-Secret: Umgebungsvariable', repo.webhook_secret_env || 'GITMESH_WEBHOOK_SECRET', 'repo-secret');
  field(grid, 'Webhooks beim Sync automatisch registrieren', !!repo.auto_webhooks, 'repo-hooks', {type: 'checkbox'});
  const releaseSync = field(choices, 'Releases von Quelle zu Zielen synchronisieren', !!repo.releases, 'repo-releases', {type: 'checkbox'});
  field(choices, 'Release-Anhaenge mitkopieren', repo.release_assets !== false, 'repo-release-assets', {type: 'checkbox'});
  field(choices, 'Maximale Groesse je Release-Anhang (MB)', repo.release_max_asset_mb ?? 512, 'repo-release-max', {type: 'number', min: 1, max: 4096});
  const create = field(grid, 'Fehlende Ziel-Repositories automatisch anlegen', !!repo.create_missing, 'repo-create', {type: 'checkbox'});
  const createPrivate = field(grid, 'Neue Ziele privat anlegen (deaktiviert = öffentlich)', repo.create_private !== false, 'repo-create-private', {type: 'checkbox'});
  const force = field(grid, 'Ich bestätige: Zielhistorie darf im Einwegmodus überschrieben werden.', repo.force_confirmed, 'repo-force', {type: 'checkbox'});
  shared.addEventListener('change', () => { card._syncOverrides = {}; applySharedRules(card); });
  const updatePolicy = () => { source.disabled = direction.value !== 'one-way' && !create.checked && !releaseSync.checked && ![...card.querySelectorAll('.sync-components input[type=checkbox]')].some(input => input.checked && !['repo-sync_branches', 'repo-tags', 'repo-lfs', 'repo-release-assets'].some(cls => input.classList.contains(cls))); createPrivate.parentElement.hidden = !create.checked; force.parentElement.hidden = policy.value !== 'force-with-lease'; force.required = policy.value === 'force-with-lease'; };
  create.addEventListener('change', updatePolicy); releaseSync.addEventListener('change', updatePolicy);
  policy.addEventListener('change', updatePolicy); direction.addEventListener('change', updatePolicy); updatePolicy();
  body.append(grid, components);
  components.addEventListener('change', updatePolicy); updatePolicy();
  const remotes = node('div', '', 'remote-list'); body.append(remotes);
  for (const [name, url] of Object.entries(repo.remotes)) remoteEditor(remotes, name, url, repo.remote_providers?.[name] || '');
  updateSource(card); source.value = repo.source || Object.keys(repo.remotes)[0];
  const sync = button('Jetzt synchronisieren', async () => {
    await request('/api/sync', 'POST', {profile: repo.name}); message('Job angefordert: ' + repo.name); await refresh();
  });
  sync.disabled = !configuration?.repositories.some(saved => saved.name === repo.name && saved.enabled !== false);
  sync.title = 'Verwendet das gespeicherte Profil. Aenderungen zuerst speichern.';
  body.append(sync);
  body.append(button('Remote hinzufügen', () => { remoteEditor(remotes); updateSource(card); updateEditorSummary(card); }), button('Profil entfernen', () => { card.remove(); message('Ungespeicherte Aenderungen. Bitte Einstellungen speichern.'); }, 'danger'));
  for (const event of ['input', 'change']) body.addEventListener(event, () => updateEditorSummary(card));
  updateEditorSummary(card);
  el('editors').append(card);
}
function updateEditorSummary(card) {
  const name = card.querySelector('.repo-name').value.trim();
  card.dataset.profileName = name;
  card.querySelector('.repo-editor-title').textContent = name || 'Neues Profil';
  const names = [...card.querySelectorAll('.remote-name')].map(input => input.value.trim()).filter(Boolean);
  const arrow = card.querySelector('.repo-direction').value === 'one-way' ? ' \u2192 ' : ' \u2194 ';
  const job = latest?.jobs.find(job => job.profile === name);
  const status = job ? ' \u00b7 ' + (job.reset_at ? 'Zurueckgesetzt (' + job.status + ')' : job.status) : '';
  card.querySelector('.repo-editor-meta').textContent = (card.querySelector('.repo-enabled').checked ? 'Aktiv' : 'Deaktiviert') + ' \u00b7 ' + names.join(arrow) + status;
}
function editProfile(name) {
  navigate('profiles');
  const card = [...el('editors').children].find(editor => editor.dataset.profileName === name);
  if (card) { card.open = true; card.scrollIntoView({behavior: 'smooth', block: 'start'}); }
}
async function loadSettings(saved) {
  configuration = saved || await request('/api/settings');
  el('public-url').value = configuration.public_url || '';
  updateToolProfiles();
  el('interval').value = configuration.interval || 300;
  const w = configuration.worker || {};
  el('parallelism').value = w.parallelism ?? 2; el('min-free').value = w.min_free_mb ?? 128;
  el('min-memory').value = w.min_memory_mb ?? 0; el('max-load').value = w.max_load ?? 0;
  const expanded = new Set([...el('editors').children].filter(card => card.open).map(card => card.querySelector('.repo-name').value.trim()));
  el('editors').replaceChildren(); configuration.repositories.forEach(repo => repoEditor(repo, {open: expanded.has(repo.name)}));
  document.querySelectorAll('.repo-editor').forEach(applySharedRules); renderGroups();
}
function collectSettings() {
  return {profiles: configuration.profiles || {}, worker_sets: configuration.worker_sets || {}, public_url: configuration.public_url || '', interval: Number(el('interval').value), worker: configuration.worker || {parallelism: 2, min_free_mb: 128, min_memory_mb: 0, max_load: 0}, repositories: [...el('editors').children].map(card => {
    const remotes = Object.create(null), remote_providers = Object.create(null);
    for (const row of card.querySelectorAll('.remote-row')) {
      const name = row.querySelector('.remote-name').value.trim();
      if (Object.hasOwn(remotes, name)) throw new Error('Remote-Namen müssen pro Profil eindeutig sein.');
      remotes[name] = row.querySelector('.remote-url').value.trim();
      const provider = row.querySelector('.remote-provider').value; if (provider) remote_providers[name] = provider;
    }
    const repo = {profile: card.querySelector('.repo-shared').value, worker_set: card.querySelector('.repo-worker-set').value, name: card.querySelector('.repo-name').value.trim(), enabled: card.querySelector('.repo-enabled').checked,
      direction: card.querySelector('.repo-direction').value, source: card.querySelector('.repo-source').value,
      releases: card.querySelector('.repo-releases').checked, release_assets: card.querySelector('.repo-release-assets').checked, release_max_asset_mb: Number(card.querySelector('.repo-release-max').value),
      auto_webhooks: card.querySelector('.repo-hooks').checked,
      create_missing: card.querySelector('.repo-create').checked, create_private: card.querySelector('.repo-create-private').checked,
      policy: card.querySelector('.repo-policy').value, force_confirmed: card.querySelector('.repo-force').checked,
      tags: card.querySelector('.repo-tags').checked, branches: card.querySelector('.repo-branches').value.split(',').map(x => x.trim()).filter(Boolean),
      webhook_secret_env: card.querySelector('.repo-secret').value.trim(), remotes, remote_providers};
    for (const key of Object.keys(syncOptions)) repo[key] = card.querySelector('.repo-' + key).checked;
    const interval = card.querySelector('.repo-interval').value;
    if (interval !== '') repo.interval = Number(interval);
    if (!repo.profile) delete repo.profile;
    else {
      const rules = configuration.profiles[repo.profile], overrides = {};
      for (const key of Object.keys(rules)) {
        if (Object.hasOwn(componentFields, key) && repo[key] !== rules[key]) overrides[key] = repo[key];
        delete repo[key];
      }
      if (Object.keys(overrides).length) repo.sync_overrides = overrides;
    }
    return repo;
  })};
}
function settingsSignature(value) {
  return JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
}
async function persistSettings(config) {
  try { await request('/api/settings', 'POST', config); }
  catch (error) { error.message = 'Nicht gespeichert: ' + error.message; throw error; }
  let saved;
  try { saved = await request('/api/settings'); }
  catch (error) { error.message = 'Speicherung konnte nicht durch erneutes Laden bestaetigt werden: ' + error.message; throw error; }
  if (settingsSignature(saved) !== settingsSignature(config)) throw new Error('Speicherung nicht bestaetigt: Der gespeicherte Stand weicht von der Auswahl ab. Bitte erneut laden und pruefen.');
  return saved;
}
async function saveSettings() {
  const saved = await persistSettings(collectSettings());
  await loadSettings(saved);
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
      el('provider-http').checked = !!p.allow_http; el('provider-url').value = p.url; el('provider-username').value = p.username; el('provider-token').value = ''; el('provider-clear').checked = false;
      el('provider-form').scrollIntoView({behavior: 'smooth'});
    }), button('Verbindung testen', async () => { try { const r = await request('/api/providers/test', 'POST', {id: p.id}); message('Verbunden als ' + r.account); } finally { await loadProviders(); } }),
    button('Entfernen', async () => { await request('/api/providers/delete', 'POST', {id: p.id}); await loadProviders(); message('Provider entfernt.'); }, 'danger'));
    return card;
  }));
  if (admin()) await loadAuthorship();
}
function authorshipBotOptions(value = '') {
  const personal = providers.find(p => p.id === el('authorship-provider').value);
  const select = el('authorship-bot'); select.replaceChildren();
  for (const [id, name] of [['', 'Bot-Provider auswaehlen'], ...providers.filter(p => personal && p.id !== personal.id && p.kind === personal.kind && p.url === personal.url).map(p => [p.id, p.name])]) {
    const option = node('option', name); option.value = id; select.append(option);
  }
  select.value = value;
}
async function loadAuthorship() {
  const id = el('authorship-provider').value, sequence = ++authorshipLoadSequence;
  const controls = [el('authorship-enabled'), el('authorship-bot'), el('authorship-load'), el('authorship-form').querySelector('button:not([type])')];
  controls.forEach(control => { control.disabled = true; });
  authorshipBotOptions();
  el('authorship-enabled').checked = false; el('authorship-info').textContent = id ? 'Zuordnung wird geladen ...' : '';
  try {
    if (!id) return;
    const value = await request('/api/authorship?' + new URLSearchParams({id}));
    if (id !== el('authorship-provider').value || sequence !== authorshipLoadSequence) return;
    authorshipBotOptions(value.bot_provider);
    el('authorship-enabled').checked = value.enabled;
    el('authorship-info').textContent = (value.enabled ? 'Aktiv. ' : 'Deaktiviert. ') + 'Persoenliches Konto: ' + (value.personal_account || 'noch nicht geprueft') + ' | Bot: ' + (value.bot_account || 'noch nicht geprueft');
  } catch (error) {
    if (sequence === authorshipLoadSequence) el('authorship-info').textContent = 'Zuordnung konnte nicht geladen werden.';
    throw error;
  } finally {
    if (sequence === authorshipLoadSequence) controls.forEach(control => { control.disabled = false; });
  }
}

async function loadUsers() {
  const users = (await request('/api/users')).users;
  el('user-list').replaceChildren(...users.map(u => {
    const card = node('article', '', 'card'); card.append(node('h2', u.name), node('p', u.role + ' · ' + (u.enabled ? 'Aktiv' : 'Deaktiviert')),
      button('Bearbeiten', () => { el('user-name').value = u.name; el('user-role').value = u.role; el('user-enabled').checked = u.enabled; el('user-password').value = ''; }));
    return card;
  }));
}
function profileCard(repo, previous) {
  const job = latest.jobs.find(j => j.profile === repo.name);
  const diagnostic = latest.jobs.find(j => j.profile === repo.name && !['queued', 'running'].includes(j.status) && !String(j.origin).startsWith('conflicts/resolve:'));
  const signature = JSON.stringify([actor?.role, repo, job, diagnostic, latest.schedules[repo.name], latest.resolved_conflicts]);
  if (previous?._profileSignature === signature) return previous;
  const card = node('article', '', 'card ' + (job?.reset_at ? 'reset' : job?.status || ''));
  card.append(node('h2', repo.name), node('p', repo.remotes.join(repo.direction === 'one-way' ? ' → ' : ' ↔ ')),
    node('small', (repo.enabled ? 'Aktiv' : 'Deaktiviert') + ' · Nächster Lauf: ' + date(latest.schedules[repo.name])),
    node('p', job ? (job.reset_at ? 'Zurueckgesetzt (' + job.status + ')' : job.status) + ' · ' + date(job.requested) : 'Noch kein Job'));
  card.dataset.profile = repo.name; card._profileSignature = signature;
  const mapping = node('details', '', 'profile-mapping');
  mapping.open = previous?.querySelector('.profile-mapping')?.open ?? openedMappings.has(repo.name);
  mapping.addEventListener('toggle', () => { if (mapping.isConnected) { if (mapping.open) openedMappings.add(repo.name); else openedMappings.delete(repo.name); } });
  mapping.append(node('summary', 'Repository-Zuordnung & Regeln'));
  for (const [name, url] of Object.entries(repo.remote_urls || {})) mapping.append(node('p', name + ': ' + url));
  mapping.append(node('p', 'Was synchronisieren: ' + (repo.selected_components || ['sync_branches', 'tags']).map(key => syncOptions[key] || ({tags: 'Tags', releases: 'Releases'})[key]).join(', ')));
  mapping.append(node('p', 'Konfliktregel: ' + repo.policy + ' | Branches: ' + (repo.branches?.join(', ') || 'alle') + ' | Intervall: ' + repo.interval + ' s'));
  card.append(mapping);
  const report = diagnostic && ['conflict', 'error'].includes(diagnostic.status) ? diagnostic : job;
  if (report) { const panel = conflictPanel(report, 'profile:' + repo.name); if (panel) card.append(panel); }
  if (admin()) {
    const sync = button('Jetzt synchronisieren', async () => {
      await request('/api/sync', 'POST', {profile: repo.name}); message('Job angefordert: ' + repo.name); await refresh();
    });
    sync.disabled = !repo.enabled;
    sync.title = repo.enabled ? 'Profil in die Warteschlange aufnehmen' : 'Profil zuerst aktivieren und speichern';
    card.append(sync, button('Bearbeiten', () => editProfile(repo.name)));
    if (latest.jobs.some(j => j.profile === repo.name && !j.reset_at && ['error', 'conflict'].includes(j.status))) card.append(button('Fehler / Konflikte zuruecksetzen', () => manageLogs('reset', {before: latest.snapshot_time, profile: repo.name})));
  }
  return card;
}
function conflictPanel(job, scope) {
  const data = job.details || {}, metadata = data.metadata || (data.items && data.counts ? data : {}), root = node('section', '', 'conflict-panel');
  const git = data.conflicts || [];
  const entries = [...(metadata.items || []).filter(item => item.status === 'conflict'), ...(data.releases?.review_items || []), ...(data.git_reviews || []), ...(data.wiki?.git_reviews || [])];
  const extra = data.releases?.conflicts || (data.wiki?.status === 'conflict' ? 1 : 0);
  const errors = [...(metadata.errors || []), ...(data.default_branch?.errors || []), ...(data.releases?.errors || [])];
  if (!git.length && !entries.length && !metadata.counts?.conflict && !extra && !errors.length && job.status !== 'error') return null;
  const resolvedCount = entries.filter(item => (latest.resolved_conflicts || []).includes(item.resolution_id)).length;
  root.append(node('h3', 'Konflikte & Fehler'), node('p', git.length + ' Git-Konflikte · ' + (metadata.counts?.conflict || entries.length) + ' Metadatenkonflikte' + (resolvedCount ? ' (' + resolvedCount + ' bereits aufgeloest)' : '') + (extra ? ' · weitere Release-/Wiki-Konflikte' : '')));
  for (const ref of git) {
    const row = node('div', '', 'conflict-entry'); row.append(node('strong', 'Git: ' + ref), node('p', 'Die Historien oder Referenzstaende unterscheiden sich. Branches vergleichen und gegebenenfalls zusammenfuehren.'));
    row.append(button('In Repository-Werkzeugen vergleichen', () => {
      const repo = latest.repositories.find(r => r.name === job.profile);
      navigate('tools'); el('tool-profile').value = job.profile;
      el('tool-left').value = repo?.source || repo?.remotes[0] || '';
      el('tool-right').value = String(ref).split(' @ ')[1] || repo?.remotes.find(n => n !== el('tool-left').value) || '';
      el('tool-ref').value = String(ref).split(' @ ')[0];
    })); root.append(row);
  }
  for (const [index, item] of entries.entries()) {
    const row = node('div', '', 'conflict-entry');
    const resolved = (latest.resolved_conflicts || []).includes(item.resolution_id);
    row.append(node('strong', (syncOptions[item.category] || item.category) + ': ' + (item.name || 'Quell-ID ' + item.item)),
      node('p', (item.target ? 'Remote: ' + item.target + '. ' : '') + (item.reason || (item.category === 'labels' ? 'Gleichnamiges vorhandenes Label ist nicht zugeordnet. Neuer Sync zeigt Namen und Vergleichswerte.' : 'Zuordnung oder Inhalte unterscheiden sich. Neuer Sync liefert weitere Details.'))));
    if (item.values) {
      const details = node('details', '', 'conflict-values'), key = scope + ':' + job.id + ':' + (item.resolution_id || index);
      details.append(node('summary', 'Versionen / Werte vergleichen'));
      details.open = openedConflictValues.has(key);
      details.addEventListener('toggle', () => { if (details.isConnected) { if (details.open) openedConflictValues.add(key); else openedConflictValues.delete(key); } });
      const versions = node('div', '', 'conflict-versions');
      for (const [remote, fields] of Object.entries(item.values)) {
        const version = node('div', '', 'conflict-version'); version.append(node('strong', remote));
        for (const [field, value] of Object.entries(fields)) version.append(node('small', ({name: 'Name', title: 'Titel', description: 'Beschreibung', color: 'Farbe', body: 'Kommentar', due: 'Termin', closed: 'Geschlossen', status: 'Status', url: 'Link'})[field] || field), node('pre', String(value ?? '')));
        versions.append(version);
      }
      details.append(versions); row.append(details);
    }
    if (resolved) row.append(node('p', 'Aufgeloest. Dieses Protokoll zeigt den damaligen Konflikt.', 'resolved-conflict'));
    else if (admin() && item.resolution_id) {
      for (const choice of item.choices || []) {
        const label = choice === 'ignore' ? 'Ignorieren und Sync sperren' : ['object_review', 'git_review'].includes(item.kind) ? ({ignore: 'Ignorieren und Sync sperren', resume: 'Sperre aufheben', restore: 'Am Ziel wiederherstellen', apply_source: 'Quellstand am Ziel uebernehmen', delete: 'Zielkopie loeschen', close: 'Am Ziel schliessen', merge: 'Am Ziel zusammenfuehren'})[choice] : item.kind === 'comment_deletion' ? (choice === 'restore' ? 'Fehlende Kopien wiederherstellen' : choice === 'delete_copies' ? 'Verbleibende Kopien loeschen' : 'Kopien behalten und Sync sperren') : item.category === 'labels' ? (choice === 'adopt' ? 'Vorhandenes Label zuordnen' : 'Quellwerte uebernehmen') : 'Version von ' + choice + ' uebernehmen';
        row.append(button(label, () => resolveConflict(job, item, choice)));
      }
      if (item.category === 'labels') row.append(node('small', 'Zuordnen behaelt die aktuellen Zielwerte; spaetere Quellaenderungen werden wieder synchronisiert.'));
    } else if (!resolved) row.append(node('small', 'Auf den Providern vergleichen oder erneut synchronisieren, um aktuelle Loesungsoptionen zu erhalten.'));
    root.append(row);
  }
  for (const error of errors) root.append(node('p', (error.category || 'API') + (error.target || error.remote ? ' / ' + (error.target || error.remote) : '') + ': ' + (error.message || 'Fehler; Rohprotokoll pruefen'), 'conflict-entry'));
  if (extra) root.append(node('p', 'Release-/Wiki-Details stehen im Rohprotokoll. Tags und Zielinhalte auf den Providern pruefen.'));
  if (metadata.truncated) root.append(node('p', 'Die Einzelliste ist gekuerzt. Gesamtzaehler und Rohprotokoll beachten.'));
  if (job.status === 'error' && !errors.length) root.append(node('p', data.message || data.git_error || 'Operation fehlgeschlagen. Details im Rohprotokoll.'));
  if ((metadata.warnings || []).length) root.append(node('p', 'Hinweise: ' + metadata.warnings.join(' · ')));
  if (admin() && entries.some(item => item.resolution_id)) root.append(node('small', 'Tabellarische Entscheidungen koennen auch waehrend eines Scans gespeichert werden; der Worker fuehrt sie nach Freigabe aus. Anzeige zuruecksetzen quittiert nur das alte Protokoll.'));
  if (admin()) root.append(button('Erneut synchronisieren', async () => { await request('/api/sync', 'POST', {profile: job.profile}); message('Neue Pruefung eingereiht.'); await refresh(); }));
  return root;
}
async function resolveConflict(job, item, choice) {
  const text = choice === 'ignore' ? 'Diesen Eintrag dauerhaft vom automatischen Abgleich ausnehmen? Die Sperre kann im Konfliktzentrum aufgehoben werden.' : ['object_review', 'git_review'].includes(item.kind) ? ('Aktion: ' + choice + '. Nur das angezeigte Ziel wird geaendert. Loeschen kann Inhalte und Anhaenge unwiderruflich entfernen. Ignorieren/Schliessen/Loeschen sperrt den weiteren automatischen Abgleich dieses Eintrags. Fortfahren?') : item.kind === 'comment_deletion' ? (choice === 'restore' ? 'Fehlende Kopien aus den vorhandenen Versionen wiederherstellen? Nummern und Autoren koennen sich aendern.' : choice === 'delete_copies' ? 'Die angezeigten zugeordneten Kopien auf den anderen Plattformen endgueltig loeschen? Vorhandene Originalkommentare bleiben erhalten. Der weitere Abgleich dieses Kommentars bleibt gesperrt.' : 'Vorhandene Kopien behalten und den weiteren Abgleich dieses Kommentars dauerhaft sperren?') : item.category === 'labels' && choice === 'adopt' ? 'Vorhandenes Label zuordnen und seine aktuellen Zielwerte behalten? Spaetere Quellaenderungen werden synchronisiert.' : 'Die ausgewaehlten Werte uebernehmen und die abweichenden Werte dieses Eintrags ersetzen?';
  if (!confirm(text)) return;
  return runSave(null, async () => {
    const result = await request('/api/conflicts/resolve', 'POST', {profile: job.profile, conflict_id: item.resolution_id, choice});
    await refresh();
    if (!el('page-conflicts').hidden) await loadConflictCenter();
    if (result.status !== 'success') throw new Error('Aufloesung nicht vollstaendig abgeschlossen. Bitte das neue Protokoll pruefen.');
    message('Konflikt aufgeloest. Fuer den aktuellen Gesamtstatus einen neuen Sync starten.');
  });
}
function jobCard(job, previous, scope) {
  const signature = JSON.stringify([actor?.role, job, latest.resolved_conflicts]);
  if (previous?._jobSignature === signature) return previous;
  const card = node('article', '', 'card ' + (job.reset_at ? 'reset' : job.status));
  card.dataset.jobId = String(job.id); card._jobSignature = signature;
  const head = node('div', '', 'heading'); head.append(node('strong', '#' + job.id + ' · ' + job.profile), node('span', job.reset_at ? 'Zurueckgesetzt (' + job.status + ')' : job.status, 'badge'));
  const details = node('details', '', 'job-raw'); details.append(node('summary', 'Details / Protokoll'), node('pre', JSON.stringify(job.details, null, 2)));
  const key = scope + ':' + job.id, oldDetails = previous?.querySelector('details.job-raw');
  if (oldDetails) details.open = oldDetails.open;
  else details.open = openedJobDetails.has(key);
  details.addEventListener('toggle', () => { if (details.open) openedJobDetails.add(key); else openedJobDetails.delete(key); });
  card.append(head);
  const conflicts = conflictPanel(job, scope); if (conflicts) card.append(conflicts);
  card.append(node('p', date(job.requested) + ' · ' + job.origin + ' · Versuche: ' + job.attempts), details);
  if (job.reset_at) card.append(node('small', 'Anzeige zurueckgesetzt am ' + date(job.reset_at) + ' von ' + job.reset_by));
  if (admin() && !['queued', 'running'].includes(job.status)) {
    if (['error', 'conflict'].includes(job.status) && !job.reset_at) card.append(button('Anzeige zuruecksetzen', () => manageLogs('reset', {id: job.id})));
    card.append(button('Protokoll loeschen', async () => { if (confirm('Protokoll #' + job.id + ' endgueltig loeschen?')) await manageLogs('delete', {id: job.id}); }, 'danger'));
  }
  if (job.status === 'queued' && admin()) card.append(button('Abbrechen', async () => { await request('/api/jobs/cancel', 'POST', {id: job.id}); await refresh(); }, 'danger'));
  return card;
}
function renderJobList(root, jobs) {
  const existing = new Map([...root.children].map(card => [card.dataset.jobId, card]));
  const cards = jobs.map(job => jobCard(job, existing.get(String(job.id)), root.id));
  if (cards.length === root.children.length && cards.every((card, i) => root.children[i] === card)) return;
  root.replaceChildren(...cards);
}
function renderJobs() {
  const filter = el('job-filter').value;
  renderJobList(el('jobs'), latest.jobs.filter(j => !filter || (filter === 'reset' ? !!j.reset_at : !j.reset_at && j.status === filter)));
}
async function refresh() {
  if (!token) return;
  try {
    const sessionToken = token, response = await request('/api/status');
    if (token !== sessionToken) return;
    latest = response;
    const stats = [['Profile', latest.repositories.length], ['Aktive Jobs', Object.keys(latest.worker.active).length],
      ['Warteschlange', latest.jobs.filter(j => j.status === 'queued').length], ['Konflikte / Fehler', latest.jobs.filter(j => !j.reset_at && ['conflict', 'error'].includes(j.status)).length]];
    el('stats').replaceChildren(...stats.map(([label, value]) => { const c = node('article', '', 'stat'); c.append(node('strong', String(value)), node('span', label)); return c; }));
    const previousProfiles = new Map([...el('repos').children].map(card => [card.dataset.profile, card]));
    const profileCards = latest.repositories.map(repo => profileCard(repo, previousProfiles.get(repo.name)));
    if (profileCards.length !== el('repos').children.length || profileCards.some((card, i) => el('repos').children[i] !== card)) el('repos').replaceChildren(...profileCards); el('profile-summary').replaceChildren(...latest.repositories.map(repo => profileCard(repo)));
    el('profile-summary').hidden = admin();
    document.querySelectorAll('.repo-editor').forEach(updateEditorSummary);
    renderJobList(el('recent'), latest.jobs.slice(0, 5)); renderJobs();
    const w = latest.worker, r = w.resources;
    renderGroups(w.groups || {});
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
  renderDiscovery();
  message(discovered.length + ' Repositories geladen.');
}
function repositoryKey(url) {
  try { const parsed = new URL(url); return parsed.hostname.toLowerCase() + ':' + (parsed.port || (parsed.protocol === 'https:' ? '443' : parsed.protocol === 'http:' ? '80' : '22')) + parsed.pathname.replace(/\/$/, '').replace(/\.git$/, ''); }
  catch { return ''; }
}
function discoveryDrafts(repo) {
  const keys = new Set([repo.clone_url, repo.ssh_url].map(repositoryKey).filter(Boolean));
  return [...el('editors').children].filter(card =>
    !(repo.syncs || []).some(sync => sync.profile === card.querySelector('.repo-name').value) &&
    [...card.querySelectorAll('.remote-url')].some(input => keys.has(repositoryKey(input.value))))
    .map(card => card.querySelector('.repo-name').value || 'Neuer Entwurf');
}
function renderDiscovery() {
  const selected = new Set([...el('discovery-results').querySelectorAll('input:checked')].map(input => input.dataset.index));
  el('discovery-results').replaceChildren(...discovered.map((repo, index) => {
    const row = node('article', '', 'card'), syncs = repo.syncs || [], drafts = discoveryDrafts(repo);
    const assigned = syncs.length > 0 || drafts.length > 0;
    const checkbox = field(row, repo.full_name + (repo.private ? ' · privat' : ''), !assigned && selected.has(String(index)), 'discovered', {type: 'checkbox', disabled: assigned});
    checkbox.dataset.index = index; row.append(node('small', repo.clone_url));
    row.append(node('p', syncs.length ? 'Sync vorhanden' : drafts.length ? 'Bereits im Profil-Editor zugeordnet' : 'Noch keinem Sync zugeordnet', 'discovery-state'));
    for (const sync of syncs) {
      const summary = node('div', '', 'mapping-summary');
      const role = sync.direction === 'one-way' ? (sync.matched_remote === sync.source ? 'Quelle' : 'Ziel') : 'Teilnehmer';
      summary.append(node('strong', sync.profile + ' · ' + (sync.enabled ? 'Aktiv' : 'Deaktiviert') + ' · ' + role));
      const urls = Object.entries(sync.remotes);
      if (sync.direction === 'one-way') {
        summary.append(node('p', 'Einweg: ' + sync.source + ': ' + sync.remotes[sync.source]));
        for (const [name, url] of urls.filter(([name]) => name !== sync.source)) summary.append(node('p', '→ ' + name + ': ' + url));
      } else summary.append(node('p', 'Bidirektional: ' + urls.map(([name, url]) => name + ': ' + url).join(' ↔ ')));
      if (sync.shared_profile) summary.append(node('small', 'Gemeinsames Profil: ' + sync.shared_profile));
      summary.append(button('Profil anzeigen', () => { navigate('profiles'); })); row.append(summary);
    }
    if (drafts.length) row.append(node('p', 'Im Editor: ' + drafts.join(', ') + '. Vor erneutem Import dort pruefen.'));
    return row;
  }));
}
function importDiscovery() {
  const target = providers.find(p => p.id === el('discovery-target').value), namespace = el('discovery-namespace').value.trim();
  if (!target || !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(namespace) || namespace.split('/').some(p => p === '.' || p === '..')) throw new Error('Ziel-Provider und gültigen Namespace angeben.');
  let count = 0;
  for (const checkbox of el('discovery-results').querySelectorAll('input:checked')) {
    const candidate = discovered[Number(checkbox.dataset.index)];
    if (candidate.syncs?.length || discoveryDrafts(candidate).length) continue;
    const repo = candidate, leaf = repo.full_name.split('/').at(-1);
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
function setupConflictForm() {
  const profiles = latest?.repositories || [], selected = el('conflict-profile').value;
  el('conflict-profile').replaceChildren(...profiles.map(r => { const o = node('option', r.name); o.value = r.name; return o; }));
  if (profiles.some(r => r.name === selected)) el('conflict-profile').value = selected;
  updateConflictTarget();
}
function updateConflictTarget() {
  const repo = latest?.repositories.find(r => r.name === el('conflict-profile').value), old = el('conflict-target').value;
  el('conflict-target').replaceChildren(...(repo?.remotes || []).filter(n => n !== repo.source).map(n => { const o = node('option', n); o.value = n; return o; }));
  if ([...el('conflict-target').options].some(o => o.value === old)) el('conflict-target').value = old;
  el('conflict-source-info').textContent = 'Quelle: ' + (repo?.source || 'Kein Profil');
}
function conflictRequest() {
  return {profile: el('conflict-profile').value, target: el('conflict-target').value, category: el('conflict-category').value,
    source_key: el('conflict-source-key').value, target_key: el('conflict-target-key').value,
    source_ticket: el('conflict-source-ticket').value, target_ticket: el('conflict-target-ticket').value, tag: el('conflict-tag').value};
}
async function loadConflictCenter(page = 1, append = false) {
  const result = await request('/api/conflicts?page=' + page);
  if (!append) conflictTableRows = [];
  conflictTableRows.push(...result.items);
  problemTable(conflictTableRows.filter(item=>!el('conflict-filter').value || JSON.stringify(item).toLowerCase().includes(el('conflict-filter').value.toLowerCase())),el('problem-table'),'conflict');
  if (!append && admin()) await loadDecisions();
  if (!append) el('conflict-list').replaceChildren();
  const filter = el('conflict-filter').value.toLowerCase();
  for (const item of result.items) {
    if (filter && !JSON.stringify([item.profile, item.category, item.name]).toLowerCase().includes(filter)) continue;
    const card = node('article', '', 'card'); card.append(node('h2', item.profile));
    const panel = conflictPanel({id: item.resolution_id, profile: item.profile, status: 'conflict', details: {metadata: {items: [item], counts: {conflict: 1}}}}, 'center');
    if (panel) card.append(panel); el('conflict-list').append(card);
  }
  if (result.next_page) el('conflict-list').append(button('Weitere Konflikte laden', async () => { await loadConflictCenter(result.next_page, true); }));
  if (!append && admin()) for (const hold of result.holds || []) {
    const row = node('article', '', 'card'); row.append(node('strong', hold.profile + ': ' + hold.item), node('p', 'Vom automatischen Abgleich ausgenommen: ' + hold.action));
    row.append(button('Sperre aufheben', () => runSave(null, async () => {
      if (!confirm('Automatischen Abgleich dieses Eintrags wieder zulassen?')) return;
      await request('/api/conflicts/resume', 'POST', {profile: hold.profile, hold_id: hold.hold_id}); await loadConflictCenter();
    }))); el('conflict-list').append(row);
  }
  if (!append && !result.items.length) el('conflict-list').append(node('p', 'Keine offenen gespeicherten Konflikte.'));
  if (!append) {
    el('conflict-other').replaceChildren();
    for (const job of latest?.jobs || []) {
      if (!(job.status === 'error' || job.details?.conflicts?.length || job.details?.wiki?.status === 'conflict' || job.details?.releases?.conflicts)) continue;
      const card = node('article', '', 'card'); card.append(node('strong', job.profile), node('pre', JSON.stringify({git: job.details.conflicts, wiki: job.details.wiki, releases: job.details?.releases?.assets?.filter(a => a.status === 'conflict'), errors: job.details?.metadata?.errors || job.details?.errors || job.details?.message || job.details?.git_error, lfs: job.details?.lfs}, null, 2)));
      if (admin()) card.append(button('Im Vergleich pruefen', () => { el('conflict-profile').value = job.profile; updateConflictTarget(); el('conflict-compare').scrollIntoView({behavior: 'smooth'}); }));
      if (admin()) card.append(button('Erneut synchronisieren', async () => { await request('/api/sync', 'POST', {profile: job.profile}); message('Neue Pruefung eingereiht.'); }));
      el('conflict-other').append(card);
    }
  }
}
el('conflict-profile').addEventListener('change', updateConflictTarget);
el('conflict-reload').addEventListener('click', () => loadConflictCenter().catch(showError));
el('conflict-filter').addEventListener('change', () => loadConflictCenter().catch(showError));
saveForm('conflict-compare', async () => {
  const payload = conflictRequest(), result = await request('/api/conflicts/compare', 'POST', payload);
  const panel = conflictPanel({id: result.resolution_id, profile: payload.profile, status: 'conflict', details: {metadata: {items: [result], counts: {conflict: 1}}}}, 'comparison');
  el('conflict-comparison').replaceChildren(panel);
  if (result.comparison) el('conflict-comparison').append(node('pre', JSON.stringify(result.comparison, null, 2)));
  message('Vergleich geladen. Noch keine Remote-Aenderung ausgefuehrt.');
});
el('conflict-inventory').addEventListener('click', () => runSave(null, async () => {
  const payload = conflictRequest(), result = await request('/api/conflicts/inventory', 'POST', payload);
  el('conflict-inventory-list').replaceChildren();
  if (result.items) {
    for (const [remote, rows] of Object.entries(result.items)) {
      const section = node('div', '', 'card'); section.append(node('h3', remote));
      for (const row of rows) section.append(button(row.key + ' · ' + row.name, () => { el(remote === payload.target ? 'conflict-target-key' : 'conflict-source-key').value = row.key; }));
      el('conflict-inventory-list').append(section);
    }
  } else {
    for (const remote of result.remotes || []) {
      const section = node('div', '', 'card'); section.append(node('h3', remote.name));
      for (const row of remote.refs || []) section.append(button(row.ref + ' · ' + row.oid.slice(0,12), () => { el('conflict-source-key').value = row.ref.replace(/^(heads|tags)\//, ''); }));
      el('conflict-inventory-list').append(section);
    }
  }
}).catch(showError));

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
saveForm('settings-form', saveSettings);
el('settings-form').addEventListener('invalid', event => { const editor = event.target.closest('details.repo-editor'); if (editor) editor.open = true; }, true);
for (const event of ['input', 'change']) el('settings-form').addEventListener(event, () => message('Ungespeicherte Aenderungen. Bitte Einstellungen speichern.'));
saveForm('worker-form', async () => {
  const saved = await request('/api/settings');
  saved.worker = {parallelism: Number(el('parallelism').value), min_free_mb: Number(el('min-free').value),
    min_memory_mb: Number(el('min-memory').value), max_load: Number(el('max-load').value)};
  await persistSettings(saved);
  configuration.worker = saved.worker;
  message('Worker-Einstellungen gespeichert.'); await refresh();
});
el('provider-new').addEventListener('click', resetProviderForm);
saveForm('provider-form', async () => {
  try {
    await request('/api/providers', 'POST', {id: el('provider-id').value || undefined, name: el('provider-name').value.trim(), kind: el('provider-kind').value,
      allow_http: el('provider-http').checked, url: el('provider-url').value.trim(), username: el('provider-username').value.trim(), token: el('provider-token').value, clear_token: el('provider-clear').checked});
    resetProviderForm(); await loadProviders(); message('Provider gespeichert.');
  } catch (error) { el('provider-token').value = ''; throw error; }
});
el('authorship-provider').addEventListener('change', () => loadAuthorship().catch(showError));
el('authorship-load').addEventListener('click', () => loadAuthorship().catch(showError));
saveForm('authorship-form', async () => {
  const result = await request('/api/authorship', 'POST', {id: el('authorship-provider').value, enabled: el('authorship-enabled').checked, bot_provider: el('authorship-bot').value});
  await loadAuthorship();
  message(result.enabled ? 'Autoren-Zuordnung gespeichert: eigene Beitraege als ' + result.personal_account + ', fremde als ' + result.bot_account + '.' : 'Autoren-Zuordnung deaktiviert. Bereits verfasste Kopien behalten ihren Autor.');
});
saveForm('user-form', async () => { const name = el('user-name').value;
  try {
    await request('/api/users', 'POST', {name, role: el('user-role').value, password: el('user-password').value, enabled: el('user-enabled').checked});
    el('user-password').value = '';
    if (actor.name === name) { clearSession(); message('Konto aktualisiert. Bitte erneut anmelden.'); }
    else { el('user-form').reset(); await loadUsers(); message('Benutzer gespeichert.'); }
  } catch (error) { el('user-password').value = ''; throw error; }
});
document.querySelectorAll('[data-worker]').forEach(b => b.addEventListener('click', () => request('/api/worker', 'POST', {action: b.dataset.worker}).then(refresh).catch(showError)));
async function manageLogs(action, body) {
  const result = await request('/api/jobs/' + action, 'POST', body);
  if (action === 'delete' && body.id) for (const key of openedJobDetails) { if (key.endsWith(':' + body.id)) openedJobDetails.delete(key); }
  message(result.changed + (action === 'delete' ? ' Protokolle geloescht.' : ' Fehler-/Konfliktanzeigen zurueckgesetzt.'));
  await refresh();
}
el('logs-delete').addEventListener('click', async () => {
  if (!latest) return;
  const before = latest.snapshot_time;
  try { if (confirm('Alle abgeschlossenen Protokolle dieses Zeitstands endgueltig loeschen? Laufende und wartende Jobs bleiben erhalten.')) await manageLogs('delete', {before}); } catch (error) { showError(error); }
});
el('logs-reset').addEventListener('click', () => { if (latest) manageLogs('reset', {before: latest.snapshot_time}).catch(showError); });
el('job-filter').addEventListener('change', renderJobs);
el('discovery-form').addEventListener('submit', e => { e.preventDefault(); discover().catch(showError); });
el('discovery-more').addEventListener('click', () => discover(discoveryNext).catch(showError));
el('discovery-import').addEventListener('click', () => { try { importDiscovery(); } catch (error) { showError(error); } });
setInterval(refresh, 5000);

let toolComparison = null;
const toolBody = () => ({profile: el('tool-profile').value});
function toolResult(result, target = 'tool-output') {
  const pre = node('pre', JSON.stringify(result, null, 2)); el(target).replaceChildren(pre);
}
async function toolCall(action, body = {}) {
  message('Repository-Operation laeuft ...');
  const result = await request('/api/' + action, 'POST', {...toolBody(), ...body});
  message('Repository-Operation abgeschlossen.'); return result;
}
el('tool-profile').addEventListener('change', () => {
  toolComparison = null; ['tool-output', 'tool-comparison', 'tool-inventory'].forEach(id => el(id).replaceChildren());
});
el('tool-preview').addEventListener('click', () => toolCall('preview').then(renderPreview).catch(showError));
async function loadToolRefs() {
  const result = await toolCall('refs'); const root = el('tool-inventory'); root.replaceChildren();
  for (const remote of result.remotes) {
    const card = node('article', '', 'card'); card.append(node('h2', remote.name + (remote.missing ? ' (fehlt)' : '')));
    for (const ref of remote.refs) {
      const row = node('p', ref.ref + ' ' + ref.oid.slice(0, 12) + ' ');
      if (admin()) row.append(button('Loeschen', async () => {
        if (!confirm(remote.name + ': ' + ref.ref + ' loeschen? Ein spaeterer Sync kann die Referenz aus anderen Remotes wiederherstellen.')) return;
        const split = ref.ref.indexOf('/');
        toolResult(await toolCall('refs/change', {remote: remote.name, kind: ref.ref.slice(0, split), name: ref.ref.slice(split + 1), action: 'delete', expected_oid: ref.oid, confirm_delete: true}));
        await loadToolRefs();
      }, 'danger'));
      card.append(row);
    }
    if (remote.truncated) card.append(node('p', 'Anzeige auf 1000 Referenzen begrenzt.'));
    root.append(card);
  }
}
el('tool-refs').addEventListener('click', () => loadToolRefs().catch(showError));
el('tool-compare').addEventListener('submit', async event => {
  event.preventDefault(); toolComparison = null;
  const body = {...toolBody(), left: el('tool-left').value.trim(), right: el('tool-right').value.trim(), ref: el('tool-ref').value.trim()};
  try { const result = await toolCall('compare', body); toolComparison = {body, result}; renderComparison(result); } catch (error) { showError(error); }
});
el('tool-merge').addEventListener('click', async () => {
  try {
    if (!toolComparison || toolComparison.body.profile !== el('tool-profile').value) throw new Error('Zuerst die Historien vergleichen.');
    const {body, result} = toolComparison; const target = el('tool-target').value.trim();
    if (!confirm(body.ref + ' auf ' + target + ' zusammenfuehren und pushen?')) return;
    toolComparison = null;
    toolResult(await toolCall('merge', {...body, target, expected_left: result.left_oid, expected_right: result.right_oid, confirm_merge: true}));
  } catch (error) { showError(error); }
});
el('tool-create').addEventListener('submit', async event => {
  event.preventDefault();
  try { toolResult(await toolCall('refs/change', {action: 'create', remote: el('tool-remote').value.trim(), kind: el('tool-kind').value, name: el('tool-name').value.trim(), from_remote: el('tool-from').value.trim(), from_ref: el('tool-from-ref').value.trim()})); await loadToolRefs(); } catch (error) { showError(error); }
});
saveForm('hook-settings', async () => {
  const config = await request('/api/settings'); config.public_url = el('public-url').value.trim(); await persistSettings(config); configuration.public_url = config.public_url; message('Oeffentliche Adresse gespeichert.');
});
el('tool-hooks').addEventListener('click', () => toolCall('webhooks/register').then(toolResult).catch(showError));

function renderComparison(result) {
  const root = el('tool-comparison'); root.replaceChildren();
  root.append(node('p', 'Nur links: ' + result.left_only + ' Commits | Nur rechts: ' + result.right_only + ' Commits'));
  const grid = node('div', '', 'grid');
  for (const [title, commits] of [['Nur links', result.left_commits], ['Nur rechts', result.right_commits]]) {
    const card = node('article', '', 'card'); card.append(node('h3', title));
    for (const c of commits) card.append(node('p', c.oid.slice(0, 12) + ' ' + c.subject + ' (' + c.author + ')'));
    grid.append(card);
  }
  root.append(grid, node('pre', result.stat), node('pre', result.patch));
  if (result.patch_truncated) root.append(node('p', 'Diff-Anzeige auf 4000 Zeichen begrenzt.'));
}

function updateToolProfiles() {
  const previous = el('tool-profile').value; el('tool-profile').replaceChildren();
  configuration.repositories.forEach(r => { const o = node('option', r.name); o.value = r.name; el('tool-profile').append(o); });
  if (configuration.repositories.some(r => r.name === previous)) el('tool-profile').value = previous;
}

function renderPreview(result) {
  const root = el('tool-output'); root.replaceChildren();
  root.append(node('h2', 'Sync-Vorschau: ' + result.status));
  if (result.blocked) root.append(node('p', 'Konfliktregel reject: Das gesamte Profil wird uebersprungen.'));
  for (const conflict of result.conflicts) root.append(node('p', 'Konflikt: ' + conflict));
  for (const missing of result.missing_repositories || []) root.append(node('p', 'Ziel muss angelegt werden: ' + missing));
  for (const update of result.plan) {
    const card = node('article', '', 'card'); card.append(node('h3', update.remote + ': ' + update.ref));
    card.append(node('p', (update.old || 'Neue Referenz') + ' → ' + (update.new || '')));
    for (const c of update.commits || []) card.append(node('p', c.oid.slice(0, 12) + ' ' + c.subject));
    root.append(card);
  }
  if (!result.plan.length) root.append(node('p', 'Keine Updates geplant.'));
  if (result.plan_truncated) root.append(node('p', 'Anzeige auf 200 Updates begrenzt.'));
}

const componentFields = {...Object.fromEntries(Object.keys(syncOptions).map(key => [key, key])), tags: 'tags', releases: 'releases', release_assets: 'release-assets', release_max_asset_mb: 'release-max'};
const sharedFields = {...componentFields, enabled: 'enabled', direction: 'direction', source: 'source', policy: 'policy', branches: 'branches', tags: 'tags', interval: 'interval', worker_set: 'worker-set', force_confirmed: 'force', create_missing: 'create', create_private: 'create-private', auto_webhooks: 'hooks', webhook_secret_env: 'secret', releases: 'releases', release_assets: 'release-assets', release_max_asset_mb: 'release-max'};
function applySharedRules(card) {
  const rules = configuration?.profiles?.[card.querySelector('.repo-shared').value] || {};
  for (const [key, cls] of Object.entries(sharedFields)) {
    const input = card.querySelector('.repo-' + cls); if (!input) continue;
    input.disabled = Object.hasOwn(rules, key) && !Object.hasOwn(componentFields, key);
    if (Object.hasOwn(rules, key)) {
      const value = Object.hasOwn(componentFields, key) && Object.hasOwn(card._syncOverrides || {}, key) ? card._syncOverrides[key] : rules[key];
      if (input.type === 'checkbox') input.checked = value;
      else input.value = Array.isArray(value) ? value.join(', ') : value;
    }
  }
  updateEditorSummary(card);
}
async function saveAdminConfig(edit) {
  return runSave(document.activeElement?.closest('form'), async () => {
    const config = await request('/api/settings'); edit(config);
    const saved = await persistSettings(config); await loadSettings(saved);
    message('Gespeichert. Repository-Editoren zeigen den gespeicherten Stand.');
  });
}
function renderGroups(states = latest?.worker?.groups || {}) {
  if (!configuration) return;
  el('shared-list').replaceChildren();
  for (const [name, profile] of Object.entries(configuration.profiles || {})) {
    const card = node('article', '', 'card');
    card.append(node('h2', name), node('p', configuration.repositories.filter(r => r.profile === name).length + ' Repository-Zuordnungen'), button('Bearbeiten', () => {
      el('shared-name').value = name; el('shared-direction').value = profile.direction || 'bidirectional';
      el('shared-source').value = profile.source || ''; el('shared-policy').value = profile.policy || 'ff-only';
      el('shared-branches').value = (profile.branches || []).join(', '); el('shared-interval').value = profile.interval ?? configuration.interval;
      el('shared-worker').value = profile.worker_set || 'default'; el('shared-enabled').checked = profile.enabled !== false;
      el('shared-releases').checked = !!profile.releases; el('shared-release-assets').checked = profile.release_assets !== false; el('shared-release-max').value = profile.release_max_asset_mb ?? 512;
      for (const key of Object.keys(syncOptions)) el('shared-' + key).checked = profile[key] ?? key === 'sync_branches';
      el('shared-tags').checked = profile.tags !== false; el('shared-force').checked = !!profile.force_confirmed;
    }), button('Entfernen', async () => {
      if (!confirm('Gemeinsames Profil ' + name + ' entfernen? Zuerst alle Zuordnungen loesen.')) return;
      await saveAdminConfig(c => { delete c.profiles[name]; });
    }, 'danger')); el('shared-list').append(card);
  }
  el('group-list').replaceChildren();
  for (const [name, settings] of Object.entries({default: configuration.worker || {}, ...(configuration.worker_sets || {})})) {
    const state = states[name] || {};
    const card = node('article', '', 'card'); card.append(node('h2', name), node('p', (state.mode || 'unbekannt') + ' | aktiv: ' + (state.active ?? 0) + ' | parallel: ' + (settings.parallelism ?? 2)), node('p', state.notice || ''));
    for (const [action, label] of [['start', 'Starten'], ['pause', 'Pausieren'], ['resume', 'Fortsetzen'], ['stop', 'Stoppen']]) card.append(button(label, async () => {
      await request('/api/worker', 'POST', {action, group: name}); await refresh();
    }));
    if (name !== 'default') {
      card.append(button('Bearbeiten', () => {
        el('group-name').value = name; el('group-parallelism').value = settings.parallelism ?? 2;
        el('group-disk').value = settings.min_free_mb ?? 128; el('group-memory').value = settings.min_memory_mb ?? 0; el('group-load').value = settings.max_load ?? 0;
      }), button('Entfernen', async () => { if (confirm('Worker-Gruppe ' + name + ' entfernen?')) await saveAdminConfig(c => { delete c.worker_sets[name]; }); }, 'danger'));
    }
    el('group-list').append(card);
  }
}
el('shared-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    const name = el('shared-name').value.trim();
    const profile = {direction: el('shared-direction').value, source: el('shared-source').value.trim(), policy: el('shared-policy').value,
      branches: el('shared-branches').value.split(',').map(x => x.trim()).filter(Boolean), interval: Number(el('shared-interval').value),
      releases: el('shared-releases').checked, release_assets: el('shared-release-assets').checked, release_max_asset_mb: Number(el('shared-release-max').value),
      worker_set: el('shared-worker').value.trim(), enabled: el('shared-enabled').checked, tags: el('shared-tags').checked, force_confirmed: el('shared-force').checked};
    for (const key of Object.keys(syncOptions)) profile[key] = el('shared-' + key).checked;
    await saveAdminConfig(c => { c.profiles ||= {}; c.profiles[name] = {...c.profiles[name], ...profile}; });
  } catch (error) { showError(error); }
});
el('shared-sync').addEventListener('click', () => request('/api/sync', 'POST', {shared_profile: el('shared-name').value.trim()}).then(() => { message('Alle aktiven Zuordnungen eingereiht.'); return refresh(); }).catch(showError));
el('group-form').addEventListener('submit', async e => {
  e.preventDefault();
  try { await saveAdminConfig(c => { c.worker_sets ||= {}; c.worker_sets[el('group-name').value.trim()] = {parallelism: Number(el('group-parallelism').value), min_free_mb: Number(el('group-disk').value), min_memory_mb: Number(el('group-memory').value), max_load: Number(el('group-load').value)}; }); }
  catch (error) { showError(error); }
});
el('transport-load').addEventListener('click', async () => {
  try {
    const value = await request('/api/transport?id=' + encodeURIComponent(el('transport-provider').value));
    el('transport-protocol').value = value.protocol; el('transport-host').value = value.ssh_host; el('transport-user').value = value.ssh_user;
    el('transport-port').value = value.ssh_port; el('transport-hosts').value = value.known_hosts; el('transport-key').value = ''; el('transport-clear').checked = false;
    el('transport-info').textContent = (value.has_key ? 'Schluessel verschluesselt gespeichert. ' : 'Kein gespeicherter Schluessel. ') + (value.fingerprints || []).join(' ');
  } catch (error) { showError(error); }
});
el('transport-provider').addEventListener('change', () => { const id = el('transport-provider').value; el('transport-form').reset(); el('transport-provider').value = id; el('transport-key').value = ''; el('transport-info').textContent = 'Transport fuer diesen Provider zuerst laden.'; });
saveForm('transport-form', async () => {
  try {
    const result = await request('/api/transport', 'POST', {id: el('transport-provider').value, protocol: el('transport-protocol').value, ssh_host: el('transport-host').value.trim(), ssh_user: el('transport-user').value.trim(), ssh_port: Number(el('transport-port').value), known_hosts: el('transport-hosts').value, key: el('transport-key').value, clear_key: el('transport-clear').checked});
    el('transport-info').textContent = (result.has_key ? 'Schluessel gespeichert. ' : 'Kein Schluessel gespeichert. ') + (result.fingerprints || []).join(' ');
    message('Transport gespeichert.');
  } finally { el('transport-key').value = ''; }
});

let mappingReport = null, mappingPaused = true, mappingBusy = false, mappingShown = 200, mappingBatchRemaining = [], mappingBatchReports = [];
function renderMappingReport() {
  const report = mappingReport, root = el('mapping-results'); root.replaceChildren();
  if (!report) return;
  el('mapping-progress').textContent = (mappingBatchReports.length ? mappingBatchReports.length + ' Profile abgeschlossen. ' : '') + report.profile + ': ' + report.processed + ' API-Seiten geladen, ' + report.pending + ' offen. ' + (report.done ? JSON.stringify(report.summary) : mappingPaused ? 'Pausiert.' : 'Pruefung laeuft.');
  el('mapping-resume').disabled = mappingBusy || (report.done && !mappingBatchRemaining.length);
  el('mapping-pause').disabled = !mappingBusy;
  el('mapping-download').disabled = false;
  const filter = el('mapping-filter').value;
  problemTable(report.results.map((item,index)=>({...item,profile:report.profile,_index:index})).filter(item=>!filter || item.status===filter).slice(0,mappingShown),el('mapping-table'),'audit');
  let shown = 0;
  for (const [index, row] of report.results.entries()) {
    if (filter && row.status !== filter) continue;
    if (++shown > mappingShown) { root.append(button('Weitere Befunde anzeigen', () => { mappingShown += 200; renderMappingReport(); })); break; }
    const card = node('article', '', 'card');
    card.append(node('strong', row.status + ' · ' + row.category + ' · ' + row.remote + ' · ' + (row.name || row.key || row.item || 'Zuordnung')), node('p', row.reason));
    if (row.values) {
      const columns = node('div', '', 'grid');
      for (const [remote, value] of Object.entries(row.values)) {
        const side = node('div'); side.append(node('strong', remote), node('p', value ? value.name + ' · Kennung ' + value.key + ' · ID ' + value.id + ' · ' + (value.state || 'Kein Status') : 'Fehlt / nicht sichtbar')); columns.append(side);
      }
      card.append(columns);
    }
    if (row.url) { try { const url = new URL(row.url); if (['https:', 'http:'].includes(url.protocol)) { const a = node('a', 'Auf Plattform oeffnen'); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; card.append(a); } } catch {} }
    if (row.source && row.target_key && !['identity', 'mapping', 'authorship'].includes(row.category)) card.append(button('Im Objektvergleich pruefen', () => {
      el('conflict-profile').value = report.profile; updateConflictTarget(); el('conflict-category').value = row.category; el('conflict-target').value = row.remote;
      // Object IDs and provider-local numbers differ: load the inventories before choosing.
      el('conflict-source-key').value = ''; el('conflict-target-key').value = row.target_key;
      el('conflict-compare').scrollIntoView({behavior: 'smooth'}); message('Quelle: ID ' + row.source_id + '. Mit Objekte laden die passende Nummer waehlen.');
    }));
    if (row.scope && row.item && row.target_key) for (const [action, text] of [['ignore', 'Ignorieren'], ['unlink', 'Verknuepfung loesen und sperren']]) card.append(button(text, () => runSave(null, async () => {
      if (!confirm(text + '? Keine Remote-Objekte werden geloescht. Der automatische Abgleich dieses Eintrags wird gesperrt.')) return;
      const result = await request('/api/decisions/enqueue', 'POST', {items:[{action:'mappings/repair',body:{profile:report.profile,audit_id:report.id,index,action}}]}); message(result.message);
      row.reason = result.message; renderMappingReport();
    })));
    root.append(card);
  }
}
async function continueMappingAudit() {
  if (!mappingReport || mappingBusy || (mappingReport.done && !mappingBatchRemaining.length)) return;
  const sessionToken = token; mappingBusy = true; mappingPaused = false;
  try {
    while (!mappingPaused && token === sessionToken) {
      if (mappingReport.done) {
        if (!mappingBatchRemaining.length) break;
        mappingBatchReports.push(mappingReport);
        const next = await request('/api/mappings/start','POST',{profile:mappingBatchRemaining.shift()});
        if (token !== sessionToken) return;
        mappingReport = next;
      }
      renderMappingReport();
      const result = await request('/api/mappings/step', 'POST', {profile:mappingReport.profile,audit_id:mappingReport.id,git:true});
      if (token !== sessionToken) return;
      mappingReport = result;
      if (result.paused) { mappingPaused = true; message(result.message); }
    }
  } catch (error) { mappingPaused = true; showError(error); }
  finally { mappingBusy = false; if (token === sessionToken) renderMappingReport(); }
}
el('mapping-start').addEventListener('click', () => runSave(null, async () => {
  if (mappingBusy) throw new Error('Laufende Diagnose zuerst pausieren.');
  const profile = el('conflict-profile').value;
  mappingPair = null; mappingBatchRemaining = []; mappingBatchReports = []; el('pair-confirm').disabled = true; mappingShown = 200;
  mappingReport = await request('/api/mappings/start', 'POST', {profile});
  renderMappingReport(); void continueMappingAudit();
}).catch(showError));
el('mapping-resume').addEventListener('click', () => continueMappingAudit().catch(showError));
el('mapping-pause').addEventListener('click', () => { mappingPaused = true; message('Pause nach der aktuellen API-Seite.'); });
el('mapping-filter').addEventListener('change', () => { mappingShown = 200; renderMappingReport(); });
el('mapping-download').addEventListener('click', () => {
  if (!mappingReport) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(mappingBatchReports.length ? [...mappingBatchReports,mappingReport] : mappingReport,null,2)], {type:'application/json'}));
  const a = document.createElement('a'); a.href = url; a.download = 'gitmesh-zuordnungen-' + mappingReport.profile + '.json'; a.click(); URL.revokeObjectURL(url);
});

let mappingPair = null;
el('mapping-latest').addEventListener('click', () => runSave(null, async () => {
  if (mappingBusy) throw new Error('Diagnose zuerst pausieren.');
  mappingReport = await request('/api/mappings/report','POST',{profile:el('conflict-profile').value}); mappingPaused = true; renderMappingReport();
}).catch(showError));
el('mapping-pair').addEventListener('input', () => { mappingPair = null; el('pair-confirm').disabled = true; });
saveForm('mapping-pair', async () => {
  if (!mappingReport?.done) throw new Error('Diagnose zuerst abschliessen.');
  const body = {profile:mappingReport.profile,audit_id:mappingReport.id,category:el('pair-category').value,source:el('pair-source').value,target:el('pair-target').value,
    source_key:el('pair-source-key').value,target_key:el('pair-target-key').value,source_ticket:el('pair-source-ticket').value,target_ticket:el('pair-target-ticket').value};
  const preview = await request('/api/mappings/pair-preview','POST',body); mappingPair = {...body,confirmation:preview.confirmation};
  el('pair-values').textContent = JSON.stringify(preview.versions,null,2); el('pair-confirm').disabled = false;
});
el('pair-confirm').addEventListener('click', () => runSave(null,async () => {
  if (!mappingPair || !confirm('Diese beiden Objekte verknuepfen? Keine Remote-Inhalte werden jetzt veraendert. Unterschiede vor dem naechsten Sync pruefen.')) return;
  const result=await request('/api/decisions/enqueue','POST',{items:[{action:'mappings/pair-apply',body:mappingPair}]}); mappingPair=null; el('pair-confirm').disabled=true; message(result.message);
}).catch(showError));

el('mapping-all').addEventListener('click', () => runSave(null,async () => {
  if (mappingBusy) throw new Error('Laufende Diagnose zuerst pausieren.');
  const profiles=(latest?.repositories || []).map(r => r.name);
  if (!profiles.length) throw new Error('Keine gespeicherten Profile.');
  mappingPair=null; el('pair-confirm').disabled=true; mappingShown=200; mappingBatchReports=[];
  mappingBatchRemaining=profiles.slice(1);
  mappingReport=await request('/api/mappings/start','POST',{profile:profiles[0]}); renderMappingReport(); void continueMappingAudit();
}).catch(showError));

const decisionSelections = new Map();
function decisionLabel(choice, item) {
  if (item?.kind === 'mesh' && choice !== 'ignore') return 'Version von ' + choice + ' auf die verknuepften Plattformen uebernehmen';
  return ({ignore:'Ignorieren und sperren',unlink:'Verknuepfung loesen und sperren',restore:'Fehlende Kopie wiederherstellen',delete:'Zielkopie loeschen',delete_copies:'Verbleibende Kopien loeschen',keep_copies:'Kopien behalten und sperren',close:'Am Ziel schliessen',merge:'Am Ziel zusammenfuehren',apply_source:'Quelle auf Ziel uebernehmen',source:'Quellwerte uebernehmen',adopt:'Vorhandene Kopie zuordnen',resume:'Sperre aufheben'})[choice] || 'Version von ' + choice + ' uebernehmen';
}
function problemTable(rows, root, mode) {
  const table=node('table','','problem-table'), head=node('thead'), header=node('tr');
  for (const title of ['Profil / Typ / Problem','Quelle / Original','Ziel / Kopie','Loesung']) header.append(node('th',title));
  head.append(header); table.append(head); const body=node('tbody');
  for (const item of rows) {
    const tr=node('tr'), info=node('td'), source=node('td'), target=node('td'), actions=node('td');
    info.append(node('strong',(item.profile || mappingReport?.profile || '')+' · '+item.category),node('p',item.name || item.item || item.key || 'Zuordnung'),node('small',item.reason || ''));
    const configured=latest?.repositories.find(r=>r.name===(item.profile || mappingReport?.profile));
    const from=item.source || (mode==='conflict' || item.source_id ? configured?.source : null), to=item.target || item.remote;
    const values=item.values || {};
    source.append(node('strong',from || 'Ursprung unklar'));
    source.append(node('pre',JSON.stringify(values[from] ?? (item.source_id ? {ID:item.source_id} : 'Nicht eindeutig zugeordnet'),null,2)));
    if (to) { target.append(node('strong',to),node('pre',JSON.stringify(values[to] ?? {Kennung:item.target_key || item.key || '',Status:item.status},null,2))); }
    else for (const [remote,value] of Object.entries(values)) if (remote!==from) target.append(node('strong',remote),node('pre',JSON.stringify(value,null,2)));
    const key=mode==='conflict' ? 'conflict:'+item.resolution_id : 'audit:'+mappingReport.id+':'+item._index;
    const choices=mode==='conflict' ? item.choices || [] : item.scope && item.item && item.target_key ? ['ignore','unlink'] : [];
    if (admin() && choices.length) {
      const select=document.createElement('select'); select.setAttribute('aria-label','Loesung fuer '+(item.name || item.item || 'Problem'));
      select.append(new Option('Noch keine Entscheidung',''));
      for (const choice of choices) select.append(new Option(decisionLabel(choice,item),choice));
      select.value=decisionSelections.get(key)?.choice || '';
      select.addEventListener('change',()=>{
        if (!select.value) { decisionSelections.delete(key); return; }
        const payload=mode==='conflict' ? {profile:item.profile,conflict_id:item.resolution_id,choice:select.value} : {profile:mappingReport.profile,audit_id:mappingReport.id,index:item._index,action:select.value};
        decisionSelections.set(key,{choice:select.value,label:(item.profile || mappingReport.profile)+' · '+(item.name || item.item || item.key || '')+' → '+decisionLabel(select.value,item),action:mode==='conflict' ? 'conflicts/resolve' : 'mappings/repair',body:payload});
      }); actions.append(select);
    } else {
      actions.append(node('p','Zuerst Zuordnung / Vergleich pruefen.'));
      if (admin() && mode==='audit' && item.key) actions.append(button('Zuordnung auswaehlen',()=>{
        el('pair-category').value=item.category; el('pair-target').value=to || ''; el('pair-target-key').value=item.key; el('pair-target-ticket').value=item.ticket || '';
        const details=el('mapping-pair').closest('details'); details.open=true; details.scrollIntoView({behavior:'smooth'});
      }));
    }
    tr.append(info,source,target,actions); body.append(tr);
  }
  table.append(body); root.replaceChildren(table);
}
async function loadDecisions() {
  if (!admin()) return;
  const result=await request('/api/decisions'); const root=el('decision-list'); root.replaceChildren();
  const labels={queued:'Wartet auf Freigabe',running:'Wird ausgefuehrt',success:'Erfolgreich',needs_review:'Erneut vergleichen',error:'Fehler – Stand pruefen',cancelled:'Abgebrochen'};
  for (const item of result.items) {
    const row=node('div','','decision-status'); row.append(node('strong','#'+item.id+' · '+item.profile+' · '+(labels[item.status] || item.status)),node('p',item.selection),node('p',item.details?.message || ''));
    if (item.status==='queued') row.append(button('Auftrag abbrechen',async()=>{await request('/api/decisions/cancel','POST',{id:item.id}); await loadDecisions();}));
    root.append(row);
  }
  if (!result.items.length) root.append(node('p','Keine gespeicherten Loesungsauftraege.'));
}
el('decision-submit').addEventListener('click',()=>runSave(null,async()=>{
  const selected=[...decisionSelections.values()];
  if (!selected.length) throw new Error('Zuerst Loesungen in den Tabellen waehlen.');
  const deletion = selected.some(x=>['delete','delete_copies'].includes(x.body.choice));
  if (!confirm('Diese Entscheidungen speichern und sobald moeglich ausfuehren?' + (deletion ? ' Loeschungen koennen nicht rueckgaengig gemacht werden.' : '') + '\n\n'+selected.map(x=>x.label).join('\n'))) return;
  const result=await request('/api/decisions/enqueue','POST',{items:selected.map(x=>({action:x.action,body:x.body}))});
  decisionSelections.clear(); message(result.message); await loadConflictCenter(); await loadDecisions(); if(mappingReport) renderMappingReport();
}).catch(showError));
el('decision-clear').addEventListener('click',()=>{decisionSelections.clear(); loadConflictCenter().catch(showError); if(mappingReport) renderMappingReport();});
