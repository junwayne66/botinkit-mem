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
let settingsRead = 0;
let lastConfigResponse = '';
let editingModel = null;

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

function modelDrafted() {
  if (editingModel) return true;
  const form = $('#model-form');
  return [...form.querySelectorAll('input')].some((field) => field.value !== '' && field.type !== 'checkbox') ||
    $('#md-protocol').value !== 'openai_chat';
}

function showSettingsMessage(node, message, error = false) {
  node.replaceChildren(el('span', { class: error ? 'settings-error' : 'settings-feedback', text: message }));
  node.hidden = false;
}

function showSettingsError(node, res) {
  const message = res.data?.message || res.networkError || '无法完成请求';
  showSettingsMessage(node, res.status === 401 ? '本机会话已结束。请在终端运行 bmem web 重新打开。' : message, true);
}

const defaultOllamaEndpoint = 'http://127.0.0.1:11434';
let ollamaDiscoveryTimer;
let ollamaDiscoveryRevision = 0;

function updateOllamaDiscovery(delay = 0) {
  clearTimeout(ollamaDiscoveryTimer);
  const revision = ++ollamaDiscoveryRevision;
  const region = $('#md-ollama-discovery');
  region.hidden = !settingsVisible || !$('#model-editor').open || $('#md-protocol').value !== 'ollama';
  if (region.hidden) return;
  const options = $('#md-ollama-options');
  const download = $('#md-ollama-download');
  const status = $('#md-ollama-status');
  options.hidden = download.hidden = true;
  $('#md-ollama-copy-status').textContent = '';
  status.textContent = '正在检测本机 Ollama…';
  ollamaDiscoveryTimer = setTimeout(async () => {
    const endpoint = $('#md-endpoint').value.trim() || defaultOllamaEndpoint;
    const res = await api('POST', '/v2/models/ollama/discover', { body: { endpoint } });
    if (revision !== ollamaDiscoveryRevision) return;
    if (!res.ok) {
      status.textContent = res.status === 503
        ? '无法连接 Ollama。请确认服务正在运行（ollama serve），然后重新检测。'
        : res.data?.message || '检测失败，请检查 BASE_URL 后重试。';
      return;
    }
    const embed = settingsState.config.vectorEnabled;
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
    if (!$('#md-endpoint').value.trim() || /^http:\/\/localhost(?=[:/]|$)/i.test(endpoint)) {
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
  }, delay);
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
  if (vectorChanged) updateOllamaDiscovery();
}

async function loadConfig({ reconcile = false, quiet = false } = {}) {
  const request = ++settingsRead;
  const res = await api('GET', '/v2/config');
  if (request !== settingsRead) return;
  if (!res.ok) {
    showSettingsError($('#cfg-result'), res);
    return;
  }
  $('#cfg-result').hidden = true;
  const banner = $('#cfg-banner');
  if (quiet && lastConfigResponse === res.raw && banner.hidden) return;
  const data = res.data;
  const drafts = configDrafts();
  const hasDraft = Object.keys(drafts).length || modelDrafted() || settingsState.pendingConfig;
  if (!reconcile && hasDraft && settingsState.config.loaded && data.config_revision !== settingsState.config.revision) {
    if (banner.dataset.revision !== String(data.config_revision) || banner.hidden) showConfigBanner(data);
    return;
  }
  applyConfig(data, drafts);
  lastConfigResponse = res.raw;
  banner.hidden = true;
  if (reconcile && hasDraft) announce('已读取最新配置，保留了正在编辑的字段。');
}

function setSettingsVisible(visible) {
  const changed = settingsVisible !== visible;
  settingsVisible = visible;
  if (changed) updateOllamaDiscovery();
  clearTimeout(settingsTimer);
  if (!visible || document.hidden) {
    ++settingsRead;
    return;
  }
  async function check() {
    clearTimeout(settingsTimer);
    if (!settingsVisible || document.hidden) return;
    if (!settingsState.savingConfig) await loadConfig({ quiet: true });
    if (settingsVisible && !document.hidden) {
      clearTimeout(settingsTimer);
      settingsTimer = setTimeout(check, 3200);
    }
  }
  check();
}

function patchConfig(patch, selector, afterSave) {
  if (settingsState.savingConfig) return;
  const container = $(selector);
  if (!settingsState.config.loaded) {
    showSettingsMessage(container, '尚未读取配置基线，请先点“重新读取”。', true);
    return;
  }
  hideConfigRetry();
  settingsState.pendingConfig = {
    body: { expected_revision: settingsState.config.revision, patch },
    key: newKey(),
    container,
    afterSave
  };
  sendConfigIntent(settingsState.pendingConfig);
}

async function sendConfigIntent(intent) {
  if (settingsState.savingConfig || settingsState.pendingConfig !== intent) return;
  const { body, key, container, afterSave } = intent;
  ++settingsRead;
  settingsState.savingConfig = true;
  setBusy(true);
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
        saved.models = op === 'remove' ? remaining : [...remaining, { ...original, ...model }];
      }
      applyConfig(saved, drafts);
      lastConfigResponse = '';
      await loadConfig({ quiet: true });
      showSettingsMessage(container, '已保存到本机配置。');
      announce('设置已保存。');
    } else {
      showSettingsError(container, res);
      if (res.status === 409 && res.code === 'revision_changed') {
        settingsState.pendingConfig = null;
        showConfigBanner();
      } else if (res.retryable || res.status === 0 || res.status >= 500) {
        const retry = el('button', { class: 'btn btn--sm cfg-retry', type: 'button', text: '重试同一提交' });
        retry.addEventListener('click', () => sendConfigIntent(intent));
        container.appendChild(retry);
      } else {
        settingsState.pendingConfig = null;
      }
    }
  } finally {
    settingsState.savingConfig = false;
    setBusy(false);
  }
}

