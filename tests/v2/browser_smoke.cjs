// Run with Node, Playwright, Chrome, and BMEM_V2_TEST_BIN set to the native binary.
const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const { once } = require('node:events');
const { mkdtempSync, readFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');

const { chromium } = require(process.env.BMEM_V2_PLAYWRIGHT_MODULE ?? 'playwright');
assert(process.env.BMEM_V2_TEST_BIN, 'Set BMEM_V2_TEST_BIN to the compiled executable');
const executable = path.resolve(process.env.BMEM_V2_TEST_BIN);
const workspace = mkdtempSync(path.join(tmpdir(), 'bmem-v2-browser-'));
const env = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => !key.startsWith('BMEM_')),
);
env.BMEM_LOCATOR_DIR = path.join(workspace, 'locator');
env.BMEM_DEFAULT_HOME = path.join(workspace, 'default vault');
const integrations = require('./browser_integrations.cjs');
if (process.platform !== 'win32') integrations.integrationEnvironment(workspace, env);
const fixture = JSON.parse(readFileSync(path.join(__dirname, 'data', 'declarations.json'), 'utf-8'));

function cli(...args) {
  const stdout = execFileSync(executable, ['--json', ...args], {
    cwd: workspace,
    env,
    encoding: 'utf-8',
    timeout: 10000,
  });
  return JSON.parse(stdout);
}

async function run() {
  let daemon;
  let browser;
  let daemonOutput = '';
  try {
    cli('--home', path.join(workspace, 'isolated vault'), 'init', '--non-interactive');
    daemon = spawn(executable, ['service', 'run'], {
      cwd: workspace,
      env,
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    daemon.stderr.on('data', (data) => { daemonOutput += data; });
    for (let remaining = 100; remaining > 0; remaining -= 1) {
      if (cli('status').state === 'running') break;
      if (daemon.exitCode !== null || remaining === 1) {
        throw new Error('Loopback service failed to start: ' + daemonOutput);
      }
      await delay(50);
    }

    const { url } = cli('web', '--no-open');
    const origin = new URL(url).origin;
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const page = await browser.newPage({ viewport: { width: 1210, height: 850 } });
    page.setDefaultTimeout(4000);
    const errors = [];
    const external = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) && url.origin !== origin) {
        external.push(url.origin);
      }
    });

    await page.goto(url);
    assert.equal(new URL(page.url()).origin, origin);
    assert.equal(new URL(page.url()).search, '');
    await page.locator('main').waitFor();
    await require('./browser_dashboard.cjs').verifyDashboard(page, cli);
    await require('./browser_intake.cjs').verifyIntake(page, cli);
    await verifyWorkbench(page);
    await require('./browser_search.cjs').verifySearch(page, cli);
    await verifyConfigFailures(page);
    await require('./browser_settings.cjs').verifyLiveSettings(page, cli);

    if (process.platform !== 'win32') {
      await integrations.verifyIntegrations(page, cli, workspace, executable, env);
      await require('./browser_data_console.cjs').verifyDataConsole(page, cli, env.BMEM_CONFIG_ROOT);
    }

    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    console.log('Browser verified settings and declarations against the native CLI.');
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      if (daemon && daemon.exitCode === null && daemon.signalCode === null) {
        const exited = once(daemon, 'exit');
        daemon.kill();
        const escalation = setTimeout(() => daemon.kill('SIGKILL'), 3000);
        try {
          await exited;
        } finally {
          clearTimeout(escalation);
        }
      }
      rmSync(workspace, { recursive: true, force: true });
    }
  }
}

