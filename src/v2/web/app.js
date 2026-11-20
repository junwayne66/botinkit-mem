'use strict';
/*
 * bmem v2 本地管理台。纯浏览器脚本，无构建、无依赖。
 * 由 Rust 程序内嵌并随 / 一同分发。所有者会话 cookie 由 CLI 预先建立
 * （GET /session?code 后重定向到 /），因此这里只用同源 fetch 携带 cookie，
 * 绝不读取、记录或保存任何凭据。
 *
 * 设计原则：
 *  - 所有服务返回的字符串一律用 textContent 渲染，不注入 HTML。
 *  - 状态码 / 错误 code / request_id 如实报告服务，不臆造成功。
 *  - 写请求携带 Content-Type: application/json 与每次意图唯一的 Idempotency-Key；
 *    仅对“重试同一提交”复用原键，正文变化即换键。
 *  - 设置修改走 compare-and-swap；409 时保留用户编辑，需显式重新读取再决定。
 */

// ---------- 小工具 ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const newKey = () =>
  crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : 'k-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);

function el(tag, props, kids) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v;
      else if (k === 'text')
        node.textContent = v; // 安全：仅文本
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v);
    }
  }
  for (const kid of [].concat(kids || [])) {
    if (kid == null) continue;
    node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
  }
  return node;
}

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}
function announce(msg) {
  const l = $('#sr-live');
  if (l) l.textContent = msg;
}

function pretty(v) {
  try {
    return JSON.stringify(v, null, 2);
  } catch (_) {
    return String(v);
  }
}
function truncate(s, n = 4000) {
  s = String(s);
  return s.length > n ? s.slice(0, n) + '\n…（已截断）' : s;
}

// 把任意可能是 对象映射 / 数组 / null 的集合规整成数组。
function toArray(x) {
  if (Array.isArray(x)) return x;
  if (x && typeof x === 'object') {
    const looksLikeMap = Object.values(x).every((v) => v && typeof v === 'object');
    if (looksLikeMap) return Object.entries(x).map(([id, val]) => Object.assign({ id }, val));
    return [x];
  }
  return [];
}

function listFrom(data, hints) {
  if (Array.isArray(data)) return data;
  if (data && typeof data === 'object') {
    for (const key of hints) {
      if (Array.isArray(data[key])) return data[key];
    }
    for (const key of hints) {
      if (data[key] && typeof data[key] === 'object') {
        const arr = Object.entries(data[key]).map(([id, v]) =>
          v && typeof v === 'object' ? Object.assign({ id }, v) : { id, value: v }
        );
        return arr;
      }
    }
  }
  return null;
}

// ---------- 单一同源 fetch 封装 ----------
async function api(method, path, opts) {
  opts = opts || {};
  const init = { method, credentials: 'same-origin', headers: Object.assign({}, opts.headers || {}) };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  if (opts.idempotencyKey) init.headers['Idempotency-Key'] = opts.idempotencyKey;

  let res,
    raw = '',
    data = null,
    networkError = null;
  try {
    res = await fetch(path, init);
  } catch (e) {
    networkError = e && e.message ? e.message : String(e);
  }
  if (res) {
    try {
      raw = await res.text();
    } catch (_) {
      raw = '';
    }
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (_) {
        data = null;
      }
    }
  }

  const code = data && (data.code || (data.error && data.error.code));
  const requestId = data && (data.request_id || (data.error && data.error.request_id));
  const retryable = data ? (data.retryable != null ? data.retryable : data.error && data.error.retryable) : undefined;

  return {
    method,
    path,
    ok: !!(res && res.ok),
    status: res ? res.status : 0,
    statusText: res ? res.statusText : '',
    networkError,
    data,
    raw,
    code,
    requestId,
    retryable: retryable === true
  };
}

// ---------- 请求结果行（如实报告状态 / code / 正文） ----------
function renderResult(node, res) {
  clear(node);
  node.hidden = res.ok && res.method === 'GET';
  if (node.hidden) return;
  const message = res.ok
    ? '已提交'
    : res.status === 401
      ? '请运行 bmem web，重新打开所有者会话。'
      : res.networkError
        ? '无法连接本机服务。'
        : res.data?.message || res.code || '服务返回 HTTP ' + res.status;
  node.append(el('p', { class: res.ok ? 'result-ok' : 'empty--warn', text: message }));
  if (!res.ok) {
    node.append(
      el('details', { class: 'diagnostic' }, [
        el('summary', { text: '技术详情' }),
        el('pre', {
          text: [res.method + ' ' + res.path, res.status || res.networkError, res.code, res.requestId]
            .filter(Boolean)
            .join(' · ')
        })
      ])
    );
  }
}
function setConn(text, kind) {
  const c = $('#conn');
  if (!c) return;
  clear(c);
  c.textContent = text;
  c.className = 'conn ' + (kind ? 'conn--' + kind : '');
}

