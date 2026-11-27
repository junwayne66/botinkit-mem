'use strict';

window.MemorySearch = (() => {
  const actors = { user_message: '本人对话', assistant_message: '助手回复', tool_result: '工具回执' };
  const modes = { lexical: '关键词', vector: '语义', hybrid: '混合' };
  const channels = { exact: '精确编号', lexical: '关键词', semantic: '语义' };
  const filterGroups = {
    '数据来源': { feishu: '飞书', codex: 'Codex', 'claude-code': 'Claude Code', dsh: 'DeepSeek Harness' },
    '内容类型': { ...actors, statement: '本人声明' },
    '飞书类别': { group: '飞书群聊', dm: '飞书私聊', docs: '飞书文档', feishu_unclassified: '未分类飞书记录' },
  };
  const fallback = {
    vector_not_ready: '向量检索尚未就绪',
    vector_provider_unavailable: '嵌入模型暂时不可用',
    vector_scan_timeout: '语义检索超时，可用排除条件缩小范围后重试',
    vector_unavailable: '语义检索暂时不可用',
  };
  // 服务端截断边界（retrieval.rs / vector.rs）：声明前 25 条，证据关键词前 30 条、语义前 20 条。
  // 命中上限时如实提示“仅显示前 N 条”，不把截断当总数。
  const CAP_STATEMENTS = 25;
  const CAP_EVIDENCE = { lexical: 30, vector: 20, hybrid: 50 };

  function open() {
    if ($('.ms-dialog[open]')) return;
    let mode = 'lexical';
    let revision = 0;
    let timer;
    const input = el('input', {
      id: 'ms-input', type: 'search', placeholder: '输入关键词…', autocomplete: 'off',
      spellcheck: 'false', 'aria-label': '搜索关键词', 'aria-controls': 'ms-results',
    });
    const close = el('button', { class: 'ms-close', type: 'button', 'aria-label': '关闭搜索', text: '关闭' });
    const modeBar = el('div', { class: 'ms-modes', role: 'group', 'aria-label': '检索方式' });
    for (const [id, label] of Object.entries(modes)) {
      const button = el('button', { type: 'button', text: label, 'aria-pressed': String(id === mode) });
      button.onclick = () => {
        if (mode === id) return;
        for (const item of modeBar.children) item.setAttribute('aria-pressed', String(item === button));
        mode = id;
        schedule();
      };
      modeBar.append(button);
    }

    const excluded = new Set();
    const countLabel = el('span', { class: 'ms-filter-count' });
    const summary = el('summary', {}, [el('span', { text: '排除条件' }), countLabel]);
    const reset = el('button', { type: 'button', class: 'btn btn--sm', text: '清除排除条件', hidden: true });
    const filters = el('details', { class: 'ms-filters' }, [summary]);
    for (const [group, options] of Object.entries(filterGroups)) {
      const controls = el('div', { class: 'ms-options' });
      for (const [id, label] of Object.entries(options)) {
        const checkbox = el('input', { type: 'checkbox', value: id });
        checkbox.onchange = () => {
          if (checkbox.checked) excluded.add(id);
          else excluded.delete(id);
          updateFilters();
        };
        controls.append(el('label', {}, [checkbox, label]));
      }
      filters.append(el('fieldset', {}, [el('legend', { text: group }), controls]));
    }
    filters.append(reset);
    reset.onclick = () => {
      for (const box of filters.querySelectorAll('input:checked')) box.checked = false;
      excluded.clear();
      summary.focus();
      updateFilters();
    };

    const status = el('p', { id: 'ms-status', role: 'status', text: '搜索当前声明和已采集的事件。' });
    const results = el('div', { id: 'ms-results', role: 'region', 'aria-label': '搜索结果' });
    const form = el('form', { class: 'ms-form', role: 'search' }, [input, el('button', { class: 'btn btn--primary', type: 'submit', text: '搜索' })]);
    const panel = el('div', { class: 'ms-panel' }, [
      el('div', { class: 'ms-heading' }, [el('h2', { id: 'ms-title', text: '搜索记忆' }), close]),
      form, modeBar, filters, status, results,
    ]);
    const dialog = el('dialog', { class: 'ms-dialog', 'aria-labelledby': 'ms-title' }, [panel]);
    close.onclick = () => dialog.close();
    // 仅当按下与抬起都落在遮罩上才关闭：在面板里选中文字后拖到遮罩释放不会误关。
    let downOnBackdrop = false;
    let upOnBackdrop = false;
    dialog.addEventListener('mousedown', event => { downOnBackdrop = event.target === dialog; });
    dialog.addEventListener('mouseup', event => { upOnBackdrop = event.target === dialog; });
    dialog.addEventListener('click', event => {
      if (event.target === dialog && downOnBackdrop && upOnBackdrop) dialog.close();
      downOnBackdrop = upOnBackdrop = false;
    });
    dialog.onclose = () => {
      clearTimeout(timer);
      revision++;
      input.value = '';
      results.classList.remove('ms-stale');
      results.removeAttribute('aria-busy');
      clear(results);
      dialog.remove();
    };
    input.oninput = schedule;
    input.oncompositionend = () => schedule();
    form.onsubmit = event => { event.preventDefault(); schedule(undefined, true); };
    dialog.onkeydown = event => {
      if (event.isComposing || !['ArrowUp','ArrowDown'].includes(event.key)) return;
      if (event.target !== input && !results.contains(event.target)) return;
      const items = [input, ...results.querySelectorAll('button:not(:disabled)')];
      if (items.length < 2) return;
      const index = items.indexOf(event.target);
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      event.preventDefault();
      items[Math.max(0, Math.min(items.length - 1, index + delta))].focus();
    };
    document.body.append(dialog);
    dialog.showModal();
    input.focus();

    function updateFilters() {
      countLabel.textContent = excluded.size ? '已选 ' + excluded.size + ' 项' : '';
      reset.hidden = !excluded.size;
      clear(results);
      schedule();
    }

    function schedule(event, immediate = false) {
      clearTimeout(timer);
      // Invalidate before the debounce so a response for the previous input cannot reappear.
      const token = ++revision;
      const query = input.value.trim();
      if (!query || event?.isComposing) {
        results.classList.remove('ms-stale');
        results.removeAttribute('aria-busy');
        clear(results);
        status.textContent = '搜索当前声明和已采集的事件。';
        return;
      }
      status.textContent = '正在检索…';
      // 保留旧结果直到更新的响应通过修订校验，避免每次按键闪烁；旧结果轻微变暗。
      if (results.querySelector('.ms-hit')) results.classList.add('ms-stale');
      results.setAttribute('aria-busy', 'true');
      if (immediate) run(query, token);
      else timer = setTimeout(() => run(query, token), 180);
    }

    async function run(query, token) {
      const body = { query, mode };
      if (excluded.size) body.exclude = [...excluded];
      const response = await api('POST', '/v2/search', { body });
      if (!dialog.open || token !== revision) return;
      const data = response.data;
      // 响应形状异常时如实报错并可重试，不能停留在假“正在检索”。
      if (!response.ok || !data || !Array.isArray(data.results)) {
        fail(response);
        return;
      }
      const items = [];
      let factCount = 0, evidenceCount = 0;
      for (const item of data.results) {
        if (!item || typeof item !== 'object') continue;
        if (item.handle) {
          evidenceCount++;
          items.push(evidence(item, token));
        } else {
          factCount++;
          const history = el('button', { class: 'btn btn--sm', type: 'button', text: '查看历史' });
          history.onclick = () => { dialog.close(); openHistory(item.fact_id); };
          items.push(el('article', { class: 'ms-hit' }, [
            el('div', { class: 'ms-hit-head' }, [pill('声明', 'info'), el('strong', { text: item.key }), history]),
            el('p', { class: 'ms-value', text: item.evidence || String(item.value) }),
            el('small', { text: scopeText(item.scope) + ' · ' + statusLabel(item.status) + ' · ' + item.value }),
          ]));
        }
      }
      const count = items.length;
      const method = modes[data.retrieval] || '关键词';
      const note = data.fallback_reason ? (fallback[data.fallback_reason] || '语义检索暂时不可用') + '，已使用关键词。' : '';
      const cap = CAP_EVIDENCE[data.retrieval] ?? CAP_EVIDENCE.lexical;
      const capped = factCount >= CAP_STATEMENTS || evidenceCount >= cap;
      const capNote = capped ? '（仅显示前 ' + count + ' 条，请细化关键词）' : '';
      const empty = excluded.size ? '没有匹配结果，试试减少排除条件或更换关键词。' : '没有匹配结果，试试其他关键词。';
      status.textContent = note + (count ? method + '检索命中 ' + count + ' 条' + capNote : empty);
      results.classList.remove('ms-stale');
      results.removeAttribute('aria-busy');
      results.replaceChildren(...items);
    }

    function fail(response) {
      results.classList.remove('ms-stale');
      results.removeAttribute('aria-busy');
      status.textContent = response.ok ? '搜索返回了无法解析的内容，请重试。' : errorHeadline(response);
      const retry = el('button', { type: 'button', class: 'btn btn--sm ms-retry', text: '重试搜索' });
      retry.onclick = () => schedule(undefined, true);
      results.replaceChildren(retry);
    }

    function evidence(item, token) {
      const source = el('pre', { class: 'ms-original', hidden: true });
      const show = el('button', { type: 'button', class: 'btn btn--sm', text: '查看原文', 'aria-expanded': 'false' });
      show.onclick = async () => {
        if (!source.hidden) {
          source.textContent = '';
          source.hidden = true;
          show.setAttribute('aria-expanded', 'false');
          show.textContent = '查看原文';
          return;
        }
        show.disabled = true;
        show.textContent = '读取中…';
        const response = await api('POST', '/v2/evidence/expand', { body: { handle: item.handle } });
        if (!dialog.open || token !== revision) return;
        show.disabled = false;
        if (!response.ok || typeof response.data?.text !== 'string') {
          status.textContent = response.ok ? '原文返回了无法解析的内容。' : errorHeadline(response);
          show.textContent = '重试读取';
          return;
        }
        source.textContent = response.data.text;
        source.hidden = false;
        show.setAttribute('aria-expanded', 'true');
        show.textContent = '收起原文';
      };
      const hits = (item.channels || []).map(channel => channels[channel]).filter(Boolean);
      const meta = [item.at ? libraryTime.format(new Date(item.at)) : '', item.task_id,
        item.version, ...hits, item.event_id].filter(Boolean);
      return el('article', { class: 'ms-hit' }, [
        el('div', { class: 'ms-hit-head' }, [pill('证据', 'muted'), el('strong', { text: actors[item.actor_kind] || item.actor_kind }), show]),
        el('small', { text: meta.join(' · ') }), source,
      ]);
    }
  }
  return { open };
})();
