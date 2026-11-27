'use strict';

const settingsState = {
  config: {
    revision: null,
    profile: null,
    vectorEnabled: null,
    models: [],
    bindings: { extract: null, embed: null },
    activity: null,
    loaded: false
  },
  pendingConfig: null,
  savingConfig: false
};

const profileNotes = {
  data: '保留完整表示，适合核对记忆细节与原文。',
  balanced: '兼顾磁盘占用、上下文长度与读取效率。',
  high: '压紧磁盘与上下文表示，需要细节时可以回查原文。'
};
const purposeNames = { extract: '提炼', embed: '嵌入' };
const readinessNames = {
  pending_model: '待绑定', pending_test: '待测试', local_model_required: '需要回环模型', ready: '就绪', disabled: '已关闭'
};
const modelInputs = { name: '#md-name', protocol: '#md-protocol', endpoint: '#md-endpoint', model_id: '#md-modelid' };
let settingsVisible = false;
let settingsTimer = 0;
// 用户主动读取（重新读取 / 合并新版本 / 保存后回读）与后台静默轮询使用独立令牌：
// 轮询不得取消用户主动发起的读取；用户读取期间静默轮询让位。
let settingsRead = 0;
let settingsQuietRead = 0;
let settingsUserLoads = 0;
let settingsLoadFailures = 0;
let settingsPollHalted = false;
let lastConfigResponse = '';
let editingModel = null;
const draftBadges = new Map();
const probeStatuses = new Map(); // model.id → { text, error, detail }，跨增量刷新保留
const probingModels = new Set();

function configDrafts() {
  const c = settingsState.config;
  const draft = {};
  if (!c.loaded) return draft;
  if ($('#cfg-profile').value !== c.profile) draft.profile = $('#cfg-profile').value;
  if ($('#cfg-vector').checked !== c.vectorEnabled) draft.vector = $('#cfg-vector').checked;
  for (const purpose of ['extract', 'embed']) {
    const chosen = $('#bind-' + purpose).value;
    if (chosen !== (c.bindings[purpose] || '')) draft[purpose] = chosen;
  }
  return draft;
}

// 与编辑快照 / 表单默认值比较： pristine 编辑器不算草稿，
// 避免切换模型时误弹确认、误挂“未保存”标记。
function modelDrafted() {
  if (editingModel) {
    for (const [field, selector] of Object.entries(modelInputs)) {
      if ($(selector).value !== String(editingModel[field] ?? '')) return true;
    }
    return $('#md-api-key').value !== '' || $('#md-clear-key').checked;
  }
  const form = $('#model-form');
  return [...form.querySelectorAll('input')].some((field) => field.type !== 'checkbox' && field.value !== '') ||
    $('#md-protocol').value !== 'openai_chat';
}

function updateDraftBadges() {
  const drafts = configDrafts();
  const show = (formId, on) => {
    const badge = draftBadges.get(formId);
    if (badge) badge.hidden = !on;
  };
  show('profile-form', 'profile' in drafts);
  show('vector-form', 'vector' in drafts);
  show('bindings-form', 'extract' in drafts || 'embed' in drafts);
  show('model-form', modelDrafted());
}

function showSettingsMessage(node, message, error = false) {
  node.replaceChildren(el('span', { class: error ? 'settings-error' : 'settings-feedback', text: message }));
  node.hidden = false;
}

function diagnosticText(res) {
  return [
    res.method + ' ' + res.path,
    res.status || res.networkError,
    res.code,
    res.requestId,
    res.data && typeof res.data.message === 'string' ? res.data.message : null
  ].filter(Boolean).join(' · ');
}

// 错误码 → 中文标题 + 下一步（共享 errorHeadline）；原始英文只留在「技术详情」。
function showSettingsError(node, res) {
  node.replaceChildren(
    el('span', { class: 'settings-error', text: errorHeadline(res) }),
    el('details', { class: 'diagnostic' }, [
      el('summary', { text: '技术详情' }),
      el('pre', { text: diagnosticText(res) })
    ])
  );
  node.hidden = false;
}

// 后端按 URL host 判定回环；localhost 不被识别，UI 先归一化再提交 / 检测。
function normalizeLoopbackEndpoint(endpoint) {
  return String(endpoint).replace(/^(https?:\/\/)localhost(?=[:/]|$)/i, (_match, scheme) => scheme + '127.0.0.1');
}
function isLoopbackEndpoint(endpoint) {
  try {
    const host = new URL(endpoint).hostname.replace(/^\[|\]$/g, '').toLowerCase();
    return host === 'localhost' || host === '::1' || /^127(\.\d+){3}$/.test(host);
  } catch (_) {
    return false;
  }
}