// ---------- 通用卡片 / 列表渲染 ----------
function pill(text, tone) {
  return el('span', { class: 'pill pill--' + (tone || 'muted'), text });
}

function scopeText(scope) {
  if (scope == null) return '—';
  if (typeof scope === 'string') return scope === 'global' ? '全局' : scope;
  const kind = scope.kind || scope.type || '';
  if (kind === 'project' && (scope.project || scope.project_id)) return '项目 · ' + (scope.project || scope.project_id);
  return kind === 'global' ? '全局' : kind || '—';
}

function card(headKids, bodyKids) {
  const c = el('div', { class: 'card' });
  if (headKids) c.appendChild(el('div', { class: 'card__head' }, [].concat(headKids)));
  if (bodyKids) c.appendChild(el('div', { class: 'card__body' }, [].concat(bodyKids)));
  return c;
}
function kv(k, v) {
  if (v == null || v === '') return null;
  return el('div', { class: 'kv' }, [
    el('span', { class: 'kv__k', text: k }),
    el('span', { class: 'kv__v', text: String(v) })
  ]);
}
function emptyMsg(text) {
  return el('p', { class: 'empty', text });
}

// 404 等不可用：诚实呈现，不伪造空成功。
function unavailableNote(container, res, label) {
  clear(container);
  if (res.networkError) {
    container.appendChild(el('p', { class: 'empty empty--warn', text: '无法连接本地服务，' + label + '暂时不可读。' }));
  } else if (res.status === 404) {
    container.appendChild(
      el('p', { class: 'empty empty--warn', text: '本构建未提供' + label + '（HTTP 404 not_available）。' })
    );
  } else if (!res.ok) {
    container.appendChild(
      el('p', {
        class: 'empty empty--warn',
        text: `${label}读取失败（HTTP ${res.status}${res.code ? ' ' + res.code : ''}）。`
      })
    );
  }
}

// =========================================================
//  全局状态
// =========================================================
const state = {
  pendingStatement: null // {body, key} 仅用于“重试同一提交”
};

// =========================================================
//  状态视图
// =========================================================
async function readConnection() {
  const res = await api('GET', '/v2/health');
  setConn(res.ok ? '本机在线' : res.status === 401 ? '需要登录' : '连接中断', res.ok ? 'ok' : 'warn');
  $('#app-version').textContent = res.ok && res.data.version ? 'v' + res.data.version : '';
}

// =========================================================
//  当前声明 + 追加
// =========================================================
let libraryProject = '';
let libraryRequest = 0;
const libraryTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });

function loadLibraryScope() {
  libraryProject = $('#library-scope').value === 'project' ? $('#library-project').value.trim() : '';
  return loadStatements();
}

document.addEventListener('DOMContentLoaded', () => {
  $('#library-search').addEventListener('click', () => $('#global-search').click());
  $('#library-filter').addEventListener('submit', (event) => {
    event.preventDefault();
    if ($('#library-scope').value === 'project' && !$('#library-project').value.trim()) {
      $('#library-project').focus();
      return;
    }
    loadLibraryScope();
  });
  $('#library-scope').addEventListener('change', () => {
    const project = $('#library-scope').value === 'project';
    $('#library-project-field').hidden = $('#library-apply').hidden = !project;
    $('#library-project').required = project;
    if (project) $('#library-project').focus();
    loadLibraryScope();
  });
});

async function loadStatements() {
  const request = ++libraryRequest;
  const result = $('#stmt-result');
  const list = $('#stmt-list');
  const seq = $('#stmt-seq');
  if ($('#library-scope').value === 'project' && !libraryProject) {
    list.replaceChildren(emptyMsg('输入项目标识，查看该项目适用的事实。'));
    result.textContent = seq.textContent = '';
    return;
  }
  result.textContent = '正在读取当前事实…';
  const res = await api(
    'GET',
    '/v2/statements' + (libraryProject ? '?project=' + encodeURIComponent(libraryProject) : '')
  );
  if (request !== libraryRequest) return;
  seq.textContent = '';
  if (!res.ok) {
    result.textContent = res.data?.message || '暂时无法读取事实。';
    list.replaceChildren();
    return;
  }
  const items = res.data.statements;
  seq.textContent = '修订序列 ' + res.data.change_seq;
  result.textContent = libraryProject
    ? libraryProject + ' · ' + items.length + ' 项适用事实，含沿用的全局声明'
    : '全局 · ' + items.length + ' 项当前事实';
  list.replaceChildren(...(items.length ? items.map(statementRow) : [emptyMsg('当前范围内没有已生效的事实。')]));
}

