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
// Local reads fail promptly if the service cannot return committed state.
// History options can call a remote connector.
const API_READ_TIMEOUT_MS = 5000;
const API_TIMEOUT_MS = 30000;
const API_LONG_TIMEOUT_MS = 120000;
function apiTimeoutFor(method, path, opts) {
  if (opts.timeoutMs != null) return opts.timeoutMs;
  if (/^\/v2\/models\/[^/]+\/test$/.test(path)) return 190000;
  const kind = opts.body && opts.body.kind;
  if (kind === 'backup' || kind === 'restore_preview') return API_LONG_TIMEOUT_MS;
  if (method === 'GET' && !path.startsWith('/v2/history/options?')) return API_READ_TIMEOUT_MS;
  return API_TIMEOUT_MS;
}
async function api(method, path, opts) {
  opts = opts || {};
  const init = { method, credentials: 'same-origin', headers: Object.assign({}, opts.headers || {}) };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  if (opts.idempotencyKey) init.headers['Idempotency-Key'] = opts.idempotencyKey;

  const controller = new AbortController();
  init.signal = controller.signal;
  const timer = setTimeout(() => controller.abort(), apiTimeoutFor(method, path, opts));
  let res,
    raw = '',
    data = null,
    networkError = null,
    timedOut = false;
  try {
    res = await fetch(path, init);
  } catch (e) {
    timedOut = controller.signal.aborted;
    networkError = timedOut ? '请求超时' : e && e.message ? e.message : String(e);
  }
  if (res) {
    try {
      raw = await res.text();
    } catch (_) {
      raw = '';
      if (controller.signal.aborted) {
        timedOut = true;
        networkError = '请求超时';
        res = null;
      }
    }
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (_) {
        data = null;
      }
    }
  }
  clearTimeout(timer);

  const code = data && (data.code || (data.error && data.error.code));
  const requestId = data && (data.request_id || (data.error && data.error.request_id));
  const retryable = data ? (data.retryable != null ? data.retryable : data.error && data.error.retryable) : undefined;

  const result = {
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
    retryable: retryable === true || timedOut
  };
  // 会话失效 / 连接中断时刷新连接指示；健康检查自身不再触发，避免递归。
  if (!opts.noConnHook) {
    if (result.status === 401) noteSessionLost();
    else if (result.networkError) readConnection();
  }
  return result;
}

// ---------- 错误标题：code → 中文 + 下一步；原始英文只留在「技术详情」 ----------
function errorHeadline(res) {
  const status = (res && res.status) || 0;
  const code = (res && res.code) || '';
  const message = res && res.data && typeof res.data.message === 'string' ? res.data.message : '';
  const chinese = /[\u3400-\u4dbf\u4e00-\u9fff]/.test(message);
  if (status === 401 || code === 'authentication_required')
    return '本机会话已结束。请在终端运行 bmem web 重新打开。';
  if (!status || (res && res.networkError)) return '无法连接本机服务。';
  const mapped = {
    revision_changed: '目标或状态已变化，请重新预览或读取最新后再提交。',
    key_conflict: '该提交键已用于另一份正文，请修改后重新提交。',
    temporarily_unavailable: '服务暂时不可用，可重试同一提交。',
    not_available: '本构建未提供该功能。'
  };
  if (mapped[code]) return mapped[code];
  if (status === 404 && !code) return '本构建未提供该功能。';
  if (code === 'model_not_ready')
    return chinese ? message : '模型尚未就绪，请先在「工作区设置」测试并绑定模型。';
  if (code === 'invalid_input') return chinese ? message : '提交内容未通过校验，请检查后重试。';
  if (chinese) return message;
  return '服务返回错误（HTTP ' + status + (code ? ' ' + code : '') + '）。';
}

// ---------- 请求结果行（如实报告状态 / code / 正文） ----------
function renderResult(node, res) {
  clear(node);
  node.hidden = res.ok && res.method === 'GET';
  if (node.hidden) return;
  const message = res.ok ? '已提交' : errorHeadline(res);
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
function emptyMsg(text, cta = null) {
  const node = el('p', { class: 'empty', text });
  if (cta && cta.label) {
    const btn = el('button', { type: 'button', class: 'btn btn--primary btn--sm empty__cta', text: cta.label });
    btn.addEventListener('click', () => {
      if (typeof cta.run === 'function') cta.run();
    });
    node.appendChild(btn);
  }
  return node;
}

// 读取中：hint-sm 行 + 小转轮；骨架屏用 .placeholder（见 app.css）。
function setLoading(node, text = '正在读取…') {
  if (!node) return;
  node.replaceChildren(
    el('p', { class: 'hint-sm loading-line' }, [
      el('span', { class: 'btn__spin', 'aria-hidden': 'true' }),
      el('span', { text })
    ])
  );
}

// 仅在文本变化时写入，避免 role=status 区域每轮重复播报。
function setText(node, text) {
  if (node && node.textContent !== text) node.textContent = text;
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
  } else if (!res.data) {
    container.appendChild(
      el('p', {
        class: 'empty empty--warn',
        text: `${label}读取失败：服务返回了无法解析的内容（HTTP ${res.status}）。`
      })
    );
  }
}

