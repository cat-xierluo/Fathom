#!/usr/bin/env node
/* ISS-156 设置「范围与覆盖」分区 + 首次启用引导回归。
 *
 * 真实 FastAPI 隔离入口 + /health pid 断言 + 生产设置页实点
 * （Playwright 单实例串行，不并行开浏览器）。
 *
 * 覆盖任务卡验收：
 * 1. 空库 / 旧 HOME 升级 / 单快照 → 设置 → 保存 → 明确首扫引导；
 * 2. 排除、部分权限、锁定或未挂载卷、计划预览失败（合成桩各态，
 *    只注入受控故障，不替代生产模块/API 调用）；
 * 3. 保存成功变更 UI、保存失败与 409 冲突旧值可辨、运行中改设置只影响
 *    下一轮的文案；
 * 4. AI 授权 / 更新恢复入口无退化（既有 settings 断言不破）；
 * 5. 980/1220/1440 视口 + 键盘焦点。
 *
 * 隔离边界（TESTING.md）：FATHOM_RUNTIME_DIR / FATHOM_DB / FATHOM_SCAN_ROOT /
 * FATHOM_PORT 全部指向临时目录与空闲端口；起服务前断言 /health 的 pid 等于
 * 自 spawn 进程且 runtime_mode=development；结束杀掉自起进程并复核端口释放。
 *
 * 用法：node scripts/verify_scope_settings_frontend.cjs [--shots <dir>]
 * 退出码 0 = 全部通过；结果 JSON 打到 stdout（ok/passed/failed/checks）。
 */
"use strict";

const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const REPO = path.resolve(__dirname, "..");
const PY = process.env.FATHOM_PYTHON
  || (fs.existsSync(path.join(REPO, ".venv", "bin", "python"))
    ? path.join(REPO, ".venv", "bin", "python")
    : path.join(REPO, ".runtime", "bin", "python"));

const args = process.argv.slice(2);
const shotsIdx = args.indexOf("--shots");
const SHOTS = shotsIdx >= 0 && args[shotsIdx + 1]
  ? path.resolve(args[shotsIdx + 1])
  : path.join(REPO, "verify-results", "iss156-scope-settings", "shots");
fs.mkdirSync(SHOTS, { recursive: true });

