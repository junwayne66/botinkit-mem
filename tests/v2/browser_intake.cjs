const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { setTimeout: delay } = require('node:timers/promises');
const fixture = require('./data/source_page.json');

async function verifyIntake(page, cli) {
  const { source, page: batch } = fixture;
  const sources = page.locator('#sources-content');
  const imports = page.locator('#imports-content');
  const list = () => cli('connector', 'list').sources;
  const findSource = id => list().find(s => s.source_id === id);
  const origin = new URL(page.url()).origin;
  const opsPost = r => new URL(r.url()).pathname === '/v2/operations' && r.request().method() === 'POST';
  const previewPost = r => new URL(r.url()).pathname === '/v2/operations/preview' && r.request().method() === 'POST';
  const opsPosts = [], previewPosts = [];
  const collect = request => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() !== 'POST') return;
    if (pathname === '/v2/operations') opsPosts.push(request);
    if (pathname === '/v2/operations/preview') previewPosts.push(request);
  };
  page.on('request', collect);
  const dialog = page.getByRole('dialog');
  // dialog.close() 先摘 open 属性（role 定位随即失配），收尾任务异步执行：
  // 焦点 / 解锁等断言一律等待可观察结果，不做 detached 后的瞬时快照。
  const waitFocus = key => page.waitForFunction(k => document.activeElement?.dataset?.focusKey === k, key);

  await page.locator('nav button[data-view="sources"]').click();
  // 空态 CTA：打开「登记新来源」并聚焦第一个字段。
  const emptyCta = sources.locator('.intake-list .empty__cta');
  await emptyCta.waitFor();
  await emptyCta.click();
  assert.ok(await sources.locator('.intake-register').evaluate(node => node.open));
  assert.equal(await page.evaluate(() => document.activeElement?.id),'source-type');
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

  // 采集开关乐观更新：pill 立即翻转，但服务端回执后才可断言 CLI 状态。
  const paused = page.waitForResponse(opsPost);
  await row.getByRole('button',{name:'暂停采集'}).click();
  await row.getByText('已暂停',{exact:true}).waitFor();
  assert.equal((await paused).status(),202);
  assert.equal(findSource(registered.source_id).capture_enabled,false);
  const resumed = page.waitForResponse(opsPost);
  await row.getByRole('button',{name:'开启采集'}).click();
  await row.getByText('采集中',{exact:true}).waitFor();
  assert.equal((await resumed).status(),202);

  // 回归：乐观翻转失败必须回滚 pill/文案，错误 toast + 原键原正文重试。
  const captureAttempts = [];
  const failFirstCapture = async route => {
    const request = route.request();
    if (request.method() !== 'POST') return route.continue();
    captureAttempts.push({key:request.headers()['idempotency-key'],body:request.postData()});
    if (captureAttempts.length === 1) {
      return route.fulfill({status:503,json:{code:'temporarily_unavailable',retryable:true}});
    }
    return route.continue();
  };
  await page.route('**/v2/operations',failFirstCapture);
  try {
    const rejected = page.waitForResponse(opsPost);
    await row.getByRole('button',{name:'暂停采集'}).click();
    await row.getByText('已暂停',{exact:true}).waitFor(); // 乐观翻转
    assert.equal((await rejected).status(),503);
    await row.getByText('采集中',{exact:true}).waitFor(); // 回滚
    await page.locator('#toast-host .toast--err').getByText('服务暂时不可用，可重试同一提交。').waitFor();
    const retry = row.getByRole('button',{name:'重试采集操作'});
    await retry.waitFor();
    const retried = page.waitForResponse(opsPost);
    await retry.click();
    assert.equal((await retried).status(),202);
    await row.getByText('已暂停',{exact:true}).waitFor();
  } finally {
    await page.unroute('**/v2/operations',failFirstCapture);
  }
  assert.equal(captureAttempts.length,2);
  assert.ok(captureAttempts[0].key);
  assert.equal(captureAttempts[0].key,captureAttempts[1].key);
  assert.equal(captureAttempts[0].body,captureAttempts[1].body);
  assert.equal(findSource(registered.source_id).capture_enabled,false);
  const resumedAgain = page.waitForResponse(opsPost);
  await row.getByRole('button',{name:'开启采集'}).click();
  assert.equal((await resumedAgain).status(),202);
  await row.getByText('采集中',{exact:true}).waitFor();

  // 模型未就绪现在在预览阶段即返回 409：不开对话框，inline 中文指引 + 查看模型设置。
  await row.getByText('用途授权',{exact:true}).click();
  const opsBeforeGrants = opsPosts.length;
  for (const [purpose,title,reason] of [
    ['extract','自动提炼','请先在模型设置中测试并绑定提炼模型，可使用服务器 API 或本地 Ollama。'],
    ['embed','向量嵌入','请先开启向量检索，再测试并绑定本机嵌入模型。'],
  ]) {
    const grant = row.getByRole('region',{name:title});
    const previewed = page.waitForResponse(previewPost);
    await grant.getByRole('button',{name:'授权' + title}).click();
    const rejected = await previewed;
    assert.equal(rejected.status(),409);
    assert.equal((await rejected.json()).code,'model_not_ready');
    assert.deepEqual(rejected.request().postDataJSON(),{kind:'change_grant',source_id:registered.source_id,purpose,allow:true});
    await grant.getByText(reason,{exact:true}).waitFor();
    await grant.getByText('未授权',{exact:true}).waitFor();
    await grant.getByRole('button',{name:'查看模型设置'}).waitFor();
    assert.equal(await dialog.count(),0);
    assert.equal(findSource(registered.source_id)[purpose + '_enabled'],false);
  }
  assert.equal(opsPosts.length,opsBeforeGrants); // 预览被拒后绝不发出确认提交
  await row.getByRole('region',{name:'自动提炼'}).getByRole('button',{name:'查看模型设置'}).click();
  await page.waitForURL('**/#/settings');
  await page.locator('nav button[data-view="sources"]').click();
  await row.getByText('用途授权',{exact:true}).waitFor();

  await row.getByRole('button',{name:'导入历史'}).click();
  await page.waitForURL('**/#/imports');
  const target = imports.getByLabel('目标来源');
  await target.locator('option').filter({hasText:source.account}).waitFor({state:'attached'});
  assert.equal(await target.inputValue(),registered.source_id);

  // 回归：瞬时 GET 失败保留已有来源选项与当前选择，只在提示行报错。
  const failSources = route => route.request().method() === 'GET'
    ? route.fulfill({status:503,json:{code:'temporarily_unavailable',retryable:true}})
    : route.continue();
  await page.route('**/v2/sources',failSources);
  try {
    await imports.getByRole('button',{name:'刷新来源'}).click();
    await imports.getByText('服务暂时不可用，可重试同一提交。').waitFor();
    assert.equal(await target.inputValue(),registered.source_id);
    assert.ok(await target.locator('option').filter({hasText:source.account}).count() > 0);
  } finally {
    await page.unroute('**/v2/sources',failSources);
  }
  await imports.getByRole('button',{name:'刷新来源'}).click();
  await target.locator('option').filter({hasText:source.account}).waitFor({state:'attached'});

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

  // 回归：内容规则在预览阶段镜像服务端校验，给出中文提示而不是迟到的英文 422。
  const nextCursor = batch.next_cursor;
  await pick('empty-checks.json',{source,page:{cursor:nextCursor,next_cursor:'offset-3',events:[
    {native_object:'run-result',native_version:'1',actor_kind:'tool_result',at:'2026-11-19T10:02:00+08:00',checks:{}},
  ]}});
  await imports.getByText(/第 1 条事件：工具回执需提供非空的 checks 对象/).waitFor();
  assert.equal(await confirm.count(),0);
  await pick('missing-text.json',{source,page:{cursor:nextCursor,next_cursor:'offset-3',events:[
    {native_object:'run-plan',native_version:'1',actor_kind:'user_message',at:'2026-11-19T10:00:00+08:00',checks:{parse:'passed'}},
  ]}});
  await imports.getByText(/第 1 条事件：本人消息需提供 text 文本/).waitFor();
  assert.equal(await confirm.count(),0);

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
    assert.equal(findSource(registered.source_id).cursor,batch.next_cursor);
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

  // 回归：卡住的导入意图可以显式放弃；放弃确认框 Escape 归还焦点，确认后解锁选择器。
  const abandonBatch = {source,page:{cursor:nextCursor,next_cursor:'offset-3',events:[
    {native_object:'abandon-probe',native_version:'1',actor_kind:'user_message',at:'2026-11-19T11:00:00+08:00',text:'用于放弃批次回归。'},
  ]}};
  await pick('abandon-page.json',abandonBatch);
  await imports.getByText('abandon-page.json · 1 条事件',{exact:true}).waitFor();
  assert.ok(await confirm.isEnabled());
  const blockEvents = route => route.fulfill({status:503,json:{code:'temporarily_unavailable',retryable:true}});
  await page.route('**/v2/events',blockEvents);
  try {
    await confirm.click();
    await imports.getByRole('button',{name:'重试同一批次'}).waitFor();
    const abandon = imports.getByRole('button',{name:'放弃此批次'});
    await abandon.waitFor();
    assert.ok(await target.isDisabled());
    await abandon.click();
    await dialog.waitFor();
    await page.keyboard.press('Escape');
    await dialog.waitFor({state:'detached'});
    await page.waitForFunction(() => document.activeElement?.textContent === '放弃此批次');
    await abandon.click();
    await dialog.getByRole('button',{name:'放弃批次'}).click();
    await dialog.waitFor({state:'detached'});
    await page.waitForFunction(() => !document.getElementById('intake-target').disabled);
  } finally {
    await page.unroute('**/v2/events',blockEvents);
  }
  assert.ok(await target.isEnabled());
  assert.equal(await imports.getByRole('button',{name:'重试同一批次'}).count(),0);
  assert.ok(await confirm.isEnabled()); // 意图已清除，文件仍在，可重新确认

  await pick('same-page.json',fixture);
  await imports.getByText(/批次起始游标与来源已提交游标不一致/).waitFor();
  assert.ok(await confirm.isDisabled());

  // 回归：实时同步推进同一来源游标后，授权确认仍然成功（预览只绑定目标态）。
  // 用回环合成 Ollama 通过真实探针并绑定提炼模型；页面零外部请求不变。
  const modelServer = createServer((request, response) => {
    if (request.method === 'POST' && request.url === '/api/chat') {
      let body = '';
      request.on('data', chunk => { body += chunk; });
      request.on('end', () => {
        response.setHeader('Content-Type','application/json');
        response.end(JSON.stringify({done:true,done_reason:'stop',message:{role:'assistant',content:'{"candidates":[]}'}}));
      });
      return;
    }
    response.writeHead(404).end();
  });
  await new Promise((resolve, reject) => {
    modelServer.once('error', reject);
    modelServer.listen(0,'127.0.0.1',resolve);
  });
  const modelEndpoint = 'http://127.0.0.1:' + modelServer.address().port;
  const modelName = 'intake-live-fixture';
  // 运行器的 cli() 是 execFileSync，会阻塞承载合成模型的本进程事件循环，
  // 使服务端探针拿不到响应而死锁：模型登记 / 探针 / 绑定一律走异步 page.request。
  const configPatch = async body => {
    const snapshot = await page.request.get(origin + '/v2/config');
    assert.equal(snapshot.status(),200);
    const revision = (await snapshot.json()).config_revision;
    const res = await page.request.patch(origin + '/v2/config', {
      headers:{Origin:origin,'Idempotency-Key':'intake-live-' + Date.now() + '-' + Math.random().toString(36).slice(2,8)},
      data:{expected_revision:revision,patch:body},
    });
    const text = await res.text();
    assert.equal(res.status(),200,text);
    return JSON.parse(text);
  };
  let modelId = null;
  try {
    const added = await configPatch({models:{op:'add',name:modelName,protocol:'ollama',endpoint:modelEndpoint,model_id:'synthetic'}});
    modelId = added.model_result.id;
    const tested = await page.request.post(origin + '/v2/models/' + encodeURIComponent(modelId) + '/test', {
      headers:{Origin:origin}, data:{purpose:'extract'},
    });
    assert.equal(tested.status(),200,await tested.text());
    await configPatch({bindings:{extract:modelId}});

    await page.locator('nav button[data-view="sources"]').click();
    await row.getByText('采集中',{exact:true}).waitFor();
    await row.getByText('用途授权',{exact:true}).click();
    const grant = row.getByRole('region',{name:'自动提炼'});
    const trigger = grant.getByRole('button',{name:'授权自动提炼'});
    const previewed = page.waitForResponse(previewPost);
    await trigger.click();
    const previewResponse = await previewed;
    assert.equal(previewResponse.status(),202);
    const preview = await previewResponse.json();
    assert.deepEqual(preview.scope,{kind:'change_grant',source_id:registered.source_id,purpose:'extract',allow:true});
    assert.equal(preview.effective,false);
    assert.equal(preview.model_destination.local_endpoint,true);
    await dialog.waitFor();
    await dialog.getByText('授权自动提炼',{exact:true}).waitFor();
    for (const key of ['来源','用途','操作']) await dialog.getByText(key,{exact:true}).waitFor();
    assert.equal(await dialog.locator('.intake-grant-warn').count(),0);
    await dialog.getByText(/数据仅发送至本机模型/).waitFor();

    // 确认前经由 page.request 推进同一来源游标：旧实现会因此 revision_changed。
    const advanced = await page.request.post(origin + '/v2/events',{
      headers:{Origin:origin,'Idempotency-Key':'live-sync-' + Date.now()},
      data:{source_id:registered.source_id,cursor:findSource(registered.source_id).cursor,next_cursor:'live-sync-offset',events:[
        {native_object:'live-sync-probe',native_version:'1',actor_kind:'user_message',at:new Date().toISOString(),text:'授权确认前的新事件。'},
      ]},
    });
    assert.equal(advanced.status(),201);
    assert.equal(findSource(registered.source_id).cursor,'live-sync-offset');

    const submitted = page.waitForResponse(opsPost);
    await dialog.getByRole('button',{name:'确认授权'}).click();
    const accepted = await submitted;
    assert.equal(accepted.status(),202);
    assert.equal(accepted.request().postDataJSON().preview_id,preview.preview_id);
    await dialog.waitFor({state:'detached'});
    await grant.getByText('已授权',{exact:true}).waitFor();
    await grant.getByText('已允许自动提炼',{exact:true}).waitFor();
    await waitFocus('extract-trigger');
    assert.equal(findSource(registered.source_id).extract_enabled,true);

    // 等后台提炼作业消化完再关合成模型，避免留下无谓的失败重试。
    const deadline = Date.now() + 20000;
    for (;;) {
      const jobsResponse = await page.request.get(origin + '/v2/jobs?limit=100');
      const { jobs } = await jobsResponse.json();
      if (!jobs.some(job => job.status === 'pending' || job.status === 'running')) break;
      assert.ok(Date.now() < deadline,'distill jobs did not drain against the synthetic model');
      await delay(400);
    }

    const revokePreview = cli('grant','source',registered.source_id,'--purpose','extract','--allow','false','--preview');
    cli('grant','source',registered.source_id,'--purpose','extract','--allow','false','--confirm',revokePreview.preview_id);
    assert.equal(findSource(registered.source_id).extract_enabled,false);
    await sources.getByRole('button',{name:'刷新来源'}).click();
    await grant.getByText('未授权',{exact:true}).waitFor();

    // 回归：服务器模型去向警示 + 取消归还焦点 + Tab 圈定在对话框内。
    const flipPreview = async route => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch();
      const payload = await response.json();
      if (payload.model_destination) payload.model_destination.local_endpoint = false;
      return route.fulfill({status:response.status(),contentType:'application/json',body:JSON.stringify(payload)});
    };
    await page.route('**/v2/operations/preview',flipPreview);
    try {
      const flipped = page.waitForResponse(previewPost);
      await trigger.click();
      await flipped;
      await dialog.waitFor();
      await dialog.getByText('来源片段将发送至服务器模型',{exact:true}).waitFor();
      await dialog.getByText(modelName,{exact:true}).waitFor();
      await dialog.getByText(modelEndpoint,{exact:true}).waitFor();
      await dialog.getByText('synthetic',{exact:true}).waitFor();
      await page.setViewportSize({width:375,height:812});
      assert.ok(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth),'grant dialog overflows at 375px');
      await page.setViewportSize({width:1210,height:850});
      // 原生模态陷阱：Chrome 会在循环中经过 body 一次；背景元素不可聚焦即合格。
      for (let i = 0; i < 4; i += 1) {
        await page.keyboard.press('Tab');
        const escaped = await page.evaluate(() => {
          const active = document.activeElement;
          if (!active || active === document.body || active === document.documentElement) return false;
          return !active.closest('dialog');
        });
        assert.ok(!escaped,'Tab reached an interactive element outside the grant dialog');
      }
      await dialog.getByRole('button',{name:'取消'}).click();
      await dialog.waitFor({state:'detached'});
      await waitFocus('extract-trigger');
    } finally {
      await page.unroute('**/v2/operations/preview',flipPreview);
    }

    // Escape 同样关闭并归还焦点。
    const escaped = page.waitForResponse(previewPost);
    await trigger.click();
    await escaped;
    await dialog.waitFor();
    await page.keyboard.press('Escape');
    await dialog.waitFor({state:'detached'});
    await waitFocus('extract-trigger');

    // 回归：确认时 revision_changed → 框内中文提示 + 自动重新预览一次（共 2 次封顶），
    // 绝不自动确认；封顶后出现手动重新预览；每次预览换新键，显式确认后成功。
    const confirmAttempts = [];
    const forceRevision = async route => {
      const request = route.request();
      if (request.method() !== 'POST') return route.continue();
      confirmAttempts.push({key:request.headers()['idempotency-key'],body:request.postDataJSON()});
      if (confirmAttempts.length <= 2) {
        return route.fulfill({status:409,json:{code:'revision_changed',message:'operation target or state changed; request a fresh preview'}});
      }
      return route.continue();
    };
    await page.route('**/v2/operations',forceRevision);
    try {
      const previewsBefore = previewPosts.length;
      const firstPreviewResponse = page.waitForResponse(previewPost);
      await trigger.click();
      const firstPreview = await (await firstPreviewResponse).json();
      await dialog.waitFor();
      const autoPreviewResponse = page.waitForResponse(previewPost);
      const firstConfirm = page.waitForResponse(opsPost);
      await dialog.getByRole('button',{name:'确认授权'}).click();
      assert.equal((await firstConfirm).status(),409);
      await dialog.getByText('来源状态已更新，请核对最新授权范围',{exact:true}).waitFor();
      const secondPreview = await (await autoPreviewResponse).json();
      assert.notEqual(secondPreview.preview_id,firstPreview.preview_id);
      assert.equal(confirmAttempts.length,1); // 没有自动确认
      assert.ok(await dialog.getByRole('button',{name:'确认授权'}).isEnabled());
      assert.equal(await dialog.getByRole('button',{name:'查看模型设置'}).count(),0);
      assert.equal(await row.getByRole('button',{name:'查看模型设置'}).count(),0);

      const secondConfirm = page.waitForResponse(opsPost);
      await dialog.getByRole('button',{name:'确认授权'}).click();
      assert.equal((await secondConfirm).status(),409);
      await dialog.getByRole('button',{name:'重新预览'}).waitFor();
      assert.ok(await dialog.getByRole('button',{name:'确认授权'}).isDisabled());
      await delay(400);
      assert.equal(previewPosts.length,previewsBefore + 2); // 没有第三次自动预览

      const manualPreviewResponse = page.waitForResponse(previewPost);
      await dialog.getByRole('button',{name:'重新预览'}).click();
      const thirdPreview = await (await manualPreviewResponse).json();
      assert.ok(await dialog.getByRole('button',{name:'确认授权'}).isEnabled());
      const thirdConfirm = page.waitForResponse(opsPost);
      await dialog.getByRole('button',{name:'确认授权'}).click();
      const finalResponse = await thirdConfirm;
      assert.equal(finalResponse.status(),202);
      assert.equal(finalResponse.request().postDataJSON().preview_id,thirdPreview.preview_id);
      await dialog.waitFor({state:'detached'});
      await grant.getByText('已授权',{exact:true}).waitFor();
      assert.equal(findSource(registered.source_id).extract_enabled,true);
      assert.equal(confirmAttempts.length,3);
      assert.equal(new Set(confirmAttempts.map(a=>a.key)).size,3);
      assert.deepEqual(confirmAttempts.map(a=>a.body.preview_id),[firstPreview.preview_id,secondPreview.preview_id,thirdPreview.preview_id]);
    } finally {
      await page.unroute('**/v2/operations',forceRevision);
    }

    const finalRevoke = cli('grant','source',registered.source_id,'--purpose','extract','--allow','false','--preview');
    cli('grant','source',registered.source_id,'--purpose','extract','--allow','false','--confirm',finalRevoke.preview_id);
    await sources.getByRole('button',{name:'刷新来源'}).click();
    await grant.getByText('未授权',{exact:true}).waitFor();
  } finally {
    if (modelId) {
      try { await configPatch({bindings:{extract:null}}); } catch (_) { /* 清理尽力而为 */ }
      try { await configPatch({models:{op:'remove',id:modelId}}); } catch (_) { /* 清理尽力而为 */ }
    }
    await new Promise(resolve => modelServer.close(resolve));
  }
  assert.equal(findSource(registered.source_id).extract_enabled,false);
  assert.equal(findSource(registered.source_id).embed_enabled,false);

  // 回归：历史同步进度可见（相对中文时间 + 错误 code 中文映射），开启时约 5s
  // 静默自动刷新，且按键控原位更新保留卡片节点与焦点。
  let historyMockHits = 0;
  const mockHistory = async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    const payload = await response.json();
    const target = payload.sources.find(s => s.source_id === registered.source_id);
    historyMockHits += 1;
    target.history = {
      supported:true, enabled:true, events:historyMockHits >= 2 ? 4 : 3,
      last_scan:Math.floor(Date.now()/1000) - 120, last_error:'feishu_cursor_expired', skipped_containers:2,
    };
    return route.fulfill({status:response.status(),contentType:'application/json',body:JSON.stringify(payload)});
  };
  await page.route('**/v2/sources',mockHistory);
  try {
    await sources.getByRole('button',{name:'刷新来源'}).click();
    await row.getByText('2 分钟前',{exact:true}).waitFor();
    await row.getByText(/飞书分页游标已失效/).waitFor();
    await row.getByText(/feishu_cursor_expired/).waitFor();
    await row.getByText('持续采集',{exact:true}).waitFor();
    await row.evaluate(el => { el.__marker = true; });
    await row.getByRole('button',{name:'导入历史'}).focus();
    const autoRefresh = page.waitForResponse(
      r => new URL(r.url()).pathname === '/v2/sources' && r.request().method() === 'GET',
      {timeout:9000},
    );
    await autoRefresh; // 未手动点击：历史开启时的定时刷新
    await row.getByText('4',{exact:true}).waitFor(); // 事件计数原位更新
    assert.ok(await row.evaluate(el => el.__marker === true),'auto refresh rebuilt the card instead of updating in place');
    assert.equal(await page.evaluate(() => document.activeElement?.dataset?.focusKey),'import');
    assert.ok(historyMockHits >= 2);
  } finally {
    await page.unroute('**/v2/sources',mockHistory);
  }
  await sources.getByRole('button',{name:'刷新来源'}).click();
  await row.getByText('2 分钟前',{exact:true}).waitFor({state:'detached'});

  // 375px 窄屏：来源与导入视图均无横向溢出。
  await page.setViewportSize({width:375,height:812});
  const menu = page.getByRole('button',{name:'打开导航'});
  for (const view of ['sources','imports']) {
    if (await menu.isVisible()) await menu.click();
    await page.locator('nav button[data-view="' + view + '"]').click();
    await page.locator('#' + view + '-content .intake-panel').first().waitFor();
    const dimensions = await page.evaluate(() => ({
      viewport: window.innerWidth,
      document: document.documentElement.scrollWidth,
    }));
    assert.ok(dimensions.document <= dimensions.viewport,view + ' does not fit the 375px viewport');
  }
  await page.setViewportSize({width:1210,height:850});
  page.removeListener('request',collect);
  console.log('Browser verified source control, preview-time model readiness, grant dialog preview/confirm with live cursor advance, revision re-preview cap, focus return, import validation/replay/abandon and 375px layout.');
}

module.exports = {verifyIntake};