// =========================================================
//  共享反馈组件（各页面脚本直接调用，勿重复实现）
// =========================================================

// ---------- 轻量通知：右下角 toast ----------
// tone: ok|warn|err|info；opts.action={label,run} 提供后续动作按钮。
// 宿主 #toast-host 常驻 role=status aria-live=polite；err 单独 role=alert。
const TOAST_LIMIT = 3;
function toast(message, tone = 'ok', opts = {}) {
  const host = $('#toast-host');
  if (!host) return null;
  const kind = ['ok', 'warn', 'err', 'info'].includes(tone) ? tone : 'ok';
  while (host.children.length >= TOAST_LIMIT) dismissToast(host.firstElementChild);
  const node = el('div', { class: 'toast toast--' + kind });
  if (kind === 'err') node.setAttribute('role', 'alert');
  node.append(el('span', { class: 'toast__msg', text: String(message) }));
  if (opts.action && opts.action.label) {
    const action = el('button', { type: 'button', class: 'btn btn--sm toast__action', text: opts.action.label });
    action.addEventListener('click', () => {
      dismissToast(node);
      if (typeof opts.action.run === 'function') opts.action.run();
    });
    node.append(action);
  }
  const close = el('button', { type: 'button', class: 'toast__close', 'aria-label': '关闭通知', text: '×' });
  close.addEventListener('click', () => dismissToast(node));
  node.append(close);
  host.append(node);
  const life = kind === 'err' ? 6000 : 4000;
  const arm = () => {
    clearTimeout(node.__life);
    node.__life = setTimeout(() => dismissToast(node), life);
  };
  // 悬停 / 键盘停留时暂停自动消失，给用户读完和操作的时间。
  node.addEventListener('mouseenter', () => clearTimeout(node.__life));
  node.addEventListener('mouseleave', arm);
  node.addEventListener('focusin', () => clearTimeout(node.__life));
  node.addEventListener('focusout', arm);
  arm();
  return node;
}
function dismissToast(node) {
  if (!node) return;
  clearTimeout(node.__life);
  node.remove();
}

// ---------- 确认对话框：原生 <dialog>，返回 Promise<boolean> ----------
// Escape / 取消 / 点击遮罩 → false；焦点圈定在对话框内，关闭后归还调用方。
// confirmString 非空时启用“输入确认”：目标与输入按同一规则去首尾空白后完全一致才允许确认。
function confirmDialog(options = {}) {
  const {
    title = '请确认',
    desc = '',
    confirmText = '确认',
    cancelText = '取消',
    danger = false,
    confirmString = null
  } = options;
  // 确认目标与输入做同一归一化（trim）：名称带首尾空白的模型也能输入确认；
  // 提示文案展示归一化后的目标。纯空白目标退化为普通确认。
  const confirmTarget = confirmString == null ? '' : String(confirmString).trim();
  const invoker = document.activeElement;
  return new Promise((resolve) => {
    let settled = false;
    let answer = false;
    const titleId = 'dlg-' + newKey();
    const confirmBtn = el('button', {
      type: 'button',
      class: 'btn ' + (danger ? 'btn--danger' : 'btn--primary'),
      text: confirmText
    });
    const cancelBtn = el('button', { type: 'button', class: 'btn', text: cancelText });
    const actions = el('div', { class: 'dialog-actions' }, [cancelBtn, confirmBtn]);
    const card = el('div', { class: 'dialog-card' }, [
      el('h2', { id: titleId, class: 'dialog-title', text: title }),
      desc ? el('p', { class: 'dialog-desc', text: desc }) : null,
      actions
    ]);
    let typeInput = null;
    if (confirmTarget) {
      typeInput = el('input', {
        type: 'text',
        class: 'dialog-input',
        autocomplete: 'off',
        spellcheck: 'false'
      });
      confirmBtn.disabled = true;
      typeInput.addEventListener('input', () => {
        confirmBtn.disabled = typeInput.value.trim() !== confirmTarget;
      });
      typeInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !confirmBtn.disabled) {
          event.preventDefault();
          finish(true);
        }
      });
      card.insertBefore(
        el('label', { class: 'dialog-type' }, [
          el('span', { text: '输入「' + confirmTarget + '」以确认' }),
          typeInput
        ]),
        actions
      );
    }
    const dialog = el('dialog', { class: 'dialog' + (danger ? ' dialog--danger' : ''), 'aria-labelledby': titleId }, [
      card
    ]);
    const finish = (value) => {
      if (settled) return;
      settled = true;
      answer = value;
      if (dialog.open) dialog.close();
      else cleanup();
    };
    const cleanup = () => {
      dialog.remove();
      if (invoker && invoker !== document.body && invoker.isConnected && typeof invoker.focus === 'function')
        invoker.focus({ preventScroll: true });
      resolve(answer);
    };
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      finish(false);
    });
    dialog.addEventListener('close', cleanup);
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) finish(false);
    });
    cancelBtn.addEventListener('click', () => finish(false));
    confirmBtn.addEventListener('click', () => finish(true));
    document.body.append(dialog);
    dialog.showModal();
    if (typeInput) typeInput.focus();
    else if (danger) cancelBtn.focus();
    else confirmBtn.focus();
  });
}

