#!/usr/bin/env node
/* ISS-159 分布页显式快照绑定：真实 serve + 真实 Chromium 实点生产页面。
 *
 * 机制与 verify_directory_bigfiles_frontend.cjs 同源：随机端口、合成快照
 * 种子（纯 DB，无真实文件树）、隔离运行根断言 fail-closed；起服务前断言
 * /health 的 pid 等于自 spawn 进程且 runtime_mode=development；结束杀掉
 * 自起进程并复核端口释放。驱动只经真实 UI（选择器/点击/键盘），不调用
 * 前端内部函数。三视口（980×640 / 1220×820 / 1440×900）截图。
 *
 * 退出码 0 = 全部通过；结果 JSON（ok/passed/failed/checks）打到 stdout，
 * 人类 PASS 行与总结打 stderr。
 *
 * 覆盖（任务卡「先复现/验收」反例 → 断言）：
 *  A1 旧 snapshot 与 latest 容量不同 → 选旧快照后分布/浏览器主读数指该
 *     快照实点，不被最新覆盖；HTTP 同断言
 *  A2 单快照/最早快照无可比前驱 → 差分「未知」如实呈现，不冒充基线
 *  A3 差分次级且区间明确 → meta/列头/详情明示「较 #id」；详情含差分区间说明
 *  A4 多卷/同路径不同身份 → 旧形态（无 snapshot_id）恒最新（HTTP 断言），
 *     显式快照按所选身份约束（越界 400，HTTP 断言）
 *  A5 缺父结构 → ghostparent（无直接入库记录）显示「未直接记录」不填 0，
 *     仍可下钻且子行有实测
 *  A6 HTML 字符路径 → 行文本字面呈现，无注入元素（无 <b> 节点）
 *  A7 分页 → 130 子目录显示「已显示 100/130」，显示更多后无重复无漏项
 *  A8 快照淘汰/路径无记录 → s2 下访问 s3 才有的 crowd 得到 404 说明 +
 *     「恢复到最新快照」一键恢复，恢复后回到同路径最新实点
 *  A9 详情（distribution 单时点）→ 快照/时点/大小/状态/次级差分绑定所选
 *     快照；趋势锚定该快照；直属子目录表渲染并可下钻；Esc 关闭
 *  A10 三桌面尺寸实际布局 → 无横向溢出 + 截图
 */
"use strict";

const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

const REPO = path.resolve(__dirname, "..");
const PY = process.env.FATHOM_PYTHON
  ? path.resolve(process.env.FATHOM_PYTHON)
  : path.join(REPO, ".venv", "bin", "python");

const args = process.argv.slice(2);
const shotsIdx = args.indexOf("--shots");
const SHOTS = shotsIdx >= 0 && args[shotsIdx + 1]
  ? path.resolve(args[shotsIdx + 1])
  : path.join(REPO, "verify-results", "iss159-browse-snapshot", "shots");
fs.mkdirSync(SHOTS, { recursive: true });

const checks = [];
function record(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail: String(detail) });
  process.stderr.write(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` | ${detail}` : ""}\n`);
  if (!ok) process.exitCode = 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* node 侧 CSS 属性选择器值转义（CSS.escape 是浏览器 API，node 无）。 */
const cssAttr = (v) => String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
const rowSel = (p) => `#tbl-browse tbody tr[data-path="${cssAttr(p)}"]`;

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

function httpJson(method, urlPath, port, { headers } = {}) {
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
    req.end();
  });
}

/* ---------- 合成快照种子（纯 DB；隔离运行根断言 fail-closed） ---------- */

