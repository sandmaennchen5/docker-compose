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
      GITMESH_HOST: '127.0.0.1', PYTHONDONTWRITEBYTECODE: '1'},
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
    await page.locator('#provider-url').fill('https://third.example.invalid');
    const created = page.waitForResponse(r => r.url().endsWith('/api/providers') && r.request().method() === 'POST');
    await page.locator('#provider-form button').first().click();
    assert.equal((await created).status(), 200);
    await page.waitForFunction(() => document.getElementById('provider-name').value === '');
    const afterEdit = (await api('/api/providers')).providers;
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
    await page.getByRole('button', {name: 'Einstellungen speichern', exact: true}).click();
    await page.getByRole('status').filter({hasText: 'Einstellungen gespeichert.'}).waitFor();
    const settings = await api('/api/settings');
    assert.deepEqual(settings.repositories[0].branches, ['main', 'release/*']);
    assert.equal(settings.repositories[0].interval, 0);
    assert.equal(settings.repositories[0].remote_providers.target, target.id);
    assert.equal(settings.repositories[0].remotes.target, 'https://target.example.invalid/mirror/project.git');
    await page.locator('[data-page=worker]').click();
    await page.locator('[data-worker=pause]').click();
    await page.locator('#worker-status h2').filter({hasText: 'paused'}).waitFor();
    await page.locator('#parallelism').fill('3');
    const workerSaved = page.waitForResponse(r => r.url().endsWith('/api/settings') && r.request().method() === 'POST');
    await page.locator('#worker-form button').click();
    assert.equal((await workerSaved).status(), 200);
    assert.equal((await api('/api/settings')).worker.parallelism, 3);
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
    await page.locator('[data-page=providers]').click();
    assert.equal(await page.locator('#provider-form').isVisible(), false);
    await page.locator('[data-page=profiles]').click();
    assert.equal(await page.locator('#settings-form').isVisible(), false);
    await page.locator('#logout').click();
    await page.locator('#auth').waitFor({state: 'visible'});
    await page.locator('#username').fill('test-admin');
    await page.locator('#password').fill('isolated-browser-password');
    await page.locator('#login button').click();
    await page.locator('#workspace').waitFor({state: 'visible'});
    assert.equal(await page.locator('[data-page=users]').isVisible(), true);
    await page.locator('[data-page=profiles]').click();
    assert.equal(await profile.locator('.repo-branches').inputValue(), 'main, release/*');
    if (process.env.GITMESH_SCREENSHOT) await page.screenshot({path: process.env.GITMESH_SCREENSHOT, fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile page must fit viewport');
    assert.deepEqual(errors, [], 'Browser must have no uncaught JavaScript errors');
    console.log('Browser smoke: provider CRUD, discovery drafts, profile persistence, worker controls, users, roles, mobile layout: OK');
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