// ---------- 按钮忙碌态：禁用 + aria-busy + 转轮 + 中文进度文案 ----------
// busy(btn,true,'正在授权…') 保存原内容与焦点；busy(btn,false) 在 finally 中还原。
function busy(btn, on, busyText) {
  if (!btn) return;
  if (on) {
    if (btn.__busyState) return;
    btn.__busyState = {
      disabled: btn.disabled,
      nodes: Array.from(btn.childNodes),
      focused: document.activeElement === btn
    };
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    btn.append(
      el('span', { class: 'btn__spin', 'aria-hidden': 'true' }),
      el('span', { class: 'btn__label', text: busyText || '处理中…' })
    );
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.classList.add('is-busy');
  } else {
    const saved = btn.__busyState;
    if (!saved) return;
    delete btn.__busyState;
    btn.classList.remove('is-busy');
    btn.removeAttribute('aria-busy');
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    for (const node of saved.nodes) btn.appendChild(node);
    btn.disabled = saved.disabled;
    if (saved.focused && btn.isConnected && !btn.disabled) btn.focus({ preventScroll: true });
  }
}

// ---------- 键控增量列表刷新 ----------
// 按 data-key 索引现有行：未变化的行原样保留（含焦点 / 展开状态 / 懒加载内容），
// 变化的行优先 updateNode 原位更新，否则单行重建；新增追加、消失移除。
// 绝不整表 replaceChildren；保留键盘焦点、滚动位置与 <details> 展开状态。
const LIST_FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';
function reconcileList(container, items, keyFn, renderNode, updateNode = null) {
  if (!container) return;
  const rows = Array.isArray(items) ? items : [];
  const active = document.activeElement;
  let focus = null;
  if (active && active !== document.body && container.contains(active)) {
    const row = active.closest('[data-key]');
    if (row && container.contains(row)) {
      focus = {
        key: row.dataset.key,
        focusKey: (active.dataset && active.dataset.focusKey) || null,
        self: active === row,
        index: $$(LIST_FOCUSABLE, row).indexOf(active)
      };
    }
  }
  const scrollTop = container.scrollTop;
  const existing = new Map();
  for (const child of Array.from(container.children)) {
    const key = child.dataset ? child.dataset.key : undefined;
    if (key != null) existing.set(key, child);
    else child.remove(); // 空态 / 加载提示等非行节点
  }
  const seen = new Set();
  const desired = [];
  for (const item of rows) {
    const key = String(keyFn(item));
    if (seen.has(key)) continue;
    seen.add(key);
    let node = existing.get(key) || null;
    const signature = JSON.stringify(item);
    if (node && node.__listSignature === signature) {
      desired.push(node); // 内容未变：不动 DOM
      continue;
    }
    if (node && updateNode) {
      updateNode(node, item);
    } else {
      const openState = node ? $$('details', node).map((d) => d.open) : null;
      const fresh = renderNode(item);
      fresh.dataset.key = key;
      if (openState) $$('details', fresh).forEach((d, i) => { if (openState[i]) d.open = true; });
      if (node) node.replaceWith(fresh);
      node = fresh;
    }
    node.__listSignature = signature;
    desired.push(node);
  }
  for (const [key, node] of existing) if (!seen.has(key)) node.remove();
  let i = 0;
  for (const node of desired) {
    const at = container.children[i];
    if (at !== node) container.insertBefore(node, at || null);
    i += 1;
  }
  if (container.scrollTop !== scrollTop) container.scrollTop = scrollTop;
  if (focus) {
    const row = container.querySelector('[data-key="' + CSS.escape(focus.key) + '"]');
    let target = null;
    if (row) {
      if (focus.self) target = row;
      else if (focus.focusKey) target = row.querySelector('[data-focus-key="' + CSS.escape(focus.focusKey) + '"]');
      if (!target && !focus.self) {
        const list = $$(LIST_FOCUSABLE, row);
        target = list[Math.min(Math.max(focus.index, 0), list.length - 1)] || null;
      }
    }
    if (target && typeof target.focus === 'function') target.focus({ preventScroll: true });
  }
}

// =========================================================
//  全局状态
// =========================================================
const state = {
  pendingStatement: null // {body, key} 仅用于“重试同一提交”
};
let statementInFlight = false; // 追加声明防重复提交