const defaultOllamaEndpoint = 'http://127.0.0.1:11434';
let ollamaDiscoveryTimer = 0;
let ollamaDiscoveryRevision = 0;
let ollamaDiscoveryBusy = false;
let ollamaDiscoveryQueued = false;

function updateOllamaDiscovery(delay = 0) {
  clearTimeout(ollamaDiscoveryTimer);
  const revision = ++ollamaDiscoveryRevision;
  const region = $('#md-ollama-discovery');
  region.hidden = !settingsVisible || !$('#model-editor').open || $('#md-protocol').value !== 'ollama';
  if (region.hidden) return;
  // 保留上一轮结果直到新结果到达，避免输入 BASE_URL 时选项闪烁；只更新状态行。
  const status = $('#md-ollama-status');
  $('#md-ollama-copy-status').textContent = '';
  status.classList.add('loading-line');
  status.replaceChildren(
    el('span', { class: 'btn__spin', 'aria-hidden': 'true' }),
    el('span', { text: '正在检测本机 Ollama…' })
  );
  ollamaDiscoveryTimer = setTimeout(() => runOllamaDiscovery(revision), delay);
}

// 单飞：同一时刻只允许一个 discover 请求在途；期间的新请求排队，完成后按最新修订重跑。
async function runOllamaDiscovery(revision) {
  if (ollamaDiscoveryBusy) {
    ollamaDiscoveryQueued = true;
    return;
  }
  ollamaDiscoveryBusy = true;
  const refresh = $('#md-ollama-refresh');
  busy(refresh, true, '正在检测…');
  const status = $('#md-ollama-status');
  const options = $('#md-ollama-options');
  const download = $('#md-ollama-download');
  try {
    const raw = $('#md-endpoint').value.trim();
    const endpoint = normalizeLoopbackEndpoint(raw || defaultOllamaEndpoint);
    const res = await api('POST', '/v2/models/ollama/discover', { body: { endpoint } });
    if (revision !== ollamaDiscoveryRevision) return;
    status.classList.remove('loading-line');
    if (!res.ok) {
      options.hidden = download.hidden = true;
      status.textContent = res.status === 503
        ? '无法连接 Ollama。请确认服务正在运行（ollama serve），然后重新检测。'
        : errorHeadline(res);
      return;
    }
    // 引导按“草稿中的”向量开关给出：勾选尚未保存时也按开启提示。
    const embed = settingsState.config.loaded && $('#cfg-vector').checked;
    const installed = res.data.models;
    const embeddings = installed.filter((model) => model.embedding === true);
    const candidates = embed ? [...embeddings, ...installed.filter((model) => model.embedding !== true)] : installed;
    const select = $('#md-ollama-models');
    select.replaceChildren(el('option', { value: '', text: '选择模型以填写 model_name' }));
    for (const model of candidates) {
      const usage = model.embedding === true ? ' · Embedding' : model.embedding === null ? ' · 用途未确认' : '';
      select.add(el('option', { value: model.name, text: model.name + usage }));
    }
    options.hidden = candidates.length === 0;
    if (!raw || /^https?:\/\/localhost(?=[:/]|$)/i.test(raw)) {
      $('#md-endpoint').value = res.data.endpoint;
    }
    const modelField = $('#md-modelid');
    if (embed && !modelField.value.trim() && embeddings.length) {
      modelField.value = (embeddings.find((model) => model.name === 'bge-m3:latest') || embeddings[0]).name;
    }
    select.value = candidates.some((model) => model.name === modelField.value) ? modelField.value : '';
    download.hidden = !embed || embeddings.length > 0;
    status.textContent = embed
      ? embeddings.length
        ? '发现 ' + embeddings.length + ' 个 Embedding 模型。添加后可测试嵌入，并在用途绑定中选用。'
        : installed.some((model) => model.embedding === null)
          ? '未确认已安装模型的 Embedding 能力。可以选择候选模型后测试，或下载推荐模型。'
          : '本机尚未安装 Embedding 模型。'
      : installed.length ? '已发现 ' + installed.length + ' 个本机模型。' : '本机尚未安装模型。';
    updateDraftBadges();
  } finally {
    ollamaDiscoveryBusy = false;
    busy(refresh, false);
    if (ollamaDiscoveryQueued) {
      ollamaDiscoveryQueued = false;
      if (!$('#md-ollama-discovery').hidden && revision !== ollamaDiscoveryRevision) {
        runOllamaDiscovery(ollamaDiscoveryRevision);
      }
    }
  }
}

