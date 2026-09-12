/* Fathom UX 原型交互（ISS-026）
 *
 * 独立合成数据原型：不请求任何后端 API、不读取真实文件系统、不操作 Finder。
 * 所有数据均为明显合成（演示用户 / 演示目录），用于评审五页导航、
 * 总览→变化→目录详情旅程、首次启动/失败恢复流程与各状态呈现。
 * 视觉与交互合同：docs/DESIGN.md。图标沿用 frontend/icons.js 的线条 SVG 规范。
 */
"use strict";
(function () {
  /* ===== 图标（与 frontend/icons.js 同规范：24 viewBox、描边 2、round） ===== */

  var ICON_PATHS = {
    anchor:
      '<circle cx="12" cy="5" r="3"/><line x1="12" x2="12" y1="22" y2="8"/><path d="M5 12H2a10 10 0 0 0 20 0h-3"/>',
    gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    pie: '<path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/>',
    fileText:
      '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    settings:
      '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    folderOpen:
      '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
    arrowUpRight: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    arrowDownRight: '<path d="m7 7 10 10"/><path d="M17 7v10H7"/>',
    alert:
      '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    shield:
      '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    refresh:
      '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    hardDrive:
      '<line x1="22" x2="2" y1="12" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" x2="6.01" y1="16" y2="16"/><line x1="10" x2="10.01" y1="16" y2="16"/>',
  };

  function icon(name, size) {
    var paths = ICON_PATHS[name];
    if (!paths) return "";
    var s = size || 16;
    return (
      '<span class="icon" style="width:' + s + "px;height:" + s + 'px" aria-hidden="true">' +
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      paths +
      "</svg></span>"
    );
  }
  var iconStyle =
    ".icon{display:inline-flex;align-items:center;justify-content:center;vertical-align:-2px;flex:none}" +
    ".icon svg{width:100%;height:100%;display:block}";
  var styleEl = document.createElement("style");
  styleEl.textContent = iconStyle;
  document.head.appendChild(styleEl);

  /* ===== 工具 ===== */

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  var G = function (x) { return Math.round(x * 1048576); };  /* GiB → KiB */
  var M = function (x) { return Math.round(x * 1024); };     /* MiB → KiB */

  function fmtKib(kib) {
    if (kib === null || kib === undefined) return "—";
    if (kib >= 1048576) return (kib / 1048576).toFixed(1) + " GiB";
    if (kib >= 1024) {
      var m = kib / 1024;
      return (m >= 100 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, "")) + " MiB";
    }
    return kib + " KiB";
  }

  /* 正数 +、负数 −（U+2212）、缺失 —；0 不着增长/缩减色（DESIGN 状态合同） */
  function delta(prev, curr) {
    if (prev === null || prev === undefined || curr === null || curr === undefined) {
      return { text: "—", cls: "delta-none", v: null };
    }
    var d = curr - prev;
    if (d === 0) return { text: "0", cls: "delta-zero", v: 0 };
    if (d > 0) return { text: "+" + fmtKib(d), cls: "delta-grow", v: d };
    return { text: "−" + fmtKib(-d), cls: "delta-shrink", v: d };
  }
  function fmtPct(part, total) {
    if (!total) return "—";
    var p = (part / total) * 100;
    return p >= 0.1 ? p.toFixed(1) + "%" : "<0.1%";
  }
  /* 中间截断：保留开头与末级目录名可读，完整路径放在 title 与详情中 */
  function midTrunc(p, max) {
    max = max || 44;
    if (p.length <= max) return p;
    return p.slice(0, 20) + "…" + p.slice(-(max - 21));
  }
  function lastSeg(p) { var i = p.lastIndexOf("/"); return i >= 0 ? p.slice(i + 1) : p; }
  function dirOf(p) { var i = p.lastIndexOf("/"); return i > 0 ? p.slice(0, i) : p; }
  function fmtElapsed(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  var toastTimer = null;
  function toast(msg) {
    var region = $("#toast-region");
    region.textContent = ""; /* 单条反馈：先清空再显示，不堆叠遮挡 */
    var t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    region.appendChild(t);
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { region.textContent = ""; }, 4600);
  }

  /* ===== 合成数据 ===== */

  var ROOT = "/Users/演示用户";
  var SNAP_A = { id: "s-0911", date: "2026-09-11", label: "9月11日 09:00" };
  var SNAP_B = { id: "s-0912", date: "2026-09-12", label: "9月12日 12:03" };
  var VOLUME = { totalG: 512, freeG: 80.0 };

  var LONG_SEG = "2026年夏季产品发布会素材（多机位拍摄）";
  var LONG_DIR =
    ROOT + "/媒体库/视频项目/" + LONG_SEG + "/A机位未剪辑片段/20260712_上午场第二段/机位A_原始素材_高码率";
  var LONG_FILE = LONG_DIR + "/raw_2026-07-12_机位A_上午场_第二段_4K_ProRes_演示素材.mov";

  /* 目录树（prev/curr 为 KiB；restrictedInPartial：部分权限场景下本轮读取受限） */
  var TREE = {
    name: ROOT, prev: G(32.0), curr: G(34.3), children: [
      { name: "媒体库", prev: G(18.0), curr: G(18.0), children: [
        { name: "视频项目", prev: G(12.0), curr: G(12.0), children: [
          { name: LONG_SEG, prev: G(6.0), curr: G(6.0), children: [
            { name: "A机位未剪辑片段", prev: G(6.0), curr: G(6.0), children: [
              { name: "20260712_上午场第二段", prev: G(6.0), curr: G(6.0), children: [
                { name: "机位A_原始素材_高码率", prev: G(6.0), curr: G(6.0) },
              ] },
            ] },
          ] },
          { name: "历届项目归档", prev: G(6.0), curr: G(6.0) },
        ] },
        { name: "照片图库", prev: G(5.0), curr: G(5.0) },
        { name: "音乐", prev: G(1.0), curr: G(1.0) },
      ] },
      { name: "开发", prev: G(4.6), curr: G(6.8), children: [
        { name: "项目A", prev: G(4.0), curr: G(6.1), children: [
          { name: "build", prev: G(1.8), curr: G(3.9), children: [
            { name: "target", prev: G(1.5), curr: G(2.9) },
            { name: "intermediates", prev: G(0.2), curr: G(0.7) },
            { name: "产物归档", prev: G(0.1), curr: G(0.1) },
          ] },
          { name: "src", prev: G(0.9), curr: G(0.9) },
          { name: "node_modules", prev: G(1.3), curr: G(1.3) },
        ] },
        { name: "项目B", prev: G(0.5), curr: G(0.6) },
        { name: "脚本与配置", prev: G(0.1), curr: G(0.1) },
      ] },
      { name: "归档", prev: G(5.0), curr: G(4.0) },
      { name: "下载", prev: G(2.8), curr: G(3.9), children: [
        { name: "系统镜像", prev: G(1.4), curr: G(2.4) },
        { name: "软件包", prev: G(0.9), curr: G(0.9) },
        { name: "其他", prev: G(0.5), curr: G(0.6) },
      ] },
      { name: "系统缓存", prev: G(0.9), curr: G(0.9) },
      { name: "Library", prev: G(0.6), curr: G(0.6), children: [
        { name: "受限目录", prev: G(0.6), curr: G(0.6), restrictedInPartial: true },
      ] },
      { name: "小缓存", prev: null, curr: M(11) },
    ],
  };

  /* 在树中按完整路径查找节点 */
  function findNode(path) {
    if (path === ROOT) return { node: TREE, chain: [] };
    if (path.indexOf(ROOT + "/") !== 0) return null;
    var segs = path.slice(ROOT.length + 1).split("/");
    var node = TREE;
    var chain = [];
    for (var i = 0; i < segs.length; i++) {
      var next = null;
      for (var j = 0; j < (node.children || []).length; j++) {
        if (node.children[j].name === segs[i]) { next = node.children[j]; break; }
      }
      if (!next) return null;
      chain.push(node);
      node = next;
    }
    return { node: node, chain: chain };
  }

  /* 变化表行（显式编排，含父子重叠与未记录样例） */
  var BUILD_PATH = ROOT + "/开发/项目A/build";
  var CHANGE_ROWS = [
    { path: BUILD_PATH, prev: G(1.8), curr: G(3.9), st: "measured" },
    { path: ROOT + "/开发", prev: G(4.6), curr: G(6.8), st: "measured" },
    { path: ROOT + "/下载", prev: G(2.8), curr: G(3.9), st: "measured" },
    { path: ROOT + "/归档", prev: G(5.0), curr: G(4.0), st: "measured" },
    { path: LONG_DIR, prev: G(6.0), curr: G(6.0), st: "measured" },
    { path: ROOT + "/小缓存", prev: null, curr: M(11), st: "unrecorded", note: "此前低于统计阈值（10 MiB），未记录不等于新增" },
    { path: ROOT + "/Library/受限目录", prev: G(0.6), curr: G(0.6), st: "measured", restrictedInPartial: true },
  ];
  var ROOT_NET = delta(G(31.9), G(34.2)); /* 已测量同口径净变化：+2.3 GiB */

  /* 走势（GiB；null = 当日未扫描，图中留 gap，不补零） */
  var DATES = ["09-06", "09-07", "09-08", "09-09", "09-10", "09-11", "09-12"];
  var VOLUME_SERIES = [86.4, 85.9, 84.8, null, 83.2, 82.1, 80.0];
  var TREND_MAP = {};
  TREND_MAP[BUILD_PATH] = [1.2, 1.2, 1.5, null, 1.6, 1.8, 3.9];
  TREND_MAP[LONG_DIR] = [6.0, 6.0, 6.0, null, 6.0, 6.0, 6.0];
  function trendFor(path) {
    if (TREND_MAP[path]) return { series: TREND_MAP[path], note: "09-09 未扫描，图中留空、不补零" };
    var found = findNode(path);
    if (!found || found.node.prev === null || found.node.prev === undefined) return null;
    var p = found.node.prev / 1048576, c = found.node.curr / 1048576;
    return { series: [p * 0.96, p * 0.97, p * 0.98, null, p * 0.99, p, c], note: "09-09 未扫描，图中留空、不补零" };
  }

  /* 大文件（合成） */
  var BIGFILES = [
    { kib: G(3.2), daysAgo: 0, mtime: "2026-09-12 10:22", path: ROOT + "/下载/系统镜像/ExampleOS-2026.09-installer.iso" },
    { kib: G(1.1), daysAgo: 1, mtime: "2026-09-11 21:04", path: LONG_FILE },
    { kib: M(840), daysAgo: 2, mtime: "2026-09-10 16:47", path: ROOT + "/开发/项目A/build/产物归档/演示应用-0.3.0-arm64.dmg" },
    { kib: M(620), daysAgo: 4, mtime: "2026-09-08 09:31", path: ROOT + "/下载/软件包/示例数据集-2026q3.tar" },
  ];

  /* 扫描运行历史 / 报告档案（合成） */
  var RUNS = {
    normal: [
      { at: "9月12日 12:03", st: "done", msg: "成功 · du 耗时 42 秒" },
      { at: "9月11日 09:00", st: "done", msg: "成功 · du 耗时 51 秒" },
    ],
    single: [{ at: "9月12日 12:03", st: "done", msg: "成功 · 首次基线 · du 耗时 44 秒" }],
    partial: [
      { at: "9月12日 12:03", st: "done", msg: "成功（部分覆盖：6 个目录读取受限）· du 46 秒" },
      { at: "9月11日 09:00", st: "done", msg: "成功 · du 耗时 51 秒" },
    ],
    failed: [
      { at: "9月12日 18:40", st: "failed", msg: "失败：du 子进程被终止（演示原因），已保留 12:03 有效快照" },
      { at: "9月12日 12:03", st: "done", msg: "成功 · du 耗时 42 秒" },
    ],
  };
  var REPORTS = [
    { date: "2026-09-12", title: "9月12日 · 净变化 +2.3 GiB",
      body: "对比 s-0911（9月11日 09:00）→ s-0912（9月12日 12:03），同根同口径。\n增长主力：开发/项目A/build +2.1 GiB；下载 +1.1 GiB；归档 −1.0 GiB。\n小缓存 11 MiB 为首次进入统计（此前低于阈值），不判为新增；列表行值不可相加。" },
    { date: "2026-09-11", title: "9月11日 · 净变化 +0.4 GiB",
      body: "对比 s-0910 → s-0911。主要来自 下载/系统镜像 +0.4 GiB。\n当日为完整覆盖，无受限目录。" },
    { date: "2026-09-10", title: "9月10日 · 净变化 −0.2 GiB",
      body: "对比 s-0908 → s-0910。09-09 未扫描，差分跳过该缺口，不跨缺口比较。" },
  ];

  var SCENARIOS = ["normal", "single", "first-launch", "partial", "failed", "offline"];
  var PAGE_TITLES = { overview: "总览", changes: "变化", browse: "分布", bigfiles: "大文件", settings: "设置" };

  /* ===== 状态 ===== */

  var state = {
    scenario: "normal",
    page: "overview",
    sortKey: "delta", sortDir: -1,
    search: "",
    rangeA: SNAP_A.date, rangeB: SNAP_B.date,
    browse: [],                 /* 面包屑节点链（不含根） */
    detailPath: null, detailReturn: null,
    bf: { queried: false, busy: false, days: 7, minMb: 100, at: null },
    onboardStep: 0,
    scan: { running: false, phase: "", startedAt: 0, timers: [] },
  };
  var wizardChoice = ROOT;

  /* ===== 渲染：外壳 ===== */

  function renderChrome() {
    document.body.dataset.scenario = state.scenario;
    document.body.dataset.detail = state.detailPath ? "open" : "closed";
    var sel = $("#scenario-select");
    if (sel && sel.value !== state.scenario) sel.value = state.scenario;

    $$(".nav-item").forEach(function (a) {
      if (a.dataset.page === state.page) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    $("#page-title").textContent = PAGE_TITLES[state.page] || "总览";

    $$(".page").forEach(function (p) { p.hidden = p.id !== "page-" + state.page; });
    renderChip();
  }

  function renderChip() {
    var chip = $("#status-chip");
    var scan = state.scan;
    var st = "idle", text = "";
    if (scan.running) {
      st = "running";
      text = "扫描中 · " + scan.phase + " · 已耗时 " + fmtElapsed(performance.now() - scan.startedAt);
    } else if (state.scenario === "offline") { st = "offline"; text = "本地服务未连接"; }
    else if (state.scenario === "failed") { st = "failed"; text = "上次扫描失败 · 9月12日 18:40"; }
    else if (state.scenario === "partial") { st = "partial"; text = "上次扫描 12:03 · 部分覆盖"; }
    else if (state.scenario === "single") { text = "基线已建立 · 9月12日 12:03"; }
    else if (state.scenario === "first-launch") { text = "尚未扫描"; }
    else { text = "上次扫描 9月12日 12:03 · 成功"; }
    chip.dataset.scanState = st;
    chip.innerHTML = '<span class="chip-dot"></span>' + esc(text);
    $("#btn-scan").disabled = scan.running;
  }

  /* ===== 渲染：总览 ===== */

  function regionError(title, extra) {
    return (
      '<div class="region-error" data-region-error="1">' +
      '<span class="re-title">' + icon("alert", 15) + " " + esc(title) + "</span>" +
      (extra || "") +
      "<span>上次成功获取数据：9月12日 12:03（当前展示为保留的缓存，非实时值）</span>" +
      '<button class="btn" data-action="retry-region">' + icon("refresh", 13) + " 重试</button>" +
      "</div>"
    );
  }

  function renderOverview() {
    var q = $('[data-region="quality"]');
    var conc = $('[data-region="conclusion"]');
    var s = state.scenario;
    var wizardOn = s === "first-launch";
    $("#onboard").hidden = !wizardOn;
    $("#ov-rest").hidden = wizardOn;
    conc.hidden = wizardOn; /* B1：向导期间隐藏结论区，切换场景不残留旧结论 */
    if (wizardOn) { renderOnboard(); q.innerHTML = ""; return; }

    /* 数据时间 / 范围 / 质量行 */
    var qh = "";
    if (s === "offline") {
      qh = '<span class="quality-chip">' + icon("alert", 12) + " 无法连接本地服务</span>" +
        "<span>上次成功数据 9月12日 12:03（缓存展示）</span>";
    } else if (s === "failed") {
      qh = '<span class="path-mono">' + esc(ROOT) + "</span>" +
        "<span>有效数据截至 9月12日 12:03</span>" +
        '<span class="quality-chip">' + icon("alert", 12) + " 9月12日 18:40 扫描失败</span>";
    } else if (s === "single") {
      qh = '<span class="path-mono">' + esc(ROOT) + "</span><span>基线 9月12日 12:03</span>" +
        '<span class="quality-chip ok">' + icon("check", 12) + " 覆盖完整</span>";
    } else {
      var partial = s === "partial";
      qh = '<span class="path-mono">' + esc(ROOT) + "</span>" +
        "<span>9月11日 09:00 → 9月12日 12:03</span>" +
        (partial
          ? '<span class="quality-chip warn">' + icon("alert", 12) + " 部分目录未读取（6 个）</span>" +
            '<button class="btn btn-ghost" data-action="goto-perm">查看权限说明 →</button>'
          : '<span class="quality-chip ok">' + icon("check", 12) + " 覆盖完整</span>");
    }
    q.innerHTML = qh;

    /* 结论区：先回答“发生了什么” */
    if (s === "offline") {
      conc.className = "conclusion error";
      conc.innerHTML =
        '<div class="conclusion-main">' +
        '<p class="conclusion-kicker">总览数据暂不可用</p>' +
        '<h2 class="conclusion-headline">无法连接本地服务</h2>' +
        '<p class="conclusion-sub">连接 127.0.0.1:7952 失败（演示）。各区域可单独重试；恢复入口见 设置 → 扫描计划与后台服务。</p>' +
        "</div>" +
        '<div class="conclusion-actions">' +
        '<button class="btn" data-action="retry-region">' + icon("refresh", 13) + " 重试</button>" +
        '<button class="btn" data-action="goto-service">查看恢复说明</button></div>';
    } else if (s === "failed") {
      conc.className = "conclusion error";
      conc.innerHTML =
        '<div class="conclusion-main">' +
        '<p class="conclusion-kicker">上次扫描失败（9月12日 18:40）</p>' +
        '<h2 class="conclusion-headline">扫描失败，已保留上次有效数据</h2>' +
        '<p class="conclusion-sub">原因：du 子进程被终止（演示）。最近一次有效快照仍为 9月12日 12:03，下方数据均来自该快照。</p>' +
        "</div>" +
        '<div class="conclusion-actions">' +
        '<button class="btn btn-primary" data-action="retry-scan">' + icon("refresh", 14) + " 重试扫描</button>" +
        '<button class="btn" data-action="goto-diag">查看诊断</button></div>';
    } else if (s === "single") {
      conc.className = "conclusion";
      conc.innerHTML =
        '<div class="conclusion-main">' +
        '<p class="conclusion-kicker">首次基线 · 9月12日 12:03</p>' +
        '<h2 class="conclusion-headline headline-ok">基线已建立</h2>' +
        '<p class="conclusion-sub">已记录 1 个有效快照：分布可用；变化对比需要下一个不同日期的快照，预计明天 12:00 自动扫描后生成。</p>' +
        "</div>" +
        '<div class="conclusion-actions">' +
        '<button class="btn btn-primary" data-action="goto" data-goto="browse">查看分布 →</button>' +
        '<button class="btn" data-action="goto" data-goto="settings">查看下次计划</button></div>';
    } else {
      var partial2 = s === "partial";
      conc.className = "conclusion";
      conc.innerHTML =
        '<div class="conclusion-main">' +
        '<p class="conclusion-kicker">最近变化 · 9月11日 09:00 → 9月12日 12:03 · 已测量同口径</p>' +
        '<h2 class="conclusion-headline headline-danger">最近增长 ' + ROOT_NET.text + "</h2>" +
        '<p class="conclusion-sub">主要来自 开发/项目A/build（' + delta(G(1.8), G(3.9)).text +
        "）与 下载（" + delta(G(2.8), G(3.9)).text + "）；" +
        (partial2 ? "部分目录读取受限，结论仅覆盖已测量范围；" : "") +
        "来源用途未识别时，只描述路径与变化量。</p>" +
        "</div>" +
        '<div class="conclusion-actions">' +
        '<button class="btn btn-primary" data-action="goto" data-goto="changes" data-journey="locate">查看变化 →</button>' +
        '<button class="btn" data-action="goto" data-goto="browse">查看分布</button></div>';
    }

    renderTopChanges();
    renderVolume();
    renderScanNote();
  }

  function renderTopChanges() {
    var el = $('[data-region="top-changes"]');
    var s = state.scenario;
    if (s === "offline") { el.innerHTML = regionError("无法连接本地服务"); return; }
    if (s === "single") {
      el.innerHTML = '<p class="bf-empty">已建立基线（9月12日 12:03）。变化结论需要下一个不同日期的快照；在此之前这里不会显示假数据。</p>';
      return;
    }
    var rows = [BUILD_PATH, ROOT + "/下载", ROOT + "/归档"].map(function (p) {
      var r = null;
      for (var i = 0; i < CHANGE_ROWS.length; i++) if (CHANGE_ROWS[i].path === p) r = CHANGE_ROWS[i];
      var d = delta(r.prev, r.curr);
      return (
        '<tr class="krow" data-path="' + esc(p) + '" tabindex="0">' +
        "<td><div class=\"cell-path\" title=\"" + esc(p) + '">' + esc(midTrunc(p)) + "</div></td>" +
        '<td class="num ' + d.cls + '">' + d.text + "</td>" +
        '<td><span class="st st-measured"><span class="st-dot"></span>已测量</span></td></tr>'
      );
    });
    el.innerHTML =
      '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th scope="col">目录</th><th scope="col" class="num">变化</th><th scope="col">状态</th>' +
      "</tr></thead><tbody>" + rows.join("") + "</tbody></table></div>" +
      '<p class="hint-line">另有 1 个目录（小缓存 11 MiB）此前未记录，不参与净变化。</p>';
  }

  function makeSpark(series, labels, unit) {
    /* 第二轮图表规格：精简网格（3 条弱参考线 + 刻度值）、明确坐标轴、
     * 首末 x 标签分别 start/end 锚定避免越出 viewBox、缺失日断开留空。 */
    var W = 640, H = 168, L = 46, R = 30, T = 12, B = 26;
    var vals = series.filter(function (v) { return v !== null; });
    if (vals.length === 0) return "";
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    if (max - min < 1) { min -= 1; max += 1; }
    var pad = (max - min) * 0.15; min -= pad; max += pad;
    function x(i) { return L + (i * (W - L - R)) / (series.length - 1); }
    function y(v) { return T + (1 - (v - min) / (max - min)) * (H - T - B); }
    function fmtTick(v) { return v >= 100 ? String(Math.round(v)) : v.toFixed(1).replace(/\.0$/, ""); }

    var grid = "", ticks = "";
    [0.25, 0.5, 0.75].forEach(function (f) {
      var gy = T + f * (H - T - B);
      var gv = max - f * (max - min);
      grid += '<line class="spark-grid" x1="' + L + '" y1="' + gy.toFixed(1) + '" x2="' + (W - R) + '" y2="' + gy.toFixed(1) + '"/>';
      ticks += '<text x="' + (L - 8) + '" y="' + (gy + 3.5).toFixed(1) + '" text-anchor="end">' + esc(fmtTick(gv)) + "</text>";
    });

    /* 分段折线与面积：缺失日断开、留空，不补零 */
    var segs = [], cur = [];
    series.forEach(function (v, i) {
      if (v === null) { if (cur.length) segs.push(cur); cur = []; }
      else cur.push({ i: i, v: v });
    });
    if (cur.length) segs.push(cur);
    var area = "", lines = "";
    segs.forEach(function (seg) {
      var pts = seg.map(function (p) { return x(p.i).toFixed(1) + "," + y(p.v).toFixed(1); });
      lines += '<polyline class="spark-line" points="' + pts.join(" ") + '"/>';
      if (seg.length >= 2) {
        var base = (H - B).toFixed(1);
        area += '<polygon class="spark-area" points="' + x(seg[0].i).toFixed(1) + "," + base + " " + pts.join(" ") +
          " " + x(seg[seg.length - 1].i).toFixed(1) + "," + base + '"/>';
      }
    });
    var dots = series.map(function (v, i) {
      return v === null ? "" :
        '<circle class="spark-dot" cx="' + x(i).toFixed(1) + '" cy="' + y(v).toFixed(1) + '" r="2.6"><title>' +
        esc(labels[i] + " · " + fmtTick(v) + " " + unit) + "</title></circle>";
    }).join("");

    var xlabels = labels.map(function (lb, i) {
      var anchor = i === 0 ? "start" : i === labels.length - 1 ? "end" : "middle";
      return '<text x="' + x(i).toFixed(1) + '" y="' + (H - 6) + '" text-anchor="' + anchor + '">' + esc(lb) + "</text>";
    }).join("");

    var hasGap = series.indexOf(null) >= 0;
    return (
      '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="走势图（' + esc(unit) +
      (hasGap ? "；缺失日期留空、不补零" : "") + "）；等价数据见下方表格\">" +
      grid + ticks +
      '<line class="spark-axis" x1="' + L + '" y1="' + (H - B) + '" x2="' + (W - R) + '" y2="' + (H - B) + '"/>' +
      area + lines + dots + xlabels + "</svg>"
    );
  }

  function sparkTable(series, labels, unit) {
    var prev = null;
    var rows = series.map(function (v, i) {
      var d;
      if (v === null || prev === null) d = "—";
      else if (v - prev === 0) d = "0";
      else d = (v - prev > 0 ? "+" : "−") + Math.abs(v - prev).toFixed(1);
      var row = "<tr><td>" + labels[i] + "</td><td class='num'>" + (v === null ? "未扫描" : v.toFixed(1)) +
        "</td><td class='num'>" + d + "</td></tr>";
      if (v !== null) prev = v;
      return row;
    }).join("");
    return '<table class="tbl tbl-mini"><thead><tr><th>日期</th><th class="num">' + esc(unit) +
      '</th><th class="num">较前点</th></tr></thead><tbody>' + rows + "</tbody></table>";
  }

  function renderVolume() {
    var el = $('[data-region="volume"]');
    var tEl = $('[data-region="volume-table"]');
    var meta = $("#vol-meta");
    var s = state.scenario;
    if (s === "offline") { el.innerHTML = regionError("无法连接本地服务"); tEl.innerHTML = ""; meta.textContent = ""; return; }
    if (s === "single") {
      el.innerHTML = '<p class="bf-empty">卷剩余 ' + VOLUME.freeG.toFixed(1) + " GiB / " + VOLUME.totalG +
        " GiB。走势需要至少两个不同日期的快照（当前 1 个），暂不绘制趋势，也不展示假 0。</p>";
      tEl.innerHTML = ""; meta.textContent = ""; return;
    }
    el.innerHTML = makeSpark(VOLUME_SERIES, DATES, "剩余 GiB");
    tEl.innerHTML = sparkTable(VOLUME_SERIES, DATES, "剩余 GiB");
    meta.textContent = "卷剩余 80.0 GiB / 512 GiB · 09-09 未扫描（留空不补零）";
  }

  function renderScanNote() {
    var el = $('[data-region="scan-note"]');
    var s = state.scenario;
    if (s === "offline") { el.innerHTML = regionError("无法获取扫描状态"); return; }
    if (s === "failed") {
      el.innerHTML =
        '<p class="hint-line" style="margin:0">9月12日 18:40 扫描失败：<span class="st-fail">du 子进程被终止（演示原因）</span>。' +
        '已保留 12:03 有效快照，数据可用。<button class="btn btn-ghost" data-action="goto-diag">查看运行历史 →</button></p>';
      return;
    }
    if (s === "single") {
      el.innerHTML = '<p class="hint-line" style="margin:0">首次基线扫描 9月12日 12:03 成功（du 44 秒）。下次计划：9月13日 12:00 自动扫描。</p>';
      return;
    }
    if (s === "partial") {
      el.innerHTML = '<p class="hint-line" style="margin:0">最近扫描 9月12日 12:03 成功，但 6 个目录读取受限（部分覆盖）。缺失目录不会被当作已删除。<button class="btn btn-ghost" data-action="goto-perm">查看权限说明 →</button></p>';
      return;
    }
    el.innerHTML = '<p class="hint-line" style="margin:0">最近扫描 9月12日 12:03 成功（du 42 秒）· 下次计划 9月13日 12:00 · <button class="btn btn-ghost" data-action="goto-diag">运行历史 →</button></p>';
  }

  /* ===== 渲染：首次启动向导 ===== */

  function phaseListHtml(current) {
    var order = ["读取目录结构", "统计目录大小", "写入快照"];
    return order.map(function (name) {
      var idx = order.indexOf(name), cur = order.indexOf(current);
      if (cur > idx) return '<li class="done">' + icon("check", 13) + " " + name + "</li>";
      if (cur === idx) return '<li class="active"><span class="spinner"></span> ' + name + "</li>";
      return "<li>" + icon("clock", 13) + " " + name + "</li>";
    }).join("");
  }

  function renderOnboard() {
    var box = $("#onboard");
    box.dataset.step = String(state.onboardStep);
    var body = $('[data-region="onboard-body"]');
    var steps = ["欢迎", "监控范围", "权限说明", "首扫进度"];
    var stepsLine = steps.map(function (n, i) {
      var cls = "step";
      if (i < state.onboardStep) cls += " step-done";
      if (i === state.onboardStep) cls += " step-on";
      return '<span class="' + cls + '"><span class="step-dot"></span>' + n + "</span>";
    }).join("");

    if (state.onboardStep === 0) {
      body.innerHTML =
        '<div class="onboard-steps">' + stepsLine + "</div>" +
        "<h2>欢迎使用 Fathom</h2>" +
        "<p>Fathom 每天记录一次目录容量，帮助你看清“磁盘被什么占了、最近哪里在增长”。</p>" +
        '<p class="hint">本机卷共 ' + VOLUME.totalG + " GiB，剩余 " + VOLUME.freeG.toFixed(1) +
        " GiB（演示值）。首次使用需要选择一个监控范围并完成一次基线扫描，之后才能对比变化。</p>" +
        '<div class="onboard-actions"><button class="btn btn-primary" data-action="wizard-next">开始配置</button>' +
        '<button class="btn btn-ghost" data-action="wizard-later">稍后再说</button></div>';
    } else if (state.onboardStep === 1) {
      body.innerHTML =
        '<div class="onboard-steps">' + stepsLine + "</div>" +
        "<h2>选择监控范围</h2>" +
        "<p>当前版本支持监控单个根目录（多根在后续版本提供）。推荐从主目录开始：</p>" +
        '<div class="choice-list">' +
        '<button class="choice" data-action="wizard-choice" data-choice-root="' + esc(ROOT) + '" aria-pressed="true"><span>' + icon("folderOpen", 16) +
        " 主目录 <code>" + esc(ROOT) + "</code><small>推荐：日常下载、开发与媒体文件的集中地</small></span></button>" +
        '<button class="choice" data-action="wizard-choice" data-choice-root="/Volumes/演示数据盘" aria-pressed="false"><span>' + icon("hardDrive", 16) +
        " 演示数据盘 <code>/Volumes/演示数据盘</code><small>外置卷（演示选项，当前未挂载）</small></span></button>" +
        "</div>" +
        '<div class="onboard-actions"><button class="btn btn-primary" data-action="wizard-next">继续</button>' +
        '<button class="btn btn-ghost" data-action="wizard-prev">上一步</button></div>';
    } else if (state.onboardStep === 2) {
      body.innerHTML =
        '<div class="onboard-steps">' + stepsLine + "</div>" +
        "<h2>后台扫描与权限说明</h2>" +
        "<p>Fathom 通过每天一次的后台扫描记录容量。读取部分系统目录（如 Library 下的受限区域）需要“完全磁盘访问”权限。</p>" +
        '<p class="hint">可以暂不授权：扫描会跳过受限目录，并在结果中如实标注“读取受限”，不会把缺失当作删除。授权可随时在 设置 → 监控范围与权限 中进行。</p>' +
        '<div class="onboard-actions">' +
        '<button class="btn" data-action="open-tcc">打开系统设置（模拟）</button>' +
        '<button class="btn btn-primary" data-action="wizard-scan">暂不授权，开始首次扫描</button>' +
        '<button class="btn btn-ghost" data-action="wizard-prev">上一步</button></div>';
    } else if (state.onboardStep === 3) {
      body.innerHTML =
        '<div class="onboard-steps">' + stepsLine + "</div>" +
        "<h2>正在建立基线…</h2>" +
        '<div class="scan-progress" data-scan-progress="1">' +
        '<ul class="scan-phase-list">' + phaseListHtml(state.scan.phase || "读取目录结构") + "</ul>" +
        '<p class="scan-elapsed" data-region="wizard-elapsed">已耗时 0:00 · 不估算百分比，避免误导</p>' +
        "</div>" +
        '<p class="hint">扫描在后台进行，可以随时取消；取消不会写入任何数据。</p>' +
        '<div class="onboard-actions"><button class="btn" data-action="wizard-cancel">取消扫描</button></div>';
    }
  }

  /* ===== 渲染：变化页 ===== */

  function effRows() {
    var partial = state.scenario === "partial";
    return CHANGE_ROWS.map(function (r) {
      if (partial && r.restrictedInPartial) {
        return { path: r.path, prev: r.prev, curr: null, st: "restricted", note: "读取受限，未计入对比" };
      }
      return r;
    });
  }

  function renderChanges() {
    var s = state.scenario;
    var metaEl = $('[data-region="diff-meta"]');
    var netEl = $('[data-region="net-line"]');
    var body = $('[data-region="changes-body"]');
    var panel = $("#page-changes .panel");
    var wrap = panel.querySelector(".tbl-wrap");

    if (s === "offline") {
      metaEl.textContent = "无法连接本地服务 · 上次成功数据 9月12日 12:03（缓存）";
      netEl.innerHTML = regionError("无法连接本地服务");
      body.innerHTML = "";
      wrap.style.display = "none";
      return;
    }
    wrap.style.display = "";

    if (s === "first-launch") {
      metaEl.textContent = "尚无快照：先完成首次扫描建立基线。";
      netEl.innerHTML =
        '<div class="region-error"><span class="re-title">还没有可对比的数据</span>' +
        "<span>完成首次扫描后，这里会展示两个日期之间的目录变化。</span>" +
        '<button class="btn btn-primary" data-action="wizard-open">开始首次扫描</button></div>';
      body.innerHTML = "";
      wrap.style.display = "none";
      return;
    }
    if (s === "single") {
      metaEl.textContent = "当前只有 1 个有效快照（9月12日 12:03）。差分需要两个不同日期的快照；同日重扫不会产生新的基线。";
      netEl.innerHTML =
        '<div class="region-error"><span class="re-title">等待下一个日期的快照</span>' +
        "<span>预计明天 12:00 自动扫描后可对比。这里不显示假 0 变化。</span>" +
        '<button class="btn" data-action="goto" data-goto="browse">先看分布</button></div>';
      body.innerHTML = "";
      wrap.style.display = "none";
      return;
    }

    var snaps = [SNAP_A, SNAP_B];
    var selA = $("#range-a"), selB = $("#range-b");
    selA.innerHTML = snaps.map(function (x) { return '<option value="' + x.date + '">' + x.label + "</option>"; }).join("");
    selB.innerHTML = snaps.map(function (x) { return '<option value="' + x.date + '">' + x.label + "</option>"; }).join("");
    selA.value = state.rangeA; selB.value = state.rangeB;

    if (state.rangeA === state.rangeB) {
      metaEl.textContent = "基线与对比是同一天（9月12日 12:03）：同日补扫不产生新的跨日基线，请选择两个不同日期。";
      netEl.innerHTML = "";
      body.innerHTML = "";
      wrap.style.display = "none";
      return;
    }

    var partial = s === "partial";
    metaEl.innerHTML = "基线 " + SNAP_A.label + " → 对比 " + SNAP_B.label +
      " · 同根 <code>" + esc(ROOT) + "</code> · 同口径（du -xk，KiB）· 覆盖：" +
      (partial ? '<span class="st-unrecorded">基线完整 → 对比部分覆盖（6 个目录读取受限）</span>' : "完整") +
      (s === "failed" ? " · 数据来自保留的有效快照（18:40 失败未替换数据）" : "");
    netEl.innerHTML =
      "<span>已测量同口径净变化 <strong class='" + ROOT_NET.cls + "'>" + ROOT_NET.text + "</strong></span>" +
      "<span>根目录 <span class='num'>" + fmtKib(G(32.0)) + " → " + fmtKib(G(34.3)) + "</span></span>" +
      '<span class="hint">口径：只统计两个快照都记录的目录；未记录目录不参与。</span>';

    /* 过滤 + 排序 */
    var rows = effRows();
    if (state.search) {
      var kw = state.search.toLowerCase();
      rows = rows.filter(function (r) { return r.path.toLowerCase().indexOf(kw) >= 0; });
    }
    rows = rows.slice().sort(function (a, b) {
      var key = state.sortKey, va, vb;
      if (key === "path") { va = a.path; vb = b.path; }
      else if (key === "prev") { va = a.prev === null ? -1 : a.prev; vb = b.prev === null ? -1 : b.prev; }
      else if (key === "curr") { va = a.curr === null ? -1 : a.curr; vb = b.curr === null ? -1 : b.curr; }
      else {
        var da = delta(a.prev, a.curr).v, db = delta(b.prev, b.curr).v;
        va = da === null ? 0 : Math.abs(da); vb = db === null ? 0 : Math.abs(db);
      }
      if (va < vb) return -1 * state.sortDir;
      if (va > vb) return 1 * state.sortDir;
      return a.path < b.path ? -1 : 1;
    });

    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="5" class="bf-empty">没有匹配“' + esc(state.search) + "”的目录。调整搜索条件；无匹配与读取失败是两种不同情况。</td></tr>";
      return;
    }
    body.innerHTML = rows.map(function (r) {
      var d = delta(r.prev, r.curr);
      var dprev = r.prev === null ? { text: "—", cls: "delta-none" } : { text: fmtKib(r.prev), cls: "delta-zero" };
      var dcurr = r.curr === null ? { text: "—", cls: "delta-none" } : { text: fmtKib(r.curr), cls: "delta-zero" };
      var stHtml;
      if (r.st === "restricted") stHtml = '<span class="st st-restricted"><span class="st-dot"></span>读取受限</span>';
      else if (r.st === "unrecorded") stHtml = '<span class="st st-unrecorded"><span class="st-dot"></span>未记录</span>';
      else stHtml = '<span class="st st-measured"><span class="st-dot"></span>已测量</span>';
      return (
        '<tr class="krow" data-path="' + esc(r.path) + '" tabindex="0" title="' + esc(r.note || r.path) + '">' +
        "<td><div class=\"cell-path\" title=\"" + esc(r.path) + '">' + esc(midTrunc(r.path)) + "</div></td>" +
        '<td class="num ' + dprev.cls + '">' + dprev.text + "</td>" +
        '<td class="num ' + dcurr.cls + '">' + dcurr.text + "</td>" +
        '<td class="num ' + d.cls + '">' + d.text + "</td>" +
        "<td>" + stHtml + "</td></tr>"
      );
    }).join("");

    $$("#changes-table .th-sort").forEach(function (btn) {
      var th = btn.closest("th");
      if (btn.dataset.sort === state.sortKey) th.setAttribute("aria-sort", state.sortDir === -1 ? "descending" : "ascending");
      else th.removeAttribute("aria-sort");
    });
  }

  function renderReports() {
    var el = $('[data-region="reports"]');
    el.innerHTML = REPORTS.map(function (r) {
      return (
        '<details class="report-item"><summary><span>' + esc(r.title) + "</span><span class='hint'>" +
        esc(r.date) + "</span></summary><div class='report-body'>" + esc(r.body) + "</div></details>"
      );
    }).join("");
  }

  /* ===== 渲染：分布页 ===== */

  var SHARE_COLORS = ["#2f6fed", "#2e9e5b", "#b98a2f", "#d64545", "#7c66d8", "#8a91a3"];

  function chainPath(idx) {
    if (idx < 0) return ROOT;
    return state.browse[idx].fullName;
  }

  function renderBrowse() {
    var s = state.scenario;
    var metaEl = $('[data-region="browse-meta"]');
    var crumbsEl = $("#crumbs");
    var shareEl = $('[data-region="share"]');
    var bodyEl = $('[data-region="browse-body"]');
    var noteEl = $('[data-region="browse-note"]');

    if (s === "offline") {
      metaEl.textContent = "无法连接本地服务 · 上次成功数据 9月12日 12:03（缓存）";
      crumbsEl.innerHTML = ""; shareEl.innerHTML = regionError("无法连接本地服务"); bodyEl.innerHTML = ""; noteEl.textContent = "";
      return;
    }
    if (s === "first-launch") {
      metaEl.textContent = "尚无快照";
      crumbsEl.innerHTML = "";
      shareEl.innerHTML =
        '<div class="region-error"><span class="re-title">尚无数据</span><span>完成首次扫描建立基线后，这里展示容量分布。</span>' +
        '<button class="btn btn-primary" data-action="wizard-open">开始首次扫描</button></div>';
      bodyEl.innerHTML = ""; noteEl.textContent = "";
      return;
    }

    var snapLabel = s === "single" ? SNAP_B.label + "（基线）" : SNAP_B.label;
    metaEl.textContent = "快照 " + snapLabel + " · 根 " + ROOT + (s === "failed" ? " · 保留的有效快照" : "");

    /* 面包屑 */
    var crumbHtml = '<button class="crumb" data-crumb="-1">主目录</button>';
    state.browse.forEach(function (n, i) {
      crumbHtml += '<span class="crumb-sep" aria-hidden="true">/</span>' +
        '<button class="crumb' + (i === state.browse.length - 1 ? " current" : "") + '" data-crumb="' + i +
        '" title="' + esc(n.fullName) + '">' + esc(n.name) + "</button>";
    });
    crumbsEl.innerHTML = crumbHtml;

    var parentNode = state.browse.length ? state.browse[state.browse.length - 1].node : TREE;
    var total = parentNode.curr;
    var single = s === "single";
    var partial = s === "partial";
    var kids = (parentNode.children || []).slice().sort(function (a, b) {
      return (b.curr || 0) - (a.curr || 0);
    });
    var big = kids.filter(function (k) { return k.curr >= M(100); });
    var small = kids.filter(function (k) { return k.curr < M(100); });

    /* 占比条 + 图例（等价数据在下方表格；颜色不是唯一信息） */
    if (big.length) {
      var segs = "", legend = "";
      big.forEach(function (k, i) {
        var pct = total ? (k.curr / total) * 100 : 0;
        var color = SHARE_COLORS[i % SHARE_COLORS.length];
        segs += '<div class="share-seg" style="width:' + pct.toFixed(2) + "%;background:" + color +
          '" title="' + esc(k.name) + " " + fmtPct(k.curr, total) + '"></div>';
        legend += '<span><span class="swatch" style="background:' + color + '"></span>' + esc(k.name) + " " + fmtPct(k.curr, total) + "</span>";
      });
      if (small.length) {
        var sum = small.reduce(function (acc, k) { return acc + (k.curr || 0); }, 0);
        var pct2 = total ? (sum / total) * 100 : 0;
        var color2 = SHARE_COLORS[SHARE_COLORS.length - 1];
        segs += '<div class="share-seg" style="width:' + pct2.toFixed(2) + "%;background:" + color2 + '"></div>';
        legend += '<span><span class="swatch" style="background:' + color2 + '"></span>其他（小于 100 MiB） ' + fmtPct(sum, total) + "</span>";
      }
      shareEl.innerHTML = '<div class="share-bar">' + segs + '</div><div class="share-legend">' + legend + "</div>";
    } else {
      shareEl.innerHTML = "";
    }

    /* 子目录表 */
    var rowsHtml = "";
    if (state.browse.length) {
      var parentPath = chainPath(state.browse.length - 2);
      rowsHtml +=
        '<tr class="krow" data-path="' + esc(parentPath) + '" tabindex="0">' +
        '<td colspan="4"><div class="cell-path">../ 返回上层（' + esc(lastSeg(parentPath) || ROOT) + "）</div></td>" +
        '<td><span class="row-actions"><button class="btn-mini" data-action="finder" data-path="' + esc(parentPath) +
        '" aria-label="在 Finder 中显示上级目录" title="在 Finder 中显示（模拟）">' + icon("folderOpen", 14) + "</button></span></td></tr>";
    }
    big.forEach(function (k) {
      var restricted = partial && k.restrictedInPartial;
      var d = (single || k.prev === null || restricted)
        ? { text: "—", cls: "delta-none" }
        : delta(k.prev, k.curr);
      var fullName = (state.browse.length ? chainPath(state.browse.length - 1) : ROOT) + "/" + k.name;
      rowsHtml +=
        '<tr class="krow" data-path="' + esc(fullName) + '" tabindex="0">' +
        "<td><div class=\"cell-path\" title=\"" + esc(fullName) + '">' + esc(k.name) + (restricted ? ' <span class="st st-restricted"><span class="st-dot"></span>读取受限</span>' : "") + "</div></td>" +
        '<td class="num">' + fmtKib(restricted ? null : k.curr) + "</td>" +
        '<td class="num ' + d.cls + '">' + d.text + "</td>" +
        '<td class="num">' + fmtPct(k.curr, total) + "</td>" +
        '<td><span class="row-actions"><button class="btn-mini" data-action="finder" data-path="' + esc(fullName) +
        '" aria-label="在 Finder 中显示 ' + esc(k.name) + '" title="在 Finder 中显示（模拟）">' + icon("folderOpen", 14) + "</button></span></td>" +
        "</tr>";
    });
    if (small.length) {
      var sum2 = small.reduce(function (acc, k) { return acc + (k.curr || 0); }, 0);
      rowsHtml +=
        "<tr><td><div class='cell-path cell-muted'>其他 " + small.length + " 个小目录（&lt;100 MiB）</div></td>" +
        "<td class='num'>" + fmtKib(sum2) + "</td><td class='num delta-none'>—</td><td class='num'>" + fmtPct(sum2, total) + "</td><td></td></tr>";
    }
    bodyEl.innerHTML = rowsHtml;
    noteEl.textContent = single
      ? "基线快照没有对比对象：“较上快照”一列为 —，等待下个日期。点击目录行下钻；无子目录的行打开详情。"
      : "子目录大小为累计值，父子不可相加；小于 100 MiB 的目录已合并为“其他”。点击目录行下钻；无子目录的行打开详情。";
  }

  /* ===== 渲染：大文件页 ===== */

  function renderBigfiles() {
    var s = state.scenario;
    var stEl = $('[data-region="bf-status"]');
    var tblEl = $('[data-region="bf-table"]');

    if (s === "offline") {
      stEl.textContent = "";
      tblEl.innerHTML = regionError("无法连接本地服务");
      return;
    }
    if (s === "first-launch") {
      stEl.textContent = "";
      tblEl.innerHTML =
        '<div class="region-error"><span class="re-title">尚未建立基线</span><span>首次扫描完成后才能查询近期大文件。</span>' +
        '<button class="btn btn-primary" data-action="wizard-open">开始首次扫描</button></div>';
      return;
    }

    if (!state.bf.queried) {
      stEl.innerHTML = '<span class="bf-status-line">尚未查询 —— 大文件查询会遍历文件元数据，需要明确点击“查询”，不会进入页面即自动执行。</span>';
      tblEl.innerHTML = "";
      return;
    }
    if (state.bf.busy) {
      stEl.innerHTML = '<span class="bf-status-line">查询中…（演示：约 0.7 秒）</span>';
      tblEl.innerHTML = "";
      return;
    }

    var days = state.bf.days, minMb = state.bf.minMb;
    var hits = BIGFILES.filter(function (f) {
      return f.daysAgo < days && f.kib >= minMb * 1024;
    });
    var cond = "近 " + days + " 天 · ≥ " + minMb + " MiB · 范围 " + ROOT;
    if (!hits.length) {
      stEl.innerHTML = '<span class="bf-status-line">无匹配结果 —— 条件：' + esc(cond) +
        " · 最近查询 " + esc(state.bf.at) + "。无匹配不等于读取失败。</span>";
      tblEl.innerHTML = "";
      return;
    }
    stEl.innerHTML = '<span class="bf-status-line">已查询 · ' + esc(state.bf.at) + " · " + esc(cond) + " · 命中 " + hits.length + " 项（上限 200，演示数据 4 项）</span>";
    tblEl.innerHTML =
      '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      '<th scope="col" class="num">大小</th><th scope="col">修改时间</th><th scope="col">文件</th><th scope="col"><span class="visually-hidden">操作</span></th>' +
      "</tr></thead><tbody>" +
      hits.map(function (f) {
        return (
          "<tr>" +
          '<td class="num">' + fmtKib(f.kib) + "</td>" +
          "<td>" + esc(f.mtime) + "</td>" +
          "<td><div class=\"cell-path\" title=\"" + esc(f.path) + '">' + esc(midTrunc(f.path, 52)) + "</div></td>" +
          '<td><span class="row-actions"><button class="btn-mini" data-action="finder" data-path="' + esc(dirOf(f.path)) +
          '" aria-label="打开所在目录" title="在 Finder 中显示（模拟）">' + icon("folderOpen", 14) + "</button></span></td>" +
          "</tr>"
        );
      }).join("") +
      "</tbody></table></div>" +
      '<p class="hint-line">大小为文件逻辑大小（st_size），与目录占用（du）口径不同。</p>';
  }

  /* ===== 渲染：设置页 ===== */

  function kv(rows) {
    return (
      '<table class="kv"><tbody>' +
      rows.map(function (r) { return "<tr><th scope='row'>" + r[0] + "</th><td>" + r[1] + "</td></tr>"; }).join("") +
      "</tbody></table>"
    );
  }

  function renderSettings() {
    var s = state.scenario;

    /* 监控范围与权限 */
    var perm;
    if (s === "partial") {
      perm = kv([
        ["监控范围", "<span class='path-mono'>" + esc(ROOT) + "</span>（单根）"],
        ["权限状态", "<span class='st st-restricted'><span class='st-dot'></span>部分受限：6 个目录读取失败</span>"],
        ["影响", "受限目录未计入容量与对比；不会被当作已删除。授权后重扫即可补全。"],
        ["操作", "<div class='settings-actions'><button class='btn' data-action='open-tcc'>打开系统设置（模拟）</button>" +
          "<button class='btn' data-action='rescan'>重扫并补全</button></div>"],
      ]);
    } else if (s === "first-launch") {
      perm = kv([
        ["监控范围", "尚未配置 —— 完成首次启动向导后确定"],
        ["权限状态", "未授权（可暂不授权，扫描将如实标注受限目录）"],
        ["操作", "<div class='settings-actions'><button class='btn btn-primary' data-action='wizard-open'>开始首次扫描</button></div>"],
      ]);
    } else if (s === "offline") {
      perm = kv([
        ["监控范围", "<span class='path-mono'>" + esc(ROOT) + "</span>（单根）· 缓存显示"],
        ["权限状态", "上次已知：已授权完全磁盘访问（演示）；当前服务未连接，无法实时核对"],
      ]);
    } else {
      perm = kv([
        ["监控范围", "<span class='path-mono'>" + esc(ROOT) + "</span>（单根；多根在后续版本提供）"],
        ["权限状态", "<span class='st st-ok'><span class='st-dot'></span>已授权完全磁盘访问（演示）</span>"],
        ["覆盖", "完整 · 最近一次扫描未出现读取受限"],
        ["操作", "<div class='settings-actions'><button class='btn' data-action='open-tcc'>打开系统设置（模拟）</button>" +
          "<button class='btn' data-action='goto' data-goto='overview'>查看覆盖摘要</button></div>"],
      ]);
    }
    $('[data-region="perm"]').innerHTML = perm;

    /* 计划与后台服务 */
    var service;
    if (s === "offline") {
      service =
        kv([
          ["扫描计划", "每日 12:00（缓存显示，服务恢复后核对）"],
          ["后台服务", "<span class='st st-fail'><span class='st-dot'></span>未连接（127.0.0.1:7952，演示）</span>"],
        ]) +
        '<div class="settings-actions">' +
        '<button class="btn" data-action="retry-region">' + icon("refresh", 13) + " 重试连接</button></div>" +
        "<h3 class='settings-h3'>恢复说明</h3><ol class='settings-note'>" +
        "<li>确认 Fathom 应用是否仍在运行（程序坞或菜单栏图标）。</li>" +
        "<li>从“应用程序”重新启动 Fathom，后台服务会随之拉起。</li>" +
        "<li>仍失败时导出匿名诊断并反馈；历史数据保留在本机，不会丢失。</li></ol>";
    } else {
      service = kv([
        ["扫描计划", "每日 12:00 自动扫描 · 首扫/重扫可手动触发"],
        ["后台服务", "<span class='st st-ok'><span class='st-dot'></span>运行中 · 已连接</span>"],
        ["通知", "日报完成后尝试系统通知；剩余空间低于 10 GiB 时提醒（与页面告警同一设置）"],
        ["操作", "<div class='settings-actions'><button class='btn' data-action='rescan'>立即扫描</button></div>"],
      ]);
    }
    $('[data-region="service"]').innerHTML = service;

    /* 数据与版本 */
    var snapCount = s === "first-launch" ? 0 : s === "single" ? 1 : 2;
    $('[data-region="data"]').innerHTML = kv([
      ["快照", snapCount === 0 ? "0（尚未扫描）" : snapCount + " 个有效快照"],
      ["保留策略", "每日保留 35 天，更早按周保留 12 周（删除前可导出）"],
      ["数据位置", "仅保存在本机（演示环境）；导出诊断不含真实路径"],
      ["版本", "0.3.0 · UX 原型（合成数据）"],
    ]);

    /* 诊断与卸载 */
    var diag;
    if (s === "offline") {
      diag = "<p class='hint' style='margin:0'>服务未连接，运行历史暂不可用（上次已知状态：9月12日 12:03 成功）。</p>";
    } else if (s === "first-launch") {
      diag = "<p class='hint' style='margin:0'>暂无运行记录。</p>";
    } else {
      var runs = RUNS[s] || RUNS.normal;
      diag =
        '<table class="tbl"><thead><tr><th>时间</th><th>结果</th><th>说明</th></tr></thead><tbody>' +
        runs.map(function (r) {
          var st = r.st === "done"
            ? "<span class='st st-ok'><span class='st-dot'></span>成功</span>"
            : "<span class='st st-fail'><span class='st-dot'></span>失败</span>";
          return "<tr><td>" + esc(r.at) + "</td><td>" + st + "</td><td style='color:var(--muted)'>" + esc(r.msg) + "</td></tr>";
        }).join("") +
        "</tbody></table>";
    }
    $('[data-region="diag"]').innerHTML =
      diag +
      '<div class="settings-actions"><button class="btn" data-action="export-diag">导出匿名诊断（模拟）</button>' +
      '<button class="btn btn-ghost" data-action="uninstall-note">卸载说明</button></div>' +
      '<p class="hint-line" style="margin-top:12px">卸载默认保留历史数据；删除数据前会单独预览并确认。诊断导出已去除真实路径与文件名。</p>';

    /* 智能分析（未启用，不假装已识别） */
    $('[data-region="agent"]').innerHTML =
      '<div class="agent-off">' +
      '<div class="agent-title">' + icon("shield", 15) + " 状态：未启用</div>" +
      "<p>智能解释（目录用途识别、Agent 解读）是远期可选功能，当前版本未提供，也不会显示任何虚构的用途结论。" +
      "启用后你可以在发送前预览数据范围：仅所选目录的最小事实集（路径、大小、变化），不含文件内容。</p>" +
      '<button class="btn" data-action="goto" data-goto="changes">在目录详情中查看“未启用”状态样例</button>' +
      "</div>";
  }

  /* ===== 渲染：目录详情（跨页共享） ===== */

  function renderDetail() {
    var box = $("#detail");
    if (!state.detailPath) { box.hidden = true; box.removeAttribute("data-path"); return; }
    var found = findNode(state.detailPath);
    if (!found) { closeDetail(true); return; }
    box.hidden = false;
    var node = found.node;
    var path = state.detailPath;
    var s = state.scenario;
    var restricted = s === "partial" && node.restrictedInPartial;
    var unrecordedPrev = node.prev === null || node.prev === undefined;
    var d = (unrecordedPrev || s === "single" || restricted)
      ? { text: "—", cls: "delta-none" }
      : delta(node.prev, node.curr);

    var stats =
      '<div class="detail-stats" data-region="detail-stats">' +
      '<div class="stat"><span class="stat-label">现在</span><strong class="num">' + fmtKib(restricted ? null : node.curr) + "</strong></div>" +
      '<div class="stat"><span class="stat-label">较上次</span><strong class="num ' + d.cls + '">' + d.text + "</strong></div>" +
      '<div class="stat"><span class="stat-label">数据截至</span><strong>9月12日 12:03</strong></div>' +
      '<div class="stat"><span class="stat-label">覆盖</span><strong>' + (s === "partial" ? "部分覆盖" : "覆盖完整") + "</strong></div></div>";

    var trendHtml = "";
    if (!restricted) {
      if (unrecordedPrev) {
        trendHtml = '<p class="hint" style="margin:0">该目录此前低于统计阈值（10 MiB），只有 1 个数据点（9月12日）。“未记录”不等于新增或删除。</p>';
      } else if (s === "single") {
        trendHtml = '<p class="hint" style="margin:0">当前仅 1 个快照，趋势需至少两个不同日期；不绘制假趋势。</p>';
      } else {
        var t = trendFor(path);
        if (t) {
          trendHtml =
            makeSpark(t.series, DATES, "GiB") +
            "<details class='tbl-toggle'><summary>以表格查看</summary>" +
            sparkTable(t.series, DATES, "GiB") +
            (t.note ? "<p class='hint'>" + esc(t.note) + "</p>" : "") +
            "</details>";
        }
      }
    }

    var subdirHtml = "";
    if (node.children && node.children.length) {
      var rows = node.children.slice().sort(function (a, b) { return (b.curr || 0) - (a.curr || 0); })
        .map(function (c) {
          var cd = (s === "single" || c.prev === null) ? { text: "—", cls: "delta-none" } : delta(c.prev, c.curr);
          var fullName = path + "/" + c.name;
          return (
            '<tr class="krow" data-path="' + esc(fullName) + '" tabindex="0">' +
            "<td><div class=\"cell-path\" title=\"" + esc(fullName) + '">' + esc(c.name) + "</div></td>" +
            "<td class='num'>" + fmtKib(c.curr) + "</td>" +
            "<td class='num " + cd.cls + "'>" + cd.text + "</td></tr>"
          );
        }).join("");
      subdirHtml =
        '<div class="detail-section" data-region="detail-subdirs"><h3>子目录</h3>' +
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>目录</th><th class="num">大小</th><th class="num">较上次</th></tr></thead><tbody>' +
        rows + "</tbody></table></div>" +
        "<p class='hint'>子目录之和可能小于目录总量（父目录含直接文件）。</p></div>";
    }

    var noteHtml = "";
    if (restricted) {
      noteHtml =
        '<div class="detail-section"><div class="agent-off"><div class="agent-title">' + icon("alert", 15) +
        " 读取受限</div><p>该目录在最近一次扫描中读取失败（权限），当前大小未知；不会计为删除。" +
        '可在 <a class="link-muted" href="#/settings">设置 → 监控范围与权限</a> 授权后重扫。</p></div></div>';
    } else if (unrecordedPrev) {
      noteHtml =
        '<div class="detail-section"><div class="agent-off"><div class="agent-title">' + icon("clock", 15) +
        " 未记录说明</div><p>两个快照使用同一统计阈值（≥10 MiB）。该目录此前低于阈值未入库，本次首次出现；" +
        "不能据此判断它是新增目录。</p></div></div>";
    }

    box.dataset.path = path;
    box.innerHTML =
      '<div class="detail-head"><h2 class="detail-title" id="detail-title" tabindex="-1">' + esc(lastSeg(path)) + "</h2>" +
      '<button class="btn btn-ghost" data-action="close-detail" aria-label="关闭目录详情">' + icon("x", 16) + " 关闭</button></div>" +
      '<p class="detail-path"><code title="' + esc(path) + '">' + esc(midTrunc(path, 52)) + "</code>" +
      '<button class="btn-mini" data-action="copy" data-path="' + esc(path) + '" aria-label="复制完整路径" title="复制完整路径">' + icon("copy", 14) + "</button></p>" +
      stats +
      (trendHtml ? '<div class="detail-section" data-region="detail-trend"><h3>历史趋势</h3>' + trendHtml + "</div>" : "") +
      subdirHtml +
      noteHtml +
      '<div class="detail-section" data-region="detail-agent"><div class="detail-footnote">' +
      '<span class="footnote-title">用途与来源：未启用</span>' +
      "<span>智能解释尚未启用；启用后会显示本地规则或 Agent 的解释及其证据与时间，且可驳回，当前不展示任何虚构用途。</span>" +
      '<a class="link-muted" href="#/settings">在设置中查看说明 →</a></div></div>' +
      '<div class="detail-actions" data-region="detail-actions">' +
      '<button class="btn" data-action="finder" data-path="' + esc(path) + '">' + icon("folderOpen", 14) + " 在 Finder 中显示</button>" +
      '<button class="btn" data-action="copy" data-path="' + esc(path) + '">' + icon("copy", 14) + " 复制路径</button></div>";
  }

  function openDetail(path, originEl) {
    if (!findNode(path)) return;
    state.detailPath = path;
    state.detailReturn = originEl || null;
    renderChrome();
    renderDetail();
    var t = $("#detail-title");
    if (t) t.focus({ preventScroll: true });
  }
  function closeDetail(silent) {
    var ret = state.detailReturn;
    state.detailPath = null;
    state.detailReturn = null;
    document.body.dataset.detail = "closed";
    renderDetail();
    renderChrome();
    if (!silent && ret && document.contains(ret)) ret.focus({ preventScroll: false });
  }

  /* ===== 模拟扫描 ===== */

  function clearScanTimers() {
    state.scan.timers.forEach(function (t) { clearTimeout(t); clearInterval(t); });
    state.scan.timers = [];
  }
  function runScanPhases(phases, onTick, onDone) {
    clearScanTimers();
    state.scan.running = true;
    state.scan.startedAt = performance.now();
    state.scan.phase = phases[0].name;
    renderChrome();
    var tick = setInterval(function () {
      renderChip();
      if (onTick) onTick();
    }, 250);
    state.scan.timers.push(tick);
    var acc = 0;
    phases.forEach(function (ph, i) {
      acc += ph.ms;
      (function (idx, at) {
        state.scan.timers.push(
          setTimeout(function () {
            if (idx + 1 < phases.length) state.scan.phase = phases[idx + 1].name;
            else {
              clearInterval(tick);
              state.scan.running = false;
              state.scan.phase = "";
            }
            renderChrome();
            if (onTick) onTick();
            if (idx + 1 === phases.length && onDone) onDone();
          }, at)
        );
      })(i, acc);
    });
  }

  function topbarScan() {
    var s = state.scenario;
    if (state.scan.running) return;
    if (s === "offline") { toast("无法连接本地服务，扫描请求未发送（演示）。"); return; }
    if (s === "first-launch") { go("overview"); state.onboardStep = 0; renderOverview(); return; }
    if (s === "failed") { retryFailedScan(); return; }
    runScanPhases(
      [{ name: "统计目录大小", ms: 1100 }, { name: "写入快照", ms: 500 }],
      null,
      function () {
        toast("演示扫描完成：生成同日快照，数据保持不变（同日补扫不产生新基线）。");
      }
    );
  }

  function retryFailedScan() {
    runScanPhases(
      [{ name: "重试统计目录大小", ms: 1100 }, { name: "写入快照", ms: 500 }],
      null,
      function () {
        setScenario("normal", { silent: true });
        toast("重试成功（演示）：18:46 生成的快照已生效；18:40 的失败记录保留在运行历史中。");
      }
    );
  }

  /* ===== 首次启动向导流程 ===== */

  function wizardScan() {
    runScanPhases(
      [{ name: "读取目录结构", ms: 900 }, { name: "统计目录大小", ms: 1200 }, { name: "写入快照", ms: 600 }],
      function () {
        var ul = $("#onboard .scan-phase-list");
        if (ul) ul.innerHTML = phaseListHtml(state.scan.phase);
        var el = $('[data-region="wizard-elapsed"]');
        if (el) el.textContent = "已耗时 " + fmtElapsed(performance.now() - state.scan.startedAt) + " · 不估算百分比，避免误导";
      },
      function () {
        setScenario("single", { silent: true });
        state.onboardStep = 0;
        go("browse");
        toast("基线已建立（演示）：已生成 1 个有效快照；变化对比将出现在明天的扫描之后。");
      }
    );
  }
  function wizardCancel() {
    clearScanTimers();
    state.scan.running = false;
    state.scan.phase = "";
    state.onboardStep = 0;
    renderChrome();
    renderOverview();
    toast("已取消首扫：未写入任何数据。");
  }

  /* ===== 场景与导航 ===== */

  function setScenario(name, opts) {
    opts = opts || {};
    if (SCENARIOS.indexOf(name) < 0) name = "normal";
    state.scenario = name;
    /* 切换场景重置瞬时交互状态，保留当前页 */
    clearScanTimers();
    state.scan.running = false;
    state.scan.phase = "";
    state.bf = { queried: false, busy: false, days: 7, minMb: 100, at: null };
    state.browse = [];
    state.sortKey = "delta"; state.sortDir = -1; state.search = "";
    $("#changes-search").value = "";
    state.rangeA = SNAP_A.date; state.rangeB = SNAP_B.date;
    $("#reports-panel").hidden = true;
    if (state.detailPath) closeDetail(true);
    if (name === "first-launch") {
      state.onboardStep = 0;
      if (state.page !== "overview") go("overview", true);
    }
    wizardChoice = ROOT;
    renderChrome();
    renderPage();
    if (!opts.silent) {
      var opt = $('#scenario-select option[value="' + name + '"]');
      toast("演示场景已切换：" + (opt ? opt.textContent : name));
    }
  }

  function go(page, renderNow) {
    if (!PAGE_TITLES[page]) page = "overview";
    state.page = page;
    location.hash = "#/" + page;
    if (renderNow) { renderChrome(); renderPage(); }
  }

  function renderPage() {
    if (state.page === "overview") renderOverview();
    else if (state.page === "changes") renderChanges();
    else if (state.page === "browse") renderBrowse();
    else if (state.page === "bigfiles") renderBigfiles();
    else if (state.page === "settings") renderSettings();
  }

  function routeFromHash() {
    var m = (location.hash || "").match(/^#\/([a-z]+)/);
    state.page = m && PAGE_TITLES[m[1]] ? m[1] : "overview";
    renderChrome();
    renderPage();
  }

  /* ===== 事件（统一委托） ===== */

  function drillTo(path) {
    var segs = path.slice(ROOT.length + 1).split("/");
    var node = TREE;
    var acc = ROOT;
    var chain = [];
    for (var i = 0; i < segs.length; i++) {
      var next = null;
      for (var j = 0; j < (node.children || []).length; j++) {
        if (node.children[j].name === segs[i]) { next = node.children[j]; break; }
      }
      if (!next) return false;
      acc = acc + "/" + segs[i];
      chain.push({ name: segs[i], fullName: acc, node: next });
      node = next;
    }
    state.browse = chain;
    renderBrowse();
    return true;
  }

  document.addEventListener("click", function (e) {
    var target = e.target;
    if (!target.closest) return;

    /* 1. 带动作的控件（按钮/选择项）优先 */
    var act = target.closest("[data-action]");
    if (act) { handleAction(act); return; }

    /* 2. 可聚焦表格行 */
    var row = target.closest("tr.krow");
    if (row) {
      var firstCell = row.querySelector(".cell-path");
      if (firstCell && firstCell.textContent.indexOf("../") === 0) {
        state.browse.pop();
        renderBrowse();
        return;
      }
      var path = row.dataset.path;
      if (!path) return;
      if (row.closest("#browse-table")) {
        var found = findNode(path);
        if (found && found.node.children && found.node.children.length) { drillTo(path); return; }
      }
      openDetail(path, row);
      return;
    }

    /* 3. 面包屑 */
    var crumb = target.closest(".crumb");
    if (crumb) {
      var idx = parseInt(crumb.dataset.crumb, 10);
      state.browse = state.browse.slice(0, idx + 1);
      renderBrowse();
      return;
    }

    /* 4. 排序表头 */
    var sortBtn = target.closest(".th-sort");
    if (sortBtn) {
      var key = sortBtn.dataset.sort;
      if (state.sortKey === key) state.sortDir *= -1;
      else { state.sortKey = key; state.sortDir = key === "path" ? 1 : -1; }
      renderChanges();
      sortBtn.focus();
      return;
    }
  });

  function handleAction(el) {
    var a = el.dataset.action;
    if (a === "goto") { go(el.dataset.goto); return; }
    if (a === "goto-perm") { go("settings", true); $("#set-perm").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
    if (a === "goto-diag") { go("settings", true); $("#set-diag").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
    if (a === "goto-service") { go("settings", true); $("#set-service").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
    if (a === "close-detail") { closeDetail(); return; }
    if (a === "copy") {
      var p = el.dataset.path;
      var show = function (ok) { toast(ok ? "已复制路径（演示）：" + p : "路径（请手动选择复制）：" + p); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(p).then(function () { show(true); }, function () { show(false); });
      } else show(false);
      return;
    }
    if (a === "finder") {
      toast("原型模拟：真实产品将在 Finder 中定位 " + el.dataset.path + "。原型不操作真实文件系统。");
      return;
    }
    if (a === "retry-region") {
      if (state.scenario === "offline") toast("重试失败（演示）：本地服务仍未连接。可按“恢复说明”重新启动应用。");
      else toast("重试成功（演示）：区域已重新加载。");
      return;
    }
    if (a === "retry-scan") { retryFailedScan(); return; }
    if (a === "rescan") { topbarScan(); return; }
    if (a === "open-tcc") { toast("原型模拟：将打开 系统设置 → 隐私与安全性 → 完全磁盘访问。授权与撤销均由你在系统界面完成。"); return; }
    if (a === "export-diag") { toast("原型模拟：将导出匿名诊断（已去除真实路径与文件名）。"); return; }
    if (a === "uninstall-note") { toast("卸载说明：默认保留历史数据；删除数据前会单独预览并确认。"); return; }
    if (a === "wizard-open") { go("overview", true); state.onboardStep = 0; renderOverview(); return; }
    if (a === "wizard-next") { state.onboardStep = Math.min(2, state.onboardStep + 1); renderOnboard(); return; }
    if (a === "wizard-prev") { state.onboardStep = Math.max(0, state.onboardStep - 1); renderOnboard(); return; }
    if (a === "wizard-later") { toast("已暂缓配置。随时可以从总览开始首次扫描。"); return; }
    if (a === "wizard-choice") {
      wizardChoice = el.dataset.choiceRoot;
      $$(".choice").forEach(function (c) { c.setAttribute("aria-pressed", String(c === el)); });
      if (wizardChoice !== ROOT) {
        toast("演示提示：原型数据只覆盖 " + ROOT + "；其他根目录会显示空状态，这里保持选择主目录。");
        wizardChoice = ROOT;
      }
      return;
    }
    if (a === "wizard-scan") { state.onboardStep = 3; renderOnboard(); wizardScan(); return; }
    if (a === "wizard-cancel") { wizardCancel(); return; }
  }

  /* 键盘：Esc 关详情回焦点；上下键在行间移动；Enter/Space 触发行 */
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && state.detailPath) {
      e.preventDefault();
      closeDetail();
      return;
    }
    var t = e.target;
    if (!t || !t.matches || !t.matches("tr.krow")) return;
    var rows = $$("tr.krow").filter(function (r) { return r.offsetParent !== null; });
    var idx = rows.indexOf(t);
    if (e.key === "ArrowDown" && idx >= 0 && idx < rows.length - 1) { e.preventDefault(); rows[idx + 1].focus(); }
    else if (e.key === "ArrowUp" && idx > 0) { e.preventDefault(); rows[idx - 1].focus(); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); t.click(); }
  });

  $("#btn-scan").addEventListener("click", topbarScan);

  $("#scenario-select").addEventListener("change", function () {
    setScenario(this.value);
  });

  $("#changes-search").addEventListener("input", function () {
    state.search = this.value.trim();
    renderChanges();
  });

  $("#range-a").addEventListener("change", function () { state.rangeA = this.value; renderChanges(); });
  $("#range-b").addEventListener("change", function () { state.rangeB = this.value; renderChanges(); });

  $("#btn-reports").addEventListener("click", function () {
    $("#reports-panel").hidden = false;
    renderReports();
  });
  $("#btn-reports-close").addEventListener("click", function () {
    $("#reports-panel").hidden = true;
  });

  $("#btn-bf").addEventListener("click", function () {
    var days = Math.max(1, Math.min(90, parseInt($("#bf-days").value, 10) || 7));
    var minMb = Math.max(1, parseInt($("#bf-mb").value, 10) || 100);
    state.bf.days = days; state.bf.minMb = minMb;
    state.bf.busy = true; state.bf.queried = true;
    renderBigfiles();
    setTimeout(function () {
      state.bf.busy = false;
      state.bf.at = "9月12日 12:10";
      renderBigfiles();
    }, 700);
  });

  window.addEventListener("hashchange", routeFromHash);

  /* ===== 启动 ===== */

  $$("[data-icon]").forEach(function (el) {
    var size = parseInt(el.dataset.iconSize || "18", 10);
    el.innerHTML = icon(el.dataset.icon, size);
  });
  routeFromHash();
})();
