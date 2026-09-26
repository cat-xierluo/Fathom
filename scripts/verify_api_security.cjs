#!/usr/bin/env node
/* ISS-022 安全验证（node scripts/verify_api_security.cjs）。
 *
 * 流程：
 * 1. 启动 scripts/security_fixture_server.py（随机端口 + 临时根 + 合成数据，
 *    open/notify/du 全部 stub），读取 stdout 身份行并经 GET /__fixture 复核
 *    fixture_id 与 pid，身份不符立即中止；
 * 2. 用 node:http 原始请求验证恶意 Host/Origin/无凭据写入与 reveal 越界案例
 *    （副作用前后用 /__fixture 复核 scan_runs / reveal_calls 未变化）；
 * 3. 用 Playwright 真实 Chromium 走合法页面操作（总览/变化/日报/分布/
 *    立即扫描/Finder 按钮），并验证恶意文件名在表格与 ECharts tooltip 中
 *    只作为文本出现、不生成执行节点；
 * 3.5 文档页边界（F1 修复回归面）：/docs /redoc /openapi.json 用独立 CSP
 *    （FastAPI 模板固定 jsdelivr CDN + 内联初始化），其余路径严格 CSP 不变；
 *    真实 Chromium 实测文档页“渲染并初始化”而非仅检查 CSP 字符串；
 * 4. finally 关闭浏览器与夹具进程，复核端口已释放，输出 JSON 结果摘要；
 *    退出码 0=全部通过。
 */
"use strict";

const { spawn } = require("child_process");
const http = require("http");
const https = require("https");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const PY = path.join(REPO, ".runtime", "bin", "python");
const FIXTURE_SCRIPT = path.join(__dirname, "security_fixture_server.py");
const OVERALL_TIMEOUT_MS = 180000;

const checks = [];
function record(name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail === undefined ? "" : String(detail) });
  process.stderr.write(`${ok ? "PASS" : "FAIL"}  ${name}${detail !== undefined && detail !== "" ? "  | " + detail : ""}\n`);
  if (!ok) process.exitCode = 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitUntil(fn, timeoutMs, what) {
  const deadline = Date.now() + timeoutMs;
  let lastErr = "";
  while (Date.now() < deadline) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) {
      lastErr = e.message;
    }
    await sleep(100);
  }
  throw new Error(`等待超时: ${what}${lastErr ? "（" + lastErr + "）" : ""}`);
}

function rawReq({ method = "GET", path: urlPath, headers = {}, body = null, port }) {
  return new Promise((resolve, reject) => {
    const base = { "Host": `127.0.0.1:${port}` };
    const req = http.request(
      { host: "127.0.0.1", port, method, path: urlPath,
        headers: { ...base, ...headers } },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          let json = null;
          try { json = JSON.parse(data); } catch (_) { /* 非 JSON */ }
          resolve({ status: res.statusCode, text: data, json, headers: res.headers });
        });
      });
    req.on("error", reject);
    if (body !== null) req.write(body);
    req.end();
  });
}

const jpost = (port, p, obj, headers = {}) =>
  rawReq({ method: "POST", path: p, port, headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(obj) });

// macOS 临时目录 /var 实为 /private/var 符号链接：服务端 reveal 记录的是规范化路径
const canon = (p) => p.replace(/^\/private(\/var\/)/, "/var/");

// ---------- 文档页（F1）辅助：CDN 探测 + 离线 route 夹具 ----------
// cdn.jsdelivr.net 是 FastAPI 文档模板固定的资源源；探测用于选择验证模式。
function cdnReachable(timeoutMs = 8000) {
  return new Promise((resolve) => {
    const req = https.request(
      "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
      { method: "GET", timeout: timeoutMs },
      (res) => { resolve(res.statusCode === 200); res.resume(); req.destroy(); });
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", () => resolve(false));
    req.end();
  });
}

