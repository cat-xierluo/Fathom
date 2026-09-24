#!/usr/bin/env node
/* ISS-087 夹具截图：5 张覆盖设置页 IA 重做的关键状态。
 * 截图存 verify-results/iss087-preview/（gitignore 已排除，运行产物）。
 * 浏览器态（无 Tauri 桥）跑全套，验证视觉合同 + 实际可达性。 */
"use strict";

const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { once } = require("events");
const { chromium } = require("playwright");

const REPO = path.resolve(__dirname, "..");
const ROOT = "/fixture/root";
const TOKEN = "iss023-fixture-token";
const EVIDENCE = path.join(REPO, "verify-results", "iss087-preview");
fs.mkdirSync(EVIDENCE, { recursive: true });

function json(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": data.length,
    "Cache-Control": "no-store",
  });
  res.end(data);
}

function snapshot(id, createdAt, overrides = {}) {
  return {
    id, created_at: createdAt, root: ROOT, total_kb: 300000,
    dir_count: 3, denied_count: 0, collection_status: "full",
    total_bytes: 1024 ** 4, free_bytes: 256 * 1024 ** 3,
    ...overrides,
  };
}

function createFixture() {
  const state = {
    mode: "dual", version: 2, scanning: false,
    config: {
      scan_root: ROOT, scan_time: "13:30",
      min_kb: 5120, free_alert_gb: 3.5,
      exclude_names: ["*.noindex"],
      sources: { scan_root: "default", scan_time: "settings", min_kb: "settings", free_alert_gb: "settings", exclude_names: "settings" },
      defaults: { scan_root: "/fixture/home", scan_time: "12:00", min_kb: 10240, free_alert_gb: 10 },
      policies: { keep_daily_days: 21, keep_weekly_weeks: 8, du_timeout_s: 14400, bigfile_default_days: 7, bigfile_default_mb: 100 },
      settings_path: "/fixture/runtime/settings.json",
      service_reload_state: { state: "drift", registered_scan_time: "12:00", current_scan_time: "13:30" },
    },
  };
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname === "/api/bootstrap") return json(res, 200, { token: TOKEN });
    if (url.pathname === "/api/status") {
      return json(res, 200, {
        root: ROOT, snapshot_count: 2,
        latest_snapshot: snapshot(2, "2026-09-13T10:00:00"),
        disk: { total_bytes: 1024 ** 4, free_bytes: 256 * 1024 ** 3 },
        db_bytes: 4096,
        scan: { running: false, started_at: null, finished_at: "2026-09-13T10:00:42" },
        port: server.address().port,
        runtime: { runtime_dir: "/fixture/runtime", db_path: "/fixture/runtime/fathom.db" },
      });
    }
    if (url.pathname === "/api/config") return json(res, 200, { ...state.config });
    if (url.pathname === "/api/scan/status") {
      return json(res, 200, {
        running: false, started_at: null, finished_at: "2026-09-13T10:00:42",
        source: "api", message: "成功 · du 耗时 42 秒",
        runs: [
          { id: 2, started_at: "2026-09-13T10:00:00", finished_at: "2026-09-13T10:00:42",
            status: "done", source: "scheduled", phase: "scan", snapshot_id: 2, report_status: "ok",
            notification_status: "ok", pruned_count: 0, message: "成功 · du 耗时 42 秒" },
          { id: 1, started_at: "2026-09-12T12:00:00", finished_at: "2026-09-12T12:00:51",
            status: "done", source: "scheduled", phase: "scan", snapshot_id: 1, report_status: "ok",
            notification_status: "ok", pruned_count: 0, message: "成功 · du 耗时 51 秒" },
        ],
      });
    }
    const staticFiles = {
      "/": ["frontend/index.html", "text/html; charset=utf-8"],
      "/app.js": ["frontend/app.js", "application/javascript; charset=utf-8"],
      "/icons.js": ["frontend/icons.js", "application/javascript; charset=utf-8"],
      "/style.css": ["frontend/style.css", "text/css; charset=utf-8"],
      "/vendor/echarts.min.js": ["frontend/vendor/echarts.min.js", "application/javascript; charset=utf-8"],
    };
    const file = staticFiles[url.pathname] ||
      (/^\/modules\/[A-Za-z0-9_][A-Za-z0-9_./-]*\.js$/.test(url.pathname)
        ? [`frontend${url.pathname}`, "application/javascript; charset=utf-8"]
        : null);
    if (!file) return json(res, 404, { detail: "not found" });
    const data = fs.readFileSync(path.join(REPO, file[0]));
    res.writeHead(200, { "Content-Type": file[1], "Content-Length": data.length });
    res.end(data);
  });
  return { server, state };
}

