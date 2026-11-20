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
  for (const host of ['codex','claude-code','dsh']) {
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
  const codex = JSON.parse(read('.codex/hooks.json'));
  const claude = JSON.parse(read('.claude/settings.json'));
  assert.equal(codex.hooks.UserPromptSubmit.length,1);
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

  const feishu = integration('feishu');
  await feishu.getByRole('button',{name:'发起授权'}).click();
  const link = feishu.getByRole('link',{name:'打开飞书授权页'});
  await link.waitFor();
  assert.equal(await link.getAttribute('href'),'https://accounts.feishu.cn/fixture');
  await feishu.getByRole('img',{name:'飞书授权二维码'}).waitFor();
  await page.waitForFunction(() => document.querySelector('#integration-feishu img').naturalWidth > 0);
  await feishu.getByText(/飞书已授权并登记为来源/).waitFor();
  const origin = new URL(page.url()).origin;
  const status = await page.request.get(origin+'/v2/integrations');
  assert(!((await status.text()).includes('private-fixture-code')));
  assert.equal(cli('connector','list').sources.filter(s=>s.type==='feishu').length,1);
  console.log('Browser verified three isolated host installations and Feishu device-flow/QR/source registration.');
}

module.exports = { integrationEnvironment, verifyIntegrations };