// 离线兜底：本地替换 FastAPI 模板引用的全部外部资源。只能证明“CSP 允许这些
// 固定来源 + 内联初始化真实执行”，不能证明真实 Swagger/Redoc 资源可用。
const FAVICON_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64");
const STUB_SWAGGER_JS = `
window.SwaggerUIBundle = function (opts) {
  window.__docsInit = { url: opts.url };
  fetch(opts.url).then((r) => r.json()).then((spec) => {
    window.__docsSpec = { title: spec.info && spec.info.title,
                          paths: Object.keys(spec.paths || {}) };
    const el = document.querySelector(opts.dom_id || "#swagger-ui");
    if (el) el.innerHTML = "<h1>" + (spec.info ? spec.info.title : "") + "</h1>" +
      "<ul>" + Object.keys(spec.paths || {}).map((p) => "<li>" + p + "</li>").join("") + "</ul>";
  });
  return { initOAuth: function () {} };
};
window.SwaggerUIBundle.presets = { apis: {} };
window.SwaggerUIBundle.SwaggerUIStandalonePreset = {};
`;
const STUB_REDOC_JS = `
(function () {
  const el = document.querySelector("redoc");
  const url = el && el.getAttribute("spec-url");
  window.__redocInit = { url: url };
  fetch(url).then((r) => r.json()).then((spec) => {
    el.innerHTML = "<h1>" + (spec.info ? spec.info.title : "") + "</h1>" +
      "<ul>" + Object.keys(spec.paths || {}).map((p) => "<li>" + p + "</li>").join("") + "</ul>";
  });
})();
`;

async function installDocsStubs(page) {
  await page.route(/https:\/\/cdn\.jsdelivr\.net\//, (route) => {
    const u = route.request().url();
    if (u.includes("swagger-ui-bundle.js")) {
      return route.fulfill({ status: 200, contentType: "application/javascript", body: STUB_SWAGGER_JS });
    }
    if (u.includes("redoc.standalone.js")) {
      return route.fulfill({ status: 200, contentType: "application/javascript", body: STUB_REDOC_JS });
    }
    if (u.endsWith(".css")) {
      return route.fulfill({ status: 200, contentType: "text/css", body: "/* route-stub */" });
    }
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body: "" });
  });
  await page.route(/https:\/\/fastapi\.tiangolo\.com\//, (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: FAVICON_PNG }));
  await page.route(/https:\/\/fonts\.googleapis\.com\//, (route) =>
    route.fulfill({ status: 200, contentType: "text/css", body: "/* route-stub */" }));
  await page.route(/https:\/\/fonts\.gstatic\.com\//, (route) =>
    route.fulfill({ status: 200, contentType: "font/woff2", body: Buffer.alloc(0) }));
}

const swaggerReady = (page) => page.evaluate(() => {
  const su = document.querySelector("#swagger-ui");
  return su && su.textContent.includes("Fathom") && su.innerHTML.length > 200
    ? { domLen: su.innerHTML.length, initVia: window.__docsInit ? "route-stub" : "real-bundle" }
    : null;
});
const redocReady = (page) => page.evaluate(() => {
  const el = document.querySelector("redoc");
  return el && el.textContent.includes("Fathom") && el.innerHTML.length > 200
    ? { domLen: el.innerHTML.length, initVia: window.__redocInit ? "route-stub" : "real-bundle" }
    : null;
});