function statementRow(item) {
  const titles = { 'report.language': '报告语言', 'report.opening': '报告开头' };
  const labels = {
    'report.language': { zh: '中文', en: '英文' },
    'report.opening': { actions: '先列行动项', conclusion: '先写结论' }
  };
  const history = el('button', { class: 'btn btn--sm', type: 'button', text: '修订历史' });
  history.addEventListener('click', () => openHistory(item.fact_id));
  const details = el('details', { class: 'library-evidence' }, [
    el('summary', { text: '查看原话与出处' }),
    el('blockquote', { text: item.evidence }),
    el('dl', { class: 'library-metadata' }, [
      el('dt', { text: '事实标识' }),
      el('dd', { text: item.fact_id }),
      el('dt', { text: '修订标识' }),
      el('dd', { text: item.revision_id }),
      el('dt', { text: '生效时间' }),
      el('dd', { text: libraryTime.format(new Date(item.valid_from)) })
    ])
  ]);
  return el('article', { class: 'library-fact' }, [
    el('div', { class: 'library-fact-head' }, [
      el('h4', { text: titles[item.key] || item.key }),
      pill(scopeText(item.scope), 'info')
    ]),
    el('div', { class: 'library-fact-value' }, [
      el('strong', { text: labels[item.key]?.[item.value] || item.value }),
      el('code', { class: 'fact-key', text: item.key })
    ]),
    el('div', { class: 'library-fact-actions' }, [details, history])
  ]);
}
function statusLabel(s) {
  return { active: '已生效', scheduled: '待生效', pending: '待归并', disputed: '有争议', historical: '仅历史' }[s] || s;
}
function statusTone(s) {
  return { active: 'ok', scheduled: 'info', pending: 'info', disputed: 'warn', historical: 'muted' }[s] || 'muted';
}

function bindAppendForm() {
  const form = $('#append-form');
  const scopeSel = $('#ap-scope');
  const projField = $('#ap-project-field');
  scopeSel.addEventListener('change', () => {
    projField.hidden = scopeSel.value !== 'project';
    if (scopeSel.value === 'project') $('#ap-project').focus();
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submitStatement();
  });
}

function collectStatement() {
  const original = $('#ap-original').value.trim();
  const scopeKind = $('#ap-scope').value;
  const share = ($('input[name="share"]:checked') || {}).value || 'owner';
  const scope = { kind: scopeKind };
  if (scopeKind === 'project') {
    const p = $('#ap-project').value.trim();
    if (p) scope.project = p;
  }
  const body = { original, scope, share };
  return {
    body,
    original,
    error: !original ? '原话不能为空。' : scopeKind === 'project' && !scope.project ? '项目内声明需填写项目标识。' : ''
  };
}

function submitStatement(reuse) {
  const rn = $('#ap-result');
  let intent;
  if (reuse && state.pendingStatement) {
    intent = state.pendingStatement; // 重试同一提交：原正文 + 原键
  } else {
    const out = collectStatement();
    if (out.error) {
      clear(rn);
      rn.hidden = false;
      rn.appendChild(el('p', { class: 'empty empty--warn', text: out.error }));
      return;
    }
    if (out.original.length > 8000) {
      clear(rn);
      rn.hidden = false;
      rn.appendChild(el('p', { class: 'empty empty--warn', text: '原话过长，已拒绝发送。' }));
      return;
    }
    intent = { body: out.body, key: newKey() };
    state.pendingStatement = intent;
  }
  setBusy(true);
  api('POST', '/v2/statements', { body: intent.body, idempotencyKey: intent.key })
    .then((res) => {
      renderResult(rn, res);
      if (res.ok) {
        state.pendingStatement = null;
        showRetry(null);
        $('#ap-original').value = ''; // 仅成功后清空原话，不动表单结构
        announce('声明已提交。');
        if (res.status === 201) rn.appendChild(receiptBlock(res.data));
        loadStatements();
      } else if (res.status === 404) {
        // 声明接口未交付：正文仍在框中，重试同一提交无意义，诚实报告即可。
        showRetry(null);
      } else if (res.retryable || res.status === 0 || res.status >= 500) {
        showRetry('retry-statement'); // 正文仍在 textarea，可原键重试
      } else {
        // 4xx 校验/权限：正文保留在框中，用户修改后将换新键。
        showRetry(null);
      }
    })
    .finally(() => setBusy(false));
}
function showRetry(btnId) {
  const b = $('[data-action="retry-statement"]');
  if (b) b.hidden = btnId !== 'retry-statement';
}