const SEED_PY = `"""ISS-159 浏览器回归种子：合成快照（隔离运行根断言）。

三个同数据集快照（/scanroot/demo）+ 一个异身份 decoy 数据集：
- s1（2026-09-21，partial，vanished=2，最早 → 无可比前驱）
- s2（2026-09-22，full）：较 s1 温和增长（差分基线明确）
- s3（2026-09-23，full，latest）：alpha 再增 + crowd 130 子目录（分页）+
  s2 没有的路径（切快照 404 反例）
- ghostparent：s1/s2 均无直接入库记录、仅深层后代（结构节点导航反例）
- <b>&"tricky：HTML 字符路径（转义渲染反例）
"""
import json, os, sqlite3, sys

assert os.environ.get("FATHOM_RUNTIME_DIR", "").startswith(sys.argv[1]), "拒绝在非隔离运行根运行"

from fathom import config, db

scanroot = str(config.DEFAULT_ROOT)
ROOT = os.path.join(scanroot, "demo")
DECOY = os.path.join(scanroot, "decoy")

def insert(conn, day, root, entries, min_kb=1024, status="full", vanished=0):
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count, exclude_names) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"{day}T12:00:00", root, len(entries) + 1, 0, 0.0,
         max(entries.values(), default=0), min_kb, status, vanished, ""))
    sid = cur.lastrowid
    conn.executemany("INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                     [(sid, p, s) for p, s in entries.items()])
    conn.execute("INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
                 (sid, 500 * 1024**3, 200 * 1024**3))
    conn.commit()
    return sid

def main(tmp):
    conn = db.connect()
    s1 = insert(conn, "2026-09-21", ROOT, {
        ROOT: 5_000_000,
        os.path.join(ROOT, "alpha"): 3_000_000,
        os.path.join(ROOT, "beta"): 1_500_000,
        os.path.join(ROOT, "ghostparent", "deepkid"): 900_000,
        os.path.join(ROOT, '<b>&"tricky'): 800_000,
    }, status="partial", vanished=2)
    s2 = insert(conn, "2026-09-22", ROOT, {
        ROOT: 8_000_000,
        os.path.join(ROOT, "alpha"): 6_000_000,
        os.path.join(ROOT, "beta"): 1_200_000,
        os.path.join(ROOT, '<b>&"tricky'): 1_100_000,
        os.path.join(ROOT, "ghostparent", "deepkid"): 950_000,
    })
    s3_entries = {
        ROOT: 8_100_000,
        os.path.join(ROOT, "alpha"): 6_050_000,
        os.path.join(ROOT, "beta"): 1_300_000,
        os.path.join(ROOT, '<b>&"tricky'): 1_000_000,
        os.path.join(ROOT, "crowd"): 400_000,
    }
    for i in range(130):
        s3_entries[os.path.join(ROOT, "crowd", "c%03d" % i)] = 1_000 + i
    s3 = insert(conn, "2026-09-23", ROOT, s3_entries)
    d1 = insert(conn, "2026-09-20", DECOY, {DECOY: 5_000})
    conn.close()
    print(json.dumps({"s1": s1, "s2": s2, "s3": s3, "decoy": d1,
                      "root": ROOT, "scanroot": scanroot}), flush=True)

main(sys.argv[1])
`;

