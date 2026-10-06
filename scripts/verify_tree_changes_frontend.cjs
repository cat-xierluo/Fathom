#!/usr/bin/env node
/* ISS-148 树形同级变化回归：真实 FastAPI（隔离 runtime/端口）+ 生产页面实点。
 *
 * 与 verify_frontend_refresh.cjs（纯 Node 合成 API）互补：本脚本对真实
 * /api/diff/children（ISS-147）驱动的生产前端做真实 Chromium 实点，覆盖任务卡
 * 验收——三层展开/聚焦/返回、父 0 子抵消、缺父结构节点、单侧未记录、分页
 * 加载更多、错误重试、改选 a/b 与路径竞态、键盘可达、HTML 路径安全、
 * 980/1220/1440 三视口无页面横向溢出、AI 区与排行/日报次级可达。
 *
 * 隔离边界（TESTING.md）：FATHOM_RUNTIME_DIR / FATHOM_DB / FATHOM_SCAN_ROOT /
 * FATHOM_PORT 全部指向临时目录与空闲端口；种子数据直连隔离库（du 不真实执行）；
 * 起服务前先断言 /health 的 pid 等于自 spawn 进程且 runtime_mode=development
 * （ISS-035B 身份合同）。结束杀掉自起进程并复核端口释放。
 *
 * 用法：node scripts/verify_tree_changes_frontend.cjs [--shots <dir>]
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
// 解释器解析：FATHOM_PYTHON（ci_browser_checks.sh 注入）→ .venv（开发机）→
// .runtime（CI 既有约定）。ci.sh 的预检只保证其一存在，这里不做兜底猜测。
const PY = process.env.FATHOM_PYTHON
  || (fs.existsSync(path.join(REPO, ".venv", "bin", "python"))
    ? path.join(REPO, ".venv", "bin", "python")
    : path.join(REPO, ".runtime", "bin", "python"));

const args = process.argv.slice(2);
const shotsIdx = args.indexOf("--shots");
const SHOTS = shotsIdx >= 0 && args[shotsIdx + 1]
  ? path.resolve(args[shotsIdx + 1])
  : path.join(REPO, "verify-results", "iss148-tree", "shots");
fs.mkdirSync(SHOTS, { recursive: true });

const BASE = "/synthetic";
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

function httpGet(urlPath, port) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port, path: urlPath, method: "GET" },
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
    req.end();
  });
}

/* ---------- 合成快照种子（直连隔离库；du 恒被隔离根替代，不触碰生产） ---------- */

const SEED_PY = `"""ISS-148 浏览器回归种子：多数据集合成快照（隔离运行根断言 fail-closed）。
数据集与反例对应：
- chain    反例1：父+20000/子+18000/孙+2000/曾孙+50（三层展开实点）
- netzero  反例2：zero 父净 0，up +20000 / down -20000 抵消
- hist     反例3：三快照（s1→s2 旧区间 vs 最新 s3）
- gap      缺父结构节点：gap 与 gap/deep 无直接记录，仅叶子实测
- oneside  单侧未记录：kept 未记录 / newcomer 首次记录
- pages    130 兄弟分页（加载更多）
- weird    HTML/引号/换行/中文目录名（路径安全）
"""
import json, os, sqlite3, sys

assert os.environ.get("FATHOM_RUNTIME_DIR", "").startswith(sys.argv[1]), "拒绝在非隔离运行根运行"

from fathom import db

def insert(conn, day, root, entries, min_kb=1024, exclude_names=""):
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count, exclude_names, "
        "confirmed_missing_count, path_unverified_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{day}T12:00:00", root, len(entries) + 1, 0, 0.0,
         max(entries.values(), default=0), min_kb, "full", 0, exclude_names, None, None))
    sid = cur.lastrowid
    conn.executemany("INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                     [(sid, p, s) for p, s in entries.items()])
    conn.execute("INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
                 (sid, 500 * 1024**3, 200 * 1024**3))
    conn.commit()
    return sid

def main(tmp):
    conn = db.connect()
    ids = {}
    ids["chain_a"] = insert(conn, "2026-09-01", "/synthetic/chain", {
        "/synthetic/chain": 100000, "/synthetic/chain/aaa": 1000,
        "/synthetic/chain/child": 82000, "/synthetic/chain/child/grand": 2000,
        "/synthetic/chain/child/grand/great": 4000, "/synthetic/chain/other": 5000})
    ids["chain_b"] = insert(conn, "2026-09-02", "/synthetic/chain", {
        "/synthetic/chain": 120000, "/synthetic/chain/aaa": 1000,
        "/synthetic/chain/child": 100000, "/synthetic/chain/child/grand": 4000,
        "/synthetic/chain/child/grand/great": 4050, "/synthetic/chain/other": 5000})
    ids["zero_a"] = insert(conn, "2026-09-03", "/synthetic/netzero", {
        "/synthetic/netzero": 60000, "/synthetic/netzero/zero": 40000,
        "/synthetic/netzero/zero/up": 10000, "/synthetic/netzero/zero/down": 30000})
    ids["zero_b"] = insert(conn, "2026-09-04", "/synthetic/netzero", {
        "/synthetic/netzero": 60000, "/synthetic/netzero/zero": 40000,
        "/synthetic/netzero/zero/up": 30000, "/synthetic/netzero/zero/down": 10000})
    ids["hist_s1"] = insert(conn, "2026-09-05", "/synthetic/hist",
                            {"/synthetic/hist": 10000, "/synthetic/hist/x": 6000})
    ids["hist_s2"] = insert(conn, "2026-09-06", "/synthetic/hist",
                            {"/synthetic/hist": 11000, "/synthetic/hist/x": 7000})
    ids["hist_s3"] = insert(conn, "2026-09-07", "/synthetic/hist",
                            {"/synthetic/hist": 99000, "/synthetic/hist/x": 95000})
    ids["gap_a"] = insert(conn, "2026-09-08", "/synthetic/gap", {
        "/synthetic/gap": 50000, "/synthetic/gap/deep/leaf": 8000})
    ids["gap_b"] = insert(conn, "2026-09-09", "/synthetic/gap", {
        "/synthetic/gap": 50000, "/synthetic/gap/deep/leaf": 8100})
    ids["one_a"] = insert(conn, "2026-09-10", "/synthetic/oneside", {
        "/synthetic/oneside": 20000, "/synthetic/oneside/kept": 5000})
    ids["one_b"] = insert(conn, "2026-09-11", "/synthetic/oneside", {
        "/synthetic/oneside": 22000, "/synthetic/oneside/newcomer": 7000})
    ids["pages_a"] = insert(conn, "2026-09-12", "/synthetic/pages", {
        **{f"/synthetic/pages/c{i:03d}": 1000 + i for i in range(130)},
        "/synthetic/pages": 300000})
    ids["pages_b"] = insert(conn, "2026-09-13", "/synthetic/pages", {
        **{f"/synthetic/pages/c{i:03d}": 1000 + 2 * i for i in range(130)},
        "/synthetic/pages": 300130})
    names = ["<img src=x onerror=alert(1)>", "line1\\nline2", "tab\\tname", 'quo"te', "中文目录"]
    ids["weird_a"] = insert(conn, "2026-09-14", "/synthetic/weird",
                            {**{f"/synthetic/weird/{n}": 1000 for n in names}})
    ids["weird_b"] = insert(conn, "2026-09-15", "/synthetic/weird",
                            {**{f"/synthetic/weird/{n}": 2000 for n in names}})
    # ISS-149 trend 锚定场景：三快照中间缺测（#17 无 leaf 条目）+ 改排除掩码
    # 形成的新数据集（#19/#20）——历史锚不混入新数据集、旧调用不跨掩码混点。
    ids["tg_s1"] = insert(conn, "2026-09-16", "/synthetic/trendgap",
                          {"/synthetic/trendgap": 10000, "/synthetic/trendgap/leaf": 1000})
    ids["tg_s2"] = insert(conn, "2026-09-17", "/synthetic/trendgap",
                          {"/synthetic/trendgap": 10000})
    ids["tg_s3"] = insert(conn, "2026-09-18", "/synthetic/trendgap",
                          {"/synthetic/trendgap": 10000, "/synthetic/trendgap/leaf": 3000})
    ids["tg_ex1"] = insert(conn, "2026-09-19", "/synthetic/trendgap",
                           {"/synthetic/trendgap": 10000, "/synthetic/trendgap/leaf": 9000},
                           exclude_names="node_modules")
    ids["tg_ex2"] = insert(conn, "2026-09-20", "/synthetic/trendgap",
                           {"/synthetic/trendgap": 10000, "/synthetic/trendgap/leaf": 9500},
                           exclude_names="node_modules")
    counts = {k: conn.execute("SELECT COUNT(*) c FROM entries WHERE snapshot_id=?",
                              (v,)).fetchone()["c"] for k, v in ids.items()}
    conn.close()
    print(json.dumps({"ids": ids, "counts": counts}), flush=True)

main(sys.argv[1])
`;