function receiptBlock(data) {
  const wrap = el('div', { class: 'receipt' });
  if (data && data.statement_id != null) wrap.appendChild(kv('statement', data.statement_id));
  if (data && typeof data.change_seq === 'number') wrap.appendChild(kv('change_seq', data.change_seq));
  const dims = data && Array.isArray(data.dimensions) ? data.dimensions : [];
  for (const d of dims) {
    wrap.appendChild(
      el(
        'div',
        { class: 'dim' },
        [
          el('strong', { text: String(d.key || '') }),
          d.status ? pill(statusLabel(d.status), statusTone(d.status)) : null,
          d.fact_id ? el('span', { class: 'muted', text: ' ' + d.fact_id }) : null,
          d.revision_id ? el('span', { class: 'muted', text: ' · ' + d.revision_id }) : null,
          d.supersedes ? el('span', { class: 'muted', text: ' ← ' + d.supersedes }) : null,
          d.reason ? el('span', { class: 'muted', text: ' · ' + d.reason }) : null
        ].filter(Boolean)
      )
    );
  }
  return wrap;
}

// =========================================================
//  历史
// =========================================================
function openHistory(factId) {
  const input = $('#his-fact');
  input.value = String(factId);
  navigate('history');
}
let historyRequest = 0;
document.addEventListener('DOMContentLoaded', () => {
  $('#history-form').addEventListener('submit', (event) => {
    event.preventDefault();
    loadHistory();
  });
});
async function loadHistory() {
  const request = ++historyRequest;
  const fact = $('#his-fact').value.trim();
  const body = $('#history-body');
  const result = $('#history-result');
  body.replaceChildren();
  if (!fact) {
    result.textContent = '从记忆库打开一项事实，或在上方输入标识。';
    return;
  }
  result.textContent = '正在读取修订历史…';
  const res = await api('GET', '/v2/facts/' + encodeURIComponent(fact) + '/history');
  if (request !== historyRequest) return;
  if (!res.ok) {
    result.textContent =
      res.status === 404 ? '找不到可读取的修订，该事实可能已撤回。' : res.data?.message || '无法连接本机服务。';
    return;
  }
  const { key, scope, revisions } = res.data;
  result.textContent = key + ' · ' + scopeText(scope) + ' · ' + revisions.length + ' 次修订';
  body.replaceChildren(
    ...revisions.map((revision) => {
      const state = { active: '记录时生效', pending: '记录时待归并', scheduled: '记录时待生效' };
      const metadata = [
        el('dt', { text: '修订标识' }),
        el('dd', { text: revision.revision_id }),
        el('dt', { text: '声明标识' }),
        el('dd', { text: revision.statement_id }),
        el('dt', { text: '生效时间' }),
        el('dd', { text: libraryTime.format(new Date(revision.valid_from)) })
      ];
      if (revision.supersedes) metadata.push(el('dt', { text: '取代的修订' }), el('dd', { text: revision.supersedes }));
      return el('article', { class: 'library-fact history-edition' }, [
        el('div', { class: 'library-fact-head' }, [
          el('time', { datetime: revision.known_from, text: libraryTime.format(new Date(revision.known_from)) }),
          pill(state[revision.status] || revision.status, statusTone(revision.status))
        ]),
        revision.original != null ? el('blockquote', { text: revision.original }) : null,
        revision.value != null ? kv('记录取值', revision.value) : null,
        el('details', { class: 'library-evidence' }, [
          el('summary', { text: '修订信息' }),
          el('dl', { class: 'library-metadata' }, metadata)
        ])
      ]);
    })
  );
}
// =========================================================
// 任务、投递与治理记录
async function loadGeneric(viewKey, path, hints, render) {
  const rn = $('#' + viewKey + '-result');
  const body = $('#' + viewKey + '-body');
  const res = await api('GET', path);
  renderResult(rn, res);
  clear(body);
  if (res.ok && res.data) {
    const items = listFrom(res.data, hints);
    if (!items) {
      body.appendChild(
        el('details', { class: 'raw' }, [
          el('summary', { text: '原始返回' }),
          el('pre', { text: truncate(pretty(res.data)) })
        ])
      );
      return;
    }
    if (items.length === 0) {
      body.appendChild(emptyMsg('暂无条目。'));
      return;
    }
    for (const it of items) body.appendChild(render(it));
  } else {
    unavailableNote(body, res, labelFor(viewKey));
  }
}
function labelFor(v) {
  return { tasks: '任务', jobs: '作业', deliv: '投递', ops: '操作' }[v] || v;
}

