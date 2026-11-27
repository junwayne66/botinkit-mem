const { readFileSync } = require('node:fs');

async function run() {
  const { chromium } = require(process.env.BMEM_V2_PLAYWRIGHT_MODULE ?? 'playwright');
  const fixture = JSON.parse(readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ timezoneId: 'UTC' });
    const validAt = new Date(fixture.valid_at).toISOString();
    page.setDefaultTimeout(5000);
    await page.goto(fixture.url);
    await page.locator('nav button[data-view="statements"]').click();
    await page.locator('#library-scope').selectOption('project');
    await page.locator('#library-project').fill(fixture.project);
    await page.locator('#library-valid-at').fill(validAt.slice(0, 19));
    const response = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/v2/statements' && url.searchParams.get('project') === fixture.project
        && url.searchParams.get('valid_at') === validAt;
    });
    await page.locator('#library-apply').click();
    const status = (await response).status();
    if (status !== 200) throw new Error('Statement preview HTTP ' + status);
    const fact = page.locator('#stmt-list .library-fact').filter({
      has: page.locator('.fact-key', { hasText: 'code_example.language' }),
    });
    const language = await fact.locator('.library-fact-value strong').innerText();
    await fact.locator('summary').click();
    const revision = fact.locator('[data-field="revision-id"]');
    await revision.waitFor({ state: 'visible' });
    console.log(JSON.stringify({
      language: language.toLowerCase(), revision_id: await revision.innerText(),
      filter: { project: fixture.project, valid_at: fixture.valid_at }, status,
    }));
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  const detail = error.code === 'MODULE_NOT_FOUND' ? 'Playwright module unavailable' : 'Chrome statement observation failed';
  console.log(JSON.stringify({ observation_error: detail }));
  process.exitCode = 1;
});
