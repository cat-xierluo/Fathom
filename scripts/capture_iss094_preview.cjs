#!/usr/bin/env node
/* ISS-094 夹具截图：内容页页内二级导航（横向 tab）的前后对比证据。
 * 用法：node scripts/capture_iss094_preview.cjs before|after
 *   before = 改动前基线（变化/分布为单页长滚动，无 tab 条）；
 *   after  = 变化页五分区 tab + 分布页两分区 tab。
 * 产物存 verify-results/iss094-preview/<tag>/（gitignore 已排除，运行产物）。
 * 覆盖：变化页默认 tab 与四个分区 tab、分布页两个 tab、大文件页
 * （调研结论：天然单区块不加 tab，前后同形态对照）。脚本对 tab DOM
 * 做存在性守卫——before（无 .page-tab）不报错、跳过分区截图。
 * 浏览器态（无 Tauri 桥），纯合成 API（夹具形状取自
 * verify_frontend_refresh.cjs 的默认态；diff 的 added/removed 各补一行，
 * 使「新出现 / 消失」分区截图有内容），随机端口，不触真实数据。 */
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
const TAG = process.argv[2] === "before" || process.argv[2] === "after"
  ? process.argv[2]
  : null;
if (!TAG) {
  console.error("用法: node scripts/capture_iss094_preview.cjs before|after");
  process.exit(1);
}
const EVIDENCE = path.join(REPO, "verify-results", "iss094-preview", TAG);
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
    scanning: false,
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
  const SNAP_COLS = ["id", "created_at", "root", "total_kb", "dir_count",
    "denied_count", "min_kb", "collection_status", "vanished_count",
    "exclude_names", "total_bytes", "free_bytes"];
  const snapshots = () => [snapshot(2, "2026-09-13T10:00:00"), snapshot(1, "2026-09-12T10:00:00")];
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
    if (url.pathname === "/api/snapshots") {
      return json(res, 200, snapshots().map((s) => {
        const row = {};
        for (const key of SNAP_COLS) if (key in s) row[key] = s[key];
        return row;
      }));
    }
    if (url.pathname === "/api/volume-trend") {
      return json(res, 200, snapshots().slice().reverse().map((s) => ({
        created_at: s.created_at, total_bytes: s.total_bytes, free_bytes: s.free_bytes,
      })));
    }
    if (url.pathname === "/api/diff") {
      return json(res, 200, {
        a: snapshots()[1], b: snapshots()[0],
        grown: [{ path: `${ROOT}/Build`, old_kb: 100000, new_kb: 101024, delta_kb: 1024 }],
        shrunk: [{ path: `${ROOT}/Archive`, old_kb: 100000, new_kb: 98976, delta_kb: -1024 }],
        added: [{ path: `${ROOT}/NewCache`, old_kb: 0, new_kb: 150000, delta_kb: 150000 }],
        removed: [{ path: `${ROOT}/OldCache`, old_kb: 120000, new_kb: 0, delta_kb: -120000 }],
      });
    }
    if (url.pathname === "/api/reports") return json(res, 200, { reports: [{ date: "2026-09-12" }] });
    if (url.pathname.startsWith("/api/reports/")) return json(res, 200, { content: "合成日报" });
    if (url.pathname === "/api/trees") {
      return json(res, 200, {
        snapshot_id: 2, root: ROOT,
        children: [{ name: "root", path: ROOT, value: 300000, children: [
          { name: "Archive", path: `${ROOT}/Archive`, value: 98976 },
          { name: "Stable", path: `${ROOT}/Stable`, value: 40000 },
        ] }],
        truncated: false, matched_count: 2, node_count: 2, node_limit: 20000,
      });
    }
    if (url.pathname === "/api/browse") {
      return json(res, 200, {
        path: ROOT, size_kb: 300000, crumbs: [{ name: "root", path: ROOT }],
        children: [
          { name: "Archive", path: `${ROOT}/Archive`, size_kb: 98976, delta_kb: -1024, is_new: false },
          { name: "Stable", path: `${ROOT}/Stable`, size_kb: 40000, delta_kb: 0, is_new: false },
        ],
        trend: [{ created_at: "2026-09-12T10:00:00", size_kb: 100000 },
          { created_at: "2026-09-13T12:03:00", size_kb: 98976 }],
      });
    }
    if (url.pathname === "/api/bigfiles") {
      return json(res, 200, {
        state: "ok",
        files: [{ path: `${ROOT}/Build/fathom-disk.img`, size: 2 * 1024 ** 3, mtime: "2026-09-12 09:00" }],
        scope: { root: ROOT, days: 7, min_mb: 100, topn: 50 },
        stats: { wall_ms: 12, peak_rss_bytes: 1392640, find_output_lines: 1,
          find_exit_code: 0, find_stderr_lines: 0, permission_denied_lines: 0 },
        truncated: false, raw_truncated: false,
        expired: false, cached: false, cache_age_s: null, error_message: null,
      });
    }
    if (url.pathname === "/api/config") return json(res, 200, { ...state.config });
    if (url.pathname.startsWith("/api/scan/status")) {
      return json(res, 200, {
        running: false, started_at: null, finished_at: "2026-09-13T12:03:00",
        source: "api", message: "成功 · du 耗时 42 秒",
        runs: [
          { id: 2, started_at: "2026-09-12T12:00:00", finished_at: "2026-09-12T12:00:42",
            status: "done", source: "scheduled", phase: "scan", snapshot_id: 2, report_status: "ok",
            notification_status: "ok", pruned_count: 0, message: "成功 · du 耗时 42 秒" },
          { id: 1, started_at: "2026-09-11T12:00:00", finished_at: "2026-09-11T12:00:51",
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
      "/vendor/echarts.min.js": ["frontend/vendor/echarts.min.js", "text/javascript; charset=utf-8"],
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
  const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
  const shot = (name) => path.join(EVIDENCE, name);
  const saved = [];
  const snap = async (name) => {
    await page.screenshot({ path: shot(name) });
    saved.push(name);
  };
  /* tab 存在性守卫：before（无 tab DOM）返回 false，跳过分区截图。 */
  const clickTab = async (pageId, tabId) => page.evaluate(
    ({ pageId, tabId }) => {
      const btn = document.querySelector(`#page-${pageId} .page-tab[data-tab="${tabId}"]`);
      if (!btn) return false;
      btn.click();
      return true;
    }, { pageId, tabId });

  try {
    /* ===== 变化页：默认视图（before=整页长滚动；after=比对明细 tab）===== */
    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.querySelectorAll("#sel-b option").length === 2);
    await page.waitForFunction(() =>
      (document.querySelector("#diff-status")?.textContent || "").includes("正在对比"));
    await page.waitForTimeout(400);
    await snap("01-changes-default.png");
    /* before：整页滚动形态另存 fullPage（tab 化前「想看增长最多必须翻到下面」的反例证据） */
    await page.screenshot({ path: shot("01b-changes-default-fullpage.png"), fullPage: true });
    saved.push("01b-changes-default-fullpage.png");

    /* 四个分区 tab（after 才存在） */
    for (const [tabId, name] of [
      ["grown", "02-changes-tab-grown.png"],
      ["shrunk", "03-changes-tab-shrunk.png"],
      ["added", "04-changes-tab-added.png"],
      ["removed", "05-changes-tab-removed.png"],
    ]) {
      if (await clickTab("changes", tabId)) {
        await page.waitForTimeout(450);  // 等图表 resize 恢复
        await snap(name);
      }
    }

    /* ===== 分布页：默认视图（before=整页；after=占用分布 tab）===== */
    await page.goto(`${base}/#/browse`, { waitUntil: "networkidle" });
    await page.waitForSelector("#chart-sunburst canvas");
    await page.waitForTimeout(400);
    await snap("06-browse-default.png");
    if (await clickTab("browse", "browser")) {
      await page.waitForSelector("#tbl-browse tbody tr");
      await page.waitForTimeout(450);
      await snap("07-browse-tab-browser.png");
    }

    /* ===== 大文件页：天然单区块，不加 tab（前后同形态对照）===== */
    await page.goto(`${base}/#/bigfiles`, { waitUntil: "networkidle" });
    await page.waitForSelector("#tbl-bigfiles tbody tr");
    await page.waitForTimeout(300);
    await snap("08-bigfiles.png");

    console.log(`${saved.length} screenshots saved to ${EVIDENCE}`);
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
