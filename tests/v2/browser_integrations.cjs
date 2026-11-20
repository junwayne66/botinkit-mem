const assert = require('node:assert/strict');
const { chmodSync, mkdirSync, readFileSync, writeFileSync } = require('node:fs');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

function integrationEnvironment(workspace, env) {
  const tools = path.join(workspace, 'tools');
  mkdirSync(tools);
  env.BMEM_CONFIG_ROOT = path.join(workspace, 'host-config');
  env.PATH = tools + path.delimiter + env.PATH;
  for (const host of ['.codex','.claude','.dsh']) {
    mkdirSync(path.join(env.BMEM_CONFIG_ROOT,host), { recursive:true });
  }
  writeFileSync(path.join(env.BMEM_CONFIG_ROOT,'.codex/config.toml'), '# fixture settings\nmodel = "synthetic"\n');
  writeFileSync(path.join(env.BMEM_CONFIG_ROOT,'.claude/settings.json'), '{"alwaysThinkingEnabled":true}');
  writeFileSync(path.join(env.BMEM_CONFIG_ROOT,'.dsh/cordis.patch.yml'), '# fixture patch\n');
  const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII=';
  writeFileSync(path.join(tools,'fixture.png'),Buffer.from(png,'base64'));
  for (const bin of ['claude','codex','dsh']) {
    writeFileSync(path.join(tools,bin),'#!/bin/sh\nexit 0\n',{mode:0o700});
  }
  const cli = path.join(tools,'lark-cli');
  writeFileSync(cli, `#!/bin/sh
case "$1:$2" in
  profile:list) printf '%s' '[{"brand":"feishu","name":"fixture","active":true}]' ;;
  auth:login)
    if [ "$3" = "--device-code" ]; then
      sleep 2
      printf '%s' '{"ok":true}'
    else
      printf '%s' '{"verification_url":"https://accounts.feishu.cn/fixture","device_code":"private-fixture-code","expires_in":30}'
    fi ;;
  auth:qrcode) cp "$(dirname "$0")/fixture.png" qr.png ;;
  auth:status) printf '%s' '{"brand":"feishu","appId":"fixture-app","identities":{"user":{"available":true,"status":"ready","openId":"fixture-user","userName":"测试飞书账号"}}}' ;;
  *) exit 1 ;;
esac
`);
  chmodSync(cli,0o700);
}