async function main() {
  // ---------- 1. 启动夹具并核对唯一身份 ----------
  const child = spawn(PY, [FIXTURE_SCRIPT], { cwd: REPO, stdio: ["pipe", "pipe", "pipe"] });
  const stderrBuf = [];
  child.stderr.on("data", (c) => stderrBuf.push(String(c)));
  const identity = await new Promise((resolve, reject) => {
    let buf = "";
    const timer = setTimeout(() => reject(new Error("夹具 30s 内未输出身份行")), 30000);
    child.stdout.on("data", (c) => {
      buf += String(c);
      const line = buf.split("\n").find((l) => l.includes('"fixture_id"'));
      if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`夹具提前退出 code=${code}: ${stderrBuf.join("").slice(0, 2000)}`));
    });
  });
  const port = identity.port;
  const markerImg = identity.markers.img;
  const markerNew = identity.markers.new;
  record("fixture-process", true, `pid=${child.pid} port=${port} root=${identity.root}`);

  const probe = await rawReq({ path: "/__fixture", port });
  record("fixture-identity-match",
    probe.status === 200 && probe.json.fixture_id === identity.fixture_id &&
      probe.json.pid === child.pid,
    `fixture_id=${identity.fixture_id.slice(0, 8)}…`);

  let browser = null;
  let docsSummary = null;
  try {
    // ---------- 2. 恶意 Host / Origin / 无凭据写入 / reveal 越界（原始 HTTP） ----------
    const staticBadHost = await rawReq({ path: "/", port, headers: { Host: "evil.example" } });
    record("static-bad-host-rejected", staticBadHost.status === 403, `GET / Host=evil.example -> ${staticBadHost.status}`);

    const bootEvil = await rawReq({ path: "/api/bootstrap", port, headers: { Origin: "http://evil.example" } });
    record("bootstrap-evil-origin-rejected",
      bootEvil.status === 403 && !bootEvil.text.includes('"token"'),
      `-> ${bootEvil.status}`);

    const boot = await rawReq({ path: "/api/bootstrap", port });  // CLI 合同：无 Origin
    const token = boot.json && boot.json.token;
    record("cli-bootstrap-ok", boot.status === 200 && typeof token === "string" && token.length >= 32);

    const sideEffectProbes = [];
    sideEffectProbes.push(["scan-bad-host", await jpost(port, "/api/scan", {}, { Host: "evil.example" })]);
    sideEffectProbes.push(["scan-evil-origin-with-token", await jpost(port, "/api/scan", {}, { Origin: "http://evil.example", "X-Fathom-Token": token })]);
    sideEffectProbes.push(["scan-no-token", await jpost(port, "/api/scan", {}, {})]);

    let fx = (await rawReq({ path: "/__fixture", port })).json;
    const rejectedBeforeSideEffect = sideEffectProbes.every(([, r]) => r.status === 403) &&
      fx.scan_runs_count === 0 && fx.reveal_calls.length === 0 &&
      sideEffectProbes.every(([, r]) => !r.text.includes(token));
    record("malicious-writes-rejected-before-side-effect", rejectedBeforeSideEffect,
      `statuses=[${sideEffectProbes.map(([, r]) => r.status)}] scan_runs=${fx.scan_runs_count}`);

    const root = identity.root;
    const revealCases = [
      ["dotdot", { path: `${root}/../outside` }, 400],
      ["symlink-escape", { path: `${root}/link-out` }, 400],
      ["prefix-similar-root", { path: `${root}-evil` }, 400],
      ["nonexistent", { path: `${root}/no-such-dir` }, 404],
    ];
    for (const [name, payload, expectStatus] of revealCases) {
      const r = await jpost(port, "/api/reveal", payload, { "X-Fathom-Token": token });
      record(`reveal-${name}-rejected`, r.status === expectStatus, `-> ${r.status}（期望 ${expectStatus}）`);
    }
    const revealNonObject = await jpost(port, "/api/reveal", [1, 2], { "X-Fathom-Token": token });
    record("reveal-non-object-rejected", revealNonObject.status === 400, `-> ${revealNonObject.status}`);
    const revealNoToken = await jpost(port, "/api/reveal", { path: `${root}/sub` }, {});
    record("reveal-without-token-rejected", revealNoToken.status === 403, `-> ${revealNoToken.status}`);
    const revealEvilOrigin = await jpost(port, "/api/reveal", { path: `${root}/sub` },
      { Origin: "http://evil.example", "X-Fathom-Token": token });
    record("reveal-evil-origin-rejected", revealEvilOrigin.status === 403, `-> ${revealEvilOrigin.status}`);

    fx = (await rawReq({ path: "/__fixture", port })).json;
    record("no-open-side-effect-for-rejected-paths", fx.reveal_calls.length === 0,
      `reveal_calls=${JSON.stringify(fx.reveal_calls)}`);

    const revealOk = await jpost(port, "/api/reveal", { path: `${root}/sub` }, { "X-Fathom-Token": token });
    fx = (await rawReq({ path: "/__fixture", port })).json;
    record("cli-reveal-valid-path-recorded",
      revealOk.status === 200 && fx.reveal_calls.length === 1 &&
        fx.reveal_calls[0][0] === "/usr/bin/open" &&
        canon(fx.reveal_calls[0][2]) === `${root}/sub`,
      `calls=${JSON.stringify(fx.reveal_calls)}`);
    await rawReq({ path: "/__fixture?reset_reveal=1", port });  // 清零，供 UI 案例独立计数

    // ---------- 3. 真实 Chromium：合法操作 + 恶意文件名渲染 ----------
    let pw;
    try {
      pw = require("playwright");
    } catch (e) {
      record("playwright-available", false, `require('playwright') 失败: ${e.message}`);
      throw new Error("playwright 不可用");
    }
    browser = await pw.chromium.launch({ headless: true });
    const browserPid = typeof browser.process === "function" && browser.process()
      ? browser.process().pid : "n/a";
    record("chromium-launched", true, `pid=${browserPid}`);
    const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const consoleErrors = [];
    const dialogs = [];
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
    page.on("dialog", (d) => { dialogs.push(d.message()); d.dismiss(); });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));

    const base = `http://127.0.0.1:${port}`;
    await page.goto(base + "/", { waitUntil: "networkidle" });
    const snaps = await page.locator("#card-snaps").textContent();
    record("overview-loads", snaps.trim() === "2", `快照数卡片=${snaps.trim()}`);
    await waitUntil(async () => page.evaluate(
      () => { const c = document.querySelector("#chart-volume canvas"); return !!c && c.width > 0; }),
      10000, "卷容量图表渲染");
    record("charts-render-under-csp", true, "ECharts canvas 非零尺寸");

    // 变化页：恶意文件名按文本渲染
    await page.goto(base + "/#/changes", { waitUntil: "networkidle" });
    await waitUntil(async () => page.evaluate(
      () => document.querySelectorAll("#tbl-added tbody tr td.path").length > 0),
      10000, "新增目录表格");
    const addedTexts = await page.evaluate(
      () => [...document.querySelectorAll("#tbl-added tbody tr td.path")].map((t) => t.textContent));
    const addedTitles = await page.evaluate(
      () => [...document.querySelectorAll("#tbl-added tbody tr td.path")].map((t) => t.getAttribute("title")));
    record("malicious-name-rendered-as-text",
      addedTexts.some((t) => t.includes(markerNew)) &&
        addedTitles.includes(`${identity.root}/${markerNew}`),
      `新增行数=${addedTexts.length}`);
    const execNodes = await page.evaluate(
      () => document.querySelectorAll("img[onerror], svg[onload], script[nonce]").length);
    record("no-execution-nodes-in-dom", execNodes === 0, `可疑节点=${execNodes}`);

    // ECharts tooltip（renderDeltaBars formatter）：优先 showTip 真实 DOM，
    // 该构建不渲染时直接以恶意参数调用生产 formatter 断言转义
    const tipCheck = await page.evaluate((marker) => {
      const el = document.getElementById("chart-grown");
      const chart = window.echarts.getInstanceByDom(el);
      const cats = chart.getOption().yAxis[0].data;
      const idx = cats.findIndex((c) => c.includes(marker.slice(0, 12)));
      if (idx < 0) return { found: false };
      chart.dispatchAction({ type: "showTip", seriesIndex: 0, dataIndex: idx });
      const div = [...el.querySelectorAll("div")]
        .filter((d) => !d.querySelector("canvas"))  // 排除 painter 层（内含 canvas）
        .find((d) => d.innerHTML.trim().length > 0);
      let html = div ? div.innerHTML : "";
      let via = div ? "showTip-dom" : "";
      if (!div) {
        const fmt = chart.getOption().tooltip[0].formatter;
        const data = chart.getOption().series[0].data[idx];
        html = fmt({ data });
        via = "formatter-direct";
      }
      return {
        found: true, via, html,
        hasImg: html.includes("<img") || !!el.querySelector("img[onerror]"),
        escaped: html.includes("&lt;img"),
      };
    }, markerImg);
    record("chart-tooltip-escapes-path",
      tipCheck.found === true && tipCheck.escaped === true && tipCheck.hasImg === false,
      tipCheck.found
        ? `via=${tipCheck.via} escaped=${tipCheck.escaped} hasImg=${tipCheck.hasImg} html=${JSON.stringify((tipCheck.html || "").slice(0, 140))}`
        : "未找到恶意条目柱条");

    // 日报：内容包含恶意文件名，只作为文本展示
    await page.waitForSelector("#report-list .report-item", { timeout: 10000 });
    await page.locator("#report-list .report-item").first().click();
    await waitUntil(async () => page.evaluate(
      () => !document.getElementById("report-view").classList.contains("hidden") &&
        document.querySelector("#report-view pre") !== null), 10000, "日报渲染");
    const reportCheck = await page.evaluate((marker) => {
      const pre = document.querySelector("#report-view pre");
      const html = document.getElementById("report-view").innerHTML;
      return {
        asText: pre && pre.textContent.includes(marker),
        asHtml: html.includes("<img"),
      };
    }, markerImg);
    record("report-renders-malicious-name-as-text",
      reportCheck.asText === true && reportCheck.asHtml === false);

    const pwnedFlags = await page.evaluate(
      () => [window.__pwned, window.__pwned2, window.__pwned3]);
    record("no-script-executed-from-names",
      pwnedFlags.every((f) => f === undefined), `flags=${JSON.stringify(pwnedFlags)}`);

    // 分布页 Finder 按钮（成功路径：sub 目录真实存在）。
    // ISS-099：分布页自 ISS-094 起为页内二级 tab（默认「占用分布」），
    // 目录表格在默认 hidden 的「目录浏览器」分区——此前直接等表格按钮
    // 可见会超时。按真实用户路径点击页内 tab 进入浏览器视图，再执行
    // 原 reveal 断言（断言与精确计数不变）。
    await page.goto(base + "/#/browse", { waitUntil: "networkidle" });
    await page.waitForSelector(
      '#page-browse:not(.hidden) [data-test="browse-tab-browser"]', { timeout: 10000 });
    await page.locator('[data-test="browse-tab-browser"]').click();
    await page.waitForSelector("#tbl-browse [data-reveal]", { timeout: 10000 });
    const firstRevealPath = await page.evaluate(
      () => document.querySelector("#tbl-browse [data-reveal]").getAttribute("data-reveal"));
    await page.locator("#tbl-browse [data-reveal]").first().click();
    await waitUntil(async () => (await rawReq({ path: "/__fixture", port })).json.reveal_calls.length > 0,
      10000, "UI reveal 生效");
    fx = (await rawReq({ path: "/__fixture", port })).json;
    record("ui-reveal-token-path-works",
      fx.reveal_calls.length === 1 && canon(fx.reveal_calls[0][2]) === canon(firstRevealPath) &&
        canon(firstRevealPath) === `${root}/sub`,
      `calls=${JSON.stringify(fx.reveal_calls)}`);

    // 立即扫描（浏览器令牌合同端到端）
    const idsBefore = (await rawReq({ path: "/__fixture", port })).json.snapshot_ids;
    await page.locator("#btn-scan").click();
    await waitUntil(async () => {
      const st = await rawReq({ path: "/api/scan/status", port });
      return st.json && st.json.status === "done";
    }, 30000, "UI 扫描完成");
    fx = (await rawReq({ path: "/__fixture", port })).json;
    record("ui-scan-with-bootstrap-token",
      fx.snapshot_ids.length === 2 && Math.max(...fx.snapshot_ids) > Math.max(...idsBefore) &&
        fx.scan_runs_count === 1,
      `ids ${JSON.stringify(idsBefore)} -> ${JSON.stringify(fx.snapshot_ids)} runs=${fx.scan_runs_count}`);

    // ---------- 4. CLI 合同：无 Origin + 令牌 触发扫描 ----------
    const cliScan = await jpost(port, "/api/scan", {}, { "X-Fathom-Token": token });
    await waitUntil(async () => {
      const st = await rawReq({ path: "/api/scan/status", port });
      return st.json && st.json.status === "done";
    }, 30000, "CLI 扫描完成");
    fx = (await rawReq({ path: "/__fixture", port })).json;
    record("cli-scan-contract-ok",
      cliScan.status === 200 && cliScan.json.ok === true && fx.scan_runs_count === 2,
      `runs=${fx.scan_runs_count}`);

    const cspViolations = consoleErrors.filter((t) => /Content Security Policy|Refused to/i.test(t));
    record("no-csp-violations", cspViolations.length === 0, cspViolations.slice(0, 2).join("; "));
    record("no-unexpected-dialogs", dialogs.length === 0, dialogs.join("; "));

    // ---------- 3.5 文档页边界（F1 修复回归面） ----------
    // /docs /redoc /openapi.json 用独立 CSP（模板固定 jsdelivr CDN + 内联初始化），
    // 其余路径严格 CSP 不变。先做响应头/守卫的原始断言，再真实 Chromium 渲染。
    const cspOf = (r) => (r.headers && r.headers["content-security-policy"]) || "";
    const docsHdr = await rawReq({ path: "/docs", port });
    const redocHdr = await rawReq({ path: "/redoc", port });
    const oapiHdr = await rawReq({ path: "/openapi.json", port });
    record("docs-paths-use-docs-csp",
      [docsHdr, redocHdr, oapiHdr].every((r) => r.status === 200 &&
        cspOf(r).includes("https://cdn.jsdelivr.net") &&
        cspOf(r).includes("'unsafe-inline'") &&
        r.headers["x-content-type-options"] === "nosniff"),
      `status=[${[docsHdr, redocHdr, oapiHdr].map((r) => r.status)}] csp=/docs:${cspOf(docsHdr).slice(0, 80)}…`);

    const idxHdr = await rawReq({ path: "/", port });
    const apiHdr = await rawReq({ path: "/api/status", port });
    record("main-app-csp-unchanged-strict",
      !cspOf(idxHdr).includes("cdn.jsdelivr.net") &&
        cspOf(idxHdr) === cspOf(apiHdr) && cspOf(apiHdr).includes("script-src 'self'"),
      `index==api_status=${cspOf(idxHdr) === cspOf(apiHdr)}`);

    const nearPaths = ["/docsx", "/docs/", "/redocs", "/openapi.jsonx", "/docs%2f"];
    const nearResp = [];
    for (const p of nearPaths) nearResp.push(await rawReq({ path: p, port }));
    record("docs-csp-exact-path-only",
      nearResp.every((r) => !cspOf(r).includes("cdn.jsdelivr.net")),
      `paths=${JSON.stringify(nearPaths.map((p, i) => [p, nearResp[i].status]))}`);

    const docsBadHost = await rawReq({ path: "/docs", port, headers: { Host: "evil.example" } });
    const docsEvilOrigin = await rawReq({ path: "/docs", port, headers: { Origin: "http://evil.example" } });
    record("docs-still-host-origin-gated",
      docsBadHost.status === 403 && docsEvilOrigin.status === 403,
      `badHost=${docsBadHost.status} evilOrigin=${docsEvilOrigin.status}`);

    // 真实 Chromium：文档页实际渲染 + 初始化（Swagger UI / Redoc 从 openapi.json
    // 生成可见 DOM），而非仅检查 CSP 字符串。CDN 不可达时以本地 route 夹具替换
    // 外部资源重试——该模式只证明策略/初始化兼容，真实 CDN 资源标记未验证。
    const docsPage = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const docsErrors = [];
    docsPage.on("console", (m) => { if (m.type() === "error") docsErrors.push(m.text()); });
    docsPage.on("pageerror", (e) => docsErrors.push(`pageerror: ${e.message}`));

    const cdnOk = await cdnReachable();
    let stubsInstalled = false;
    if (!cdnOk) { await installDocsStubs(docsPage); stubsInstalled = true; }

    async function renderDocsPage(docsPath, readyFn, what) {
      const errFrom = docsErrors.length;  // 只看本页本次尝试新增的报错
      const newRefusal = () => docsErrors.slice(errFrom)
        .find((t) => /Content Security Policy|Refused to/i.test(t));
      try {
        await docsPage.goto(base + docsPath, { waitUntil: "load", timeout: 60000 });
        const v = await waitUntil(() => readyFn(docsPage), 15000, what);
        return { ok: true, via: stubsInstalled ? "route-stub" : "real-cdn", ...v };
      } catch (e) {
        const refusal = newRefusal();
        if (refusal) return { ok: false, reason: "csp-refused", detail: refusal.slice(0, 160) };
        // CSP 未拒绝而初始化失败：网络不可达/中途抖动，用本地 route 夹具复验
        // 策略/初始化兼容性（真实 CDN 资源可用性不在该模式证明范围内）
        if (cdnOk) {
          try {
            await installDocsStubs(docsPage);
            stubsInstalled = true;
            await docsPage.goto(base + docsPath, { waitUntil: "load", timeout: 60000 });
            const v = await waitUntil(() => readyFn(docsPage), 15000, `${what}（route 夹具重试）`);
            return { ok: true, via: "route-stub-after-cdn-failed", ...v };
          } catch (e2) {
            const r2 = newRefusal();
            return { ok: false, reason: r2 ? "csp-refused" : "timeout",
                     detail: String(r2 || e2.message || e2).slice(0, 160) };
          }
        }
        return { ok: false, reason: "timeout", detail: String(e.message || e).slice(0, 160) };
      }
    }

    const swagger = await renderDocsPage("/docs", swaggerReady, "/docs Swagger 初始化");
    record("docs-swagger-initializes", swagger.ok,
      swagger.ok
        ? `via=${swagger.via} domLen=${swagger.domLen}${swagger.via !== "real-cdn" ? "（CDN 不可达：真实 Swagger 资源 NOT_VERIFIED）" : ""}`
        : `reason=${swagger.reason} ${swagger.detail || ""}`);
    const redoc = await renderDocsPage("/redoc", redocReady, "/redoc 渲染");
    record("redoc-initializes", redoc.ok,
      redoc.ok
        ? `via=${redoc.via} domLen=${redoc.domLen}${redoc.via !== "real-cdn" ? "（CDN 不可达：真实 Redoc 资源 NOT_VERIFIED）" : ""}`
        : `reason=${redoc.reason} ${redoc.detail || ""}`);

    const oapiBrowser = await docsPage.evaluate(async () => {
      const r = await fetch("/openapi.json");
      const j = await r.json().catch(() => null);
      return { ok: r.ok, title: j && j.info && j.info.title,
               paths: j ? Object.keys(j.paths || {}) : [] };
    });
    record("docs-openapi-json-readable",
      oapiBrowser.ok && oapiBrowser.title === "Fathom" &&
        oapiBrowser.paths.includes("/api/scan") && oapiBrowser.paths.includes("/api/reveal"),
      `title=${oapiBrowser.title} paths=${oapiBrowser.paths.length}`);

    const docsViolations = docsErrors.filter((t) => /Content Security Policy|Refused to/i.test(t));
    record("docs-pages-no-csp-violations", docsViolations.length === 0,
      docsViolations.slice(0, 2).join("; ").slice(0, 200));
    docsSummary = { cdn_reachable: cdnOk, swagger: swagger, redoc: redoc,
                    console_errors: docsErrors.length };
  } finally {
    // ---------- 5. 清理并复核 ----------
    if (browser) {
      await browser.close().catch(() => {});
      record("chromium-closed", true);
    }
    if (child.pid) {
      child.kill("SIGTERM");
      const code = await new Promise((resolve) => {
        const t = setTimeout(() => { child.kill("SIGKILL"); resolve("killed-9"); }, 10000);
        child.on("exit", (c) => { clearTimeout(t); resolve(c); });
      });
      record("fixture-stopped", code === 0 || code === "killed-9", `exit=${code}`);
    }
    let portFreed = false;
    try {
      await rawReq({ path: "/__fixture", port });
    } catch (e) {
      portFreed = /ECONNREFUSED|ECONNRESET/.test(e.message);
    }
    record("port-released", portFreed, `127.0.0.1:${port}`);
  }

  const failed = checks.filter((c) => !c.ok);
  process.stdout.write(JSON.stringify({
    ok: failed.length === 0,
    passed: checks.length - failed.length,
    failed: failed.length,
    checks,
    fixture: { pid: child.pid, port, fixture_id: identity.fixture_id },
    docs: docsSummary,
  }, null, 2) + "\n");
}

const watchdog = setTimeout(() => {
  process.stderr.write("整体超时（180s），强制退出\n");
  process.exit(1);
}, OVERALL_TIMEOUT_MS);

main().catch((e) => {
  process.stderr.write(`FATAL: ${e.message}\n${e.stack || ""}\n`);
  process.exitCode = 1;
}).finally(() => clearTimeout(watchdog));