function applyConfig(data, drafts = {}) {
  const c = settingsState.config;
  const vectorChanged = c.vectorEnabled !== data.vector_search.enabled;
  Object.assign(c, {
    revision: data.config_revision,
    profile: data.profile,
    vectorEnabled: data.vector_search.enabled,
    models: data.models,
    bindings: data.bindings,
    activity: data.activity,
    loaded: true
  });
  $('#cfg-rev').textContent = '配置版本 ' + c.revision;
  $('#cfg-profile').value = drafts.profile ?? c.profile;
  $('#cfg-profile-note').textContent = profileNotes[$('#cfg-profile').value];
  $('#cfg-vector').checked = drafts.vector ?? c.vectorEnabled;
  rebuildModelSelects(drafts);
  renderModelsList();
  renderActivity();
  updateDraftBadges();
  if (vectorChanged) updateOllamaDiscovery();
}

async function loadConfig({ reconcile = false, quiet = false } = {}) {
  if (quiet && settingsUserLoads > 0) return; // 用户主动读取优先，静默轮询让位
  const request = quiet ? ++settingsQuietRead : ++settingsRead;
  if (!quiet) {
    settingsUserLoads += 1;
    ++settingsQuietRead; // 作废在途的静默轮询
  }
  if (!settingsState.config.loaded) {
    setLoading($('#cfg-result'), '正在读取本机配置…');
    $('#cfg-result').hidden = false;
  }
  let res;
  try {
    res = await api('GET', '/v2/config');
  } finally {
    if (!quiet) settingsUserLoads -= 1;
  }
  if (quiet ? (request !== settingsQuietRead || settingsUserLoads > 0) : request !== settingsRead) return;
  if (!res.ok) {
    showSettingsError($('#cfg-result'), res);
    if (!settingsState.config.loaded && !$('#models-list .empty')) {
      $('#models-list').replaceChildren(
        emptyMsg('尚未读取配置基线，模型列表暂不可用。', { label: '重新读取', run: () => loadConfig() })
      );
    }
    // 会话失效 / 连接中断：连续失败后暂停轮询，挂出持久提示与“重新读取”动作。
    if (res.status === 401 || res.networkError || res.status === 0) {
      settingsLoadFailures += 1;
      if (settingsLoadFailures >= (res.status === 401 ? 2 : 3)) {
        settingsPollHalted = true;
        clearTimeout(settingsTimer);
        const reload = el('button', { class: 'btn btn--sm', type: 'button', text: '重新读取' });
        reload.addEventListener('click', restartSettingsPolling);
        $('#cfg-result').appendChild(reload);
        announce(res.status === 401 ? '本机会话已结束，自动刷新已暂停。' : '无法连接本机服务，自动刷新已暂停。');
      }
    }
    return;
  }
  settingsLoadFailures = 0;
  settingsPollHalted = false;
  $('#cfg-result').hidden = true;
  const banner = $('#cfg-banner');
  if (quiet && lastConfigResponse === res.raw && banner.hidden) {
    armSettingsPoll();
    return;
  }
  const data = res.data;
  const drafts = configDrafts();
  const hasDraft = Object.keys(drafts).length || modelDrafted() || settingsState.pendingConfig;
  if (!reconcile && hasDraft && settingsState.config.loaded && data.config_revision !== settingsState.config.revision) {
    if (banner.dataset.revision !== String(data.config_revision) || banner.hidden) showConfigBanner(data);
    armSettingsPoll();
    return;
  }
  applyConfig(data, drafts);
  lastConfigResponse = res.raw;
  banner.hidden = true;
  if (reconcile && hasDraft) announce('已读取最新配置，保留了正在编辑的字段。');
  armSettingsPoll();
}

function armSettingsPoll() {
  clearTimeout(settingsTimer);
  if (settingsVisible && !document.hidden && !settingsPollHalted) {
    settingsTimer = setTimeout(checkSettingsPoll, 3200);
  }
}

async function checkSettingsPoll() {
  clearTimeout(settingsTimer);
  if (!settingsVisible || document.hidden || settingsPollHalted) return;
  if (!settingsState.savingConfig) await loadConfig({ quiet: true });
  armSettingsPoll();
}

function restartSettingsPolling() {
  settingsLoadFailures = 0;
  settingsPollHalted = false;
  $('#cfg-result').replaceChildren();
  $('#cfg-result').hidden = true;
  if (settingsVisible && !document.hidden) checkSettingsPoll();
}

function setSettingsVisible(visible) {
  const changed = settingsVisible !== visible;
  settingsVisible = visible;
  if (changed) updateOllamaDiscovery();
  clearTimeout(settingsTimer);
  if (!visible || document.hidden) {
    ++settingsRead;
    ++settingsQuietRead;
    return;
  }
  checkSettingsPoll();
}

// 保存期间禁用全部提交 / 测试 / 移除按钮，并给出可见的“正在保存…”进度。
function setSettingsSaving(on) {
  for (const btn of $$('#view-settings form button[type="submit"]')) btn.disabled = on;
  syncModelButtons();
}