async function verifyIntegrations(page, cli, workspace, executable, env) {
  await page.setViewportSize({width:1210,height:850});
  await page.locator('nav button[data-view="sources"]').click();
  const integration = host => page.locator('#integration-'+host);
  const feishu = integration('feishu');
  // 先展开登记表单读取一次客户端列表：安装成功后必须作废旧缓存并刷新选项。
  const registration = page.locator('#sources-content .intake-register');
  await registration.locator('summary').click();
  const clientSelect = page.locator('#source-client');
  await clientSelect.locator('option').first().waitFor({state:'attached'});
  assert.equal(await clientSelect.locator('option').count(),1);
  const integrationsGet = r => new URL(r.url()).pathname === '/v2/integrations' && r.request().method() === 'GET';

  // 先发起飞书授权，让其 1.2s 轮询与宿主安装并行，覆盖两类回归：
  // 1) 单次 GET 失败不得终止轮询；2) 轮询刷新不得重新启用/改写安装中的按钮。
  await feishu.getByRole('button',{name:'发起授权'}).click();
  const link = feishu.getByRole('link',{name:'打开飞书授权页'});
  await link.waitFor();
  assert.equal(await link.getAttribute('href'),'https://accounts.feishu.cn/fixture');
  await feishu.getByRole('img',{name:'飞书授权二维码'}).waitFor();
  await page.waitForFunction(() => document.querySelector('#integration-feishu img').naturalWidth > 0);

  let failedGets = 0;
  const failFirstGet = async route => {
    if (route.request().method() !== 'GET') return route.continue();
    if (failedGets === 0) {
      failedGets += 1;
      return route.fulfill({status:503,json:{code:'temporarily_unavailable',retryable:true}});
    }
    return route.continue();
  };
  await page.route('**/v2/integrations',failFirstGet);
  let releaseInstall;
  const installGate = new Promise(resolve => { releaseInstall = resolve; });
  const holdInstall = async route => { await installGate; return route.continue(); };
  await page.route('**/v2/integrations/codex/install',holdInstall);

  const codex = integration('codex');
  await codex.getByText('配置集成',{exact:true}).click();
  await codex.getByLabel('开启新会话采集').check();
  await codex.getByLabel('允许助手读取已授权声明').check();
  // 卡片内唯一按钮；忙碌态会改写文案，定位不能依赖名称。
  const install = codex.getByRole('button');
  const failedPoll = page.waitForResponse(r => integrationsGet(r) && r.status() === 503);
  await install.click();
  await codex.getByRole('button',{name:'正在写入配置…'}).waitFor();
  assert.equal((await failedPoll).status(),503); // 轮询读到一次 503
  assert.equal(failedGets,1);
  const okPoll = page.waitForResponse(r => integrationsGet(r) && r.ok());
  await okPoll; // 失败后轮询必须继续：下一轮成功并刷新宿主卡片
  await page.waitForTimeout(200);
  assert.ok(await install.isDisabled(),'poll re-enabled an in-flight install button');
  await codex.getByRole('button',{name:'正在写入配置…'}).waitFor(); // 文案也未被轮询改写
  releaseInstall();
  await codex.getByText('已写入配置',{exact:true}).waitFor();
  await codex.getByRole('button',{name:'更新集成',exact:true}).waitFor();
  await page.unroute('**/v2/integrations/codex/install',holdInstall);
  await page.unroute('**/v2/integrations',failFirstGet);
  assert.equal(failedGets,1);

  for (const host of ['claude-code','dsh']) {
    const card = integration(host);
    await card.getByText('配置集成',{exact:true}).click();
    await card.getByLabel('开启新会话采集').check();
    await card.getByLabel('允许助手读取已授权声明').check();
    await card.getByRole('button',{name:'添加集成',exact:true}).click();
    await card.getByText('已写入配置',{exact:true}).waitFor();
    await card.getByRole('button',{name:'更新集成',exact:true}).click();
    await card.getByRole('button',{name:'更新集成',exact:true}).waitFor();
  }

  const root = path.join(workspace,'host-config');
  const read = name => readFileSync(path.join(root,name),'utf-8');
  const codexConfig = JSON.parse(read('.codex/hooks.json'));
  const claude = JSON.parse(read('.claude/settings.json'));
  assert.equal(codexConfig.hooks.UserPromptSubmit.length,1);
  assert.equal(claude.hooks.UserPromptSubmit.length,1);
  assert.match(read('.codex/config.toml'), /mcp_servers\.bmem-v2/);
  assert.match(read('.codex/config.toml'), /# fixture settings\nmodel = "synthetic"/);
  assert.equal(claude.alwaysThinkingEnabled,true);
  assert.match(read('.dsh/cordis.patch.yml'), /^# fixture patch/);
  assert.equal(JSON.parse(read('.claude/settings.json.bmem-v2.previous')).alwaysThinkingEnabled,true);
  assert.equal(JSON.parse(read('.claude.json')).mcpServers['bmem-v2'].command,executable);
  assert.match(read('.dsh/cordis.patch.yml'), /transport: stdio/);
  assert.equal(read('.dsh/cordis.patch.yml').split('# BEGIN bmem-v2').length,2);

  const source = cli('connector','list').sources.find(s=>s.type==='dsh');
  const client = cli('client','list').clients.find(c=>c.host==='dsh');
  assert.equal(source.capture_enabled,true);
  assert.equal(client.shared_statements,true);
  const capture = spawnSync(executable,['--json','hook','dsh','--source',source.source_id,'--client',client.client_id],{
    env, encoding:'utf8',input:JSON.stringify({
      session_id:'fixture-dsh',prompt_id:'fixture-message',hook_event_name:'AssistantMessage',
      prompt:'DSH synthetic capture smoke',
    }),
  });
  assert.equal(capture.status,0,capture.stderr);
  assert.notEqual(cli('connector','list').sources.find(s=>s.source_id===source.source_id).cursor,'');

  // 历史同步开关：乐观文案 + 忙碌进度，真实端点开启 / 暂停后卡片原位更新。
  const dshCard = page.locator('#sources-content .intake-list article').filter({hasText:'dsh ·'});
  const historyPost = r => new URL(r.url()).pathname === '/v2/sources/' + source.source_id + '/history' && r.request().method() === 'POST';
  const historyOn = page.waitForResponse(historyPost);
  await dshCard.getByRole('button',{name:'同步历史',exact:true}).click();
  assert.equal((await historyOn).status(),202);
  await dshCard.getByRole('button',{name:'暂停历史同步',exact:true}).waitFor();
  await dshCard.getByText('持续采集',{exact:true}).waitFor();
  const historyOff = page.waitForResponse(historyPost);
  await dshCard.getByRole('button',{name:'暂停历史同步',exact:true}).click();
  assert.equal((await historyOff).status(),202);
  await dshCard.getByRole('button',{name:'同步历史',exact:true}).waitFor();

  // 安装成功后客户端缓存已作废：展开中的登记表单读到了新客户端。
  await clientSelect.locator('option').filter({hasText:'dsh'}).waitFor({state:'attached'});
  await registration.locator('summary').click();

  await feishu.getByText(/飞书已授权并登记为来源/).waitFor();
  const origin = new URL(page.url()).origin;
  const status = await page.request.get(origin+'/v2/integrations');
  assert(!((await status.text()).includes('private-fixture-code')));
  assert.equal(cli('connector','list').sources.filter(s=>s.type==='feishu').length,1);
  console.log('Browser verified three isolated host installations, install busy-state under polling, poll survival after a failed GET, and Feishu device-flow/QR/source registration.');
}

module.exports = { integrationEnvironment, verifyIntegrations };
