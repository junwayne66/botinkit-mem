'use strict';

window.MemorySearch = (() => {
  const actors = { user_message: '本人对话', assistant_message: '助手回复', tool_result: '工具回执' };
  const modes = { lexical: '关键词', vector: '语义' };
  const fallback = {
    vector_not_ready: '向量检索尚未就绪',
    vector_provider_unavailable: '嵌入模型暂时不可用',
    vector_unavailable: '语义检索暂时不可用',
  };

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

    const status = el('p', { id: 'ms-status', role: 'status', text: '搜索当前声明和已采集的事件。' });
    const results = el('div', { id: 'ms-results', role: 'region', 'aria-label': '搜索结果' });
    const form = el('form', { class: 'ms-form', role: 'search' }, [input, el('button', { class: 'btn btn--primary', type: 'submit', text: '搜索' })]);
    const panel = el('div', { class: 'ms-panel' }, [
      el('div', { class: 'ms-heading' }, [el('h2', { id: 'ms-title', text: '搜索记忆' }), close]),
      form, modeBar, status, results,
    ]);
    const dialog = el('dialog', { class: 'ms-dialog', 'aria-labelledby': 'ms-title' }, [panel]);
    close.onclick = () => dialog.close();
    dialog.onclick = event => { if (event.target === dialog) dialog.close(); };
    dialog.onclose = () => {
      clearTimeout(timer);
      revision++;
      input.value = '';
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

    function schedule(event, immediate = false) {
      clearTimeout(timer);
      // Invalidate before the debounce so a response for the previous input cannot reappear.
      const token = ++revision;
      clear(results);
      const query = input.value.trim();
      if (!query || event?.isComposing) {
        status.textContent = '搜索当前声明和已采集的事件。';
        return;
      }
      status.textContent = '正在检索…';
      if (immediate) run(query, token);
      else timer = setTimeout(() => run(query, token), 180);
    }

    async function run(query, token) {
      const response = await api('POST', '/v2/search', { body: { query, mode } });
      if (!dialog.open || token !== revision) return;
      if (!response.ok) {
        status.textContent = response.data?.message || '无法连接本地服务。请重新搜索。';
        return;
      }
      const data = response.data;
      const count = data.results.length;
      const method = modes[data.retrieval];
      const note = data.fallback_reason ? (fallback[data.fallback_reason] || '语义检索暂时不可用') + '，已使用关键词。' : '';
      status.textContent = note + (count ? method + '检索命中 ' + count + ' 条' : '没有匹配结果，试试其他关键词。');
      for (const item of data.results) {
        if (item.handle) results.append(evidence(item, token));
        else {
          const history = el('button', { class: 'btn btn--sm', type: 'button', text: '查看历史' });
          history.onclick = () => { dialog.close(); openHistory(item.fact_id); };
          results.append(el('article', { class: 'ms-hit' }, [
            el('div', { class: 'ms-hit-head' }, [pill('声明', 'info'), el('strong', { text: item.key }), history]),
            el('p', { class: 'ms-value', text: item.evidence || String(item.value) }),
            el('small', { text: scopeText(item.scope) + ' · ' + statusLabel(item.status) + ' · ' + item.value }),
          ]));
        }
      }
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
        if (!response.ok) {
          status.textContent = response.data?.message || '原文暂时不可读。';
          show.textContent = '重试读取';
          return;
        }
        source.textContent = response.data.text;
        source.hidden = false;
        show.setAttribute('aria-expanded', 'true');
        show.textContent = '收起原文';
      };
      const meta = [item.at ? libraryTime.format(new Date(item.at)) : '', item.task_id, item.event_id].filter(Boolean);
      return el('article', { class: 'ms-hit' }, [
        el('div', { class: 'ms-hit-head' }, [pill('证据', 'muted'), el('strong', { text: actors[item.actor_kind] || item.actor_kind }), show]),
        el('small', { text: meta.join(' · ') }), source,
      ]);
    }
  }
  return { open };
})();