// =========================================================
//  状态视图
// =========================================================
let connChecking = false;
let sessionBannerKind = null; // 'auth' | 'net'，同一状态只提示一次

async function readConnection() {
  if (connChecking) return;
  connChecking = true;
  try {
    const res = await api('GET', '/v2/health', { noConnHook: true, timeoutMs: 8000 });
    setConn(res.ok ? '本机在线' : res.status === 401 ? '需要登录' : '连接中断', res.ok ? 'ok' : 'warn');
    const version = $('#app-version');
    if (version) version.textContent = res.ok && res.data && res.data.version ? 'v' + res.data.version : '';
    if (res.ok) hideSessionBanner();
    else showSessionBanner(res.status === 401 ? 'auth' : 'net');
  } finally {
    connChecking = false;
  }
}

// 任何请求 401：会话确定失效，立即挂出一次性横幅并复核连接。
function noteSessionLost() {
  showSessionBanner('auth');
  readConnection();
}
function showSessionBanner(kind) {
  const banner = $('#session-banner');
  if (!banner || (sessionBannerKind === kind && !banner.hidden)) return;
  sessionBannerKind = kind;
  const retry = el('button', { class: 'btn btn--sm', type: 'button', text: '重新检测连接' });
  retry.addEventListener('click', () => readConnection());
  banner.replaceChildren(
    el('strong', { text: kind === 'auth' ? '本机会话已结束。' : '无法连接本机服务。' }),
    el('span', {
      text:
        kind === 'auth'
          ? '请在终端运行 bmem web 重新打开管理台。'
          : '请确认 bmem service 正在运行，然后重新检测。'
    }),
    retry
  );
  banner.hidden = false;
}
function hideSessionBanner() {
  sessionBannerKind = null;
  const banner = $('#session-banner');
  if (banner) {
    banner.hidden = true;
    banner.replaceChildren();
  }
}

// =========================================================
//  当前声明 + 追加
// =========================================================
let libraryProject = '';
let libraryValidAt = '';
let libraryRequest = 0;
const libraryTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });

function loadLibraryScope() {
  libraryProject = $('#library-scope').value === 'project' ? $('#library-project').value.trim() : '';
  libraryValidAt = $('#library-valid-at').value.trim();
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
    $('#library-project-field').hidden = !project;
    $('#library-project').required = project;
    if (project) $('#library-project').focus();
    loadLibraryScope();
  });
});

let proposalsRequest = 0;
async function loadLibrary() {
  const request = ++proposalsRequest;
  const host = $('#library-proposals');
  const [, res] = await Promise.all([loadStatements(), api('GET', '/v2/jobs?has_candidates=true&limit=4')]);
  if (request !== proposalsRequest || currentView !== 'statements') return;
  const jobs = res.data?.jobs;
  if (!res.ok || !Array.isArray(jobs)) {
    host.replaceChildren(emptyMsg(res.ok ? '提炼结果读取失败。' : errorHeadline(res)));
  } else if (!jobs.length) {
    host.replaceChildren(emptyMsg('还没有可查看的提炼候选。可以在「后台作业」查看处理进度。'));
  } else {
    reconcileList(host, jobs, job => job.job_id, job => el('article', { class: 'library-job' }, [
      el('strong', { text: job.source_label + ' · ' + (jobKinds[job.actor_kind] || job.actor_kind) }),
      jobCandidateDetails(job)
    ]));
  }
}

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
  setLoading(result, '正在读取当前事实…');
  const query = new URLSearchParams();
  if (libraryProject) query.set('project', libraryProject);
  if (libraryValidAt) query.set('valid_at', libraryValidAt);
  const res = await api('GET', '/v2/statements' + (query.size ? '?' + query : ''));
  if (request !== libraryRequest) return;
  seq.textContent = '';
  const items = res.ok && res.data && Array.isArray(res.data.statements) ? res.data.statements : null;
  if (!items) {
    result.textContent = res.ok
      ? '事实列表读取失败：服务返回了无法解析的内容（HTTP ' + res.status + '）。'
      : errorHeadline(res);
    list.replaceChildren();
    return;
  }
  setText(seq, '修订序列 ' + res.data.change_seq);
  setText(
    result,
    libraryValidAt
      ? (libraryProject || '全局') + ' · ' + libraryValidAt + ' · ' + items.length + ' 项适用事实'
      : libraryProject
      ? libraryProject + ' · ' + items.length + ' 项适用事实，含沿用的全局声明'
      : '全局 · ' + items.length + ' 项当前事实'
  );
  if (!items.length) {
    list.replaceChildren(emptyMsg('当前范围内没有已生效的事实。'));
    return;
  }
  reconcileList(list, items, (it) => it.fact_id + '·' + scopeText(it.scope), statementRow);
}

