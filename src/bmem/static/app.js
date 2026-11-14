/* bmem web v0.1 — 真实管理台脚本。
 * 无框架 / 无构建 / 无 CDN：数据全部来自本机 bmemd HTTP API（契约 specs/001-personal-memory/contracts/interfaces.md）。
 * 每个数据区四态：loading / empty / error / full。琥珀 = 用户声明，蓝 = Agent 派生，二者永不互换。
 */
(function () {
  "use strict";

  /* ================= 基础工具 ================= */
  var SVG_NS = "http://www.w3.org/2000/svg";
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function svg(tag, attrs) {
    var n = document.createElementNS(SVG_NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function chip(text, cls) { return h("span", "chip" + (cls ? " " + cls : ""), text); }
  function mkButton(label, fn, cls) {
    var b = h("button", cls || "btn", label);
    b.type = "button";
    if (fn) b.addEventListener("click", fn);
    return b;
  }
  function icon(useId) {
    var s = svg("svg", { class: "icon", "aria-hidden": "true" });
    var u = svg("use", { href: "#" + useId });
    s.appendChild(u);
    return s;
  }
  function fmtNum(n) {
    var v = Number(n);
    if (!isFinite(v)) return "—";
    return v.toLocaleString();
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var p = function (x) { return String(x).padStart(2, "0"); };
    var hm = p(d.getHours()) + ":" + p(d.getMinutes());
    if (d.getFullYear() === new Date().getFullYear()) return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + hm;
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + hm;
  }
  function timeEl(iso, cls) {
    var t = h("time", (cls || "") , fmtTime(iso));
    if (iso) t.setAttribute("datetime", String(iso));
    t.classList.add("measured");
    return t;
  }
  function truncate(s, n) {
    s = String(s == null ? "" : s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function asObj(v) {
    if (v && typeof v === "object") return v;
    if (typeof v === "string") {
      var s = v.trim();
      if (s && (s.charAt(0) === "{" || s.charAt(0) === "[")) {
        try { return JSON.parse(s); } catch (e) { return null; }
      }
    }
    return null;
  }
  function condText(c) {
    var o = asObj(c);
    if (!o) { return (c && String(c).trim() && String(c) !== "{}") ? String(c) : "无附加条件"; }
    if (Array.isArray(o)) return o.length ? o.join(" · ") : "无附加条件";
    var keys = Object.keys(o);
    if (!keys.length) return "无附加条件";
    return keys.map(function (k) { return k + "=" + (typeof o[k] === "object" ? JSON.stringify(o[k]) : o[k]); }).join(" · ");
  }

  /* ================= API 客户端 ================= */
  var EP = {
    health: "/api/health",
    stats: "/api/stats",
    memories: "/api/memories",
    memory: function (id) { return "/api/memories/" + encodeURIComponent(id); },
    forget: function (id) { return "/api/memories/" + encodeURIComponent(id) + "/forget"; },
    declare: "/api/declarations",
    search: "/api/search",
    stats_arb: "/api/arbitration",
    injections: "/api/injections",
    imports: "/api/imports",
    jobs: "/api/jobs",
    graph: "/api/graph",
    event: function (id) { return "/api/events/" + encodeURIComponent(id); },
    sources: "/api/sources",
    semantic: "/api/settings/semantic",
    runtime: "/api/runtime",
    embedInstall: "/api/runtime/embedding/install",
    backup: "/api/backup",
    restore: "/api/restore",
    workerRun: "/api/worker/run",
    rebuild: "/api/indexes/rebuild",
    connector: function (brand) { return "/api/connectors/" + brand; },
    connectorConnect: function (brand) { return "/api/connectors/" + brand + "/connect"; },
    connectorConfig: function (brand) { return "/api/connectors/" + brand + "/config"; },
    connectorSync: function (brand) { return "/api/connectors/" + brand + "/sync"; },
    connectorQr: function (brand) { return "/api/connectors/" + brand + "/qrcode"; }
  };

  function ApiError(status, message) {
    var e = new Error(message || ("HTTP " + status));
    e.status = status;
    return e;
  }

  function apiFetch(method, path, opts) {
    opts = opts || {};
    var url = path;
    if (opts.query) {
      var qs = new URLSearchParams();
      Object.keys(opts.query).forEach(function (k) {
        var v = opts.query[k];
        if (v !== null && v !== undefined && v !== "" && v !== "all") qs.set(k, String(v));
      });
      var s = qs.toString();
      if (s) url += (url.indexOf("?") >= 0 ? "&" : "?") + s;
    }
    var init = { method: method, headers: { Accept: "application/json" } };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(url, init).then(function (res) {
      var ct = (res.headers.get("content-type") || "");
      var parse = ct.indexOf("json") >= 0 ? res.json().catch(function () { return null; }) : res.text().catch(function () { return null; });
      return parse.then(function (data) {
        if (!res.ok) {
          var msg = "";
          if (data && typeof data === "object") {
            var d = data.detail !== undefined ? data.detail : data.message;
            msg = typeof d === "string" ? d : (d ? JSON.stringify(d) : "");
          } else if (typeof data === "string") {
            msg = truncate(data.replace(/<[^>]*>/g, " "), 160);
          }
          throw ApiError(res.status, msg || ("HTTP " + res.status));
        }
        return data;
      });
    }, function (err) {
      if (err && err.name === "AbortError") throw err;
      throw ApiError(0, "无法连接本机 bmemd（" + ((err && err.message) || "网络错误") + "）");
    });
  }

  function itemsOf(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.items)) return data.items;
    if (data && Array.isArray(data.results)) return data.results;
    return [];
  }

  /* ================= 记忆字段绑定（后端契约字段，见 interfaces.md） ================= */
  function mid(m) { return String((m && (m.memory_id || m.id)) || ""); }
  /* 记忆 DTO 用 body；星图节点用 memory_id + label（正文截断）。 */
  function mbody(m) { return String((m && (m.body !== undefined ? m.body : m.label)) || ""); }
  function mkind(m) { return String((m && m.kind) || "fact"); }
  function mstatus(m) { return String((m && m.status) || ""); }
  function mrev(m) {
    var n = Number(m && m.revision_no);
    return isFinite(n) && n > 0 ? n : 1;
  }
  function mtime(m) { return (m && (m.updated_at || m.created_at || m.occurred_at)) || ""; }
  /* trust_tier 恒为字符串 T1..T4（契约：UI 直接渲染，不做数字转换）。 */
  var TIER_RE = /^T[1-4]$/;
  function tierOf(x) {
    var t = String((x && x.trust_tier) || "").trim().toUpperCase();
    return TIER_RE.test(t) ? t : "";
  }
  var TIER_ZH = { T1: "用户声明", T2: "工具验证", T3: "Agent 派生", T4: "外部内容" };
  function isUserTier(t) { return t === "T1" || t === "T2"; }
  /* 琥珀 = 用户侧（T1 声明 / T2 验证）；蓝 = Agent 派生（T3/T4）。 */
  function misUser(m) {
    if (!m) return false;
    var st = String(m.source_type || "");
    if (st === "user_declaration" || st === "verification") return true;
    return isUserTier(tierOf(m));
  }
  function tierChipCls(t) { return isUserTier(t) ? "chip--amber" : "chip--muted"; }
  function sensOf(m) { return String((m && m.sensitivity) || "normal"); }
  var STATUS_CHIP = { active: "chip--ok", proposed: "chip--amber", disputed: "chip--bad", superseded: "chip--muted", retracted: "chip--muted", deleted: "chip--muted", quarantined: "chip--bad" };
  var JOB_STATUS_CHIP = { pending: "chip--muted", running: "chip--cool", done: "chip--ok", failed: "chip--bad" };
  function statusChip(s) { return chip(s || "—", STATUS_CHIP[s]); }
  function jobStatusChip(s) {
    var k = String(s || "pending");
    return chip(k, JOB_STATUS_CHIP[k] || "chip--muted");
  }
  function sourceLabel(m) {
    var harness = m && m.harness ? String(m.harness) : "";
    var base = misUser(m) ? "用户声明" : "Agent 派生";
    if (harness && harness !== "manual") return base + " · " + harness;
    return base;
  }
  /* 统计计数：契约字段缺失即为 0（后端按 GROUP BY 只返回出现过的状态）。 */
  function countOf(v) {
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function intOf(v, dflt) {
    var n = Number(v);
    return isFinite(n) ? n : (dflt === undefined ? 0 : dflt);
  }
  function asDict(v) { return (v && typeof v === "object" && !Array.isArray(v)) ? v : {}; }

  /* ================= 数据区四态 ================= */
  var sections = {};
  $all("[data-sec]").forEach(function (el) { sections[el.getAttribute("data-sec")] = el; });

  function skelBlock(layout) {
    var wrap = h("div");
    if (layout === "rows") {
      for (var i = 0; i < 4; i++) {
        var r = h("div", "skel-row");
        ["", "col-hide-sm", "", "", "col-hide-sm", "col-hide-sm"].forEach(function (c) {
          r.appendChild(h("span", "skel" + (c ? " " + c : "")));
        });
        wrap.appendChild(r);
      }
    } else if (layout === "stats") {
      var g = h("div", "panels stats");
      for (var j = 0; j < 6; j++) {
        var s = h("div", "skel-stat");
        s.appendChild(h("span", "skel"));
        s.appendChild(h("span", "skel"));
        g.appendChild(s);
      }
      wrap.appendChild(g);
    } else if (layout === "canvas") {
      wrap.appendChild(h("div", "skel skel-canvas"));
    } else {
      var l = h("div", "skel-lines");
      l.appendChild(h("span", "skel"));
      l.appendChild(h("span", "skel"));
      l.appendChild(h("span", "skel"));
      wrap.appendChild(l);
    }
    return wrap;
  }

  function statePanel(kind, title, message, opts) {
    opts = opts || {};
    var p = h("div", "state-panel" + (kind === "error" ? " state-panel--error" : ""));
    p.appendChild(h("h2", null, title));
    if (message) p.appendChild(h("p", null, message));
    var rowBtns = null;
    function btnRow() {
      if (!rowBtns) { rowBtns = h("div", "row-btns"); p.appendChild(rowBtns); }
      return rowBtns;
    }
    if (opts.retry) btnRow().appendChild(mkButton("重试", opts.retry));
    if (opts.doctor && kind === "error") p.appendChild(h("p", null, "服务异常时运行 bmem doctor 自检。"));
    if (opts.actionLabel && (opts.action || opts.actionHref)) {
      var b = opts.actionHref ? h("a", "btn btn--primary", opts.actionLabel) : mkButton(opts.actionLabel, opts.action, "btn btn--primary");
      if (opts.actionHref) b.href = opts.actionHref;
      btnRow().appendChild(b);
    }
    return p;
  }

  /* secShow(name, "loading"|"empty"|"error"|"full", opts)
   * opts: layout, title, message, retry, actionLabel/action, actionHref, node (full), tableTbody (selector, rows into tbody) */
  function secShow(name, mode, opts) {
    opts = opts || {};
    var el = sections[name];
    if (!el) return;
    if (opts.tableTbody) {
      var tbody = $(opts.tableTbody);
      if (tbody) {
        tbody.innerHTML = "";
        if (mode === "loading") tbody.appendChild(skelRows(opts.cols || 6));
      }
      el.innerHTML = "";
      if (mode === "empty") el.appendChild(statePanel("empty", opts.title, opts.message, opts));
      if (mode === "error") el.appendChild(statePanel("error", opts.title || "暂时读不到数据", opts.message, opts));
      el.setAttribute("aria-busy", mode === "loading" ? "true" : "false");
      return;
    }
    el.setAttribute("aria-busy", mode === "loading" ? "true" : "false");
    el.innerHTML = "";
    if (mode === "loading") { el.appendChild(skelBlock(opts.layout || "lines")); return; }
    if (mode === "empty") { el.appendChild(statePanel("empty", opts.title || "暂无数据", opts.message, opts)); return; }
    if (mode === "error") { el.appendChild(statePanel("error", opts.title || "暂时读不到数据", opts.message, opts)); return; }
    if (mode === "full" && opts.node) { el.appendChild(opts.node); }
  }

  function skelRows(cols) {
    var tr = document.createElement("tr");
    var td = document.createElement("td");
    td.colSpan = cols;
    td.style.padding = "0";
    for (var i = 0; i < 4; i++) {
      var r = h("div", "skel-row");
      for (var c = 0; c < cols; c++) r.appendChild(h("span", "skel" + (i >= 0 && (c === 1 || c === 4 || c === 5) ? " col-hide-sm" : "")));
      td.appendChild(r);
    }
    tr.appendChild(td);
    return tr;
  }

  function fullNode(children) {
    var fragHost = h("div", "full-contents");
    fragHost.style.display = "contents";
    children.forEach(function (c) { if (c) fragHost.appendChild(c); });
    return fragHost;
  }

  /* ================= toast ================= */
  var toastRegion = $("#toast-region");
  function toast(msg, opts) {
    opts = opts || {};
    var el = h("div", "toast" + (opts.bad ? " toast--bad" : ""));
    el.appendChild(h("span", null, msg));
    var timer;
    function dismiss() {
      clearTimeout(timer);
      el.classList.remove("is-visible");
      setTimeout(function () { el.remove(); }, 300);
    }
    if (opts.actionLabel && opts.action) {
      el.appendChild(mkButton(opts.actionLabel, function () { opts.action(); dismiss(); }, null));
    }
    toastRegion.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("is-visible"); });
    timer = setTimeout(dismiss, opts.actionLabel ? 7000 : 3200);
  }

  /* ================= 路由 / URL 状态 ================= */
  var SCREENS = ["overview", "memories", "graph", "injections", "imports", "sources", "settings"];
  var TITLES = { overview: "总览", memories: "记忆库", graph: "记忆星图", injections: "注入台账", imports: "历史导入", sources: "来源", settings: "设置" };
  var PAGE = 25;

  var state = {
    screen: "overview",
    params: new URLSearchParams(),
    memories: { items: [], sortKey: null, sortDir: 1, count: 0, lastOffset: 0 },
    stats: null,
    graph: { data: null },
    inj: { items: null },
    imports: { items: null },
    currentId: null,
    connector: null,
    runtime: null,
    lastBackup: "",
    booting: true
  };

  function hashFor(screen, params) {
    var p = new URLSearchParams();
    ["q", "kind", "status", "harness", "offset", "prov", "gstatus", "connector"].forEach(function (k) {
      var v = params.get(k);
      if (v && v !== "all" && v !== "0" && v !== "false") p.set(k, v);
    });
    var s = p.toString();
    return "#/" + screen + (s ? "?" + s : "");
  }

  function navigate(screen, mutator) {
    var p = new URLSearchParams();
    if (screen === state.screen) p = new URLSearchParams(state.params.toString());
    if (mutator) mutator(p);
    var next = hashFor(screen, p);
    if (location.hash === next) { applyRoute(); return; }
    location.hash = next;
  }

  function applyRoute() {
    var raw = location.hash.replace(/^#\/?/, "");
    var parts = raw.split("?");
    var screen = SCREENS.indexOf(parts[0]) >= 0 ? parts[0] : "overview";
    state.screen = screen;
    state.params = new URLSearchParams(parts[1] || "");
    render();
  }

  var lastScreen = null;
  function render() {
    var screenChanged = lastScreen !== state.screen;
    lastScreen = state.screen;
    SCREENS.forEach(function (name) {
      var sec = $("#screen-" + name);
      if (sec) sec.hidden = name !== state.screen;
    });
    $all(".nav-item").forEach(function (a) {
      if (a.getAttribute("data-nav") === state.screen) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.title = "bmem web · " + TITLES[state.screen] + " | 本地记忆管理台";
    syncChips();
    if (state.screen === "memories") {
      var q = state.params.get("q") || "";
      var input = $("#mem-search");
      if (input && input.value !== q) input.value = q;
    }
    if (screenChanged && !state.booting) window.scrollTo({ top: 0 });
    /* 连接器：GET 与轮询都在 loadSources / loadSettings / connSyncBrand 内各自管理，避免每次 render 重复取。 */
    if (state.screen === "sources" || state.screen === "settings") $all(".connector-brand").forEach(function (s) { s.value = connSel.brand; });
    else connectorStop();
    var act = {
      overview: loadOverview,
      memories: loadMemories,
      graph: loadGraph,
      injections: loadInjections,
      imports: loadImports,
      sources: loadSources,
      settings: loadSettings
    }[state.screen];
    if (act) act();
  }

  window.addEventListener("hashchange", applyRoute);

  /* ================= 筛选 chips（状态写入 URL） ================= */
  function syncChips() {
    $all("[data-filter-group]").forEach(function (g) {
      var f = g.getAttribute("data-filter-group");
      var onMem = f === "kind" || f === "status" || f === "harness";
      var active = (onMem && state.screen === "memories") || (!onMem && f === "inj-harness" && state.screen === "injections");
      var key = f === "inj-harness" ? "harness" : f;
      var cur = active ? (state.params.get(key) || "all") : "all";
      $all(".chip", g).forEach(function (c) { c.setAttribute("aria-pressed", String(c.getAttribute("data-value") === cur)); });
    });
    $all("[data-graph-filter]").forEach(function (g) {
      var key = g.getAttribute("data-graph-filter") === "prov" ? "prov" : "gstatus";
      var cur = state.screen === "graph" ? (state.params.get(key) || "all") : "all";
      $all(".chip", g).forEach(function (c) { c.setAttribute("aria-pressed", String(c.getAttribute("data-value") === cur)); });
    });
  }

  $all("[data-filter-group],[data-graph-filter]").forEach(function (g) {
    g.addEventListener("click", function (e) {
      var c = e.target.closest ? e.target.closest(".chip") : null;
      if (!c || c.tagName !== "BUTTON") return;
      var v = c.getAttribute("data-value");
      if (g.hasAttribute("data-graph-filter")) {
        var gkey = g.getAttribute("data-graph-filter") === "prov" ? "prov" : "gstatus";
        navigate("graph", function (p) { p.set(gkey, v); });
      } else {
        var fkey = g.getAttribute("data-filter-group") === "inj-harness" ? "harness" : g.getAttribute("data-filter-group");
        if (fkey === "harness" && state.screen === "injections") {
          navigate("injections", function (p) { p.set("harness", v); });
        } else {
          navigate("memories", function (p) { p.set(fkey, v); p.set("offset", "0"); });
        }
      }
    });
  });

  /* ================= 记忆库 ================= */
  var memSeq = 0;
  function loadMemories() {
    var seq = ++memSeq;
    var p = state.params;
    var q = p.get("q") || "";
    var kind = p.get("kind") || "all";
    var status = p.get("status") || "all";
    var harness = p.get("harness") || "all";
    var offset = Math.max(0, parseInt(p.get("offset") || "0", 10) || 0);
    var hasFilter = !!(q || kind !== "all" || status !== "all" || harness !== "all");
    secShow("mem-list", "loading", { tableTbody: "#mem-tbody", cols: 6 });
    setPager(offset, false, false);
    $("#mem-count").textContent = "";
    apiFetch("GET", EP.memories, { query: { q: q, kind: kind, status: status, harness: harness, target: "local", limit: PAGE, offset: offset } })
      .then(function (data) {
        if (seq !== memSeq) return;
        var items = itemsOf(data);
        state.memories.items = items;
        state.memories.count = items.length;
        state.memories.lastOffset = offset;
        if (!items.length) {
          $("#mem-tbody").innerHTML = "";
          if (hasFilter) {
            secShow("mem-list", "empty", {
              tableTbody: "#mem-tbody",
              title: "没有符合条件的记忆",
              message: "调整或清除筛选后重试；当前条件：" + (hasFilter ? describeFilters(q, kind, status, harness) : "无"),
              actionLabel: "清除筛选",
              action: function () { location.hash = "#/memories"; }
            });
          } else {
            secShow("mem-list", "empty", {
              tableTbody: "#mem-tbody",
              title: "这里还没有记忆",
              message: "连接一个 Harness 开始采集，发起一条声明，或到「历史导入」导入既有会话与 CLAUDE.md。",
              actionLabel: "发起声明",
              action: function () { openDeclare("new"); }
            });
          }
          $("#mem-count").textContent = "0 条";
          return;
        }
        sections["mem-list"].innerHTML = "";
        renderMemRows();
        $("#mem-count").textContent = "第 " + fmtNum(offset + 1) + "–" + fmtNum(offset + items.length) + " 条（本页 " + items.length + "）";
        setPager(offset, offset === 0, items.length < PAGE);
      })
      .catch(function (err) {
        if (seq !== memSeq) return;
        $("#mem-tbody").innerHTML = "";
        secShow("mem-list", "error", {
          tableTbody: "#mem-tbody",
          title: "记忆库暂时读不出来",
          message: "GET /api/memories 失败：" + err.message,
          retry: loadMemories,
          doctor: true
        });
      });
  }

  function describeFilters(q, kind, status, harness) {
    var bits = [];
    if (q) bits.push('关键词 "' + truncate(q, 24) + '"');
    if (kind !== "all") bits.push("kind=" + kind);
    if (status !== "all") bits.push("status=" + status);
    if (harness !== "all") bits.push("harness=" + harness);
    return bits.join("，");
  }

  function setPager(offset, prevDisabled, nextDisabled) {
    var prev = $("#mem-prev"), next = $("#mem-next");
    if (prev) prev.disabled = !!prevDisabled;
    if (next) next.disabled = !!nextDisabled;
  }

  function renderMemRows() {
    var tbody = $("#mem-tbody");
    tbody.innerHTML = "";
    var items = state.memories.items.slice();
    if (state.memories.sortKey) {
      var key = state.memories.sortKey, dir = state.memories.sortDir;
      var ORDER = { active: 0, proposed: 1, disputed: 2, superseded: 3, retracted: 4, quarantined: 5, deleted: 6 };
      items.sort(function (a, b) {
        var va, vb;
        if (key === "kind") { va = mkind(a); vb = mkind(b); }
        else if (key === "status") { va = ORDER[mstatus(a)] !== undefined ? ORDER[mstatus(a)] : 9; vb = ORDER[mstatus(b)] !== undefined ? ORDER[mstatus(b)] : 9; }
        else if (key === "ver") { va = mrev(a); vb = mrev(b); }
        else { va = mtime(a) || ""; vb = mtime(b) || ""; }
        if (va < vb) return -1 * dir;
        if (va > vb) return 1 * dir;
        return 0;
      });
    }
    items.forEach(function (m) {
      var id = mid(m);
      var tr = document.createElement("tr");
      tr.setAttribute("data-id", id);
      var st = mstatus(m);
      if (st === "retracted" || st === "deleted") tr.classList.add("is-retracted");

      var td0 = document.createElement("td");
      var btn = h("button", "mem-link");
      btn.type = "button";
      btn.appendChild(h("span", "mem-title", mbody(m) || "（正文已清除）"));
      btn.appendChild(h("span", "mem-id", id + (m.fact_key ? " · " + m.fact_key : "")));
      btn.addEventListener("click", function () { openMemory(id); });
      td0.appendChild(btn);
      tr.appendChild(td0);

      var td1 = h("td", "col-hide-sm", mkind(m));
      tr.appendChild(td1);
      var td2 = document.createElement("td");
      td2.appendChild(statusChip(st));
      tr.appendChild(td2);
      var td3 = h("td", "measured", "v" + mrev(m));
      tr.appendChild(td3);

      var td4 = document.createElement("td");
      td4.classList.add("col-hide-sm");
      td4.appendChild(h("span", null, sourceLabel(m)));
      var t = tierOf(m);
      if (t) td4.appendChild(chip(t, tierChipCls(t)));
      if (sensOf(m) === "private") td4.appendChild(chip("private", "chip--bad"));
      tr.appendChild(td4);

      var td5 = document.createElement("td");
      td5.classList.add("col-hide-sm", "measured");
      td5.appendChild(timeEl(mtime(m)));
      tr.appendChild(td5);

      tbody.appendChild(tr);
    });
  }

  $all("#mem-table th[data-sort-key]").forEach(function (th) {
    $(".th-sort", th).addEventListener("click", function () {
      var key = th.getAttribute("data-sort-key");
      if (state.memories.sortKey === key) state.memories.sortDir *= -1;
      else { state.memories.sortKey = key; state.memories.sortDir = 1; }
      $all("#mem-table th[data-sort-key]").forEach(function (o) { o.setAttribute("aria-sort", "none"); });
      th.setAttribute("aria-sort", state.memories.sortDir === 1 ? "ascending" : "descending");
      renderMemRows();
    });
  });

  $("#mem-search").addEventListener("input", debounce(function (e) {
    navigate("memories", function (p) { p.set("q", e.target.value.trim()); p.set("offset", "0"); });
  }, 350));

  $("#mem-prev").addEventListener("click", function () {
    var offset = Math.max(0, (parseInt(state.params.get("offset") || "0", 10) || 0) - PAGE);
    navigate("memories", function (p) { p.set("offset", String(offset)); });
  });
  $("#mem-next").addEventListener("click", function () {
    var offset = (parseInt(state.params.get("offset") || "0", 10) || 0) + PAGE;
    navigate("memories", function (p) { p.set("offset", String(offset)); });
  });

  $("#act-new-declare").addEventListener("click", function () { openDeclare("new"); });

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(null, args); }, ms);
    };
  }

  /* ================= 总览 ================= */
  function loadOverview() {
    /* 单次 GET /api/stats，三个面板共用同一份规范计数（canonical: memories / status_counts /
     * kind_counts / jobs{pending,running,done,failed,total} / injections / imports）。 */
    var statsReq = apiFetch("GET", EP.stats);

    secShow("ov-stats", "loading", { layout: "stats" });
    statsReq.then(function (s) {
      s = s || {};
      state.stats = s;
      var byStatus = asDict(s.status_counts);
      var jobs = asDict(s.jobs);
      var queued = intOf(jobs.pending) + intOf(jobs.running);
      var defs = [
        ["记忆总数", intOf(s.memories), "全部状态合计"],
        ["active", intOf(byStatus.active), "当前有效"],
        ["proposed", intOf(byStatus.proposed), "观察中，自动仲裁"],
        ["disputed", intOf(byStatus.disputed), "冲突待裁"],
        ["注入记录", intOf(s.injections), "台账总条数"],
        ["队列积压", queued, "jobs.pending + jobs.running"]
      ];
      var grid = h("div", "panels stats");
      grid.setAttribute("role", "group");
      grid.setAttribute("aria-label", "记忆库关键数字");
      defs.forEach(function (d) {
        var st = h("div", "stat");
        st.appendChild(h("span", "stat-label", d[0]));
        /* 计数缺失即 0（GROUP BY 不返回未出现的状态），不能显示成占位符。 */
        st.appendChild(h("span", "stat-figure", fmtNum(d[1])));
        st.appendChild(h("span", "stat-sub", d[2]));
        grid.appendChild(st);
      });
      secShow("ov-stats", "full", { node: grid });
      updateSidebarCounts(s);
      if (s.semantic_enabled !== undefined) setTopSemantic(!!s.semantic_enabled);
    }).catch(function (err) {
      state.stats = null;
      secShow("ov-stats", "error", { title: "统计读取失败", message: "GET /api/stats：" + err.message, retry: loadOverview, doctor: true });
    });

    /* 星图预览（节点字段：memory_id + label） */
    secShow("ov-graph", "loading", { layout: "canvas" });
    apiFetch("GET", EP.graph, { query: { target: "local" } }).then(function (g) {
      state.graph.data = g;
      if (!graphNodes(g).length) {
        secShow("ov-graph", "empty", { title: "还没有记忆节点", message: "入库后自动出现在星图。", layout: "lines", actionLabel: "发起声明", action: function () { openDeclare("new"); } });
        return;
      }
      renderConstellation("ov-graph", g, { mini: true, limit: 24 });
    }).catch(function (err) {
      secShow("ov-graph", "error", { title: "星图读取失败", message: "GET /api/graph：" + err.message, retry: loadOverview });
    });

    /* 当前事实 */
    secShow("ov-facts", "loading", { layout: "lines" });
    apiFetch("GET", EP.memories, { query: { kind: "fact", status: "active", target: "local", limit: 4 } }).then(function (data) {
      var items = itemsOf(data);
      if (!items.length) {
        secShow("ov-facts", "empty", { title: "尚无 active 事实", message: "发起一条声明，或等 Worker 蒸馏出候选事实。", actionLabel: "发起声明", action: function () { openDeclare("new"); } });
        return;
      }
      var ul = h("ul", "facts");
      items.forEach(function (m) {
        var li = document.createElement("li");
        var btn = h("button", "fact-btn");
        btn.type = "button";
        btn.appendChild(h("span", "fact-prov" + (misUser(m) ? " fact-prov--user" : ""), ""));
        btn.appendChild(h("span", "fact-text", truncate(mbody(m), 90)));
        btn.addEventListener("click", function () { openMemory(mid(m)); });
        li.appendChild(btn);
        var meta = h("div", "fact-meta");
        meta.appendChild(h("span", "measured", mid(m) + " v" + mrev(m)));
        meta.appendChild(h("span", null, sourceLabel(m)));
        var ft = tierOf(m);
        if (ft) meta.appendChild(chip(ft, tierChipCls(ft)));
        if (sensOf(m) === "private") meta.appendChild(chip("private", "chip--bad"));
        li.appendChild(meta);
        ul.appendChild(li);
      });
      secShow("ov-facts", "full", { node: ul });
    }).catch(function (err) {
      secShow("ov-facts", "error", { title: "当前事实读取失败", message: err.message, retry: loadOverview });
    });

    /* 类型分布（canonical: kind_counts） */
    secShow("ov-kinds", "loading", { layout: "lines" });
    statsReq.then(function (s) {
      var byKind = asDict(s && s.kind_counts);
      var order = ["episode", "fact", "method", "decision", "preference"];
      var labels = { episode: "episode 经历", fact: "fact 事实", method: "method 方法", decision: "decision 决策", preference: "preference 偏好" };
      var max = 0;
      order.forEach(function (k) { max = Math.max(max, intOf(byKind[k])); });
      if (!max) {
        secShow("ov-kinds", "empty", { title: "暂无类型计数", message: "记忆入库后显示分布。" });
        return;
      }
      var bars = h("div", "bars");
      bars.setAttribute("role", "img");
      bars.setAttribute("aria-label", "类型分布：" + order.map(function (k) { return labels[k] + " " + intOf(byKind[k]); }).join("，"));
      order.forEach(function (k) {
        var v = intOf(byKind[k]);
        var row = h("div", "bar-row");
        row.appendChild(h("span", "bar-label", labels[k]));
        var track = h("span", "bar-track");
        var fill = h("span", "bar-fill bar-fill--" + k);
        fill.style.width = Math.round((v / max) * 100) + "%";
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(h("span", "bar-value", fmtNum(v)));
        bars.appendChild(row);
      });
      secShow("ov-kinds", "full", { node: bars });
    }).catch(function (err) {
      secShow("ov-kinds", "error", { title: "类型分布读取失败", message: "GET /api/stats：" + err.message, retry: loadOverview });
    });

    /* 生命周期（canonical: status_counts，含 quarantined / deleted） */
    secShow("ov-lifecycle", "loading", { layout: "lines" });
    statsReq.then(function (s) {
      var byStatus = asDict(s && s.status_counts);
      var order = ["active", "proposed", "disputed", "superseded", "retracted", "quarantined", "deleted"];
      var lc = h("div", "lifecycle");
      var any = false;
      order.forEach(function (k) {
        if (byStatus[k] === undefined) return;
        any = true;
        var c = h("span", "chip " + (STATUS_CHIP[k] || ""));
        c.appendChild(document.createTextNode(k + " "));
        c.appendChild(h("span", "measured", fmtNum(intOf(byStatus[k]))));
        lc.appendChild(c);
      });
      if (!any) {
        secShow("ov-lifecycle", "empty", { title: "暂无状态计数", message: "记忆入库后显示生命周期。" });
        return;
      }
      secShow("ov-lifecycle", "full", { node: lc });
    }).catch(function (err) {
      secShow("ov-lifecycle", "error", { title: "生命周期读取失败", message: "GET /api/stats：" + err.message, retry: loadOverview });
    });

    /* 提炼 Worker：health（service=bmem、worker.running 布尔）+ stats.jobs + /api/jobs 明细 */
    secShow("ov-worker", "loading", { layout: "lines" });
    Promise.all([apiFetch("GET", EP.health), apiFetch("GET", EP.jobs), statsReq.catch(function () { return null; })]).then(function (rs) {
      secShow("ov-worker", "full", { node: workerPanel(rs[0] || {}, itemsOf(rs[1]), rs[2] || {}) });
    }).catch(function (err) {
      secShow("ov-worker", "error", { title: "Worker 状态读取失败", message: err.message, retry: loadOverview, doctor: true });
    });

    /* 仲裁队列 */
    secShow("ov-arb", "loading", { layout: "lines" });
    apiFetch("GET", EP.stats_arb, { query: { limit: 6 } }).then(function (data) {
      var items = itemsOf(data);
      if (!items.length) {
        secShow("ov-arb", "empty", { title: "仲裁台账为空", message: "候选记忆进入观察期或发生冲突时，自动决策会记录在这里。" });
        return;
      }
      var ul = h("ul", "arb-queue");
      items.slice(0, 6).forEach(function (d) {
        var li = document.createElement("li");
        var head = h("div", "arb-head");
        var id = String(d.memory_id || "");
        if (id) {
          var b = h("button", "arb-link measured", id);
          b.type = "button";
          b.addEventListener("click", function () { openMemory(id); });
          head.appendChild(b);
        }
        var dec = String(d.decision || "—");
        head.appendChild(chip(dec, decisionChipCls(dec)));
        if (d.confidence !== undefined && d.confidence !== null && isFinite(Number(d.confidence))) head.appendChild(h("span", "measured", "conf " + Number(d.confidence).toFixed(2)));
        head.appendChild(timeEl(d.occurred_at || d.created_at));
        li.appendChild(head);
        li.appendChild(h("span", "arb-detail", truncate(d.reason || "（无原因记录）", 160)));
        ul.appendChild(li);
      });
      secShow("ov-arb", "full", { node: ul });
    }).catch(function (err) {
      secShow("ov-arb", "error", { title: "仲裁台账读取失败", message: "GET /api/arbitration：" + err.message, retry: loadOverview });
    });

    /* 最近活动（jobs: kind / status / attempts / event_id / last_error） */
    secShow("ov-activity", "loading", { layout: "lines" });
    apiFetch("GET", EP.jobs).then(function (data) {
      var items = sortJobsDesc(itemsOf(data)).slice(0, 6);
      if (!items.length) {
        secShow("ov-activity", "empty", { title: "暂无后台任务", message: "采集事件入库后，Worker 会排队蒸馏与向量化任务。" });
        return;
      }
      var ol = h("ol", "trail");
      items.forEach(function (j) {
        var li = document.createElement("li");
        li.appendChild(h("span", "trail-ver", jobKind(j)));
        var cls = j.status === "done" ? "who--agent" : (j.status === "failed" ? "who--user" : "who--system");
        li.appendChild(h("span", cls, jobLabel(jobKind(j))));
        li.appendChild(jobStatusChip(j.status));
        li.appendChild(timeEl(j.updated_at || j.finished_at || j.created_at || j.available_at));
        var attempts = intOf(j.attempts, 1);
        var txt = j.event_id ? "事件 " + truncate(String(j.event_id), 24) + " · 尝试 " + attempts + " 次" : "尝试 " + attempts + " 次";
        if (j.status === "failed" && j.last_error) txt += " · 错误：" + truncate(String(j.last_error), 80);
        li.appendChild(h("span", "trail-text", txt));
        ol.appendChild(li);
      });
      secShow("ov-activity", "full", { node: ol });
    }).catch(function (err) {
      secShow("ov-activity", "error", { title: "活动读取失败", message: "GET /api/jobs：" + err.message, retry: loadOverview });
    });
  }

  var JOB_LABELS = { distill: "蒸馏候选", embed: "向量回填", promote: "到期晋升", maintain: "周期维护", import: "历史导入" };
  /* jobs 契约字段：kind / status / attempts；kind 直接绑定，不猜别名。 */
  function jobKind(j) { return String((j && j.kind) || "job"); }
  function jobLabel(k) { return JOB_LABELS[k] || k; }
  function sortJobsDesc(items) {
    return items.slice().sort(function (a, b) {
      return String(b.updated_at || b.finished_at || b.created_at || "").localeCompare(String(a.updated_at || a.finished_at || a.created_at || ""));
    });
  }

  /* 提炼 Worker 面板：GET /api/health → {service,version,schema_version,worker:{running,cycles,
   * errors,last_error,last_success_at}}；GET /api/stats → jobs:{pending,running,done,failed,total}。 */
  function workerPanel(health, jobs, s) {
    var dl = h("dl", "kv");
    function row(label, node) {
      dl.appendChild(h("dt", null, label));
      var dd = document.createElement("dd");
      dd.appendChild(node);
      dl.appendChild(dd);
    }
    /* 成功的 health 响应即代表服务在线；service 是产品标识（bmem），不是状态字面量。 */
    var svcName = String(health.service || "bmem");
    var ver = health.version ? " v" + String(health.version) : "";
    row("服务", chip(svcName + ver + " 在线", "chip--ok"));

    var wk = asDict(health.worker);
    var workerRunning = wk.running === true;
    var wChip = chip(workerRunning ? "运行中" : "已停止", workerRunning ? "chip--ok" : "chip--bad");
    if (wk.cycles !== undefined) wChip.appendChild(document.createTextNode(" · 周期 " + fmtNum(intOf(wk.cycles))));
    row("Worker", wChip);
    row("最近成功", wk.last_success_at ? timeEl(wk.last_success_at) : h("span", null, "尚无记录"));
    var errs = intOf(wk.errors);
    if (errs > 0) {
      var en = h("span", null, errs + " 次错误");
      if (wk.last_error) en.appendChild(chip(truncate(String(wk.last_error), 60), "chip--bad"));
      row("Worker 错误", en);
    }

    var jc = asDict(s && s.jobs);
    var hasJobs = jc.pending !== undefined || jc.running !== undefined || jc.done !== undefined || jc.failed !== undefined || jc.total !== undefined;
    if (hasJobs) {
      row("队列", h("span", null,
        intOf(jc.pending) + " 待处理 · " + intOf(jc.running) + " 运行中 · " +
        intOf(jc.done) + " 已完成 · " + intOf(jc.failed) + " 失败（共 " + intOf(jc.total) + "）"));
    } else {
      row("队列", h("span", null, "stats 未返回 jobs 计数"));
    }

    var sorted = sortJobsDesc(jobs);
    var latest = sorted.length ? sorted[0] : null;
    row("最近任务", latest
      ? h("span", "measured", jobLabel(jobKind(latest)) + " · " + String(latest.status || "—") + " · " + fmtTime(latest.updated_at || latest.finished_at || latest.created_at))
      : h("span", null, "暂无任务"));
    var failed = jobs.filter(function (j) { return j.status === "failed"; });
    if (failed.length) {
      var fnode = h("span", null, failed.length + " 个失败");
      fnode.appendChild(chip(truncate(failed[0].last_error || "错误已记录", 60), "chip--bad"));
      row("失败", fnode);
    }
    return dl;
  }
  var DECISION_CHIP = { ADD: "chip--ok", UPDATE: "chip--cool", NOOP: "chip--muted", DISPUTE: "chip--bad", QUARANTINE: "chip--bad", PROMOTE: "chip--ok", FORGET: "chip--muted", SUPERSEDE: "chip--muted", DEMOTE: "chip--muted" };
  /* 决策标签着色：用户声明产生的决策走琥珀，Agent 派生走蓝，冲突/隔离走红。 */
  function decisionChipCls(dec) {
    var d = String(dec || "").toUpperCase();
    if (d === "ADD" || d === "DECLARE" || d === "CREATE") return "chip--amber";
    return DECISION_CHIP[d] || "chip--cool";
  }

  $("#btn-worker-run").addEventListener("click", function () {
    var b = this;
    if (b.disabled) return;
    b.disabled = true;
    apiFetch("POST", EP.workerRun).then(function (r) {
      /* POST /api/worker/run → run_once()：{ok,claimed,job_id,kind,status,error,promoted,maintenance}
       * promoted = {promoted_count, disputed_count, checked, …} */
      r = r || {};
      var bits = [];
      if (r.claimed === true) bits.push("处理 " + String(r.kind || "job") + " → " + String(r.status || "—"));
      else if (String(r.status || "") === "idle") bits.push("队列空闲");
      else if (r.status) bits.push("状态 " + String(r.status));
      var pr = asDict(r.promoted);
      if (pr.promoted_count !== undefined || pr.disputed_count !== undefined) {
        bits.push("晋升 " + intOf(pr.promoted_count) + " · 转争议 " + intOf(pr.disputed_count) + " · 检查 " + intOf(pr.checked));
      }
      if (r.error) bits.push("错误：" + truncate(String(r.error), 80));
      toast("run_once：" + (bits.join(" · ") || "无返回字段"));
      afterWrite();
    }).catch(function (err) {
      toast("处理队列任务失败：" + err.message, { bad: true });
    }).then(function () { b.disabled = false; });
  });

  function updateSidebarCounts(s) {
    s = s || {};
    /* 侧边栏计数直接绑定 stats 的规范字段。 */
    var total = countOf(s.memories);
    var inj = countOf(s.injections);
    var imp = countOf(s.imports);
    var n1 = $("#nav-count-memories"), n2 = $("#nav-count-injections"), n3 = $("#nav-count-imports");
    if (n1) n1.textContent = total === null ? "" : fmtNum(total);
    if (n2) n2.textContent = inj === null ? "" : fmtNum(inj);
    if (n3) n3.textContent = imp === null ? "" : fmtNum(imp);
  }

  /* 声明 / 纠正 / 遗忘之后刷新：概览数字、侧边栏、列表与星图缓存都不能停留在旧快照。 */
  function refreshSidebarStats() {
    return apiFetch("GET", EP.stats).then(function (s) {
      state.stats = s || {};
      updateSidebarCounts(state.stats);
      if (state.stats.semantic_enabled !== undefined) setTopSemantic(!!state.stats.semantic_enabled);
      return state.stats;
    }).catch(function () {
      state.stats = null;
      updateSidebarCounts({});
      return null;
    });
  }

  function reloadScreenData() {
    var act = {
      overview: loadOverview,
      memories: loadMemories,
      graph: loadGraph,
      injections: loadInjections,
      imports: loadImports,
      sources: loadSources,
      settings: loadSettings
    }[state.screen];
    if (act) act();
  }

  function afterWrite() {
    invalidateCaches();
    pollHealth();
    if (state.screen === "sources" || state.screen === "settings") connectorRefresh();
    if (state.screen === "overview") {
      loadOverview();
    } else {
      refreshSidebarStats();
      reloadScreenData();
    }
  }

  /* ================= 记忆星图 ================= */
  var svgSeq = 0;
  /* graph 契约：{nodes:[{memory_id,body|label,kind,status,trust_tier,sensitivity,fact_key}],
   * edges:[{source,target,relation}]}。节点正文在真实响应里是截断后的 label 字段。 */
  function graphNodes(g) {
    return (g && Array.isArray(g.nodes)) ? g.nodes : [];
  }
  function graphId(node) { return String(node.id || mid(node)); }
  function graphEdges(g) {
    var ids = {};
    graphNodes(g).forEach(function (n) { ids[graphId(n)] = true; });
    var edges = (g && Array.isArray(g.edges)) ? g.edges : [];
    return edges.filter(function (e) { return e && e.source && e.target && ids[e.source] && ids[e.target] && e.source !== e.target; });
  }
  function nodeShape(kind) {
    if (kind === "source") return svg("path", { class: "shape", d: "M-12 -10H12V10H-12ZM-7 -4H7M-7 3H4", fill: "none" });
    if (kind === "decision") return svg("rect", { class: "shape", x: -11, y: -11, width: 22, height: 22, rx: 3 });
    if (kind === "method") return svg("path", { class: "shape", d: "M0 -14 14 0 0 14 -14 0Z" });
    if (kind === "episode") return svg("path", { class: "shape", d: "M0 -13 11 -6.5 11 6.5 0 13 -11 6.5 -11 -6.5Z" });
    if (kind === "preference") return svg("rect", { class: "shape", x: -16, y: -8, width: 32, height: 16, rx: 8 });
    return svg("circle", { class: "shape", cx: 0, cy: 0, r: 13 });
  }

  function forceLayout(nodes, edges, W, H) {
    var pos = {};
    nodes.forEach(function (n, i) {
      var a = i * 2.399963229728653;
      var r = 26 * Math.sqrt(i + 1);
      var x = W / 2 + r * Math.cos(a) * 0.9;
      var y = H / 2 + r * Math.sin(a) * 0.62;
      pos[graphId(n)] = { x: clamp(x, 40, W - 40), y: clamp(y, 40, H - 40), vx: 0, vy: 0 };
    });
    var links = edges.filter(function (e) { return pos[e.source] && pos[e.target] && e.source !== e.target; });
    var iterations = nodes.length > 60 ? 180 : 260;
    for (var it = 0; it < iterations; it++) {
      var alpha = 1 - it / iterations;
      for (var i = 0; i < nodes.length; i++) {
        var pi = pos[graphId(nodes[i])];
        for (var j = i + 1; j < nodes.length; j++) {
          var pj = pos[graphId(nodes[j])];
          var dx = pj.x - pi.x, dy = pj.y - pi.y;
          var d2 = dx * dx + dy * dy;
          if (d2 < 1) { dx = (i - j) * 0.13; dy = 0.37; d2 = 1; }
          var f = Math.min(2600 / d2, 4) * alpha;
          var d0 = Math.sqrt(d2);
          pi.vx -= (dx / d0) * f; pi.vy -= (dy / d0) * f;
          pj.vx += (dx / d0) * f; pj.vy += (dy / d0) * f;
        }
        pi.vx += (W / 2 - pi.x) * 0.014 * alpha;
        pi.vy += (H / 2 - pi.y) * 0.018 * alpha;
      }
      links.forEach(function (e) {
        var ps = pos[e.source], pt = pos[e.target];
        var dx = pt.x - ps.x, dy = pt.y - ps.y;
        var d = Math.sqrt(dx * dx + dy * dy) || 1;
        var f = (d - 108) * 0.018 * alpha;
        ps.vx += (dx / d) * f; ps.vy += (dy / d) * f;
        pt.vx -= (dx / d) * f; pt.vy -= (dy / d) * f;
      });
      nodes.forEach(function (n) {
        var p = pos[graphId(n)];
        p.x = clamp(p.x + p.vx * 0.6, 40, W - 40);
        p.y = clamp(p.y + p.vy * 0.6, 34, H - 34);
        p.vx *= 0.55; p.vy *= 0.55;
      });
    }
    return pos;
  }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function renderConstellation(host, data, opts) {
    opts = opts || {};
    var hostEl = host === "graph" ? sections["graph"] : sections[host];
    hostEl.innerHTML = "";
    var limit = opts.limit || 240;
    var view = graphView(data, limit);
    var nodes = view.nodes;
    var edges = view.edges;

    var W = 960, H = 560;
    var pos = forceLayout(nodes, edges, W, H);
    var seq = ++svgSeq;
    var glowId = "node-glow-" + seq, arrowId = "edge-arrow-" + seq;

    var root = svg("svg", {
      class: "constellation" + (nodes.length > 24 ? " constellation--dense" : ""), viewBox: "0 0 " + W + " " + H,
      role: opts.mini ? "img" : "group",
      "aria-label": opts.mini
        ? "记忆星图缩略预览：形状分类型，琥珀为用户声明，蓝为 Agent 派生，虚线环为待确认"
        : "交互记忆星图：Tab 聚焦节点或关系，Enter 打开详情，悬停高亮关联"
    });
    var defs = svg("defs");
    var filter = svg("filter", { id: glowId, x: "-80%", y: "-80%", width: "260%", height: "260%" });
    filter.appendChild(svg("feGaussianBlur", { stdDeviation: "4" }));
    defs.appendChild(filter);
    var marker = svg("marker", { id: arrowId, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
    marker.appendChild(svg("path", { d: "M0 0 8 4 0 8Z", fill: "var(--color-rule-2)" }));
    defs.appendChild(marker);
    root.appendChild(defs);

    var gEdges = svg("g");
    var gNodes = svg("g");
    var edgeEls = [];
    edges.forEach(function (e) {
      var ps = pos[e.source], pt = pos[e.target];
      if (!ps || !pt) return;
      var rel = String(e.relation || e.kind || "");
      var historical = e.evidence && e.evidence.length && e.evidence.every(function (p) { return !!p.invalidated_at; });
      var dx = pt.x - ps.x, dy = pt.y - ps.y;
      var d = Math.sqrt(dx * dx + dy * dy) || 1;
      var inset = rel.indexOf("supersede") >= 0 ? 16 : 19;
      var line = svg("line", {
        class: "edge" + (rel.indexOf("supersede") >= 0 ? " edge--supersede" : "") + (historical ? " edge--historical" : ""),
        x1: ps.x + (dx / d) * 15, y1: ps.y + (dy / d) * 15,
        x2: pt.x - (dx / d) * inset, y2: pt.y - (dy / d) * inset
      });
      line.setAttribute("data-from", e.source);
      line.setAttribute("data-to", e.target);
      if (rel === "derived") line.setAttribute("marker-end", "url(#" + arrowId + ")");
      var t = svg("title");
      t.textContent = (RELATION_ZH[rel] || rel) + (historical ? " · 历史已失效" : "");
      line.appendChild(t);
      var group = svg("g", { class: "graph-relation", role: "button", tabindex: "0", "aria-label": t.textContent + "，查看证据" });
      var hit = svg("line", { class: "edge-hit", x1: line.getAttribute("x1"), y1: line.getAttribute("y1"), x2: line.getAttribute("x2"), y2: line.getAttribute("y2") });
      group.appendChild(line);
      group.appendChild(hit);
      group.addEventListener("click", function () { openGraphRelation(e, data); });
      group.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openGraphRelation(e, data); }
      });
      gEdges.appendChild(group);
      edgeEls.push({ el: line, from: e.source, to: e.target, data: e });
    });
    root.appendChild(gEdges);

    var nodeEls = [];
    nodes.forEach(function (n) {
      var p = pos[graphId(n)];
      if (!p) return;
      var id = graphId(n), st = mstatus(n), kind = mkind(n);
      var user = misUser(n);
      var cls = "node " + (st === "disputed" ? "node--disputed" : (user ? "node--user" : "node--agent")) + " node--" + (st || "active") + (n.node_type === "source" ? " node--source" : "");
      var g = svg("g", { class: cls, transform: "translate(" + p.x.toFixed(1) + " " + p.y.toFixed(1) + ")" });
      g.setAttribute("data-id", id);
      g.setAttribute("tabindex", "0");
      g.setAttribute("role", "button");
      var label = truncate(mbody(n) || id, 26);
      var kindZh = KIND_ZH[kind] || kind;
      g.setAttribute("aria-label", kindZh + " " + label + (n.node_type === "revision" ? " v" + n.revision_no : "") + (st ? "，" + st : ""));
      g.appendChild(svg("circle", { class: "halo", cx: 0, cy: 0, r: st === "disputed" ? 21 : 21 }));
      g.appendChild(nodeShape(kind));
      if (st === "disputed") g.appendChild(svg("circle", { class: "shape", cx: 0, cy: 0, r: 7, fill: "none" }));
      var hit = svg("circle", { class: "hit", cx: 0, cy: 0, r: 26 });
      g.insertBefore(hit, g.firstChild);
      var ttl = svg("title");
      ttl.textContent = id + " · " + label;
      g.insertBefore(ttl, g.firstChild);
      if (!opts.mini) {
        var txt = svg("text", { class: "node-label", x: 0, y: 32 });
        txt.textContent = (n.node_type === "revision" ? "v" + n.revision_no + " · " : "") + truncate(mbody(n) || id, 16);
        g.appendChild(txt);
      }
      g.addEventListener("click", function () { openGraphNode(n); });
      g.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openGraphNode(n); }
      });
      gNodes.appendChild(g);
      nodeEls.push({ el: g, id: id, data: n });
    });
    root.appendChild(gNodes);
    hostEl.appendChild(root);

    function neighbors(id) {
      var set = {};
      set[id] = true;
      edgeEls.forEach(function (e) {
        if (e.from === id) set[e.to] = true;
        if (e.to === id) set[e.from] = true;
      });
      return set;
    }
    function highlight(id) {
      var set = id ? neighbors(id) : null;
      nodeEls.forEach(function (n) {
        var vis = n.el.style.display !== "none";
        var lit = !!(set && set[n.id]) && vis;
        n.el.classList.toggle("is-dim", !!set && !lit);
        n.el.classList.toggle("is-lit", lit);
      });
      edgeEls.forEach(function (e) {
        var vis = e.el.style.display !== "none";
        var on = !!(set && vis && (e.from === id || e.to === id));
        e.el.classList.toggle("is-dim", !!set && !on);
      });
    }
    nodeEls.forEach(function (n) {
      n.el.addEventListener("mouseenter", function () { highlight(n.id); });
      n.el.addEventListener("mouseleave", function () { highlight(null); });
      n.el.addEventListener("focus", function () { highlight(n.id); });
      n.el.addEventListener("blur", function () { highlight(null); });
    });

    var note = h("p", "panel-note", "");
    var memories = nodes.filter(function (n) { return !n.node_type || n.node_type === "memory"; }).length;
    note.textContent = memories + " 条记忆、" + (nodes.length - memories) + " 个来源或历史节点、" + edges.length + " 条关系。";
    if (view.total > nodes.length) note.textContent += " 当前展示 " + nodes.length + " / " + fmtNum(view.total) + " 个筛选后节点，优先显示有关联的端点。";
    if (note.textContent) hostEl.appendChild(note);
  }

  var KIND_ZH = { fact: "事实", decision: "决策", method: "方法", episode: "经历", preference: "偏好", source: "来源" };
  var RELATION_ZH = { derived: "派生依据", evidence: "共同来源", supersedes: "版本替代" };

  function graphView(data, limit) {
    var prov = state.screen === "graph" ? (state.params.get("prov") || "all") : "all";
    var gstatus = state.screen === "graph" ? (state.params.get("gstatus") || "all") : "all";
    var visible = {};
    var nodes = graphNodes(data);
    nodes.forEach(function (n) {
      if (n.node_type && n.node_type !== "memory") return;
      var st = mstatus(n);
      var user = misUser(n);
      var provOk = prov === "all" || (prov === "user" ? user : !user);
      var stOk = gstatus === "all" || st === gstatus || (gstatus === "active" && (st === "" || st === "active"));
      if (provOk && stOk) visible[graphId(n)] = true;
    });
    nodes.forEach(function (n) {
      if (n.node_type === "revision" && visible[n.memory_id]) visible[graphId(n)] = true;
      if (n.node_type === "source" && n.memory_ids.some(function (id) { return visible[id]; })) visible[graphId(n)] = true;
    });
    nodes = nodes.filter(function (n) { return visible[graphId(n)]; });
    var byId = {}, rank = {};
    nodes.forEach(function (n, i) { byId[graphId(n)] = n; rank[graphId(n)] = i; });
    var edges = graphEdges(data).filter(function (e) { return visible[e.source] && visible[e.target]; });
    edges.sort(function (a, b) { return Math.min(rank[a.source], rank[a.target]) - Math.min(rank[b.source], rank[b.target]); });
    var chosen = {}, selected = [];
    edges.forEach(function (e) {
      var additions = [e.source, e.target].filter(function (id) { return !chosen[id]; });
      if (selected.length + additions.length > limit) return;
      additions.forEach(function (id) { chosen[id] = true; selected.push(byId[id]); });
    });
    nodes.forEach(function (n) {
      var id = graphId(n);
      if (selected.length < limit && !chosen[id] && (!n.node_type || n.node_type === "memory")) {
        chosen[id] = true;
        selected.push(n);
      }
    });
    return { nodes: selected, edges: edges.filter(function (e) { return chosen[e.source] && chosen[e.target]; }), total: nodes.length };
  }

  /* 父级直接来自详情 memory.sources[].derived_from（不依赖星图缓存）；
   * 子级来自 /api/graph 的 edges（source=本条，target=派生记忆）。 */
  function linkBtn(text, targetId) {
    var b = h("button", "arb-link measured", text);
    b.type = "button";
    b.addEventListener("click", function () { openMemory(targetId); });
    return b;
  }
  function parentIds(memory) {
    var seen = {}, out = [];
    var srcs = Array.isArray(memory && memory.sources) ? memory.sources : [];
    srcs.forEach(function (s) {
      var der = Array.isArray(s && s.derived_from) ? s.derived_from : [];
      der.forEach(function (d) {
        var pid = String(d || "");
        if (!pid || pid === mid(memory) || seen[pid]) return;
        seen[pid] = true;
        out.push(pid);
      });
    });
    return out;
  }
  function childIds(g, id) {
    var seen = {}, out = [];
    graphEdges(g).forEach(function (e) {
      if (e.relation !== "derived" || e.source !== id || seen[e.target]) return;
      seen[e.target] = true;
      out.push(e.target);
    });
    return out;
  }
  function paintDerived(host, id, parents, children, childLabel) {
    host.innerHTML = "";
    if (parents.length) {
      host.appendChild(h("span", "derived-label", "向上派生自："));
      parents.forEach(function (pid) { host.appendChild(linkBtn(pid, pid)); });
    }
    if (children.length) {
      host.appendChild(h("span", "derived-label", "由本条派生："));
      children.forEach(function (cid) {
        var text = cid + (childLabel && childLabel[cid] ? " · " + truncate(childLabel[cid], 24) : "");
        host.appendChild(linkBtn(text, cid));
      });
    }
    if (!parents.length && !children.length) {
      host.appendChild(h("span", "measured", "当前无上下游派生关系。"));
    }
  }
  function fillDerived(id, host, memory) {
    var parents = parentIds(memory);
    function paintChildren(g) {
      var byId = {};
      graphNodes(g).forEach(function (n) { if (!n.node_type || n.node_type === "memory") byId[mid(n)] = mbody(n); });
      paintDerived(host, id, parents, childIds(g, id), byId);
    }
    if (state.graph.data && graphNodes(state.graph.data).length) { paintChildren(state.graph.data); return; }
    /* 星图缓存未加载也要先显示父级（来自 sources[].derived_from），再补子级。 */
    paintDerived(host, id, parents, [], null);
    if (parents.length) host.appendChild(h("span", "measured", " 下游派生读取中…"));
    apiFetch("GET", EP.graph, { query: { target: "local" } }).then(function (g) {
      state.graph.data = g;
      paintChildren(g);
    }).catch(function (err) {
      if (!parents.length) {
        host.innerHTML = "";
        host.appendChild(h("span", "measured", " 派生链读取失败：" + err.message));
      } else {
        host.appendChild(h("span", "measured", " 下游派生读取失败：" + err.message));
      }
    });
  }

  function loadGraph() {
    secShow("graph", "loading", { layout: "canvas" });
    apiFetch("GET", EP.graph, { query: { target: "local" } }).then(function (g) {
      state.graph.data = g;
      if (!graphNodes(g).length) {
        secShow("graph", "empty", { title: "还没有记忆节点", message: "入库后自动出现在星图；也可先发起一条声明。", actionLabel: "发起声明", action: function () { openDeclare("new"); } });
        return;
      }
      renderConstellation("graph", g, { mini: false });
    }).catch(function (err) {
      secShow("graph", "error", { title: "星图读取失败", message: "GET /api/graph：" + err.message, retry: function () { state.graph.data = null; loadGraph(); }, doctor: true });
    });
  }

  /* ================= 详情抽屉 ================= */
  $("#graph-refresh").addEventListener("click", loadGraph);

  function readOnlyDetail(title, id) {
    drawerRequest += 1;
    state.currentId = null;
    state.currentMemory = null;
    openDrawerShell(document.activeElement);
    $("#drawer-title").textContent = title;
    $("#drawer-id").textContent = id || "";
    $("#drawer-status").hidden = true;
    $("#drawer-chips").innerHTML = "";
    drawerLoading();
    var host = $("#drawer-detail");
    host.innerHTML = "";
    return host;
  }

  function eventLink(id) {
    var button = h("button", "arb-link measured", "来源事件 " + truncate(String(id), 18));
    button.type = "button";
    button.addEventListener("click", function () { openEvent(String(id)); });
    return button;
  }

  function openEvent(id) {
    var host = readOnlyDetail("来源事件", id);
    var request = drawerRequest;
    host.appendChild(h("p", "drawer-text", "读取中…"));
    apiFetch("GET", EP.event(id), { query: { target: "local" } }).then(function (event) {
      if (request !== drawerRequest || !drawerOpen) return;
      host.innerHTML = "";
      [["来源", event.harness], ["类型", event.source_type], ["发生时间", event.occurred_at], ["事实生效时间", event.effective_at],
       ["采集时间", event.ingested_at], ["原始标识", event.native_event_id]].forEach(function (pair) {
        if (pair[1]) host.appendChild(h("p", "measured", pair[0] + " " + String(pair[1])));
      });
      host.appendChild(h("pre", "drawer-text", event.body || "原文已清除。"));
      host.appendChild(h("p", "measured", event.has_evidence ? "原始证据文件可用" : "原始证据文件已不可用"));
    }).catch(function (err) {
      if (request !== drawerRequest || !drawerOpen) return;
      host.innerHTML = "";
      host.appendChild(statePanel("error", "来源读取失败", err.message, { retry: function () { openEvent(id); } }));
    });
  }

  function openGraphNode(node) {
    if (node.node_type !== "source") {
      openMemory(node.memory_id || graphId(node), true, node.node_type === "revision" ? node.revision_no : undefined);
      return;
    }
    var host = readOnlyDetail("共同来源", node.root_source_id);
    host.appendChild(h("p", "drawer-text", mbody(node)));
    node.event_ids.forEach(function (id) { host.appendChild(eventLink(id)); });
    var byId = {};
    graphNodes(state.graph.data).forEach(function (n) { byId[graphId(n)] = n; });
    var section = h("div", "drawer-section");
    section.appendChild(h("h3", null, "关联记忆"));
    node.memory_ids.forEach(function (id) { section.appendChild(linkBtn(byId[id] ? mbody(byId[id]) : id, id)); });
    host.appendChild(section);
  }

  function openGraphRelation(edge, data) {
    var host = readOnlyDetail(RELATION_ZH[edge.relation] || edge.relation);
    var endpoints = {};
    graphNodes(data).forEach(function (n) { endpoints[graphId(n)] = n; });
    var labels = edge.relation === "supersedes" ? ["上一版", "当前版"] : ["依据", "关联记忆"];
    [edge.source, edge.target].forEach(function (id, index) {
      var node = endpoints[id];
      var button = h("button", "arb-link", (node.node_type === "revision" ? "v" + node.revision_no + " · " : "") + mbody(node));
      button.type = "button";
      button.addEventListener("click", function () { openGraphNode(node); });
      var section = h("div", "drawer-section");
      section.appendChild(h("p", "measured", labels[index]));
      section.appendChild(button);
      host.appendChild(section);
    });
    edge.evidence.forEach(function (proof) {
      var section = h("div", "drawer-section");
      section.appendChild(h("h3", null, proof.source_row_id ? "来源记录 #" + proof.source_row_id : "修订记录"));
      if (proof.from_revision) section.appendChild(h("p", "drawer-text", "v" + proof.from_revision + " → v" + proof.to_revision));
      [["证据根", proof.root_source_id], ["来源类型", proof.source_type], ["可信度", proof.trust_tier],
       ["记录时间", proof.created_at], ["失效时间", proof.invalidated_at]].forEach(function (pair) {
        if (pair[1]) section.appendChild(h("p", "measured", pair[0] + " " + String(pair[1])));
      });
      section.appendChild(h("p", "drawer-text", proof.invalidated_at ? "历史关系，来源已失效" : "依据保存在来源或修订记录中"));
      if (proof.previous_event_id) section.appendChild(eventLink(proof.previous_event_id));
      if (proof.event_id && proof.event_id !== proof.previous_event_id) section.appendChild(eventLink(proof.event_id));
      host.appendChild(section);
    });
  }

  var drawer = $("#drawer");
  var frame = $("#app-frame");
  var scrim = $("#scrim");
  var lastFocus = null;
  var drawerOpen = false;
  var drawerRequest = 0;
  var forgetArmed = false;

  function focusables(root) {
    return $all('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', root)
      .filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }

  function openDrawerShell(trigger) {
    if (drawerOpen) return;
    drawerOpen = true;
    if (trigger && trigger.focus) lastFocus = trigger; /* 键盘触发：记住触发元素，Escape 归还焦点 */
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    scrim.hidden = false;
    try { frame.inert = true; } catch (e) { /* 老浏览器靠 Tab 捕获兜底 */ }
    document.body.style.overflow = "hidden";
    /* 抽屉刚加 is-open（transform: none）时同一帧内 offsetParent 仍为 null；
     * 等下一帧可见后再聚焦关闭按钮或第一个可聚焦控件。 */
    requestAnimationFrame(function () {
      if (!drawerOpen) return;
      var close = $("#drawer-close");
      var f = focusables(drawer);
      var target = (close && !close.disabled) ? close : (f.length ? f[0] : null);
      if (target) target.focus();
    });
  }
  function closeDrawer() {
    if (!drawerOpen) return;
    drawerRequest += 1;
    drawerOpen = false;
    drawer.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
    scrim.hidden = true;
    try { frame.inert = false; } catch (e) {}
    document.body.style.overflow = "";
    disarmForget();
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  scrim.addEventListener("click", closeDrawer);
  $("#drawer-close").addEventListener("click", closeDrawer);
  drawer.addEventListener("keydown", function (e) {
    if (e.key !== "Tab" || !drawerOpen) return;
    var f = focusables(drawer);
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (drawerOpen) { closeDrawer(); return; }
      if (!searchPop.hidden) { hideSearchPop(true); return; }
    }
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      $("#global-search").focus();
      $("#global-search").select();
    }
  });

  function drawerLoading(text) {
    $("#drawer-detail").hidden = true;
    $("#declare-form").hidden = true;
    $("#drawer-actions").hidden = true;
    var host = $("#drawer-detail");
    host.innerHTML = "";
    host.appendChild(h("p", "drawer-text", text || "读取中…"));
    host.hidden = false;
  }

  function openMemory(id, viaKeyboard, revisionNo) {
    if (!id) { toast("缺少记忆 ID"); return; }
    var request = ++drawerRequest;
    lastFocus = document.activeElement;
    state.currentId = id;
    openDrawerShell(viaKeyboard ? lastFocus : null);
    $("#drawer-id").textContent = id;
    $("#drawer-status").hidden = true;
    $("#drawer-chips").innerHTML = "";
    drawerLoading("正在读取 " + id + " …");
    apiFetch("GET", EP.memory(id), { query: { target: "local" } }).then(function (m) {
      if (request !== drawerRequest || !drawerOpen) return;
      renderMemoryDetail(m || { memory_id: id });
      if (revisionNo !== undefined) {
        $("#drawer-chips").appendChild(chip("查看历史 v" + revisionNo, "chip--muted"));
        var revision = $("#drawer-detail [data-revision-no='" + Number(revisionNo) + "']");
        if (revision) {
          revision.classList.add("rev-row--selected");
          revision.setAttribute("tabindex", "-1");
          revision.focus();
          revision.scrollIntoView({ block: "center" });
        }
      }
    }).catch(function (err) {
      if (request !== drawerRequest || !drawerOpen) return;
      var host = $("#drawer-detail");
      host.innerHTML = "";
      host.appendChild(statePanel("error", "详情读取失败", err.message, {
        retry: function () { openMemory(id); },
        actionLabel: null
      }));
      host.hidden = false;
    });
  }

  function renderMemoryDetail(m) {
    var id = mid(m);
    state.currentMemory = m;
    var st = mstatus(m);
    $("#drawer-id").textContent = id;
    var stEl = $("#drawer-status");
    stEl.textContent = st || "—";
    stEl.className = "chip " + (STATUS_CHIP[st] || "");
    stEl.hidden = false;

    var title = $("#drawer-title");
    title.textContent = mbody(m) || "（正文已清除）";

    var chips = $("#drawer-chips");
    chips.innerHTML = "";
    chips.appendChild(chip(mkind(m)));
    chips.appendChild(chip("v" + mrev(m), "chip--cool"));
    chips.appendChild(chip(sensOf(m), sensOf(m) === "private" ? "chip--bad" : ""));
    var userSide = misUser(m);
    chips.appendChild(chip(userSide ? "用户声明" : "Agent 派生", userSide ? "chip--amber" : "chip--cool"));
    /* trust_tier 是字符串 T1..T4：原样渲染，不加前缀、不做数字转换。 */
    var tier = tierOf(m);
    if (tier) chips.appendChild(chip(tier + " · " + TIER_ZH[tier], tierChipCls(tier)));
    if (st === "disputed") chips.appendChild(chip("冲突待裁", "chip--bad"));
    var conf = Number(m.confidence);
    if (isFinite(conf) && conf > 0 && conf <= 1 && !userSide) {
      chips.appendChild(chip("置信 " + conf.toFixed(2), "chip--muted"));
    }
    if (m.evidence_count !== undefined) chips.appendChild(chip("证据 " + intOf(m.evidence_count) + " 条", "chip--muted"));
    if (m.model_id) chips.appendChild(chip(String(m.model_id), "chip--muted"));

    $("#drawer-detail").hidden = false;
    var host = $("#drawer-detail");
    host.innerHTML = "";

    var secBody = h("div", "drawer-section");
    secBody.appendChild(h("h3", null, "当前正文"));
    secBody.appendChild(h("p", "drawer-text", mbody(m) || "（正文已清除）"));
    if (m.effective_at) {
      var factTime = h("p", "measured", "事实时间水位 ");
      factTime.appendChild(timeEl(m.effective_at));
      secBody.appendChild(factTime);
    }
    host.appendChild(secBody);

    var secCond = h("div", "drawer-section");
    secCond.appendChild(h("h3", null, "适用条件"));
    secCond.appendChild(h("p", "drawer-text", condText(m.conditions)));
    host.appendChild(secCond);

    /* 版本轨迹（revisions[].revision_no / body / trust_tier / reason / created_at） */
    var revs = Array.isArray(m.revisions) ? m.revisions.slice() : [];
    if (!revs.length && mbody(m)) revs = [{ revision_no: mrev(m), body: mbody(m), created_at: mtime(m), trust_tier: m.trust_tier, reason: "current" }];
    var secRevs = h("div", "drawer-section");
    secRevs.appendChild(h("h3", null, "版本轨迹"));
    var ol = h("ol", "trail trail--diff");
    if (!revs.length) ol.appendChild(h("li", null, "暂无版本记录"));
    revs.forEach(function (r) {
      r = r || {};
      var li = h("li", "rev-row");
      var rno = Number(r.revision_no);
      li.setAttribute("data-revision-no", String(rno));
      var isCurrent = isFinite(rno) && rno === mrev(m) && ["active", "proposed", "disputed", "quarantined"].indexOf(st) >= 0;
      var retired = ["superseded", "retracted", "deleted"].indexOf(String(r.status || st)) >= 0;
      if (isCurrent) li.classList.add("row--current");
      else if (retired) li.classList.add("row--retired");
      li.appendChild(h("span", "trail-ver", "v" + (isFinite(rno) ? rno : "?")));
      /* 版本来源由 trust_tier（字符串 T1..T4）判定，而不是猜字段。 */
      var rtier = tierOf(r) || tier;
      var who = rtier ? TIER_ZH[rtier] : (r.source_event_id ? "事件采入" : "系统");
      li.appendChild(h("span", isUserTier(rtier) ? "who--user" : "who--agent", who));
      li.appendChild(timeEl(r.created_at));
      if (r.effective_at) {
        var factTime = h("span", "measured", "事实时间 ");
        factTime.appendChild(timeEl(r.effective_at));
        li.appendChild(factTime);
      }
      if (isCurrent) li.appendChild(chip("当前", "chip--cool"));
      li.appendChild(h("p", null, String(r.body || "（正文已清除）")));
      if (r.reason) li.appendChild(h("p", null, "原因：" + String(r.reason)));
      if (r.model_id) li.appendChild(h("p", "measured", "决策者：" + String(r.model_id)));
      if (r.source_event_id) li.appendChild(eventLink(r.source_event_id));
      ol.appendChild(li);
    });
    secRevs.appendChild(ol);
    host.appendChild(secRevs);

    /* 证据来源（sources[].source_type / trust_tier / event_id / root_source_id / derived_from） */
    var sources = Array.isArray(m.sources) ? m.sources : [];
    var secSrc = h("div", "drawer-section");
    secSrc.appendChild(h("h3", null, "证据来源"));
    if (!sources.length) secSrc.appendChild(h("p", "drawer-text", "暂无证据记录。"));
    sources.forEach(function (s) {
      s = s || {};
      var block = h("div", "src-block");
      var head = h("div", "src-head");
      var stier = tierOf(s);
      var userS = String(s.source_type || "") === "user_declaration" || String(s.source_type || "") === "verification" || isUserTier(stier);
      head.appendChild(chip(String(s.source_type || "source"), userS ? "chip--amber" : "chip--cool"));
      if (stier) head.appendChild(chip(stier, tierChipCls(stier)));
      if (s.event_id) head.appendChild(eventLink(s.event_id));
      if (s.invalidated_at) head.appendChild(chip("历史来源已失效", "chip--muted"));
      head.appendChild(timeEl(s.created_at));
      block.appendChild(head);
      if (s.root_source_id) block.appendChild(h("span", "src-head measured", "证据根 " + truncate(String(s.root_source_id), 40)));
      var der = Array.isArray(s.derived_from) ? s.derived_from : [];
      der.forEach(function (d) {
        var row = h("div", "src-head");
        row.appendChild(document.createTextNode("派生自"));
        var b = h("button", "arb-link measured", String(d));
        b.type = "button";
        b.addEventListener("click", function () { openMemory(String(d)); });
        row.appendChild(b);
        block.appendChild(row);
      });
      secSrc.appendChild(block);
    });
    host.appendChild(secSrc);

    /* 仲裁决策（arbitration[].decision / reason / evidence_ids / confidence / model_id） */
    var decisions = Array.isArray(m.decisions) ? m.decisions : [];
    var secDec = h("div", "drawer-section");
    secDec.appendChild(h("h3", null, "仲裁决策"));
    var ul = h("ul", "arb-queue");
    if (!decisions.length) ul.appendChild(h("li", null, h("span", "arb-detail", "暂无决策记录。")));
    decisions.slice(0, 8).forEach(function (d) {
      d = d || {};
      var li = document.createElement("li");
      var head = h("div", "arb-head");
      var dec = String(d.decision || "—");
      head.appendChild(chip(dec, decisionChipCls(dec)));
      var dtier = tierOf(d);
      if (dtier) head.appendChild(chip(dtier, tierChipCls(dtier)));
      if (d.model_id) head.appendChild(h("span", "measured", String(d.model_id)));
      head.appendChild(timeEl(d.occurred_at || d.created_at));
      li.appendChild(head);
      li.appendChild(h("span", "arb-detail", truncate(d.reason || "（无原因记录）", 200)));
      if (Array.isArray(d.evidence_ids) && d.evidence_ids.length) {
        li.appendChild(h("span", "arb-detail", "证据：" + d.evidence_ids.map(function (x) { return truncate(String(x), 12); }).join("，")));
      }
      ul.appendChild(li);
    });
    secDec.appendChild(ul);
    host.appendChild(secDec);

    /* 派生链：父级来自 sources[].derived_from，子级来自 graph edges（source=本条, target=派生记忆） */
    var secDer = h("div", "drawer-section");
    secDer.appendChild(h("h3", null, "派生链"));
    var pDer = h("div", "drawer-text");
    pDer.appendChild(h("span", "derived-note", "纠正或删除本条会沿派生链传播。"));
    var host2 = h("div", "chip-row");
    pDer.appendChild(host2);
    secDer.appendChild(pDer);
    host.appendChild(secDer);
    fillDerived(id, host2, m);

    /* 操作 */
    $("#declare-form").hidden = true;
    var live = st !== "deleted" && st !== "retracted";
    $("#drawer-actions").hidden = !live;
    disarmForget();
    focusDrawerContent();
  }

  /* 详情加载完成后焦点移到关闭按钮（或第一个可聚焦控件），
   * 避免停留在刚消失的触发控件上；Escape / 关闭时由 closeDrawer 归还触发元素。 */
  function focusDrawerContent() {
    if (!drawerOpen) return;
    var f = focusables(drawer);
    var target = $("#drawer-close");
    if (!target || target.disabled) target = f.length ? f[0] : null;
    if (target) target.focus();
  }

  /* ================= 声明 / 遗忘 ================= */
  var declareMode = "new";

  function openDeclare(mode, memory) {
    var m = memory || state.currentMemory;
    declareMode = mode;
    lastFocus = document.activeElement;
    openDrawerShell();
    $("#drawer-detail").hidden = true;
    $("#drawer-actions").hidden = true;
    $("#declare-form").hidden = false;
    $("#declare-error").hidden = true;
    $("#declare-error").textContent = "";
    $("#declare-effective-at").value = "";
    var corr = $("#declare-correction-fields");
    if (mode === "correct" && m && mid(m)) {
      state.currentId = mid(m);
      $("#drawer-id").textContent = mid(m);
      $("#drawer-title").textContent = "声明修正：" + truncate(mbody(m), 60);
      $("#declare-heading").textContent = "对本条发起声明（系统将自主整合为新版本）";
      corr.hidden = false;
      $("#declare-memid").value = mid(m);
      $("#declare-rev").value = String(mrev(m));
      $("#declare-kind").value = mkind(m);
      $("#declare-factkey").value = String(m.fact_key || "");
      $("#declare-sensitivity").value = sensOf(m);
      if (m.conditions && asObj(m.conditions)) $("#declare-conditions").value = JSON.stringify(asObj(m.conditions));
    } else {
      corr.hidden = true;
      $("#declare-memid").value = "";
      $("#declare-rev").value = "";
      $("#drawer-id").textContent = "新声明";
      $("#drawer-title").textContent = "发起声明";
      $("#declare-heading").textContent = "发起声明（T1 高信任，系统自主整合）";
      $("#declare-kind").value = "fact";
      $("#declare-factkey").value = "";
      $("#declare-sensitivity").value = "normal";
      $("#declare-conditions").value = "";
    }
    $("#declare-text").value = "";
    $("#declare-text").focus();
  }

  $("#act-declare").addEventListener("click", function () {
    if (state.currentMemory) openDeclare("correct", state.currentMemory);
  });
  $("#declare-cancel").addEventListener("click", function () {
    $("#declare-form").hidden = true;
    if (state.currentMemory && state.currentId) renderMemoryDetail(state.currentMemory);
    else closeDrawer();
  });

  $("#declare-save").addEventListener("click", function () {
    var b = this;
    var err = $("#declare-error");
    err.hidden = true;
    var text = ($("#declare-text").value || "").trim();
    if (!text) { showDeclareError(err, "声明内容不能为空"); $("#declare-text").focus(); return; }
    var payload = {
      body: text,
      kind: $("#declare-kind").value,
      sensitivity: $("#declare-sensitivity").value,
      source_type: "user_declaration",
      harness: "manual"
    };
    var fk = ($("#declare-factkey").value || "").trim();
    if (fk) payload.fact_key = fk;
    var effective = $("#declare-effective-at").value;
    if (effective) payload.effective_at = new Date(effective).toISOString();
    var condRaw = ($("#declare-conditions").value || "").trim();
    if (condRaw) {
      var cond = asObj(condRaw);
      if (!cond || Array.isArray(cond)) { showDeclareError(err, "conditions 需为合法 JSON 对象，例如 {\"phase\":1}"); return; }
      payload.conditions = cond;
    }
    if (declareMode === "correct") {
      var memId = ($("#declare-memid").value || "").trim();
      if (!memId) { showDeclareError(err, "缺少 memory_id"); return; }
      var rev = parseInt($("#declare-rev").value, 10);
      if (!isFinite(rev) || rev < 1) { showDeclareError(err, "修正必须提供 expected_revision（整数 ≥1）"); return; }
      payload.memory_id = memId;
      payload.expected_revision = rev;
    }
    b.disabled = true;
    b.textContent = "提交中…";
    apiFetch("POST", EP.declare, { body: payload }).then(function (r) {
      r = r || {};
      /* POST /api/declarations → {memory, event_id, duplicate} */
      var mem = r.memory || {};
      var id = mid(mem) || r.memory_id || "";
      if (r.duplicate === true) {
        toast("重复声明已按事件去重归并" + (id ? "：" + id : ""));
      } else {
        toast("声明已立即整合" + (id ? "：" + id + " v" + mrev(mem) : "") + "，派生链随之传播");
      }
      if (id) {
        lastFocus = lastFocus || document.activeElement;
        openMemory(id);
      } else closeDrawer();
      /* 声明/纠正后：数字、列表、星图缓存全部刷新，不停留在旧快照。 */
      afterWrite();
    }).catch(function (e) {
      if (e.status === 409) showDeclareError(err, "冲突（409）：目标版本已被更新，请关闭后重新打开本条记忆再声明。");
      else if (e.status === 400 || e.status === 422) showDeclareError(err, "校验失败（" + e.status + "）：" + e.message);
      else showDeclareError(err, "提交失败：" + e.message);
    }).then(function () {
      b.disabled = false;
      b.textContent = "提交声明";
    });
  });

  function showDeclareError(errEl, msg) {
    errEl.hidden = false;
    errEl.textContent = msg;
  }

  function disarmForget() {
    forgetArmed = false;
    var b = $("#act-forget");
    b.textContent = "遗忘声明";
    b.classList.remove("btn--danger");
  }

  $("#act-forget").addEventListener("click", function () {
    var b = this;
    if (!state.currentId) return;
    if (!forgetArmed) {
      forgetArmed = true;
      b.textContent = "再次点击确认遗忘 " + state.currentId;
      b.classList.add("btn--danger");
      setTimeout(function () { if (forgetArmed) disarmForget(); }, 6000);
      return;
    }
    var id = state.currentId;
    b.disabled = true;
    apiFetch("POST", EP.forget(id)).then(function (r) {
      disarmForget();
      b.disabled = false;
      /* POST /api/memories/{id}/forget → {memory_id,status,deleted,tombstone,affected,jobs} */
      r = r || {};
      var extra = "";
      if (Array.isArray(r.affected) && r.affected.length) extra = "；沿派生链清理 " + r.affected.length + " 条";
      toast("遗忘声明已执行：" + (r.memory_id || id) + " 已 tombstone" + extra);
      closeDrawer();
      afterWrite();
    }).catch(function (e) {
      b.disabled = false;
      disarmForget();
      toast("遗忘失败：" + e.message, { bad: true });
    });
  });

  /* ================= 全局搜索 ================= */
  var globalSearch = $("#global-search");
  var searchPop = $("#search-pop");
  var searchSeq = 0;
  var searchItems = [];
  var searchActive = -1;

  globalSearch.addEventListener("input", debounce(function () {
    var q = globalSearch.value.trim();
    if (q.length < 1) { hideSearchPop(); return; }
    runGlobalSearch(q);
  }, 250));

  globalSearch.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (searchPop.hidden) runGlobalSearch(globalSearch.value.trim(), true);
      e.preventDefault();
      var opts = $all(".pop-item", searchPop);
      if (!opts.length) return;
      searchActive = (searchActive + (e.key === "ArrowDown" ? 1 : opts.length - 1)) % opts.length;
      opts.forEach(function (o, i) {
        o.classList.toggle("is-active", i === searchActive);
        o.setAttribute("aria-selected", String(i === searchActive));
      });
      globalSearch.setAttribute("aria-activedescendant", "search-opt-" + searchActive);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (searchActive >= 0 && searchItems[searchActive]) {
        var id = searchItems[searchActive].id;
        hideSearchPop(true);
        openMemory(id);
        return;
      }
      var q = globalSearch.value.trim();
      hideSearchPop(true);
      if (state.screen === "memories") {
        navigate("memories", function (p) { p.set("q", q); p.set("offset", "0"); });
      } else {
        location.hash = hashFor("memories", (function () { var p = new URLSearchParams(); if (q) p.set("q", q); return p; })());
      }
    } else if (e.key === "Escape" && !searchPop.hidden) {
      e.preventDefault();
      hideSearchPop(true);
    }
  });
  globalSearch.addEventListener("blur", function () {
    setTimeout(function () { if (!searchPop.contains(document.activeElement)) hideSearchPop(); }, 150);
  });

  function runGlobalSearch(q, immediate) {
    if (!q) { hideSearchPop(); return; }
    var seq = ++searchSeq;
    searchActive = -1;
    searchPop.hidden = false;
    globalSearch.setAttribute("aria-expanded", "true");
    searchPop.innerHTML = "";
    var note = h("p", "pop-note", immediate ? "正在搜索…" : "正在搜索…");
    searchPop.appendChild(note);
    apiFetch("GET", EP.search, { query: { q: q, limit: 8, target: "local" } }).then(function (data) {
      if (seq !== searchSeq) return;
      searchPop.innerHTML = "";
      searchItems = itemsOf(data).map(function (m) { return { id: mid(m), mem: m }; }).filter(function (x) { return x.id; });
      if (!searchItems.length) {
        searchPop.appendChild(h("p", "pop-note", "没有匹配的记忆。回车可在记忆库中按该关键词筛选。"));
        return;
      }
      searchItems.forEach(function (x, i) {
        var b = h("button", "pop-item");
        b.type = "button";
        b.id = "search-opt-" + i;
        b.setAttribute("role", "option");
        b.appendChild(h("span", "pop-title", truncate(mbody(x.mem) || x.id, 72)));
        var meta = h("span", "pop-meta");
        meta.appendChild(h("span", null, x.id));
        meta.appendChild(h("span", null, mkind(x.mem) + " v" + mrev(x.mem)));
        meta.appendChild(chip(mstatus(x.mem), STATUS_CHIP[mstatus(x.mem)]));
        /* search 命中包含 score 与 matched_by（fts / semantic / hybrid）。 */
        var sc = Number(x.mem.score);
        if (isFinite(sc) && sc !== 0) meta.appendChild(h("span", null, "score " + sc.toFixed(2)));
        if (x.mem.matched_by) meta.appendChild(h("span", null, "命中 " + String(x.mem.matched_by)));
        b.appendChild(meta);
        b.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
        b.addEventListener("click", function () { hideSearchPop(true); openMemory(x.id); });
        searchPop.appendChild(b);
      });
      var all = h("button", "pop-all", "在记忆库查看全部结果 →");
      all.type = "button";
      all.addEventListener("click", function () {
        hideSearchPop(true);
        location.hash = hashFor("memories", (function () { var p = new URLSearchParams(); p.set("q", q); return p; })());
      });
      searchPop.appendChild(all);
    }).catch(function (err) {
      if (seq !== searchSeq) return;
      searchPop.innerHTML = "";
      searchPop.appendChild(h("p", "pop-note", "搜索失败：" + err.message));
    });
  }

  function hideSearchPop(refocus) {
    searchPop.hidden = true;
    searchPop.innerHTML = "";
    globalSearch.setAttribute("aria-expanded", "false");
    globalSearch.removeAttribute("aria-activedescendant");
    searchItems = [];
    searchActive = -1;
    if (refocus) globalSearch.focus();
  }

  /* ================= 注入台账 ================= */
  function loadInjections() {
    /* 空台账不留在缓存里：注入发生后不能永远停在空态。 */
    if (state.inj.items && state.inj.items.length) { renderInjections(state.inj.items); return; }
    secShow("inj-list", "loading", { tableTbody: "#inj-tbody", cols: 6 });
    $("#inj-summary").textContent = "";
    apiFetch("GET", EP.injections).then(function (data) {
      var items = itemsOf(data);
      state.inj.items = items;
      renderInjections(items);
    }).catch(function (err) {
      $("#inj-tbody").innerHTML = "";
      secShow("inj-list", "error", {
        tableTbody: "#inj-tbody",
        title: "台账读取失败",
        message: "GET /api/injections：" + err.message,
        retry: function () { state.inj.items = null; loadInjections(); },
        doctor: true
      });
      $("#inj-summary").textContent = "";
    });
  }

  function renderInjections(all) {
    var harness = state.screen === "injections" ? (state.params.get("harness") || "all") : "all";
    var items = all.filter(function (x) { return harness === "all" || String(x.harness || "") === harness; });
    var tbody = $("#inj-tbody");
    tbody.innerHTML = "";
    if (!all.length) {
      secShow("inj-list", "empty", {
        tableTbody: "#inj-tbody",
        title: "还没有注入记录",
        message: "接入 Harness 后，每次自动注入都会在这里留痕，用于回答“为什么注入 / 没注入”。"
      });
      $("#inj-summary").textContent = "";
      return;
    }
    if (!items.length) {
      secShow("inj-list", "empty", {
        tableTbody: "#inj-tbody",
        title: "当前 Harness 无记录",
        message: "筛选 harness=" + harness + " 下没有注入；切换筛选查看其他来源。",
        actionLabel: "清除筛选", action: function () { navigate("injections", function (p) { p.set("harness", "all"); }); }
      });
      $("#inj-summary").textContent = "";
      return;
    }
    sections["inj-list"].innerHTML = "";
    items.forEach(function (inj) {
      var tr = document.createElement("tr");
      tr.setAttribute("data-harness", String(inj.harness || ""));
      var td0 = document.createElement("td");
      td0.classList.add("measured");
      td0.appendChild(timeEl(inj.created_at || inj.occurred_at || inj.time));
      tr.appendChild(td0);

      var td1 = document.createElement("td");
      td1.appendChild(document.createTextNode(HARNESS_ZH[inj.harness] || String(inj.harness || "—")));
      td1.appendChild(h("span", "mem-id", " " + truncate(String(inj.session_id || ""), 20) + (inj.turn_id ? " · t" + truncate(String(inj.turn_id), 10) : "")));
      tr.appendChild(td1);

      var td2 = h("td", "col-hide-sm", injTextCell(inj));
      tr.appendChild(td2);

      var td3 = document.createElement("td");
      var its = Array.isArray(inj.items) ? inj.items : (asObj(inj.items) || []);
      if (!Array.isArray(its)) its = [];
      var n = its.length;
      if (n === 0) {
        td3.appendChild(chip("0 条（去重或未变，不注入）", "chip--muted"));
      } else {
        var det = document.createElement("details");
        det.className = "trace";
        var sum = document.createElement("summary");
        sum.setAttribute("aria-label", "展开注入明细，共 " + n + " 条");
        sum.appendChild(chip(n + " 条，展开看明细"));
        det.appendChild(sum);
        var body = h("div", "trace-body");
        var row = h("div", "chip-row");
        its.forEach(function (it) {
          it = it || {};
          var idm = mid(it);
          var c = chip((idm || "?") + " v" + mrev(it), "chip--cool");
          if (idm) {
            c.style.cursor = "pointer";
            c.addEventListener("click", function () { openMemory(idm); });
            c.title = "打开 " + idm;
          }
          row.appendChild(c);
        });
        body.appendChild(row);
        body.appendChild(h("p", "panel-note", "台账命中：注入内容再次出现在会话历史时不再计为新的独立证据（防回灌）；去重命中见「去重」列。"));
        det.appendChild(body);
        td3.appendChild(det);
      }
      tr.appendChild(td3);

      tr.appendChild(h("td", "measured", fmtNum(intOf(inj.token_count))));
      var td5 = document.createElement("td");
      td5.classList.add("col-hide-sm");
      td5.appendChild(boolChip(inj.deduplicated, "命中", "未命中"));
      tr.appendChild(td5);
      tbody.appendChild(tr);
    });
    var sumTok = items.reduce(function (a, x) { return a + intOf(x.token_count); }, 0);
    var dedHits = items.filter(function (x) { return x.deduplicated === true; }).length;
    $("#inj-summary").textContent = "显示 " + items.length + " 条注入记录" + (harness !== "all" ? "（harness=" + harness + "）" : "") +
      " · tokens 合计 " + fmtNum(sumTok) + " · 去重命中 " + dedHits + " 条";
  }
  /* 注入台账列绑定：target（local/cloud）+ task + context_epoch。 */
  function injTextCell(inj) {
    var bits = [];
    if (inj.target) bits.push(String(inj.target));
    if (inj.task) bits.push("task=" + truncate(String(inj.task), 18));
    if (inj.context_epoch) bits.push("epoch=" + truncate(String(inj.context_epoch), 12));
    return bits.length ? bits.join(" · ") : "—";
  }
  function boolChip(v, yes, no) {
    if (v === true) return chip(yes, "chip--amber");
    if (v === false) return chip(no, "chip--muted");
    return chip("—", "chip--muted");
  }
  var HARNESS_ZH = { "claude-code": "Claude Code", codex: "Codex", dsh: "DSH", manual: "手动", mcp: "MCP" };

  /* ================= 历史导入 ================= */
  function loadImports() {
    if (state.imports.items && state.imports.items.length) { renderImports(state.imports.items); return; }
    secShow("imports-list", "loading", { tableTbody: "#imports-tbody", cols: 8 });
    apiFetch("GET", EP.imports).then(function (data) {
      var items = itemsOf(data);
      state.imports.items = items;
      renderImports(items);
    }).catch(function (err) {
      $("#imports-tbody").innerHTML = "";
      secShow("imports-list", "error", {
        tableTbody: "#imports-tbody",
        title: "导入清单读取失败",
        message: "GET /api/imports：" + err.message,
        retry: function () { state.imports.items = null; loadImports(); },
        doctor: true
      });
    });
  }

  function renderImports(items) {
    var tbody = $("#imports-tbody");
    tbody.innerHTML = "";
    if (!items.length) {
      secShow("imports-list", "empty", {
        tableTbody: "#imports-tbody",
        title: "还没有导入记录",
        message: "对已接入的 Harness 触发增量补扫，覆盖既有会话与记忆文件；先覆盖，后提炼。",
        actionLabel: "触发增量补扫",
        action: triggerImportScan
      });
      $("#imports-summary").textContent = "";
      return;
    }
    sections["imports-list"].innerHTML = "";
    var tD = 0, tI = 0, tS = 0;
    items.forEach(function (c) {
      c = c || {};
      var tr = document.createElement("tr");
      var discovered = Number(c.discovered || 0), imported = Number(c.imported || 0), skipped = Number(c.skipped || 0);
      tD += discovered; tI += imported; tS += skipped;
      var td0 = document.createElement("td");
      /* import_cursors 契约字段：source / path / parser_version / discovered / imported /
       * skipped / last_cursor / error / updated_at */
      td0.appendChild(h("span", null, String(c.source || "—")));
      if (c.path) td0.appendChild(h("span", "mem-id", " " + truncate(String(c.path), 44)));
      if (c.error) td0.appendChild(chip(truncate(String(c.error), 60), "chip--bad"));
      tr.appendChild(td0);
      tr.appendChild(h("td", "measured", fmtNum(discovered)));
      tr.appendChild(h("td", "measured", fmtNum(imported)));
      tr.appendChild(h("td", "col-hide-sm measured", fmtNum(skipped)));

      var td4 = document.createElement("td");
      var cov = discovered > 0 ? Math.min(100, Math.round((imported / discovered) * 100)) : 0;
      var wrap = h("div", "coverage");
      var track = h("div", "bar-track");
      var fill = h("div", "bar-fill" + (cov >= 100 ? " bar-fill--amber" : ""));
      fill.style.width = cov + "%";
      track.appendChild(fill);
      wrap.appendChild(track);
      wrap.appendChild(h("span", "mem-id", cov + "%"));
      td4.appendChild(wrap);
      tr.appendChild(td4);

      tr.appendChild(h("td", "col-hide-sm measured", truncate(String(c.last_cursor !== undefined ? c.last_cursor : "—"), 30)));
      tr.appendChild(h("td", "col-hide-sm measured", String(c.parser_version || "—")));
      var td7 = document.createElement("td");
      td7.classList.add("col-hide-sm", "measured");
      td7.appendChild(timeEl(c.updated_at));
      tr.appendChild(td7);
      tbody.appendChild(tr);
    });
    var pct = tD > 0 ? Math.round((tI / tD) * 100) : 0;
    $("#imports-summary").textContent = "总体覆盖率 " + pct + "%（已导入 " + fmtNum(tI) + " / 已发现 " + fmtNum(tD) + "，跳过 " + fmtNum(tS) + "）";
  }

  function triggerImportScan() {
    var b = $("#btn-import-scan");
    if (b.disabled) return;
    b.disabled = true;
    b.textContent = "补扫进行中…";
    apiFetch("POST", EP.imports, { body: { harness: "all" } }).then(function (r) {
      /* import_history 返回：{harness,config_root,parser_version,files,sources,discovered,imported,skipped,errors,…} */
      r = asDict(r);
      var bits = ["发现 " + fmtNum(intOf(r.discovered)), "导入 " + fmtNum(intOf(r.imported)),
        "跳过 " + fmtNum(intOf(r.skipped)), "错误 " + fmtNum(intOf(r.errors))];
      if (r.history_protocol) bits.push("协议 " + String(r.history_protocol));
      if (r.config_root) bits.push("配置根 " + truncate(String(r.config_root), 28));
      toast("增量补扫完成：" + bits.join(" · "));
      state.imports.items = null;
      afterWrite();
    }).catch(function (e) {
      toast("补扫失败：" + e.message, { bad: true });
    }).then(function () {
      b.disabled = false;
      b.textContent = "触发增量补扫";
    });
  }
  $("#btn-import-scan").addEventListener("click", triggerImportScan);

  /* ================= 来源 ================= */
  var SOURCE_LABELS = {
    harness: "Harness", status: "状态", installed: "已安装", version: "本地版本",
    config_root: "配置路径", paths: "配置文件", managed: "bmem 托管", capabilities: "能力",
    errors: "错误", notes: "说明", profiles: "DSH 配置档", url: "端点"
  };
  var DOCTOR_STATUS_ZH = { connected: "已接入", ready: "已接入", partial: "部分接入", unavailable: "未接入", missing: "未安装" };

  function loadSources() {
    secShow("sources", "loading", { layout: "lines" });
    connectorEnter("sources");
    apiFetch("GET", EP.sources).then(function (data) {
      var items = itemsOf(data);
      if (!items.length) {
        secShow("sources", "empty", {
          title: "尚未接入任何 Harness",
          message: "运行 bmem connect claude-code / codex / dsh 写入 hooks 与 MCP 配置；接入后这里显示 doctor 实测状态。",
          actionLabel: "查看历史导入", actionHref: "#/imports"
        });
        return;
      }
      var frag = [];
      items.forEach(function (it) { frag.push(sourcePanel(it || {})); });
      secShow("sources", "full", { node: fullNode(frag) });
    }).catch(function (err) {
      secShow("sources", "error", { title: "来源状态读取失败", message: "GET /api/sources：" + err.message, retry: loadSources, doctor: true });
    });
  }

  function sourcePanel(it) {
    var p = h("div", "panel source-panel");
    var head = h("h2");
    head.appendChild(h("span", null, HARNESS_ZH[it.harness] || String(it.harness || it.name || "来源")));
    /* doctor 结果用 status：connected / partial / unavailable（不再有 connected:true 之类字段）。 */
    var status = String(it.status || "");
    if (status) head.appendChild(chip(DOCTOR_STATUS_ZH[status] || status, doctorStatusCls(status)));
    p.appendChild(head);
    var dl = h("dl", "kv");
    Object.keys(it).forEach(function (k) {
      if (k === "harness" || k === "name") return;
      var v = it[k];
      if (v === null || v === undefined || v === "" ) return;
      if (Array.isArray(v) && !v.length) return;
      dl.appendChild(h("dt", null, SOURCE_LABELS[k] || k));
      var dd = document.createElement("dd");
      if (typeof v === "boolean") dd.appendChild(chip(v ? "是" : "否", v ? "chip--ok" : "chip--muted"));
      else if (Array.isArray(v)) {
        v.slice(0, 6).forEach(function (x) { dd.appendChild(chip(truncate(typeof x === "object" ? JSON.stringify(x) : String(x), 60), (k === "errors" ? "chip--bad" : ""))); });
      } else if (typeof v === "object") {
        Object.keys(v).forEach(function (sk) {
          var sv = v[sk];
          var text = (typeof sv === "object") ? JSON.stringify(sv) : String(sv);
          dd.appendChild(chip(sk + "=" + truncate(text, 48), k === "errors" ? "chip--bad" : (typeof sv === "boolean" ? (sv ? "chip--ok" : "chip--muted") : "chip--muted")));
        });
      } else if (/_at$/.test(k)) dd.appendChild(timeEl(v));
      else dd.appendChild(h("span", "measured", truncate(String(v), 140)));
      dl.appendChild(dd);
    });
    p.appendChild(dl);
    var note = h("p", "panel-note");
    var a = h("a", null, "该来源的记忆视图");
    a.href = "#/memories?harness=" + encodeURIComponent(String(it.harness || ""));
    note.appendChild(a);
    p.appendChild(note);
    return p;
  }
  function doctorStatusCls(status) {
    if (status === "connected" || status === "ready") return "chip--ok";
    if (status === "partial") return "chip--amber";
    return "chip--bad";
  }

  /* ================= 设置：语义开关 / 安装轮询 / 备份 ================= */
  var semanticToggle = $("#semantic-toggle");
  var semanticBusy = false;

  function loadSettings() {
    apiFetch("GET", EP.semantic).then(function (s) { applySettings(s || {}); })
      .catch(function (e) {
        var st = $("#semantic-state");
        st.textContent = "读取失败";
        st.className = "chip chip--bad";
        toast("语义开关读取失败：" + e.message, { bad: true });
      });
    refreshRuntime();
    connectorEnter("settings");
  }

  function applySettings(s) {
    s = s || {};
    /* settings 契约：{semantic_enabled: bool, ...}（PUT 返回同结构 + queued_jobs）。 */
    var enabled = !!s.semantic_enabled;
    if (!semanticBusy) semanticToggle.checked = enabled;
    var st = $("#semantic-state");
    st.textContent = enabled ? "已启用" : "已关闭";
    st.className = "chip " + (enabled ? "chip--ok" : "chip--muted");
    setTopSemantic(enabled);
  }

  semanticToggle.addEventListener("change", function () {
    var want = semanticToggle.checked;
    semanticBusy = true;
    var st = $("#semantic-state");
    st.textContent = "写入中…";
    st.className = "chip chip--muted";
    apiFetch("PUT", EP.semantic, { body: { enabled: want } }).then(function (s) {
      semanticBusy = false;
      s = s || {};
      applySettings(s);
      var queued = s.queued_jobs;
      $("#semantic-backfill").textContent = (s.semantic_enabled ? "已开启：向量参与召回" : "已关闭：只走事实 + 关键词检索，向量数据保留。")
        + (queued !== undefined ? " 本次为存量记忆排队 " + intOf(queued) + " 个回填任务。" : "");
      toast(want ? "语义检索已开启（即时生效）" : "语义检索已关闭（向量保留，未删除）");
      afterWrite();
    }).catch(function (e) {
      semanticBusy = false;
      semanticToggle.checked = !want;
      st.textContent = "切换失败";
      st.className = "chip chip--bad";
      toast("设置写入失败：" + e.message, { bad: true });
    });
  });

  /* Runtime.status() 的真实安装阶段（bmem/runtime.py 写入 state 文件的取值）。 */
  var INSTALL_ACTIVE = ["detecting", "downloading", "extracting", "starting", "installing", "verifying"];
  var INSTALL_ZH = {
    idle: "未开始", detecting: "检测本机 Ollama", downloading: "下载 Ollama", extracting: "校验并解压",
    starting: "启动本地服务", installing: "拉取模型", verifying: "校验模型与摘要",
    ready: "已就绪", error: "安装失败", failed: "安装失败"
  };
  function runtimeInstall(rt) {
    /* install: {state, progress(0..1), stage, error, updated_at} */
    return asDict(rt && rt.install);
  }
  function installState(inst) { return String(asDict(inst).state || "idle").toLowerCase(); }
  function installActive(inst) {
    var s = installState(inst);
    return INSTALL_ACTIVE.some(function (k) { return s === k; });
  }
  function installStageText(inst) {
    var s = installState(inst);
    var zh = INSTALL_ZH[s] || s;
    var stage = String(asDict(inst).stage || "");
    return stage && stage !== s ? zh + "（" + truncate(stage, 60) + "）" : zh;
  }

  var pollTimer = null, pollFails = 0;
  function startRuntimePoll() {
    if (pollTimer) return;
    pollRuntimeOnce();
    pollTimer = setInterval(pollRuntimeOnce, 1500);
  }
  function stopRuntimePoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function pollRuntimeOnce() {
    apiFetch("GET", EP.runtime).then(function (rt) {
      pollFails = 0;
      state.runtime = rt;
      renderRuntime(rt);
      if (!installActive(runtimeInstall(rt))) stopRuntimePoll();
    }).catch(function () {
      pollFails++;
      if (pollFails > 8) {
        stopRuntimePoll();
        $("#install-status").textContent = "运行时状态读取失败，停止轮询；点击安装可重试。";
      }
    });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopRuntimePoll();
    else if (state.screen === "settings" && state.runtime && installActive(runtimeInstall(state.runtime))) startRuntimePoll();
  });

  function refreshRuntime() {
    apiFetch("GET", EP.runtime).then(function (rt) {
      state.runtime = rt;
      renderRuntime(rt);
      if (installActive(runtimeInstall(rt))) startRuntimePoll();
    }).catch(function (e) {
      var dl = $("#runtime-info");
      dl.innerHTML = "";
      dl.appendChild(h("dt", null, "状态"));
      var dd = document.createElement("dd");
      dd.appendChild(chip("读取失败", "chip--bad"));
      dd.appendChild(h("span", null, e.message));
      dl.appendChild(dd);
      $("#endpoint-tbody").innerHTML = "";
      var tr = document.createElement("tr");
      var td = h("td", null, "运行时状态不可用：" + e.message + "（采集与关键词检索不受影响）");
      td.colSpan = 4;
      tr.appendChild(td);
      $("#endpoint-tbody").appendChild(tr);
    });
  }

  function renderRuntime(rt) {
    rt = rt || {};
    var inst = runtimeInstall(rt);
    var iState = installState(inst);
    var ready = iState === "ready";
    var failed = iState === "error" || iState === "failed";
    var active = installActive(inst);

    var dl = $("#runtime-info");
    dl.innerHTML = "";
    function row(label, node) {
      dl.appendChild(h("dt", null, label));
      var dd = document.createElement("dd");
      if (node && node.nodeType) dd.appendChild(node);
      else dd.appendChild(h("span", "measured", String(node == null ? "—" : node)));
      dl.appendChild(dd);
    }
    /* available / version / endpoint 都是 Runtime.status() 的实际字段。 */
    var av = rt.available === true ? chip("可用", "chip--ok") : (rt.available === false ? chip("不可用", "chip--bad") : chip("未知", "chip--muted"));
    row("运行时", av);
    row("版本", rt.version ? String(rt.version) : "—");
    if (rt.pinned_version) {
      var match = rt.version_matches_pinned === true ? chip("与锁定版本一致", "chip--ok") : chip("与锁定版本 " + String(rt.pinned_version) + " 不一致", "chip--amber");
      row("锁定版本", match);
    }
    if (rt.endpoint || rt.local_endpoint) row("端点", String(rt.endpoint || rt.local_endpoint));
    if (rt.error) row("错误", truncate(String(rt.error), 200));
    if (rt.install_stage || inst.stage) row("当前阶段", installStageText(inst));
    row("安装状态", chip(INSTALL_ZH[iState] || iState, ready ? "chip--ok" : failed ? "chip--bad" : active ? "chip--cool" : "chip--muted"));

    $("#embed-runtime").textContent = rt.available === true ? ("Ollama " + (rt.version ? rt.version : "可用")) : (rt.available === false ? "运行时不可用" : "状态未知");
    $("#embed-endpoint").textContent = String(rt.endpoint || rt.local_endpoint || "—");
    var models = asDict(rt.models);
    var embKey = String(rt.embedding_model || "bge-m3");
    var emb = asDict(models[embKey]);
    $("#embed-digest").textContent = emb.digest ? String(emb.digest) : "未安装（期望 " + truncate(String(emb.expected_digest || "—"), 22) + "）";

    var errEl = $("#install-error");
    if (failed && !active) {
      errEl.hidden = false;
      $("#install-error-msg").textContent = inst.error ? String(inst.error) : "安装失败（Runtime.status() 未给出 error）";
    } else errEl.hidden = true;

    var fill = $("#install-fill"), status = $("#install-status"), prog = $("#install-progress");
    var btn = $("#embed-install");
    var pr = Number(inst.progress);
    var frac = isFinite(pr) ? clamp(pr, 0, 1) : 0;
    if (active) {
      btn.disabled = true;
      btn.textContent = "安装进行中…";
      status.textContent = "阶段：" + installStageText(inst) + " · " + Math.round(frac * 100) + "%";
      fill.style.transform = "scaleX(" + frac + ")";
      prog.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
      startRuntimePoll();
    } else {
      btn.disabled = false;
      btn.textContent = ready ? "重新安装 / 修复" : "一键安装 / 修复";
      if (ready) {
        fill.style.transform = "scaleX(1)";
        prog.setAttribute("aria-valuenow", "100");
        status.textContent = "bge-m3 已就绪" + (inst.updated_at ? "（" + fmtTime(inst.updated_at) + "）" : "");
      } else if (failed) {
        fill.style.transform = "scaleX(" + frac + ")";
        prog.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
        status.textContent = "安装未完成（" + Math.round(frac * 100) + "%），可重试。";
      } else if (iState === "idle") {
        fill.style.transform = "scaleX(0)";
        prog.setAttribute("aria-valuenow", "0");
        status.textContent = "尚未安装本地语义模型。";
      } else {
        status.textContent = "状态：" + (INSTALL_ZH[iState] || iState);
      }
    }

    var etbody = $("#endpoint-tbody");
    etbody.innerHTML = "";
    var keys = Object.keys(models);
    if (!keys.length) {
      var tr0 = document.createElement("tr");
      var td0 = h("td", null, "运行时状态未返回 models（本地模型不可用）");
      td0.colSpan = 4;
      tr0.appendChild(td0);
      etbody.appendChild(tr0);
    }
    keys.forEach(function (k) {
      var mo = asDict(models[k]);
      var name = String(mo.name || k);
      /* model 条目字段：present / state("ready"|"missing") / location / purpose / digest */
      var stName = String(mo.state || (mo.present === true ? "ready" : "missing"));
      var tr = document.createElement("tr");
      tr.appendChild(h("td", "measured", name + (k === embKey ? "（Embedding）" : "")));
      tr.appendChild(h("td", null, String(mo.purpose || (name.indexOf("bge") >= 0 ? "Embedding（唯一）" : name.indexOf("qwen") >= 0 ? "蒸馏 + 自主仲裁" : "本地模型"))));
      tr.appendChild(h("td", "col-hide-sm", String(mo.location || "local")));
      var td4 = document.createElement("td");
      td4.appendChild(chip(stName, stName === "ready" ? "chip--ok" : "chip--bad"));
      tr.appendChild(td4);
      etbody.appendChild(tr);
    });
  }

  $("#embed-install").addEventListener("click", requestEmbedInstall);
  $("#install-retry").addEventListener("click", requestEmbedInstall);
  function requestEmbedInstall() {
    var btn = $("#embed-install");
    btn.disabled = true;
    btn.textContent = "正在启动…";
    $("#install-status").textContent = "后台任务启动中…";
    $("#install-error").hidden = true;
    apiFetch("POST", EP.embedInstall).then(function (r) {
      toast(r && r.started === false ? "安装未启动：" + String(r.reason || r.message || "见运行时状态") : "后台安装已开始：运行时检测 → 下载 bge-m3 → 校验 → 回填");
      startRuntimePoll();
    }).catch(function (e) {
      if (e.status === 409) {
        toast("已有安装任务在运行，改为跟踪进度", { bad: false });
        startRuntimePoll();
      } else {
        btn.disabled = false;
        btn.textContent = "一键安装 / 修复";
        $("#install-error").hidden = false;
        $("#install-error-msg").textContent = "无法启动安装：" + e.message;
      }
    });
  }

  $("#btn-backup").addEventListener("click", function () {
    var b = this;
    if (b.disabled) return;
    b.disabled = true;
    b.textContent = "备份中…";
    apiFetch("POST", EP.backup).then(function (r) {
      /* POST /api/backup → {path} */
      var path = asDict(r).path || "";
      if (path) {
        state.lastBackup = String(path);
        $("#backup-result").textContent = String(path) + " · " + fmtTime(new Date().toISOString());
        var inp = $("#restore-path");
        if (inp && !inp.value) inp.value = String(path);
        toast("备份完成：" + truncate(String(path), 90));
      } else toast("备份请求已返回，但未给出路径");
    }).catch(function (e) {
      toast("备份失败：" + e.message, { bad: true });
    }).then(function () {
      b.disabled = false;
      b.textContent = "立即备份";
    });
  });

  var restoreArmed = false;
  $("#btn-restore").addEventListener("click", function () {
    var b = this;
    var path = ($("#restore-path").value || "").trim();
    if (!path) { toast("请先输入备份归档的绝对路径", { bad: true }); return; }
    if (!restoreArmed) {
      restoreArmed = true;
      b.textContent = "再次点击确认恢复";
      b.classList.add("btn--danger");
      setTimeout(function () {
        restoreArmed = false;
        b.textContent = "恢复";
        b.classList.remove("btn--danger");
      }, 6000);
      return;
    }
    restoreArmed = false;
    b.textContent = "恢复";
    b.classList.remove("btn--danger");
    b.disabled = true;
    apiFetch("POST", EP.restore, { body: { path: path } }).then(function (r) {
      /* restore 返回：{restored:true, tombstones_replayed:int, …} */
      r = asDict(r);
      var extra = r.tombstones_replayed !== undefined ? "，重放删除日志 " + fmtNum(intOf(r.tombstones_replayed)) + " 条" : "";
      toast("恢复完成" + extra + "；界面数据已刷新");
      closeDrawer();
      afterWrite();
    }).catch(function (e) {
      toast("恢复失败：" + e.message, { bad: true });
    }).then(function () {
      b.disabled = false;
    });
  });

  $("#btn-rebuild").addEventListener("click", function () {
    var b = this;
    b.disabled = true;
    apiFetch("POST", EP.rebuild).then(function (r) {
      /* rebuild_indexes 返回：{rebuilt:true, memories_indexed:int, fts_rows:int} */
      r = asDict(r);
      var t = r.rebuilt === true ? ("已重建，FTS 行数 " + fmtNum(intOf(r.fts_rows)) + "，索引记忆 " + fmtNum(intOf(r.memories_indexed)) + " 条") : "服务端未返回 rebuilt";
      toast("索引重建：" + t);
      state.graph.data = null;
    }).catch(function (e) {
      toast("重建失败：" + e.message, { bad: true });
    }).then(function () {
      b.disabled = false;
    });
  });

  /* ================= 健康 / 顶栏 ================= */
  function setTopSemantic(enabled) {
    var c = $("#top-semantic");
    c.textContent = enabled ? "语义检索开" : "语义检索关";
    c.className = "chip " + (enabled ? "chip--cool" : "chip--muted");
  }

  function pollHealth() {
    /* 契约：GET /api/health → {service:"bmem", version, schema_version, worker:{running,…}}。
     * 在线状态来自「health 请求成功」本身；service 是产品标识，不是 ok/true。 */
    apiFetch("GET", EP.health).then(function (hlt) {
      hlt = hlt || {};
      var svcName = String(hlt.service || "bmem");
      var ver = hlt.version ? " v" + String(hlt.version) : "";
      var svc = $("#service-state");
      svc.textContent = svcName + ver + " 运行中";
      svc.className = "chip chip--ok";
      var wk = asDict(hlt.worker);
      var wOk = wk.running === true;
      var wc = $("#top-worker");
      wc.textContent = wOk ? "Worker 运行中" : "Worker 已停止";
      wc.className = "chip " + (wOk ? "chip--cool" : "chip--muted");
      $("#foot-service").textContent = svcName + ver;
      $("#foot-schema").textContent = hlt.schema_version !== undefined ? "SQLite s" + fmtNum(hlt.schema_version) : "SQLite";
      $("#foot-origin").textContent = location.host || "127.0.0.1";
      if (hlt.semantic_enabled !== undefined) setTopSemantic(!!hlt.semantic_enabled);
    }).catch(function (err) {
      var svc = $("#service-state");
      svc.textContent = "bmemd 离线";
      svc.className = "chip chip--bad";
      svc.title = err.message;
      $("#top-worker").textContent = "Worker —";
      $("#top-worker").className = "chip chip--muted";
      $("#foot-service").textContent = "离线";
      $("#foot-origin").textContent = location.host || "127.0.0.1";
    });
  }

  function invalidateCaches() {
    state.graph.data = null;
    state.inj.items = null;
    state.imports.items = null;
    state.memories.items = [];
  }

  /* ================= 连接器：飞书 / Lark（契约 specs/002-feishu-memory/contracts/interfaces.md） ================= */
  var CONN_BRANDS = ["feishu", "lark"];
  var CONN_NAME = { feishu: "飞书", lark: "Lark" };
  var CONN_PHASE = {
    unconfigured: "未配置", installing: "安装 CLI", configuring: "准备认证", authorizing: "等待授权",
    connected: "已连接", syncing: "读取中", error: "出错"
  };
  var CONN_ACTIVE = { installing: 1, configuring: 1, authorizing: 1, syncing: 1 };
  var CONN_SOURCES = [
    { key: "private_chats", label: "私聊" },
    { key: "group_chats", label: "群聊" },
    { key: "documents", label: "文档" }
  ];
  var CONN_OFFICIAL = ["feishu.cn", "larksuite.com"];
  var connSel = { brand: "feishu", draft: null };

  function connBrand(v) { return CONN_BRANDS.indexOf(String(v || "")) >= 0 ? String(v) : "feishu"; }
  function connName(b) { return CONN_NAME[b] || CONN_NAME.feishu; }
  function connData(d) { return (d && d.brand === connSel.brand) ? d : null; }
  function connPhase(d) { return d ? String(d.phase || "unconfigured") : ""; }
  function connError(error) { return error && error.message ? String(error.message) : String(error || ""); }
  function connActive(d) { return !!(d && (d.busy === true || CONN_ACTIVE[String(d.phase || "")])); }
  function connLocked(d) { return CB.sending || connActive(d); }
  function connLabel(key) {
    var o = CONN_SOURCES.filter(function (x) { return x.key === key; })[0];
    return o ? o.label : "";
  }

  /* 授权链接：仅 https、无凭据、主机为官方域名（feishu.cn / *.feishu.cn / larksuite.com / *.larksuite.com）。 */
  function connSafeUrl(raw) {
    if (typeof raw !== "string" || !raw || raw.length > 4096) return null;
    var u;
    try { u = new URL(raw, location.origin); } catch (e) { return null; }
    if (u.protocol !== "https:" || u.username || u.password) return null;
    var host = u.hostname.toLowerCase();
    var ok = CONN_OFFICIAL.some(function (d) { return host === d || host.slice(-(d.length + 1)) === "." + d; });
    return ok ? raw : null;
  }

  function connSourcesOf(d) {
    var hs = asDict(d && d.history);
    return Array.isArray(hs.sources) ? hs.sources.filter(function (s) { return connLabel(s); }) : [];
  }
  function connSensitivity(d) { return String(asDict(d && d.history).sensitivity || "private") === "normal" ? "normal" : "private"; }
  /* 表单选择：用户草稿优先；首次未确认三项全不勾选；已确认回显已存配置。 */
  function connSelection(d) {
    if (connSel.draft && connSel.draft.brand === connSel.brand) return { sources: connSel.draft.sources.slice(), sensitivity: connSel.draft.sensitivity };
    if (!d || !asDict(d.history).confirmed) return { sources: [], sensitivity: "private" };
    return { sources: connSourcesOf(d), sensitivity: connSensitivity(d) };
  }
  function connSame(sel, saved) {
    return sel.sensitivity === saved.sensitivity && sel.sources.join(",") === saved.sources.join(",");
  }
  function connSaved(d) {
    return (d && asDict(d.history).confirmed) ? { sources: connSourcesOf(d), sensitivity: connSensitivity(d) } : { sources: [], sensitivity: "private" };
  }
  function connSourceZh(list) { return list.length ? list.map(connLabel).join(" · ") : "暂不读取"; }

  /* ---- 加载 / 独立轮询：仅 sources/settings 且页面可见 ---- */
  var CB = { data: null, err: null, sending: false, qr: { url: "", img: null, imgSrc: "", imgUrl: "" }, poll: { timer: null, fails: 0 } };

  function connActivePage() { return state.screen === "sources" || state.screen === "settings"; }
  /* 每个屏幕加载时调用一次：对齐品牌（路由参数可覆盖），再取一次状态。 */
  function connectorEnter() {
    connSyncBrand();
  }
  function connectorRefresh() { connectorGet(); }
  /* 品牌来自路由 connector=feishu|lark（有效才采用，否则保持当前）；换品牌即停旧轮询、清空草稿与旧状态，之后仅接受新品牌响应。 */
  function connSyncBrand() {
    if (!connActivePage()) { connectorStop(); return; }
    var route = connBrand(state.params.get("connector"));
    var b = CONN_BRANDS.indexOf(String(state.params.get("connector") || "")) >= 0 ? route : connSel.brand;
    if (b !== connSel.brand) {
      connectorStopPoll();
      connSel.brand = b;
      connSel.draft = null;
      CB.data = null;
      CB.err = null;
      CB.sending = false;
      CB.qr = { url: "", img: null, imgSrc: "", imgUrl: "" };
    }
    var sel = $("#connector-brand");
    if (sel) sel.value = connSel.brand;
    connectorRefresh();   // 进入/返回页面或换品牌：立即刷新，进行中则恢复轮询
  }
  function connectorApply(data) {
    var previous = CB.data && CB.data.sync;
    CB.data = asDict(data);
    var current = CB.data.sync || {};
    if (current.last_success_at && (!previous || current.last_success_at !== previous.last_success_at)) {
      invalidateCaches();
      refreshSidebarStats();
    }
  }
  function connectorGet() {
    var brand = connSel.brand;
    apiFetch("GET", EP.connector(brand)).then(function (d) {
      if (brand !== connSel.brand) return;                 // 丢弃上一品牌响应
      CB.poll.fails = 0;
      connectorApply(d);
      CB.err = null;
      connectorRender();
      if (connActive(CB.data) && document.hidden !== true) connectorStartPoll();
      else connectorStopPoll();
    }).catch(function (e) {
      if (brand !== connSel.brand) return;
      CB.err = e;
      CB.poll.fails++;
      if (CB.poll.fails > 6) connectorStopPoll();
      connectorRender();
    });
  }
  function connPollOnce() {
    if (document.hidden || !connActivePage() || CB.sending) return;
    var brand = connSel.brand;
    apiFetch("GET", EP.connector(brand)).then(function (d) {
      if (brand !== connSel.brand) return;
      CB.poll.fails = 0;
      connectorApply(d);
      CB.err = null;
      connectorRender();
      if (!connActive(CB.data)) connectorStopPoll();
    }).catch(function () {
      if (brand !== connSel.brand) return;
      CB.poll.fails++;
      if (CB.poll.fails > 6) { connectorStopPoll(); CB.err = ApiError(0, "连接状态读取失败，已暂停轮询，点「重试读取」恢复。"); connectorRender(); }
    });
  }
  function connectorStartPoll() {
    if (CB.poll.timer || document.hidden || !connActivePage()) return;
    CB.poll.fails = 0;
    CB.poll.timer = setInterval(connPollOnce, 2000);
  }
  /* 操作刚返回时后端会短暂标记 busy：稍后确认一次再进行状态轮询。 */
  function connectorPeekPoll() {
    connectorStopPoll();
    setTimeout(function () { if (connActivePage() && !CB.poll.timer) connectorStartPoll(); }, 600);
  }
  function connectorStopPoll() { if (CB.poll.timer) { clearInterval(CB.poll.timer); CB.poll.timer = null; } }
  function connectorStop() { connectorStopPoll(); }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) connectorStopPoll();
    else if (connActivePage()) connectorRefresh();
  });

  /* ---- 变更：本地发送中或后端 busy 时拒绝重复提交 ---- */
  function connMutate(ep, method, body, okMsg, onOk) {
    if (connLocked(connData(CB.data))) return;
    var brand = connSel.brand;
    CB.sending = true;
    CB.err = null;
    connectorRender();
    apiFetch(method, ep, body === undefined ? {} : { body: body }).then(function (d) {
      if (brand !== connSel.brand) return;
      connectorApply(d);
      CB.err = null;
      if (onOk) onOk();   // 成功确认后、渲染前
      if (okMsg) toast(d && d.error ? okMsg + "；错误：" + truncate(connError(d.error), 90) : okMsg, { bad: !!(d && d.error) });
      connectorRender();
      if (connActive(CB.data)) connectorStartPoll(); else connectorStopPoll();
    }).catch(function (e) {
      if (brand !== connSel.brand) return;
      CB.err = e;
      toast("请求未完成：" + e.message, { bad: true });
      connectorRender();
      if (e.status === 409) { connectorGet(); connectorPeekPoll(); }   // 已有操作在运行：改为跟踪当前操作
    }).then(function () {
      if (brand !== connSel.brand) return;
      CB.sending = false;
      connectorRender();
    });
  }
  function connSaveSelection() {
    var sel = connSelection(connData(CB.data));
    var brand = connSel.brand;
    connMutate(EP.connectorConfig(brand), "PUT",
      { sources: sel.sources.slice(), enabled: sel.sources.length > 0, sensitivity: sel.sensitivity },
      "读取范围已保存：" + connSourceZh(sel.sources), function () {
        if (connSel.draft && connSel.draft.brand === brand) connSel.draft = null;   // 仅 PUT 成功后清除草稿
      });
  }
  function connConnect() {
    var sel = connSelection(connData(CB.data));
    /* 新连接：先保存范围与敏感级别（PUT config），再以显式 sources 发起 connect。 */
    var brand = connSel.brand;
    if (connLocked(connData(CB.data))) return;
    CB.sending = true;
    CB.err = null;
    connectorRender();
    apiFetch("PUT", EP.connectorConfig(brand), { body: { sources: sel.sources.slice(), enabled: sel.sources.length > 0, sensitivity: sel.sensitivity } })
      .then(function (d) {
        if (brand !== connSel.brand) return null;
        connectorApply(d);
        return apiFetch("POST", EP.connectorConnect(brand), { body: { sources: sel.sources.slice() } });
      }).then(function (d) {
        if (!d || brand !== connSel.brand) return;
        connectorApply(d);
        connSel.draft = null;
        CB.err = null;
        toast(d.error ? "连接未成功：" + truncate(connError(d.error), 120) : "已发起连接：" + connSourceZh(sel.sources), { bad: !!d.error });
        connectorRender();
        connectorStartPoll();
      }).catch(function (e) {
        if (brand !== connSel.brand) return;
        CB.err = e;
        toast("连接失败：" + e.message, { bad: true });
        connectorRender();
      }).then(function () {
        if (brand !== connSel.brand) return;
        CB.sending = false;
        connectorRender();
      });
  }
  function connSyncNow(sel) {
    if (!sel.sources.length) {
      toast("暂未选择读取范围：请先勾选私聊 / 群聊 / 文档并保存", { bad: true });
      return;
    }
    connMutate(EP.connectorSync(connSel.brand), "POST", undefined, "已开始读取所选范围");
  }

  /* ---- 小部件 ---- */
  function connKv(label, node) {
    var pairs = [];
    pairs.push(h("dt", null, label));
    var dd = document.createElement("dd");
    if (node && node.nodeType) dd.appendChild(node);
    else dd.appendChild(h("span", "measured", String(node === undefined || node === null ? "—" : node)));
    pairs.push(dd);
    return pairs;
  }
  function connAppendKv(dl, label, node) { connKv(label, node).forEach(function (n) { dl.appendChild(n); }); }

  /* 授权 URL（真实链接，新标签）与同源二维码；二维码以挑战 URL 缓存，仅当前挑战显示，二维码生成中时，后续状态轮询继续尝试加载。 */
  function connAuthBlock(d, active) {
    var wrap = h("div", "connector-part");
    var auth = asDict(d && d.authorization);
    var raw = auth.url;
    if (raw !== CB.qr.url) CB.qr = { url: raw, img: null, imgSrc: "", imgUrl: "" };   // 新挑战重新加载二维码
    var safe = connSafeUrl(raw);
    if (safe) {
      var row = h("div", "connector-actions");
      var a = h("a", "btn btn--primary", "打开授权页面");
      a.href = safe;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      row.appendChild(a);
      if (auth.expires_at) row.appendChild(h("span", "connector-msg", "有效期至 " + fmtTime(auth.expires_at)));
      wrap.appendChild(row);
    }
    var qrUrl = EP.connectorQr(connBrand(d && d.brand));
    if (active && raw && typeof raw === "string") {
      var box = h("div", "connector-qr");
      /* 同一挑战内轮询复用已加载的图片节点，不新建、不重设 src；新挑战才重新取同源端点。 */
      if (CB.qr.img && CB.qr.imgSrc && CB.qr.imgUrl === qrUrl) {
        box.appendChild(CB.qr.img);
      } else {
        var img = h("img");
        img.alt = connName(connSel.brand) + " 授权二维码";
        img.width = 200;
        img.height = 200;
        img.id = "conn-qr-img";
        img.addEventListener("load", function () { CB.qr.img = img; CB.qr.imgSrc = img.src; CB.qr.imgUrl = qrUrl; });
        img.addEventListener("error", function () { CB.qr.img = null; CB.qr.imgSrc = ""; box.remove(); });   // 图片尚未就绪时隐藏，下一次状态轮询再加载
        img.src = qrUrl;                           // 固定同源端点，从不使用 DTO 的 URL
        box.appendChild(img);
      }
      box.appendChild(h("span", "connector-msg", (safe ? "或用手机" : "") + connName(connSel.brand) + "扫码授权" + (auth.expires_at ? "，有效期至 " + fmtTime(auth.expires_at) : "")));
      wrap.appendChild(box);
    }
    if (!wrap.firstChild) return null;
    return wrap;
  }

  /* ---- 来源页：内联连接面板 ---- */
  function connectorRenderSources() {
    var host = $("#connector-body");
    if (!host) return;
    var d = connData(CB.data);
    var locked = connLocked(d);
    var focus = $("#connector-body").contains(document.activeElement)
      ? (document.activeElement.id || (document.activeElement.getAttribute("data-conn-source") ? "conn-src-" + document.activeElement.getAttribute("data-conn-source") : null))
      : null;
    var status = $("#connector-status");

    var phase = connPhase(d);
    var chips = h("div", "chip-row");
    var phaseChip = chip(connName(connSel.brand) + " · " + (CONN_PHASE[phase] || phase || "读取中"));
    phaseChip.className = "chip " + (phase === "connected" ? "chip--ok" : phase === "error" || CB.err ? "chip--bad" : CONN_ACTIVE[phase] ? "chip--cool" : "chip--muted");
    phaseChip.id = "connector-phase";
    chips.appendChild(phaseChip);
    if (CB.err) {
      var ec = chip((CB.err.status === 0 ? "bmemd 离线" : "请求失败"), "chip--bad");
      ec.id = "connector-error";
      chips.appendChild(ec);
    }
    var retry = mkButton("重试读取", connectorGet, "btn btn--quiet");
    retry.id = "connector-refresh";
    chips.appendChild(retry);

    var dl = h("dl", "kv");
    if (CB.err) {
      connAppendKv(dl, "错误", h("span", "connector-msg", CB.err.message));
    }
    if (d) {
      connAppendKv(dl, "CLI", d.installed === true ? chip("已安装", "chip--ok") : chip("未安装", "chip--muted"));
      connAppendKv(dl, "版本", d.cli_version ? String(d.cli_version) : "—");
      var authRow = h("span", "chip-row");
      authRow.appendChild(d.authenticated === true ? chip("身份与只读范围已验证", "chip--ok") : chip("尚无读取授权", "chip--amber"));
      var ac = asDict(d.account);
      var acName = String(ac.name || ac.id || "");
      if (acName) authRow.appendChild(chip(truncate(acName, 40), "chip--muted"));
      connAppendKv(dl, "认证", authRow);
      connAppendKv(dl, "已存范围", connSourceZh(connSourcesOf(d)) + (asDict(d.history).confirmed ? "" : "（尚未确认）"));
      var sy = asDict(d.sync);
      connAppendKv(dl, "同步", "阶段 " + String(sy.stage || "idle") + (sy.running === true ? "（进行中）" : ""));
      connAppendKv(dl, "导入 / 跳过 / 错误", fmtNum(intOf(sy.imported)) + " / " + fmtNum(intOf(sy.skipped)) + " / " + fmtNum(intOf(sy.errors)));
      connAppendKv(dl, "上次成功", sy.last_success_at ? timeEl(String(sy.last_success_at)) : "—");
      if (d.error) connAppendKv(dl, "服务端错误", h("span", "connector-msg", truncate(connError(d.error), 400)));
    }

    /* 范围表单：三项独立复选，互斥无；保存时按选择启用 enabled。 */
    var sel = connSelection(d);
    var form = h("div", "connector-part");
    var fieldset = h("fieldset", "connector-ranges");
    var legend = h("legend", "row-label", "读取范围（三类可任选，勾选范围后可保存并同步）");
    legend.id = "connector-ranges-legend";
    fieldset.appendChild(legend);
    fieldset.setAttribute("aria-labelledby", "connector-ranges-legend");
    CONN_SOURCES.forEach(function (o) {
      var lab = h("label", "connector-range");
      var cb = h("input");
      cb.type = "checkbox";
      cb.id = "conn-src-" + o.key;
      cb.value = o.key;
      cb.checked = sel.sources.indexOf(o.key) >= 0;
      cb.setAttribute("data-conn-source", o.key);
      cb.disabled = locked;
      cb.addEventListener("change", function () {
        var cur = connSelection(connData(CB.data));
        var srcs = CONN_SOURCES.map(function (s) { return s.key; }).filter(function (k) { return $("#conn-src-" + k).checked; });
        connSel.draft = { brand: connSel.brand, sources: srcs, sensitivity: cur.sensitivity };
        connectorRender();
      });
      lab.appendChild(cb);
      lab.appendChild(h("span", null, o.label));
      fieldset.appendChild(lab);
    });
    form.appendChild(fieldset);

    var sensLab = h("label", "connector-range");
    var sensCb = h("input");
    sensCb.type = "checkbox";
    sensCb.id = "conn-sensitivity";
    sensCb.checked = sel.sensitivity === "normal";
    sensCb.disabled = locked;
    sensCb.setAttribute("aria-describedby", "conn-sensitivity-desc");
    sensCb.addEventListener("change", function () {
      var cur = connSelection(connData(CB.data));
      connSel.draft = { brand: connSel.brand, sources: cur.sources, sensitivity: sensCb.checked ? "normal" : "private" };
      connectorRender();
    });
    sensLab.appendChild(sensCb);
    sensLab.appendChild(h("span", null, "允许云端助手召回"));
    form.appendChild(sensLab);
    var sensDesc = h("p", "connector-msg", sensCb.checked
      ? "已勾选：导入记忆为 normal，可被云端助手召回。"
      : "默认 private，仅供本地检索与注入。");
    sensDesc.id = "conn-sensitivity-desc";
    form.appendChild(sensDesc);

    var dirty = connSel.draft !== null && connSel.draft.brand === connSel.brand && !connSame(sel, connSaved(d));
    var acts = h("div", "connector-actions");
    if (!sel.sources.length) form.appendChild(h("p", "connector-msg", "暂不读取，勾选范围后可保存并同步。"));
    var saveBtn = mkButton(dirty ? "保存范围（未保存）" : "保存范围", connSaveSelection, dirty ? "btn btn--primary" : "btn");
    saveBtn.id = "conn-save";
    var connBtn = mkButton(phase === "unconfigured" ? "连接" : "重新连接", connConnect, "btn btn--primary");
    connBtn.id = "conn-connect";
    var syncBtn = mkButton("立即同步", function () { connSyncNow(connSelection(connData(CB.data))); });
    syncBtn.id = "conn-sync";
    /* 同步始终使用后端已保存的范围；草稿未保存时禁用，避免读取旧范围。 */
    syncBtn.disabled = locked || !sel.sources.length || dirty;
    var syncHintText = !sel.sources.length ? "暂未选择读取范围" : (dirty ? "先保存范围，再同步" : "");
    if (syncHintText) {
      var hint = h("span", "connector-msg", syncHintText);
      hint.id = "conn-sync-hint";
      syncBtn.setAttribute("aria-describedby", "conn-sync-hint");
      acts.appendChild(hint);
    }
    [saveBtn, connBtn, syncBtn].forEach(function (b) {
      if (connLocked(d)) b.disabled = true;
      acts.appendChild(b);
    });
    form.appendChild(acts);

    /* 目标 DOM 先清空再装配；同一挑战内轮询复用已加载的二维码，不反复请求同源端点。 */
    host.innerHTML = "";
    host.appendChild(chips);
    var authHost = h("div");
    host.appendChild(authHost);
    host.appendChild(dl);
    host.appendChild(form);

    var authBlock = connAuthBlock(d, CONN_ACTIVE[phase] === 1 && phase === "authorizing");
    if (authBlock) authHost.appendChild(authBlock);
    if (status) status.textContent = CB.sending ? "正在发送请求…" : (d && d.busy === true ? "后端操作进行中，完成后自动刷新。" : "读取范围与敏感级别：" + (asDict(d && d.history).confirmed ? "已确认" : "尚未确认"));
    if (focus) {
      var fEl = document.getElementById(focus);
      if (fEl && fEl.focus && !fEl.disabled) fEl.focus({ preventScroll: true });
    }
  }

  /* ---- 设置页：紧凑摘要 + 管理连接 ---- */
  function connectorRenderSettings() {
    var host = $("#settings-connector-body");
    if (!host) return;
    var d = connData(CB.data);
    var phase = connPhase(d);
    var dl = h("dl", "kv");
    connAppendKv(dl, "品牌", connName(connSel.brand) + "（" + connSel.brand + "）");
    var sc = chip(CONN_PHASE[phase] || phase || "读取中", phase === "connected" ? "chip--ok" : phase === "error" || CB.err ? "chip--bad" : "chip--muted");
    sc.id = "settings-connector-state";
    connAppendKv(dl, "状态", sc);
    if (d) {
      connAppendKv(dl, "认证", d.authenticated === true ? chip("已授权", "chip--ok") : chip("未授权", "chip--muted"));
      connAppendKv(dl, "读取范围", connSourceZh(connSourcesOf(d)));
    }
    if (CB.err) connAppendKv(dl, "错误", h("span", "connector-msg", (CB.err.status === 0 ? "bmemd 离线：" : "读取失败：") + CB.err.message));
    host.innerHTML = "";
    host.appendChild(dl);
    var link = $("#settings-connector-link");
    if (link) link.href = "#/sources?connector=" + connSel.brand;
  }

  function connectorRender() {
    if (state.screen === "sources") connectorRenderSources();
    else if (state.screen === "settings") connectorRenderSettings();
  }

  $("#connector-brand").addEventListener("change", function () {
    var v = connBrand(this.value);
    navigate("sources", function (p) { p.set("connector", v); });
  });

  /* ================= 启动 ================= */
  $("#page-foot-origin").textContent = location.origin;
  if (!location.hash || location.hash === "#" || location.hash === "#/") {
    history.replaceState(null, "", hashFor("overview", new URLSearchParams()));
  }
  applyRoute();
  state.booting = false;
  pollHealth();
  /* 侧边栏计数与当前屏无关：启动即取一次 stats（声明/纠正/遗忘后再取）。 */
  if (state.screen !== "overview") refreshSidebarStats();
  setInterval(pollHealth, 20000);

})();
