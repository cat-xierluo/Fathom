#!/usr/bin/env node
/* ISS-158 整盘总览与变化入口：真实 serve + 真实 Chromium 实点生产总览页。
 *
 * 机制与 verify_browse_snapshot_frontend.cjs 同源：随机端口、隔离运行根
 * 断言 fail-closed（种子自带 FATHOM_RUNTIME_DIR 前缀断言）、起服务前断言
 * /health 的 pid 等于自 spawn 进程且 runtime_mode=development、结束杀自起
 * 进程并复核端口释放。驱动只经真实 UI（选择器/点击/键盘），不调用前端内部
 * 函数。数据全部是**合成落库事实**（纯 DB，不做真实全盘扫描）。
 *
 * 覆盖（任务卡「先复现/验收」反例 → 断言）：
 *  C1 容器占用 +26MB / 可比目录 +20MB → 「未知差额 6MB」且措辞不是可清理
 *     （无「可清理/可回收」误导词），两卷共享 free 不翻倍，重叠根去重。
 *  C2 负差额保号（不称可回收）；仅 legacy statvfs / 样本错时 / 部分失败
 *     各态可辨；失败成员旧值标 stale。
 *  C3 实点「排查这次变化」进入变化页，scope/a/b 一致（b 为摘要真实快照 ID）。
 *  C4 三桌面尺寸无横向溢出、长范围名不撑破布局、键盘可达、图表恢复非零。
 *  C5 legacy 首页（未启用范围能力）不伪装整盘。
 *
 * 退出码 0 = 全部通过；结果 JSON（ok/passed/failed/checks）走 stdout，
 * 人类 PASS 行与总结走 stderr（ci_browser_checks.sh 的 assert_result_json
 * 消费 stdout）。
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
  ? path.resolve(process.env.FATHOM_PYTHON)
  : path.join(REPO, ".venv", "bin", "python");

const args = process.argv.slice(2);
const shotsIdx = args.indexOf("--shots");
const SHOTS = shotsIdx >= 0 && args[shotsIdx + 1]
  ? path.resolve(args[shotsIdx + 1])
  : path.join(REPO, "verify-results", "iss158-storage-overview", "shots");
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
    try { const v = await fn(); if (v) return v; }
    catch (e) { lastErr = e.message; }
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
    const req = http.request({
      host: "127.0.0.1", port, method, path: urlPath,
      headers: { "Content-Type": "application/json", ...(headers || {}) },
    }, (res) => {
      let body = "";
      res.on("data", (c) => { body += String(c); });
      res.on("end", () => {
        let json = null;
        try { json = body ? JSON.parse(body) : null; } catch (_) { json = null; }
        resolve({ status: res.statusCode, json, body });
      });
    });
    req.on("error", reject);
    req.end();
  });
}

/* ------------------------------------------------------------------
 * 种子：两轮同计划事实（容器 26MB 占用增长 vs 目录 20MB 增长 → 未知 6MB）
 * + **兄弟测量根**（clean 相）：.../main 与 .../media 互不重叠、无父子
 *   包含关系 ⇒ 157 R1 的 ``own_round_reasons`` 不触发（无 absorbed_roots），
 *   两侧 comparable=true，6MB 差额真实成立。
 *   旧夹具用父子根（main + main/Downloads）而被吸收，157 R1 对重叠根判
 *   comparable=false 是**正确**行为，错的只是夹具。
 * + **父子重叠测量根**（stale 相）：覆盖「不可简单相加」语义 —— 该相期望
 *   comparable=false 并给出「父子重叠测量根」原因。
 * + 同一容器的第二条卷级样本（共享 free，不得翻倍）
 * + 本轮一个 failed 成员（无快照 → stale）
 * + 用户设置里落 storage_scope（启用整盘范围能力，否则是 legacy 首页）
 * ------------------------------------------------------------------ */