async function main() {
  // ---------- 1. 隔离运行根 + 种子 ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "iss159-verify-"));
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
  record("fixture-runtime-isolated",
    runtimeDir.startsWith(os.tmpdir()) && scanRoot.startsWith(os.tmpdir()),
    runtimeDir);

  const seed = spawn(PY, [seedPath, tmp], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const seedOut = await new Promise((resolve, reject) => {
    let buf = "";
    seed.stdout.on("data", (c) => (buf += String(c)));
    seed.stderr.on("data", (c) => (buf += String(c)));
    seed.on("exit", (code) =>
      code === 0 ? resolve(buf) : reject(new Error(`seed exit ${code}: ${buf.slice(-800)}`)));
  });
  const info = JSON.parse(seedOut.trim().split("\n").filter((l) => l.startsWith("{")).pop());
  const { s1, s2, s3, root: ROOT } = info;
  record("fixture-seed-ok", s1 === 1 && s3 > s2 && s2 > s1,
    `s1=${s1} s2=${s2} s3=${s3} decoy=${info.decoy}`);

  // ---------- 2. 真实 serve（身份断言后继续） ----------
  const child = spawn(PY, ["-m", "fathom", "serve"], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  let serveLog = "";
  child.stderr.on("data", (c) => (serveLog += String(c)));
  const health = await waitUntil(async () => {
    const r = await httpJson("GET", "/health", port);
    if (r.status === 200 && r.json && r.json.pid === child.pid) return r.json;
    return null;
  }, 30000, "serve /health");
  record("serve-health-identity",
    health.pid === child.pid && health.runtime_mode === "development" && health.port === port,
    `pid=${health.pid} mode=${health.runtime_mode}`);

  const base = `http://127.0.0.1:${port}`;
  const get = (p) => httpJson("GET", p, port);

  // ---------- 3. HTTP 层断言（真实 HTTP，不经测试旁路） ----------
  const legacy = await get("/api/browse");
  record("http-legacy-latest-form",
    legacy.status === 200 && String(legacy.json.snapshot_at).startsWith("2026-09-23")
      && legacy.json.delta_kb === 100_000 && !("snapshot" in legacy.json)
      && !("comparison" in legacy.json) && !("pagination" in legacy.json),
    `snapshot_at=${legacy.json.snapshot_at} delta=${legacy.json.delta_kb}`);

  const old1 = await get(`/api/browse?snapshot_id=${s1}`);
  record("http-old-snapshot-bound",
    old1.status === 200 && old1.json.size_kb === 5_000_000
      && old1.json.comparison === null && old1.json.delta_kb === null
      && old1.json.snapshot.collection_status === "partial"
      && old1.json.snapshot.vanished_count === 2,
    `size=${old1.json.size_kb} cmp=${JSON.stringify(old1.json.comparison)}`);

  const cmp2 = await get(`/api/browse?snapshot_id=${s2}`);
  record("http-secondary-delta-explicit",
    cmp2.status === 200 && cmp2.json.comparison
      && cmp2.json.comparison.snapshot_id === s1
      && cmp2.json.delta_kb === 3_000_000,
    `cmp=#${cmp2.json.comparison && cmp2.json.comparison.snapshot_id} delta=${cmp2.json.delta_kb}`);

  const crowd2 = await get(`/api/browse?snapshot_id=${s2}&path=${encodeURIComponent(path.join(ROOT, "crowd"))}`);
  record("http-path-missing-404", crowd2.status === 404, `status=${crowd2.status}`);

  const missing = await get("/api/browse?snapshot_id=999999");
  record("http-snapshot-missing-404", missing.status === 404, `status=${missing.status}`);

  const cross = await get(`/api/browse?snapshot_id=${s1}&path=${encodeURIComponent(path.join(scanRoot, "decoy"))}`);
  record("http-cross-root-400", cross.status === 400, `status=${cross.status}`);

  const page1 = await get(`/api/browse?snapshot_id=${s3}&path=${encodeURIComponent(path.join(ROOT, "crowd"))}`);
  const page2 = await get(`/api/browse?snapshot_id=${s3}&path=${encodeURIComponent(path.join(ROOT, "crowd"))}&cursor=${encodeURIComponent(page1.json.pagination.next_cursor)}`);
  const p1names = page1.json.children.map((c) => c.name);
  const p2names = page2.json.children.map((c) => c.name);
  record("http-pagination-stable",
    page1.json.pagination.total === 130 && p1names.length === 100
      && page2.json.pagination.total === 130 && p2names.length === 30
      && new Set([...p1names, ...p2names]).size === 130,
    `p1=${p1names.length} p2=${p2names.length}`);

  // ---------- 4. 真实 Chromium 实点生产页面 ----------
  let browser = null;
  const consoleErrors = [];
  const dialogs = [];
  try {
    const pw = require("playwright");
    browser = await pw.chromium.launch({ headless: true });
    record("chromium-launched", true);
    const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    page.on("console", (m) => {
      if (m.type() === "error" && !/^Failed to load resource/.test(m.text())) {
        consoleErrors.push(m.text());
      }
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
    page.on("dialog", (d) => { dialogs.push(d.message()); d.dismiss(); });

    await page.goto(`${base}/#/browse`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() =>
      document.querySelector("[data-test='browse-snapshot-select']") &&
      document.querySelector("[data-test='browse-snapshot-select']").options.length >= 3,
      null, { timeout: 20000 });

    // 默认最新快照：选择器与旭日上下文一致指 latest
    const selDefault = await page.$eval("[data-test='browse-snapshot-select']", (el) => el.value);
    record("ui-default-latest", selDefault === String(s3), `sel=${selDefault}`);
    await page.waitForFunction((sid) =>
      document.querySelector("[data-test='sunburst-context']").textContent.includes(`#${sid}`),
      s3, { timeout: 15000 });
    record("ui-sunburst-context-latest", true, `#s${s3}`);

    // ---------- A1+A2+A5+A6：选最早快照（无前驱 → 差分未知） ----------
    await page.selectOption("[data-test='browse-snapshot-select']", String(s1));
    await page.waitForFunction((sid) =>
      document.querySelector("[data-test='sunburst-context']").textContent.includes(`#${sid}`),
      s1, { timeout: 15000 });
    await page.click("[data-test='browse-tab-browser']");
    await page.waitForFunction((txt) =>
      document.getElementById("browser-meta").textContent.includes(txt),
      "无可比前驱快照，差分未知", { timeout: 15000 });
    const metaS1 = await page.$eval("#browser-meta", (el) => el.textContent);
    record("ui-old-snapshot-no-predecessor-unknown",
      metaS1.includes("无可比前驱快照，差分未知") && metaS1.includes(`#${s1}`),
      metaS1.trim());
    const qualityS1 = await page.$eval("[data-test='browse-snapshot-quality']", (el) => el.textContent);
    record("ui-partial-quality-hint",
      qualityS1.includes("部分采集") && qualityS1.includes("2 个路径采集时消失"),
      qualityS1.trim());
    // alpha 实点 3,000,000 KB = "2.9 GB"，不是 latest 的 "5.8 GB"
    const alphaRow = await page.$(rowSel(path.join(ROOT, "alpha")));
    const alphaText = alphaRow ? await alphaRow.innerText() : "";
    record("ui-old-snapshot-real-size",
      alphaText.includes("2.9 GB") && !alphaText.includes("5.8 GB"),
      alphaText.split("\n").slice(0, 2).join(" | "));
    // 结构节点：ghostparent 无直接记录 → 「未直接记录」不填 0
    const ghostRow = await page.$(rowSel(path.join(ROOT, "ghostparent")));
    const ghostText = ghostRow ? await ghostRow.innerText() : "";
    record("ui-structural-not-zero",
      ghostText.includes("未直接记录") && !/GB|MB/.test(ghostText.split("\n")[1] || ""),
      ghostText.split("\n").slice(0, 2).join(" | "));
    // HTML 字符路径：字面呈现，无注入元素
    const trickyRow = await page.$(rowSel(path.join(ROOT, '<b>&"tricky')));
    const trickyName = trickyRow ? await trickyRow.$eval(".dir-name", (el) => el.textContent) : "";
    const bCount = await page.$$eval("#tbl-browse b", (els) => els.length);
    record("ui-html-path-escaped",
      trickyName === '<b>&"tricky' && bCount === 0,
      `name=${trickyName} <b>count=${bCount}`);
    // 列头：无 comparison 时「较前快照」（不暗示基线）
    const thS1 = await page.$eval("#th-delta", (el) => el.textContent);
    record("ui-delta-header-unknown-baseline", thS1 === "较前快照", thS1);

    // ---------- A9：distribution 详情（绑定 s1） ----------
    // 只经真实 UI：点行内「目录详情」按钮（行点击语义是下钻，不是详情）。
    await page.click(`${rowSel(path.join(ROOT, "ghostparent"))} [data-detail]`);
    await page.waitForSelector("[data-test='browse-detail-stats']", { timeout: 15000 });
    const snapCell = await page.$eval("[data-test='browse-detail-snapshot']", (el) => el.textContent);
    const sizeCell = await page.$eval("[data-test='browse-detail-size']", (el) => el.textContent);
    const statusCell = await page.$eval("[data-test='browse-detail-status']", (el) => el.textContent);
    const deltaNote = await page.$eval("[data-test='browse-detail-delta-range']", (el) => el.textContent);
    record("ui-detail-bound-to-old-snapshot",
      snapCell === `#${s1}` && sizeCell.includes("未直接记录")
        && statusCell.includes("结构节点") && deltaNote.includes("差分未知"),
      `snap=${snapCell} size=${sizeCell} status=${statusCell}`);
    // 趋势锚定所选快照
    await page.waitForFunction((sid) =>
      document.querySelector("#browse-detail .detail-section h3").textContent.includes(`#${sid}`),
      s1, { timeout: 15000 });
    // 直属子目录表渲染，行点击在详情内下钻
    await page.waitForSelector("[data-test='browse-detail-children-table']", { timeout: 15000 });
    await page.click("[data-test='browse-detail-children-table'] tbody tr");
    await page.waitForFunction((needle) =>
      document.querySelector("#browse-detail .detail-path").textContent.includes(needle),
      "deepkid", { timeout: 15000 });
    const deepSize = await page.$eval("[data-test='browse-detail-size']", (el) => el.textContent);
    record("ui-detail-drill-down", deepSize.includes("GB") || deepSize.includes("MB"),
      `deepkid size=${deepSize}`);
    // 大文件区口径标注：非快照时点
    const bfHead = await page.$eval("[data-test='detail-bigfiles-section'] h3", (el) => el.textContent);
    record("ui-detail-bigfiles-not-snapshot-time", bfHead.includes("非快照"), bfHead.trim());
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.getElementById("browse-detail").hidden, null, { timeout: 5000 });
    record("ui-detail-esc-closed", true);

    // ---------- A3：s2 差分次级且区间明确 ----------
    await page.selectOption("[data-test='browse-snapshot-select']", String(s2));
    await page.waitForFunction((txt) =>
      document.getElementById("browser-meta").textContent.includes(txt),
      `较 #${s1}`, { timeout: 15000 });
    const thS2 = await page.$eval("#th-delta", (el) => el.textContent);
    const alphaS2 = await page.$(rowSel(path.join(ROOT, "alpha")));
    const alphaS2Text = alphaS2 ? await alphaS2.innerText() : "";
    record("ui-secondary-delta-explicit",
      thS2 === `较 #${s1}` && alphaS2Text.includes("2.9 GB") && alphaS2Text.includes("+"),
      `th=${thS2} row=${alphaS2Text.split("\n").slice(0, 3).join(" | ")}`);

    // ---------- A8：404 恢复（s3 下钻 crowd → 切 s2 该路径无记录） ----------
    await page.selectOption("[data-test='browse-snapshot-select']", String(s3));
    await page.waitForFunction((sid) =>
      document.getElementById("browser-meta").textContent.includes(`#${sid}`),
      s3, { timeout: 15000 });
    const crowdRow = await page.$(rowSel(path.join(ROOT, "crowd")));
    if (crowdRow) {
      await crowdRow.click();
      await page.waitForFunction((needle) =>
        Array.from(document.querySelectorAll("#crumbs .crumb")).some((c) => c.textContent === needle),
        "crowd", { timeout: 15000 });
      await page.selectOption("[data-test='browse-snapshot-select']", String(s2));
      await page.waitForSelector("[data-test='browse-not-found']", { timeout: 15000 });
      const nfText = await page.$eval("[data-test='browse-not-found']", (el) => el.textContent);
      record("ui-404-explained", nfText.includes("无记录"), nfText.trim().slice(0, 80));
      await page.click("[data-test='browse-recover-latest']");
      await page.waitForFunction((sid) =>
        document.getElementById("browser-meta").textContent.includes(`#${sid}`)
          && document.querySelector("[data-test='browse-snapshot-select']").value === String(sid),
        s3, { timeout: 15000 });
      const recoveredPath = await page.$eval("#crumbs .crumb.current", (el) => el.textContent);
      record("ui-404-recover-latest", recoveredPath === "crowd", `crumb=${recoveredPath}`);
    } else {
      record("ui-404-explained", false, "s3 下未找到 crowd 行（前置失败）");
      record("ui-404-recover-latest", false, "前置失败");
    }

    // ---------- A7：分页（s3 crowd 130 子） ----------
    await page.waitForFunction((needle) =>
      document.getElementById("browser-meta").textContent.includes(needle),
      "已显示 100/130", { timeout: 15000 });
    const moreBtn = await page.$("[data-test='browse-more']");
    await moreBtn.click();
    await page.waitForFunction(() => {
      const rows = document.querySelectorAll("#tbl-browse tbody tr");
      const more = document.querySelector("[data-test='browse-more']");
      return rows.length === 130 && !more;
    }, null, { timeout: 15000 });
    const names = await page.$$eval("#tbl-browse tbody tr", (trs) =>
      trs.map((tr) => tr.querySelector(".dir-name")?.textContent).filter(Boolean));
    record("ui-pagination-append-unique",
      names.length === 130 && new Set(names).size === 130,
      `rows=${names.length} unique=${new Set(names).size}`);

    // ---------- A10：三桌面尺寸实际布局 ----------
    for (const [w, h] of [[980, 640], [1220, 820], [1440, 900]]) {
      await page.setViewportSize({ width: w, height: h });
      await sleep(400);
      const overflow = await page.evaluate(() =>
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
          - document.documentElement.clientWidth);
      record(`viewport-${w}x${h}-no-overflow`, overflow <= 2, `overflowPx=${overflow}`);
      await page.screenshot({ path: path.join(SHOTS, `viewport-${w}x${h}.png`) });
    }

    record("ui-console-clean", consoleErrors.length === 0,
      consoleErrors.slice(0, 3).join(" ; ") || "无页面错误");
    record("ui-no-native-dialog", dialogs.length === 0,
      dialogs.join(" ; ") || "无原生弹窗");
  } catch (e) {
    record("browser-flow-error", false, e.message);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }

  // ---------- 5. 回收：杀自起 serve，复核端口释放 ----------
  try { child.kill("SIGTERM"); } catch (_) { /* 已退出 */ }
  const portFreed = await waitUntil(async () => {
    try { await httpJson("GET", "/health", port); return false; }
    catch (_) { return true; }
  }, 10000, "端口释放");
  record("port-released", portFreed, `127.0.0.1:${port}`);

  // ---------- 6. 汇总 ----------
  const failed = checks.filter((c) => !c.ok);
  process.stderr.write(`\n== ISS-159 前端验证：${checks.length - failed.length}/${checks.length} 通过 ==\n`);
  if (failed.length) {
    process.stderr.write("失败项：\n" + failed.map((c) => `- ${c.name}: ${c.detail}`).join("\n") + "\n");
  }
  fs.writeFileSync(path.join(SHOTS, "..", "result.json"), JSON.stringify({
    task: "iss159-browse-snapshot-frontend",
    pass: failed.length === 0,
    total: checks.length, failed: failed.length,
    checks, shots: fs.readdirSync(SHOTS),
    source: require("child_process").execSync("git rev-parse HEAD", { cwd: REPO }).toString().trim(),
  }, null, 2));

  /* 结果 JSON 走 stdout（单行，整体可 JSON.parse；契约同
   * verify_tree_changes_frontend.cjs 的 assert_result_json 消费方式）：
   * 人类 PASS 行与总结留 stderr，ci_browser_checks.sh 对 tee 捕获的
   * stdout 整体解析，只读 ok/passed/failed 判门禁。 */
  process.stdout.write(JSON.stringify({
    ok: failed.length === 0,
    passed: checks.length - failed.length,
    failed: failed.length,
    checks,
  }) + "\n");
}

main().catch((e) => { console.error(e); process.exit(1); });