function syncModelButtons() {
  for (const row of $$('#models-list [data-key]')) {
    const probing = probingModels.has(row.dataset.key);
    for (const btn of $$('[data-probe]', row)) {
      btn.disabled = settingsState.savingConfig || probing || btn.dataset.vectorOff === 'true';
    }
    for (const btn of $$('[data-remove]', row)) {
      btn.disabled = settingsState.savingConfig || btn.dataset.bound === 'true';
    }
  }
}

function patchConfig(patch, selector, afterSave, button) {
  const container = $(selector);
  if (settingsState.savingConfig) {
    if (container !== settingsState.pendingConfig?.container) {
      showSettingsMessage(container, '正在保存上一笔更改，请稍候…', true);
    }
    return;
  }
  if (!settingsState.config.loaded) {
    showSettingsMessage(container, '尚未读取配置基线，请先点“重新读取”。', true);
    return;
  }
  if (settingsState.pendingConfig) {
    // 可重试的旧提交仍在：不允许新保存静默丢弃它，先重试或显式放弃。
    const pending = settingsState.pendingConfig;
    showSettingsMessage(pending.container, '上一笔提交尚未完成。可重试同一提交，或放弃后再保存新的更改。', true);
    appendPendingButtons(pending.container, pending);
    if (container !== pending.container) {
      showSettingsMessage(container, '有一笔未完成的提交，请先重试或放弃。', true);
    }
    return;
  }
  hideConfigRetry();
  settingsState.pendingConfig = {
    body: { expected_revision: settingsState.config.revision, patch },
    key: newKey(),
    container,
    afterSave,
    button
  };
  sendConfigIntent(settingsState.pendingConfig);
}

async function sendConfigIntent(intent) {
  if (settingsState.savingConfig || settingsState.pendingConfig !== intent) return;
  const { body, key, container, afterSave, button } = intent;
  ++settingsRead;
  ++settingsQuietRead;
  const retryFocused = document.activeElement?.classList.contains('cfg-retry');
  hideConfigRetry();
  settingsState.savingConfig = true;
  setBusy(true);
  busy(button, true, '正在保存…');
  setSettingsSaving(true);
  setLoading(container, '正在保存…');
  container.hidden = false;
  try {
    const res = await api('PATCH', '/v2/config', { body, idempotencyKey: key });
    if (res.ok) {
      const drafts = configDrafts();
      if (drafts.profile === body.patch.profile) delete drafts.profile;
      if (drafts.vector === body.patch.vector_search?.enabled) delete drafts.vector;
      for (const purpose of ['extract', 'embed']) {
        if (
          body.patch.bindings &&
          purpose in body.patch.bindings &&
          drafts[purpose] === (body.patch.bindings[purpose] || '')
        )
          delete drafts[purpose];
      }
      settingsState.pendingConfig = null;
      $('#cfg-banner').hidden = true;
      if (afterSave) afterSave();
      // A write receipt contains only the touched model or binding. Read back the full CLI snapshot.
      const c = settingsState.config;
      const receipt = res.data;
      const saved = {
        ...receipt,
        models: c.models,
        bindings: receipt.bindings ?? c.bindings,
        activity: { ...c.activity, ...receipt.activity }
      };
      if (receipt.model_result) {
        const { op, ...model } = receipt.model_result;
        const original = c.models.find((item) => item.id === model.id);
        const remaining = c.models.filter((item) => item.id !== model.id);
        if (op === 'remove') {
          saved.models = remaining;
        } else {
          const merged = { ...original, ...model };
          // 协议 / 地址 / 模型标识 / 凭据变化会使旧测试失效：回读前不再沿用 tested。
          const invalidated = original && ['protocol', 'endpoint', 'model_id', 'credential_configured'].some(
            (field) => merged[field] !== original[field]
          );
          if (invalidated) {
            delete merged.tested;
            probeStatuses.delete(model.id); // 旧测试回执不再可信
          } else if (!merged.tested) merged.tested = [];
          merged.local_endpoint = isLoopbackEndpoint(merged.endpoint);
          saved.models = [...remaining, merged];
        }
      }
      applyConfig(saved, drafts);
      lastConfigResponse = '';
      await loadConfig({ quiet: true });
      showSettingsMessage(container, '已保存到本机配置。');
      toast('更改已保存到本机配置（bmem 工作区）。', 'ok');
      announce('设置已保存。');
    } else {
      showSettingsError(container, res);
      toast('保存失败：' + errorHeadline(res), 'err');
      if (res.status === 409 && res.code === 'revision_changed') {
        settingsState.pendingConfig = null;
        showConfigBanner();
      } else if (res.retryable || res.status === 0 || res.status >= 500) {
        appendPendingButtons(container, intent);
        if (retryFocused) $('.cfg-retry', container)?.focus({ preventScroll: true });
      } else {
        settingsState.pendingConfig = null;
      }
    }
  } finally {
    busy(button, false);
    settingsState.savingConfig = false;
    setSettingsSaving(false);
    setBusy(false);
  }
}

