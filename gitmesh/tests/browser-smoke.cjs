/* End-to-end smoke test with an isolated local backend; discovery response is a fixture. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {randomBytes} = require('node:crypto');
const {chromium} = require(process.env.GITMESH_PLAYWRIGHT || 'playwright');
const root = path.resolve(__dirname, '..');

async function freePort() {
  const server = net.createServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function main() {
  const data = await fs.mkdtemp(path.join(os.tmpdir(), 'gitmesh-browser-'));
  const port = await freePort(), base = 'http://127.0.0.1:' + port;
  const token = randomBytes(32).toString('hex');
  const child = spawn(process.env.GITMESH_PYTHON || 'python', [path.join(root, 'gitmesh.py')], {
    cwd: root, windowsHide: true,
    env: {...process.env, GITMESH_ADMIN_TOKEN: token, GITMESH_DATA: data,
      GITMESH_CONFIG: path.join(data, 'config.json'), GITMESH_PORT: String(port),
      GITMESH_HOST: '127.0.0.1', GITMESH_ROLE: 'web', PYTHONDONTWRITEBYTECODE: '1'},
    stdio: ['ignore', 'pipe', 'pipe']
  });
  let logs = '', browser;
  child.stdout.on('data', chunk => { logs += chunk; });
  child.stderr.on('data', chunk => { logs += chunk; });
  const api = async endpoint => {
    const response = await fetch(base + endpoint, {headers: {Authorization: 'Bearer ' + token}});
    assert.equal(response.status, 200, endpoint); return response.json();
  };
  try {
    for (let i = 0; i < 100; i++) {
      if (child.exitCode !== null) throw new Error('Backend exited: ' + logs);
      try { if ((await fetch(base + '/healthz')).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
      if (i === 99) throw new Error('Backend did not become ready');
    }
    browser = await chromium.launch({headless: true,
      ...(process.env.GITMESH_BROWSER_EXECUTABLE ? {executablePath: process.env.GITMESH_BROWSER_EXECUTABLE} : {})});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = []; page.on('pageerror', err => errors.push(err.message));
    await page.goto(base);
    await page.locator('details').filter({has: page.locator('#bootstrap')}).locator('summary').click();
    await page.locator('#token').fill(token);
    await page.locator('#bootstrap button').click();
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('[data-page=providers]').click();
    for (const name of ['Source', 'Target']) {
      await page.locator('#provider-name').fill(name);
      await page.locator('#provider-kind').selectOption('forgejo');
      await page.locator('#provider-url').fill('https://' + name.toLowerCase() + '.example.invalid');
      await page.locator('#provider-username').fill('test-user');
      await page.locator('#provider-token').fill('fake-token-for-isolated-tests');
      const saved = page.waitForResponse(r => r.url().endsWith('/api/providers') && r.request().method() === 'POST');
      await page.locator('#provider-form button').first().click();
      assert.equal((await saved).status(), 200);
      await page.waitForFunction(() => document.getElementById('provider-id').value === '' && document.getElementById('provider-name').value === '');
    await page.waitForFunction(() => activeSave === null);
    }
    const providers = (await api('/api/providers')).providers;
    assert.equal(providers.length, 2);
    const source = providers.find(p => p.name === 'Source'), target = providers.find(p => p.name === 'Target');
    assert.ok(!JSON.stringify(providers).includes('fake-token'));
    // Editing assigns the hidden ID. A subsequent create must not reuse it.
    const sourceCard = page.locator('#provider-list article').filter({has: page.getByRole('heading', {name: 'Source', exact: true})});
    await sourceCard.getByRole('button', {name: 'Bearbeiten', exact: true}).click();
    assert.equal(await page.locator('#provider-id').inputValue(), source.id);
    await page.locator('#provider-username').fill('updated-user');
    const edited = page.waitForResponse(r => r.url().endsWith('/api/providers') && r.request().method() === 'POST');
    await page.locator('#provider-form button').first().click();
    assert.equal((await edited).status(), 200);
    await page.waitForFunction(() => document.getElementById('provider-name').value === '');
    await page.waitForFunction(() => activeSave === null);
    await page.locator('#provider-name').fill('Third');
    await page.locator('#provider-kind').selectOption('forgejo');
    await page.locator('#provider-url').fill('http://127.0.0.1:34051');
    await page.locator('#provider-http').check();
    const created = page.waitForResponse(r => r.url().endsWith('/api/providers') && r.request().method() === 'POST');
    await page.locator('#provider-form button').first().click();
    assert.equal((await created).status(), 200);
    await page.waitForFunction(() => document.getElementById('provider-name').value === '');
    await page.waitForFunction(() => activeSave === null);
    const afterEdit = (await api('/api/providers')).providers;
    assert.equal(afterEdit.find(p => p.name === 'Third').allow_http, true);
    await page.locator('#provider-list article').filter({has: page.getByRole('heading', {name: 'Third', exact: true})}).getByRole('button', {name: 'Bearbeiten', exact: true}).click();
    assert.equal(await page.locator('#provider-http').isChecked(), true);
    await page.locator('#provider-new').click();
    assert.equal(await page.locator('#provider-http').isChecked(), false);
    assert.equal(afterEdit.length, 3, 'Adding after editing must create another provider');
    assert.equal(afterEdit.find(p => p.id === source.id).name, 'Source');
    assert.equal(afterEdit.find(p => p.id === source.id).username, 'updated-user');
    assert.equal(afterEdit.find(p => p.id === source.id).has_token, true);
    await page.locator('#provider-list article').filter({has: page.getByRole('heading', {name: 'Target', exact: true})}).getByRole('button', {name: 'Bearbeiten', exact: true}).click();
    await page.locator('#provider-new').click();
    assert.equal(await page.locator('#provider-id').inputValue(), '', 'New provider must clear the edit ID');

    // Authorship UI filters bot credentials to the same host and provider kind.
    const botResponse = await fetch(base + '/api/providers', {method: 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify({name: 'GitMesh-Bot', kind: source.kind, url: source.url, token: 'fake-bot-credential'})});
    assert.equal(botResponse.status, 200);
    const botProvider = (await botResponse.json()).id;
    const authorSettings = new Map();
    let authorPayload;
    await page.route('**/api/authorship*', async route => {
      const post = route.request().method() === 'POST';
      const payload = post ? route.request().postDataJSON() : null;
      const id = post ? payload.id : new URL(route.request().url()).searchParams.get('id');
      if (post) {
        authorPayload = payload;
        authorSettings.set(id, {id, enabled: payload.enabled, bot_provider: payload.enabled ? payload.bot_provider : '', personal_account: 'michael', bot_account: payload.enabled ? 'GitMesh-Bot' : ''});
      }
      await route.fulfill({json: authorSettings.get(id) || {id, enabled: false, bot_provider: '', personal_account: '', bot_account: ''}});
    });
    await page.evaluate(() => loadProviders());
    await page.locator('#authorship-provider').selectOption(source.id);
    await page.locator('#authorship-load').click();
    await page.waitForFunction(() => document.querySelectorAll('#authorship-bot option').length === 2);
    assert.equal(await page.locator('#authorship-bot option[value="' + target.id + '"]').count(), 0);
    await page.locator('#authorship-enabled').check();
    await page.locator('#authorship-bot').selectOption(botProvider);
    await page.locator('#authorship-form button').last().click();
    await page.getByRole('status').filter({hasText: 'eigene Beitraege als michael, fremde als GitMesh-Bot'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal(authorPayload.id, source.id);
    assert.equal(authorPayload.bot_provider, botProvider);
    assert.equal(authorPayload.enabled, true);
    await page.locator('#authorship-provider').selectOption(target.id);
    await page.locator('#authorship-load').click();
    await page.waitForFunction(() => document.querySelectorAll('#authorship-bot option').length === 1);
    await page.locator('#authorship-provider').selectOption(source.id);
    await page.locator('#authorship-load').click();
    await page.waitForFunction(() => document.getElementById('authorship-enabled').checked);
    assert.equal(await page.locator('#authorship-bot').inputValue(), botProvider);
    await page.unroute('**/api/authorship*');
    await page.route('**/api/discover?*', route => route.fulfill({json: {
      repositories: [{full_name: 'team/project', clone_url: 'https://source.example.invalid/team/project.git', private: true}],
      next_page: null, page: 1
    }}));
    await page.locator('[data-page=discovery]').click();
    await page.locator('#discovery-provider').selectOption(source.id);
    await page.locator('#discovery-scope').selectOption('org');
    await page.locator('#discovery-owner').fill('team');
    await page.locator('#discovery-form button').click();
    await page.locator('#discovery-results input').check();
    await page.locator('#discovery-target').selectOption(target.id);
    await page.locator('#discovery-namespace').fill('mirror');
    await page.locator('#discovery-import').click();
    await page.locator('#page-profiles').waitFor({state: 'visible'});
    const profile = page.locator('.repo-editor');
    assert.equal(await profile.count(), 1);
    assert.equal(await profile.evaluate(node => node.open), true, 'New discovery drafts open for editing');
    assert.equal(await profile.locator('.repo-enabled').isChecked(), false);
    await profile.getByRole('button', {name: 'Remote hinzufügen', exact: true}).click();
    const proposedRemote = profile.locator('.remote-row').last();
    await proposedRemote.locator('.remote-name').fill('extra');
    await proposedRemote.locator('.remote-provider').selectOption(target.id);
    assert.equal(await proposedRemote.locator('.remote-url').inputValue(), 'https://target.example.invalid/team/project.git');
    await proposedRemote.locator('.remote-provider').selectOption(source.id);
    assert.equal(await proposedRemote.locator('.remote-url').inputValue(), source.url + '/team/project.git');
    await proposedRemote.locator('.remote-url').fill('https://manual.example/own/repo.git');
    await proposedRemote.locator('.remote-provider').selectOption(target.id);
    assert.equal(await proposedRemote.locator('.remote-url').inputValue(), 'https://manual.example/own/repo.git', 'Manual URLs must remain intact');
    await proposedRemote.getByRole('button', {name: 'URL vom Provider uebernehmen', exact: true}).click();
    assert.equal(await proposedRemote.locator('.remote-url').inputValue(), target.url + '/own/repo.git');
    await proposedRemote.getByRole('button', {name: 'Remote entfernen', exact: true}).click();

    await profile.locator('.repo-branches').fill('main, release/*');
    await profile.locator('.repo-interval').fill('0');
    await profile.locator('.repo-tags').uncheck();
    assert.equal(await profile.getByText('Was synchronisieren?', {exact: true}).count(), 1);
    await profile.locator('.repo-sync_branches').uncheck();
    for (const key of ['issues', 'comments', 'labels', 'milestones', 'pull_requests', 'wiki', 'lfs', 'repository_details']) await profile.locator('.repo-' + key).check();
    assert.equal(await profile.locator('.repo-create').isChecked(), false);
    await profile.locator('.repo-create').check();
    assert.equal(await profile.locator('.repo-create-private').isChecked(), true);
    await profile.locator(':scope > summary').click();
    assert.equal(await profile.locator('.repo-editor-body').isVisible(), false);
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await page.getByRole('status').filter({hasText: 'Einstellungen gespeichert.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    const settings = await api('/api/settings');
    assert.deepEqual(settings.repositories[0].branches, ['main', 'release/*']);
    assert.equal(settings.repositories[0].interval, 0);
    assert.equal(settings.repositories[0].sync_branches, false);
    for (const key of ['issues', 'comments', 'labels', 'milestones', 'pull_requests', 'wiki', 'lfs', 'repository_details']) assert.equal(settings.repositories[0][key], true);
    assert.equal(settings.repositories[0].create_missing, true);
    assert.equal(settings.repositories[0].create_private, true);
    assert.equal(settings.repositories[0].remote_providers.target, target.id);
    assert.equal(settings.repositories[0].remotes.target, 'https://target.example.invalid/mirror/project.git');
    assert.equal(await profile.evaluate(node => node.open), false, 'Saving retains collapsed state and all field values');
    await profile.locator(':scope > summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await profile.evaluate(node => node.open), true, 'Keyboard expands the editor');
    // Unchecking Wiki must survive a blocked save, a real save and a reload.
    await profile.locator('.repo-wiki').uncheck();
    await page.getByRole('status').filter({hasText: 'Ungespeicherte Aenderungen.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    let releaseBlockedSave;
    const blockedSaveGate = new Promise(resolve => { releaseBlockedSave = resolve; });
    await page.route('**/api/settings', async route => {
      if (route.request().method() === 'POST') { await blockedSaveGate; return route.fulfill({status: 409, json: {error: 'Synchronisierung laeuft'}}); }
      return route.continue();
    });
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await page.locator('#save-progress[data-state="saving"]').waitFor();
    assert.equal(await page.locator('#settings-form button').filter({hasText: 'Wird gespeichert'}).isDisabled(), true);
    assert.equal(await page.locator('#logout').isDisabled(), true);
    await page.locator('[data-page=dashboard]').click();
    assert.equal(await page.locator('#page-profiles').isVisible(), true, 'Pending save must retain the source page');
    assert.equal(await page.locator('#page-dashboard').isVisible(), false);
    assert.equal(await page.evaluate(() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }), true);
    if (process.env.GITMESH_SAVING_SCREENSHOT) await page.screenshot({path: process.env.GITMESH_SAVING_SCREENSHOT});
    releaseBlockedSave();
    await page.getByRole('status').filter({hasText: 'Nicht gespeichert:'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    await page.locator('#save-progress[data-state="error"]').waitFor();
    assert.equal(await page.locator('#page-profiles').isVisible(), true, 'Failed save must cancel the requested navigation');
    assert.equal((await api('/api/settings')).repositories[0].wiki, true);
    assert.equal(await profile.locator('.repo-wiki').isChecked(), false, 'Blocked save must retain the draft');
    await page.unroute('**/api/settings');
    let releaseSuccessfulSave, savingPosts = 0;
    const successfulSaveGate = new Promise(resolve => { releaseSuccessfulSave = resolve; });
    await page.route('**/api/settings', async route => {
      if (route.request().method() === 'POST') { savingPosts++; await successfulSaveGate; }
      return route.continue();
    });
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await page.locator('#save-progress[data-state="saving"]').waitFor();
    await page.locator('[data-page=jobs]').click();
    assert.equal(await page.locator('#page-profiles').isVisible(), true);
    await page.evaluate(() => document.getElementById('settings-form').requestSubmit());
    releaseSuccessfulSave();
    await page.locator('#save-progress[data-state="saved"]').waitFor();
    await page.locator('#page-jobs').waitFor({state: 'visible'});
    assert.equal(savingPosts, 1, 'Repeated submit must not issue a second write');
    assert.equal((await api('/api/settings')).repositories[0].wiki, false);
    assert.equal(await page.evaluate(() => document.querySelector('#workspace .content').inert), false);
    await page.unroute('**/api/settings');
    await page.locator('[data-page=profiles]').click();
    await page.locator('#reload').click();
    await page.getByRole('status').filter({hasText: 'Gespeicherten Stand geladen.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal(await profile.locator('.repo-wiki').isChecked(), false);
    // A differing read-back must never produce a success message.
    await page.route('**/api/settings', async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch(), value = await response.json();
      value.repositories[0].wiki = true;
      await route.fulfill({response, json: value});
    });
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await page.getByRole('status').filter({hasText: 'Speicherung nicht bestaetigt:'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal(await profile.locator('.repo-wiki').isChecked(), false);
    await page.unroute('**/api/settings');
    let releaseVerification;
    const verificationGate = new Promise(resolve => { releaseVerification = resolve; });
    let verificationStarted;
    const verificationRequest = new Promise(resolve => { verificationStarted = resolve; });
    await page.route('**/api/settings', async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch(); verificationStarted(); await verificationGate;
      await route.fulfill({response});
    });
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await verificationRequest;
    await page.locator('[data-page=dashboard]').click();
    assert.equal(await page.locator('#page-profiles').isVisible(), true, 'Read-back verification is part of saving');
    assert.equal(await page.locator('#save-progress').getAttribute('data-state'), 'saving');
    releaseVerification();
    await page.locator('#page-dashboard').waitFor({state: 'visible'});
    await page.unroute('**/api/settings');
    await page.locator('[data-page=profiles]').click();
    // Open logs must survive the real five-second poll and changed job details.
    let logVersion = 1, statusSequence = 0, fixtureResolved = false, deletionResolved = false;
    await page.route('**/api/status', async route => {
      const response = await route.fetch(), data = await response.json();
      data.testSequence = ++statusSequence;
      data.resolved_conflicts = [...(fixtureResolved ? ['browser-label-conflict'] : []), ...(deletionResolved ? ['browser-deletion-conflict'] : [])];
      data.jobs.push({id: 900002, profile: settings.repositories[0].name, status: 'conflict', requested: 1, origin: 'fixture', attempts: 1, details: {metadata: {counts: {conflict: 1}, items: [{category: 'comments', status: 'conflict', kind: 'comment_deletion', name: 'Issue #15: Discussion', target: 'GitHub', reason: 'Kommentar geloescht; Entscheidung erforderlich.', resolution_id: 'browser-deletion-conflict', choices: ['delete_copies', 'keep_copies'], values: {GitHub: {status: 'Geloescht'}, Forgejo: {status: 'Kopie', body: 'Original discussion'}}}]}}});
      data.jobs.unshift({id: 900001, profile: settings.repositories[0].name, status: 'conflict', requested: 1, origin: 'fixture', attempts: 1, details: {message: 'Log version ' + logVersion, conflicts: [], metadata: {counts: {conflict: 1}, items: [{category: 'labels', status: 'conflict', target: 'Forgejo', item: '77', name: 'bug', reason: 'Gleichnamiges vorhandenes Label hat andere Werte.', values: {GitHub: {color: 'ff0000'}, Forgejo: {color: '000000'}}, resolution_id: 'browser-label-conflict', choices: ['adopt', 'source']}]}}});
      await route.fulfill({response, json: data});
    });
    await page.evaluate(() => refresh());
    await page.locator('[data-page=jobs]').click();
    const log = page.locator('#jobs article[data-job-id="900001"] details.job-raw');
    await log.locator('summary').click();
    assert.equal(await log.evaluate(node => node.open), true);
    const previousLog = await log.elementHandle();
    await page.locator('[data-page=dashboard]').click();
    const mapping = page.locator('#repos .profile-mapping').first();
    await mapping.locator('summary').click();
    const previousMapping = await mapping.elementHandle();
    assert.equal(await mapping.evaluate(node => node.open), true);
    const conflictValues = page.locator('#repos .conflict-values').first();
    await conflictValues.locator('summary').click();
    await page.locator('#repos .conflict-entry strong').filter({hasText: 'Labels: bug'}).waitFor();
    const previousSequence = await page.evaluate(() => latest.testSequence);
    await page.waitForFunction(sequence => latest.testSequence > sequence, previousSequence);
    assert.equal(await log.evaluate(node => node.open), true, 'Automatic refresh must keep logs open');
    assert.equal(await log.evaluate((node, previous) => node === previous, previousLog), true, 'Unchanged logs must retain their DOM node');
    assert.equal(await mapping.evaluate(node => node.open), true, 'Dashboard mapping must survive polling');
    assert.equal(await mapping.evaluate((node, old) => node === old, previousMapping), true, 'Unchanged dashboard cards retain DOM');
    assert.equal(await conflictValues.evaluate(node => node.open), true);
    logVersion = 2; await page.evaluate(() => refresh());
    assert.equal(await mapping.evaluate(node => node.open), true, 'Changed profile job must retain expanded mapping');
    assert.equal(await conflictValues.evaluate(node => node.open), true, 'Changed job keeps conflict comparison open');
    await page.locator('[data-page=jobs]').click();
    await log.locator('pre').filter({hasText: 'Log version 2'}).waitFor();
    assert.equal(await log.evaluate(node => node.open), true, 'Changed details must also keep logs open');
    await page.locator('#job-filter').selectOption('error');
    await page.locator('#job-filter').selectOption('');
    assert.equal(await log.evaluate(node => node.open), true, 'Filtering must preserve expanded state');
    let resolutionPayload;
    await page.route('**/api/conflicts/resolve', async route => {
      resolutionPayload = route.request().postDataJSON();
      if (resolutionPayload.conflict_id === 'browser-deletion-conflict') deletionResolved = true; else fixtureResolved = true;
      await route.fulfill({json: {status: 'success'}});
    });
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#jobs article[data-job-id="900001"]').getByRole('button', {name: 'Vorhandenes Label zuordnen', exact: true}).click();
    await page.locator('#jobs .resolved-conflict').filter({hasText: 'Aufgeloest'}).waitFor();
    assert.equal(resolutionPayload.choice, 'adopt');
    assert.equal(resolutionPayload.profile, settings.repositories[0].name);
    assert.equal(await page.locator('#jobs article[data-job-id="900001"]').getByRole('button', {name: 'Vorhandenes Label zuordnen', exact: true}).count(), 0);
    await page.waitForFunction(() => activeSave === null);
    const deletionCard = page.locator('#jobs article[data-job-id="900002"]');
    await deletionCard.getByRole('button', {name: 'Kopien behalten und Sync sperren', exact: true}).waitFor();
    await deletionCard.locator('details.conflict-values summary').click();
    await deletionCard.locator('pre').filter({hasText: 'Original discussion'}).first().waitFor();
    let deletePrompt = '';
    page.once('dialog', async dialog => { deletePrompt = dialog.message(); await dialog.accept(); });
    await deletionCard.getByRole('button', {name: 'Verbleibende Kopien loeschen', exact: true}).click();
    await deletionCard.locator('.resolved-conflict').waitFor();
    assert.equal(resolutionPayload.choice, 'delete_copies');
    assert.equal(resolutionPayload.conflict_id, 'browser-deletion-conflict');
    assert.match(deletePrompt, /endgueltig loeschen/);
    assert.match(deletePrompt, /Originalkommentare bleiben erhalten/);
    await page.waitForFunction(() => activeSave === null);
    await page.unroute('**/api/conflicts/resolve');
    await page.unroute('**/api/status');
    // Log actions use real backend records, including protected live jobs.
    const seedLogs = spawn(process.env.GITMESH_PYTHON || 'python', ['-c',
      "import sqlite3,sys,time,json; db=sqlite3.connect(sys.argv[1]); now=time.time(); rows=[('log-error','error'),('log-conflict','conflict'),('log-success','success'),('log-active','running'),('log-waiting','queued')]; db.executemany(\"INSERT INTO jobs(profile,origin,status,requested,started,finished,details) VALUES(?,'browser-fixture',?,?,?,?,'{\\\"message\\\":\\\"original diagnosis\\\"}')\",[(p,s,now,now,None if s in ('running','queued') else now) for p,s in rows]); db.commit(); db.close()",
      path.join(data, 'history.db')], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
    let seedErrors = ''; seedLogs.stderr.on('data', c => { seedErrors += c; });
    assert.equal((await once(seedLogs, 'exit'))[0], 0, seedErrors);
    await page.evaluate(() => refresh());
    const fixtureJobs = (await api('/api/jobs')).jobs;
    const failedLog = fixtureJobs.find(j => j.profile === 'log-error');
    const runningLog = fixtureJobs.find(j => j.profile === 'log-active');
    const failedCard = page.locator('#jobs article[data-job-id="' + failedLog.id + '"]');
    await failedCard.getByRole('button', {name: 'Anzeige zuruecksetzen', exact: true}).click();
    await failedCard.locator('.badge').filter({hasText: 'Zurueckgesetzt (error)'}).waitFor();
    assert.equal((await api('/api/jobs')).jobs.find(j => j.id === failedLog.id).status, 'error');
    await page.locator('#logs-reset').click();
    await page.getByRole('status').filter({hasText: '1 Fehler-/Konfliktanzeigen zurueckgesetzt.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    await page.locator('#job-filter').selectOption('reset');
    await page.waitForFunction(() => document.querySelectorAll('#jobs article').length === 2);
    assert.equal(await page.locator('#jobs article').count(), 2);
    page.once('dialog', dialog => dialog.accept());
    await failedCard.getByRole('button', {name: 'Protokoll loeschen', exact: true}).click();
    await failedCard.waitFor({state: 'detached'});
    assert.equal((await api('/api/jobs')).jobs.some(j => j.id === failedLog.id), false);
    await page.locator('#job-filter').selectOption('');
    assert.equal(await page.locator('#jobs article[data-job-id="' + runningLog.id + '"]').getByRole('button', {name: 'Protokoll loeschen'}).count(), 0);
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#logs-delete').click();
    await page.waitForFunction(() => latest.jobs.every(j => ['running', 'queued'].includes(j.status)));
    assert.ok((await api('/api/jobs')).jobs.some(j => j.id === runningLog.id));
    // Discovery shows saved directions and disallows another import of the same repository.
    await page.unroute('**/api/discover?*');
    let discoveredAsTarget = false, bidirectional = false;
    await page.route('**/api/discover?*', route => route.fulfill({json: {repositories: [{full_name: discoveredAsTarget ? 'mirror/project' : 'team/project',
      clone_url: discoveredAsTarget ? settings.repositories[0].remotes.target : settings.repositories[0].remotes.source,
      syncs: [{profile: settings.repositories[0].name, enabled: false, direction: bidirectional ? 'bidirectional' : 'one-way', source: 'source',
        remotes: settings.repositories[0].remotes, matched_remote: discoveredAsTarget ? 'target' : 'source'}]}]}}));
    await page.locator('[data-page=discovery]').click();
    await page.locator('#discovery-form button').click();
    await page.locator('.discovery-state').filter({hasText: 'Sync vorhanden'}).waitFor();
    assert.equal(await page.locator('#discovery-results input').isDisabled(), true);
    assert.ok((await page.locator('.mapping-summary').textContent()).includes('Deaktiviert · Quelle'));
    assert.ok((await page.locator('.mapping-summary').textContent()).includes(settings.repositories[0].remotes.target));
    discoveredAsTarget = true;
    await page.locator('#discovery-provider').selectOption(target.id);
    await page.locator('#discovery-form button').click();
    await page.locator('.mapping-summary strong').filter({hasText: 'Deaktiviert · Ziel'}).waitFor();
    bidirectional = true;
    await page.locator('#discovery-form button').click();
    await page.locator('.mapping-summary p').filter({hasText: 'Bidirektional:'}).waitFor();
    await page.locator('[data-page=worker]').click();
    await page.locator('[data-worker=pause]').click();
    await page.locator('#worker-status h2').filter({hasText: 'paused'}).waitFor();
    await page.locator('#parallelism').fill('3');
    const workerSaved = page.waitForResponse(r => r.url().endsWith('/api/settings') && r.request().method() === 'POST');
    await page.locator('#worker-form button').click();
    assert.equal((await workerSaved).status(), 200);
    assert.equal((await api('/api/settings')).worker.parallelism, 3);
    await page.locator('[data-page=tools]').click();
    assert.equal(await page.locator('#tool-profile').inputValue(), settings.repositories[0].name);
    await page.route('**/api/preview', route => route.fulfill({json: {status: 'success', plan: [{remote: 'target', ref: 'heads/main'}], conflicts: []}}));
    await page.locator('#tool-preview').click();
    await page.locator('#tool-output').filter({hasText: 'heads/main'}).waitFor();
    await page.route('**/api/compare', route => route.fulfill({json: {left_oid: 'a'.repeat(40), right_oid: 'b'.repeat(40), left_only: 1, right_only: 1, left_commits: [{oid: 'a'.repeat(40), subject: 'Left change', author: 'Test'}], right_commits: [{oid: 'b'.repeat(40), subject: 'Right change', author: 'Test'}], stat: 'content.txt', patch: '+new text'}}));
    await page.locator('#tool-left').fill('source'); await page.locator('#tool-right').fill('target');
    await page.locator('#tool-compare button').first().click();
    await page.locator('#tool-comparison').filter({hasText: 'Left change'}).waitFor();
    await page.locator('#public-url').fill('https://mesh.example.invalid');
    await page.locator('#hook-settings button').first().click();
    await page.getByRole('status').filter({hasText: 'Oeffentliche Adresse gespeichert.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal((await api('/api/settings')).public_url, 'https://mesh.example.invalid');
    await page.locator('[data-page=groups]').click();
    await page.locator('#group-name').fill('team');
    await page.locator('#group-parallelism').fill('1');
    await page.locator('#group-form button').click();
    await page.locator('#group-list h2').filter({hasText: /^team$/}).waitFor();
    assert.equal((await api('/api/settings')).worker_sets.team.parallelism, 1);
    await page.locator('#shared-name').fill('shared-test');
    await page.locator('#shared-direction').selectOption('one-way');
    await page.locator('#shared-source').fill('source');
    await page.locator('#shared-branches').fill('main, release/*');
    await page.locator('#shared-interval').fill('0');
    await page.locator('#shared-worker').fill('team');
    await page.locator('#shared-enabled').uncheck();
    await page.locator('#shared-releases').check();
    await page.locator('#shared-wiki').check();
    await page.locator('#shared-release-max').fill('64');
    await page.locator('#shared-form button').first().click();
    await page.locator('#shared-list h2').filter({hasText: /^shared-test$/}).waitFor();
    await page.locator('[data-page=profiles]').click();
    await profile.locator('.repo-shared').selectOption('shared-test');
    assert.equal(await profile.locator('.repo-branches').isDisabled(), true);
    assert.equal(await profile.locator('.repo-releases').isChecked(), true);
    assert.equal(await profile.locator('.repo-releases').isDisabled(), false);
    assert.equal(await profile.locator('.repo-wiki').isChecked(), true);
    assert.equal(await profile.locator('.repo-wiki').isDisabled(), false);
    await profile.locator('.repo-wiki').uncheck();
    assert.equal(await profile.locator('.repo-release-max').inputValue(), '64');
    const sharedSaved = page.waitForResponse(r => r.url().endsWith('/api/settings') && r.request().method() === 'POST');
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    assert.equal((await sharedSaved).status(), 200);
    await page.getByRole('status').filter({hasText: 'Einstellungen gespeichert.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal((await api('/api/settings')).repositories[0].sync_overrides.wiki, false);
    assert.equal((await api('/api/settings')).profiles['shared-test'].wiki, true);
    await page.locator('#reload').click();
    await page.getByRole('status').filter({hasText: 'Gespeicherten Stand geladen.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal(await profile.locator('.repo-wiki').isChecked(), false);
    assert.equal((await api('/api/status')).repositories[0].selected_components.includes('wiki'), false);
    assert.equal((await api('/api/settings')).repositories[0].profile, 'shared-test');
    assert.equal((await api('/api/settings')).profiles['shared-test'].releases, true);
    await page.locator('[data-page=providers]').click();
    await page.locator('#transport-provider').selectOption(target.id);
    await page.locator('#transport-load').click();
    await page.locator('#transport-info').filter({hasText: 'Kein gespeicherter Schluessel'}).waitFor();
    await page.locator('#transport-protocol').selectOption('https');
    await page.locator('#transport-form button').last().click();
    await page.getByRole('status').filter({hasText: 'Transport gespeichert.'}).waitFor();
    await page.waitForFunction(() => activeSave === null);
    assert.equal((await api('/api/transport?id=' + target.id)).protocol, 'https');
    await page.locator('[data-page=users]').click();
    for (const [name, role] of [['test-admin', 'admin'], ['test-reader', 'readonly']]) {
      await page.locator('#user-name').fill(name);
      await page.locator('#user-role').selectOption(role);
      await page.locator('#user-password').fill('isolated-browser-password');
      const saved = page.waitForResponse(r => r.url().endsWith('/api/users') && r.request().method() === 'POST');
      await page.locator('#user-form button').click(); assert.equal((await saved).status(), 200);
      await page.waitForFunction(() => document.getElementById('user-name').value === '');
    await page.waitForFunction(() => activeSave === null);
    }
    await page.locator('#logout').click();
    await page.locator('#auth').waitFor({state: 'visible'});
    await page.locator('#username').fill('test-reader');
    await page.locator('#password').fill('isolated-browser-password');
    await page.locator('#login button').click();
    await page.locator('#workspace').waitFor({state: 'visible'});
    assert.equal(await page.locator('[data-page=users]').isVisible(), false);
    assert.equal(await page.locator('[data-page=groups]').isVisible(), false);
    await page.locator('[data-page=providers]').click();
    assert.equal(await page.locator('#transport-form').isVisible(), false);
    assert.equal(await page.locator('#authorship-form').isVisible(), false);
    await page.locator('[data-page=providers]').click();
    assert.equal(await page.locator('#provider-form').isVisible(), false);
    await page.locator('[data-page=profiles]').click();
    assert.equal(await page.locator('#settings-form').isVisible(), false);
    await page.locator('[data-page=tools]').click();
    assert.equal(await page.locator('#tool-merge').isVisible(), false);
    assert.equal(await page.locator('#tool-create').isVisible(), false);
    await page.locator('#logout').click();
    await page.locator('#auth').waitFor({state: 'visible'});
    await page.locator('#username').fill('test-admin');
    await page.locator('#password').fill('isolated-browser-password');
    await page.locator('#login button').click();
    await page.locator('#workspace').waitFor({state: 'visible'});
    assert.equal(await page.locator('[data-page=users]').isVisible(), true);
    await page.locator('[data-page=profiles]').click();
    assert.equal(await profile.locator('.repo-branches').inputValue(), 'main, release/*');
    assert.equal(await profile.locator('.repo-create').isChecked(), true);
    assert.equal(await profile.locator('.repo-create-private').isChecked(), true);
    assert.equal(await profile.locator('.repo-wiki').isChecked(), false, 'Wiki override must persist across logout/login');
    assert.equal(await profile.evaluate(node => node.open), false, 'Saved profiles initially load collapsed');
    assert.equal(await page.locator('#profile-summary').isVisible(), false, 'Administrators see one compact list');
    await page.locator('[data-page=dashboard]').click();
    await page.locator('#repos').getByRole('button', {name: 'Bearbeiten', exact: true}).click();
    await page.locator('#page-profiles').waitFor({state: 'visible'});
    assert.equal(await profile.evaluate(node => node.open), true, 'Overview edit button opens the matching profile');
    await page.locator('#add-repo').click();
    const invalidDraft = page.locator('.repo-editor').last();
    assert.equal(await invalidDraft.evaluate(node => node.open), true);
    const blankRemote = invalidDraft.locator('.remote-row').first();
    await blankRemote.locator('.remote-provider').selectOption(source.id);
    assert.equal(await blankRemote.locator('.remote-url').inputValue(), source.url + '/');
    assert.equal(await blankRemote.locator('.remote-url').evaluate(input => input.validity.customError), true);
    await blankRemote.locator('.remote-url').fill(source.url + '/team/new.git');
    assert.equal(await blankRemote.locator('.remote-url').evaluate(input => input.validity.customError), false);

    await invalidDraft.locator(':scope > summary').click();
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    assert.equal(await invalidDraft.evaluate(node => node.open), true, 'Invalid collapsed fields must open for correction');
    assert.equal(await page.evaluate(() => activeSave === null), true);
    await invalidDraft.getByRole('button', {name: 'Profil entfernen', exact: true}).click();
    assert.equal(await profile.count(), 1);
    await profile.locator(':scope > summary').click();

    if (process.env.GITMESH_SCREENSHOT) await page.screenshot({path: process.env.GITMESH_SCREENSHOT, fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile page must fit viewport');
    assert.deepEqual(errors, [], 'Browser must have no uncaught JavaScript errors');
    console.log('Browser smoke: provider CRUD, discovery drafts, profile persistence, worker controls, preview, comparison, webhook settings, shared profiles, worker groups, SSH transport settings, users, roles, mobile layout: OK');
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) { const exit = once(child, 'exit'); child.kill(); await exit; }
    // Only remove the uniquely-created temporary directory after its resolved path is checked.
    const resolved = path.resolve(data), prefix = path.resolve(os.tmpdir()) + path.sep + 'gitmesh-browser-';
    if (!resolved.startsWith(prefix)) throw new Error('Unexpected temporary directory');
    await fs.rm(resolved, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