function detailsRecord(record) {
  const details = el('details', { class: 'raw' }, [el('summary', { text: '记录详情' })]);
  details.addEventListener('toggle', () => {
    if (details.open && !details.querySelector('pre')) details.append(el('pre', { text: truncate(pretty(record)) }));
  });
  return details;
}

const loadTasks = () =>
  loadGeneric('tasks', '/v2/tasks', ['tasks'], (task) => {
    const handoff = el('details', { class: 'raw' }, [el('summary', { text: '查看交接证据' })]);
    handoff.addEventListener('toggle', async () => {
      if (!handoff.open || handoff.childElementCount > 1) return;
      const content = el('div', { class: 'handoff', text: '读取中…' });
      handoff.append(content);
      const res = await api('GET', '/v2/tasks/' + encodeURIComponent(task.task_id) + '/handoff');
      if (!res.ok) {
        unavailableNote(content, res, '交接证据');
        return;
      }
      const evidence = res.data.evidence;
      content.replaceChildren(
        el('p', { text: evidence.length + ' 条证据 · ' + res.data.observations.length + ' 项观察' })
      );
      for (const item of evidence)
        content.append(el('p', { text: truncate(item.text || pretty(item.checks || item), 500) }));
    });
    return card(
      [el('strong', { text: task.anchor }), task.project ? pill(task.project, 'info') : null],
      [kv('任务标识', task.task_id), handoff]
    );
  });
const jobLabels = { pending: '等待中', running: '运行中', done: '已完成', failed: '失败', cancelled: '已取消' };
const jobTones = { pending: 'info', running: 'info', done: 'ok', failed: 'warn', cancelled: 'muted' };
const jobKinds = { user_message: '用户消息', assistant_message: '助手回复', tool_result: '工具回执' };
const candidateKinds = { fact: '事实', plan: '计划', experience: '经验' };
let jobPages = [null];
let nextJobCursor = null;
let jobRequest = 0;

document.addEventListener('DOMContentLoaded', () => {
  $('#jobs-filter').addEventListener('change', () => loadJobs());
  $('#jobs-next').addEventListener('click', () => {
    if (nextJobCursor) loadJobPage([...jobPages, nextJobCursor]);
  });
  $('#jobs-prev').addEventListener('click', () => {
    if (jobPages.length > 1) loadJobPage(jobPages.slice(0, -1));
  });
});

function loadJobs() {
  return loadJobPage([null]);
}

async function loadJobPage(pages, message = '') {
  const request = ++jobRequest;
  const status = $('#jobs-filter').value;
  const result = $('#jobs-result');
  const query = new URLSearchParams({ limit: '40' });
  if (status) query.set('status', status);
  if (pages.at(-1)) query.set('before', pages.at(-1));
  result.textContent = '正在读取作业…';
  $('#jobs-pages').hidden = true;
  const res = await api('GET', '/v2/jobs?' + query);
  if (request !== jobRequest) return;
  const body = $('#jobs-body');
  if (!res.ok) {
    result.textContent = res.data?.message || '暂时无法读取提炼队列。';
    body.replaceChildren();
    return;
  }
  jobPages = pages;
  nextJobCursor = res.data.next_cursor;
  const jobs = res.data.jobs;
  result.textContent = message || (jobs.length ? '最近创建优先 · 本页 ' + jobs.length + ' 项' : '');
  body.replaceChildren(
    ...(jobs.length
      ? jobs.map(jobRow)
      : [emptyMsg(status ? '没有处于此状态的作业。' : '尚无提炼作业。接收到来源事件后可在这里查看进度。')])
  );
  $('#jobs-pages').hidden = !nextJobCursor && pages.length === 1;
  $('#jobs-prev').disabled = pages.length === 1;
  $('#jobs-next').disabled = !nextJobCursor;
  $('#jobs-page').textContent = '第 ' + pages.length + ' 页';
}