function hideConfigRetry() {
  $$('.cfg-retry, .cfg-discard').forEach((button) => button.remove());
}

function appendPendingButtons(container, intent) {
  $$('.cfg-retry, .cfg-discard', container).forEach((button) => button.remove());
  const retry = el('button', { class: 'btn btn--sm cfg-retry', type: 'button', text: '重试同一提交' });
  retry.addEventListener('click', () => sendConfigIntent(intent));
  const discard = el('button', { class: 'btn btn--sm cfg-discard', type: 'button', text: '放弃该提交' });
  discard.addEventListener('click', abandonPendingConfig);
  container.append(retry, discard);
}

function abandonPendingConfig() {
  const intent = settingsState.pendingConfig;
  if (!intent || settingsState.savingConfig) return;
  settingsState.pendingConfig = null;
  hideConfigRetry();
  showSettingsMessage(intent.container, '已放弃未完成的提交。可修改后重新保存。');
  announce('已放弃未完成的提交。');
  updateDraftBadges();
}

function showConfigBanner(latest) {
  const banner = $('#cfg-banner');
  const revision = latest?.config_revision;
  banner.dataset.revision = revision ?? '';
  const changes = [];
  if (latest && latest.profile !== settingsState.config.profile) {
    const option = [...$('#cfg-profile').options].find((item) => item.value === latest.profile);
    if (option) changes.push('当前模式为' + option.textContent);
  }
  if (latest && latest.vector_search.enabled !== settingsState.config.vectorEnabled) {
    changes.push('向量检索已' + (latest.vector_search.enabled ? '开启' : '关闭'));
  }
  const reload = el('button', { class: 'btn btn--sm', type: 'button', text: '读取新版本并保留草稿' });
  reload.addEventListener('click', () => {
    busy(reload, true, '正在读取最新配置…');
    loadConfig({ reconcile: true }).finally(() => busy(reload, false));
  });
  banner.className = 'banner banner--conflict';
  banner.replaceChildren(
    el('strong', { text: revision == null ? '配置已在其他位置更新。' : '发现新的配置版本 ' + revision + '。' }),
    el('span', {
      text: (changes.length ? changes.join('，') + '。' : '') + '已保留你的编辑。读取新版本后可以检查并保存。'
    }),
    reload
  );
  banner.hidden = false;
}

function attachDraftBadge(form, submitBtn) {
  const badge = pill('有未保存的更改', 'warn');
  badge.classList.add('settings-draft-pill');
  badge.hidden = true;
  if (form.id === 'model-form') {
    submitBtn.insertAdjacentElement('afterend', badge);
  } else {
    const row = el('div', { class: 'settings-submit-row' });
    form.insertBefore(row, submitBtn);
    row.append(submitBtn, badge);
  }
  draftBadges.set(form.id, badge);
}

