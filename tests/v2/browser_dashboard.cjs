const assert = require('node:assert/strict');
const fixture = require('./data/declarations.json');

// Called after opening a fresh authenticated vault, before global declarations are seeded.
async function verifyDashboard(page, cli) {
  const overview = page.locator('#view-overview');
  const preview = page.locator('#atlas-preview');
  const full = page.locator('#atlas-full');

  await page.goto(new URL('/#/overview', page.url()).href);
  await overview.getByRole('heading', { name: '记忆总览' }).waitFor();
  assert.match(await page.title(), /Botinkit Memory/);
  await page.getByRole('link', { name: 'Botinkit Memory', exact: true }).waitFor();
  await overview.locator('#metric-statements').getByText('0', { exact: true }).waitFor();
  await preview.getByText('示例星图', { exact: true }).waitFor();
  await preview.getByRole('button', { name: '隐藏示例' }).click();
  await preview.getByText('还没有可绘制的记忆').waitFor();
  await preview.getByRole('button', { name: '查看示例' }).click();
  await preview.getByText('示例星图', { exact: true }).waitFor();

  for (const days of [30, 7]) {
    const button = overview.getByRole('button', { name: days + ' 天', exact: true });
    const response = page.waitForResponse(r => {
      const url = new URL(r.url());
      return url.pathname === '/v2/overview' && Number(url.searchParams.get('days') || 7) === days;
    });
    await button.click();
    const returned = await response;
    assert.equal(returned.status(), 200);
    assert.equal((await returned.json()).activity.length, days);
    await page.waitForFunction(
      count => document.querySelectorAll('#activity-chart tbody tr').length === count, days,
    );
    assert.equal(await button.getAttribute('aria-pressed'), 'true');
  }

  await page.route('**/v2/graph?*', route => route.fulfill({
    status: 503, json: { code: 'temporarily_unavailable' },
  }), { times: 1 });
  await overview.getByRole('button', { name: '刷新总览' }).click();
  await preview.getByText(/星图读取失败/).waitFor();
  assert.equal(await preview.locator('canvas').count(), 0, 'A failed graph read cannot appear as sample data');

  const declaration = cli('declare', fixture.project_override,
    '--scope', 'project', '--project', fixture.project_id, '--share', 'owner');
  await overview.getByRole('button', { name: '刷新总览' }).click();
  await overview.locator('#metric-statements').getByText('1', { exact: true }).waitFor();
  await overview.locator('#recent-feed').getByText(fixture.project_override, { exact: true }).waitFor();
  await preview.getByText('本机星图', { exact: true }).waitFor();
  assert.equal(await preview.getByText('示例星图', { exact: true }).count(), 0);

  await overview.getByRole('link', { name: '探索星图' }).click();
  await page.waitForURL('**/#/graph');
  await full.getByText('本机星图', { exact: true }).waitFor();
  const canvas = full.getByRole('img', { name: /主题社区关系图/ });
  await canvas.focus();
  await page.keyboard.press('+');
  assert.equal(await full.locator('.atlas-meter').textContent(), '125%');
  await page.keyboard.press('0');
  assert.equal(await full.locator('.atlas-meter').textContent(), '100%');

  const play = full.getByRole('button', { name: '播放动画' });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await play.waitFor();
  assert.ok(await play.isDisabled(), 'Graph should honor reduced motion');
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await full.getByRole('button', { name: '暂停动画' }).click();
  assert.ok(await play.isEnabled(), 'A manually paused graph can resume');

  await full.getByLabel('筛选社区', { exact: true }).selectOption({ label: fixture.project_id });
  await full.getByLabel('查看节点', { exact: true }).selectOption(declaration.statement_id);
  const inspector = full.locator('.atlas-inspector');
  await inspector.getByText(fixture.project_override, { exact: true }).waitFor();
  await inspector.getByRole('button', { name: '查看声明历史' }).waitFor();
  await inspector.getByRole('combobox', { name: /相连节点/ }).selectOption({ index: 1 });
  await inspector.getByText(fixture.project_id, { exact: true }).waitFor();

  const desktop = page.viewportSize();
  await page.setViewportSize({ width: 375, height: 667 });
  await page.locator('.topbar').getByText('Botinkit Memory', { exact: true }).waitFor();
  await canvas.scrollIntoViewIfNeeded();
  const frame = await canvas.boundingBox();
  assert.ok(frame && frame.width > 160 && frame.x >= 0 && frame.x + frame.width <= 376,
    'The complete graph canvas fits the narrow viewport');
  await page.setViewportSize(desktop);

  await page.goBack();
  await page.waitForURL('**/#/overview');
  await overview.getByRole('heading', { name: '记忆总览' }).waitFor();
  await page.goForward();
  await page.waitForURL('**/#/graph');
  await full.getByText('本机星图', { exact: true }).waitFor();
  // A pending graph response must not remount the atlas after a quick route change.
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const holdGraph = async route => {await gate; await route.continue();};
  await page.route('**/v2/graph?*', holdGraph);
  const inflight = page.waitForRequest(r => new URL(r.url()).pathname === '/v2/graph');
  await page.locator('nav [data-view="overview"]').click();
  await inflight;
  await page.locator('nav [data-view="operations"]').click();
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/v2/graph');
  release();
  await (await response).finished();
  await page.unroute('**/v2/graph?*',holdGraph);
  assert.equal(await page.locator('.memory-atlas').count(),0);
  const archives = page.locator('.archive-tools');
  await archives.locator('summary').click();
  await archives.getByRole('button',{name:'创建备份'}).click();
  await archives.locator('output').getByText(/已创建备份/).waitFor();
  const backup = await archives.getByLabel('备份标识').inputValue();
  assert.equal(cli('restore','preview',backup).integrity,'verified');
  await archives.getByRole('button',{name:'检查备份'}).click();
  await archives.locator('output').getByText(/备份校验通过/).waitFor();
  console.log('Browser verified live dashboard metrics, graph navigation and accessible controls.');
}

module.exports = { verifyDashboard };