const SEED_PY = `"""ISS-158 浏览器回归种子：整盘摘要事实（隔离运行根断言）。"""
import json, os, sqlite3, sys

assert os.environ.get("FATHOM_RUNTIME_DIR", "").startswith(sys.argv[1]), "拒绝在非隔离运行根运行"

from fathom import config, db

runtime = config.settings_path().parent
scanroot = str(config.DEFAULT_ROOT)
STALE = os.environ.get("ISS158_STALE") == "1"
if STALE:
    # 父子重叠根：子根被吸收 ⇒ comparable=false（覆盖「不可简单相加」）
    ROOT = os.path.join(scanroot, "main")
    KID = os.path.join(scanroot, "main", "Downloads")
    KB_ROUND2 = (20 * 1024, 20 * 1024)
else:
    # 兄弟根：无重叠 ⇒ 两侧可比，20MB 增长全记在第二个根上
    ROOT = os.path.join(scanroot, "main")
    KID = os.path.join(scanroot, "media")
    KB_ROUND2 = (0, 20 * 1024)
CONTAINER = "apfs-container:1111-2222"
MB = 1024 * 1024

# 启用范围能力（真实能力开关走 settings.json，不是页面参数）
# 范围根必须是**真实存在的目录**：config 对 storage_scope 校验是 fail-closed
# （路径不存在/非目录 → ConfigurationError，serve 直接拒绝启动）。
os.makedirs(ROOT, exist_ok=True)
os.makedirs(KID, exist_ok=True)
runtime.mkdir(parents=True, exist_ok=True)
runtime.joinpath("settings.json").write_text(json.dumps({
    "storage_scope": {
        "schema": config.SCOPE_SCHEMA,
        "mode": "custom_directory",
        "roots": [ROOT, KID],
        "scope_ids": ["s-root", "s-kid"],
        "container_id": CONTAINER,
        "identity_version": config.SCOPE_IDENTITY_VERSION,
        "revision": 1,
        "selected_at": "2026-10-01T00:00:00",
    },
}, ensure_ascii=False), encoding="utf-8")

def seed_round(conn, day, kb_tuple, free_bytes, member_status="done", snapshot_status="active"):
    rid = conn.execute(
        "INSERT INTO scan_rounds(started_at, finished_at, status) VALUES (?,?,?)",
        (f"{day}T01:00:00", f"{day}T01:10:00", "full")).lastrowid
    sids = []
    for seq, (root, plan_id, total_kb) in enumerate(
            ((ROOT, "p-root", kb_tuple[0]), (KID, "p-kid", kb_tuple[1]))):
        conn.execute(
            "INSERT OR IGNORE INTO scan_scopes(scope_id, kind, container_id, "
            "mount_path, display_name, created_at) VALUES (?,?,?,?,?,?)",
            (f"scope-{seq}", "apfs_volume", CONTAINER, root, f"v{seq}", "2026-10-01T00:00:00"))
        conn.execute(
            "INSERT OR IGNORE INTO scan_plans(plan_id, scope_id, canonical_root, "
            "metric_version, min_kb, created_at) VALUES (?,?,?,?,?,?)",
            (plan_id, f"scope-{seq}", root, 1, 512, "2026-10-01T00:00:00"))
        sid = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, plan_id, collection_status) VALUES (?,?,1,0,0.1,?,?,'full')",
            (f"{day}T01:0{seq}:00", root, total_kb, plan_id)).lastrowid
        conn.execute("INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                     (sid, root, total_kb))
        # 卷趋势读数（总览次级走势图的数据源；不插则图表显示空态、无 canvas）
        conn.execute("INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) "
                     "VALUES (?,?,?)", (sid, 100 * 1024**3, free_bytes))
        conn.execute(
            "INSERT INTO scan_round_members(round_id, seq, plan_id, scope_id, "
            "snapshot_id, snapshot_status, status, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, seq, plan_id, f"scope-{seq}", sid, snapshot_status, member_status,
             f"{day}T01:0{seq}:00", f"{day}T01:0{seq}:30"))
        sids.append(sid)
    # 容器级样本：free 100MB → 74MB ⇒ 占用 +26MB
    conn.execute(
        "INSERT INTO container_capacity_samples(container_id, total_bytes, "
        "free_bytes, source, sampled_at, round_id) VALUES (?,?,?,?,?,?)",
        (CONTAINER, 100 * 1024**3, free_bytes, "storage-discovery",
         f"{day}T01:09:00", rid))
    # 同容器的第二条**卷级**样本：共享同一份剩余空间，页面不得翻倍
    conn.execute(
        "INSERT INTO container_capacity_samples(container_id, total_bytes, "
        "free_bytes, source, sampled_at, round_id) VALUES (?,?,?,?,?,?)",
        (CONTAINER, 100 * 1024**3, free_bytes, "storage-discovery",
         f"{day}T01:09:05", rid))
    conn.commit()
    return rid, sids

conn = db.connect()
r1, s1 = seed_round(conn, "2026-10-01", (0, 0), 100 * MB)
# 目录侧只涨 20MB（KB 口径，clean 相全落在兄弟根 media 上），容器占用涨 26MB
# ⇒ 未知差额 6MB（free_delta 26MB - measured_delta 20MB，真实可算）
r2, s2 = seed_round(conn, "2026-10-02", KB_ROUND2, 74 * MB)
# 失败成员（仅 stale 相）：本轮追加一个无快照引用的成员 ⇒ stale 且不可比。
# 157 合同规定：存在失败成员时 comparable_to_previous=false，差额必须为 null
# ——「未知 6」与「stale 不可比」是**互斥**两态，故分两个隔离夹具各跑一次。
if os.environ.get("ISS158_STALE") == "1":
    conn.execute(
        "INSERT INTO scan_round_members(round_id, seq, plan_id, scope_id, "
        "snapshot_id, snapshot_status, status) VALUES (?,2,'p-root','scope-x',NULL,NULL,'failed')",
        (r2,))
conn.commit()
conn.close()
print(json.dumps({"r1": r1, "r2": r2, "s1": s1, "s2": s2,
                  "root": ROOT, "kid": KID, "container": CONTAINER}), flush=True)
`;

