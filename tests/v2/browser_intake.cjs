const assert = require('node:assert/strict');
const fixture = require('./data/source_page.json');

async function verifyIntake(page, cli) {
  const { source, page: batch } = fixture;
  const sources = page.locator('#sources-content');
  const imports = page.locator('#imports-content');
  const list = () => cli('connector', 'list').sources;
  await page.locator('nav button[data-view="sources"]').click();
  await sources.getByText('登记新来源', {exact:true}).click();
  await sources.getByLabel('来源类型').fill(source.type);
  await sources.getByLabel('来源账号').fill(source.account);
  await sources.getByRole('button', {name:'登记来源',exact:true}).click();
  const row = sources.getByRole('article').filter({hasText:source.account});
  await row.getByText('已暂停', {exact:true}).waitFor();
  const registered = list().find(s => s.type === source.type && s.account === source.account);
  assert.equal(registered.capture_enabled, false);
  cli('connector','resume',registered.source_id);
  await sources.getByRole('button',{name:'刷新来源'}).click();
  await row.getByText('采集中',{exact:true}).waitFor();
  await row.getByRole('button',{name:'暂停采集'}).click();
  await row.getByText('已暂停',{exact:true}).waitFor();
  assert.equal(list().find(s=>s.source_id === registered.source_id).capture_enabled,false);
  await row.getByRole('button',{name:'开启采集'}).click();
  await row.getByText('采集中',{exact:true}).waitFor();
  await row.getByText('用途授权',{exact:true}).click();
  for (const [purpose,title,reason] of [
    ['extract','自动提炼','请先在模型设置中测试并绑定提炼模型，可使用服务器 API 或本地 Ollama。'],
    ['embed','向量嵌入','请先开启向量检索，再测试并绑定本机嵌入模型。'],
  ]) {
    const grant = row.getByRole('region',{name:title});
    const previewed = page.waitForResponse(r => new URL(r.url()).pathname === '/v2/operations/preview');
    await grant.getByRole('button',{name:'授权' + title}).click();
    const preview = await (await previewed).json();
    assert.deepEqual(preview.scope, {kind:'change_grant',source_id:registered.source_id,purpose,allow:true});
    assert.equal(preview.effective,false);
    assert.equal(list().find(s=>s.source_id === registered.source_id)[purpose + '_enabled'],false);
    const submitted = page.waitForResponse(r => new URL(r.url()).pathname === '/v2/operations' && r.request().method() === 'POST');
    await grant.getByRole('button',{name:'确认授权'}).click();
    const rejected = await submitted;
    assert.equal(rejected.request().postDataJSON().preview_id,preview.preview_id);
    assert.equal(rejected.status(),409);
    assert.equal((await rejected.json()).code, 'model_not_ready');
    await grant.getByText(reason,{exact:true}).waitFor();
    await grant.getByText('未授权',{exact:true}).waitFor();
    assert.equal(list().find(s=>s.source_id === registered.source_id)[purpose + '_enabled'],false);
  }
  await row.getByRole('button',{name:'导入历史'}).click();
  await page.waitForURL('**/#/imports');
  const target = imports.getByLabel('目标来源');
  await target.locator('option').filter({hasText:source.account}).waitFor({state:'attached'});
  assert.equal(await target.inputValue(),registered.source_id);

  const pick = (name, payload) => imports.getByLabel('JSON 批次文件').setInputFiles({
    name, mimeType:'application/json', buffer:Buffer.from(JSON.stringify(payload)),
  });
  const confirm = imports.getByRole('button',{name:'确认导入'});
  await pick('wrong-account.json',{source:{...source, account:source.account+'-other'}, page:batch});
  await imports.getByText(/文件声明的来源与所选来源不一致/).waitFor();
  assert.ok(await confirm.isDisabled());
  await pick('wrong-cursor.json',{source,page:{...batch,cursor:'wrong-offset'}});
  await imports.getByText(/批次起始游标与来源已提交游标不一致/).waitFor();
  assert.ok(await confirm.isDisabled());
  await pick('source-page.json', fixture);
  await imports.getByText('source-page.json · ' + batch.events.length + ' 条事件',{exact:true}).waitFor();
  await imports.getByText('事件明细',{exact:true}).click();
  await imports.getByText(batch.events[0].text,{exact:true}).waitFor();

  const attempts = [];
  const dropFirstReply = async route => {
    const request = route.request();
    const response = await route.fetch();
    const receipt = await response.json();
    attempts.push({key:request.headers()['idempotency-key'],body:request.postData(),receipt,status:response.status()});
    if (attempts.length === 1) {
      await route.fulfill({status:503,json:{code:'temporarily_unavailable'}});
    } else {
      await route.fulfill({response});
    }
  };
  await page.route('**/v2/events',dropFirstReply);
  try {
    await confirm.click();
    await imports.getByRole('button',{name:'重试同一批次'}).waitFor();
    assert.equal(list().find(s=>s.source_id === registered.source_id).cursor,batch.next_cursor);
    await page.locator('nav button[data-view="sources"]').click();
    await row.getByRole('button',{name:'导入历史'}).click();
    await imports.getByRole('button',{name:'重试同一批次'}).click();
    await imports.getByText('批次已受理',{exact:true}).waitFor();
  } finally {
    await page.unroute('**/v2/events',dropFirstReply);
  }
  assert.equal(attempts.length,2);
  assert.ok(attempts[0].key);
  assert.equal(attempts[0].key,attempts[1].key);
  assert.equal(attempts[0].body,attempts[1].body);
  assert.deepEqual(attempts.map(a=>a.status),[201,201]);
  assert.equal(attempts[1].receipt.ingest_id,attempts[0].receipt.ingest_id);
  assert.equal(attempts[1].receipt.events.length,batch.events.length);
  assert.deepEqual(attempts[1].receipt,attempts[0].receipt);
  await pick('same-page.json',fixture);
  await imports.getByText(/批次起始游标与来源已提交游标不一致/).waitFor();
  assert.ok(await confirm.isDisabled());
  console.log('Browser verified source control against the CLI, import scope/cursor validation and replay after a lost receipt.');
}

module.exports = {verifyIntake};
