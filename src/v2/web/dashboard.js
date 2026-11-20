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
  let days = 7;
  let summaryRequest = 0;
  let graphRequest = 0;
  let unmountGraph;

  function svgElement(tag, attrs) {
    const shape = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name, value] of Object.entries(attrs)) shape.setAttribute(name, value);
    return shape;
  }

  function activity(rows) {
    const totals = rows.map((row) => row.statements + row.events);
    const total = totals.reduce((a, b) => a + b, 0);
    const totalLine = el('div', { class: 'chart-main' }, [
      el('strong', { text: numbers.format(total) }),
      el('span', { text: '条写入' })
    ]);
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
        ['events', '#72a6e1'],
        ['statements', '#61dec0']
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

  function recent(rows) {
    const list = $('#recent-feed');
    list.replaceChildren();
    if (!rows.length) {
      list.append(el('p', { class: 'chart-empty', text: '首条声明或导入事件会出现在这里。' }));
      return;
    }
    for (const row of rows) {
      list.append(
        el('div', { class: 'feed-row series-' + row.kind }, [
          el('span', { class: 'feed-dot', 'aria-hidden': 'true' }),
          el('span', { class: 'feed-title', text: row.label, title: row.label }),
          el('span', { class: 'feed-type', text: kinds[row.kind] }),
          el('time', { datetime: row.at, text: stamp.format(new Date(row.at)), title: row.at })
        ])
      );
    }
  }

  async function summary() {
    const request = ++summaryRequest;
    const host = $('#view-overview');
    const res = await api('GET', '/v2/overview?days=' + days);
    if (request !== summaryRequest || host.hidden) return;
    const error = $('#overview-error');
    error.hidden = res.ok;
    if (!res.ok) {
      error.textContent =
        res.status === 401
          ? '当前页面没有所有者会话。在终端运行 bmem web 重新打开。'
          : '总览读取失败，请检查本机服务并刷新。';
      for (const id of ['activity-chart', 'distribution-chart', 'recent-feed', 'job-summary'])
        $('#' + id).replaceChildren();
      for (const node of host.querySelectorAll('.metric strong')) node.textContent = '—';
      return;
    }
    const data = res.data;
    for (const [name, count] of Object.entries(data.counts)) {
      const metric = $('#metric-' + name);
      if (metric) metric.textContent = numbers.format(count);
    }
    activity(data.activity);
    composition(data.distribution);
    recent(data.recent);
    const queue = $('#job-summary');
    queue.replaceChildren(
      ...data.jobs.map((job) =>
        el('div', { class: 'queue-row' }, [
          jobNames[job.status] || job.status,
          el('b', { text: numbers.format(job.count) })
        ])
      )
    );
    if (!data.jobs.length)
      queue.append(el('p', { class: 'chart-empty', text: '暂无后台作业。导入获准的事件后可在此查看进度。' }));
  }

  function leave() {
    graphRequest++;
    summaryRequest++;
    unmountGraph?.();
    unmountGraph = null;
  }

  async function graph(preview) {
    const request = ++graphRequest;
    const host = $(preview ? '#atlas-preview' : '#atlas-full');
    const [res] = await Promise.all([api('GET', '/v2/graph?limit=' + (preview ? 40 : 120)), loadFeature('atlas')]);
    if (request !== graphRequest) return;
    unmountGraph?.();
    unmountGraph = null;
    if (!res.ok) {
      unavailableNote(host, res, '星图');
      return;
    }
    unmountGraph = window.MemoryAtlas.mount(host, res.data, {
      preview,
      onHistory: openHistory
    });
  }

  function enter(view) {
    if (view === 'overview') summary();
    graph(view === 'overview').catch((error) =>
      showFeatureError($(view === 'overview' ? '#atlas-preview' : '#atlas-full'), error)
    );
  }

  function bind() {
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
  }
  return { enter, leave, bind };
})();