function jobRow(job) {
  const status = job.status;
  const meta = [
    el('dt', { text: '作业标识' }),
    el('dd', { text: job.job_id }),
    el('dt', { text: '来源标识' }),
    el('dd', { text: job.source_id }),
    el('dt', { text: '事件标识' }),
    el('dd', { text: job.event_id })
  ];
  if (status === 'pending' || status === 'failed') {
    meta.push(el('dt', { text: '可领取时间' }), el('dd', { text: libraryTime.format(new Date(job.available_at)) }));
  }
  const actions = el('div', { class: 'job-actions' }, [
    el('details', { class: 'library-evidence' }, [
      el('summary', { text: '作业详情' }),
      el('dl', { class: 'library-metadata' }, meta)
    ])
  ]);
  if (status === 'failed') {
    const retry = el('button', { type: 'button', class: 'btn btn--sm', text: '重新排队' });
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      const res = await api('POST', '/v2/jobs/' + encodeURIComponent(job.job_id) + '/retry', { body: {} });
      if (res.ok) {
        await loadJobPage([null], '作业已重新排队。');
      } else {
        $('#jobs-result').textContent = res.data?.message || '重新排队失败，请刷新确认状态。';
        retry.disabled = false;
      }
    });
    actions.appendChild(retry);
  }
  const row = el('article', { class: 'library-job' }, [
    el('div', { class: 'job-row-head' }, [
      el('strong', { text: (jobKinds[job.actor_kind] || job.actor_kind) + '提炼' }),
      pill(jobLabels[status] || status, jobTones[status])
    ]),
    el('p', { class: 'job-source', text: job.source_label }),
    el('div', { class: 'job-digest' }, [
      el('span', { text: '已尝试 ' + job.attempts + ' 次' }),
      el('span', { text: '已记录候选 ' + job.candidates_recorded + ' 项' }),
      el('time', { datetime: job.updated_at, text: libraryTime.format(new Date(job.updated_at)) + ' 更新' })
    ])
  ]);
  if (job.fail_reason) {
    const reasons = {
      temporarily_unavailable: '提炼服务暂时不可用',
      invalid_input: '提炼输入或模型响应未通过校验',
      model_route_changed: '处理期间模型配置已更改'
    };
    row.appendChild(
      el('p', { class: 'job-failure' }, [reasons[job.fail_reason] || '提炼失败', el('code', { text: job.fail_reason })])
    );
  }
  row.appendChild(actions);
  if (status === 'done' && job.candidates_recorded > 0) row.appendChild(jobCandidateDetails(job));
  return row;
}

function jobCandidateDetails(job) {
  const feedback = el('p', { class: 'hint-sm', role: 'status' });
  const content = el('div');
  const details = el('details', { class: 'library-evidence' }, [
    el('summary', { text: '查看提炼候选 · ' + job.candidates_recorded + ' 项' }),
    feedback,
    content
  ]);
  let request = 0;
  details.addEventListener('toggle', async () => {
    const current = ++request;
    content.replaceChildren();
    feedback.textContent = '';
    if (!details.open) return;
    feedback.textContent = '正在读取候选与引用片段…';
    const res = await api('GET', '/v2/jobs/' + encodeURIComponent(job.job_id) + '/candidates');
    if (current !== request || !details.open) return;
    if (!res.ok) {
      feedback.textContent = res.status === 404 ? '该作业的候选已不可用。' : '暂时无法读取提炼候选。请折叠后重试。';
      return;
    }
    feedback.textContent = '模型生成的记忆提案。可按引用片段核对，正式声明在「记忆库」查看。';
    const { occurred_at: occurredAt, intake_version: version, candidates, evidence } = res.data;
    const spans = new Map(evidence.map(span => [span.span_id, span]));
    content.replaceChildren(
      el('dl', { class: 'library-metadata' }, [
        el('dt', { text: '发生时间' }), el('dd', { text: libraryTime.format(new Date(occurredAt)) }),
        el('dt', { text: '事件版本' }), el('dd', { text: version })
      ]),
      ...candidates.map(candidate => el('section', { class: 'library-fact' }, [
        el('div', { class: 'library-fact-head' }, [
          el('h4', { text: candidate.subject + ' · ' + candidate.key }),
          el('span', { class: 'hint-sm', text: candidateKinds[candidate.kind] || candidate.kind })
        ]),
        el('p', { class: 'library-fact-value', text: proposalValue(candidate.value) }),
        candidate.condition && el('p', { text: '适用条件 · ' + candidate.condition }),
        ...candidate.support.map(id => {
          const span = spans.get(id);
          return span && el('div', {}, [
            el('p', { class: 'hint-sm', text: '引用字段 · ' + span.field }),
            el('blockquote', { text: proposalValue(span.value) })
          ]);
        })
      ]))
    );
  });
  return details;
}