function statementRow(item) {
  const titles = { 'report.language': '报告语言', 'report.opening': '报告开头', 'code_example.language': '代码示例语言' };
  const labels = {
    'report.language': { zh: '中文', en: '英文' },
    'report.opening': { actions: '先列行动项', conclusion: '先写结论' }
  };
  const history = el('button', { class: 'btn btn--sm', type: 'button', text: '修订历史', 'data-focus-key': 'history' });
  history.addEventListener('click', () => openHistory(item.fact_id));
  const details = el('details', { class: 'library-evidence' }, [
    el('summary', { text: '查看原话与出处' }),
    el('blockquote', { text: item.evidence }),
    el('dl', { class: 'library-metadata' }, [
      el('dt', { text: '事实标识' }),
      el('dd', { text: item.fact_id }),
      el('dt', { text: '修订标识' }),
      el('dd', { text: item.revision_id, 'data-field': 'revision-id' }),
      el('dt', { text: '生效时间' }),
      el('dd', { text: item.valid_from }),
      ...(item.valid_until ? [
        el('dt', { text: '有效至（不含）' }),
        el('dd', { text: item.valid_until }),
      ] : []),
      ...(item.timezone ? [
        el('dt', { text: '解析时区' }),
        el('dd', { text: item.timezone }),
      ] : []),
      ...(item.clause ? [
        el('dt', { text: '对应分句' }),
        el('dd', { text: item.clause }),
      ] : [])
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
  if (statementInFlight) return; // 防双击重复提交
  const rn = $('#ap-result');
  let intent;
  let asNew = false;
  if (reuse && state.pendingStatement) {
    const pending = state.pendingStatement;
    const out = collectStatement();
    if (!out.error && JSON.stringify(out.body) !== JSON.stringify(pending.body)) {
      // 正文已被编辑：不再是“同一提交”。按新意图换新键发送，不静默替换用户编辑。
      intent = { body: out.body, key: newKey() };
      state.pendingStatement = intent;
      asNew = true;
    } else if (out.error && out.original) {
      // 编辑后的内容不合法：如实报告，保留待重试的原提交。
      clear(rn);
      rn.hidden = false;
      rn.appendChild(el('p', { class: 'empty empty--warn', text: out.error }));
      return;
    } else {
      intent = pending; // 重试同一提交：原正文 + 原键
    }
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
  statementInFlight = true;
  setBusy(true);
  const submitBtn = $('#append-form button[type="submit"]');
  busy(submitBtn, true, '正在提交…');
  api('POST', '/v2/statements', { body: intent.body, idempotencyKey: intent.key })
    .then((res) => {
      renderResult(rn, res);
      if (res.ok) {
        state.pendingStatement = null;
        showRetry(null);
        $('#ap-original').value = ''; // 仅成功后清空原话，不动表单结构
        announce('声明已提交。');
        toast(asNew ? '已按修改后的内容提交新声明。' : '声明已提交。', 'ok');
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
    .finally(() => {
      statementInFlight = false;
      setBusy(false);
      busy(submitBtn, false);
    });
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
  if (!fact) {
    body.replaceChildren();
    result.textContent = '从记忆库打开一项事实，或在上方输入标识。';
    return;
  }
  setLoading(result, '正在读取修订历史…');
  const res = await api('GET', '/v2/facts/' + encodeURIComponent(fact) + '/history');
  if (request !== historyRequest) return;
  const revisions = res.ok && res.data && Array.isArray(res.data.revisions) ? res.data.revisions : null;
  if (!revisions) {
    result.textContent = !res.ok
      ? res.status === 404
        ? '找不到可读取的修订，该事实可能已撤回。'
        : errorHeadline(res)
      : '修订历史读取失败：服务返回了无法解析的内容（HTTP ' + res.status + '）。';
    body.replaceChildren();
    return;
  }
  const { key, scope } = res.data;
  setText(result, key + ' · ' + scopeText(scope) + ' · ' + revisions.length + ' 次修订');
  if (!revisions.length) {
    body.replaceChildren(emptyMsg('该事实暂无修订记录。'));
    return;
  }
  reconcileList(body, revisions, (revision) => revision.revision_id, historyRow);
}

function historyRow(revision) {
  const statusNames = { active: '记录时生效', pending: '记录时待归并', scheduled: '记录时待生效' };
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
      pill(statusNames[revision.status] || revision.status, statusTone(revision.status))
    ]),
    revision.original != null ? el('blockquote', { text: revision.original }) : null,
    revision.value != null ? kv('记录取值', revision.value) : null,
    el('details', { class: 'library-evidence' }, [
      el('summary', { text: '修订信息' }),
      el('dl', { class: 'library-metadata' }, metadata)
    ])
  ]);
}
// =========================================================
// 任务、投递与治理记录
const genericRequests = Object.create(null); // 每视图独立请求计数，丢弃过期响应
async function loadGeneric(viewKey, path, hints, render, keyFn = null) {
  const request = (genericRequests[viewKey] = (genericRequests[viewKey] || 0) + 1);
  const rn = $('#' + viewKey + '-result');
  const body = $('#' + viewKey + '-body');
  setLoading(rn, '正在读取' + labelFor(viewKey) + '…');
  const res = await api('GET', path);
  if (request !== genericRequests[viewKey]) return;
  renderHidden(rn); // GET 的成败都由正文区呈现，避免双重错误渲染
  if (res.ok && res.data) {
    const items = listFrom(res.data, hints);
    if (!items) {
      clear(body);
      body.appendChild(
        el('details', { class: 'raw' }, [
          el('summary', { text: '原始返回' }),
          el('pre', { text: truncate(pretty(res.data)) })
        ])
      );
      return;
    }
    if (items.length === 0) {
      body.replaceChildren(emptyMsg('暂无条目。'));
      return;
    }
    if (keyFn) reconcileList(body, items, keyFn, render);
    else {
      clear(body);
      for (const it of items) body.appendChild(render(it));
    }
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
      if (!res.ok || !res.data) {
        unavailableNote(content, res, '交接证据');
        return;
      }
      const evidence = Array.isArray(res.data.evidence) ? res.data.evidence : [];
      const observations = Array.isArray(res.data.observations) ? res.data.observations : [];
      content.replaceChildren(el('p', { text: evidence.length + ' 条证据 · ' + observations.length + ' 项观察' }));
      for (const item of evidence)
        content.append(el('p', { text: truncate(item.text || pretty(item.checks || item), 500) }));
    });
    return card(
      [el('strong', { text: task.anchor }), task.project ? pill(task.project, 'info') : null],
      [kv('任务标识', task.task_id), handoff]
    );
  }, (task) => task.task_id);
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

// 作业自动刷新：仅当作业视图可见、页面可见且本页有等待 / 运行中作业。
// 焦点在输入控件时暂停，避免打断筛选操作；翻页状态保持不变。
let jobsLive = false;
let jobsTimer = 0;
function scheduleJobsRefresh() {
  clearTimeout(jobsTimer);
  jobsTimer = 0;
  if (currentView !== 'jobs' || document.hidden || !jobsLive) return;
  jobsTimer = setTimeout(() => {
    jobsTimer = 0;
    if (currentView !== 'jobs' || document.hidden || !jobsLive) return;
    const active = document.activeElement;
    if (active && /^(input|select|textarea)$/i.test(active.tagName)) {
      scheduleJobsRefresh();
      return;
    }
    loadJobPage(jobPages, '', true);
  }, 5000);
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) scheduleJobsRefresh();
});