function hideConfigRetry() {
  $$('.cfg-retry').forEach((button) => button.remove());
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
  reload.addEventListener('click', () => loadConfig({ reconcile: true }));
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

function bindSettingsForms() {
  document.addEventListener('visibilitychange', () => setSettingsVisible(settingsVisible));
  window.addEventListener('focus', () => {
    if (settingsVisible) setSettingsVisible(true);
  });
  $('#cfg-profile').addEventListener('change', (e) => {
    $('#cfg-profile-note').textContent = profileNotes[e.target.value];
  });
  $('#profile-form').addEventListener('submit', (e) => {
    e.preventDefault();
    patchConfig({ profile: $('#cfg-profile').value }, '#profile-result');
  });
  $('#vector-form').addEventListener('submit', (e) => {
    e.preventDefault();
    patchConfig({ vector_search: { enabled: $('#cfg-vector').checked } }, '#vector-result');
  });
  $('#bindings-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const bindings = {};
    for (const purpose of ['extract', 'embed']) {
      const value = $('#bind-' + purpose).value || null;
      if (value !== settingsState.config.bindings[purpose]) bindings[purpose] = value;
    }
    if (Object.keys(bindings).length) patchConfig({ bindings }, '#bindings-result');
    else showSettingsMessage($('#bindings-result'), '用途绑定没有变化。');
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

function renderModelsList() {
  const list = $('#models-list');
  if (!settingsState.config.models.length) {
    list.replaceChildren(emptyMsg('尚未添加模型。可连接已有 API，或填写本机 Ollama 地址。'));
    return;
  }
  const rows = settingsState.config.models.map((model) => {
    const head = [
      el('strong', { text: model.name }),
      pill(model.protocol),
      model.local_endpoint === false ? pill('服务器', 'muted') : null,
      model.credential_configured ? pill('API_KEY 已配置', 'ok') : null
    ];
    const meta = el('div', { class: 'metarow' }, [kv('模型', model.model_id), kv('地址', model.endpoint)]);
    const actions = el('div', { class: 'row-actions' });
    const edit = el('button', { class: 'btn btn--sm', type: 'button', text: '编辑' });
    edit.addEventListener('click', () => editModel(model));
    actions.appendChild(edit);
    for (const purpose of ['extract', 'embed']) {
      if (
        purpose === 'embed'
          ? !['ollama', 'openai_embeddings'].includes(model.protocol)
          : model.protocol === 'openai_embeddings'
      )
        continue;
      const disabled = purpose === 'embed' && !settingsState.config.vectorEnabled;
      const probe = el('button', {
        class: 'btn btn--sm',
        type: 'button',
        disabled,
        title: disabled ? '先开启向量相似检索' : undefined,
        text: (model.tested?.includes(purpose) ? '重新测试' : '测试') + purposeNames[purpose]
      });
      probe.addEventListener('click', async () => {
        probe.disabled = true;
        const output = $('#catalog-result');
        showSettingsMessage(output, '正在连接 ' + model.name + '…');
        const res = await api('POST', '/v2/models/' + encodeURIComponent(model.id) + '/test', { body: { purpose } });
        if (res.ok) {
          const test = purpose === 'embed' && model.local_endpoint === false ? '连接' : purposeNames[purpose];
          showSettingsMessage(output, model.name + ' 已通过' + test + '测试。');
          await loadConfig({ quiet: true });
        } else showSettingsError(output, res);
        probe.disabled = false;
      });
      actions.appendChild(probe);
    }
    const remove = el('button', { class: 'btn btn--sm btn--danger', type: 'button', text: '移除' });
    remove.addEventListener('click', () => {
      if (window.confirm('从本机配置移除模型「' + model.name + '」？')) {
        patchConfig({ models: { op: 'remove', id: model.id } }, '#catalog-result', () => {
          if (editingModel?.id === model.id) closeModelEdit();
        });
      }
    });
    actions.appendChild(remove);
    return card(head, [meta, actions]);
  });
  list.replaceChildren(...rows);
}

function editModel(model) {
  if (modelDrafted() && !window.confirm('切换模型会替换当前模型草稿，继续编辑？')) return;
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
}

function submitModel() {
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
  });
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