function proposalValue(value) {
  return typeof value === 'string' ? value : pretty(value);
}

const loadDeliveries = () =>
  loadGeneric('deliv', '/v2/deliveries', ['deliveries'], (d) =>
    card(
      [el('strong', { text: d.route_ref }), pill('已提供', 'info')],
      [
        el('div', { class: 'metarow' }, [
          kv('会话', d.session_id),
          kv('提供时间', new Date(d.provided_at).toLocaleString()),
          kv('提供修订', d.provided_revision_ids.length),
          kv('装入回执', d.loaded ? '已观察' : '待回执'),
          kv('采用回执', d.used ? '已观察' : '待回执')
        ]),
        detailsRecord(d)
      ]
    )
  );
const operationNames = {
  change_grant: '变更授权',
  stop_capture: '暂停采集',
  resume_capture: '恢复采集',
  forget: '遗忘记忆'
};
const loadOperations = () =>
  loadGeneric('ops', '/v2/operations', ['operations'], (op) =>
    card(
      [
        el('strong', { text: operationNames[op.kind] || op.kind }),
        pill(op.effective ? '已生效' : '待生效', op.effective ? 'ok' : 'warn')
      ],
      [kv('操作标识', op.operation_id), detailsRecord(op)]
    )
  );

async function archiveAction(button, body) {
  const output = $('#backup-result');
  button.disabled = true;
  output.textContent = '处理中…';
  try {
    const res = await api('POST', '/v2/operations', { body, idempotencyKey: newKey() });
    if (!res.ok) {
      renderResult(output, res);
      return;
    }
    if (body.kind === 'backup') {
      $('#backup-id').value = res.data.backup_id;
      output.textContent = '已创建备份 · ' + res.data.backup_id;
    } else {
      output.textContent =
        res.data.state === 'verified' ? '备份校验通过，预检完成。' : '备份完整；当前工作区的撤回状态需要另行核对。';
    }
  } finally {
    button.disabled = false;
  }
}

//  路由
// =========================================================
const routes = {
  overview: 'overview',
  memories: 'statements',
  graph: 'graph',
  history: 'history',
  settings: 'settings',
  tasks: 'tasks',
  jobs: 'jobs',
  injections: 'deliveries',
  operations: 'operations',
  sources: 'sources',
  imports: 'imports'
};
const views = Object.fromEntries(Object.entries(routes).map(([route, view]) => [view, route]));
let currentView;
const features = new Map();

function showFeatureError(host, error) {
  if (host) host.replaceChildren(el('p', { class: 'empty empty--warn', text: '无法载入此功能。请刷新重试。' }));
  console.error(error);
}

function viewStyle(name) {
  let link = document.getElementById('style-' + name);
  if (!link) {
    link = el('link', { id: 'style-' + name, rel: 'stylesheet', href: '/' + name + '.css' });
    link.onerror = () => link.remove();
    document.head.append(link);
  }
}

function loadFeature(name) {
  if (!features.has(name)) {
    const css = el('link', { rel: 'stylesheet', href: '/' + name + '.css' });
    const script = el('script', { src: '/' + name + '.js' });
    const ready = (asset) =>
      new Promise((resolve, reject) => {
        asset.onload = resolve;
        asset.onerror = () => reject(new Error('Unable to load ' + name));
      });
    const promise = Promise.all([ready(css), ready(script)]).catch((error) => {
      features.delete(name);
      css.remove();
      script.remove();
      throw error;
    });
    features.set(name, promise);
    document.head.append(css, script);
  }
  return features.get(name);
}

async function openSearch() {
  const button = $('#global-search');
  try {
    await loadFeature('search');
    window.MemorySearch.open();
  } catch {
    button.title = '搜索界面载入失败，点击重试';
    announce(button.title);
  }
}
function navigate(view) {
  const target = '#/' + views[view];
  if (location.hash === target) switchView(view);
  else location.hash = target;
}
function setMenu(open) {
  document.body.classList.toggle('nav-open', open);
  $('#nav-toggle').setAttribute('aria-expanded', String(open));
  $('#nav-toggle').setAttribute('aria-label', open ? '关闭导航' : '打开导航');
  $('#nav-scrim').hidden = !open;
  const mobile = matchMedia('(max-width:940px)').matches;
  $('.rail').inert = mobile && !open;
  $('.workspace').inert = mobile && open;
  if (open) $('.navbtn.is-active')?.focus();
}
function closeMenu() {
  setMenu(false);
  $('#nav-toggle').focus();
}