async function loadJobPage(pages, message = '', quiet = false) {
  const request = ++jobRequest;
  const status = $('#jobs-filter').value;
  const result = $('#jobs-result');
  const query = new URLSearchParams({ limit: '40' });
  if (status === 'candidates') query.set('has_candidates', 'true');
  else if (status) query.set('status', status);
  if (pages.at(-1)) query.set('before', pages.at(-1));
  if (!quiet) {
    setLoading(result, '正在读取作业…');
    $('#jobs-pages').hidden = true;
  }
  const res = await api('GET', '/v2/jobs?' + query);
  if (request !== jobRequest) return;
  const body = $('#jobs-body');
  const jobs = res.ok && res.data && Array.isArray(res.data.jobs) ? res.data.jobs : null;
  if (!jobs) {
    setText(
      result,
      res.ok
        ? '提炼队列读取失败：服务返回了无法解析的内容（HTTP ' + res.status + '）。'
        : errorHeadline(res)
    );
    if (!quiet) {
      jobsLive = false;
      body.replaceChildren();
    }
    // 静默刷新失败：保留已读到的行与轮询，下一轮再试。
    scheduleJobsRefresh();
    return;
  }
  jobPages = pages;
  nextJobCursor = res.data.next_cursor;
  jobsLive = jobs.some((job) => job.status === 'pending' || job.status === 'running');
  setText(result, message || (jobs.length ? '最近创建优先 · 本页 ' + jobs.length + ' 项' : ''));
  if (!jobs.length) {
    body.replaceChildren(
      emptyMsg(status ? '没有处于此状态的作业。' : '尚无提炼作业。接收到来源事件后可在这里查看进度。')
    );
  } else {
    reconcileList(body, jobs, (job) => job.job_id, jobRow);
  }
  $('#jobs-pages').hidden = !nextJobCursor && pages.length === 1;
  $('#jobs-prev').disabled = pages.length === 1;
  $('#jobs-next').disabled = !nextJobCursor;
  setText($('#jobs-page'), '第 ' + pages.length + ' 页');
  scheduleJobsRefresh();
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
    const retry = el('button', { type: 'button', class: 'btn btn--sm', text: '重新排队', 'data-focus-key': 'retry' });
    retry.addEventListener('click', async () => {
      busy(retry, true, '正在排队…');
      try {
        const res = await api('POST', '/v2/jobs/' + encodeURIComponent(job.job_id) + '/retry', { body: {} });
        if (res.ok) {
          toast('作业已重新排队。', 'ok');
          await loadJobPage(jobPages, '作业已重新排队。'); // 留在当前页，不跳回第一页
        } else {
          setText($('#jobs-result'), errorHeadline(res));
          toast('重新排队失败：' + errorHeadline(res), 'err');
        }
      } finally {
        busy(retry, false);
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
      model_timeout: '提炼模型响应超时',
      model_unreachable: '无法连接到提炼模型',
      model_incomplete: '提炼模型未返回完整候选',
      model_invalid_output: '提炼模型返回的候选或原文引用无效',
      invalid_input: '提炼输入或模型响应未通过校验',
      model_route_changed: '处理期间模型配置已更改'
    };
    row.appendChild(
      el('p', { class: 'job-failure' }, [reasons[job.fail_reason] || (/^model_http_\d{3}$/.test(job.fail_reason) ? '提炼服务返回 HTTP ' + job.fail_reason.slice(-3) : '提炼失败'), el('code', { text: job.fail_reason })])
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
    if (!res.ok || !res.data) {
      feedback.textContent = !res.ok
        ? res.status === 404
          ? '该作业的候选已不可用。'
          : '暂时无法读取提炼候选。请折叠后重试。'
        : '提炼候选读取失败：服务返回了无法解析的内容。请折叠后重试。';
      return;
    }
    feedback.textContent = '模型生成的记忆提案。可按引用片段核对，正式声明在「记忆库」查看。';
    const { occurred_at: occurredAt, intake_version: version } = res.data;
    const candidates = Array.isArray(res.data.candidates) ? res.data.candidates : [];
    const evidence = Array.isArray(res.data.evidence) ? res.data.evidence : [];
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
        ...(Array.isArray(candidate.support) ? candidate.support : []).map(id => {
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

function loadDeliveries() {
  const root = $('#deliv-body');
  return loadFeature('ledger').then(() => {
    if (currentView === 'deliveries') return window.MemoryLedger.open(root);
  }).catch(error => showFeatureError(root, error, loadDeliveries));
}
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
  , (op) => op.operation_id);

async function archiveAction(button, body) {
  const output = $('#backup-result');
  busy(button, true, body.kind === 'backup' ? '正在创建备份…' : '正在检查备份…');
  output.textContent = '处理中…';
  try {
    const res = await api('POST', '/v2/operations', { body, idempotencyKey: newKey() });
    if (!res.ok) {
      renderResult(output, res);
      return;
    }
    if (!res.data) {
      output.replaceChildren(
        el('p', { class: 'empty empty--warn', text: '服务返回了无法解析的内容（HTTP ' + res.status + '），请重试。' })
      );
      return;
    }
    if (body.kind === 'backup') {
      if (!res.data.backup_id) {
        output.replaceChildren(el('p', { class: 'empty empty--warn', text: '备份回执缺少备份标识，请在操作记录中核对。' }));
        return;
      }
      $('#backup-id').value = res.data.backup_id;
      output.textContent = '已创建备份 · ' + res.data.backup_id;
      toast('备份已创建。', 'ok');
    } else {
      renderRestorePreview(output, res.data);
    }
  } finally {
    busy(button, false);
  }
}

// 恢复预检按真实 state 分支：blocked 必须醒目呈现真实阻止原因，不淡化。
function renderRestorePreview(output, data) {
  if (data.state === 'verified') {
    output.textContent = '备份校验通过，预检完成。';
    return;
  }
  if (data.state === 'blocked') {
    const reasons = {
      independent_revocation_watermark_required: '该备份来自另一个工作区实例，恢复前需要独立的撤回水位核对。',
      authority_state_changed_requires_revocation_replay: '备份后当前工作区的撤回状态已变化，恢复前需要重放撤回记录。'
    };
    const reason = (data.reason && reasons[data.reason]) || '恢复预检被阻止，需先核对撤回状态。';
    output.replaceChildren(
      el('p', { class: 'empty empty--warn' }, [
        reason,
        data.reason ? el('code', { text: data.reason }) : null,
        el('span', { class: 'muted', text: ' · 备份完整性已校验，但暂不可用于恢复。' })
      ])
    );
    toast('恢复预检被阻止：' + reason, 'warn');
    return;
  }
  output.textContent = '备份完整；当前工作区的撤回状态需要另行核对。';
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
  'data-management': 'imports',
  imports: 'imports'
};
const views = Object.fromEntries(Object.entries(routes).map(([route, view]) => [view, route]));
let currentView;
const features = new Map();

// 面包屑 / 标题按视图 id 取，不再依赖 h-<name> 与视图 id 恰好同名。
const viewTitles = {
  overview: '记忆总览',
  statements: '记忆库',
  graph: '记忆星图',
  history: '历史与出处',
  settings: '工作区设置',
  tasks: '任务接手',
  jobs: '后台作业',
  deliveries: '注入台账',
  operations: '操作记录',
  sources: '数据来源',
  imports: '数据管理'
};

function showFeatureError(host, error, retry = null) {
  if (host) {
    const kids = [el('p', { class: 'empty empty--warn', text: '无法载入此功能。请刷新重试。' })];
    if (retry) {
      const btn = el('button', { type: 'button', class: 'btn btn--primary btn--sm', text: '重试载入' });
      btn.addEventListener('click', retry);
      kids.push(btn);
    }
    host.replaceChildren(...kids);
    host.hidden = false;
  }
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
    button.classList.remove('search-launch--err');
    button.removeAttribute('title');
    button.setAttribute('aria-label', '搜索记忆');
    window.MemorySearch.open();
  } catch (error) {
    console.error(error);
    button.classList.add('search-launch--err');
    button.title = '搜索界面载入失败，点击重试';
    button.setAttribute('aria-label', '搜索记忆（载入失败，点击重试）');
    announce(button.title);
    toast('搜索界面载入失败。', 'err', { action: { label: '重试', run: openSearch } });
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

// 设置区懒加载：载入前 / 失败时禁用全部表单面板，防止原生表单提交；
// 头部与 #cfg-result 保持可交互，失败时在结果行给出“重试载入”。
// （整段 section.inert 会把重试按钮一并禁掉，故按面板粒度 inert。）
function settingsPanels() {
  return $$('#view-settings > .settings-grid, #view-settings > .settings-panel, #view-settings > .settings-links');
}
function enterSettings() {
  const panels = settingsPanels();
  const setInert = (on) => panels.forEach((panel) => { panel.inert = on; });
  const result = $('#cfg-result');
  if (window.MemorySettings) {
    setInert(false);
    result.hidden = true;
    clear(result);
    window.MemorySettings.visible(true);
    return;
  }
  setInert(true);
  loadFeature('settings')
    .then(() => {
      setInert(false);
      result.hidden = true;
      clear(result);
      if (currentView === 'settings') window.MemorySettings.visible(true);
    })
    .catch((error) => {
      setInert(true); // 失败时表单保持禁用
      showFeatureError(result, error, enterSettings);
    });
}

function switchView(name) {
  if (name === 'statements' || name === 'jobs' || name === 'history') viewStyle('library');
  const old = currentView;
  currentView = name;
  if ((old === 'sources' || old === 'imports') && old !== name) window.MemoryIntake?.leave();
  if (old === 'deliveries' && old !== name) window.MemoryLedger?.leave();
  if (old === 'overview' || old === 'graph') window.MemoryDashboard?.leave();
  setMenu(false);
  if (name !== 'jobs') scheduleJobsRefresh(); // 离开作业视图即停轮询
  for (const view of $$('.view')) {
    view.hidden = view.id !== 'view-' + name;
    view.classList.toggle('is-active', !view.hidden);
  }
  const section = $('#view-' + name);
  const title = viewTitles[name] || $('#h-' + name, section)?.textContent || name;
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
  if (name === 'settings') enterSettings();
  if (name === 'overview' || name === 'graph') {
    if (window.MemoryDashboard) window.MemoryDashboard.enter(name);
    else
      showFeatureError(
        $(name === 'overview' ? '#overview-error' : '#atlas-full'),
        new Error('dashboard.js 未载入')
      );
  } else if (name === 'sources' || name === 'imports') {
    const host = $('#' + name + '-content');
    loadFeature('intake')
      .then(() => {
        if (currentView === name) return window.MemoryIntake.open(name, host);
      })
      .catch((error) => showFeatureError(host, error));
  } else {
    const load = {
      statements: loadLibrary,
      history: loadHistory,
      tasks: loadTasks,
      jobs: loadJobs,
      deliveries: loadDeliveries,
      operations: loadOperations
    }[name];
    if (load) load();
  }
  readConnection(); // 每次切换视图复核连接指示
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
    // 移动导航展开时工作区是 inert 的：先收起导航，跳转才有效。
    if (document.body.classList.contains('nav-open')) closeMenu();
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
    'reload-statements': loadLibrary,
    'show-candidates': () => {
      $('#jobs-filter').value = 'candidates';
      navigate('jobs');
    },
    'reload-history': loadHistory,
    'reload-config': () => (window.MemorySettings ? window.MemorySettings.refresh() : switchView('settings')),
    'reload-tasks': loadTasks,
    'reload-jobs': loadJobs,
    'reload-deliveries': () => window.MemoryLedger ? window.MemoryLedger.refresh() : loadDeliveries(),
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
  if (window.MemoryDashboard) window.MemoryDashboard.bind();
  else showFeatureError($('#overview-error'), new Error('dashboard.js 未载入'));
  readConnection();
  handleRoute();
}
document.addEventListener('DOMContentLoaded', boot);
