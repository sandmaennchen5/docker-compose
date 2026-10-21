let token = '';
const el = id => document.getElementById(id);
function node(tag, text = '', cls) {
  const n = document.createElement(tag);
  n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
async function request(path, method = 'GET', body) {
  const r = await fetch(path, {
    method, headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const result = await r.json();
  if (!r.ok) throw new Error(result.error || 'Anfrage fehlgeschlagen (' + r.status + ')');
  return result;
}
function field(parent, text, value, cls, options = {}) {
  const label = node('label', text);
  const input = document.createElement('input');
  input.value = value;
  input.className = cls;
  Object.assign(input, options);
  label.append(input); parent.append(label);
  return input;
}
function button(text, action) {
  const b = node('button', text); b.type = 'button'; b.addEventListener('click', action); return b;
}
function remoteEditor(parent, name = '', url = '') {
  const row = node('div', '', 'remote-row');
  field(row, 'Remote-Name', name, 'remote-name', {required: true, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,39}'});
  field(row, 'Git-URL (HTTPS/SSH)', url, 'remote-url', {required: true, type: 'url'});
  row.append(button('Remote entfernen', () => row.remove()));
  parent.append(row);
}
function repoEditor(repo = {name: '', enabled: true, remotes: {github: '', gitlab: ''}, webhook_secret_env: 'GITMESH_WEBHOOK_SECRET'}) {
  const card = node('fieldset', '', 'repo-editor');
  card.append(node('legend', 'Repository-Netz'));
  field(card, 'Netzname', repo.name, 'repo-name', {required: true, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,79}'});
  const active = field(card, 'Aktiv', '', 'repo-enabled', {type: 'checkbox'}); active.checked = repo.enabled !== false;
  field(card, 'Webhook-Secret: Umgebungsvariable', repo.webhook_secret_env || '', 'repo-secret', {placeholder: 'GITMESH_WEBHOOK_SECRET'});
  const remotes = node('div', '', 'remote-list');
  for (const [name, url] of Object.entries(repo.remotes)) remoteEditor(remotes, name, url);
  card.append(remotes, button('Remote hinzufügen', () => remoteEditor(remotes)), button('Netz entfernen', () => card.remove()));
  el('editors').append(card);
}
async function loadSettings() {
  const config = await request('/api/settings');
  el('interval').value = config.interval || 300;
  el('editors').replaceChildren();
  config.repositories.forEach(repoEditor);
  el('settings').hidden = false;
}
function collectSettings() {
  return {interval: Number(el('interval').value), repositories: [...el('editors').children].map(card => {
    const remotes = Object.create(null);
    for (const row of card.querySelectorAll('.remote-row')) {
      const name = row.querySelector('.remote-name').value.trim();
      if (Object.hasOwn(remotes, name)) throw new Error('Remote-Namen müssen pro Netz eindeutig sein.');
      remotes[name] = row.querySelector('.remote-url').value.trim();
    }
    return {name: card.querySelector('.repo-name').value.trim(), enabled: card.querySelector('.repo-enabled').checked,
      webhook_secret_env: card.querySelector('.repo-secret').value.trim(), remotes};
  })};
}
async function refresh() {
  if (!token) return;
  try {
    const d = await request('/api/status');
    el('controls').hidden = false;
    el('sync').disabled = d.running;
    el('repos').replaceChildren(...d.repositories.map(r => {
      const c = node('article'); c.append(node('h3', r.name), node('p', r.remotes.join(' ↔ ')), node('small', r.enabled ? 'Aktiv' : 'Deaktiviert')); return c;
    }));
    el('history').replaceChildren(...d.history.map(r => {
      const c = node('article', '', r.status); c.append(node('strong', r.repository + ' · ' + r.status), node('p', new Date(r.time * 1000).toLocaleString()), node('pre', JSON.stringify(r.details, null, 2))); return c;
    }));
  } catch (e) { el('message').textContent = e.message; }
}
el('login').addEventListener('submit', async e => {
  e.preventDefault(); token = el('token').value; el('token').value = '';
  try { await loadSettings(); await refresh(); el('message').textContent = 'Verbunden. Einstellungen können bearbeitet werden.'; }
  catch (err) { token = ''; el('message').textContent = err.message; }
});
el('logout').addEventListener('click', () => {
  token = ''; el('settings').hidden = true; el('controls').hidden = true;
  for (const id of ['editors', 'repos', 'history']) el(id).replaceChildren();
  el('message').textContent = 'Abgemeldet.';
});
el('sync').addEventListener('click', async () => {
  try { await request('/api/sync', 'POST'); el('message').textContent = 'Synchronisierung angefordert.'; }
  catch (e) { el('message').textContent = e.message; }
});
el('add-repo').addEventListener('click', () => repoEditor());
el('reload').addEventListener('click', async () => {
  try { await loadSettings(); el('message').textContent = 'Gespeicherten Stand geladen.'; } catch (e) { el('message').textContent = e.message; }
});
el('settings-form').addEventListener('submit', async e => {
  e.preventDefault();
  try { await request('/api/settings', 'POST', collectSettings()); el('message').textContent = 'Einstellungen gespeichert.'; await refresh(); }
  catch (err) { el('message').textContent = err.message; }
});
setInterval(refresh, 10000);
