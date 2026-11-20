'use strict';

window.MemoryIntake = (() => {
  const eventKinds = {user_message:'本人消息', assistant_message:'助手消息', tool_result:'工具回执'};
  const dispositions = {accepted:'已受理', duplicate:'已存在', revoked:'已撤回', revoked_blocked:'已屏蔽', revoked_unknown:'未找到撤回目标'};
  const draft = { sourceId:'', file:null, intent:null, sending:false, receipt:null, last:null };
  let drawImport, refreshImport;
  // 每次挂载独享一个中止信号：open() 替换根节点前中止上一轮挂载，
  // document 级监听器与轮询定时器随之立即释放，不再滞留已分离的视图树。
  let mountAbort = null;
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
  // 视图可见且页面在前台时才轮询，避免后台标签页空转。
  function viewVisible(node) {
    return !!node && node.isConnected && !node.closest('.view')?.hidden && !document.hidden;
  }
  function relativeTime(ms) {
    if (!Number.isFinite(ms)) return '';
    const diff = Date.now() - ms;
    if (diff < 45000) return '刚刚';
    const minutes = Math.floor(diff / 60000);
    if (minutes < 60) return minutes + ' 分钟前';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' 小时前';
    const days = Math.floor(hours / 24);
    if (days < 30) return days + ' 天前';
    return new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'short'}).format(new Date(ms));
  }
  // 历史同步错误 code → 中文 + 下一步；未知 code 原样呈现，不臆造含义。
  const historyErrors = {
    feishu_cursor_expired:'飞书分页游标已失效，将从本次时间窗口起点重新读取。',
    feishu_container_unavailable:'当前账号无法读取部分飞书会话或话题。',
    feishu_authorization_required:'飞书授权不足，请检查账号及历史读取权限。',
    action_denied:'历史读取被拒绝，请在数据来源中重新授权这个飞书账号。',
    temporarily_unavailable:'历史读取暂时不可用，稍后会自动重试。',
    invalid_input:'历史数据未通过校验，请检查来源会话文件。',
    not_available:'历史读取暂不可用，请确认来源类型受支持。',
    model_not_ready:'模型尚未就绪，请先在设置中测试并绑定模型。',
    internal_error:'历史读取出现内部错误，可稍后重试。',
  };
  function historyErrorText(code) {
    return historyErrors[code] ? historyErrors[code] + '（' + code + '）' : String(code);
  }

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

  function integrations(onChange, signal) {
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
      let installing = false;
      const install = button('添加集成', async () => {
        if (installing) return;
        installing = true;
        busy(install,true,'正在写入配置…');
        output.textContent = '正在写入本机接入配置…';
        const res = await api('POST','/v2/integrations/'+id+'/install',{
          body:{capture:capture.checked,share_statements:share.checked},
        });
        busy(install,false);
        installing = false;
        renderResult(output,res);
        if (res.ok) {
          output.textContent = '已配置。'+details.usage;
          await refresh();
          await onChange();
        }
      },true);
      add.append(
        el('div',{class:'intake-connect-options'},[
          el('label',{},[capture,' 开启新会话采集']),
          el('label',{},[share,' 允许助手读取已授权声明']),
        ]),install,output
      );
      cards[id] = {availability,add,install,isInstalling:() => installing};
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
    let starting = false;
    const start = button('发起授权', async () => {
      if (starting) return;
      starting = true;
      start.disabled = true;
      setText(feishuStatus,'正在发起飞书授权…');
      const res = await api('POST','/v2/integrations/feishu/start');
      starting = false;
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
    let lastFeishuBusy = false;
    function schedulePoll() {
      clearTimeout(poll);
      poll = setTimeout(() => {
        if (viewVisible(region)) void refresh();
      },1200);
    }
    function updateFeishu(state) {
      clearTimeout(poll);
      const changed = lastStage !== state.stage;
      lastStage = state.stage;
      lastFeishuBusy = !!state.busy;
      if (!starting) {
        start.disabled = state.busy || !state.available;
        start.textContent = state.busy ? '授权进行中' : state.stage === 'connected' ? '重新授权' : '发起授权';
      }
      const base = feishuStages[state.stage] || (state.available
        ? state.registered ? '已有飞书来源。可重新授权当前账号。' : '等待授权。'
        : '需要先安装官方 CLI：npm install -g @larksuite/cli');
      // 仅在文本变化时写入，避免 role=status 每轮重复播报。
      setText(feishuStatus, state.stage === 'failed'
        ? state.result?.message || base : state.result?.account_name ? base+' '+state.result.account_name : base);
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
        schedulePoll();
      } else if (changed && state.stage === 'connected') {
        void onChange();
      }
    }

    async function refresh() {
      const res = await api('GET','/v2/integrations');
      renderResult(general,res);
      if (!res.ok) {
        // 单次读取失败不终止授权轮询：按最近一次 busy 状态继续重排。
        if (lastFeishuBusy && viewVisible(region)) schedulePoll();
        return;
      }
      for (const state of res.data.hosts) {
        const card = cards[state.host];
        if (!card) continue;
        card.availability.textContent = state.configured ? '已写入配置' : state.available ? '已检测到' : '未检测到';
        // 安装进行中不改写按钮禁用态与文案，避免轮询把忙碌按钮重新启用。
        if (!card.isInstalling()) {
          card.install.disabled = !state.available;
          card.install.textContent = state.configured ? '更新集成' : '添加集成';
        }
        card.add.hidden = !state.available;
      }
      updateFeishu(res.data.feishu);
    }

    // 页面回到前台时按最近一次 busy 状态恢复飞书授权轮询；
    // 监听器与定时器随挂载信号中止，视图替换时立即释放。
    document.addEventListener('visibilitychange',() => {
      if (lastFeishuBusy) schedulePoll();
    },{signal});
    signal.addEventListener('abort',() => clearTimeout(poll));

    start.disabled = true;
    queueMicrotask(refresh);
    return region;
  }

  // 骨架宽度走 CSS 类：服务端 CSP 禁止内联 style 属性。
  function skeletonCard() {
    return el('article',{class:'intake-source intake-skeleton','aria-hidden':'true'},[
      el('span',{class:'placeholder'}),
      el('span',{class:'placeholder'}),
      el('span',{class:'placeholder'}),
    ]);
  }

  function openSources(root, signal) {
    const summary = el('strong',{text:'正在读取来源…'});
    const list = el('div',{class:'intake-list'});
    const result = statusLine();
    const refreshButton = button('刷新来源',() => void refresh());
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
    async function loadClients() {
      const res = await api('GET','/v2/clients');
      renderResult(clientError,res);
      if (!res.ok) return;
      const previous = client.value;
      client.replaceChildren(new Option('仅本机所有者',''));
      for (const item of res.data.clients) {
        const opt = new Option(item.host + ' · ' + item.destination,item.client_id);
        opt.dataset.host = item.host;
        client.add(opt);
      }
      client.value = previous;
      type.readOnly = !!client.value;
      clientsLoaded = true;
    }
    // 安装集成 / 飞书登记后客户端列表可能变化：作废缓存，展开时重新读取。
    function invalidateClients() {
      clientsLoaded = false;
      if (registration.open) void loadClients();
    }
    registration.addEventListener('toggle',() => {
      if (registration.open && !clientsLoaded) void loadClients();
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
    root.append(integrations(async () => { invalidateClients(); await refresh(); },signal),panel,registration);

    let loaded = false;
    let historyLive = false;
    let historyTimer = 0;
    // 历史同步开启时约 5s 静默增量刷新；视图隐藏或页面后台即停。
    function scheduleHistoryRefresh() {
      clearTimeout(historyTimer);
      historyTimer = 0;
      if (!historyLive || !viewVisible(panel)) return;
      historyTimer = setTimeout(() => {
        historyTimer = 0;
        if (!historyLive || !viewVisible(panel)) return;
        void refresh();
      },5000);
    }
    // 页面可见性变化时暂停 / 恢复历史自动刷新；监听器与定时器随挂载信号中止。
    document.addEventListener('visibilitychange',() => scheduleHistoryRefresh(),{signal});
    signal.addEventListener('abort',() => clearTimeout(historyTimer));

    async function refresh() {
      refreshButton.disabled = true;
      const res = await api('GET','/v2/sources');
      refreshButton.disabled = false;
      renderResult(result,res);
      if (!res.ok) {
        summary.textContent = '已登记来源';
        if (!loaded) list.replaceChildren();
        scheduleHistoryRefresh();
        return;
      }
      loaded = true;
      summary.textContent = '已登记来源 · ' + res.data.sources.length;
      historyLive = res.data.sources.some(source => source.history?.enabled);
      reconcileList(list,res.data.sources,source => source.source_id,
        source => sourceCard(source,refresh),
        (node,source) => node.__intakeUpdate(source));
      if (!res.data.sources.length) {
        list.replaceChildren(emptyMsg('暂无来源。先登记一个账号，再开启采集或导入历史。',{
          label:'登记新来源',
          run:() => {
            registration.open = true;
            registration.scrollIntoView({block:'nearest'});
            type.focus();
          },
        }));
      }
      scheduleHistoryRefresh();
    }
    list.replaceChildren(skeletonCard(),skeletonCard());
    setLoading(result,'正在读取来源…');
    return refresh();
  }

  function sourceCard(source, refresh) {
    let current = source;
    let toggling = false;
    let historyToggling = false;
    const capturePill = pill(source.capture_enabled ? '采集中' : '已暂停',source.capture_enabled ? 'ok' : 'muted');
    const output = statusLine();
    const metadata = el('div',{class:'intake-meta'});

    function setCaptureUi(enabled) {
      capturePill.textContent = enabled ? '采集中' : '已暂停';
      capturePill.className = 'pill pill--' + (enabled ? 'ok' : 'muted');
      control.textContent = enabled ? '暂停采集' : '开启采集';
    }
    // 采集开关乐观更新：立即翻转 pill 与按钮文案，失败回滚 + 错误 toast + 原键重试。
    async function runCapture(body,key,enabling) {
      if (toggling) return;
      toggling = true;
      output.replaceChildren();
      setCaptureUi(enabling);
      busy(control,true,enabling ? '正在开启…' : '正在暂停…');
      const res = await api('POST','/v2/operations',{body,idempotencyKey:key});
      busy(control,false);
      toggling = false;
      if (res.ok) {
        output.replaceChildren(el('p',{class:'result-ok',text:enabling ? '已开启采集' : '已暂停采集'}));
        announce(enabling ? '已开启采集' : '已暂停采集');
        await refresh();
        return;
      }
      setCaptureUi(!enabling);
      renderResult(output,res);
      toast(errorHeadline(res),'err');
      if (uncertain(res)) {
        output.append(button('重试采集操作',() => void runCapture(body,key,enabling)));
      }
    }
    const control = button(source.capture_enabled ? '暂停采集' : '开启采集',() => {
      if (toggling) return;
      const enabling = !current.capture_enabled;
      void runCapture({kind:enabling ? 'resume_capture' : 'stop_capture',source_id:current.source_id},newKey(),enabling);
    });
    control.dataset.focusKey = 'capture';

    const history = source.history;
    const showHistory = history?.supported;
    function setHistoryUi(enabled) {
      if (syncHistory) syncHistory.textContent = enabled ? '暂停历史同步' : '同步历史';
    }
    async function runHistory(body,key,enabling) {
      if (historyToggling) return;
      historyToggling = true;
      output.replaceChildren();
      setHistoryUi(enabling);
      busy(syncHistory,true,enabling ? '正在开启…' : '正在暂停…');
      const res = await api('POST','/v2/sources/'+encodeURIComponent(current.source_id)+'/history',{
        body,idempotencyKey:key,
      });
      busy(syncHistory,false);
      historyToggling = false;
      if (res.ok) {
        output.replaceChildren(el('p',{class:'result-ok',text:enabling ? '已开启历史同步' : '已暂停历史同步'}));
        announce(enabling ? '已开启历史同步' : '已暂停历史同步');
        await refresh();
        return;
      }
      setHistoryUi(!enabling);
      renderResult(output,res);
      toast(errorHeadline(res),'err');
      if (uncertain(res)) {
        output.append(button('重试历史同步',() => void runHistory(body,key,enabling)));
      }
    }
    const syncHistory = showHistory ? button(history.enabled ? '暂停历史同步' : '同步历史',() => {
      if (historyToggling) return;
      const enabling = !current.history?.enabled;
      void runHistory({enabled:enabling},newKey(),enabling);
    }) : null;
    if (syncHistory) syncHistory.dataset.focusKey = 'history';

    const importButton = button('导入历史',() => {
      if (!draft.intent && !draft.sending && draft.sourceId !== current.source_id) {
        Object.assign(draft,{sourceId:current.source_id,file:null,receipt:null,last:null});
      }
      navigate('imports');
    });
    importButton.dataset.focusKey = 'import';

    function buildMeta(next) {
      const h = next.history;
      const rows = [
        kv('来源 ID',next.source_id),
        kv('已提交游标',next.cursor || '起点'),
        kv('资料类别',next.categories.length ? next.categories.join('、') : '未指定'),
      ];
      if (h?.supported) {
        rows.push(kv('历史同步',h.enabled ? (next.capture_enabled ? '持续采集' : '等待采集授权') : '已暂停'));
        rows.push(kv('已入库历史事件',new Intl.NumberFormat().format(h.events ?? 0)));
        if (h.last_scan) rows.push(kv('上次扫描',relativeTime(h.last_scan * 1000)));
        if (h.last_error) rows.push(kv('上次同步错误',historyErrorText(h.last_error)));
        if (h.skipped_containers) rows.push(kv('暂无法读取的会话或话题次数',String(h.skipped_containers)));
      }
      return rows.filter(Boolean);
    }
    metadata.replaceChildren(...buildMeta(source));

    const extractGrant = sourceGrant(source,'extract','自动提炼',refresh);
    const embedGrant = sourceGrant(source,'embed','向量嵌入',refresh);
    const grants = el('details',{class:'intake-grants',dataset:{source:source.source_id}},[
      el('summary',{text:'用途授权'}),
      extractGrant.node,
      embedGrant.node,
    ]);

    const node = el('article',{class:'intake-source'},[
      el('header',{class:'intake-header'},[el('h3',{text:label(source)}),capturePill]),
      metadata, el('div',{class:'intake-actions'},[control,syncHistory,importButton].filter(Boolean)),output,grants,
    ]);
    // 原位增量更新：不重建卡片，保留结果行（含重试按钮）、焦点与展开状态。
    node.__intakeUpdate = next => {
      current = next;
      if (!toggling) setCaptureUi(next.capture_enabled);
      metadata.replaceChildren(...buildMeta(next));
      if (!historyToggling) setHistoryUi(next.history?.enabled);
      extractGrant.update(next);
      embedGrant.update(next);
    };
    return node;
  }

  function sourceGrant(source, purpose, title, refresh) {
    let current = source;
    let dialogOpen = false;
    const badge = pill('', 'muted');
    const output = statusLine();
    function state() {
      const enabled = current[purpose + '_enabled'];
      const reauthorize = purpose === 'extract' && enabled && current.extract_reauthorization_required;
      return {enabled,reauthorize,allow:!enabled || reauthorize};
    }
    const start = button('',() => void preview(start,state().allow));
    start.dataset.focusKey = purpose + '-trigger';
    const revoke = button('撤销' + title,() => void preview(revoke,false));
    revoke.dataset.focusKey = purpose + '-revoke';
    revoke.hidden = true;
    const controls = [start,revoke];
    const setDisabled = disabled => controls.forEach(control => {control.disabled = disabled;});
    function paint() {
      const {enabled,reauthorize} = state();
      badge.textContent = reauthorize ? '待重新授权' : enabled ? '已授权' : '未授权';
      badge.className = 'pill pill--' + (enabled && !reauthorize ? 'ok' : 'muted');
      start.textContent = (reauthorize ? '重新授权' : enabled ? '撤销' : '授权') + title;
      revoke.hidden = !reauthorize;
    }
    paint();

    async function preview(trigger,allow) {
      if (dialogOpen) return;
      const command = {kind:'change_grant',source_id:current.source_id,purpose,allow};
      const heading = (allow ? (state().reauthorize ? '重新授权' : '授权') : '撤销') + title;
      setDisabled(true);
      output.replaceChildren();
      busy(trigger,true,'正在预览…');
      const result = await api('POST','/v2/operations/preview',{body:command,idempotencyKey:newKey()});
      busy(trigger,false);
      if (!result.ok) {
        // 模型未就绪在预览阶段即拦截：不开对话框， inline 中文指引 + 前往设置。
        renderResult(output,result);
        if (result.code === 'model_not_ready') {
          output.append(button('查看模型设置',() => navigate('settings')));
        }
        setDisabled(false);
        return;
      }
      dialogOpen = true;
      openGrantDialog(result.data,trigger,heading);
    }

    // 授权影响预览对话框：kv 行 + 数据去向警示；revision_changed 在框内提示并
    // 自动重新预览一次（共 2 次封顶），绝不自动确认；超出后提供手动重新预览。
    function openGrantDialog(data,trigger,heading) {
      let previewData = data;
      let body = {...data.scope,preview_id:data.preview_id};
      let key = newKey();
      let previewsUsed = 1;
      let submitting = false;
      let settled = false;
      let successResult = null;
      const titleId = 'grant-dlg-' + newKey();
      const headingNode = el('h2',{id:titleId,class:'dialog-title',text:heading});
      const content = el('div',{class:'intake-grant-preview'});
      const notice = el('div',{class:'intake-grant-notice'});
      const confirmBtn = el('button',{type:'button',class:'btn btn--primary'});
      const cancelBtn = el('button',{type:'button',class:'btn',text:'取消'});
      const repreviewBtn = button('重新预览',() => void repreview());
      repreviewBtn.hidden = true;
      const actions = el('div',{class:'dialog-actions'},[cancelBtn,repreviewBtn,confirmBtn]);
      const dialog = el('dialog',{class:'dialog','aria-labelledby':titleId},[
        el('div',{class:'dialog-card'},[headingNode,content,notice,actions]),
      ]);

      function paintDialog() {
        const scope = previewData.scope;
        confirmBtn.textContent = scope.allow ? '确认授权' : '确认撤销';
        const model = previewData.model_destination;
        content.replaceChildren(
          el('div',{class:'intake-meta'},[
            kv('来源',label(current)),
            kv('用途',title),
            kv('操作',(scope.allow ? '允许' : '停止') + title),
          ].filter(Boolean)),
          model && model.local_endpoint === false
            ? el('div',{class:'intake-grant-warn',role:'note'},[
                el('strong',{text:'来源片段将发送至服务器模型'}),
                el('div',{class:'intake-meta'},[
                  kv('模型名称',model.name),kv('服务端点',model.endpoint),kv('模型标识',model.model_id),
                ].filter(Boolean)),
              ])
            : model
              ? el('p',{class:'hint-sm',text:'数据仅发送至本机模型：' + model.name + ' · ' + model.endpoint + ' · ' + model.model_id})
              : null
        );
      }
      function showRevisionNotice() {
        notice.replaceChildren(el('p',{class:'empty--warn',role:'status',text:'来源状态已更新，请核对最新授权范围'}));
      }
      function showError(result,retryRun,retryLabel) {
        const err = statusLine();
        renderResult(err,result);
        notice.replaceChildren(err);
        if (result.code === 'model_not_ready') {
          notice.append(button('查看模型设置',() => {close(false);navigate('settings');}));
          confirmBtn.disabled = true;
        } else if (uncertain(result)) {
          // 同一意图原键重试；确认按钮本身也可再次点击（同正文同键）。
          notice.append(button(retryLabel,retryRun));
        } else {
          confirmBtn.disabled = true;
          repreviewBtn.hidden = false;
        }
      }
      async function repreview() {
        if (submitting) return;
        submitting = true;
        repreviewBtn.hidden = true;
        confirmBtn.disabled = true;
        cancelBtn.disabled = true;
        busy(confirmBtn,true,'正在重新预览…');
        const command = {kind:'change_grant',source_id:current.source_id,purpose,allow:previewData.scope.allow};
        const result = await api('POST','/v2/operations/preview',{body:command,idempotencyKey:newKey()});
        busy(confirmBtn,false);
        submitting = false;
        cancelBtn.disabled = false;
        previewsUsed += 1;
        if (!result.ok) {
          showError(result,() => void repreview(),'重试重新预览');
          if (result.code !== 'model_not_ready') repreviewBtn.hidden = false;
          return;
        }
        previewData = result.data;
        body = {...result.data.scope,preview_id:result.data.preview_id};
        key = newKey(); // 正文（preview_id）变化即换键；同意图重试仍复用键。
        paintDialog();
        confirmBtn.disabled = false;
        confirmBtn.focus();
      }
      async function apply() {
        if (submitting || settled) return;
        submitting = true;
        notice.replaceChildren();
        cancelBtn.disabled = true;
        repreviewBtn.disabled = true;
        busy(confirmBtn,true,'正在提交授权变更…');
        const result = await api('POST','/v2/operations',{body,idempotencyKey:key});
        busy(confirmBtn,false);
        submitting = false;
        if (settled) return;
        cancelBtn.disabled = false;
        repreviewBtn.disabled = false;
        if (result.ok) {
          successResult = result;
          close(true);
          return;
        }
        if (result.code === 'revision_changed') {
          showRevisionNotice();
          if (previewsUsed < 2) {
            await repreview(); // 自动重新预览一次；确认必须由用户再次点击。
          } else {
            confirmBtn.disabled = true;
            repreviewBtn.hidden = false;
            repreviewBtn.focus();
          }
          return;
        }
        showError(result,() => void apply(),'重试授权变更');
      }
      let cleaned = false;
      function close(success) {
        if (settled) return;
        settled = true;
        if (!success) successResult = null;
        if (dialog.open) dialog.close();
        cleanup(); // 同步收尾：close 事件异步触发，焦点归还与回执不能等它。
      }
      function cleanup() {
        if (cleaned) return;
        cleaned = true;
        dialog.remove();
        dialogOpen = false;
        setDisabled(false);
        // 冻结要求：取消 / Escape / 遮罩关闭后焦点归还触发按钮。
        if (trigger.isConnected) trigger.focus({preventScroll:true});
        if (successResult) {
          const allow = body.allow;
          toast('已更新' + title + '授权','ok');
          announce('已更新' + title + '授权');
          output.replaceChildren(...[
            el('p',{class:'result-ok',text:(allow ? '已允许' : '已停止') + title}),
            successResult.data?.operation_id ? kv('操作标识',successResult.data.operation_id) : null,
          ].filter(Boolean));
          void refresh();
        }
      }
      dialog.addEventListener('cancel',event => {
        event.preventDefault();
        if (!submitting) close(false);
      });
      dialog.addEventListener('close',cleanup);
      dialog.addEventListener('click',event => {
        if (event.target === dialog && !submitting) close(false);
      });
      cancelBtn.addEventListener('click',() => close(false));
      confirmBtn.addEventListener('click',() => void apply());
      document.body.append(dialog);
      dialog.showModal();
      paintDialog();
      confirmBtn.focus();
    }

    const node = el('section',{class:'intake-grant','aria-label':title},[
      el('div',{class:'intake-header'},[el('strong',{text:title}),badge,start,revoke]),output,
    ]);
    return {
      node,
      update:next => {
        current = next;
        paint(); // 不改写 disabled / 结果行：预览与对话框生命周期自管理。
      },
    };
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
      const n = index + 1;
      if (!event || typeof event.native_object !== 'string' || !event.native_object.trim() || !Object.hasOwn(eventKinds,event.actor_kind)) {
        throw new Error('第 ' + n + ' 条事件需提供原生对象标识和有效的消息类别。');
      }
      // 与服务端 ingestion 内容规则对齐，提前给出中文校验，避免提交后才收到英文 422。
      if (event.at != null && (typeof event.at !== 'string' ||
          !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(event.at.trim()) || Number.isNaN(Date.parse(event.at)))) {
        throw new Error('第 ' + n + ' 条事件的 at 需为带时区的 ISO 8601 时间字符串。');
      }
      const changeKind = event.change_kind ?? 'created';
      if (!['created','updated','revoked'].includes(changeKind)) {
        throw new Error('第 ' + n + ' 条事件的 change_kind 只能是 created、updated 或 revoked。');
      }
      if (event.native_version != null && typeof event.native_version !== 'string' && typeof event.native_version !== 'number') {
        throw new Error('第 ' + n + ' 条事件的 native_version 需为字符串或数字。');
      }
      if (event.task_id != null && (typeof event.task_id !== 'string' || !event.task_id.trim())) {
        throw new Error('第 ' + n + ' 条事件的 task_id 需为非空字符串。');
      }
      const hasText = Object.hasOwn(event,'text');
      const hasChecks = Object.hasOwn(event,'checks');
      if (hasText && hasChecks) {
        throw new Error('第 ' + n + ' 条事件不能同时包含 text 和 checks。');
      }
      if (changeKind === 'revoked') {
        const version = event.native_version;
        if (!(typeof version === 'number' || (typeof version === 'string' && version.trim()))) {
          throw new Error('第 ' + n + ' 条事件：撤回需指明要撤回的 native_version。');
        }
        continue;
      }
      if (event.actor_kind === 'tool_result') {
        const checks = event.checks;
        if (!checks || typeof checks !== 'object' || Array.isArray(checks) || Object.keys(checks).length === 0) {
          throw new Error('第 ' + n + ' 条事件：工具回执需提供非空的 checks 对象。');
        }
      } else if (typeof event.text !== 'string') {
        throw new Error('第 ' + n + ' 条事件：' + eventKinds[event.actor_kind] + '需提供 text 文本。');
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
    const refreshButton = button('刷新来源',() => void readSources());
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
      if (!sources.length) setLoading(notice,'正在读取来源…');
      const res = await api('GET','/v2/sources');
      if (generation !== readGeneration) return;
      refreshButton.disabled = false;
      if (!res.ok) {
        // 瞬时读取失败保留已有来源与选项，只在提示行报错，不清空选择器。
        renderResult(notice,res);
        draw();
        return;
      }
      renderResult(notice,res);
      sources = res.data.sources;
      select.replaceChildren(new Option('请选择来源',''));
      for (const s of sources) select.add(new Option(label(s),s.source_id));
      if (!draft.sourceId && sources.length === 1) draft.sourceId = sources[0].source_id;
      select.value = draft.sourceId;
      draw();
    }

    async function abandon() {
      const ok = await confirmDialog({
        title:'放弃此批次？',
        desc:'将清除待重试的导入意图，已选文件不会提交。之后可重新选择来源与文件。',
        confirmText:'放弃批次',
        cancelText:'继续重试',
        danger:true,
      });
      if (!ok) return;
      draft.intent = null; draft.last = null;
      draw();
      select.focus();
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
          kv('来源已提交游标',source ? source.cursor || '起点' : '待选择')].filter(Boolean)));
      const items = el('ul',{class:'intake-events'},page.events.map(event => {
        const excerpt = event.change_kind === 'revoked' ? '撤回版本 ' + (event.native_version ?? '') :
          typeof event.text === 'string' ? event.text : Object.keys(event.checks || {}).join('、');
        return el('li',{},[el('div',{},[el('code',{text:event.native_object}),
          el('p',{class:'intake-excerpt',text:truncate(excerpt,150)})]),el('span',{text:eventKinds[event.actor_kind]})]);
      }));
      const details = el('details',{class:'intake-event-list'},[el('summary',{text:'事件明细'}),items]);
      preview.append(details);
      if (locked) {
        if (draft.sending) {
          preview.append(el('p',{class:'hint-sm loading-line',role:'status'},[
            el('span',{class:'btn__spin','aria-hidden':'true'}),
            el('span',{text:'正在提交批次…'}),
          ]));
        } else {
          preview.append(el('p',{text:'提交结果待确认，请使用原请求重试。'}),
            button('重试同一批次',sendImport,true),
            button('放弃此批次',() => void abandon()));
        }
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
    mountAbort?.abort();
    mountAbort = new AbortController();
    const root = el('div',{class:'intake-workspace'});
    host.replaceChildren(root);
    return view === 'sources' ? openSources(root,mountAbort.signal) : openImports(root);
  }
  return {open};
})();
