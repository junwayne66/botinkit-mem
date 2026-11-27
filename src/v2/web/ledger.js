'use strict';

window.MemoryLedger = (() => {
  const numbers = new Intl.NumberFormat('zh-CN');
  const loaded = {pending:'待回执',partial:'部分装入',observed:'已观察'};
  const used = {pending:'待回执',observed:'已观察'};
  let root = null;
  let filters = null;
  let result = null;
  let summary = null;
  let page = 1;
  let request = 0;

  function control(label, input) {
    return el('label',{class:'ledger-field'},[el('span',{text:label}),input]);
  }

  function select(label, options) {
    return el('select',{class:'ledger-select','aria-label':label},[
      new Option('全部状态',''),...Object.entries(options).map(([key,name]) => new Option(name,key)),
    ]);
  }

  function button(text, action) {
    const node = el('button',{class:'btn',type:'button',text});
    node.addEventListener('click',action);
    return node;
  }

  function label(value, options) {
    const tone = value === 'observed' ? 'ok' : value === 'partial' ? 'warn' : 'muted';
    return el('span',{class:'ledger-pill ledger-pill--'+tone,text:options[value] || '待回执'});
  }

  function buildFilters() {
    const host = el('input',{class:'ledger-input',placeholder:'输入宿主名称','aria-label':'宿主名称'});
    const load = select('装入状态筛选',loaded);
    const use = select('采用状态筛选',used);
    filters = {host,load,use};
    const apply = el('button',{class:'btn btn--primary',type:'submit',text:'应用筛选'});
    const reset = button('重置',() => {
      host.value = load.value = use.value = '';
      page = 1;
      void refresh();
    });
    const form = el('form',{class:'ledger-filters'},[
      control('宿主',host),control('装入回执',load),control('采用回执',use),
      el('div',{class:'ledger-filter-actions'},[apply,reset]),
    ]);
    form.addEventListener('submit',event => {
      event.preventDefault();
      page = 1;
      void refresh();
    });
    return form;
  }

  function metrics(data) {
    return [
      ['已提供',data.provided],
      ['装入已观察',data.loaded],
      ['采用已观察',data.used],
    ].map(([name,value]) => el('div',{class:'ledger-metric'},[
      el('span',{class:'ledger-metric-label',text:name}),
      el('strong',{class:'ledger-metric-value',text:numbers.format(value)}),
    ]));
  }

  function details(row) {
    const data = el('dl',{class:'ledger-detail-pairs'},[
      el('dt',{text:'模型路由'}),el('dd',{text:row.route_ref}),
      el('dt',{text:'会话'}),el('dd',{text:row.session_id}),
      el('dt',{text:'修订标识'}),el('dd',{text:row.provided_revision_ids.join('、') || '无'}),
    ]);
    for (const [heading,value] of [['装入回执',row.loaded],['采用回执',row.used]]) {
      if (value) data.append(el('dt',{text:heading}),el('dd',{},[el('pre',{text:pretty(value)})]));
    }
    return el('details',{},[el('summary',{text:'查看详情'}),data]);
  }

  function table(deliveries) {
    return el('div',{class:'ledger-table-scroll'},[
      el('table',{class:'ledger-table'},[
        el('thead',{},[el('tr',{},['提供时间','宿主','会话','提供修订','装入回执','采用回执','详情'].map(
          text => el('th',{scope:'col',text}),
        ))]),
        el('tbody',{},deliveries.map(row => {
          const session = row.session_id.length > 12 ? '…'+row.session_id.slice(-10) : row.session_id;
          return el('tr',{},[
            el('td',{},[el('time',{datetime:row.provided_at,text:new Date(row.provided_at).toLocaleString('zh-CN',{hour12:false})})]),
            el('td',{text:row.host}),el('td',{},[el('code',{text:session})]),
            el('td',{text:String(row.provided_revision_ids.length)}),
            el('td',{},[label(row.loaded_state,loaded)]),el('td',{},[label(row.used_state,used)]),
            el('td',{},[details(row)]),
          ]);
        })),
      ]),
    ]);
  }

  function pagination(data) {
    const pages = Math.max(1,Math.ceil(data.total / data.page_size));
    const previous = button('上一页',() => { page -= 1; void refresh(); });
    const next = button('下一页',() => { page += 1; void refresh(); });
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    return el('div',{class:'ledger-pagination'},[
      previous,el('span',{text:'第 '+page+' 页 / 共 '+pages+' 页'}),next,
    ]);
  }

  async function refresh() {
    if (!root) return;
    const generation = ++request;
    const params = new URLSearchParams({page:String(page)});
    for (const [key,input] of [['host',filters.host],['loaded',filters.load],['used',filters.use]]) {
      if (input.value.trim()) params.set(key,input.value.trim());
    }
    result.hidden = false;
    setLoading(result,'正在读取台账…');
    const res = await api('GET','/v2/deliveries/ledger?'+params);
    if (generation !== request || !root) return;
    if (!res.ok) {
      renderResult(result,res);
      return;
    }
    summary.replaceChildren(...metrics(res.data.summary));
    result.replaceChildren(
      res.data.deliveries.length ? table(res.data.deliveries) : emptyMsg('最近 7 天内没有符合条件的投递记录。'),
      pagination(res.data),
    );
  }

  function open(container) {
    root = container;
    page = 1;
    root.className = 'ledger-root';
    summary = el('div',{class:'ledger-summary'});
    result = el('div',{class:'ledger-result','aria-live':'polite'});
    root.replaceChildren(summary,buildFilters(),result);
    void refresh();
  }

  function leave() {
    request += 1;
    root = null;
  }

  return {open,refresh,leave};
})();