const checks = [];
function record(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail: String(detail) });
  process.stderr.write(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` | ${detail}` : ""}\n`);
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
    } catch (e) { lastErr = e.message; }
    await sleep(120);
  }
  throw new Error(`等待超时: ${what}${lastErr ? `（${lastErr}）` : ""}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function httpJson(method, urlPath, port, { body, headers } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port, path: urlPath, method, headers },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          let json = null;
          try { json = JSON.parse(data); } catch (_) { /* 非 JSON */ }
          resolve({ status: res.statusCode, json });
        });
      });
    req.on("error", reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

async function main() {
  // ---------- 1. 隔离运行根（合成根，不开系统权限面板） ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "iss156-verify-"));
  const runtimeDir = path.join(tmp, "runtime");
  const scanRoot = path.join(tmp, "scanroot");
  const customRoot = path.join(tmp, "custom");
  fs.mkdirSync(scanRoot, { recursive: true });
  fs.mkdirSync(customRoot, { recursive: true });
  fs.writeFileSync(path.join(customRoot, "sample.bin"), Buffer.alloc(64 * 1024));
  // macOS /var -> /private/var realpath：服务端按 realpath 归一后回显，
  // 断言必须用同一口径，否则把正常归一化误读成不一致。
  const customReal = fs.realpathSync(customRoot);
  const scanReal = fs.realpathSync(scanRoot);
  record("fixture-runtime-isolated",
    runtimeDir.startsWith(os.tmpdir()) && scanRoot.startsWith(os.tmpdir()),
    runtimeDir);

  const port = await freePort();
  const env = {
    ...process.env,
    FATHOM_RUNTIME_DIR: runtimeDir,
    FATHOM_DB: path.join(runtimeDir, "data", "fathom.db"),
    FATHOM_SCAN_ROOT: scanRoot,
    FATHOM_PORT: String(port),
    PYTHONPATH: REPO,
  };

  // ---------- 2. 起真实 serve 并核对身份 ----------
  const child = spawn(PY, ["-m", "fathom", "serve"], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const serveErr = [];
  child.stderr.on("data", (c) => serveErr.push(String(c)));
  let browser = null;
  try {
    const health = await waitUntil(async () => {
      const r = await httpJson("GET", "/health", port);
      return r.status === 200 && r.json && r.json.pid ? r.json : null;
    }, 30000, "serve /health");
    record("serve-health-identity",
      health.pid === child.pid && health.runtime_mode === "development" && health.port === port,
      `pid=${health.pid} mode=${health.runtime_mode} port=${health.port}`);

    const boot = await httpJson("GET", "/api/bootstrap", port);
    const TOKEN = boot.json && boot.json.token;
    record("bootstrap-token-ok", Boolean(TOKEN));
    const jput = (p, obj) => httpJson("PUT", p, port, {
      body: JSON.stringify(obj),
      headers: { "Content-Type": "application/json", "X-Fathom-Token": TOKEN },
    });

    // ---------- 3. 真实 HTTP 合同（旧 HOME 未启用范围能力） ----------
    const view0 = await httpJson("GET", "/api/storage/plan/preview", port);
    record("http-old-home-not-enabled",
      view0.status === 200 && view0.json.enabled === false &&
        view0.json.selection === null &&
        String(view0.json.hint || "").includes("旧单根口径"),
      JSON.stringify(view0.json).slice(0, 200));

    const prevCustom = await httpJson(
      "GET", `/api/storage/plan/preview?mode=custom_directory&roots=${encodeURIComponent(customRoot)}`, port);
    const plans = prevCustom.json?.plan?.plans || [];
    record("http-preview-no-side-effect",
      prevCustom.status === 200 && plans.length === 1 &&
        plans[0].root === customReal && Boolean(plans[0].scope_id) &&
        Boolean(plans[0].plan_id) &&
        prevCustom.json.plan.read_limits?.du_timeout_s > 0 &&
        prevCustom.json.plan.budget?.min_kb > 0,
      JSON.stringify(plans).slice(0, 200));

    const afterPreview = await httpJson("GET", "/api/storage/plan/preview", port);
    record("http-preview-does-not-save",
      afterPreview.json.enabled === false && afterPreview.json.selection === null,
      `enabled=${afterPreview.json.enabled}`);

    // 409：expected_revision 与当前生效版本不一致 → 旧值不动
    const conflict = await jput("/api/storage/scope", {
      mode: "custom_directory", roots: [customRoot], expected_revision: 7,
    });
    const afterConflict = await httpJson("GET", "/api/storage/plan/preview", port);
    record("http-409-conflict-keeps-old",
      conflict.status === 409 && afterConflict.json.enabled === false &&
        afterConflict.json.selection === null,
      `status=${conflict.status} detail=${String(conflict.json?.detail || "").slice(0, 80)}`);

    // 400：未知字段/非法模式 → 不落盘
    const bad = await jput("/api/storage/scope", { mode: "nope", roots: [customRoot] });
    const afterBad = await httpJson("GET", "/api/storage/plan/preview", port);
    record("http-400-invalid-no-side-effect",
      bad.status === 400 && afterBad.json.selection === null,
      `status=${bad.status}`);

    // 真实保存：revision 1
    const saved = await jput("/api/storage/scope", {
      mode: "custom_directory", roots: [customRoot],
    });
    const savedScope = saved.json?.scope?.selection;
    record("http-save-revision-and-plan",
      saved.status === 200 && saved.json.applied === true &&
        saved.json.triggers_scan === false &&
        savedScope?.revision === 1 &&
        saved.json.plan?.plans?.[0]?.root === customReal &&
        String(saved.json.hint || "").includes("下一轮"),
      JSON.stringify(savedScope).slice(0, 200));

    // ---------- 4. 真实 Chromium 实点生产设置页 ----------
    const consoleErrors = [];
    const apiCalls = { scopePut: 0, scopeScan: 0 };
    const pw = require("playwright");
    browser = await pw.chromium.launch({ headless: true });
    record("chromium-launched", true);
    const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const resourceErrors = [];
    page.on("console", (m) => {
      if (m.type() !== "error") return;
      const text = m.text();
      if (/^Failed to load resource/.test(text)) { resourceErrors.push(text); return; }
      consoleErrors.push(text);
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
    page.on("dialog", (d) => d.dismiss());
    page.on("request", (r) => {
      const u = r.url();
      if (u.includes("/api/storage/scope") && r.method() === "PUT") apiCalls.scopePut += 1;
      if (u.endsWith("/api/scan") && r.method() === "POST") apiCalls.scopeScan += 1;
    });

    const base = `http://127.0.0.1:${port}`;
    const openScope = async () => {
      await page.goto(`${base}/#/settings/scope`, { waitUntil: "networkidle" });
      await page.click("[data-test='settings-nav-scope']");
      await waitUntil(async () =>
        await page.isVisible("[data-test='scope-panel'] [data-test='scope-preview-btn']"),
        15000, "范围分区可见");
      await waitUntil(async () =>
        Boolean(await page.textContent("[data-test='scope-current']")) &&
        !(await page.textContent("[data-test='scope-current']")).includes("读取中"),
        15000, "生效范围回填");
    };

    /* ---- A. 生产页面：生效范围/修订可读，发现≠已监控 ---- */
    await openScope();
    const cur = await page.evaluate(() => {
      const el = document.querySelector("[data-test='scope-current']");
      return { text: el?.textContent.replace(/\s+/g, " ").trim() || "", enabled: el?.dataset.enabled };
    });
    record("page-current-scope-revision-visible",
      cur.enabled === "1" && cur.text.includes("revision") === false &&
        cur.text.includes("1") && cur.text.includes(customRoot),
      cur.text.slice(0, 200));

    const note = await page.textContent("[data-test='scope-discovery-note']");
    record("page-discovery-not-monitored",
      note.includes("发现结果") && note.includes("不代表已在监控"),
      note.replace(/\s+/g, " ").trim().slice(0, 160));

    /* ---- B. 预览（真实端点）显示身份/限制/不确定性，不改配置 ---- */
    await page.fill("[data-test='scope-roots']", customReal);
    await page.click("[data-test='scope-preview-btn']");
    await waitUntil(async () => Boolean(await page.$("[data-test='scope-preview']")),
      15000, "计划预览渲染");
    const plan = await page.evaluate(() => ({
      table: document.querySelector(".scope-plan-table")?.textContent.replace(/\s+/g, " ").trim() || "",
      limits: document.querySelector("[data-test='scope-preview-limits']")?.textContent.replace(/\s+/g, " ").trim() || "",
      uncertainty: document.querySelector("[data-test='scope-preview-uncertainty']")?.textContent.replace(/\s+/g, " ").trim() || "",
      feedback: document.querySelector("[data-test='scope-feedback']")?.textContent.replace(/\s+/g, " ").trim() || "",
    }));
    record("page-preview-identity-limits-uncertainty",
      plan.table.includes(customReal) &&
        /范围身份|计划身份/.test(plan.table) &&
        plan.limits.includes("du 超时") && plan.limits.includes("min_kb") &&
        plan.limits.includes("身份版本") &&
        plan.uncertainty.includes("耗时不确定") &&
        plan.feedback.includes("不改任何配置") && plan.feedback.includes("不触发扫描"),
      JSON.stringify(plan).slice(0, 260));

    const revisionAfterPreview = await httpJson("GET", "/api/storage/plan/preview", port);
    record("page-preview-no-save",
      revisionAfterPreview.json.selection?.revision === 1 && apiCalls.scopePut === 0,
      `rev=${revisionAfterPreview.json.selection?.revision} put=${apiCalls.scopePut}`);

    /* ---- C. 保存成功 → UI 才变更 + 明确首扫引导 ---- */
    await page.fill("[data-test='scope-roots']", `${customReal}\n${scanReal}`);
    await page.click("[data-test='scope-save-btn']");
    await waitUntil(async () => Boolean(await page.$("[data-test='scope-firstrun']")),
      15000, "首扫引导出现");
    const savedUi = await page.evaluate(() => ({
      feedback: document.querySelector("[data-test='scope-feedback']")?.textContent.replace(/\s+/g, " ").trim() || "",
      kind: document.querySelector("[data-test='scope-feedback']")?.dataset.kind || "",
      cur: document.querySelector("[data-test='scope-current']")?.textContent.replace(/\s+/g, " ").trim() || "",
      firstrun: document.querySelector("[data-test='scope-firstrun']")?.textContent.replace(/\s+/g, " ").trim() || "",
      expect: document.querySelector("[data-test='scope-snapshot-expect']")?.textContent.replace(/\s+/g, " ").trim() || "",
      legacy: document.querySelector("[data-test='scope-legacy-note']")?.textContent.replace(/\s+/g, " ").trim() || "",
    }));
    record("page-save-success-updates-ui",
      savedUi.kind === "ok" && savedUi.feedback.includes("下一轮") &&
        (savedUi.feedback.includes("不触发扫描") || savedUi.feedback.includes("另行显式触发")) &&
        savedUi.cur.includes("2") && savedUi.cur.includes(scanReal),
      JSON.stringify(savedUi).slice(0, 260));
    record("page-firstscan-is-separate-action",
      savedUi.firstrun.includes("独立动作") &&
        savedUi.firstrun.includes("不会") && savedUi.firstrun.includes("自动扫描") &&
        savedUi.expect.includes("单个快照") &&
        savedUi.expect.includes("第二个可比日期") &&
        savedUi.expect.includes("替换") &&
        savedUi.legacy.includes("legacy"),
      JSON.stringify({ fr: savedUi.firstrun, ex: savedUi.expect, lg: savedUi.legacy }).slice(0, 300));
    record("page-save-does-not-trigger-scan", apiCalls.scopeScan === 0, `scan=${apiCalls.scopeScan}`);

    await page.screenshot({ path: path.join(SHOTS, "scope-saved.png") });

    /* ---- D. 保存成功后才变更 UI：预览后改内容不点保存，UI 不动 ---- */
    const revBefore = await httpJson("GET", "/api/storage/plan/preview", port);
    await page.fill("[data-test='scope-roots']", `${scanReal}\n${customReal}`);
    await sleep(400);
    const uiUnchanged = await page.evaluate(() =>
      document.querySelector("[data-test='scope-current']")?.textContent.replace(/\s+/g, " ").trim() || "");
    const revAfter = await httpJson("GET", "/api/storage/plan/preview", port);
    record("page-ui-changes-only-on-save",
      revBefore.json.selection.revision === 2 && revAfter.json.selection.revision === 2 &&
        uiUnchanged.includes(String(revBefore.json.selection.revision)) &&
        apiCalls.scopePut === 1,
      `rev=${revAfter.json.selection.revision} put=${apiCalls.scopePut}`);

    /* ---- E. 409 冲突：旧值可辨（合成桩只注入受控并发故障） ---- */
    // 先让页面回到 revision 2 的视图，再用过期修订号触发 409
    await page.click("[data-test='scope-reload-btn']");
    await waitUntil(async () =>
      (await page.textContent("[data-test='scope-current']")).includes("2"),
      10000, "重读回到 revision 2");
    await page.route("**/api/storage/scope", (route) => {
      if (route.request().method() !== "PUT") return route.continue();
      route.fulfill({
        status: 409, contentType: "application/json",
        body: JSON.stringify({ detail: "范围配置版本冲突：提交的是 1，当前为 2；请重新预览后再保存（旧值未改动）" }),
      });
    });
    await page.fill("[data-test='scope-roots']", customReal);
    await page.click("[data-test='scope-save-btn']");
    await waitUntil(async () => {
      const el = await page.$("[data-test='scope-feedback']");
      return el && (await el.getAttribute("data-kind")) === "error";
    }, 10000, "409 冲突反馈");
    const conflictUi = await page.evaluate(() => ({
      fb: document.querySelector("[data-test='scope-feedback']")?.textContent.replace(/\s+/g, " ").trim() || "",
      cur: document.querySelector("[data-test='scope-current']")?.textContent.replace(/\s+/g, " ").trim() || "",
    }));
    record("page-409-old-value-distinguishable",
      conflictUi.fb.includes("保存冲突") && conflictUi.fb.includes("旧值未改动") &&
        conflictUi.fb.includes("2") && conflictUi.cur.includes("2") &&
        conflictUi.cur.includes(customReal),
      JSON.stringify(conflictUi).slice(0, 260));
    await page.unroute("**/api/storage/scope");
    const revAfterConflict = await httpJson("GET", "/api/storage/plan/preview", port);
    record("page-409-no-write-through", revAfterConflict.json.selection.revision === 2,
      `rev=${revAfterConflict.json.selection.revision}`);

    /* ---- F. 保存失败（服务不可达形态）旧值可辨 ---- */
    await page.route("**/api/storage/scope", (route) => {
      if (route.request().method() !== "PUT") return route.continue();
      route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "settings.json 写入失败（旧文件未改动）：ENOSPC" }) });
    });
    await page.click("[data-test='scope-save-btn']");
    await waitUntil(async () => {
      const t = await page.textContent("[data-test='scope-feedback']");
      return t.includes("保存失败");
    }, 10000, "保存失败反馈");
    const failUi = await page.evaluate(() => ({
      fb: document.querySelector("[data-test='scope-feedback']")?.textContent.replace(/\s+/g, " ").trim() || "",
      cur: document.querySelector("[data-test='scope-current']")?.textContent.replace(/\s+/g, " ").trim() || "",
    }));
    record("page-save-failure-old-value-distinguishable",
      failUi.fb.includes("保存失败") && failUi.fb.includes("保持不变") &&
        failUi.cur.includes("2"),
      JSON.stringify(failUi).slice(0, 240));
    await page.unroute("**/api/storage/scope");

    /* ---- G. 计划预览失败（合成桩）不谎报、当前值不丢 ---- */
    await page.route("**/api/storage/plan/preview?*", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "预览内部错误" }) }));
    await page.click("[data-test='scope-preview-btn']");
    await waitUntil(async () => Boolean(await page.$("[data-test='scope-preview-error']")),
      10000, "预览失败可辨");
    const prevFailUi = await page.evaluate(() => ({
      err: document.querySelector("[data-test='scope-preview-error']")?.textContent.replace(/\s+/g, " ").trim() || "",
      cur: document.querySelector("[data-test='scope-current']")?.textContent.replace(/\s+/g, " ").trim() || "",
    }));
    record("page-preview-failure-visible",
      prevFailUi.err.includes("计划预览失败") &&
        prevFailUi.err.includes("当前生效范围未改动") && prevFailUi.cur.includes("2"),
      JSON.stringify(prevFailUi).slice(0, 240));
    await page.unroute("**/api/storage/plan/preview?*");

    /* ---- H. 发现失败：不得声称 0 未读/可读目录 ---- */
    await page.route("**/api/storage/discovery", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "发现内部错误" }) }));
    await page.click("[data-test='scope-reload-btn']");
    await waitUntil(async () => Boolean(await page.$("[data-test='scope-discovery-error']")),
      10000, "发现失败可辨");
    const discFail = await page.textContent("[data-test='scope-discovery-error']");
    record("page-discovery-failure-not-zero",
      discFail.includes("发现失败") && discFail.includes("不代表") &&
        !discFail.includes("0 个可读") && !discFail.includes("可读目录为 0 "),
      discFail.replace(/\s+/g, " ").trim().slice(0, 200));
    await page.unroute("**/api/storage/discovery");

    /* ---- I. 锁定/未挂载卷：发现到但不可选（合成桩各态） ---- */
    await page.route("**/api/storage/discovery", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        discovery: {
          startup_container: { container_id: "apfs-container:SYNTH", container_reference: "synth-container" },
          startup_volumes: [
            { volume_id: "apfs-volume:SYNTH-DATA", name: "SynthData", roles: ["data"], mount_point: "/Volumes/SynthData", status: "accessible" },
            { volume_id: "apfs-volume:SYNTH-LOCK", name: "SynthLocked", roles: ["data"], mount_point: "/Volumes/SynthLocked", status: "locked", filevault: "on" },
            { volume_id: "apfs-volume:SYNTH-GONE", name: "SynthUnmounted", roles: [], mount_point: null, status: "unmounted" },
          ],
          other_devices: [
            { device_id: "partition:SYNTH-EXT", name: "SynthExt", mount_point: "/Volumes/SynthExt" },
          ],
        },
      }),
    }));
    await page.click("[data-test='scope-reload-btn']");
    await waitUntil(async () =>
      (await page.$$("[data-test='scope-volume']")).length === 3, 10000, "合成卷渲染");
    const vols = await page.evaluate(() => [...document.querySelectorAll("[data-test='scope-volume']")].map((li) => ({
      id: li.dataset.volumeId, status: li.dataset.status,
      checkDisabled: Boolean(li.querySelector("[data-test='scope-volume-check']")?.disabled),
      text: li.textContent.replace(/\s+/g, " ").trim(),
    })));
    record("page-locked-unmounted-not-selectable",
      vols.length === 3 &&
        vols.filter((v) => !v.checkDisabled).length === 1 &&
        vols.find((v) => v.status === "locked")?.checkDisabled === true &&
        vols.find((v) => v.status === "locked")?.text.includes("FileVault") &&
        vols.find((v) => v.status === "unmounted")?.text.includes("未挂载") &&
        (await 0, true),
      JSON.stringify(vols).slice(0, 320));
    record("page-other-volume-independent",
      (await page.textContent("[data-test='scope-device']")).includes("非启动容器"),
      (await page.textContent("[data-test='scope-device']")).replace(/\s+/g, " ").trim().slice(0, 160));
    record("page-other-volume-not-auto-included",
      !(await page.textContent("[data-test='scope-mode-desc']")).includes("自动纳入其它卷"),
      (await page.textContent("[data-test='scope-mode-desc']")).replace(/\s+/g, " ").trim().slice(0, 200));
    await page.unroute("**/api/storage/discovery");
    await page.click("[data-test='scope-reload-btn']");
    await waitUntil(async () => !(await page.$("[data-test='scope-volume']")), 10000, "恢复真实发现");
    // 故障注入阶段（409/500 合成桩）故意产生失败资源响应；「无资源错误」只对
    // 其后的真实端点阶段计数，否则会把受控故障反例读成页面缺陷。
    resourceErrors.length = 0;

    /* ---- J. 排除编辑器无退化（既有 settings 断言不破） ---- */
    await page.click("[data-test='settings-nav-monitoring']");
    await waitUntil(async () => await page.isVisible("#exclude-panel"), 10000, "排除面板在场");
    const excludeOk = await page.evaluate(() => ({
      add: Boolean(document.getElementById("btn-exclude-add")),
      input: Boolean(document.getElementById("exclude-new-input")),
      save: Boolean(document.getElementById("btn-exclude-save")),
    }));
    record("page-exclude-editor-no-regression", excludeOk.add && excludeOk.input && excludeOk.save,
      JSON.stringify(excludeOk));

    /* ---- K. AI 授权 / 更新恢复入口无退化 ---- */
    await page.click("[data-test='settings-nav-analysis']");
    await waitUntil(async () =>
      Boolean(await page.$("[data-test='analysis-detect-btn']")), 10000, "AI 分区在场");
    const aiOk = await page.evaluate(() => ({
      detect: Boolean(document.querySelector("[data-test='analysis-detect-btn']")),
      rows: document.querySelectorAll("[data-test='analysis-runtime-row']").length,
    }));
    record("page-analysis-authorization-no-regression", aiOk.detect && aiOk.rows > 0, JSON.stringify(aiOk));

    await page.click("[data-test='settings-nav-about']");
    await waitUntil(async () => Boolean(await page.$("[data-test='updater-check']")
      || await page.$("#btn-updater-check") || await page.$("[data-test='settings-nav-about']")),
      10000, "关于分区在场");
    const aboutOk = await page.evaluate(() => {
      const sec = document.getElementById("settings-section-about");
      return { visible: sec ? !sec.hidden : false, hasUpdate: /更新/.test(sec?.textContent || "") };
    });
    record("page-updater-recovery-no-regression", aboutOk.visible && aboutOk.hasUpdate,
      JSON.stringify(aboutOk));

    /* ---- L. 键盘焦点：左导航 Tab 可达 + 方向键切换 ---- */
    await page.click("[data-test='settings-nav-scope']");
    await waitUntil(async () => await page.isVisible("[data-test='scope-save-btn']"), 8000, "回到范围分区");
    const focusWalk = await page.evaluate(async () => {
      const nav = document.querySelector("[data-test='settings-nav']");
      const items = [...nav.querySelectorAll(".settings-nav-item")];
      const target = items.findIndex((b) => b.dataset.section === "scope");
      items[target].focus();
      const focused = document.activeElement === items[target];
      items[target].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
      await new Promise((r) => setTimeout(r, 120));
      const after = document.activeElement?.dataset?.section || "";
      return { focused, after, hasScope: target >= 0 };
    });
    record("page-keyboard-nav-focus",
      focusWalk.hasScope && focusWalk.focused && focusWalk.after === "schedule",
      JSON.stringify(focusWalk));

    const tabReachable = await page.evaluate(() => {
      const btn = document.querySelector("[data-test='scope-save-btn']");
      btn.focus();
      return document.activeElement === btn;
    });
    record("page-save-button-keyboard-focusable", tabReachable === true, `focusable=${tabReachable}`);

    /* ---- M. 三视口无横向溢出 ---- */
    const viewports = [[980, 640], [1220, 820], [1440, 900]];
    for (const [w, h] of viewports) {
      await page.setViewportSize({ width: w, height: h });
      await page.click("[data-test='settings-nav-scope']");
      await sleep(350);
      const overflow = await page.evaluate(() => {
        const el = document.documentElement;
        return { scroll: el.scrollWidth, client: el.clientWidth };
      });
      record(`page-viewport-${w}-no-horizontal-overflow`,
        overflow.scroll <= overflow.client + 1,
        `scroll=${overflow.scroll} client=${overflow.client}`);
      if (w === 1220) {
        await page.screenshot({ path: path.join(SHOTS, `scope-${w}.png`), fullPage: false });
      }
    }
    await page.setViewportSize({ width: 1220, height: 820 });

    record("no-page-console-errors", consoleErrors.length === 0,
      consoleErrors.slice(0, 3).join(" | ").slice(0, 300));
    record("no-resource-load-errors", resourceErrors.length === 0,
      resourceErrors.slice(0, 3).join(" | ").slice(0, 200));
  } finally {
    if (browser) await browser.close().catch(() => {});
    child.kill("SIGTERM");
    await sleep(600);
    try { process.kill(child.pid, 0); child.kill("SIGKILL"); } catch (_) { /* 已退出 */ }
  }

  // 端口释放复核（不留自有进程）
  const released = await new Promise((resolve) => {
    const srv = net.createServer();
    srv.on("error", () => resolve(false));
    srv.listen(port, "127.0.0.1", () => srv.close(() => resolve(true)));
  });
  record("serve-port-released", released === true, `port=${port}`);

  const passed = checks.filter((c) => c.ok).length;
  const failed = checks.length - passed;
  // stdout 必须输出结果 JSON（空 stdout 会挂门禁，#249 已踩）
  process.stdout.write(`${JSON.stringify({ ok: failed === 0, passed, failed, checks })}\n`);
}

main().catch((e) => {
  process.stderr.write(`套件异常终止: ${e && e.stack ? e.stack : e}\n`);
  process.stdout.write(`${JSON.stringify({ ok: false, passed: 0, failed: 1, error: String(e && e.message || e) })}\n`);
  process.exit(1);
});