async function main() {
  const fixture = createFixture();
  fixture.server.listen(0, "127.0.0.1");
  await once(fixture.server, "listening");
  const base = `http://127.0.0.1:${fixture.server.address().port}`;
  const browser = await chromium.launch({ headless: true });
  const browserPage = await browser.newPage({ viewport: { width: 1220, height: 820 } });
  const packagedPage = await browser.newPage({ viewport: { width: 1220, height: 820 } });
  await packagedPage.addInitScript(`
    window.__tauriMock = { invokes: [], handlers: {} };
    Object.defineProperty(window, "__TAURI__", { value: {
      core: { invoke: (cmd, args) => {
        window.__tauriMock.invokes.push({ cmd, args });
        if (cmd === "autostart_status") return Promise.resolve({ scan: "enabled", web: "enabled", login_item: "disabled" });
        if (cmd === "updater_check") return Promise.resolve({ state: "up_to_date", current_version: "0.3.2" });
        return Promise.resolve();
      } },
      event: { listen: () => Promise.resolve(0) },
    }, configurable: true });
  `);

  try {
    /* ===== 浏览器态 ===== */
    await browserPage.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    // ISS-087：默认 = 监控，settings-table 在「高级与诊断」下不可见。
    // 截图前用 evaluate 直接点 nav 绕过 Playwright 可见性闸门（用户真实交互
    // 中 nav 永远 sticky 在 page-container 顶部可见）。

    // 1) 默认监控 section
    await browserPage.waitForSelector("#config-form");
    await browserPage.screenshot({
      path: path.join(EVIDENCE, "01-settings-monitoring-default.png"),
      fullPage: true,
    });

    // 2) 计划与通知 section
    await browserPage.evaluate(() => {
      document.querySelector('.settings-nav-item[data-section="schedule"]')?.click();
    });
    await browserPage.waitForSelector("#scan-history table, #scan-history .hint");
    await browserPage.screenshot({
      path: path.join(EVIDENCE, "02-settings-schedule-section.png"),
      fullPage: true,
    });

    // 3) 浏览器态高级与诊断：默认全量渲染，无折叠区
    await browserPage.evaluate(() => {
      document.querySelector('.settings-nav-item[data-section="advanced"]')?.click();
    });
    await browserPage.waitForSelector("#settings-table");
    await browserPage.screenshot({
      path: path.join(EVIDENCE, "03-settings-advanced-browser-full.png"),
      fullPage: true,
    });

    /* ===== 打包态（mock 桥）：默认折叠 + 关于区 ===== */
    await packagedPage.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    // 打包态默认进入「监控」section，折叠区在「高级与诊断」下默认 closed；
    // 截图前先切到 advanced 让 settings-table 与折叠区可见。
    await packagedPage.waitForSelector("#config-form");
    await packagedPage.evaluate(() => {
      document.querySelector('.settings-nav-item[data-section="advanced"]')?.click();
    });
    await packagedPage.waitForSelector("#advanced-panel details[data-test='advanced-toggle']");
    await packagedPage.screenshot({
      path: path.join(EVIDENCE, "04-settings-advanced-packaged-collapsed.png"),
      fullPage: true,
    });

    // 5) 关于区（打包态：含品牌位 + 检查更新面板）
    await packagedPage.evaluate(() => {
      document.querySelector('.settings-nav-item[data-section="about"]')?.click();
    });
    await packagedPage.waitForSelector('[data-test="about-panel"]');
    await packagedPage.waitForSelector("#updater-panel");
    await packagedPage.screenshot({
      path: path.join(EVIDENCE, "05-settings-about-section.png"),
      fullPage: true,
    });

    console.log(`5 screenshots saved to ${EVIDENCE}`);
  } finally {
    await browser.close();
    fixture.server.close();
    await once(fixture.server, "close");
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
