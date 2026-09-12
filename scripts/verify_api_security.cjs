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
 * 4. finally 关闭浏览器与夹具进程，复核端口已释放，输出 JSON 结果摘要；
 *    退出码 0=全部通过。
 */
"use strict";

const { spawn } = require("child_process");
const http = require("http");
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
          resolve({ status: res.statusCode, text: data, json });
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

    // 分布页 Finder 按钮（成功路径：sub 目录真实存在）
    await page.goto(base + "/#/browse", { waitUntil: "networkidle" });
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
