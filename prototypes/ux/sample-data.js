/* ISS-104 视觉样板对照 · 共享合成数据与工具
 *
 * 数据值与 prototypes/ux/app.js（ISS-026，用户已确认的原型）完全一致：
 * 同一根目录、同一快照对、同一净变化结论，保证「现状复刻」与「候选方案」
 * 在同一数据下对照。本文件不依赖 app.js（其为 IIFE），仅复制所需子集。
 * 品牌几何 brandBasin 从 frontend/icons.js 逐参数复制（只读来源），
 * 与 scripts/ci_brand_geometry.sh 门禁同一几何来源。
 *
 * 全部为合成数据，仅用于评审；不连接真实扫描服务。
 */
"use strict";

var SAMPLE = (function () {
  var ROOT = "/Users/演示用户";
  var SNAP_A = { id: "s-0911", date: "2026-09-11", label: "9月11日 09:00" };
  var SNAP_B = { id: "s-0912", date: "2026-09-12", label: "9月12日 12:03" };
  var VOLUME = { totalG: 512, freeG: 80.0 };
  /* 剩余空间序列（GiB；null = 当日未扫描，图中留空、不补零） */
  var DATES = ["09-06", "09-07", "09-08", "09-09", "09-10", "09-11", "09-12"];
  var FREE_SERIES = [86.4, 85.9, 84.8, null, 83.2, 82.1, 80.0];
  /* 已用 = 总量 − 剩余（现状顶卡与双线图口径） */
  var USED_SERIES = FREE_SERIES.map(function (v) { return v === null ? null : VOLUME.totalG - v; });
  var SNAP_COUNT = 2;

  var LONG_SEG = "2026年夏季产品发布会素材（多机位拍摄）";
  var LONG_DIR =
    ROOT + "/媒体库/视频项目/" + LONG_SEG + "/A机位未剪辑片段/20260712_上午场第二段/机位A_原始素材_高码率";

  /* 最近变化 Top 行（与 app.js CHANGE_ROWS 同值；G/M 与 app.js 同函数） */
  var G = function (x) { return Math.round(x * 1048576); };
  var M = function (x) { return Math.round(x * 1024); };
  var TOP_ROWS = [
    { path: ROOT + "/开发/项目A/build", prev: G(1.8), curr: G(3.9), st: "measured" },
    { path: ROOT + "/下载", prev: G(2.8), curr: G(3.9), st: "measured" },
    { path: ROOT + "/归档", prev: G(5.0), curr: G(4.0), st: "measured" },
    { path: LONG_DIR, prev: G(6.0), curr: G(6.0), st: "measured" },
  ];
  var NET = { text: "+2.3 GiB", from: "32.0 GiB", to: "34.3 GiB" };

  /* 设置（演示值，与产品设置字段同名同义） */
  var SETTINGS = {
    root: ROOT,
    intervalKb: 5120,
    lowSpaceGb: 3.5,
    schedule: "12:00",
    masks: ["*.noindex", "node_modules"],
  };

  function fmtG(gb) {
    return (gb >= 100 ? gb.toFixed(0) : gb.toFixed(1)) + " GB";
  }
  function fmtKib(kib) {
    if (kib === null || kib === undefined) return "—";
    if (kib >= 1048576) return (kib / 1048576).toFixed(1) + " GiB";
    if (kib >= 1024) {
      var m = kib / 1024;
      return (m >= 100 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, "")) + " MiB";
    }
    return kib + " KiB";
  }
  function deltaText(prev, curr) {
    if (prev === null || prev === undefined || curr === null || curr === undefined) return "—";
    var d = curr - prev;
    if (d === 0) return "0";
    return (d > 0 ? "+" : "−") + fmtKib(Math.abs(d));
  }
  function deltaCls(prev, curr) {
    if (prev === null || prev === undefined || curr === null || curr === undefined) return "delta-none";
    var d = curr - prev;
    if (d === 0) return "delta-zero";
    return d > 0 ? "delta-grow" : "delta-shrink";
  }
  /* 中间截断（与 app.js midTrunc 同规则） */
  function midTrunc(p, max) {
    max = max || 44;
    if (p.length <= max) return p;
    return p.slice(0, 20) + "…" + p.slice(-(max - 21));
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ===== brandBasin（层叠深潭；逐参数复制自 frontend/icons.js，fill 固定色板） ===== */
  var BRAND_BASIN_PATHS =
    '<ellipse class="bv-base" fill="#FCFAF4" cx="12" cy="13.4" rx="11" ry="10.5"/>' +
    '<ellipse class="bv-l1" fill="#C6DCE4" cx="12" cy="13.4" rx="9.2" ry="9.5"/>' +
    '<ellipse class="bv-l2" fill="#286B88" cx="12" cy="13.4" rx="6.6" ry="7"/>' +
    '<ellipse class="bv-l3" fill="#062743" cx="12" cy="13.4" rx="4" ry="4.4"/>' +
    '<ellipse class="bv-core" fill="#00142A" cx="12" cy="13.4" rx="2.1" ry="2.5"/>' +
    '<path class="bv-notch" fill="#FCFAF4" d="M10.4 1.6 L12 4.4 L13.6 1.6 Z"/>';
  function brandBasinSvg(size) {
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="' + size + '" height="' + size +
      '" role="img" aria-label="Fathom 应用图标（brandBasin 矢量版，' + size + 'px）">' +
      BRAND_BASIN_PATHS +
      "</svg>"
    );
  }

  /* 总览主结论背景等深线（与 app.js CONTOUR_BG 同几何；低对比、装饰性） */
  var CONTOUR_BG =
    '<div class="brand-contour" data-brand="contour" aria-hidden="true">' +
    '<svg viewBox="0 0 340 160" preserveAspectRatio="xMaxYMid slice" focusable="false">' +
    [70, 105, 140, 175, 210, 245]
      .map(function (r, i) {
        return (
          '<circle cx="340" cy="80" r="' + r + '" fill="none" stroke="#345d7f" stroke-opacity="' +
          (0.1 - i * 0.012).toFixed(3) + '" stroke-width="1"/>'
        );
      })
      .join("") +
    "</svg></div>";

  return {
    ROOT: ROOT, SNAP_A: SNAP_A, SNAP_B: SNAP_B, VOLUME: VOLUME,
    DATES: DATES, FREE_SERIES: FREE_SERIES, USED_SERIES: USED_SERIES,
    SNAP_COUNT: SNAP_COUNT, LONG_DIR: LONG_DIR, TOP_ROWS: TOP_ROWS, NET: NET,
    SETTINGS: SETTINGS,
    fmtG: fmtG, fmtKib: fmtKib, deltaText: deltaText, deltaCls: deltaCls,
    midTrunc: midTrunc, esc: esc,
    brandBasinSvg: brandBasinSvg, CONTOUR_BG: CONTOUR_BG,
  };
})();