function switchView(name) {
  if (name === 'statements' || name === 'jobs' || name === 'history') viewStyle('library');
  const old = currentView;
  currentView = name;
  if (old === 'overview' || old === 'graph') MemoryDashboard.leave();
  setMenu(false);
  for (const view of $$('.view')) {
    view.hidden = view.id !== 'view-' + name;
    view.classList.toggle('is-active', !view.hidden);
  }
  const section = $('#view-' + name);
  const title = $('#h-' + name, section)?.textContent || section.querySelector('h1,h2').textContent;
  $('#crumb').textContent = title;
  document.title = title + ' · Botinkit Memory';
  for (const btn of $$('.navbtn')) {
    const active = btn.dataset.view === name;
    btn.classList.toggle('is-active', active);
    if (active) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  }
  if (old) {
    section.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }
  if (old === 'settings' && name !== old) window.MemorySettings?.visible(false);
  if (name === 'settings') {
    section.inert = !window.MemorySettings;
    loadFeature('settings')
      .then(() => {
        section.inert = false;
        if (currentView === name) window.MemorySettings.visible(true);
      })
      .catch((error) => {
        section.inert = false;
        showFeatureError($('#cfg-result'), error);
        $('#cfg-result').hidden = false;
      });
  }
  if (name === 'overview' || name === 'graph') MemoryDashboard.enter(name);
  else if (name === 'sources' || name === 'imports') {
    const host = $('#' + name + '-content');
    loadFeature('intake')
      .then(() => {
        if (currentView === name) return window.MemoryIntake.open(name, host);
      })
      .catch((error) => showFeatureError(host, error));
  } else {
    const load = {
      statements: loadStatements,
      history: loadHistory,
      tasks: loadTasks,
      jobs: loadJobs,
      deliveries: loadDeliveries,
      operations: loadOperations
    }[name];
    if (load) load();
  }
}

function handleRoute() {
  const slug = location.hash.replace(/^#\/?/, '').split('?')[0];
  switchView(Object.hasOwn(routes, slug) ? routes[slug] : Object.hasOwn(views, slug) ? slug : 'overview');
  if (focusComposer && currentView === 'statements') {
    focusComposer = false;
    $('#ap-original').focus();
  }
}
let focusComposer = false;
function bindNav() {
  $$('.navbtn').forEach((btn) => btn.addEventListener('click', () => navigate(btn.dataset.view)));
  $('#nav-toggle').onclick = () => setMenu(true);
  $('#nav-scrim').onclick = closeMenu;
  $('.skip-link').onclick = (event) => {
    event.preventDefault();
    $('#main').focus();
  };
  matchMedia('(max-width:940px)').addEventListener('change', () => setMenu(false));
  $('#create-backup').onclick = (event) => archiveAction(event.currentTarget, { kind: 'backup' });
  $('#preview-backup').onsubmit = (event) => {
    event.preventDefault();
    archiveAction(event.submitter, { kind: 'restore_preview', backup_id: $('#backup-id').value.trim() });
  };
  $('#global-search').onclick = openSearch;
  $('#global-search kbd').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ K' : 'Ctrl K';
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (!document.querySelector('dialog[open]')) openSearch();
    }
    if (event.key === 'Escape' && document.body.classList.contains('nav-open')) closeMenu();
  });
  $('#new-statement').onclick = () => {
    focusComposer = true;
    navigate('statements');
  };
  const actions = {
    'reload-statements': loadStatements,
    'reload-history': loadHistory,
    'reload-config': () => (window.MemorySettings ? window.MemorySettings.refresh() : switchView('settings')),
    'reload-tasks': loadTasks,
    'reload-jobs': loadJobs,
    'reload-deliveries': loadDeliveries,
    'reload-operations': loadOperations,
    'retry-statement': () => submitStatement(true)
  };
  for (const btn of $$('[data-action]'))
    if (actions[btn.dataset.action]) btn.addEventListener('click', actions[btn.dataset.action]);
  window.addEventListener('hashchange', handleRoute);
}

function renderHidden(node) {
  clear(node);
  node.hidden = true;
}
function setBusy(on) {
  document.body.classList.toggle('is-busy', !!on);
}
function boot() {
  bindNav();
  bindAppendForm();
  MemoryDashboard.bind();
  readConnection();
  handleRoute();
}
document.addEventListener('DOMContentLoaded', boot);
