const assert = require('node:assert/strict');
const { mkdirSync, readFileSync, writeFileSync } = require('node:fs');
const path = require('node:path');

async function verifyDataConsole(page, cli, hostConfig) {
  const source = cli('connector','list').sources.find(item => item.type === 'codex' && item.account === 'local');
  assert.ok(source, 'Codex fixture installation is required');
  const sessions = path.join(hostConfig,'.codex','sessions');
  mkdirSync(sessions,{recursive:true});
  writeFileSync(path.join(sessions,'rollout-console-fixture.jsonl'),
    readFileSync(path.join(__dirname,'data','history_codex.jsonl')));

  const origin = new URL(page.url()).origin;
  await page.goto(origin+'/#/imports');
  await page.getByRole('heading',{name:'数据管理',exact:true}).waitFor();
  await page.getByRole('button',{name:'从来源导入历史'}).click();
  const dialog = page.getByRole('dialog');
  await dialog.locator('#history-source-picker').selectOption(source.source_id);
  await dialog.getByRole('button',{name:'选择来源'}).click();
  const allSessions = dialog.getByRole('checkbox',{name:/全部历史会话/});
  assert.equal(await allSessions.isChecked(),false);
  const submit = dialog.getByRole('button',{name:'创建导入任务'});
  assert.equal(await submit.isDisabled(),true);
  assert.equal(cli('connector','history-jobs').jobs.length,0);
  await allSessions.check();
  assert.equal(cli('connector','history-jobs').jobs.length,0);
  await submit.click();
  await page.locator('.intake-jobs-list').getByText('历史会话 · 已完成').waitFor({timeout:12000});
  await page.locator('.intake-jobs-list').getByText('1 / 1 已处理').waitFor();
  assert.equal(cli('connector','list').sources.find(item => item.source_id === source.source_id).capture_enabled,false);

  await page.getByRole('button',{name:'导出数据',exact:true}).click();
  await dialog.getByLabel('数据来源').selectOption(source.source_id);
  await dialog.getByLabel('起始日期').fill('2026-11-18');
  await dialog.getByLabel('截止日期').fill('2026-11-21');
  const downloading = page.waitForEvent('download');
  await dialog.getByRole('button',{name:'确认并下载'}).click();
  const download = await downloading;
  const document = JSON.parse(readFileSync(await download.path(),'utf8'));
  assert.equal(document.source.source_id,source.source_id);
  assert.deepEqual(document.events.map(event => event.actor_kind),['user_message','tool_result']);
  assert.match(document.events[0].text,/历史续读/);
  await dialog.getByRole('button',{name:'取消'}).click();
  await page.getByRole('table').getByRole('cell',{name:'导出',exact:true}).waitFor();

  const registration = await page.request.post(origin+'/v2/clients',{
    headers:{origin,'Idempotency-Key':'ledger-browser-fixture'},
    data:{host:'ledger-fixture',destination:'loopback-browser',capabilities:{load_context:true}},
  });
  assert.equal(registration.status(),201);
  const client = await registration.json();
  const delivery = await page.request.post(origin+'/v2/context/load',{
    headers:{origin,Authorization:'Bearer '+client.token,
      'X-Bmem-Instance':registration.headers()['x-bmem-instance'],'Idempotency-Key':'ledger-browser-delivery'},
    data:{route_ref:client.route_ref,session_id:'browser-session',context_epoch:'epoch-1',budget:{statement_tokens:1024,evidence_tokens:0}},
  });
  assert.equal(delivery.status(),200);
  await page.goto(origin+'/#/injections');
  await page.locator('#deliv-body').getByLabel('装入状态筛选').waitFor();
  const ledger = page.locator('#deliv-body');
  const row = ledger.getByRole('row').filter({hasText:'ledger-fixture'});
  await row.getByRole('cell',{name:'ledger-fixture'}).waitFor();
  await row.getByText('查看详情').click();
  await row.getByText('模型路由').waitFor();
  await page.setViewportSize({width:390,height:840});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await ledger.getByLabel('宿主名称').fill('ledger-fixture');
  await ledger.getByLabel('装入状态筛选').selectOption('observed');
  await ledger.getByRole('button',{name:'应用筛选'}).click();
  await ledger.getByText('最近 7 天内没有符合条件的投递记录。').waitFor();
  console.log('Browser verified opt-in full history, task receipt, V2 JSON download, and the narrow delivery ledger.');
}

module.exports = {verifyDataConsole};
