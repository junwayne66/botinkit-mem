// The base canvas is redrawn only when the layout or selection changes.
// A separate transparent layer carries low-frequency motion along actual edges.
(() => {
  'use strict';
  const palette = ['#4ce6e1', '#aa9aff', '#ffc781', '#83d5fb', '#f697c7', '#bcdf94'];
  const kinds = { statement: '声明', event: '事件', community: '社区', topic: '主题', scope: '范围', source: '来源' };
  const relation = { topic: '归属主题', scope: '适用范围', source: '来自来源' };
  const symbols = {
    plus: 'M12 5v14M5 12h14',
    minus: 'M5 12h14',
    target: 'M4 9V4h5m6 0h5v5m0 6v5h-5m-6 0H4v-5m5-3h6m-3-3v6',
    pause: 'M8 5v14m8-14v14',
    play: 'M8 5l11 7-11 7z',
    arrow: 'M6 18 18 6M7 6h11v11',
    close: 'm6 6 12 12M18 6 6 18'
  };
  const golden = Math.PI * (3 - Math.sqrt(5));

  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function icon(button, name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', symbols[name]);
    svg.append(path);
    button.replaceChildren(svg);
  }
  function control(label, symbol, action) {
    const button = element('button', 'atlas-control');
    button.type = 'button';
    button.title = label;
    button.setAttribute('aria-label', label);
    icon(button, symbol);
    button.addEventListener('click', action);
    return button;
  }
  function selectField(label, initial) {
    const wrap = element('label', 'atlas-field');
    wrap.append(element('span', null, label));
    const input = element('select');
    input.setAttribute('aria-label', label);
    input.add(new Option(initial, ''));
    wrap.append(input);
    return { wrap, input };
  }
  function hash(text) {
    let n = 2166136261;
    for (const c of text) n = Math.imul(n ^ c.charCodeAt(0), 16777619);
    return (n >>> 0) / 4294967296;
  }
  function exampleGraph() {
    const groups = [
      ['工作偏好', ['表达习惯', '文档风格', '任务流程', '交付标准'], 20],
      ['项目决策', ['存储设计', '接口约定', '数据模型', '设计评审'], 16],
      ['工具经验', ['命令行', '配置检查', '测试记录', '发布记录'], 12]
    ];
    const nodes = [],
      edges = [],
      communities = [];
    groups.forEach(([label, subjects, count], index) => {
      const id = 'demo-group-' + index;
      communities.push({ id, label, size: count, kind: 'topic' });
      nodes.push({ id, label, community: id, kind: 'community', detail: label + '的示例主题社区' });
      for (let i = 0; i < count; i++) {
        const node = {
          id: id + '-' + i,
          label: subjects[i % subjects.length] + ' · ' + (1 + Math.floor(i / subjects.length)),
          community: id,
          kind: i % 4 === 1 ? 'event' : 'statement',
          detail: '用于探索星图交互的示例记录'
        };
        nodes.push(node);
        edges.push({ source: node.id, target: id, kind: 'topic' });
        if (i % 7 === 0)
          edges.push({ source: node.id, target: 'demo-group-' + ((index + 1) % groups.length), kind: 'topic' });
      }
    });
    return { nodes, edges, communities };
  }

  // Place community hubs first. Golden-angle packing gives every record a stable
  // position without a force simulation or a quadratic pairwise collision pass.
  function layout(graph, filter, width, height) {
    const included = filter ? new Set([filter]) : null;
    if (included) for (const edge of graph.edges) if (edge.target === filter) included.add(edge.source);
    const communities = graph.communities.map((c, i) => ({ ...c, tone: palette[i % palette.length] }));
    const groups = communities.filter((c) => !filter || c.id === filter);
    const visible = graph.nodes.filter((n) => !included || included.has(n.id));
    const members = new Map(groups.map((g) => [g.id, []]));
    for (const n of visible) {
      if (n.kind !== 'community') members.get(filter || n.community)?.push(n);
    }
    groups.sort((a, b) => members.get(b.id).length - members.get(a.id).length || a.id.localeCompare(b.id));
    const hub = new Map(visible.filter((n) => n.kind === 'community').map((n) => [n.id, n]));
    const count = groups.length,
      positions = new Map();
    const columns = Math.max(1, Math.min(count, Math.round(Math.sqrt((count * width) / height))));
    const rows = Math.ceil(count / columns);
    const presets = {
      1: [[0.5, 0.5]],
      2: [
        [0.32, 0.52],
        [0.72, 0.43]
      ],
      3: [
        [0.29, 0.39],
        [0.73, 0.36],
        [0.58, 0.74]
      ],
      4: [
        [0.28, 0.34],
        [0.72, 0.35],
        [0.59, 0.75],
        [0.19, 0.77]
      ]
    };
    for (let i = 0; i < count; i++) {
      const g = groups[i],
        list = members.get(g.id);
      const row = Math.floor(i / columns),
        remaining = Math.min(columns, count - row * columns);
      const [nx, ny] = presets[count]?.[i] ?? [
        ((i % columns) + 0.5 + (hash(g.id + 'x') - 0.5) * 0.24) / remaining,
        (row + 0.5 + (hash(g.id + 'y') - 0.5) * 0.32) / rows
      ];
      const x = width * nx,
        y = height * ny;
      const rx = count === 1 ? width * 0.33 : count <= 4 ? width * (i === 0 ? 0.215 : 0.177) : (width / columns) * 0.39;
      const ry = count === 1 ? height * 0.32 : count <= 4 ? height * (i === 0 ? 0.248 : 0.206) : (height / rows) * 0.31;
      g.x = x;
      g.y = y;
      g.rx = rx;
      g.ry = ry;
      const center = hub.get(g.id);
      if (center) positions.set(g.id, { ...center, x, y, tone: g.tone });
      const rotation = hash(g.id) * 2 * Math.PI;
      list.forEach((node, j) => {
        const angle = j * golden + rotation + 0.28 * hash(node.id);
        const radius = 0.22 + 0.78 * Math.sqrt((j + 0.6) / list.length);
        positions.set(node.id, {
          ...node,
          x: x + Math.cos(angle) * rx * radius,
          y: y + Math.sin(angle) * ry * radius,
          tone: g.tone
        });
      });
    }
    const edges = [];
    for (const e of graph.edges) {
      const a = positions.get(e.source),
        b = positions.get(e.target);
      if (a && b) edges.push({ ...e, a, b, cross: a.community !== b.community, phase: hash(e.source + e.target) });
    }
    return { groups, nodes: [...positions.values()], positions, edges };
  }

  function mount(container, graph, options = {}) {
    const preview = !!options.preview;
    const empty = graph.nodes.length === 0;
    let sample = empty;
    let data = sample ? exampleGraph() : graph;
    let filter = '',
      selected = '',
      hovered = '';
    let zoom = 1,
      panX = 0,
      panY = 0;
    let labelHits = [];
    let width = 0,
      height = 0,
      model,
      timer = 0,
      raf = 0;
    let onScreen = false,
      destroyed = false,
      elapsed = 0,
      lastFrame = 0;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    let paused = motion.matches;
    const root = element('div', 'memory-atlas ' + (preview ? 'atlas-preview' : 'atlas-full'));
    const stage = element('div', 'atlas-stage');
    const base = element('canvas', 'atlas-base'),
      fx = element('canvas', 'atlas-fx');
    base.setAttribute('role', 'img');
    base.setAttribute('aria-label', '主题社区关系图，可使用下方节点选择器逐条浏览');
    base.tabIndex = 0;
    fx.setAttribute('aria-hidden', 'true');
    const ctx = base.getContext('2d'),
      glow = fx.getContext('2d');
    const toolbar = element('div', 'atlas-tools');
    const zoomIn = control('放大星图', 'plus', () => scale(zoom * 1.25));
    const zoomOut = control('缩小星图', 'minus', () => scale(zoom / 1.25));
    const fit = control('复位视图', 'target', resetCamera);
    const pause = control('暂停动画', 'pause', () => {
      paused = !paused;
      syncPause();
      animate();
    });
    toolbar.append(zoomIn, zoomOut, fit, pause);
    if (preview && options.onOpen) toolbar.append(control('打开完整星图', 'arrow', options.onOpen));
    const note = element('div', 'atlas-note');
    const stamp = element('span', 'atlas-stamp');
    const cut = element('span', 'atlas-cut');
    const link = element('button', 'atlas-link');
    link.type = 'button';
    const notice = element('div', 'atlas-empty', '还没有可绘制的记忆');
    note.append(stamp, cut);
    if (empty) note.append(link);
    const meter = element('span', 'atlas-meter', '100%');
    meter.setAttribute('aria-hidden', 'true');
    const tip = element('div', 'atlas-tooltip');
    tip.hidden = true;
    stage.append(base, fx, note, notice, tip, meter, toolbar);
    const legend = element('div', 'atlas-legend');
    const controls = element('div', 'atlas-controls');
    const community = selectField('筛选社区', '全部社区'),
      record = selectField('查看节点', '选择节点…');
    controls.append(community.wrap, record.wrap);
    const explorer = element('details', 'atlas-explorer');
    const summary = element('summary', null, '浏览节点与连接');
    explorer.append(summary, controls);
    if (!preview) explorer.open = true;
    const detail = element('div', 'atlas-inspector');
    detail.setAttribute('aria-live', 'polite');
    detail.hidden = true;
    root.append(stage, legend, explorer, detail);
    container.replaceChildren(root);
    const signal = new AbortController(),
      listen = { signal: signal.signal };
    function syncPause() {
      const label = paused ? '播放动画' : '暂停动画';
      pause.title = label;
      pause.setAttribute('aria-label', label);
      pause.setAttribute('aria-pressed', String(paused));
      icon(pause, paused ? 'play' : 'pause');
      pause.disabled = motion.matches || !data.nodes.length;
      if (motion.matches) pause.title = '已随系统减少动态效果';
    }
    function setCamera() {
      zoomIn.disabled = zoom >= 3.5 || !data.nodes.length;
      zoomOut.disabled = zoom <= 0.6 || !data.nodes.length;
      fit.disabled = !data.nodes.length;
      meter.textContent = Math.round(zoom * 100) + '%';
      draw();
    }
    function resetCamera() {
      zoom = 1;
      panX = panY = 0;
      setCamera();
    }
    function scale(next, x = width / 2, y = height / 2) {
      const target = Math.min(3.5, Math.max(0.6, next)),
        ratio = target / zoom;
      panX = (x - width / 2) * (1 - ratio) + panX * ratio;
      panY = (y - height / 2) * (1 - ratio) + panY * ratio;
      zoom = target;
      setCamera();
    }
    function rebuildControls() {
      community.input.replaceChildren(new Option('全部社区', ''));
      legend.replaceChildren();
      data.communities.forEach((c, i) => {
        community.input.add(new Option(c.label, c.id));
        if (i >= 4 && c.id !== filter) return;
        const item = element('button', 'atlas-key atlas-color-' + (i % palette.length), c.label);
        item.type = 'button';
        item.setAttribute('aria-pressed', String(c.id === filter));
        item.title = '筛选社区 · ' + c.label;
        item.addEventListener('click', () => changeFilter(filter === c.id ? '' : c.id));
        legend.append(item);
      });
      if (data.communities.length > 4) {
        const more = element('button', 'atlas-more', '全部 ' + data.communities.length + ' 个社区');
        more.type = 'button';
        more.onclick = () => {
          explorer.open = true;
          community.input.focus();
        };
        legend.append(more);
      }
      community.input.value = filter;
      const capped = !sample && graph.truncated;
      stamp.textContent = sample ? '示例星图' : empty ? '空记忆库' : capped ? '星图切片' : '本机星图';
      stamp.classList.toggle('atlas-stamp-sample', sample);
      cut.hidden = !capped;
      if (capped) cut.textContent = graph.shown_records + ' / ' + graph.total_records + ' 条记录';
      link.textContent = sample ? '隐藏示例' : '查看示例';
      notice.hidden = !empty || sample;
      toolbar.hidden = meter.hidden = explorer.hidden = !data.nodes.length;
      legend.hidden = !data.communities.length;
      syncPause();
    }
    function populateNodes() {
      record.input.replaceChildren(new Option('选择节点…', ''));
      for (const n of model.nodes) record.input.add(new Option((kinds[n.kind] || '节点') + ' · ' + n.label, n.id));
      record.input.value = selected;
    }
    function changeFilter(value) {
      filter = value;
      selected = hovered = '';
      tip.hidden = detail.hidden = true;
      rebuildControls();
      rebuildLayout();
      resetCamera();
    }
    function rebuildLayout() {
      if (!width || !height) return;
      model = layout(data, filter, width, height);
      populateNodes();
      draw();
    }
    function selectNode(id) {
      selected = id;
      hovered = '';
      tip.hidden = true;
      const n = model.positions.get(id);
      record.input.value = id;
      detail.replaceChildren();
      detail.hidden = !n;
      if (n) {
        const main = element('div', 'atlas-inspector-body');
        const title = element('strong', null, n.label);
        main.append(element('span', 'atlas-kind', (sample ? '示例 · ' : '') + (kinds[n.kind] || '节点')), title);
        if (n.detail && n.detail !== n.label) main.append(element('p', null, n.detail));
        const neighbors = model.edges.filter((e) => e.source === id || e.target === id);
        if (neighbors.length) {
          const adjacent = selectField('相连节点（' + neighbors.length + '）', '沿关系查看…');
          neighbors.forEach((e) => {
            const other = e.source === id ? e.b : e.a;
            adjacent.input.add(new Option((relation[e.kind] || '关联') + ' · ' + other.label, other.id));
          });
          adjacent.input.addEventListener('change', () => {
            if (adjacent.input.value) {
              selectNode(adjacent.input.value);
              detail.querySelector('select')?.focus();
            }
          });
          main.append(adjacent.wrap);
        }
        if (n.fact_id && options.onHistory) {
          const history = element('button', 'atlas-history', '查看声明历史');
          history.type = 'button';
          history.addEventListener('click', () => options.onHistory(n.fact_id));
          main.append(history);
        }
        const close = control('关闭节点详情', 'close', () => {
          selectNode('');
          base.focus();
        });
        detail.append(main, close);
      }
      draw();
    }
    function resize() {
      const rect = stage.getBoundingClientRect();
      const w = Math.round(rect.width),
        h = Math.round(rect.height);
      if (w <= 0 || h <= 0 || (w === width && h === height)) return;
      width = w;
      height = h;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      for (const [canvas, context] of [
        [base, ctx],
        [fx, glow]
      ]) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        context.setTransform(canvas.width / w, 0, 0, canvas.height / h, 0, 0);
      }
      rebuildLayout();
    }
    function project(c) {
      c.translate(width / 2 + panX, height / 2 + panY);
      c.scale(zoom, zoom);
      c.translate(-width / 2, -height / 2);
    }
    function point(n) {
      return { x: (n.x - width / 2) * zoom + width / 2 + panX, y: (n.y - height / 2) * zoom + height / 2 + panY };
    }
    function circle(c, x, y, r, fill, stroke) {
      c.beginPath();
      c.arc(x, y, r, 0, Math.PI * 2);
      if (fill) {
        c.fillStyle = fill;
        c.fill();
      }
      if (stroke) {
        c.strokeStyle = stroke;
        c.stroke();
      }
    }
    function draw() {
      if (!model) return;
      ctx.clearRect(0, 0, width, height);
      ctx.save();
      project(ctx);
      const active = hovered || selected;
      const lit = active ? new Set([active]) : null;
      if (lit)
        for (const e of model.edges)
          if (e.source === active || e.target === active) {
            lit.add(e.source);
            lit.add(e.target);
          }
      for (const g of model.groups) {
        const radius = Math.max(g.rx * 1.3, g.ry * 1.45, 12);
        const aura = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, radius);
        aura.addColorStop(0, g.tone + '28');
        aura.addColorStop(0.6, g.tone + '0b');
        aura.addColorStop(1, g.tone + '00');
        ctx.fillStyle = aura;
        ctx.fillRect(g.x - radius, g.y - radius, radius * 2, radius * 2);
        drawContour(g);
      }
      for (const e of model.edges) {
        const hot = e.source === active || e.target === active;
        ctx.strokeStyle = hot ? e.a.tone + 'e6' : active ? '#66809824' : e.a.tone + (e.cross ? '65' : '58');
        ctx.lineWidth = (hot ? 1.65 : e.cross ? 1 : 0.85) / Math.sqrt(zoom);
        ctx.setLineDash(e.cross ? [4, 4] : []);
        ctx.beginPath();
        ctx.moveTo(e.a.x, e.a.y);
        ctx.lineTo(e.b.x, e.b.y);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      for (const n of model.nodes) {
        if (n.kind === 'community') continue;
        const isLit = !lit || lit.has(n.id),
          hot = n.id === active;
        const radius = (hot ? 4.8 : 2.5 + hash(n.id) * 1.3) / Math.sqrt(zoom);
        if (isLit) circle(ctx, n.x, n.y, radius + 3, n.tone + '1d');
        if (n.kind === 'event') {
          ctx.save();
          ctx.translate(n.x, n.y);
          ctx.rotate(Math.PI / 4);
          ctx.fillStyle = isLit ? n.tone : '#536775';
          ctx.fillRect(-radius * 0.81, -radius * 0.81, radius * 1.62, radius * 1.62);
          ctx.restore();
        } else circle(ctx, n.x, n.y, radius, isLit ? n.tone : '#536775');
        if (hot) circle(ctx, n.x, n.y, radius + 5, null, n.tone);
      }
      for (const g of model.groups) {
        const r = g.id === active ? 6.5 : 5;
        ctx.lineWidth = 1 / zoom;
        circle(ctx, g.x, g.y, r + 5, g.tone + '18', g.tone + '91');
        circle(ctx, g.x, g.y, r, g.tone + 'cc');
        circle(ctx, g.x, g.y, 2, '#ecffff');
      }
      ctx.restore();
      drawLabels();
      drawMotion();
    }
    function drawContour(g) {
      ctx.lineWidth = 0.8 / zoom;
      for (let ring = 0; ring < 2; ring++) {
        ctx.beginPath();
        for (let k = 0; k <= 64; k++) {
          const t = (k * Math.PI) / 32,
            wave = 1 + 0.09 * Math.sin(3 * t + 0.8) + 0.06 * Math.cos(2 * t + hash(g.id) * 5);
          const radius = (0.98 + ring * 0.13) * wave;
          const x = g.x + Math.cos(t) * g.rx * radius,
            y = g.y + Math.sin(t) * g.ry * radius;
          if (k) ctx.lineTo(x, y);
          else ctx.moveTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = g.tone + (ring ? '1b' : '31');
        ctx.stroke();
      }
    }
    function drawLabels() {
      const placed = [];
      labelHits = placed;
      const font = '600 12px system-ui, "PingFang SC", sans-serif';
      ctx.font = font;
      ctx.textBaseline = 'middle';
      for (const g of model.groups.slice(0, width < 400 ? 4 : preview ? 6 : 12)) {
        const p = point(g),
          label = Array.from(g.label);
        const text = label.length > 12 ? label.slice(0, 11).join('') + '…' : g.label;
        const w = Math.ceil(ctx.measureText(text).width) + 16,
          h = 23;
        if (p.x < 0 || p.x > width || p.y < 0 || p.y > height) continue;
        const choices = [
          [13, -h - 2],
          [-w - 14, -h - 2],
          [13, 6],
          [-w - 14, 6]
        ];
        for (const [dx, dy] of choices) {
          const x = p.x + dx,
            y = p.y + dy;
          if (
            x < 4 ||
            x + w > width - 4 ||
            y < 5 ||
            y + h > height - 4 ||
            placed.some((r) => x < r[0] + r[2] + 3 && x + w > r[0] - 3 && y < r[1] + h + 2 && y + h > r[1] - 2)
          )
            continue;
          ctx.fillStyle = '#0b1729ed';
          ctx.strokeStyle = g.tone + '6b';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect(x + 0.5, y + 0.5, w, h, 4);
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = '#dceaf4';
          ctx.fillText(text, x + 8, y + h / 2 + 0.5);
          placed.push([x, y, w, g.id]);
          break;
        }
      }
    }
    function drawMotion() {
      glow.clearRect(0, 0, width, height);
      if (!model || paused || motion.matches || !onScreen || document.hidden || !model.edges.length) return;
      glow.save();
      project(glow);
      // Keep community motion and travelling edges bounded independently of the slice size.
      const orbitCount = Math.min(model.groups.length, 6);
      for (let i = 0; i < orbitCount; i++) {
        const group = model.groups[i];
        const angle = elapsed * 0.00019 + i * golden;
        glow.beginPath();
        glow.ellipse(group.x, group.y, group.rx * 1.05, group.ry * 1.04, 0, angle, angle + 0.51);
        glow.strokeStyle = group.tone + '55';
        glow.lineWidth = 1 / zoom;
        glow.stroke();
      }
      const stride = Math.max(1, Math.ceil(model.edges.length / 20));
      for (let i = 0; i < model.edges.length; i += stride) {
        const e = model.edges[i],
          t = (e.phase + elapsed * (0.00014 + e.phase * 0.00007)) % 1;
        const x = e.a.x + (e.b.x - e.a.x) * t,
          y = e.a.y + (e.b.y - e.a.y) * t;
        const tail = Math.max(0, t - 0.075);
        glow.beginPath();
        glow.moveTo(e.a.x + (e.b.x - e.a.x) * tail, e.a.y + (e.b.y - e.a.y) * tail);
        glow.lineTo(x, y);
        glow.lineWidth = 1.3 / zoom;
        glow.strokeStyle = e.a.tone + '90';
        glow.stroke();
        circle(glow, x, y, 3 / zoom, e.a.tone + '35');
        circle(glow, x, y, 1.25 / zoom, '#deffff');
        if (t < 0.13) {
          glow.globalAlpha = 1 - t / 0.13;
          circle(glow, e.a.x, e.a.y, (4 + t * 28) / zoom, null, e.a.tone);
          glow.globalAlpha = 1;
        }
      }
      glow.restore();
    }
    function animate() {
      clearTimeout(timer);
      cancelAnimationFrame(raf);
      lastFrame = 0;
      if (destroyed || paused || motion.matches || !onScreen || document.hidden || !data.edges.length) {
        glow.clearRect(0, 0, width, height);
        return;
      }
      function frame(now) {
        if (lastFrame) elapsed += Math.min(200, now - lastFrame);
        lastFrame = now;
        drawMotion();
        timer = setTimeout(() => {
          raf = requestAnimationFrame(frame);
        }, 40);
      }
      raf = requestAnimationFrame(frame);
    }
    function nearest(x, y) {
      if (!model) return null;
      for (const [left, top, w, id] of labelHits) {
        if (x >= left && x <= left + w && y >= top && y <= top + 23) return model.positions.get(id);
      }
      let found = null,
        distance = 15 * 15;
      for (const n of model.nodes) {
        const p = point(n),
          d = (p.x - x) ** 2 + (p.y - y) ** 2;
        if (d < distance) {
          found = n;
          distance = d;
        }
      }
      return found;
    }
    let drag;
    function pointerPos(event) {
      const rect = base.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }
    base.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || !data.nodes.length || !event.isPrimary) return;
      const p = pointerPos(event);
      drag = { ...p, initialX: panX, initialY: panY, moved: false };
      base.setPointerCapture(event.pointerId);
      tip.hidden = true;
    });
    base.addEventListener('pointermove', (event) => {
      const p = pointerPos(event);
      if (drag) {
        const dx = p.x - drag.x,
          dy = p.y - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 5) drag.moved = true;
        if (drag.moved) {
          panX = drag.initialX + dx;
          panY = drag.initialY + dy;
          hovered = '';
          base.classList.add('atlas-panning');
          draw();
        }
        return;
      }
      if (event.pointerType === 'touch') return;
      const n = nearest(p.x, p.y);
      if ((n?.id || '') !== hovered) {
        hovered = n?.id || '';
        draw();
        tip.hidden = !n;
        base.classList.toggle('atlas-over-node', !!n);
        if (n) tip.textContent = (kinds[n.kind] || '节点') + ' · ' + n.label;
      }
      if (n) {
        tip.style.left = Math.max(4, Math.min(width - tip.offsetWidth - 4, p.x + 12)) + 'px';
        tip.style.top = Math.max(4, p.y - 34) + 'px';
      }
    });
    base.addEventListener('pointerup', (event) => {
      if (!drag) return;
      if (!drag.moved) {
        const p = pointerPos(event);
        selectNode(nearest(p.x, p.y)?.id || '');
      }
      drag = null;
      base.classList.remove('atlas-panning');
    });
    base.addEventListener('pointercancel', () => {
      drag = null;
      base.classList.remove('atlas-panning');
    });
    base.addEventListener('pointerleave', () => {
      if (!hovered) return;
      hovered = '';
      tip.hidden = true;
      base.classList.remove('atlas-over-node');
      draw();
    });
    base.addEventListener(
      'wheel',
      (event) => {
        if (!event.ctrlKey || !data.nodes.length) return;
        event.preventDefault();
        const p = pointerPos(event);
        scale(zoom * Math.exp(-event.deltaY * 0.01), p.x, p.y);
      },
      { passive: false }
    );
    base.addEventListener('keydown', (event) => {
      if (!data.nodes.length) return;
      if (['+', '=', '-', '0', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Escape'].includes(event.key))
        event.preventDefault();
      else return;
      if (event.key === '+' || event.key === '=') scale(zoom * 1.25);
      else if (event.key === '-') scale(zoom / 1.25);
      else if (event.key === '0') resetCamera();
      else if (event.key === 'Escape') selectNode('');
      else {
        if (event.key === 'ArrowLeft') panX += 32;
        if (event.key === 'ArrowRight') panX -= 32;
        if (event.key === 'ArrowUp') panY += 32;
        if (event.key === 'ArrowDown') panY -= 32;
        draw();
      }
    });
    community.input.addEventListener('change', () => changeFilter(community.input.value));
    record.input.addEventListener('change', () => selectNode(record.input.value));
    link.onclick = () => {
      sample = !sample;
      data = sample ? exampleGraph() : graph;
      changeFilter('');
      animate();
    };
    motion.addEventListener(
      'change',
      () => {
        paused = motion.matches;
        syncPause();
        animate();
      },
      listen
    );
    document.addEventListener('visibilitychange', animate, listen);
    const sizeObserver = new ResizeObserver(resize);
    sizeObserver.observe(stage);
    const observer = new IntersectionObserver((entries) => {
      onScreen = entries[0].isIntersecting;
      animate();
    });
    observer.observe(stage);
    rebuildControls();
    resize();
    setCamera();
    return () => {
      destroyed = true;
      animate();
      observer.disconnect();
      sizeObserver.disconnect();
      signal.abort();
      root.remove();
    };
  }
  window.MemoryAtlas = { mount };
})();