function bindSettingsForms() {
  document.addEventListener('visibilitychange', () => setSettingsVisible(settingsVisible));
  window.addEventListener('focus', () => {
    if (settingsVisible) setSettingsVisible(true);
  });
  for (const formId of ['profile-form', 'vector-form', 'bindings-form', 'model-form']) {
    const form = $('#' + formId);
    attachDraftBadge(form, $('button[type="submit"]', form));
  }
  $('#view-settings').addEventListener('input', updateDraftBadges);
  $('#view-settings').addEventListener('change', updateDraftBadges);
  $('#cfg-profile').addEventListener('change', (e) => {
    $('#cfg-profile-note').textContent = profileNotes[e.target.value];
  });
  $('#profile-form').addEventListener('submit', (e) => {
    e.preventDefault();
    patchConfig({ profile: $('#cfg-profile').value }, '#profile-result', undefined, e.submitter);
  });
  $('#vector-form').addEventListener('submit', (e) => {
    e.preventDefault();
    patchConfig({ vector_search: { enabled: $('#cfg-vector').checked } }, '#vector-result', undefined, e.submitter);
  });
  $('#bindings-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const bindings = {};
    for (const purpose of ['extract', 'embed']) {
      const value = $('#bind-' + purpose).value || null;
      if (value !== settingsState.config.bindings[purpose]) bindings[purpose] = value;
    }
    if (!Object.keys(bindings).length) {
      showSettingsMessage($('#bindings-result'), '用途绑定没有变化。');
      return;
    }
    const patch = { bindings };
    let alsoVector = false;
    if (bindings.embed) {
      // 预检：向量检索关闭时嵌入绑定必然失败，先给出引导而不是提交注定失败的请求。
      const vectorDraftOn = settingsState.config.loaded ? $('#cfg-vector').checked : settingsState.config.vectorEnabled;
      if (!vectorDraftOn) {
        showSettingsMessage($('#bindings-result'), '向量相似检索尚未开启，嵌入绑定无法生效。请先开启“向量相似检索”并保存，再绑定嵌入模型。', true);
        return;
      }
      if (!settingsState.config.vectorEnabled) {
        patch.vector_search = { enabled: true }; // 草稿已勾选：随本次提交一并开启
        alsoVector = true;
      }
    }
    patchConfig(patch, '#bindings-result', alsoVector ? () => toast('已同时开启向量相似检索。', 'info') : undefined, e.submitter);
  });
  $('#model-form').addEventListener('submit', (e) => {
    e.preventDefault();
    submitModel();
  });
  $('#model-cancel').addEventListener('click', () => {
    closeModelEdit();
    $('#model-editor summary').focus();
  });
  $('#md-clear-key').addEventListener('change', () => {
    if ($('#md-clear-key').checked) $('#md-api-key').value = '';
  });
  $('#md-api-key').addEventListener('input', () => {
    if ($('#md-api-key').value) $('#md-clear-key').checked = false;
  });
  $('#md-protocol').addEventListener('change', () => {
    $('#md-endpoint').placeholder = {
      openai_chat: 'https://api.openai.com/v1',
      openai_embeddings: 'https://api.openai.com/v1',
      anthropic_messages: 'https://api.anthropic.com/v1',
      ollama: 'http://127.0.0.1:11434'
    }[$('#md-protocol').value];
    if ($('#md-protocol').value === 'ollama' && !$('#md-endpoint').value.trim()) {
      $('#md-endpoint').value = defaultOllamaEndpoint;
    }
    updateOllamaDiscovery();
  });
  $('#model-editor').addEventListener('toggle', () => updateOllamaDiscovery());
  $('#md-endpoint').addEventListener('input', () => updateOllamaDiscovery(350));
  $('#md-ollama-refresh').addEventListener('click', () => updateOllamaDiscovery());
  $('#md-ollama-models').addEventListener('change', (e) => {
    if (e.target.value) $('#md-modelid').value = e.target.value;
  });
  $('#md-ollama-copy').addEventListener('click', async () => {
    const status = $('#md-ollama-copy-status');
    try {
      await navigator.clipboard.writeText($('#md-ollama-command').textContent);
      status.textContent = '已复制。请在终端运行。';
    } catch {
      status.textContent = '请手动复制上方命令。';
    }
  });
  renderModelsList();
  updateDraftBadges();
}

function rebuildModelSelects(drafts) {
  const c = settingsState.config;
  for (const purpose of ['extract', 'embed']) {
    const select = $('#bind-' + purpose);
    const chosen = drafts[purpose] ?? c.bindings[purpose] ?? '';
    select.replaceChildren(el('option', { value: '', text: '未绑定' }));
    for (const model of c.models) {
      const compatible =
        model.protocol === 'ollama' ||
        (purpose === 'embed' ? model.protocol === 'openai_embeddings' : model.protocol !== 'openai_embeddings');
      if (!compatible && model.id !== chosen) continue;
      const local = model.local_endpoint !== false;
      const tested = model.tested?.includes(purpose);
      const unsupported = purpose === 'embed' && !local;
      select.add(
        el('option', {
          value: model.id,
          disabled: ((unsupported || !tested) && model.id !== chosen),
          text: model.name + (unsupported ? ' · 仅连接测试' : tested ? '' : ' · 待测试')
        })
      );
    }
    if (chosen && ![...select.options].some((option) => option.value === chosen)) {
      select.add(el('option', { value: chosen, text: chosen + ' · 已移除' }));
    }
    select.value = chosen;
  }
}

function renderProbeStatus(node, modelId) {
  if (!node) return;
  const entry = probeStatuses.get(modelId);
  node.replaceChildren();
  node.hidden = !entry;
  if (!entry) return;
  node.appendChild(el('span', { class: entry.error ? 'settings-error' : 'settings-feedback', text: entry.text }));
  if (entry.detail) {
    node.appendChild(
      el('details', { class: 'diagnostic' }, [
        el('summary', { text: '技术详情' }),
        el('pre', { text: entry.detail })
      ])
    );
  }
}

