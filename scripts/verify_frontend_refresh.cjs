#!/usr/bin/env node
/* ISS-023 前端回归：随机端口、纯合成 API、真实 Chromium。 */
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
const evidenceDir = fs.mkdtempSync(path.join(os.tmpdir(), "fathom-iss023-"));
const checks = [];

function record(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail: String(detail) });
  process.stderr.write(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` | ${detail}` : ""}\n`);
}

function json(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": data.length,
  });
  res.end(data);
}

function snapshot(id, createdAt) {
  return {
    id, created_at: createdAt, root: ROOT, total_kb: 300000,
    dir_count: 3, denied_count: 0,
    total_bytes: 1024 ** 4, free_bytes: 256 * 1024 ** 3,
  };
}

function createFixture() {
  const state = {
    mode: "dual", version: 2, scanning: false, lastDiff: null,
    staleDiffs: 0, treeSnapshotId: null, scenario: null, counts: {},
  };
  const nextCall = (name) => {
    state.counts[name] = (state.counts[name] || 0) + 1;
    return state.counts[name];
  };
  const later = (res, status, body, ms = 250) => {
    setTimeout(() => json(res, status, body), ms);
  };

  const snapshots = () => {
    if (state.mode === "empty") return [];
    if (state.mode === "single") return [snapshot(1, "2026-09-12T10:00:00")];
    const latest = state.version === 3
      ? snapshot(3, "2026-09-13T12:03:00")
      : snapshot(2, "2026-09-13T10:00:00");
    return [latest, snapshot(1, "2026-09-12T10:00:00")];
  };

  const diff = (a, b) => ({
    a: snapshots().find((s) => String(s.id) === String(a)),
    b: snapshots().find((s) => String(s.id) === String(b)),
    grown: [{ path: `${ROOT}/Build`, old_kb: 100000, new_kb: 101024, delta_kb: 1024 }],
    shrunk: [{ path: `${ROOT}/Archive`, old_kb: 100000, new_kb: 98976, delta_kb: -1024 }],
    added: [], removed: [],
  });

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname === "/__fixture") {
      if (url.searchParams.has("mode")) {
        state.mode = url.searchParams.get("mode");
        state.version = 2;
        state.scanning = false;
        state.lastDiff = null;
        state.staleDiffs = 0;
        state.treeSnapshotId = null;
        state.scenario = null;
        state.counts = {};
      }
      if (url.searchParams.has("scenario")) {
        state.scenario = url.searchParams.get("scenario") || null;
        state.counts = {};
      }
      if (url.searchParams.has("version")) state.version = Number(url.searchParams.get("version"));
      return json(res, 200, state);
    }
    if (url.pathname === "/api/bootstrap") return json(res, 200, { token: TOKEN });
    if (url.pathname === "/api/status") {
      const rows = snapshots();
      return json(res, 200, {
        root: ROOT,
        snapshot_count: rows.length,
        latest_snapshot: rows[0] || null,
        disk: { total_bytes: 1024 ** 4, free_bytes: 256 * 1024 ** 3 },
        db_bytes: 4096,
        scan: {
          running: state.scanning,
          started_at: state.scanning ? "2026-09-13T12:02:00" : null,
          finished_at: state.version === 3 ? "2026-09-13T12:03:00" : null,
        },
        port: server.address().port,
      });
    }
    if (url.pathname === "/api/snapshots") {
      if (state.mode === "snapshots500") return json(res, 500, { detail: "合成快照故障" });
      if (state.scenario === "snapshots-delay" && nextCall("snapshots") === 1) {
        return later(res, 200, snapshots());
      }
      return json(res, 200, snapshots());
    }
    if (url.pathname === "/api/volume-trend") {
      return json(res, 200, snapshots().slice().reverse().map((s) => ({
        created_at: s.created_at,
        total_bytes: s.total_bytes,
        free_bytes: s.free_bytes,
      })));
    }
    if (url.pathname === "/api/diff") {
      if (state.scenario === "overview-old-success-new-500") {
        const call = nextCall("diff");
        if (call === 1) return later(res, 200, diff(1, 2));
        if (call === 2) return json(res, 500, { detail: "较新的合成差分故障" });
      }
      if (state.mode === "error500") return json(res, 500, { detail: "合成差分故障" });
      const rows = snapshots();
      if (rows.length < 2) return json(res, 409, { detail: "至少需要两个快照才能对比" });
      const a = url.searchParams.get("a") || String(rows[1].id);
      const b = url.searchParams.get("b") || String(rows[0].id);
      state.lastDiff = [a, b];
      if (!rows.some((s) => String(s.id) === a) || !rows.some((s) => String(s.id) === b)) {
        state.staleDiffs += 1;
        return json(res, 404, { detail: "快照不存在" });
      }
      if (state.mode === "onlyadded" || state.mode === "addedremoved") {
        const body = diff(a, b);
        body.grown = [];
        body.shrunk = [];
        body.added = [{ path: `${ROOT}/NewCache`, old_kb: 0, new_kb: 150000, delta_kb: 150000 }];
        if (state.mode === "addedremoved") {
          body.removed = [{ path: `${ROOT}/OldCache`, old_kb: 120000, new_kb: 0, delta_kb: -120000 }];
        }
        return json(res, 200, body);
      }
      return json(res, 200, diff(a, b));
    }
    if (url.pathname === "/api/reports") {
      if (state.scenario === "report-old-success-new-500") {
        const call = nextCall("reports");
        if (call === 1) return later(res, 200, { reports: [{ date: "2026-09-11" }] });
        if (call === 2) return json(res, 500, { detail: "较新的合成报告故障" });
      }
      return json(res, 200, { reports: [{ date: state.version === 3 ? "2026-09-13" : "2026-09-12" }] });
    }
    if (url.pathname.startsWith("/api/reports/")) {
      return json(res, 200, { content: "合成日报" });
    }
    if (url.pathname === "/api/trees") {
      if (state.scenario === "tree-browse-race") {
        const call = nextCall("trees");
        const name = call === 1 ? "OldTree" : "NewTree";
        const body = {
          snapshot_id: call === 1 ? 2 : 3, root: ROOT,
          children: [{ name: "root", path: ROOT, value: 300000, children: [
            { name, path: `${ROOT}/${name}`, value: 98976 },
          ] }],
        };
        if (call === 1) return later(res, 200, body);
        return json(res, 200, body);
      }
      const rows = snapshots();
      const sid = rows[0] ? rows[0].id : null;
      state.treeSnapshotId = sid;
      return json(res, 200, sid == null ? { snapshot_id: null, root: null, children: [] } : {
        snapshot_id: sid,
        root: ROOT,
        children: [{ name: "root", path: ROOT, value: 300000, children: [
          { name: "Archive", path: `${ROOT}/Archive`, value: 98976 },
        ] }],
      });
    }
    if (url.pathname === "/api/browse") {
      if (state.scenario === "tree-browse-race") {
        const call = nextCall("browse");
        const name = call === 1 ? "OldChild" : "NewChild";
        const body = {
          path: ROOT, size_kb: 300000, crumbs: [{ name: "root", path: ROOT }],
          children: [{ name, path: `${ROOT}/${name}`, size_kb: 98976, delta_kb: -1024, is_new: false }],
          trend: [{ created_at: "2026-09-13T12:03:00", size_kb: 98976 }],
        };
        if (call === 1) return later(res, 200, body);
        return json(res, 200, body);
      }
      if (state.scenario === "browse-navigation-delay") {
        nextCall("browse");
        return later(res, 200, {
          path: ROOT, size_kb: 300000, crumbs: [{ name: "root", path: ROOT }],
          children: [{ name: "OldAfterNavigation", path: `${ROOT}/OldAfterNavigation`, size_kb: 90000,
            delta_kb: 1024, is_new: false }],
          trend: [],
        });
      }
      if (!snapshots().length) return json(res, 409, { detail: "尚无快照，请先扫描" });
      return json(res, 200, {
        path: ROOT,
        size_kb: 300000,
        crumbs: [{ name: "root", path: ROOT }],
        children: [{
          name: "Archive", path: `${ROOT}/Archive`, size_kb: 98976,
          delta_kb: state.mode === "single" ? null : -1024,
          is_new: false,
        }],
        trend: [{ created_at: "2026-09-12T10:00:00", size_kb: 100000 },
          { created_at: "2026-09-13T12:03:00", size_kb: 98976 }],
      });
    }
    if (url.pathname === "/api/bigfiles") return json(res, 200, { files: [] });
    if (url.pathname === "/api/reveal" && req.method === "POST") return json(res, 200, { ok: true });
    if (url.pathname === "/api/scan" && req.method === "POST") {
      if (req.headers["x-fathom-token"] !== TOKEN) return json(res, 403, { detail: "令牌无效" });
      state.scanning = true;
      setTimeout(() => {
        state.version = 3;
        state.scanning = false;
      }, 150);
      return json(res, 200, { ok: true, run_id: 1 });
    }

    const staticFiles = {
      "/": ["frontend/index.html", "text/html; charset=utf-8"],
      "/app.js": ["frontend/app.js", "application/javascript; charset=utf-8"],
      "/icons.js": ["frontend/icons.js", "application/javascript; charset=utf-8"],
      "/style.css": ["frontend/style.css", "text/css; charset=utf-8"],
      "/vendor/echarts.min.js": ["frontend/vendor/echarts.min.js", "application/javascript; charset=utf-8"],
    };
    const file = staticFiles[url.pathname];
    if (!file) return json(res, 404, { detail: "not found" });
    const data = fs.readFileSync(path.join(REPO, file[0]));
    res.writeHead(200, { "Content-Type": file[1], "Content-Length": data.length });
    res.end(data);
  });
  return { server, state };
}

async function waitForText(page, selector, expected) {
  await page.waitForFunction(
    ({ selector, expected }) => document.querySelector(selector)?.textContent.includes(expected),
    { selector, expected }, { timeout: 10000 });
}

async function main() {
  const fixture = createFixture();
  fixture.server.listen(0, "127.0.0.1");
  await once(fixture.server, "listening");
  const base = `http://127.0.0.1:${fixture.server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    const setMode = async (mode) => {
      const response = await page.request.get(`${base}/__fixture?mode=${mode}`);
      if (!response.ok()) throw new Error(`无法切换夹具模式：${mode}`);
    };
    const setScenario = async (scenario) => {
      const response = await page.request.get(`${base}/__fixture?scenario=${scenario}`);
      if (!response.ok()) throw new Error(`无法切换夹具竞态：${scenario}`);
    };
    const setVersion = async (version) => {
      const response = await page.request.get(`${base}/__fixture?version=${version}`);
      if (!response.ok()) throw new Error(`无法切换夹具版本：${version}`);
    };
    const waitForCount = async (name, count) => {
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        const response = await page.request.get(`${base}/__fixture`);
        const body = await response.json();
        if ((body.counts[name] || 0) >= count) return body;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      throw new Error(`等待夹具调用超时：${name} >= ${count}`);
    };

    await page.goto(`${base}/#/overview`, { waitUntil: "networkidle" });
    await page.waitForSelector("#overview-summary table");
    const visibleValues = await page.evaluate(() => [
      fmtDelta(1024), fmtDelta(-1024), fmtDelta(0), fmtDelta(null),
    ]);
    record("delta-signs-and-unknown",
      JSON.stringify(visibleValues) === JSON.stringify(["+1.0 MB", "−1.0 MB", "0.0 B", "—"]),
      JSON.stringify(visibleValues));
    const overviewIcon = await page.evaluate(() => ({
      literal: document.querySelector("#overview-summary").textContent.includes("icon("),
      svg: Boolean(document.querySelector("#overview-summary [data-reveal] svg")),
    }));
    record("overview-finder-button-is-svg", !overviewIcon.literal && overviewIcon.svg);
    const overviewShot = path.join(evidenceDir, "overview-values-1220x820.png");
    await page.screenshot({ path: overviewShot });

    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.querySelectorAll("#sel-b option").length === 2);
    const before = await page.evaluate(() => ({
      options: [...document.querySelectorAll("#sel-b option")].map((o) => o.value),
      selected: [document.querySelector("#sel-a").value, document.querySelector("#sel-b").value],
    }));
    record("initial-snapshot-selection",
      JSON.stringify(before) === JSON.stringify({ options: ["2", "1"], selected: ["1", "2"] }),
      JSON.stringify(before));
    await page.click("#btn-scan");
    await page.waitForFunction(() =>
      [...document.querySelectorAll("#sel-b option")].map((o) => o.value).join(",") === "3,1",
    null, { timeout: 12000 });
    await waitForText(page, "#diff-status", "已更新或不再可用");
    const after = await page.evaluate(() => ({
      options: [...document.querySelectorAll("#sel-b option")].map((o) => o.value),
      selected: [document.querySelector("#sel-a").value, document.querySelector("#sel-b").value],
      status: document.querySelector("#diff-status").textContent,
      report: document.querySelector("#report-list").textContent,
      canvases: [...document.querySelectorAll("#page-changes canvas")].map((c) => [c.width, c.height]),
    }));
    record("same-day-rescan-reconciles-ids",
      after.options.join(",") === "3,1" && after.selected.join(",") === "1,3" &&
        after.status.includes("已更新或不再可用"), JSON.stringify(after));
    record("diff-and-report-refresh-after-scan",
      fixture.state.lastDiff?.join(",") === "1,3" && fixture.state.staleDiffs === 0 &&
        after.report.includes("2026-09-13") && after.canvases.every(([w, h]) => w > 0 && h > 0));
    const changesShot = path.join(evidenceDir, "changes-after-rescan-1220x820.png");
    await page.screenshot({ path: changesShot });

    await page.click('a[data-page="browse"]');
    await page.waitForURL("**/#/browse");
    await page.waitForSelector("#tbl-browse tbody tr");
    const browse = await page.evaluate(() => ({
      text: document.querySelector("#tbl-browse").textContent,
      literal: document.querySelector("#page-browse").textContent.includes("icon("),
      svg: Boolean(document.querySelector("#tbl-browse [data-reveal] svg")),
      canvases: [...document.querySelectorAll("#page-browse canvas")].map((c) => [c.width, c.height]),
    }));
    record("browse-refreshes-latest-tree-and-svg",
      fixture.state.treeSnapshotId === 3 && browse.text.includes("−1.0 MB") &&
        !browse.literal && browse.svg && browse.canvases.every(([w, h]) => w > 0 && h > 0));
    const browseShot = path.join(evidenceDir, "browse-after-rescan-1220x820.png");
    await page.screenshot({ path: browseShot });

    await setMode("onlyadded");
    await page.goto(`${base}/#/overview`, { waitUntil: "networkidle" });
    await waitForText(page, "#overview-summary", "不能判断为“无变化”");
    const onlyAdded = await page.locator("#overview-summary").textContent();
    record("only-added-is-not-no-change",
      onlyAdded.includes("新增或未记录") && !onlyAdded.includes("期间没有 ≥1MB"));

    await setMode("empty");
    await page.reload({ waitUntil: "networkidle" });
    await waitForText(page, "#overview-summary", "还不能比较");
    record("empty-overview-is-baseline-state",
      (await page.locator("#card-latest").textContent()).includes("尚无快照") &&
        !(await page.locator("#overview-summary").textContent()).includes("加载失败"));
    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await waitForText(page, "#diff-status", "尚无快照");
    const empty = await page.evaluate(() => ({
      options: document.querySelectorAll("#sel-a option").length,
      disabled: document.querySelector("#btn-diff").disabled,
    }));
    record("empty-changes-clears-stale-results", empty.options === 0 && empty.disabled, JSON.stringify(empty));

    await setMode("single");
    await page.reload({ waitUntil: "networkidle" });
    await waitForText(page, "#diff-status", "基线已建立");
    record("single-snapshot-enables-distribution-not-diff",
      await page.locator("#btn-diff").isDisabled());
    await page.goto(`${base}/#/browse`, { waitUntil: "networkidle" });
    await page.waitForSelector("#tbl-browse tbody tr");
    record("single-snapshot-distribution-loads",
      (await page.locator("#tbl-browse").textContent()).includes("Archive") &&
        (await page.locator("#tbl-browse").textContent()).includes("—"));

    await setMode("error500");
    await page.goto(`${base}/#/overview`, { waitUntil: "networkidle" });
    await waitForText(page, "#overview-summary", "HTTP 500");
    const error500 = await page.locator("#overview-summary").textContent();
    record("http-500-is-not-first-scan-or-no-change",
      error500.includes("加载失败") && !error500.includes("需要至少两个快照") &&
        !error500.includes("期间没有 ≥1MB"), error500);

    await setMode("snapshots500");
    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await waitForText(page, "#diff-status", "HTTP 500");
    record("snapshot-500-disables-stale-comparison",
      await page.locator("#btn-diff").isDisabled());

    await setMode("dual");
    await page.route("**/api/diff*", (route) => route.abort("internetdisconnected"));
    await page.goto(`${base}/#/overview`, { waitUntil: "networkidle" });
    await waitForText(page, "#overview-summary", "无法连接本地服务");
    record("network-error-is-explicit",
      !(await page.locator("#overview-summary").textContent()).includes("需要至少两个快照"));
    await page.unroute("**/api/diff*");

    // 旧成功不能覆盖后发错误：同一加载域以 request generation 判定最终写入者。
    await setMode("dual");
    await setScenario("overview-old-success-new-500");
    await page.evaluate(() => { void loadOverviewSummary(); });
    await waitForCount("diff", 1);
    await page.evaluate(() => { void loadOverviewSummary(); });
    await waitForText(page, "#overview-summary", "较新的合成差分故障");
    await page.waitForTimeout(350);
    record("late-overview-success-cannot-overwrite-newer-error",
      (await page.locator("#overview-summary").textContent()).includes("较新的合成差分故障"));

    // 报告列表同样覆盖“旧成功晚于新 500”。
    await setMode("dual");
    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await setScenario("report-old-success-new-500");
    await page.evaluate(() => { void loadReportList(); });
    await waitForCount("reports", 1);
    await page.evaluate(() => { void loadReportList(); });
    await waitForText(page, "#report-list", "较新的合成报告故障");
    await page.waitForTimeout(350);
    record("late-report-success-cannot-overwrite-newer-error",
      (await page.locator("#report-list").textContent()).includes("较新的合成报告故障"));

    // 树与目录浏览器各自使用独立 generation；旧响应比新响应晚到仍被丢弃。
    await setMode("dual");
    await page.goto(`${base}/#/browse`, { waitUntil: "networkidle" });
    await setScenario("tree-browse-race");
    await page.evaluate(() => { void loadTree(); void loadBrowse(null); });
    await waitForCount("trees", 1);
    await waitForCount("browse", 1);
    await page.evaluate(() => { void loadTree(); void loadBrowse(null); });
    await waitForText(page, "#tbl-browse", "NewChild");
    await page.waitForTimeout(350);
    const treeBrowseRace = await page.evaluate(() => {
      const chart = window.echarts.getInstanceByDom(document.getElementById("chart-sunburst"));
      return {
        child: document.querySelector("#tbl-browse").textContent,
        tree: chart?.getOption()?.series?.[0]?.data?.[0]?.name,
      };
    });
    record("late-tree-and-browse-results-are-discarded",
      treeBrowseRace.tree === "NewTree" && treeBrowseRace.child.includes("NewChild") &&
        !treeBrowseRace.child.includes("OldChild"), JSON.stringify(treeBrowseRace));

    // 离开页面后，延迟的目录响应不得再改写隐藏页 DOM。
    await setScenario("browse-navigation-delay");
    await page.evaluate(() => { void loadBrowse(null); });
    await waitForCount("browse", 1);
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.waitForTimeout(350);
    record("navigation-invalidates-late-browse-write",
      !(await page.locator("#tbl-browse").textContent()).includes("OldAfterNavigation"));

    // snapshots 等待期间用户主动改选 [2,1]，响应不得用请求前 [1,2] 覆盖。
    await setMode("dual");
    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.querySelectorAll("#sel-b option").length === 2);
    await setScenario("snapshots-delay");
    await page.evaluate(() => { void loadSnapshotsForDiff(); });
    await waitForCount("snapshots", 1);
    await page.selectOption("#sel-a", "2");
    await page.selectOption("#sel-b", "1");
    await waitForText(page, "#diff-status", "正在对比快照 #2 → #1");
    const delayedSelection = await page.evaluate(() => [
      document.querySelector("#sel-a").value, document.querySelector("#sel-b").value,
    ]);
    record("delayed-snapshot-response-preserves-user-selection",
      delayedSelection.join(",") === "2,1" && fixture.state.lastDiff?.join(",") === "2,1",
      JSON.stringify(delayedSelection));

    // 对比发出前后发生同日替换：先收到 404，再协调快照列表并自动恢复。
    await setMode("dual");
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForFunction(() => document.querySelector("#sel-b").value === "2");
    await setVersion(3);
    await page.click("#btn-diff");
    await page.waitForFunction(() => document.querySelector("#sel-b").value === "3");
    await waitForText(page, "#diff-status", "已更新或不再可用");
    const recovered404 = await page.evaluate(() => ({
      selected: [document.querySelector("#sel-a").value, document.querySelector("#sel-b").value],
      status: document.querySelector("#diff-status").textContent,
    }));
    record("diff-404-coordinates-snapshot-recovery",
      recovered404.selected.join(",") === "1,3" && fixture.state.staleDiffs === 1 &&
        fixture.state.lastDiff?.join(",") === "1,3", JSON.stringify(recovered404));

    await setMode("addedremoved");
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("#tbl-added [data-reveal]");
    await page.waitForSelector("#tbl-removed [data-reveal]");
    const accessibleNames = await page.evaluate(() =>
      ["#tbl-added [data-reveal]", "#tbl-removed [data-reveal]"].map((selector) => {
        const button = document.querySelector(selector);
        return [button.getAttribute("aria-label"), button.getAttribute("title")];
      }));
    record("added-and-removed-actions-have-accessible-names",
      accessibleNames.every(([aria, title]) => aria === "在 Finder 中显示" && title === aria),
      JSON.stringify(accessibleNames));

    record("no-unhandled-page-errors", pageErrors.length === 0, pageErrors.join("; "));
    const failed = checks.filter((c) => !c.ok);
    process.stdout.write(JSON.stringify({
      ok: failed.length === 0,
      passed: checks.length - failed.length,
      failed: failed.length,
      evidence: [overviewShot, changesShot, browseShot],
      checks,
    }, null, 2) + "\n");
    if (failed.length) process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    fixture.server.close();
    await once(fixture.server, "close");
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
