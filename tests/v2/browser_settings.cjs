const assert = require('node:assert/strict');
const { createServer } = require('node:http');

async function verifyLiveSettings(page, cli) {
  await page.setViewportSize({ width: 1210, height: 850 });
  await page.locator('nav button[data-view="settings"]').click();
  await page.locator('#view-settings [data-action="reload-config"]').click();
  const snapshot = cli('config', 'get');
  const waitForProfile = value => page.waitForFunction(
    expected => document.querySelector('#cfg-profile').value === expected,
    value, { timeout: 12000 },
  );
  const saved = () => page.waitForResponse(response =>
    new URL(response.url()).pathname === '/v2/config' && response.request().method() === 'PATCH');
  const cliMode = snapshot.profile === 'data' ? 'balanced' : 'data';
  cli('config', 'set', 'profile', cliMode);
  await waitForProfile(cliMode);

  const draft = cliMode === 'balanced' ? 'data' : 'high';
  await page.locator('#cfg-profile').selectOption(draft);
  const vector = !snapshot.vector_search.enabled;
  cli('config', 'set', 'vector_search.enabled', String(vector));
  const reconcile = page.locator('#cfg-banner button');
  await reconcile.waitFor({ timeout: 12000 });
  assert.equal(await page.inputValue('#cfg-profile'), draft);
  await reconcile.click();
  await page.locator('#cfg-banner').waitFor({ state: 'hidden' });
  assert.equal(await page.inputValue('#cfg-profile'), draft);
  assert.equal(await page.locator('#cfg-vector').isChecked(), vector);
  const profileSaved = saved();
  await page.locator('#profile-form [type="submit"]').click();
  assert.equal((await profileSaved).status(), 200);
  const joined = cli('config', 'get');
  assert.equal(joined.profile, draft);
  assert.equal(joined.vector_search.enabled, vector);

  const name = 'settings-live-test';
  const endpoint = 'http://127.0.0.1:11434';
  cli('model', 'add', name, '--protocol', 'ollama', '--model-id', 'synthetic', '--endpoint', endpoint);
  const row = page.locator('#models-list .card').filter({ hasText: name });
  await row.waitFor({ timeout: 12000 });
  await row.getByRole('button', { name: '编辑', exact: true }).click();
  await page.locator('#md-endpoint').fill(endpoint + '/v1');
  const modelSaved = saved();
  await page.locator('#model-form [type="submit"]').click();
  assert.equal((await modelSaved).status(), 200);
  assert.equal(cli('model', 'list').models.find(model => model.name === name).endpoint, endpoint + '/v1');
  cli('model', 'configure', name, '--model-id', 'synthetic-updated');
  await row.getByText('synthetic-updated', { exact: true }).waitFor({ timeout: 12000 });
  // 回归：CLI 改向量开关后，键控增量刷新须原位更新嵌入测试的禁用态与 title，
  // 模型行本身未变也不能停留在旧门禁（无需整页重载）；来回翻转并还原现场。
  const waitProbeGate = (disabled, title) => page.waitForFunction(({ modelName, disabled, title }) => {
    const card = [...document.querySelectorAll('#models-list .card')]
      .find(node => node.querySelector('.card__head strong')?.textContent === modelName);
    const btn = card?.querySelector('[data-probe="embed"]');
    return !!btn && btn.disabled === disabled && btn.getAttribute('title') === title;
  }, { modelName: name, disabled, title }, { timeout: 12000 });
  const vectorOn = cli('config', 'get').vector_search.enabled;
  await waitProbeGate(!vectorOn, vectorOn ? null : '先开启向量相似检索');
  cli('config', 'set', 'vector_search.enabled', String(!vectorOn));
  await waitProbeGate(vectorOn, vectorOn ? '先开启向量相似检索' : null);
  cli('config', 'set', 'vector_search.enabled', String(vectorOn));
  await waitProbeGate(!vectorOn, vectorOn ? null : '先开启向量相似检索');
  if (!await page.locator('#model-editor').evaluate(editor => editor.open)) {
    await page.locator('#model-editor summary').click();
  }
  const apiKey = page.locator('#md-api-key');
  const modelId = 'web-configured-model';
  const key = 'fixture-' + require('node:crypto').randomUUID();
  await page.locator('#md-protocol').selectOption('ollama');
  await page.locator('#md-endpoint').fill(endpoint);
  await page.locator('#md-modelid').fill(modelId);
  assert.equal(await apiKey.getAttribute('type'), 'password');
  await apiKey.fill(key);
  const keySaved = saved();
  await page.locator('#model-submit').click();
  const keyResponse = await keySaved;
  assert.equal(keyResponse.status(), 200);
  assert(!((await keyResponse.text()).includes(key)), 'Save must not return API_KEY');
  const added = page.locator('#models-list .card').filter({ hasText: modelId });
  await added.getByText('API_KEY 已配置').waitFor();
  assert.equal(await apiKey.inputValue(), '');
  await added.getByRole('button', { name: '编辑', exact: true }).click();
  assert.equal(await apiKey.inputValue(), '');
  assert((await apiKey.getAttribute('aria-describedby')).includes('md-key-hint'));
  await page.locator('#md-clear-key').check();
  const cleared = saved();
  await page.locator('#model-submit').click();
  assert.equal((await cleared).status(), 200);
  await added.getByText('API_KEY 已配置').waitFor({ state: 'hidden' });
  const listed = cli('model', 'list').models.find(item => item.name === modelId);
  assert.equal(listed.credential_configured, false);
  assert(!JSON.stringify(listed).includes(key), 'CLI must not return API_KEY');
  await verifyOllamaDiscovery(page, cli);
  console.log('Open settings followed native CLI writes and preserved independent drafts across revisions.');
}