function renderModelCard(model) {
  const c = settingsState.config;
  const bound = ['extract', 'embed'].filter((purpose) => c.bindings[purpose] === model.id);
  const boundText = '该模型仍被' + bound.map((purpose) => purposeNames[purpose]).join('/') + '绑定，请先解绑';
  const head = [
    el('strong', { text: model.name }),
    pill(model.protocol),
    model.local_endpoint === false ? pill('服务器', 'muted') : null,
    model.credential_configured ? pill('API_KEY 已配置', 'ok') : null
  ];
  const meta = el('div', { class: 'metarow' }, [kv('模型', model.model_id), kv('地址', model.endpoint)]);
  const actions = el('div', { class: 'row-actions' });
  // 每张卡片独立的测试状态行：并发测试互不覆盖，且跨增量刷新保留。
  const statusLine = el('p', { class: 'settings-probe-status', role: 'status', hidden: true });
  renderProbeStatus(statusLine, model.id);
  const edit = el('button', { class: 'btn btn--sm', type: 'button', text: '编辑', 'data-focus-key': 'edit' });
  edit.addEventListener('click', () => editModel(model));
  actions.appendChild(edit);
  for (const purpose of ['extract', 'embed']) {
    if (
      purpose === 'embed'
        ? !['ollama', 'openai_embeddings'].includes(model.protocol)
        : model.protocol === 'openai_embeddings'
    )
      continue;
    const vectorOff = purpose === 'embed' && !c.vectorEnabled;
    const probe = el('button', {
      class: 'btn btn--sm',
      type: 'button',
      'data-probe': purpose,
      'data-focus-key': 'probe-' + purpose,
      'data-vector-off': vectorOff ? 'true' : 'false',
      disabled: vectorOff || settingsState.savingConfig,
      title: vectorOff ? '先开启向量相似检索' : undefined,
      text: (model.tested?.includes(purpose) ? '重新测试' : '测试') + purposeNames[purpose]
    });
    probe.addEventListener('click', () => probeModel(model, purpose, probe, statusLine));
    actions.appendChild(probe);
  }
  const remove = el('button', {
    class: 'btn btn--sm btn--danger',
    type: 'button',
    text: '移除',
    'data-remove': 'true',
    'data-focus-key': 'remove',
    'data-bound': bound.length ? 'true' : 'false',
    disabled: bound.length > 0 || settingsState.savingConfig,
    title: bound.length ? boundText : undefined
  });
  remove.addEventListener('click', () => removeModel(model, remove));
  actions.appendChild(remove);
  const body = [meta];
  if (bound.length) body.push(el('p', { class: 'hint-sm settings-bound-hint', text: boundText + '后再移除。' }));
  body.push(actions, statusLine);
  return card(head, body);
}

function renderModelsList() {
  const list = $('#models-list');
  if (!settingsState.config.loaded) {
    list.replaceChildren(
      el('div', { class: 'card settings-skeleton', 'aria-hidden': 'true' }, [
        el('span', { class: 'placeholder settings-skeleton__title' }),
        el('span', { class: 'placeholder settings-skeleton__line' }),
        el('span', { class: 'placeholder settings-skeleton__actions' })
      ])
    );
    return;
  }
  const models = settingsState.config.models;
  // 键控增量刷新：未变化的卡片原样保留，焦点 / 展开状态不丢失。
  // 行签名除模型本身外还包含向量开关与用途绑定：模型行未变而上下文变化时
  // 整卡重建，测试 / 移除按钮的禁用态、title 与绑定提示才不会停留在旧上下文。
  const c = settingsState.config;
  const rows = models.map((model) => ({ model, vectorEnabled: c.vectorEnabled, bindings: c.bindings }));
  reconcileList(list, rows, (row) => row.model.id, (row) => renderModelCard(row.model));
  if (!models.length && !list.querySelector('.empty')) {
    list.replaceChildren(
      emptyMsg('尚未添加模型。可连接已有 API，或填写本机 Ollama 地址。', {
        label: '添加模型',
        run: () => {
          $('#model-editor').open = true;
          $('#md-name').focus();
        }
      })
    );
  }
  syncModelButtons();
}

async function probeModel(model, purpose, probe, statusLine) {
  if (probingModels.has(model.id) || settingsState.savingConfig) return;
  probingModels.add(model.id);
  busy(probe, true, '正在测试…');
  syncModelButtons();
  probeStatuses.set(model.id, { text: '正在连接 ' + model.name + '…', error: false });
  renderProbeStatus(statusLine, model.id);
  try {
    const res = await api('POST', '/v2/models/' + encodeURIComponent(model.id) + '/test', { body: { purpose } });
    if (res.ok) {
      const test = purpose === 'embed' && model.local_endpoint === false ? '连接' : purposeNames[purpose];
      const text = model.name + ' 已通过' + test + '测试。';
      probeStatuses.set(model.id, { text, error: false });
      toast(text, 'ok');
      await loadConfig({ quiet: true });
    } else {
      const text = errorHeadline(res);
      probeStatuses.set(model.id, { text, error: true, detail: diagnosticText(res) });
      toast(model.name + ' 测试失败：' + text, 'err');
    }
  } finally {
    busy(probe, false);
    probingModels.delete(model.id);
    syncModelButtons();
    renderProbeStatus(statusLine, model.id);
  }
}

