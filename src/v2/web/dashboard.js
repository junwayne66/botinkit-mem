'use strict';

window.MemoryDashboard = (() => {
  const kinds = {
    statement: '本人声明',
    user_message: '用户消息',
    assistant_message: '助手消息',
    tool_result: '工具回执'
  };
  const jobNames = { pending: '等待处理', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消' };
  const numbers = new Intl.NumberFormat('zh-CN');
  const stamp = new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  });
  const clock = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  let days = 7;
  let summaryRequest = 0;
  let pollTimer = 0;
  // 预览与完整星图各自持有挂载状态：切换视图只刷新进入的一方，另一方保持挂载。
  const atlas = {
    preview: { request: 0, unmount: null },
    full: { request: 0, unmount: null }
  };

  // 服务 CSP 为 default-src 'self'：内联 <style> / style 属性会被拦截，
  // 因此保底高度与行按钮样式一律走 CSSOM 赋值（不受 style-src 限制）。
  const ROW_BUTTON_STYLE = {
    fontFamily: 'inherit',
    background: 'transparent',
    border: '0',
    borderTop: '1px solid var(--divider)',
    width: '100%',
    textAlign: 'start',
    cursor: 'pointer'
  };
  function rowButton(className, title, kids, inheritColor) {
    const button = el('button', { type: 'button', class: className, title }, kids);
    Object.assign(button.style, ROW_BUTTON_STYLE);
    if (inheritColor) button.style.color = 'inherit';
    return button;
  }
  function skeleton(kind, height, margin) {
    const node = el('div', { class: 'placeholder skeleton-' + kind });
    node.style.height = height;
    node.style.marginTop = margin;
    return node;
  }

  function svgElement(tag, attrs) {
    const shape = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name, value] of Object.entries(attrs)) shape.setAttribute(name, value);
    return shape;
  }

  // 柱状图与图例共用同一 --series token，经 getComputedStyle 解析，颜色不再两处硬编码。
  function seriesTone(className, fallbackColor) {
    const probe = el('span', { class: className });
    probe.style.display = 'none';
    document.body.append(probe);
    const tone = getComputedStyle(probe).getPropertyValue('--series').trim();
    probe.remove();
    return tone || fallbackColor;
  }

  function activity(rows) {
    const totals = rows.map((row) => row.statements + row.events);
    const total = totals.reduce((a, b) => a + b, 0);
    const totalLine = el('div', { class: 'chart-main' }, [
      el('strong', { text: numbers.format(total) }),
      el('span', { text: '条写入' })
    ]);
    const statementTone = seriesTone('series-statement', '#61dec0');
    const eventTone = seriesTone('series-assistant_message', '#72a6e1');
    const plot = svgElement('svg', {
      viewBox: '0 0 300 76',
      preserveAspectRatio: 'none',
      class: 'sparkchart',
      role: 'img',
      'aria-label': '每日声明与采集事件写入量，详细数据见下表'
    });
    const peak = Math.max(1, ...totals);
    for (const y of [8, 36, 66])
      plot.append(
        svgElement('path', {
          d: 'M0 ' + y + ' H300',
          stroke: '#2a3a49',
          'stroke-width': '.8',
          'stroke-dasharray': '3 5'
        })
      );
    const step = 300 / rows.length;
    const bar = Math.min(19, step * 0.62);
    rows.forEach((row, i) => {
      const x = step * (i + 0.5) - bar / 2;
      let bottom = 67;
      for (const [key, color] of [
        ['events', eventTone],
        ['statements', statementTone]
      ]) {
        const h = (58 * row[key]) / peak;
        if (!h) continue;
        bottom -= h;
        const rect = svgElement('rect', { x, y: bottom, width: bar, height: h, rx: 1.5, fill: color });
        const tip = svgElement('title', {});
        tip.textContent = row.date + ' · ' + (key === 'events' ? '采集事件 ' : '声明 ') + row[key];
        rect.append(tip);
        plot.append(rect);
      }
    });
    plot.append(svgElement('line', { x1: 0, x2: 300, y1: 67, y2: 67, stroke: '#42645e' }));
    const axis = el('div', { class: 'axis-labels' }, [
      el('span', { text: rows[0].date.slice(5) }),
      el('span', { text: rows[rows.length - 1].date.slice(5) })
    ]);
    const detail = el('details', { class: 'chart-detail' }, [el('summary', { text: '每日明细 · UTC' })]);
    const table = el('table', { 'aria-label': '每日写入量（UTC）' }, [
      el('thead', {}, [
        el(
          'tr',
          {},
          ['日期', '声明', '事件'].map((text) => el('th', { scope: 'col', text }))
        )
      ])
    ]);
    table.append(
      el(
        'tbody',
        {},
        rows.map((row) =>
          el(
            'tr',
            {},
            [row.date, row.statements, row.events].map((n) => el('td', { text: String(n) }))
          )
        )
      )
    );
    detail.append(table);
    const key = el('div', { class: 'plot-key' }, [
      el('span', { class: 'series-statement' }, [el('i', { class: 'legend-mark' }), '声明']),
      el('span', { class: 'series-assistant_message' }, [el('i', { class: 'legend-mark' }), '采集事件'])
    ]);
    $('#activity-chart').replaceChildren(el('div', { class: 'plot-top' }, [totalLine, key]), plot, axis, detail);
  }

  function composition(rows) {
    const host = $('#distribution-chart');
    const total = rows.reduce((sum, row) => sum + row.count, 0);
    const ring = svgElement('svg', { viewBox: '0 0 100 100', 'aria-hidden': true });
    const segment = (attrs) =>
      svgElement('circle', { cx: 50, cy: 50, r: 39, fill: 'none', 'stroke-width': 9, pathLength: 100, ...attrs });
    ring.append(segment({ stroke: '#293644' }));
    let offset = 0;
    const legend = el('ul', { class: 'legend', 'aria-label': '记忆构成' });
    for (const row of rows) {
      const cls = 'series-' + row.kind;
      if (row.count) {
        const share = (100 * row.count) / total;
        ring.append(
          segment({
            class: cls,
            stroke: 'var(--series,#a3b4c7)',
            'stroke-dasharray': share + ' ' + (100 - share),
            'stroke-dashoffset': -offset
          })
        );
        offset += share;
      }
      legend.append(
        el('li', { class: cls }, [
          el('span', { class: 'legend-mark', 'aria-hidden': 'true' }),
          kinds[row.kind] || row.kind,
          el('b', { text: numbers.format(row.count) })
        ])
      );
    }
    const label = el('div', { class: 'donut-value' }, [numbers.format(total), el('small', { text: '条记录' })]);
    host.replaceChildren(
      el('div', { class: 'composition' }, [el('div', { class: 'donut-wrap' }, [ring, label]), legend])
    );
  }

  // 声明行可经 /v2/statements 把 statement_id 解析成 fact_id，直达历史与出处；
  // 尚未形成事实（待归并）时退回记忆库。事件行没有可按 id 打开的视图，保持纯展示。
  async function openStatementHistory(statementId, button) {
    busy(button, true, '正在定位事实…');
    const res = await api('GET', '/v2/statements');
    busy(button, false);
    if (!res.ok) {
      toast(errorHeadline(res), 'err');
      return;
    }
    const items = res.data && Array.isArray(res.data.statements) ? res.data.statements : [];
    const fact = items.find((item) => item && item.statement_id === statementId);
    if (fact && fact.fact_id) openHistory(fact.fact_id);
    else navigate('statements');
  }

  function recent(rows) {
    const list = $('#recent-feed');
    list.replaceChildren();
    if (!rows.length) {
      list.append(el('p', { class: 'chart-empty', text: '首条声明或导入事件会出现在这里。' }));
      return;
    }
    for (const row of rows) {
      const kids = [
        el('span', { class: 'feed-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'feed-title', text: row.label, title: row.label }),
        el('span', { class: 'feed-type', text: kinds[row.kind] }),
        el('time', { datetime: row.at, text: stamp.format(new Date(row.at)), title: row.at })
      ];
      if (row.kind === 'statement' && row.id) {
        const button = rowButton('feed-row series-' + row.kind, '查看这条声明的历史与出处', kids, true);
        button.addEventListener('click', () => openStatementHistory(row.id, button));
        list.append(button);
      } else {
        list.append(el('div', { class: 'feed-row series-' + row.kind }, kids));
      }
    }
  }

  // 首次进入 / 失败清空后展示骨架屏；刷新与轮询保留既有内容，成功时整体替换。
  function prepareOverview() {
    const fill = (host, kids) => {
      if (host && !host.childElementCount) host.replaceChildren(...kids);
    };
    fill($('#activity-chart'), [
      skeleton('line', '14px', '12px'),
      skeleton('chart', '96px', '12px'),
      skeleton('line', '14px', '12px')
    ]);
    fill($('#distribution-chart'), [skeleton('chart', '96px', '12px')]);
    fill($('#recent-feed'), [1, 2, 3].map(() => skeleton('row', '34px', '10px')));
    fill($('#job-summary'), [1, 2].map(() => skeleton('row', '34px', '10px')));
    if (!$('#overview-updated')) {
      const updated = el('p', { id: 'overview-updated', class: 'hint-sm overview-updated', hidden: true });
      updated.style.fontVariantNumeric = 'tabular-nums';
      $('#view-overview .view__head > div')?.append(updated);
    }
  }

  async function summary() {
    const request = ++summaryRequest;
    const host = $('#view-overview');
    const res = await api('GET', '/v2/overview?days=' + days);
    if (request !== summaryRequest || host.hidden) return;
    const data = res.data;
    const valid =
      res.ok && data && data.counts &&
      Array.isArray(data.activity) && Array.isArray(data.distribution) &&
      Array.isArray(data.recent) && Array.isArray(data.jobs);
    const error = $('#overview-error');
    error.hidden = !!valid;
    if (!valid) {
      error.textContent =
        res.status === 401
          ? '当前页面没有所有者会话。在终端运行 bmem web 重新打开。'
          : res.ok
            ? '总览读取失败：服务返回了无法解析的内容。'
            : '总览读取失败，请检查本机服务并刷新。';
      for (const id of ['activity-chart', 'distribution-chart', 'recent-feed', 'job-summary'])
        $('#' + id).replaceChildren();
      for (const node of host.querySelectorAll('.metric strong')) node.textContent = '—';
      return;
    }
    for (const [name, count] of Object.entries(data.counts)) {
      const metric = $('#metric-' + name);
      if (metric) metric.textContent = numbers.format(count);
    }
    activity(data.activity);
    composition(data.distribution);
    recent(data.recent);
    const queue = $('#job-summary');
    queue.replaceChildren(
      ...data.jobs.map((job) => {
        const button = rowButton('queue-row', '查看后台作业', [
          jobNames[job.status] || job.status,
          el('b', { text: numbers.format(job.count) })
        ]);
        button.addEventListener('click', () => navigate('jobs'));
        return button;
      })
    );
    if (!data.jobs.length)
      queue.append(el('p', { class: 'chart-empty', text: '暂无后台作业。导入获准的事件后可在此查看进度。' }));
    const updated = $('#overview-updated');
    if (updated) {
      updated.hidden = false;
      setText(updated, '更新于 ' + clock.format(new Date()));
    }
  }

  // 仅在总览可见且页面可见时轮询总览数据；隐藏即暂停，不触碰星图。
  function startPoll() {
    stopPoll();
    pollTimer = setInterval(() => {
      if (document.hidden || $('#view-overview').hidden) return;
      summary();
    }, 15000);
  }
  function stopPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = 0;
  }

  function leave() {
    summaryRequest++;
    stopPoll();
    // switchView 先更新 hash 再调用 leave：overview↔graph 互切时保留双方挂载，
    // 离开这两个视图才整体 teardown（与 handleRoute 的解析保持一致）。
    const slug = location.hash.replace(/^#\/?/, '').split('?')[0];
    const next = Object.hasOwn(routes, slug) ? routes[slug] : Object.hasOwn(views, slug) ? slug : 'overview';
    if (next === 'overview' || next === 'graph') return;
    for (const slot of [atlas.preview, atlas.full]) {
      slot.request++;
      slot.unmount?.();
      slot.unmount = null;
    }
  }

  async function graph(preview) {
    const slot = preview ? atlas.preview : atlas.full;
    const request = ++slot.request;
    const host = $(preview ? '#atlas-preview' : '#atlas-full');
    // 已有挂载时保留旧图直到新数据到达；首次加载给出中文读取提示，不留空面板。
    if (!slot.unmount) setLoading(host, '正在读取星图…');
    const [res] = await Promise.all([api('GET', '/v2/graph?limit=' + (preview ? 40 : 120)), loadFeature('atlas')]);
    if (request !== slot.request) return;
    slot.unmount?.();
    slot.unmount = null;
    if (!res.ok || !res.data || !Array.isArray(res.data.nodes)) {
      unavailableNote(host, res, '星图');
      return;
    }
    slot.unmount = window.MemoryAtlas.mount(host, res.data, {
      preview,
      onHistory: openHistory
    });
  }

  function enter(view) {
    const preview = view === 'overview';
    if (preview) {
      prepareOverview();
      summary();
      startPoll();
    }
    graph(preview).catch((error) => {
      const slot = preview ? atlas.preview : atlas.full;
      slot.request++;
      slot.unmount?.();
      slot.unmount = null;
      showFeatureError($(preview ? '#atlas-preview' : '#atlas-full'), error, () => enter(view));
    });
  }

  function bind() {
    // 保底高度：星图宿主与图表盒在读取期间不塌陷，成功后内容自然撑开。
    for (const id of ['atlas-preview', 'atlas-full']) $('#' + id).style.minHeight = '320px';
    $('#activity-chart').style.minHeight = '168px';
    $('#distribution-chart').style.minHeight = '112px';
    $('#refresh-overview').onclick = () => {
      enter('overview');
      readConnection();
    };
    $('#refresh-graph').onclick = () => enter('graph');
    for (const button of $$('button[data-days]'))
      button.addEventListener('click', () => {
        days = Number(button.dataset.days);
        for (const b of $$('button[data-days]')) b.setAttribute('aria-pressed', String(b === button));
        summary();
      });
    // 页面重新可见时立即补一次总览，不必等下一个轮询周期。
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && !$('#view-overview').hidden) summary();
    });
  }
  return { enter, leave, bind };
})();