async function verifyOllamaDiscovery(page, cli) {
  let haveEmbedding = false;
  const server = createServer(async (request, response) => {
    const names = haveEmbedding ? ['chat-fixture:latest', 'bge-m3:latest'] : ['chat-fixture:latest'];
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/api/tags') {
      response.end(JSON.stringify({ models: names.map(model => ({ model })) }));
    } else if (request.url === '/api/show') {
      let body = '';
      for await (const chunk of request) body += chunk;
      const { model } = JSON.parse(body);
      response.end(JSON.stringify({ capabilities: [model === 'bge-m3:latest' ? 'embedding' : 'completion'] }));
    } else {
      response.writeHead(404).end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  try {
    cli('config', 'set', 'vector_search.enabled', 'true');
    await page.locator('[data-action="reload-config"]').click();
    await page.locator('#cfg-vector').waitFor();
    await page.waitForFunction(() => document.querySelector('#cfg-vector').checked);
    if (!await page.locator('#model-editor').evaluate(editor => editor.open)) {
      await page.locator('#model-editor summary').click();
    }
    const endpoint = 'http://127.0.0.1:' + server.address().port;
    await page.locator('#md-endpoint').fill(endpoint);
    await page.locator('#md-protocol').selectOption('ollama');
    const panel = page.locator('#md-ollama-discovery');
    await panel.getByText('本机尚未安装 Embedding 模型。', { exact: true }).waitFor();
    await panel.getByText('ollama pull bge-m3', { exact: true }).waitFor();
    assert.equal(await page.locator('#md-modelid').inputValue(), '');
    haveEmbedding = true;
    await panel.getByRole('button', { name: '重新检测' }).click();
    await page.waitForFunction(() => document.querySelector('#md-modelid').value === 'bge-m3:latest');
    await panel.getByText('ollama pull bge-m3', { exact: true }).waitFor({ state: 'hidden' });
    const options = await panel.locator('option').allTextContents();
    assert(options[1].includes('bge-m3:latest') && options[1].includes('Embedding'));
    await panel.locator('select').selectOption('chat-fixture:latest');
    assert.equal(await page.locator('#md-modelid').inputValue(), 'chat-fixture:latest');
    const origin = new URL(page.url()).origin;
    const denied = await page.request.post(origin + '/v2/models/ollama/discover', {
      headers: { Origin: origin },
      data: { endpoint: 'http://example.invalid:11434' }
    });
    assert.equal(denied.status(), 422);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}

module.exports = { verifyLiveSettings };