async function removeModel(model, removeBtn) {
  const bound = ['extract', 'embed'].filter((purpose) => settingsState.config.bindings[purpose] === model.id);
  if (bound.length) {
    showSettingsMessage(
      $('#catalog-result'),
      '该模型仍被' + bound.map((purpose) => purposeNames[purpose]).join('/') + '绑定，请先解绑后再移除。',
      true
    );
    return;
  }
  const ok = await confirmDialog({
    title: '移除模型',
    desc: '将从本机配置（bmem 工作区）移除模型「' + model.name + '」。不会删除远端服务上的模型，也不会删除本机已下载的模型文件。',
    confirmText: '移除模型',
    cancelText: '取消',
    danger: true,
    confirmString: model.name
  });
  if (!ok) return;
  patchConfig({ models: { op: 'remove', id: model.id } }, '#catalog-result', () => {
    if (editingModel?.id === model.id) closeModelEdit();
    probeStatuses.delete(model.id);
    $('#model-editor summary').focus({ preventScroll: true });
  }, removeBtn);
}

async function editModel(model) {
  if (modelDrafted()) {
    const ok = await confirmDialog({
      title: '替换当前模型草稿？',
      desc: '模型表单中还有未保存的更改。切换到「' + model.name + '」会替换这些草稿。',
      confirmText: '替换并编辑',
      cancelText: '保留草稿',
      danger: true
    });
    if (!ok) return;
  }
  editingModel = { ...model };
  for (const [field, selector] of Object.entries(modelInputs)) $(selector).value = model[field];
  $('#md-api-key').value = '';
  $('#md-clear-key').checked = false;
  $('#md-key-clear-row').hidden = !model.credential_configured;
  $('#md-key-hint').textContent = model.credential_configured
    ? '已配置。留空保留原密钥，填写新值则替换。'
    : '本地 Ollama 可留空。密钥保存后只显示配置状态。';
  $('#model-editor-title').textContent = '编辑模型 · ' + model.name;
  $('#model-submit').textContent = '保存模型';
  $('#model-cancel').hidden = false;
  $('#model-result').replaceChildren();
  $('#model-editor').open = true;
  updateOllamaDiscovery();
  updateDraftBadges();
  $('#md-name').focus();
}

function closeModelEdit() {
  editingModel = null;
  $('#model-form').reset();
  $('#md-key-clear-row').hidden = $('#model-cancel').hidden = true;
  $('#md-key-hint').textContent = '本地 Ollama 可留空。密钥保存后只显示配置状态。';
  $('#md-endpoint').placeholder = 'https://api.openai.com/v1';
  $('#model-editor-title').textContent = '新增模型';
  $('#model-submit').textContent = '添加模型';
  updateOllamaDiscovery();
  updateDraftBadges();
}

function submitModel() {
  // 提交前归一化回环地址，避免 http://localhost 被后端判为非回环。
  const endpointField = $('#md-endpoint');
  const rawEndpoint = endpointField.value.trim();
  const normalized = normalizeLoopbackEndpoint(rawEndpoint);
  if (normalized !== rawEndpoint) endpointField.value = normalized;
  const values = Object.fromEntries(
    Object.entries(modelInputs).map(([field, selector]) => [field, $(selector).value.trim()])
  );
  if (!values.name) values.name = values.model_id;
  const key = $('#md-api-key').value.trim();
  if (key) values.api_key = key;
  if (editingModel && $('#md-clear-key').checked) values.api_key = null;
  let models;
  if (editingModel) {
    const changes = Object.fromEntries(
      Object.entries(values).filter(([field, value]) => value !== editingModel[field])
    );
    if (!Object.keys(changes).length) {
      showSettingsMessage($('#model-result'), '模型配置没有变化。');
      return;
    }
    models = { op: 'update', id: editingModel.id, changes };
  } else models = { op: 'add', ...values };
  const fields = [...$('#model-form').querySelectorAll('input, select')];
  const submitted = fields.map((field) => field.type === 'checkbox' ? field.checked : field.value);
  patchConfig({ models }, '#model-result', () => {
    if (fields.every((field, i) => (field.type === 'checkbox' ? field.checked : field.value) === submitted[i])) {
      closeModelEdit();
    } else if ($('#md-api-key').value.trim() === key) {
      $('#md-api-key').value = '';
    }
  }, $('#model-submit'));
}

function renderActivity() {
  const box = $('#cfg-activity');
  box.hidden = !settingsState.config.activity;
  if (box.hidden) return;
  box.replaceChildren(
    ...Object.entries(purposeNames).map(([purpose, name]) => {
      const value = settingsState.config.activity[purpose];
      return pill(name + ' · ' + (readinessNames[value] || '未知'), value === 'ready' ? 'ok' : 'muted');
    })
  );
}

bindSettingsForms();
window.MemorySettings = { visible: setSettingsVisible, refresh: () => loadConfig({ reconcile: true }) };