async function runPhase(withStale) {
  PHASE = withStale ? "stale" : "clean";
  const envExtra = withStale ? { ISS158_STALE: "1" } : {};
  // ---------- 1. 隔离运行根 + 种子 ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "iss158-verify-"));
  const runtimeDir = path.join(tmp, "runtime");
  const scanRoot = path.join(tmp, "scanroot");
  fs.mkdirSync(scanRoot, { recursive: true });
  const seedPath = path.join(tmp, "seed.py");
  fs.writeFileSync(seedPath, SEED_PY);
  const port = await freePort();
  const env = {
    ...process.env, ...envExtra,
    FATHOM_RUNTIME_DIR: runtimeDir,
    FATHOM_DB: path.join(runtimeDir, "data", "fathom.db"),
    FATHOM_SCAN_ROOT: scanRoot,
    FATHOM_PORT: String(port),
    PYTHONPATH: REPO,
  };
  record("fixture-runtime-isolated",
    runtimeDir.startsWith(os.tmpdir()) && scanRoot.startsWith(os.tmpdir()), runtimeDir);
  const seed = spawn(PY, [seedPath, tmp], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const seedOut = await new Promise((resolve, reject) => {
    let buf = "";
    seed.stdout.on("data", (c) => (buf += String(c)));
    seed.stderr.on("data", (c) => (buf += String(c)));
    seed.on("exit", (code) =>
      code === 0 ? resolve(buf) : reject(new Error(`seed exit ${code}: ${buf.slice(-900)}`)));
  });
  const line = seedOut.trim().split("\n").filter((l) => l.startsWith("{")).pop();
  if (!line) throw new Error(`种子未输出 JSON: ${seedOut.slice(-500)}`);
  const info = JSON.parse(line);
  const { s1, s2, root: ROOT, kid: KID, container: CONTAINER } = info;
  record("fixture-seed-ok", Boolean(s2) && s2.length === 2,
    `phase=${PHASE} r1=${info.r1} r2=${info.r2} s1=${JSON.stringify(s1)} s2=${JSON.stringify(s2)}`);

  const child = spawn(PY, ["-m", "fathom", "serve"], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  let serveLog = "";
  child.stderr.on("data", (c) => (serveLog += String(c)));
  let health;
  try {
    health = await waitUntil(async () => {
      const r = await httpJson("GET", "/health", port);
      if (r.status === 200 && r.json && r.json.pid === child.pid) return r.json;
      return null;
    }, 30000, "serve /health");
  } catch (e) {
    record("serve-health-identity", false, `${e.message}; serve=${serveLog.slice(-300)}`);
    try { child.kill("SIGTERM"); } catch (_) {}
    return;
  }
  record("serve-health-identity",
    health.pid === child.pid && health.runtime_mode === "development" && health.port === port,
    `pid=${health.pid} mode=${health.runtime_mode}`);

  const base = `http://127.0.0.1:${port}`;
  const get = (p) => httpJson("GET", p, port);

  // ---------- 3. HTTP 层 ----------
  const sum = await get("/api/storage/summary");
  const S = sum.json || {};
  const cap = S.capacity || {};
  const unexp = S.unexplained || {};
  const attr = S.attribution || {};
  record("http-summary-200", sum.status === 200 && S.scope && S.capacity, `status=${sum.status}`);
  record("http-scope-whole-disk",
    S.scope && S.scope.container_id === CONTAINER && S.scope.mode === "custom_directory",
    `container=${S.scope && S.scope.container_id} mode=${S.scope && S.scope.mode}`);
  record("http-shared-free-not-doubled", cap.free_bytes === 74 * 1024 * 1024,
    `free=${cap.free_bytes}（两卷样本同容器，不得翻倍为 148MB）`);
  // 测量根归因：clean 相为**兄弟根**（无重叠 ⇒ 两根都保留、无吸收，故两侧
  // comparable=true）；stale 相为**父子根**（子根被吸收 ⇒ 不可简单相加，
  // 157 R1 据此判 comparable=false）。
  const rootsWant = PHASE === "clean" ? [ROOT, KID] : [ROOT];
  const absorbedWant = PHASE === "clean" ? [] : [KID];
  record("http-attribution-roots-by-phase",
    JSON.stringify(attr.attribution_roots) === JSON.stringify(rootsWant)
      && JSON.stringify(attr.absorbed_roots) === JSON.stringify(absorbedWant),
    `roots=${JSON.stringify(attr.attribution_roots)} absorbed=${JSON.stringify(attr.absorbed_roots)}`);

  if (PHASE === "clean") {
    record("http-unexplained-six", unexp.comparable === true && unexp.bytes === 6 * 1024 * 1024,
      `comparable=${unexp.comparable} bytes=${unexp.bytes}`);
    record("http-unexplained-limitation",
      unexp.limitation === "尚无法由目录变化解释"
        && String(unexp.sign_semantics || "").includes("不代表垃圾量"),
      `limitation=${unexp.limitation}`);
    record("http-clean-no-stale",
      (attr.stale_members || []).length === 0 && attr.comparable_to_previous === true,
      `stale=${(attr.stale_members || []).length} comparable=${attr.comparable_to_previous}`);
  } else {
    record("http-failed-member-stale",
      Array.isArray(attr.stale_members) && attr.stale_members.length === 1
        && attr.stale_members[0].stale === true,
      `stale=${attr.stale_members && attr.stale_members.length}`);
    // stale 相同时覆盖「父子重叠测量根」语义：157 R1 判 comparable=false
    // 并给出可读原因（被吸收子根不可简单相加），而非静默放行。
    record("http-stale-blocks-comparable",
      attr.comparable_to_previous === false
        && String(unexp.reason || "").includes("父子重叠测量根")
        && String(unexp.reason || "").includes("不可简单相加"),
      `comparable_to_previous=${attr.comparable_to_previous} reason=${unexp.reason}`);
    record("http-stale-forces-null-difference",
      unexp.comparable === false && unexp.bytes === null,
      `comparable=${unexp.comparable} bytes=${unexp.bytes}（缺样本不补 0）`);
  }

  // ---------- 4. 真实 Chromium 实点生产总览页 ----------
  let browser = null;
  const consoleErrors = [];
  const dialogs = [];
  try {
    const pw = require("playwright");
    browser = await pw.chromium.launch({ headless: true });
    record("chromium-launched", true);
    const page = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    page.on("console", (m) => {
      if (m.type() === "error" && !/^Failed to load resource/.test(m.text())) consoleErrors.push(m.text());
    });
    page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
    page.on("dialog", (d) => { dialogs.push(d.message()); d.dismiss(); });

    await page.goto(`${base}/#/overview`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("[data-test='storage-panel']", { timeout: 20000 });
    await page.waitForFunction(() => {
      const p = document.getElementById("overview-storage");
      return p && p.querySelector("[data-test='storage-unexplained'],[data-test='storage-headline'],[data-test='storage-state']");
    }, null, { timeout: 20000 });

    const freeText = await page.$eval("[data-test='storage-free']", (el) => el.textContent);
    const usedText = await page.$eval("[data-test='storage-used']", (el) => el.textContent);
    record("ui-free-single-readout", freeText === "74.0 MB" && usedText === "99.9 GB",
      `free=${freeText} used=${usedText}`);
    const rootsText = await page.$eval("[data-test='storage-roots']", (el) => el.textContent);
    record("ui-attribution-roots-by-phase",
      PHASE === "clean"
        ? (rootsText.includes("测量根 2 个") && !rootsText.includes("已吸收子根"))
        : (rootsText.includes("测量根 1 个") && rootsText.includes("已吸收子根 1 个")),
      rootsText.trim());

    if (PHASE === "clean") {
      const unexpText = await page.$eval("[data-test='storage-unexplained']", (el) => el.textContent);
      const unexpBytes = await page.$eval("[data-test='storage-unexplained']", (el) => el.dataset.bytes);
      const flat = unexpText.replace(/\s+/g, " ");
      record("ui-unexplained-six-mb",
        unexpBytes === String(6 * 1024 * 1024) && flat.includes("6.0 MB"),
        `bytes=${unexpBytes} text=${flat.slice(0, 90)}`);
      record("ui-unexplained-not-reclaimable",
        flat.includes("未知差额") && flat.includes("尚无法由目录变化解释")
          && flat.includes("不代表垃圾量或可回收空间")
          && !/可清理|可释放/.test(flat)
          && !/[（(]可回收[)）]/.test(flat),
        "措辞=未知差额/尚无法由目录变化解释 + 显式否定可回收，无可清理误导");
    } else {
      const unexpText = await page.$eval("[data-test='storage-unexplained']", (el) => el.textContent);
      const state = await page.$eval("[data-test='storage-unexplained']", (el) => el.dataset.state);
      const flat = unexpText.replace(/\s+/g, " ");
      record("ui-stale-unexplained-unknown",
        state === "unknown" && flat.includes("不可计算") && !flat.includes("6.0 MB"),
        `state=${state} text=${flat.slice(0, 90)}`);
      const staleCount = await page.$$eval("[data-test='storage-stale'] li", (els) => els.length);
      const staleText = staleCount
        ? await page.$eval("[data-test='storage-stale']", (el) => el.textContent) : "";
      record("ui-stale-member-marked", staleCount === 1 && staleText.includes("本轮无新快照"),
        `stale=${staleCount}`);
      const incomparText = await page.$eval("[data-test='storage-incomparable']", (el) => el.textContent);
      record("ui-incomparable-explained", incomparText.includes("同主体") && incomparText.includes("可比"),
        incomparText.replace(/\s+/g, " ").slice(0, 70));
    }

    // C3：实点主按钮 → 变化页，a/b 来自真实快照且一致
    const bAttr = await page.$eval("[data-test='storage-cta']", (el) => el.dataset.b);
    await page.click("[data-test='storage-cta']");
    await page.waitForFunction(() => (location.hash || "").startsWith("#/changes"), null, { timeout: 15000 });
    await page.waitForFunction((want) => {
      const sel = document.getElementById("sel-b");
      return sel && sel.options.length > 0 && sel.value === want;
    }, String(bAttr), { timeout: 15000 });
    const aVal = await page.$eval("#sel-a", (el) => el.value);
    const bVal = await page.$eval("#sel-b", (el) => el.value);
    record("ui-cta-carries-real-b", bVal === String(bAttr) && bVal === String(s2[0]),
      `cta-b=${bAttr} sel-b=${bVal} 摘要本轮成员快照=${s2[0]}`);
    record("ui-cta-a-real-predecessor",
      Boolean(aVal) && aVal !== bVal && [...s1, ...s2].map(String).includes(aVal),
      `sel-a=${aVal} sel-b=${bVal}（a 须为真实且不同于 b）`);

    /* ISS-178 D2：交接后基线（a 侧）必须非空且可构造对比。
     *
     * 实机反例：总览 CTA 带 data-a="" 交接，b 侧正确带入，但 a 侧选项为空，
     * 对比无法构造。根因是交接在**派发 b 侧 change 之前**就读 a 侧选项，而 a 侧
     * 要等变化页按 b 所属数据集收敛（applyConvergedOptions）才重填。
     *
     * 本段先**预热**变化页（把 b 选到预热集合的快照，让 a 侧先行收敛落定），
     * 再回总览点 CTA 交接 summary 侧的真实 b，断言交接后 a 侧非空、与 b 同
     * 数据集、且 diff 自动构造。 */
    const d2 = await (async () => {
      // 用**页内 hash 导航**（与真实用户一致），不做整页 goto：整页导航会卸载
      // 文档、打断在途 fetch，凭空制造「无法连接本地服务」控制台错误。
      const goHash = async (hash) => {
        await page.evaluate((h) => { location.hash = h; }, hash);
        await page.waitForFunction((h) => (location.hash || "") === h, hash,
          { timeout: 10000 });
      };
      await goHash("#/changes");
      await page.waitForFunction(() => {
        const sel = document.getElementById("sel-b");
        return sel && sel.options.length > 1;
      }, null, { timeout: 20000 });
      const otherB = String((s1 && s1[0]) ?? "");
      if (!otherB) return { skipped: true };
      await page.selectOption("#sel-b", otherB);
      await page.waitForFunction((id) => document.getElementById("sel-b").value === id,
        otherB, { timeout: 15000 });
      // 预热：a 侧此刻已收敛到 otherB 所属数据集
      const staleOpts = await page.$$eval("#sel-a option", (els) =>
        els.map((o) => o.value).filter(Boolean));

      await goHash("#/overview");
      const cta = await page.waitForSelector("[data-test='storage-cta']",
        { timeout: 20000 }).catch(() => null);
      if (!cta) return { skipped: true, noCta: true };
      const wantB = await page.$eval("[data-test='storage-cta']", (el) => el.dataset.b);
      await page.click("[data-test='storage-cta']");
      await page.waitForFunction(() => (location.hash || "").startsWith("#/changes"),
        null, { timeout: 15000 });
      await page.waitForFunction((want) => {
        const sel = document.getElementById("sel-b");
        return sel && sel.options.length > 0 && sel.value === want;
      }, String(wantB), { timeout: 20000 });
      await page.waitForFunction(() => {
        const sel = document.getElementById("sel-a");
        return sel && sel.options.length > 0 && sel.value !== "";
      }, null, { timeout: 20000 }).catch(() => {});
      const now = await page.evaluate(() => {
        const a = document.getElementById("sel-a");
        const b = document.getElementById("sel-b");
        return {
          aValue: a.value,
          aOptions: Array.from(a.options).map((o) => o.value).filter(Boolean),
          bValue: b.value,
        };
      });
      return { skipped: false, staleOpts, otherB, wantB: String(wantB),
               aValue: String(now.aValue), aOptions: now.aOptions,
               bValue: String(now.bValue) };
    })();

    if (!d2.skipped) {
      // 交接后 a 侧非空、选中值非空且不同于 b
      record("ui-cta-handoff-baseline-non-empty",
        d2.aOptions.length > 0 && d2.aValue !== "" && d2.aValue !== d2.bValue,
        `预热 a 侧=${JSON.stringify(d2.staleOpts)} → 交接后 a=#${d2.aValue}`
        + `（${d2.aOptions.map((v) => "#" + v).join(",")}）b=#${d2.bValue}`);
      // a 侧必须已收敛到 b 所属数据集（同身份），不得残留别的数据集的选项。
      // 判据直接取 /api/snapshots 的真实 plan_id（ISS-176 前端同源口径），
      // 不假设夹具里哪两个集合跨数据集。
      const snapJson = await httpJson("GET", "/api/snapshots", port).catch(() => null);
      const allSnaps = (snapJson && snapJson.json) || [];
      const keyOf = (id) => {
        const s = allSnaps.find((x) => String(x.id) === String(id));
        if (!s) return null;
        return s.plan_id ? `plan:${s.plan_id}` : `legacy:${s.root}`;
      };
      const bKey = keyOf(d2.bValue);
      const foreign = d2.aOptions.filter((v) => keyOf(v) && bKey && keyOf(v) !== bKey);
      record("ui-cta-baseline-same-dataset-as-b",
        Boolean(bKey) && foreign.length === 0 && d2.aOptions.length > 0,
        `b=#${d2.bValue}(${bKey}) a 侧=${JSON.stringify(d2.aOptions)}`
        + ` 异数据集残留=${JSON.stringify(foreign)}`);
      // 交接语义不变：绝不塞假基线——a 必须是目录里真实的快照
      const net0 = await page.$eval("#changes-net", (el) => el.textContent).catch(() => "");
      record("ui-cta-diff-autoconstructed",
        net0.trim().length > 0 && !/正在读取|正在加载/.test(net0),
        net0.replace(/\s+/g, " ").slice(0, 90));
    }

    // C4：键盘 + 图表恢复非零
    await page.goto(`${base}/#/overview`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("[data-test='storage-cta']", { timeout: 20000 });
    await page.focus("[data-test='storage-cta']");
    const focused = await page.evaluate(() =>
      document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.test : null);
    record("ui-cta-keyboard-focusable", focused === "storage-cta", `active=${focused}`);
    const boxOf = () => page.$eval("#chart-volume", (el) => ({
      w: el.clientWidth, h: el.clientHeight,
    })).catch(() => ({ w: 0, h: 0 }));
    const beforeHide = await boxOf();
    // 离页（容器隐藏、clientWidth=0）→ 返回：resumeChartsIn 必须把尺寸恢复非零
    await page.click("a.nav-item[data-page='browse']").catch(() => {});
    await sleep(500);
    const whileHidden = await boxOf();
    await page.click("a.nav-item[data-page='overview']");
    await page.waitForSelector("[data-test='storage-panel']", { timeout: 15000 });
    await sleep(500);
    const afterRestore = await boxOf();
    record("ui-chart-restore-nonzero",
      beforeHide.w > 0 && beforeHide.h > 0 && afterRestore.w > 0 && afterRestore.h > 0,
      `返回前=${beforeHide.w}x${beforeHide.h} 返回后=${afterRestore.w}x${afterRestore.h}`);
    await page.waitForSelector("[data-test='storage-meta']", { timeout: 15000 }).catch(() => {});
    const metaInfo = await page.$eval("[data-test='storage-meta']", (el) => ({
      scroll: el.scrollWidth, client: el.clientWidth, title: el.title })).catch(() => ({ scroll: 0, client: 0, title: "" }));
    record("ui-long-scope-name-clipped", metaInfo.client > 0 && metaInfo.title.length > 0,
      `scroll=${metaInfo.scroll} client=${metaInfo.client}`);

    for (const [w, h] of [[980, 640], [1220, 820], [1440, 900]]) {
      await page.setViewportSize({ width: w, height: h });
      await sleep(400);
      const overflow = await page.evaluate(() =>
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
          - document.documentElement.clientWidth);
      record(`viewport-${w}x${h}-no-overflow`, overflow <= 2, `overflowPx=${overflow}`);
      await page.screenshot({ path: path.join(SHOTS, `phase-${PHASE}-${w}x${h}.png`) });
    }

    // C5：legacy（未启用范围能力）不伪装整盘
    await page.route("**/api/storage/summary", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ scope: { container_id: null, mode: null, roots: [] },
        capacity: { subject: null, free_bytes: 74 * 1024 * 1024, total_bytes: 100 * 1024 ** 3 },
        round: null, previous_round: null, attribution: null,
        unexplained: { bytes: null, comparable: false, reason: "尚无完整轮次对（需同主体、同计划的两轮）。" } }),
    }));
    await page.goto(`${base}/#/overview`, { waitUntil: "domcontentloaded" });
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("[data-test='storage-headline']", { timeout: 20000 });
    const legacyHead = await page.$eval("[data-test='storage-headline']", (el) => el.textContent);
    const legacySub = await page.$eval("[data-test='storage-sub']", (el) => el.textContent);
    const legacyCta = await page.$("[data-test='storage-cta']");
    record("ui-legacy-no-fake-whole-disk",
      legacyHead.includes("尚未启用整盘范围") && legacySub.includes("不把目录结果冒充整盘") && !legacyCta,
      `head=${legacyHead.trim()}`);
    record("ui-legacy-settings-entry", Boolean(await page.$("[data-test='storage-settings-entry']")), "给设置入口");

    /* ISS-171 容量文案：负差额不得表述为「容器占用减少」。反例口径——
     * 容器占用其实**增加**了（free 100MB→90MB 即占用 +10MB），只是目录测量
     * 增长更大（+24MB），差额 = 10 − 24 = −14MB。负号只说明「目录测量增长
     * 大于容器占用增长」，推不出占用减少（旧文案在此失真）。 */
    await page.route("**/api/storage/summary", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ scope: { container_id: null, mode: "startup_storage",
        roots: ["/fixture/negative-delta"] },
        capacity: { subject: null, free_bytes: 90 * 1024 * 1024, total_bytes: 100 * 1024 ** 3 },
        round: { id: 2 }, previous_round: { id: 1 }, attribution: null,
        unexplained: { bytes: -14 * 1024 ** 2, comparable: true,
          limitation: "尚无法由目录变化解释" } }),
    }));
    await page.goto(`${base}/#/overview`, { waitUntil: "domcontentloaded" });
    await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForSelector("[data-test='storage-unexplained']", { timeout: 20000 });
    const negFlat = (await page.$eval("[data-test='storage-unexplained']",
      (el) => el.textContent)).replace(/\s+/g, " ");
    const negBytes = await page.$eval("[data-test='storage-unexplained']", (el) => el.dataset.bytes);
    record("ui-negative-unexplained-not-claimed-as-shrink",
      negBytes === String(-14 * 1024 ** 2)
        && negFlat.includes("目录测量增长大于容器占用增长")
        && !negFlat.includes("容器占用减少")
        && !/占用减少/.test(negFlat),
      `bytes=${negBytes}（数值未变），文案=${negFlat.slice(0, 110)}`);

    await page.unroute("**/api/storage/summary");

    record("ui-console-clean", consoleErrors.length === 0,
      consoleErrors.slice(0, 3).join(" ; ") || "无页面错误");
    record("ui-no-native-dialog", dialogs.length === 0, dialogs.join(" ; ") || "无原生弹窗");
  } catch (e) {
    record("browser-flow-error", false, e.message);
    if (serveLog) process.stderr.write(`serve stderr 尾部: ${serveLog.slice(-500)}\n`);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }

  try { child.kill("SIGTERM"); } catch (_) { /* 已退出 */ }
  const portFreed = await waitUntil(async () => {
    try { await httpJson("GET", "/health", port); return false; } catch (_) { return true; }
  }, 10000, "端口释放");
  record("port-released", portFreed, `127.0.0.1:${port}`);
}

async function main() {
  // 两个隔离夹具：clean（两侧可比 ⇒ 未知 6MB）与 stale（失败成员 ⇒ 不可比）。
  // 157 合同下二者互斥（stale 会让 comparable_to_previous=false），故分跑。
  await runPhase(false);
  await runPhase(true);

  const failed = checks.filter((c) => !c.ok);
  process.stderr.write(`\n== ISS-158 前端验证：${checks.length - failed.length}/${checks.length} 通过 ==\n`);
  if (failed.length) {
    process.stderr.write("失败项：\n" + failed.map((c) => `- ${c.name}: ${c.detail}`).join("\n") + "\n");
  }
  fs.writeFileSync(path.join(SHOTS, "..", "result.json"), JSON.stringify({
    task: "iss158-storage-overview-frontend",
    pass: failed.length === 0, total: checks.length, failed: failed.length, checks,
    source: require("child_process").execSync("git rev-parse HEAD", { cwd: REPO }).toString().trim(),
  }, null, 2));
  process.stdout.write(JSON.stringify({
    ok: failed.length === 0, passed: checks.length - failed.length, failed: failed.length, checks,
  }) + "\n");
}

let PHASE = "clean";
main().catch((e) => { console.error(e); process.exit(1); });