async function main() {
  // ---------- 1. 隔离运行根 + 种子 ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "iss148-verify-"));
  const runtimeDir = path.join(tmp, "runtime");
  const scanRoot = path.join(tmp, "scanroot");
  fs.mkdirSync(scanRoot, { recursive: true });
  const seedPath = path.join(tmp, "seed.py");
  fs.writeFileSync(seedPath, SEED_PY);

  const port = await freePort();
  const env = {
    ...process.env,
    FATHOM_RUNTIME_DIR: runtimeDir,
    FATHOM_DB: path.join(runtimeDir, "data", "fathom.db"),
    FATHOM_SCAN_ROOT: scanRoot,
    FATHOM_PORT: String(port),
    PYTHONPATH: REPO,
  };
  record("fixture-runtime-isolated", runtimeDir.startsWith(os.tmpdir()) && scanRoot.startsWith(os.tmpdir()),
    `${runtimeDir}`);

  const seed = spawn(PY, [seedPath, tmp], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const seedOut = await new Promise((resolve, reject) => {
    let buf = "";
    seed.stdout.on("data", (c) => (buf += String(c)));
    seed.stderr.on("data", (c) => (buf += String(c)));
    seed.on("exit", (code) => code === 0 ? resolve(buf) : reject(new Error(`seed exit ${code}: ${buf.slice(0, 1500)}`)));
  });
  const seedIds = JSON.parse(seedOut.trim().split("\n").filter((l) => l.startsWith("{")).pop());
  record("fixture-seed-ok",
    Object.keys(seedIds.ids).length === 20 && (seedIds.ids.weird_b === 15) &&
      (seedIds.ids.tg_ex2 === 20),
    `ids=${JSON.stringify(seedIds.ids)}`);

  // ---------- 2. 起真实 serve 并核对身份 ----------
  const child = spawn(PY, ["-m", "fathom", "serve"], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const serveErr = [];
  child.stderr.on("data", (c) => serveErr.push(String(c)));
  const health = await waitUntil(async () => {
    const r = await httpGet("/health", port);
    return r.status === 200 && r.json && r.json.pid ? r.json : null;
  }, 30000, "serve /health");
  record("serve-health-identity",
    health.pid === child.pid && health.runtime_mode === "development" && health.port === port,
    `pid=${health.pid} mode=${health.runtime_mode}`);

  const base = `http://127.0.0.1:${port}`;

  // ---------- 3. 真实 Chromium 实点生产页面 ----------
  let browser = null;
  const consoleErrors = [];
  const dialogs = [];
  try {
    const pw = require("playwright");
    browser = await pw.chromium.launch({ headless: true });
    record("chromium-launched", true);
    const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    // Chromium 对任何 4xx/5xx 响应都会打 "Failed to load resource" 网络日志；
    // 本场景的 400（两步改选的跨数据集瞬时拒绝、browse 最新口径越界）是
    // 已被显式断言的产品行为，单独归类——代码级 console.error/pageerror 仍须为零。
    const resourceErrors = [];
    page.on("console", (m) => {
      if (m.type() !== "error") return;
      const text = m.text();
      if (/^Failed to load resource/.test(text)) { resourceErrors.push(text); return; }
      consoleErrors.push(text);
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
    page.on("dialog", (d) => { dialogs.push(d.message()); d.dismiss(); });

    const rowPaths = () => page.evaluate(() =>
      [...document.querySelectorAll("#changes-body tr.focusable")].map((tr) => tr.dataset.path));
    const rowDepth = (p) => page.evaluate((sel) => {
      const tr = document.querySelector(`#changes-body tr[data-path="${CSS.escape(sel)}"]`);
      return tr ? Number(tr.dataset.depth) : -1;
    }, p);

    /* ---- 基线：默认最新数据集（ISS-149 后最新为 trendgap 排除掩码 #19 → #20）---- */
    await page.goto(`${base}/#/changes`, { waitUntil: "networkidle" });
    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #19 → #20"),
      20000, "默认对比完成");
    record("page-opens-default-diff", true, "状态行完成态出现");

    // weird 基线断言：默认数据集已随 ISS-149 seed 前移，显式切到 #14 → #15
    await page.selectOption("#sel-b", "15");
    await page.selectOption("#sel-a", "14");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #14 → #15"),
      20000, "weird 对比完成");

    const weirdRows = await rowPaths();
    record("tree-root-rows-render", weirdRows.length === 5, `rows=${weirdRows.length}`);
    const evil = "/synthetic/weird/<img src=x onerror=alert(1)>";
    record("tree-weird-names-literal-as-text",
      weirdRows.includes(evil) && weirdRows.includes("/synthetic/weird/中文目录") &&
        weirdRows.includes("/synthetic/weird/line1\nline2") &&
        weirdRows.includes("/synthetic/weird/quo\"te"),
      `rows=${JSON.stringify(weirdRows)}`);
    const injection = await page.evaluate(() => ({
      imgOnerror: document.querySelectorAll("#page-changes img[onerror]").length,
      svgOnload: document.querySelectorAll("#page-changes svg[onload]").length,
      pwned: window.__pwned,
    }));
    record("tree-no-execution-nodes",
      injection.imgOnerror === 0 && injection.svgOnload === 0 && injection.pwned === undefined,
      JSON.stringify(injection));
    const parentHiddenAtRoot = await page.evaluate(() =>
      document.getElementById("tree-parent").hidden &&
      document.getElementById("tree-crumbs").hidden);
    record("tree-root-hides-crumbs-and-parent", parentHiddenAtRoot === true);

    /* ---- chain（#1 → #2）：数值、展开三层、详情、聚焦、面包屑、Esc、键盘 ---- */
    await page.selectOption("#sel-b", "2");
    await page.selectOption("#sel-a", "1");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #1 → #2"),
      20000, "chain 对比完成");
    await page.screenshot({ path: path.join(SHOTS, "chain-root-before-expand.png") });

    const chainRows = await rowPaths();
    // delta 排序（接口语义）：|delta| 降序 → child(+17.6MB) 首位；aaa/other
    // 均 0 并列按名 → aaa 在 other 前
    record("chain-root-rows", chainRows.join("|") === "/synthetic/chain/child|/synthetic/chain/aaa|/synthetic/chain/other",
      JSON.stringify(chainRows));
    const childDelta = await page.evaluate(() => {
      const tr = document.querySelector('#changes-body tr[data-path="/synthetic/chain/child"]');
      return tr ? tr.querySelectorAll("td")[3].textContent.trim() : "";
    });
    record("chain-child-delta-value", childDelta === "+17.6 MB", childDelta);
    const otherDelta = await page.evaluate(() => {
      const tr = document.querySelector('#changes-body tr[data-path="/synthetic/chain/other"]');
      return tr ? tr.querySelectorAll("td")[3].textContent.trim() : "";
    });
    record("chain-zero-delta-renders-value-not-dash", otherDelta === "0.0 B", otherDelta);

    const chevrons = await page.evaluate(() =>
      [...document.querySelectorAll("#changes-body [data-expand]")].map((b) => b.dataset.expand));
    record("chain-chevron-only-on-has-children",
      chevrons.length === 1 && chevrons[0] === "/synthetic/chain/child", JSON.stringify(chevrons));

    // 三层展开：child（第1层）→ grand（第2层）→ great（第3层）
    await page.click('#changes-body [data-expand="/synthetic/chain/child"]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === 1, 10000, "grand 展开");
    await page.click('#changes-body tr[data-path="/synthetic/chain/child/grand"] [data-expand]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand/great")) === 2, 10000, "great 展开");
    const depthSnapshot = await page.evaluate(() =>
      [...document.querySelectorAll("#changes-body tr.focusable")].map((tr) =>
        `${tr.dataset.path}@${tr.dataset.depth}`));
    record("chain-three-levels-expanded",
      depthSnapshot.includes("/synthetic/chain/child@0") &&
        depthSnapshot.includes("/synthetic/chain/child/grand@1") &&
        depthSnapshot.includes("/synthetic/chain/child/grand/great@2"),
      JSON.stringify(depthSnapshot));
    const greatDelta = await page.evaluate(() => {
      const tr = document.querySelector('#changes-body tr[data-path="/synthetic/chain/child/grand/great"]');
      return tr ? tr.querySelectorAll("td")[3].textContent.trim() : "";
    });
    record("chain-great-delta-value", greatDelta === "+50.0 KB", greatDelta);
    await page.screenshot({ path: path.join(SHOTS, "chain-expanded-three-levels.png") });

    // 折叠：收起 grand 后其子级消失
    await page.click('#changes-body tr[data-path="/synthetic/chain/child/grand"] [data-expand]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand/great")) === -1, 10000, "great 折叠");
    record("chain-collapse-hides-subtree", true);

    // 键盘展开：先收起 child 回到收起态，再聚焦箭头按钮 Enter 展开
    await page.click('#changes-body tr[data-path="/synthetic/chain/child"] [data-expand]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === -1, 10000, "child 复位收起");
    await page.focus('#changes-body tr[data-path="/synthetic/chain/child"] [data-expand]');
    await page.keyboard.press("Enter");
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === 1, 10000, "键盘展开 grand");
    record("chain-keyboard-expand", true);
    await page.click('#changes-body tr[data-path="/synthetic/chain/child"] [data-expand]'); // 复位收起
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === -1, 10000, "grand 复位收起");

    // 详情：区间绑定 a/b + 锚定趋势就绪（等 trend/browse 异步完成）
    await page.click('#changes-body tr[data-path="/synthetic/chain/child"] .tree-name');
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("changes-detail").hidden &&
      Boolean(document.querySelector("#detail-trend-chart canvas")) &&
      (document.querySelector("[data-test='tree-detail-trend-latest']")?.textContent || "").includes("锚定 #2") &&
      !document.getElementById("detail-current-info").textContent.includes("加载中")),
      10000, "详情数据就绪");
    const detail = await page.evaluate(() => ({
      path: document.querySelector("#changes-detail .detail-path").textContent,
      stats: document.querySelector("[data-test='tree-detail-range-stats']").textContent,
      trendTitle: document.querySelector("#changes-detail .detail-section h3").textContent,
      latest: document.querySelector("[data-test='tree-detail-trend-latest']")?.textContent || "",
      current: document.getElementById("detail-current-info")?.textContent || "",
      canvas: Boolean(document.querySelector("#detail-trend-chart canvas")),
      closeLabel: document.querySelector("#changes-detail .detail-close")?.getAttribute("aria-label"),
    }));
    record("chain-detail-opens-bound",
      detail.path === "/synthetic/chain/child" &&
        detail.stats.includes("#1 → #2") && detail.stats.includes("80.1 MB") &&
        detail.stats.includes("97.7 MB") && detail.stats.includes("+17.6 MB") &&
        detail.canvas && detail.closeLabel === "关闭详情",
      JSON.stringify(detail).slice(0, 220));
    record("chain-detail-latest-labeled",
      detail.trendTitle.includes("#2 锚定同数据集") && detail.latest.includes("锚定 #2") &&
        detail.current.includes("最新快照"),
      `latest=${detail.latest} current=${detail.current}`);
    await page.screenshot({ path: path.join(SHOTS, "chain-detail-open.png") });

    // 详情 → 聚焦此目录：面包屑 + 本级摘要
    await page.click('[data-test="tree-detail-focus"]');
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("tree-crumbs").hidden &&
      !document.getElementById("tree-parent").hidden), 10000, "聚焦生效");
    const focusState = await page.evaluate(() => ({
      crumbs: [...document.querySelectorAll("#tree-crumbs .crumb")].map((c) => c.dataset.crumb),
      parent: document.getElementById("tree-parent").textContent,
    }));
    record("chain-focus-action-crumbs-parent",
      focusState.crumbs.join("|") === "/synthetic/chain|/synthetic/chain/child" &&
        focusState.parent.includes("80.1 MB") && focusState.parent.includes("+17.6 MB") &&
        focusState.parent.includes("累计值"),
      JSON.stringify(focusState));
    await page.screenshot({ path: path.join(SHOTS, "chain-focused-child.png") });

    // Esc：先关详情，再逐级返回上级
    await page.keyboard.press("Escape");
    await waitUntil(async () => page.evaluate(() => document.getElementById("changes-detail").hidden),
      5000, "Esc 关详情");
    record("chain-esc-closes-detail", true);
    await page.keyboard.press("Escape");
    await waitUntil(async () => page.evaluate(() =>
      document.getElementById("tree-crumbs").hidden), 5000, "Esc 返回根聚焦");
    record("chain-esc-returns-parent-focus", true);
    await page.keyboard.press("Escape"); // 已在根：无动作
    const stillRoot = await page.evaluate(() =>
      document.getElementById("tree-crumbs").hidden &&
      document.querySelectorAll('#changes-body tr[data-path="/synthetic/chain/child"]').length > 0);
    record("chain-esc-at-root-noop", stillRoot === true);

    // 面包屑点击下钻 + 返回
    await page.click('#changes-body [data-focus="/synthetic/chain/child"]');
    await waitUntil(async () => page.evaluate(() => !document.getElementById("tree-crumbs").hidden),
      10000, "聚焦按钮生效");
    await page.click('#tree-crumbs [data-crumb="/synthetic/chain"]');
    await waitUntil(async () => page.evaluate(() => document.getElementById("tree-crumbs").hidden),
      10000, "面包屑返回根");
    record("chain-crumbs-navigate", true);

    // 键盘：行 Enter 打开详情，Esc 关闭且焦点回触发行
    await page.focus('#changes-body tr[data-path="/synthetic/chain/child"]');
    await page.keyboard.press("Enter");
    await waitUntil(async () => page.evaluate(() => !document.getElementById("changes-detail").hidden),
      5000, "行 Enter 详情");
    await page.keyboard.press("Escape");
    await waitUntil(async () => page.evaluate(() => document.getElementById("changes-detail").hidden),
      5000, "Esc 关闭");
    const focusBack = await page.evaluate(() =>
      document.activeElement && document.activeElement.dataset
        ? document.activeElement.dataset.path || "" : "");
    record("chain-keyboard-detail-focus-return",
      focusBack === "/synthetic/chain/child", `active=${focusBack}`);

    // 筛选 changed：隐藏无变化方向并显示说明（aaa/other 均 0 且无变化后代 → 只剩 child）
    await page.selectOption("#changes-filter", "changed");
    await waitUntil(async () => {
      const rows = await rowPaths();
      return rows.length === 1 && rows[0] === "/synthetic/chain/child";
    }, 10000, "changed 筛选生效");
    const noteText = await page.evaluate(() =>
      document.getElementById("tree-note").hidden ? "" : document.getElementById("tree-note").textContent);
    record("chain-filter-changed-hides-note",
      noteText.includes("隐藏") && noteText.includes("全部同级行"), noteText.slice(0, 80));
    await page.selectOption("#changes-filter", "all");
    await waitUntil(async () => (await rowPaths()).length === 3, 10000, "筛选复位");
    // 排序 name：接口升序 → aaa 第一；aria-sort=ascending
    await page.click('#changes-table .th-sort[data-sort="name"]');
    await waitUntil(async () => (await rowPaths())[0] === "/synthetic/chain/aaa", 10000, "name 排序");
    const ariaSort = await page.evaluate(() =>
      document.querySelector('#changes-table .th-sort[data-sort="name"]').closest("th").getAttribute("aria-sort"));
    record("chain-sort-by-name", ariaSort === "ascending", `aria-sort=${ariaSort}`);
    await page.click('#changes-table .th-sort[data-sort="delta"]');
    await waitUntil(async () => (await rowPaths())[0] === "/synthetic/chain/child", 10000, "回 delta 排序");
    record("chain-sort-back-delta", true);

    /* ---- ISS-148 审计返修：树序深度优先（子树紧邻父行）+ 搜索触达已加载子树 ---- */
    // 展开 child → grand → great 形成两层以上分叉（根下还有 aaa/other 两个兄弟）
    await page.click('#changes-body [data-expand="/synthetic/chain/child"]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === 1, 10000, "返修展开 grand");
    await page.click('#changes-body tr[data-path="/synthetic/chain/child/grand"] [data-expand]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand/great")) === 2, 10000, "返修展开 great");
    // DOM 顺序必须严格等于 [父, 子, …, 下一兄弟]：包含关系由邻接表达，
    // 同级之间保持 API 排序（delta 降序：child → aaa → other）
    const dfPaths = await rowPaths();
    record("tree-order-depth-first-adjacent-subtree",
      dfPaths.join("|") ===
        "/synthetic/chain/child|/synthetic/chain/child/grand|/synthetic/chain/child/grand/great|/synthetic/chain/aaa|/synthetic/chain/other",
      JSON.stringify(dfPaths));

    // 搜索触达已加载子树："grand" 命中 child/grand 与 child/grand/great
    // （后者完整路径含 grand），祖先链 child 保留并弱化标注。
    // input 事件同步触发 renderTree，短暂等待后直接断言（不等轮询）。
    await page.fill("#changes-search", "grand");
    await sleep(400);
    const searchNote = await page.evaluate(() =>
      document.getElementById("tree-note").hidden ? "" : document.getElementById("tree-note").textContent);
    const searched = await rowPaths();
    record("search-reaches-loaded-descendants",
      searched.join("|") ===
        "/synthetic/chain/child|/synthetic/chain/child/grand|/synthetic/chain/child/grand/great" &&
        searchNote.includes("子树"),
      `rows=${JSON.stringify(searched)} note=${searchNote.slice(0, 60)}`);
    const searchMark = await page.evaluate(() => ({
      childAncestor: document.querySelector('#changes-body tr[data-path="/synthetic/chain/child"]')
        ?.classList.contains("tree-ancestor-hit") === true,
      grandAncestor: document.querySelector('#changes-body tr[data-path="/synthetic/chain/child/grand"]')
        ?.classList.contains("tree-ancestor-hit") === true,
      childMark: (document.querySelector('#changes-body tr[data-path="/synthetic/chain/child"] .tree-ancestor-mark')
        ?.textContent || ""),
    }));
    record("search-ancestor-chain-weakened",
      searchMark.childAncestor === true && searchMark.grandAncestor === false &&
        searchMark.childMark.includes("子级命中"),
      JSON.stringify(searchMark));
    // 清空搜索：恢复深度优先序且展开状态未丢
    await page.fill("#changes-search", "");
    await sleep(400);
    const restored = await rowPaths();
    record("search-clear-restores-depth-first-order",
      restored.join("|") ===
        "/synthetic/chain/child|/synthetic/chain/child/grand|/synthetic/chain/child/grand/great|/synthetic/chain/aaa|/synthetic/chain/other",
      JSON.stringify(restored));
    // 复位收起，恢复进入后续区块前的状态
    await page.click('#changes-body [data-expand="/synthetic/chain/child"]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === -1, 10000, "返修复位收起");

    /* ---- netzero（#3 → #4）：父 0 子抵消 + changed 保留导航父行 ---- */
    await page.selectOption("#sel-b", "4");
    await page.selectOption("#sel-a", "3");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #3 → #4"),
      20000, "netzero 完成");
    await page.click('#changes-body [data-expand="/synthetic/netzero/zero"]');
    await waitUntil(async () => (await rowDepth("/synthetic/netzero/zero/up")) === 1, 10000, "zero 展开");
    const netzero = await page.evaluate(() => {
      const cell = (p) => {
        const tr = document.querySelector(`#changes-body tr[data-path="${CSS.escape(p)}"]`);
        return tr ? tr.querySelectorAll("td")[3].textContent.trim() : "";
      };
      const badge = (p) => {
        const tr = document.querySelector(`#changes-body tr[data-path="${CSS.escape(p)}"]`);
        return tr ? tr.querySelector(".st").textContent.trim() : "";
      };
      return {
        zero: cell("/synthetic/netzero/zero"), up: cell("/synthetic/netzero/zero/up"),
        down: cell("/synthetic/netzero/zero/down"),
        zeroBadge: badge("/synthetic/netzero/zero"),
      };
    });
    record("netzero-parent-zero-children-offset",
      netzero.zero === "0.0 B" && netzero.up === "+19.5 MB" && netzero.down === "−19.5 MB",
      JSON.stringify(netzero));
    await page.selectOption("#changes-filter", "changed");
    await waitUntil(async () => {
      const rows = await rowPaths();
      return rows.length === 1 && rows[0] === "/synthetic/netzero/zero";
    }, 10000, "changed 保留净 0 父行");
    record("netzero-filter-changed-keeps-zero-parent", true, "净 0 但有变化后代，不被筛选漏掉");
    await page.selectOption("#changes-filter", "all");

    /* ---- 错误与重试：children 中断在表内展示错误，重试恢复（基础完成态不受影响） ---- */
    await page.route("**/api/diff/children*", (r) => r.abort("internetdisconnected"));
    await page.selectOption("#sel-b", "9");
    await page.selectOption("#sel-a", "8");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #8 → #9"),
      20000, "children 中断但 diff 完成态");
    const errRow = await page.evaluate(() => ({
      hasRetry: Boolean(document.querySelector("#changes-body [data-tree-retry]")),
      text: (document.querySelector("#changes-body .tree-error-text")?.textContent || "").slice(0, 60),
    }));
    record("tree-children-error-row-with-retry",
      errRow.hasRetry && errRow.text.includes("无法连接本地服务"), JSON.stringify(errRow));
    await page.unroute("**/api/diff/children*");
    await page.click("#changes-body [data-tree-retry]");
    await waitUntil(async () => (await rowDepth("/synthetic/gap/deep")) === 0, 10000, "重试恢复");
    record("tree-children-retry-recovers", true);

    /* ---- gap（#8 → #9）：缺父结构节点不渲染 0 ---- */
    await page.selectOption("#sel-b", "9");
    await page.selectOption("#sel-a", "8");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #8 → #9"),
      20000, "gap 完成");
    const gapRow = await page.evaluate(() => {
      const tr = document.querySelector('#changes-body tr[data-path="/synthetic/gap/deep"]');
      if (!tr) return null;
      const tds = tr.querySelectorAll("td");
      return { prev: tds[1].textContent.trim(), now: tds[2].textContent.trim(),
        delta: tds[3].textContent.trim(), badge: tr.querySelector(".st").textContent.trim() };
    });
    record("gap-structural-not-zero",
      gapRow && gapRow.prev === "—" && gapRow.now === "—" && gapRow.delta === "—" &&
        gapRow.badge === "结构节点",
      JSON.stringify(gapRow));
    await page.click('#changes-body [data-expand="/synthetic/gap/deep"]');
    await waitUntil(async () => (await rowDepth("/synthetic/gap/deep/leaf")) === 1, 10000, "deep 展开");
    const leafDelta = await page.evaluate(() => {
      const tr = document.querySelector('#changes-body tr[data-path="/synthetic/gap/deep/leaf"]');
      return tr ? tr.querySelectorAll("td")[3].textContent.trim() : "";
    });
    record("gap-leaf-measured-below-structural", leafDelta === "+100.0 KB", leafDelta);

    /* ---- oneside（#10 → #11）：单侧未记录 / 首次记录 ---- */
    await page.selectOption("#sel-b", "11");
    await page.selectOption("#sel-a", "10");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #10 → #11"),
      20000, "oneside 完成");
    const oneside = await page.evaluate(() => {
      const read = (p) => {
        const tr = document.querySelector(`#changes-body tr[data-path="${CSS.escape(p)}"]`);
        if (!tr) return null;
        const tds = tr.querySelectorAll("td");
        return { prev: tds[1].textContent.trim(), now: tds[2].textContent.trim(),
          delta: tds[3].textContent.trim(), badge: tr.querySelector(".st").textContent.trim() };
      };
      return { kept: read("/synthetic/oneside/kept"), newcomer: read("/synthetic/oneside/newcomer") };
    });
    record("oneside-unrecorded-and-first-recorded",
      oneside.kept && oneside.newcomer &&
        oneside.kept.now === "—" && oneside.kept.delta === "—（曾有 4.9 MB）" &&
        oneside.kept.badge === "未记录" &&
        oneside.newcomer.prev === "—" && oneside.newcomer.delta === "—（现有 6.8 MB）" &&
        oneside.newcomer.badge === "首次记录",
      JSON.stringify(oneside));

    /* ---- pages（#12 → #13）：130 兄弟分页与加载更多 ---- */
    await page.selectOption("#sel-b", "13");
    await page.selectOption("#sel-a", "12");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #12 → #13"),
      20000, "pages 完成");
    const firstPage = await rowPaths();
    const moreBtn = await page.$('[data-test="tree-load-more"]');
    record("pages-first-page-100-with-load-more",
      firstPage.length === 100 && moreBtn !== null &&
        (await moreBtn.textContent()).includes("100") &&
        (await moreBtn.textContent()).includes("130"),
      `rows=${firstPage.length}`);
    await moreBtn.click();
    await waitUntil(async () => (await rowPaths()).length === 130, 15000, "加载更多完成");
    const allRows = await rowPaths();
    const moreGone = await page.$('[data-test="tree-load-more"]');
    record("pages-load-more-130-no-duplicates",
      allRows.length === 130 && new Set(allRows).size === 130 && moreGone === null,
      `rows=${allRows.length} dup=${allRows.length - new Set(allRows).size}`);

    /* ---- hist（#5 → #6）：旧区间详情绑定，最新口径另标 ---- */
    await page.selectOption("#sel-b", "6");
    await page.selectOption("#sel-a", "5");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #5 → #6"),
      20000, "hist 完成");
    await page.click('#changes-body tr[data-path="/synthetic/hist/x"] .tree-name');
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("changes-detail").hidden &&
      document.querySelector("#detail-current-info")?.textContent.includes("最新快照")), 10000, "hist 详情");
    const histDetail = await page.evaluate(() => ({
      stats: document.querySelector("[data-test='tree-detail-range-stats']").textContent,
      current: document.getElementById("detail-current-info").textContent,
      latest: document.querySelector("[data-test='tree-detail-trend-latest']")?.textContent || "",
    }));
    record("hist-detail-bound-to-old-range",
      histDetail.stats.includes("#5 → #6") && histDetail.stats.includes("5.9 MB") &&
        histDetail.stats.includes("6.8 MB") && !histDetail.stats.includes("92.8 MB"),
      JSON.stringify(histDetail).slice(0, 200));
    // ISS-149：趋势锚定所选区间 b（#6），说明行给出同数据集窗口与身份，
    // 不再以最新快照（#7）口径标注；browse 区如实说明其绑定最新快照、
    // 不代表所选旧区间（最新数据集为 weird，路径越界 400）
    record("hist-detail-latest-labeled-separately",
      histDetail.latest.includes("锚定 #6") && histDetail.latest.includes("/synthetic/hist") &&
        histDetail.current.includes("最新快照") && histDetail.current.includes("不代表所选"),
      `current=${histDetail.current.slice(0, 80)} latest=${histDetail.latest}`);
    await page.keyboard.press("Escape");

    /* ---- ISS-149：trend 锚定与缺测窗口（真实 /api/trend + 生产详情实点） ---- */
    // 场景（trendgap #16-#20）：同数据集三快照中间缺测（#17 无 leaf 条目），
    // 改排除掩码形成新数据集（#19/#20）——历史锚不混入、旧调用不跨掩码混点。
    const TG_LEAF = "/synthetic/trendgap/leaf";
    const fetchTrend = (qs) => page.evaluate(async (query) => {
      const r = await fetch(`/api/trend?${query}`);
      return r.json();
    }, qs);
    const trendApi17 = await fetchTrend(
      `path=${encodeURIComponent(TG_LEAF)}&anchor_snapshot_id=17`);
    record("trend-api-anchor-window-null-gap",
      trendApi17.anchor_snapshot_id === 17 &&
        trendApi17.points.map((p) => p.snapshot_id).join(",") === "16,17,18" &&
        trendApi17.points[1].size_kb === null && trendApi17.points[1].recorded === false &&
        trendApi17.points[0].recorded === true &&
        trendApi17.points[0].created_at === "2026-09-16T12:00:00" &&
        trendApi17.points[0].size_kb === 1000,
      JSON.stringify(trendApi17).slice(0, 260));
    record("trend-api-dataset-identity-and-window",
      trendApi17.dataset && trendApi17.dataset.root === "/synthetic/trendgap" &&
        trendApi17.dataset.min_kb === 1024 && trendApi17.dataset.exclude_names === "" &&
        trendApi17.total_snapshots === 3 && trendApi17.truncated === false,
      JSON.stringify(trendApi17.dataset));
    const trendApi16 = await fetchTrend(
      `path=${encodeURIComponent(TG_LEAF)}&anchor_snapshot_id=16`);
    record("trend-api-historical-anchor-keeps-old-dataset",
      trendApi16.anchor_snapshot_id === 16 && trendApi16.points.length === 3 &&
        !trendApi16.points.some((p) => p.snapshot_id >= 19) &&
        trendApi16.points[1].size_kb === null,
      JSON.stringify(trendApi16.points.map((p) => p.snapshot_id)));
    const trendLegacy = await fetchTrend(`path=${encodeURIComponent(TG_LEAF)}`);
    record("trend-api-legacy-shape-and-exclude-isolation",
      Array.isArray(trendLegacy.points) && trendLegacy.points.length === 2 &&
        trendLegacy.points.every((p) => p.created_at.slice(0, 10) >= "2026-09-19") &&
        Object.keys(trendLegacy.points[0]).sort().join(",") === "created_at,size_kb" &&
        !Array.isArray(trendLegacy.dataset),
      JSON.stringify(trendLegacy));

    // 详情实点：选 #16 → #18 打开 leaf 详情——缺测断线（data 含 null）
    await page.selectOption("#sel-b", "18");
    await page.selectOption("#sel-a", "16");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #16 → #18"),
      20000, "trendgap 完成");
    await page.click(`#changes-body tr[data-path="${TG_LEAF}"] .tree-name`);
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("changes-detail").hidden &&
      Boolean(document.querySelector("#detail-trend-chart canvas")) &&
      (document.querySelector("[data-test='tree-detail-trend-latest']")?.textContent || "").includes("锚定 #18")),
      10000, "trendgap 详情锚定就绪");
    const trendTitle = await page.evaluate(() =>
      document.querySelector("#changes-detail .detail-section h3")?.textContent || "");
    record("trendgap-detail-title-anchored",
      trendTitle.includes("#18 锚定同数据集"), trendTitle);
    const trendOpt = await page.evaluate(() => {
      const chart = echarts.getInstanceByDom(document.getElementById("detail-trend-chart"));
      const opt = chart.getOption();
      return { data: opt.series[0].data, connectNulls: opt.series[0].connectNulls,
        cats: opt.xAxis[0].data };
    });
    record("trendgap-chart-null-gap-not-connected",
      trendOpt.connectNulls === false && trendOpt.data.length === 3 &&
        trendOpt.data[0] === 1000 && trendOpt.data[1] === null && trendOpt.data[2] === 3000,
      JSON.stringify(trendOpt));
    record("trendgap-x-categories-full-timestamps",
      trendOpt.cats.length === 3 && trendOpt.cats[0] === "2026-09-16T12:00:00" &&
        trendOpt.cats[1] === "2026-09-17T12:00:00",
      JSON.stringify(trendOpt.cats));

    // 等价读数表：三行、缺测行标"—（未记录）"不补 0、键盘聚焦行可读
    const tableState = await page.evaluate(() => {
      const host = document.getElementById("detail-trend-table-host");
      const rows = [...host.querySelectorAll("[data-test='detail-trend-row']")];
      return {
        hidden: host.hidden,
        n: rows.length,
        firstAria: rows[0] ? rows[0].getAttribute("aria-label") || "" : "",
        midText: rows[1] ? rows[1].textContent || "" : "",
        midAria: rows[1] ? rows[1].getAttribute("aria-label") || "" : "",
      };
    });
    record("trendgap-table-rows-first-last-readable",
      tableState.hidden === false && tableState.n === 3 &&
        tableState.firstAria.includes("2026-09-16 12:00:00") &&
        tableState.firstAria.includes("快照 #16") && tableState.firstAria.includes("大小"),
      JSON.stringify(tableState).slice(0, 260));
    record("trendgap-table-gap-row-not-zero",
      tableState.midText.includes("—") && !tableState.midText.includes("2.9 MB") &&
        tableState.midAria.includes("未记录"),
      `${tableState.midText} | aria=${tableState.midAria}`);
    const midFocusAria = await page.evaluate(() => {
      const row = document.querySelectorAll("#detail-trend-table-host [data-test='detail-trend-row']")[1];
      row.focus();
      return document.activeElement === row
        ? (row.getAttribute("aria-label") || "") : "";
    });
    record("trendgap-table-keyboard-readable-gap",
      midFocusAria.includes("2026-09-17 12:00:00") && midFocusAria.includes("未记录"),
      midFocusAria);

    // 真实鼠标悬停（不调 formatter/showTip）：首点完整时间+快照号+容量，
    // 缺测点明确"未记录（缺测）"——不冒充实测读数。
    await page.locator("#detail-trend-chart").scrollIntoViewIfNeeded();
    const hoverXY = await page.evaluate(() => {
      const target = document.getElementById("detail-trend-chart");
      const chart = echarts.getInstanceByDom(target);
      const box = target.getBoundingClientRect();
      const xs = [0, 1].map((idx) => chart.convertToPixel({ xAxisIndex: 0 }, idx));
      return xs.map((x) => ({ x: box.x + x, y: box.y + box.height / 2 }));
    });
    const trendShots = [];
    const expectedHover = [
      { date: "2026-09-16T12:00:00", sid: "#16", size: "1000.0 KB" },
      { date: "2026-09-17T12:00:00", sid: "#17", size: "未记录（缺测）" },
    ];
    for (const [idx, exp] of expectedHover.entries()) {
      await page.mouse.move(hoverXY[idx].x, hoverXY[idx].y);
      await page.waitForTimeout(350);
      const tooltipText = await page.locator("#detail-trend-chart").innerText();
      record(`trendgap-hover-point-${idx + 1}`,
        tooltipText.includes(exp.date) && tooltipText.includes(exp.sid) &&
          tooltipText.includes(exp.size),
        JSON.stringify({ expected: exp, tooltipText }));
      const shot = path.join(SHOTS, `iss149-trend-gap-point-${idx + 1}.png`);
      await page.screenshot({ path: shot });
      trendShots.push(shot);
    }

    // 竞态：详情打开且 trend 响应被延迟时改选 a/b——详情关闭，迟到旧响应
    // 不得恢复旧曲线；新区间重新打开后锚随新区间（#20，排除掩码数据集）。
    // continue 需吞掉 unroute 自动放行后的二次处理（Playwright 语义）。
    await page.keyboard.press("Escape");
    await page.route("**/api/trend*", (route) => {
      setTimeout(() => { route.continue().catch(() => {}); }, 1200);
    });
    await page.click(`#changes-body tr[data-path="${TG_LEAF}"] .tree-name`);
    await waitUntil(async () => page.evaluate(() => !document.getElementById("changes-detail").hidden),
      10000, "延迟详情打开");
    await page.selectOption("#sel-b", "20");
    await page.selectOption("#sel-a", "19");

    await waitUntil(async () => page.evaluate(() => document.getElementById("changes-detail").hidden),
      10000, "改选后详情关闭");
    await page.unroute("**/api/trend*");
    await sleep(1600);  // 迟到的旧 trend 响应到达
    const staleTrend = await page.evaluate(() => ({
      hidden: document.getElementById("changes-detail").hidden,
      rows: document.querySelectorAll("#detail-trend-table-host [data-test='detail-trend-row']").length,
    }));
    record("trend-race-stale-response-not-restored",
      staleTrend.hidden === true && staleTrend.rows === 0,
      JSON.stringify(staleTrend));
    // 新区间重新打开：锚 = #20（排除掩码数据集），身份说明可见
    await page.click(`#changes-body tr[data-path="${TG_LEAF}"] .tree-name`);
    await waitUntil(async () => page.evaluate(() =>
      (document.querySelector("[data-test='tree-detail-trend-latest']")?.textContent || "").includes("锚定 #20")),
      10000, "新区间锚定就绪");
    const exDetail = await page.evaluate(() => ({
      latest: document.querySelector("[data-test='tree-detail-trend-latest']")?.textContent || "",
      cats: (() => {
        const chart = echarts.getInstanceByDom(document.getElementById("detail-trend-chart"));
        return chart ? chart.getOption().xAxis[0].data : [];
      })(),
    }));
    record("trendgap-excluded-dataset-anchored-on-reswitch",
      exDetail.latest.includes("锚定 #20") && exDetail.latest.includes("node_modules") &&
        exDetail.cats.length === 2 && exDetail.cats[0] === "2026-09-19T12:00:00",
      JSON.stringify(exDetail).slice(0, 220));
    await page.keyboard.press("Escape");

    /* ---- 竞态：快速改选 a/b，迟到的旧层级/详情响应不混入 ---- */
    await page.selectOption("#sel-b", "2");
    await page.selectOption("#sel-a", "1");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #1 → #2"),
      20000, "回 chain");
    // 给 pages 根层请求注入 2s 延迟，然后先选 pages 再立即改选 chain
    await page.route("**/api/diff/children*", (route) => {
      const u = new URL(route.request().url());
      if (u.searchParams.get("path") === "/synthetic/pages") {
        setTimeout(() => route.continue(), 2000);
      } else {
        route.continue();
      }
    });
    await page.selectOption("#sel-b", "13");
    await page.selectOption("#sel-a", "12");

    await page.selectOption("#sel-b", "2");
    await page.selectOption("#sel-a", "1");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #1 → #2"),
      20000, "chain 再对比");
    await sleep(2600);  // 等迟到的 pages 响应到达
    const afterRace = await rowPaths();
    record("race-stale-level-response-discarded",
      afterRace.includes("/synthetic/chain/child") && !afterRace.some((p) => p.startsWith("/synthetic/pages")),
      `rows=${afterRace.slice(0, 4).join(",")}…`);
    await page.unroute("**/api/diff/children*");

    // 详情区间竞态：打开 chain child 详情后改选区间，详情立即失效且不被迟到响应复活
    await page.click('#changes-body tr[data-path="/synthetic/chain/child"] .tree-name');
    await waitUntil(async () => page.evaluate(() => !document.getElementById("changes-detail").hidden),
      5000, "详情打开（竞态前）");
    await page.selectOption("#sel-b", "4");
    await page.selectOption("#sel-a", "3");

    await sleep(800);
    const detailGone = await page.evaluate(() => document.getElementById("changes-detail").hidden);
    record("race-detail-invalidated-on-range-change", detailGone === true);

    /* ---- AI 解读区 / 排行与日报次级可达（既有逻辑只保不破） ---- */
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("analysis-panel").hidden &&
      Boolean(document.querySelector("[data-test='analysis-state-disabled']"))),
      15000, "AI 解读区未启用态");
    const analysisState = await page.evaluate(() => ({
      hidden: document.getElementById("analysis-panel").hidden,
      disabled: Boolean(document.querySelector("[data-test='analysis-state-disabled']")),
    }));
    record("analysis-panel-preserved",
      analysisState.hidden === false && analysisState.disabled === true,
      JSON.stringify(analysisState));
    const reportHint = await page.evaluate(() =>
      (document.getElementById("report-list").textContent || "").length > 0);
    record("reports-region-reachable", reportHint === true);
    await page.click('[data-test="changes-tab-grown"]');
    await waitUntil(async () => page.evaluate(() =>
      Boolean(document.querySelector("#chart-grown canvas")) &&
      document.querySelector("#tbl-grown-top tbody").textContent.includes("/synthetic/netzero/zero/up")),
      10000, "增长分区渲染");
    record("grown-tab-secondary-reachable", true);
    await page.click('[data-test="changes-tab-detail"]');

    /* ---- 三视口无页面横向溢出；窄窗详情可见且有返回 ---- */
    // 场景：chain 三层展开 + 详情打开（最重状态）
    await page.selectOption("#sel-b", "2");
    await page.selectOption("#sel-a", "1");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #1 → #2"),
      20000, "chain 复位");
    await page.click('#changes-body [data-expand="/synthetic/chain/child"]');
    await waitUntil(async () => (await rowDepth("/synthetic/chain/child/grand")) === 1, 10000, "展开复位");
    await page.click('#changes-body tr[data-path="/synthetic/chain/child/grand"] .tree-name');
    await waitUntil(async () => page.evaluate(() => !document.getElementById("changes-detail").hidden),
      5000, "详情复位");
    for (const [w, h] of [[980, 640], [1220, 820], [1440, 900]]) {
      await page.setViewportSize({ width: w, height: h });
      await sleep(350);
      const geo = await page.evaluate(() => ({
        docOverflow: document.scrollingElement.scrollWidth > window.innerWidth,
        detailVisible: !document.getElementById("changes-detail").hidden,
        detailW: document.getElementById("changes-detail").getBoundingClientRect().width,
        closeVisible: Boolean(document.querySelector("#changes-detail .detail-close")?.getClientRects().length),
      }));
      record(`viewport-${w}x${h}-no-horiz-overflow-and-detail`,
        geo.docOverflow === false && geo.detailVisible === true &&
          geo.detailW <= w && geo.closeVisible === true,
        JSON.stringify(geo));
      // ISS-148 审计返修：窄断点（≤980，现有最小窗口 980×640）详情改全宽
      // （≥95% 视口宽，真实全宽而非仅 ≤窗口）；宽窗保留侧栏形态
      if (w <= 980) {
        record(`viewport-${w}-detail-full-width`,
          geo.detailW >= w * 0.95,
          `detailW=${geo.detailW} 期望≥${Math.round(w * 0.95)}`);
      } else {
        record(`viewport-${w}-detail-sidebar-kept`,
          geo.detailW > 0 && geo.detailW <= 480,
          `detailW=${geo.detailW}（宽窗保留侧栏形态）`);
      }
      await page.screenshot({ path: path.join(SHOTS, `viewport-${w}x${h}.png`) });
    }
    await page.keyboard.press("Escape");

    /* ---- weird 详情：恶意路径在详情内仍为纯文本 ---- */
    await page.selectOption("#sel-b", "15");
    await page.selectOption("#sel-a", "14");

    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #14 → #15"),
      20000, "weird 复位");
    await page.click('#changes-body tr[data-path="/synthetic/weird/中文目录"] .tree-name');
    await waitUntil(async () => page.evaluate(() => !document.getElementById("changes-detail").hidden),
      5000, "weird 详情");
    const weirdDetail = await page.evaluate(() => ({
      path: document.querySelector("#changes-detail .detail-path").textContent,
      imgs: document.querySelectorAll("#changes-detail img").length,
    }));
    record("weird-detail-path-as-text",
      weirdDetail.path === "/synthetic/weird/中文目录" && weirdDetail.imgs === 0,
      JSON.stringify(weirdDetail));

    record("no-console-errors", consoleErrors.length === 0,
      `${consoleErrors.slice(0, 3).join("; ")}${resourceErrors.length ? `（另有 ${resourceErrors.length} 条预期 4xx 网络日志，不计入）` : ""}`);
    record("no-dialogs", dialogs.length === 0, dialogs.join("; "));
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
      record("chromium-closed", true);
    }
    // ---------- 4. 进程回收与端口释放 ----------
    if (child.pid) {
      child.kill("SIGTERM");
      const exited = await new Promise((resolve) => {
        const t = setTimeout(() => { child.kill("SIGKILL"); resolve("killed-9"); }, 10000);
        child.on("exit", (code) => { clearTimeout(t); resolve(code); });
      });
      record("serve-stopped", exited === 0 || exited === "killed-9", `exit=${exited}`);
    }
    let portFreed = false;
    try {
      await httpGet("/health", port);
    } catch (e) {
      portFreed = /ECONNREFUSED|ECONNRESET/.test(e.message);
    }
    record("port-released", portFreed, `127.0.0.1:${port}`);
    process.stderr.write(`tmp 根保留供复查：${tmp}\n`);
  }

  const failed = checks.filter((c) => !c.ok);
  process.stdout.write(JSON.stringify({
    ok: failed.length === 0,
    passed: checks.length - failed.length,
    failed: failed.length,
    checks,
    shots: SHOTS,
  }, null, 2) + "\n");
}

const watchdog = setTimeout(() => {
  process.stderr.write("整体超时（300s），强制退出\n");
  process.exit(1);
}, 300000);

main().catch((e) => {
  process.stderr.write(`FATAL: ${e.message}\n${e.stack || ""}\n`);
  process.exitCode = 1;
}).finally(() => clearTimeout(watchdog));
