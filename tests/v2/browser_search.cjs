const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');

async function verifySearch(page, cli) {
  const fixture = JSON.parse(readFileSync(path.join(__dirname, 'data/declarations.json'), 'utf8'));
  const event = require('./data/search_event.json');
  const source = cli('connector', 'add', 'fixture', '--account', event.account);
  cli('connector', 'resume', source.source_id);
  const origin = new URL(page.url()).origin;
  const raw = event.page.events[0].text;
  const ingested = await page.request.post(origin + '/v2/events', {
    headers: { Origin: origin, 'Idempotency-Key': 'search-page' },
    data: {source_id: source.source_id, ...event.page},
  });
  assert.equal(ingested.status(), 201, await ingested.text());
  assert.equal(cli('search', 'lunarchecksum').results.length, 1);
  const expandCalls = [];
  const watch = req => {
    if (new URL(req.url()).pathname === '/v2/evidence/expand') expandCalls.push(req.postDataJSON());
  };
  page.on('request', watch);
  try {
    await page.locator('#global-search').focus();
    await page.keyboard.press('Control+k');
    const dialog = page.getByRole('dialog', { name: '搜索记忆' });
    const query = dialog.getByRole('searchbox');
    await dialog.waitFor();
    assert(await query.evaluate(el => el === document.activeElement));
    await query.fill('先写结论');
    await dialog.getByText(fixture.correction, { exact: true }).waitFor();
    await dialog.getByRole('button', { name: '语义', exact: true }).click();
    await dialog.getByRole('status').getByText(/已使用关键词/).waitFor();
    await dialog.getByText(fixture.correction, { exact: true }).waitFor();
    await dialog.getByRole('button', { name: '关键词', exact: true }).click();
    await query.fill('lunarchecksum');
    const read = dialog.getByRole('button', { name: '查看原文' });
    await read.waitFor();
    assert.equal(expandCalls.length, 0);
    await query.press('ArrowDown');
    assert(await read.evaluate(el => el === document.activeElement));
    await page.keyboard.press('Enter');
    await dialog.getByText(raw, { exact: true }).waitFor();
    assert.equal(expandCalls.length, 1);

    // Hold a real response while the search box is cleared, then deliver it after the newer input.
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const requestSeen = page.waitForRequest('**/v2/search');
    const delivered = page.waitForResponse('**/v2/search');
    await page.route('**/v2/search', async route => {
      const response = await route.fetch();
      await gate;
      await route.fulfill({ response });
    }, { times: 1 });
    try {
      await query.fill('report.opening');
      await requestSeen;
      await query.fill('');
    } finally { release(); }
    await (await delivered).finished();
    await query.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await dialog.locator('.ms-hit').count(), 0);
    assert.equal(await dialog.getByText(raw, { exact: true }).count(), 0);

    await query.fill('lunarchecksum');
    await read.click();
    await dialog.getByText(raw, { exact: true }).waitFor();
    assert.equal(expandCalls.length, 2);
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'detached' });
    assert.equal(await page.evaluate(() => document.activeElement.id), 'global-search');
    await page.locator('#global-search').click();
    assert.equal(await query.inputValue(), '');
    assert.equal(await dialog.locator('.ms-hit').count(), 0);
    await dialog.getByRole('button', { name: '关闭搜索' }).click();
    console.log('Search returned native facts, fell back to keywords and fetched original evidence only on demand.');
  } finally {
    page.off('request', watch);
  }
}

module.exports = { verifySearch };