async function verifyWorkbench(page) {
  const showSettings = () => goToSettings(page);
  const profile = page.locator('#cfg-profile');
  const save = page.locator('#profile-form button[type="submit"]');
  await showSettings();
  await page.waitForFunction(() => document.querySelector('#cfg-profile').value === 'balanced');
  const revision = cli('config', 'get').config_revision;
  await profile.selectOption('data');
  cli('config', 'set', 'profile', 'high', '--expected-revision', String(revision));

  const configResponse = () => page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/v2/config' && response.request().method() === 'PATCH'
  ));
  const stale = configResponse();
  await save.click();
  assert.equal((await stale).status(), 409);
  assert.equal(await profile.inputValue(), 'data');
  assert.equal(cli('config', 'get').profile, 'high');

  await page.reload();
  await showSettings();
  await page.waitForFunction(() => document.querySelector('#cfg-profile').value === 'high');
  await profile.selectOption('data');
  const configSaved = configResponse();
  await save.click();
  assert.equal((await configSaved).status(), 200);
  assert.equal(cli('config', 'get').profile, 'data');

  cli('declare', fixture.initial, '--scope', 'global', '--share', 'authorized_assistants');
  await page.locator('nav button[data-view="statements"]').click();
  await page.locator('button[data-action="reload-statements"]').click();
  await page.locator('#stmt-list').getByText(fixture.language_key, { exact: true }).waitFor();
  const declaration = page.locator('#append-form');
  await declaration.locator('textarea').fill(fixture.correction);
  await declaration.locator('input[value="authorized_assistants"]').check();
  const declarationSaved = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/v2/statements' && response.request().method() === 'POST'
  ));
  await declaration.locator('button[type="submit"]').click();
  assert.equal((await declarationSaved).status(), 201);
  const current = cli('statement', 'list').statements;
  assert.equal(current.find((entry) => entry.key === fixture.opening_key).value, fixture.corrected_opening);
  assert.equal(current.find((entry) => entry.key === fixture.language_key).value, fixture.language);

  await page.setViewportSize({ width: 390, height: 844 });
  await showSettings();
  await profile.waitFor();
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
  }));
  assert.ok(dimensions.document <= dimensions.viewport, 'Settings does not fit the mobile viewport');
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function goToSettings(page) {
  const menu = page.getByRole('button', {name: '打开导航'});
  if (await menu.isVisible()) await menu.click();
  await page.locator('nav button[data-view="settings"]').click();
}

async function verifyConfigFailures(page) {
  await goToSettings(page);
  await page.locator('#cfg-rev').waitFor();
  const configResponse = () => page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/v2/config' && response.request().method() === 'PATCH'
  ));
  await page.locator('summary').getByText('新增模型', { exact: true }).click();
  const modelName = page.locator('#md-name');
  const endpoint = page.locator('#md-endpoint');
  const modelId = page.locator('#md-modelid');
  await modelName.fill('kept draft');
  await endpoint.fill('http://127.0.0.1:11434');
  await modelId.fill('synthetic-model');
  cli('config', 'set', 'profile', 'balanced');
  const submit = page.locator('#model-form button[type="submit"]');
  const stale = configResponse();
  await submit.click();
  assert.equal((await stale).status(), 409);
  assert.equal(await modelName.inputValue(), 'kept draft');
  assert.equal(await endpoint.inputValue(), 'http://127.0.0.1:11434');
  assert.equal(await modelId.inputValue(), 'synthetic-model');
  await page.locator('#cfg-banner button').click();
  await page.locator('#cfg-banner').waitFor({ state: 'hidden' });

  const intents = [];
  const failures = async (route) => {
    const request = route.request();
    if (request.method() !== 'PATCH') return route.continue();
    intents.push([request.headers()['idempotency-key'], request.postData()]);
    if (intents.length > 2) return route.continue();
    return route.fulfill({status: 503, json: {code: 'temporarily_unavailable', retryable: true}});
  };
  await page.route('**/v2/config', failures);
  const first = configResponse();
  await submit.click();
  assert.equal((await first).status(), 503);
  const retry = page.locator('#model-result .cfg-retry');
  await retry.waitFor();
  const second = configResponse();
  await retry.click();
  assert.equal((await second).status(), 503);
  await retry.waitFor();
  assert.equal(await modelName.inputValue(), 'kept draft');
  const third = configResponse();
  await retry.click();
  assert.equal((await third).status(), 200);
  await page.waitForFunction(() => document.querySelector('#md-name').value === '');
  assert.equal(intents.length, 3);
  assert.deepEqual(intents, [intents[0], intents[0], intents[0]]);
  assert.equal(cli('model', 'list').models.filter((x) => x.name === 'kept draft').length, 1);
  await page.unroute('**/v2/config', failures);

  const block = (route) => route.abort();
  await page.route('**/v2/config', block);
  await page.reload();
  await goToSettings(page);
  await page.locator('#profile-form button[type="submit"]').click();
  await page.locator('#profile-result').getByText('尚未读取配置基线', { exact: false }).waitFor();
  await page.unroute('**/v2/config', block);
  console.log('Browser retained a stale model draft, retried the same key/body twice, and safely reported missing settings baseline.');
}
