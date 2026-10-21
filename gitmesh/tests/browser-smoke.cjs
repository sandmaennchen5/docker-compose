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
    await page.locator('#provider-name').fill('Third');
    await page.locator('#provider-kind').selectOption('forgejo');
    await page.locator('#provider-url').fill('http://127.0.0.1:34051');
    await page.locator('#provider-http').check();
    const created = page.waitForResponse(r => r.url().endsWith('/api/providers') && r.request().method() === 'POST');
    await page.locator('#provider-form button').first().click();
    assert.equal((await created).status(), 200);
    await page.waitForFunction(() => document.getElementById('provider-name').value === '');
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
    assert.equal(await profile.locator('.repo-enabled').isChecked(), false);
    await profile.locator('.repo-branches').fill('main, release/*');
    await profile.locator('.repo-interval').fill('0');
    await profile.locator('.repo-tags').uncheck();
    assert.equal(await profile.getByText('Was synchronisieren?', {exact: true}).count(), 1);
    await profile.locator('.repo-sync_branches').uncheck();
    for (const key of ['issues', 'comments', 'labels', 'milestones', 'pull_requests', 'wiki', 'lfs', 'repository_details']) await profile.locator('.repo-' + key).check();
    assert.equal(await profile.locator('.repo-create').isChecked(), false);
    await profile.locator('.repo-create').check();
    assert.equal(await profile.locator('.repo-create-private').isChecked(), true);
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await page.getByRole('status').filter({hasText: 'Einstellungen gespeichert.'}).waitFor();
    const settings = await api('/api/settings');
    assert.deepEqual(settings.repositories[0].branches, ['main', 'release/*']);
    assert.equal(settings.repositories[0].interval, 0);
    assert.equal(settings.repositories[0].sync_branches, false);
    for (const key of ['issues', 'comments', 'labels', 'milestones', 'pull_requests', 'wiki', 'lfs', 'repository_details']) assert.equal(settings.repositories[0][key], true);
    assert.equal(settings.repositories[0].create_missing, true);
    assert.equal(settings.repositories[0].create_private, true);
    assert.equal(settings.repositories[0].remote_providers.target, target.id);
    assert.equal(settings.repositories[0].remotes.target, 'https://target.example.invalid/mirror/project.git');
    // Open logs must survive the real five-second poll and changed job details.
    let logVersion = 1, statusSequence = 0;
    await page.route('**/api/status', async route => {
      const response = await route.fetch(), data = await response.json();
      data.testSequence = ++statusSequence;
      data.jobs.unshift({id: 900001, profile: settings.repositories[0].name, status: 'success', requested: 1, origin: 'fixture', attempts: 1, details: {message: 'Log version ' + logVersion}});
      await route.fulfill({response, json: data});
    });
    await page.evaluate(() => refresh());
    await page.locator('[data-page=jobs]').click();
    const log = page.locator('#jobs article[data-job-id="900001"] details');
    await log.locator('summary').click();
    assert.equal(await log.evaluate(node => node.open), true);
    const previousLog = await log.elementHandle();
    const previousSequence = await page.evaluate(() => latest.testSequence);
    await page.waitForFunction(sequence => latest.testSequence > sequence, previousSequence);
    assert.equal(await log.evaluate(node => node.open), true, 'Automatic refresh must keep logs open');
    assert.equal(await log.evaluate((node, previous) => node === previous, previousLog), true, 'Unchanged logs must retain their DOM node');
    logVersion = 2; await page.evaluate(() => refresh());
    await log.locator('pre').filter({hasText: 'Log version 2'}).waitFor();
    assert.equal(await log.evaluate(node => node.open), true, 'Changed details must also keep logs open');
    await page.locator('#job-filter').selectOption('error');
    await page.locator('#job-filter').selectOption('');
    assert.equal(await log.evaluate(node => node.open), true, 'Filtering must preserve expanded state');
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
    await page.locator('#shared-release-max').fill('64');
    await page.locator('#shared-form button').first().click();
    await page.locator('#shared-list h2').filter({hasText: /^shared-test$/}).waitFor();
    await page.locator('[data-page=profiles]').click();
    await profile.locator('.repo-shared').selectOption('shared-test');
    assert.equal(await profile.locator('.repo-branches').isDisabled(), true);
    assert.equal(await profile.locator('.repo-releases').isChecked(), true);
    assert.equal(await profile.locator('.repo-releases').isDisabled(), true);
    assert.equal(await profile.locator('.repo-release-max').inputValue(), '64');
    const sharedSaved = page.waitForResponse(r => r.url().endsWith('/api/settings') && r.request().method() === 'POST');
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    assert.equal((await sharedSaved).status(), 200);
    assert.equal((await api('/api/settings')).repositories[0].profile, 'shared-test');
    assert.equal((await api('/api/settings')).profiles['shared-test'].releases, true);
    await page.locator('[data-page=providers]').click();
    await page.locator('#transport-provider').selectOption(target.id);
    await page.locator('#transport-load').click();
    await page.locator('#transport-info').filter({hasText: 'Kein gespeicherter Schluessel'}).waitFor();
    await page.locator('#transport-protocol').selectOption('https');
    await page.locator('#transport-form button').last().click();
    await page.getByRole('status').filter({hasText: 'Transport gespeichert.'}).waitFor();
    assert.equal((await api('/api/transport?id=' + target.id)).protocol, 'https');
    await page.locator('[data-page=users]').click();
    for (const [name, role] of [['test-admin', 'admin'], ['test-reader', 'readonly']]) {
      await page.locator('#user-name').fill(name);
      await page.locator('#user-role').selectOption(role);
      await page.locator('#user-password').fill('isolated-browser-password');
      const saved = page.waitForResponse(r => r.url().endsWith('/api/users') && r.request().method() === 'POST');
      await page.locator('#user-form button').click(); assert.equal((await saved).status(), 200);
      await page.waitForFunction(() => document.getElementById('user-name').value === '');
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
