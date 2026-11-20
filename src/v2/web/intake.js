'use strict';

window.MemoryIntake = (() => {
  const eventKinds = {user_message:'本人消息', assistant_message:'助手消息', tool_result:'工具回执'};
  const dispositions = {accepted:'已受理', duplicate:'已存在', revoked:'已撤回', revoked_blocked:'已屏蔽', revoked_unknown:'未找到撤回目标'};
  const draft = { sourceId:'', file:null, intent:null, sending:false, receipt:null, last:null };
  let drawImport, refreshImport;
  const limit = 4 * 1024 * 1024;

  function button(text, run, primary = false) {
    const node = el('button',{class:'btn' + (primary ? ' btn--primary' : ''),type:'button',text});
    node.addEventListener('click',run);
    return node;
  }
  function field(id, name, control) {
    control.id = id;
    return el('div',{class:'field'},[el('label',{for:id,text:name}),control]);
  }
  function statusLine() { return el('div',{class:'resultline','aria-live':'polite'}); }
  function label(source) { return source.type + ' · ' + source.account; }
  function uncertain(res) { return res.networkError || res.status >= 500 || res.retryable; }

  const integrationHosts = {
    codex: { name:'Codex', usage:'采集新会话，通过 MCP 查询已授权声明。添加后在 Codex 中审阅并信任新的 Hook。' },
    'claude-code': { name:'Claude Code', usage:'采集新会话，通过 Hook 和 MCP 使用已授权声明。添加后打开新会话。' },
    dsh: { name:'DeepSeek Harness（DSH）', usage:'通过原生插件采集新会话，并通过 MCP 查询已授权声明。添加后重新加载 DSH 配置。' },
  };
  const feishuStages = {
    checking:'正在核对飞书 CLI 配置…',
    configuring:'请在飞书官方页面完成应用设置。',
    authorizing:'请在飞书官方页面确认授权。',
    connected:'飞书已授权并登记为来源。',
  };

  function integrations(onChange) {
    const region = el('section',{class:'intake-panel intake-integrations'},[
      el('h3',{text:'添加集成'}),
    ]);
    const catalog = el('div',{class:'intake-integration-grid'});
    const general = statusLine();
    region.append(general,catalog);
    const cards = {};
    for (const [id, details] of Object.entries(integrationHosts)) {
      const availability = el('span',{class:'pill',text:'检测中'});
      const add = el('details',{class:'intake-connect'});
      add.append(el('summary',{text:'配置集成'}));
      const capture = el('input',{type:'checkbox'});
      const share = el('input',{type:'checkbox'});
      const output = statusLine();
      const install = button('添加集成', async () => {
        install.disabled = true;
        output.textContent = '正在写入本机接入配置…';
        const res = await api('POST','/v2/integrations/'+id+'/install',{
          body:{capture:capture.checked,share_statements:share.checked},
        });
        renderResult(output,res);
        if (res.ok) {
          output.textContent = '已配置。'+details.usage;
          await refresh();
          await onChange();
        }
        install.disabled = false;
      },true);
      add.append(
        el('div',{class:'intake-connect-options'},[
          el('label',{},[capture,' 开启新会话采集']),
          el('label',{},[share,' 允许助手读取已授权声明']),
        ]),install,output
      );
      cards[id] = {availability,add,install};
      catalog.append(el('article',{class:'intake-source',id:'integration-'+id},[
        el('header',{class:'intake-header'},[el('h4',{text:details.name}),availability]),
        el('p',{class:'hint-sm',text:details.usage}),add,
      ]));
    }

    const feishuStatus = el('p',{class:'hint-sm',role:'status'});
    const authControls = el('div',{class:'intake-feishu-auth',hidden:true});
    const openAuth = el('a',{class:'btn btn--primary',target:'_blank',rel:'noopener noreferrer',text:'打开飞书授权页'});
    const qr = el('img',{width:'156',height:'156',alt:'飞书授权二维码',hidden:true});
    authControls.append(openAuth,qr);
    const start = button('发起授权', async () => {
      start.disabled = true;
      feishuStatus.textContent = '正在发起飞书授权…';
      const res = await api('POST','/v2/integrations/feishu/start');
      if (res.ok) updateFeishu(res.data);
      else {renderResult(feishuStatus,res);start.disabled = false;}
    },true);
    catalog.prepend(el('article',{class:'intake-source',id:'integration-feishu'},[
      el('header',{class:'intake-header'},[el('h4',{text:'飞书'})]),
      el('p',{class:'hint-sm',text:'使用官方 lark-cli 在浏览器授权并登记账号。授权后可按来源导入事件批次。'}),
      feishuStatus,start,authControls
    ]));

    let poll;
    let lastStage;
    let lastQrUrl;
    function updateFeishu(state) {
      clearTimeout(poll);
      const changed = lastStage !== state.stage;
      lastStage = state.stage;
      start.disabled = state.busy || !state.available;
      start.textContent = state.busy ? '授权进行中' : state.stage === 'connected' ? '重新授权' : '发起授权';
      const base = feishuStages[state.stage] || (state.available
        ? state.registered ? '已有飞书来源。可重新授权当前账号。' : '等待授权。'
        : '需要先安装官方 CLI：npm install -g @larksuite/cli');
      feishuStatus.textContent = state.stage === 'failed'
        ? state.result.message : state.result?.account_name ? base+' '+state.result.account_name : base;
      authControls.hidden = !state.url;
      if (state.url) openAuth.href = state.url;
      else openAuth.removeAttribute('href');
      qr.hidden = !state.qr_ready;
      if (state.qr_ready && lastQrUrl !== state.url) {
        lastQrUrl = state.url;
        qr.src = '/v2/integrations/feishu/qr?view='+Date.now();
      } else if (!state.qr_ready && !state.busy) {
        lastQrUrl = null;
        qr.removeAttribute('src');
      }
      if (state.busy) {
        poll = setTimeout(() => {
          if (region.isConnected && !region.closest('.view')?.hidden) void refresh();
        },1200);
      } else if (changed && state.stage === 'connected') {
        void onChange();
      }
    }

    async function refresh() {
      const res = await api('GET','/v2/integrations');
      renderResult(general,res);
      if (!res.ok) return;
      for (const state of res.data.hosts) {
        const card = cards[state.host];
        if (!card) continue;
        card.availability.textContent = state.configured ? '已写入配置' : state.available ? '已检测到' : '未检测到';
        card.install.disabled = !state.available;
        card.install.textContent = state.configured ? '更新集成' : '添加集成';
        card.add.hidden = !state.available;
      }
      updateFeishu(res.data.feishu);
    }

    start.disabled = true;
    queueMicrotask(refresh);
    return region;
  }

  function openSources(root) {
    const summary = el('strong',{text:'正在读取来源…'});
    const list = el('div',{class:'intake-list'});
    const result = statusLine();
    const refreshButton = button('刷新来源',refresh);
    const panel = el('section',{class:'intake-panel'},[
      el('header',{class:'intake-header'},[summary,refreshButton]), result, list,
    ]);
    const registration = el('details',{class:'intake-panel intake-register'});
    registration.append(el('summary',{text:'登记新来源'}));
    const type = el('input',{type:'text',required:true,list:'intake-hosts',autocomplete:'off',value:'claude-code'});
    const types = el('datalist',{id:'intake-hosts'},['claude-code','codex','dsh','feishu'].map(value=>el('option',{value})));
    const account = el('input',{type:'text',required:true,autocomplete:'off'});
    const client = el('select');
    client.add(new Option('仅本机所有者',''));
    const clientError = statusLine();
    const registerStatus = statusLine();
    const save = el('button',{class:'btn btn--primary',type:'submit',text:'登记来源'});
    const form = el('form',{class:'intake-form'},[
      field('source-type','来源类型',type), types, field('source-account','来源账号',account),
      field('source-client','提交客户端',client), clientError,
      el('p',{class:'view-desc',text:'登记后，在来源卡片上开启采集。'}), save, registerStatus,
    ]);
    let clientsLoaded = false;
    registration.addEventListener('toggle', async () => {
      if (!registration.open || clientsLoaded) return;
      const res = await api('GET','/v2/clients');
      renderResult(clientError,res);
      if (!res.ok) return;
      for (const item of res.data.clients) {
        const opt = new Option(item.host + ' · ' + item.destination,item.client_id);
        opt.dataset.host = item.host;
        client.add(opt);
      }
      clientsLoaded = true;
    });
    client.onchange = () => {
      type.readOnly = !!client.value;
      if (client.value) type.value = client.selectedOptions[0].dataset.host;
    };
    form.onsubmit = async e => {
      e.preventDefault();
      const body = {type:type.value.trim(),account:account.value.trim()};
      if (client.value) body.client_id = client.value;
      save.disabled = true;
      const res = await api('POST','/v2/sources',{body,idempotencyKey:newKey()});
      save.disabled = false;
      renderResult(registerStatus,res);
      if (res.ok) { account.value = ''; await refresh(); }
    };
    registration.append(form);
    root.append(integrations(refresh),panel,registration);
    async function refresh() {
      refreshButton.disabled = true;
      const res = await api('GET','/v2/sources');
      refreshButton.disabled = false;
      renderResult(result,res);
      summary.textContent = res.ok ? '已登记来源 · ' + res.data.sources.length : '已登记来源';
      const expanded = new Set([...list.querySelectorAll('.intake-grants[open]')].map(node => node.dataset.source));
      list.replaceChildren();
      if (!res.ok) return;
      for (const source of res.data.sources) list.append(sourceCard(source,refresh,expanded.has(source.source_id)));
      if (!res.data.sources.length) list.append(emptyMsg('暂无来源。先登记一个账号，再开启采集或导入历史。'));
    }
    return refresh();
  }

  function sourceCard(source, refresh, expanded) {
    const change = source.capture_enabled ? '暂停采集' : '开启采集';
    const control = button(change, () => submit({kind:source.capture_enabled ? 'stop_capture' : 'resume_capture',source_id:source.source_id},newKey()));
    const importButton = button('导入历史', () => {
      if (!draft.intent && !draft.sending && draft.sourceId !== source.source_id) {
        Object.assign(draft,{sourceId:source.source_id,file:null,receipt:null,last:null});
      }
      navigate('imports');
    });
    const history = source.history;
    const showHistory = history?.supported;
    const output = statusLine();
    const syncHistory = showHistory ? button(history.enabled ? '暂停历史同步' : '同步历史', async () => {
      syncHistory.disabled = true;
      const res = await api('POST','/v2/sources/'+encodeURIComponent(source.source_id)+'/history',{
        body:{enabled:!history.enabled},
      });
      renderResult(output,res);
      if (res.ok) {
        announce(history.enabled ? '已暂停历史同步' : '已开启历史同步');
        await refresh();
      } else syncHistory.disabled = false;
    }) : null;
    async function submit(body,key) {
      control.disabled = true;
      const res = await api('POST','/v2/operations',{body,idempotencyKey:key});
      renderResult(output,res);
      if (res.ok) { announce('已更新来源采集状态'); await refresh(); return; }
      if (uncertain(res)) {
        const retry = button('重试采集操作',() => {retry.disabled = true; submit(body,key);});
        output.append(retry);
      } else control.disabled = false;
    }
    const metadata = el('div',{class:'intake-meta'},[
      kv('来源 ID',source.source_id),kv('已提交游标',source.cursor || '起点'),
      kv('资料类别',source.categories.length ? source.categories.join('、') : '未指定'),
      ...(showHistory ? [
        kv('历史同步',history.enabled ? (source.capture_enabled ? '持续采集' : '等待采集授权') : '已暂停'),
        kv('已入库历史事件',new Intl.NumberFormat().format(history.events ?? 0)),
      ] : []),
    ]);
    if (history?.last_error) metadata.append(kv('上次同步错误',history.last_error));
    if (history?.skipped_containers) metadata.append(kv('暂无法读取的会话或话题次数',String(history.skipped_containers)));
    const grants = el('details',{class:'intake-grants',dataset:{source:source.source_id}},[
      el('summary',{text:'用途授权'}),
      sourceGrant(source,'extract','自动提炼',refresh),
      sourceGrant(source,'embed','向量嵌入',refresh),
    ]);
    grants.open = expanded;
    return el('article',{class:'intake-source'},[
      el('header',{class:'intake-header'},[el('h3',{text:label(source)}),pill(source.capture_enabled ? '采集中' : '已暂停',source.capture_enabled ? 'ok' : 'muted')]),
      metadata, el('div',{class:'intake-actions'},[control,syncHistory,importButton].filter(Boolean)),output,grants,
    ]);
  }

  function sourceGrant(source, purpose, title, refresh) {
    const enabled = source[purpose + '_enabled'];
    const reauthorize = purpose === 'extract' && enabled && source.extract_reauthorization_required;
    const allow = !enabled || reauthorize;
    const start = button((reauthorize ? '重新授权' : enabled ? '撤销' : '授权') + title,() => preview(allow,start));
    const revoke = reauthorize ? button('撤销' + title,() => preview(false,revoke)) : null;
    const controls = [start,revoke].filter(Boolean);
    const setDisabled = disabled => controls.forEach(control => {control.disabled = disabled;});
    const output = statusLine();
    async function preview(allow, control) {
      const command = {kind:'change_grant',source_id:source.source_id,purpose,allow};
      setDisabled(true); output.replaceChildren();
      const result = await api('POST','/v2/operations/preview',{body:command,idempotencyKey:newKey()});
      if (!result.ok) { renderResult(output,result); setDisabled(false); return; }
      const scope = result.data.scope;
      const model = result.data.model_destination;
      const changeText = scope.allow ? '允许' : '停止';
      const body = {...scope,preview_id:result.data.preview_id};
      const key = newKey();
      const confirm = button(scope.allow ? '确认授权' : '确认撤销',() => apply(body,key),true);
      const cancel = button('取消',() => {
        output.replaceChildren(); setDisabled(false); control.focus();
      });
      output.replaceChildren(el('p',{text:changeText + '使用「' + label(source) + '」的数据进行' + title + '？'}));
      if (model) {
        const destination = model.local_endpoint ? '本机模型' : '将发送提炼所需的来源片段至服务器模型';
        output.append(el('p',{text:`${destination}「${model.name}」 · ${model.endpoint} · ${model.model_id}`}));
      }
      output.append(el('div',{class:'intake-actions'},[confirm,cancel]));
      confirm.focus();
    }
    async function apply(body,key) {
      output.textContent = '正在提交授权变更…';
      const result = await api('POST','/v2/operations',{body,idempotencyKey:key});
      if (result.ok) { announce('已更新' + title + '授权'); await refresh(); return; }
      renderResult(output,result);
      if (uncertain(result)) {
        output.append(button('重试授权变更',() => apply(body,key)));
      } else {
        setDisabled(false);
        if (body.allow) output.append(button('查看模型设置',() => navigate('settings')));
      }
    }
    return el('section',{class:'intake-grant','aria-label':title},[
      el('div',{class:'intake-header'},[el('strong',{text:title}),
        pill(reauthorize ? '待重新授权' : enabled ? '已授权' : '未授权',enabled && !reauthorize ? 'ok' : 'muted'),
        ...controls]),output,
    ]);
  }

  function parseBatch(doc) {
    if (!doc || typeof doc !== 'object') throw new Error('批次文件需包含一个 JSON 对象。');
    const page = doc.page ?? doc;
    if (!Array.isArray(page?.events) || page.events.length === 0 || page.events.length > 100) {
      throw new Error('每个批次需包含 1 至 100 个事件。');
    }
    if (page.cursor != null && typeof page.cursor !== 'string' ||
        typeof page.next_cursor !== 'string' || !page.next_cursor.trim()) {
      throw new Error('核对批次的起始游标 cursor 和下一游标 next_cursor。');
    }
    for (const [index,event] of page.events.entries()) {
      if (!event || typeof event.native_object !== 'string' || !event.native_object.trim() || !Object.hasOwn(eventKinds,event.actor_kind)) {
        throw new Error('第 ' + (index+1) + ' 条事件需提供原生对象标识和有效的消息类别。');
      }
    }
    return {page,source:doc.source,sourceId:doc.source_id};
  }

  function previewProblem(file,source) {
    if (!source) return '请选择导入目标来源。';
    const {page,source:declared,sourceId} = file;
    if ([sourceId,page.source_id,declared?.source_id].some(id => id != null && id !== source.source_id)
      || declared?.type != null && declared.type !== source.type
      || declared?.account != null && declared.account !== source.account) {
      return '文件声明的来源与所选来源不一致。请选择匹配的类型和账号。';
    }
    if (!source.capture_enabled) return '来源采集已暂停。请先前往数据来源开启采集。';
    if ((page.cursor ?? '') !== source.cursor) return '批次起始游标与来源已提交游标不一致。请使用接续当前游标的文件。';
    return '';
  }

  async function openImports(root) {
    const select = el('select'), input = el('input',{type:'file',accept:'.json,application/json'});
    const notice = statusLine(), preview = el('section',{class:'intake-panel'});
    const refreshButton = button('刷新来源',readSources);
    const manage = button('管理来源',() => navigate('sources'));
    const panel = el('section',{class:'intake-panel'},[
      el('header',{class:'intake-header'},[el('h3',{text:'选择批次'}),el('div',{class:'intake-actions'},[refreshButton,manage])]),
      el('div',{class:'intake-pickers'},[
        field('intake-target','目标来源',select),
        field('intake-file','JSON 批次文件',input),
      ]),
      el('p',{class:'hint-sm',text:'支持事件页或含 source、page 的 JSON 文件。预览后确认导入。'}),notice,
    ]);
    let sources = [], readGeneration = 0, readFile = 0;
    root.append(panel,preview);
    drawImport = draw; refreshImport = readSources;
    select.onchange = () => {
      draft.sourceId = select.value; draft.receipt = draft.last = null; draw();
    };
    input.onchange = async () => {
      const generation = ++readFile;
      const picked = input.files[0];
      input.value = '';
      Object.assign(draft,{file:null,intent:null,receipt:null,last:null});
      draw();
      if (!picked) return;
      try {
        if (picked.size > limit) throw new Error('文件超过 4 MiB，请拆分批次。');
        const doc = await picked.text();
        if (generation !== readFile || !root.isConnected || root.closest('.view')?.hidden) return;
        draft.file = {...parseBatch(JSON.parse(doc)),name:picked.name};
      } catch (error) {
        draft.last = {message: error instanceof SyntaxError ? '文件不是有效的 JSON。' : error.message};
      }
      draw();
    };
    async function readSources() {
      const generation = ++readGeneration;
      refreshButton.disabled = true;
      const res = await api('GET','/v2/sources');
      if (generation !== readGeneration) return;
      refreshButton.disabled = false;
      renderResult(notice,res);
      sources = res.ok ? res.data.sources : [];
      select.replaceChildren(new Option('请选择来源',''));
      for (const s of sources) select.add(new Option(label(s),s.source_id));
      if (!draft.sourceId && sources.length === 1) draft.sourceId = sources[0].source_id;
      select.value = draft.sourceId;
      draw();
    }

    function draw() {
      const source = sources.find(s => s.source_id === select.value);
      const locked = Boolean(draft.intent) || draft.sending;
      select.disabled = locked;
      input.disabled = locked;
      preview.replaceChildren(el('h3',{text: draft.receipt ? '导入回执' : '批次预览'}));
      if (draft.receipt) {
        const r = draft.receipt;
        preview.append(el('p',{class:'intake-success',text:'批次已受理'}),
          kv('已提交游标',r.cursor),kv('新建作业',r.jobs_created),
          el('ul',{class:'intake-events'},r.events.map(ev=>el('li',{},[
            el('code',{text:ev.native_object}),el('span',{text:dispositions[ev.disposition] || ev.disposition}),
          ]))),button('查看后台作业',()=>navigate('jobs')));
        return;
      }
      if (draft.last?.message) preview.append(el('p',{class:'empty--warn',role:'status',text:draft.last.message}));
      else if (draft.last) { const err = statusLine(); renderResult(err,draft.last); preview.append(err); }
      if (!draft.file) {
        if (!draft.last) preview.append(emptyMsg('选择文件后可核对事件、来源与游标。'));
        return;
      }
      const {name,page} = draft.file;
      preview.append(el('p',{class:'intake-filename',text:name+' · '+page.events.length+' 条事件'}),
        el('div',{class:'intake-meta'},[kv('文件起始游标',page.cursor || '起点'),kv('文件下一游标',page.next_cursor),
          kv('来源已提交游标',source ? source.cursor || '起点' : '待选择')]));
      const items = el('ul',{class:'intake-events'},page.events.map(event => {
        const excerpt = event.change_kind === 'revoked' ? '撤回版本 ' + (event.native_version ?? '') :
          typeof event.text === 'string' ? event.text : Object.keys(event.checks || {}).join('、');
        return el('li',{},[el('div',{},[el('code',{text:event.native_object}),
          el('p',{class:'intake-excerpt',text:truncate(excerpt,150)})]),el('span',{text:eventKinds[event.actor_kind]})]);
      }));
      const details = el('details',{class:'intake-event-list'},[el('summary',{text:'事件明细'}),items]);
      preview.append(details);
      if (locked) {
        if (draft.sending) preview.append(el('p',{role:'status',text:'正在提交批次…'}));
        else preview.append(el('p',{text:'提交结果待确认，请使用原请求重试。'}),button('重试同一批次',sendImport,true));
        return;
      }
      const issue = previewProblem(draft.file,source);
      if (issue) preview.append(el('p',{class:'empty--warn',role:'status',text:issue}));
      const commit = button('确认导入', () => {
        const body = {source_id:source.source_id,cursor:page.cursor ?? '',next_cursor:page.next_cursor,events:page.events};
        if (new Blob([JSON.stringify(body)]).size > limit) {
          draft.last = {message:'提交正文超过 4 MiB，请拆分批次。'}; draw(); return;
        }
        draft.intent = {body,key:newKey()};
        sendImport();
      },true);
      commit.disabled = !!issue;
      preview.append(commit);
    }
    draw(); await readSources();
  }

  async function sendImport() {
    if (draft.sending || !draft.intent) return;
    draft.sending = true; draft.last = null; drawImport?.();
    const {key,body} = draft.intent;
    const res = await api('POST','/v2/events',{body,idempotencyKey:key});
    draft.sending = false;
    if (res.ok) {
      draft.receipt = res.data; draft.file = draft.intent = null;
    } else {
      draft.last = res;
      if (!uncertain(res)) draft.intent = null;
    }
    drawImport?.();
    if (res.ok || res.status === 409) await refreshImport?.();
  }

  function open(view, host) {
    drawImport = refreshImport = null;
    const root = el('div',{class:'intake-workspace'});
    host.replaceChildren(root);
    return view === 'sources' ? openSources(root) : openImports(root);
  }
  return {open};
})();
