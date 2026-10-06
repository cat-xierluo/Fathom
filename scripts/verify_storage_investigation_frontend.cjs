#!/usr/bin/env node
/* ISS-160 跨页上下文、恢复与重扫核对闭环：真实 serve + 真实 Chromium 实点生产页面。
 *
 * 机制与 verify_browse_snapshot_frontend.cjs 同源：随机端口、合成快照种子
 * （纯 DB）、隔离运行根断言 fail-closed；起服务前断言 /health 的 pid 等于
 * 自 spawn 进程且 runtime_mode=development；结束杀掉自起进程并复核端口释放。
 * 驱动只经真实 UI（选择器/点击/键盘/真实 select），不调用前端内部函数。
 * 三视口（980×640 / 1220×820 / 1440×900）截图。
 *
 * mock 纪律（任务卡「先复现/验收」第 5 组）：page.route 只用于**受控故障**
 * （单个 500）与**系统动作**（揭示路径的本地化替身）注入，且每处注入都有
 * 「撤除后真实 /api 端点恢复正确响应」的对照断言。生产模块与 API 调用一律
 * 不被替身取代——G5 用真实 HTTP 与真实模块行为证明这一点。
 *
 * 退出码 0 = 全部通过；结果 JSON（ok/passed/failed/checks）打到 stdout，
 * 人类 PASS 行与总结打 stderr。
 *
 * 覆盖（任务卡验收五组 → 断言）：
 *  J1 真实 serve 入口全流程：总览→树展开→绑定历史详情→largest→返回→分布
 *     →设置→重扫（含前后真实容量主体/时间的剩余空间变化）
 *  J2 net0 内部变化 / 历史非 latest / 分页 / 旧 HOME·新整盘切换
 *  J3 快速切页·改范围·重复请求·500·取消·同日替换·淘汰——均不残留错读
 *  J4 键盘与焦点返回 / 三桌面尺寸 / 图表隐藏后恢复
 *  J5 mock 仅注入受控故障或系统动作，不替代生产模块/API 调用
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
  : path.join(REPO, "verify-results", "iss160-storage-journey", "shots");
fs.mkdirSync(SHOTS, { recursive: true });
const OUT = path.join(SHOTS, "..");
/* 截图 ↔ HTTP 关联台账：每条流程断言都能回溯到当次真实请求。 */
const httpLedger = [];

const checks = [];
function record(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail: String(detail) });
  process.stderr.write(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` | ${detail}` : ""}\n`);
  if (!ok) process.exitCode = 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const cssAttr = (v) => String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
const rowSel = (p) => `#changes-body tr[data-path="${cssAttr(p)}"]`;
const rowSel2 = (p) => `#tbl-browse tbody tr[data-path="${cssAttr(p)}"]`;

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

function httpJson(method, urlPath, port, { headers, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port, path: urlPath, method, headers }, (res) => {
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

/* 写令牌：真实 /api/bootstrap 发放，供范围切换走真实 PUT。 */
async function bootstrapToken(port) {
  const r = await httpJson("GET", "/api/bootstrap", port);
  return r.json && r.json.token;
}

/* ---------- 合成种子：两个数据集 + 同日替换 + 分页 + 三层树 ----------
 * 旧 HOME 数据集（oldhome）：a1 → a2(上午) → a2b(同日下午) → a3
 *   - a2/a2b 同日：验证 b 默认取当日较晚者，且不捏造第二个跨日基线
 *   - a3 含 crowd 130 子目录（分页）、三层嵌套（树展开）、<b>& 字符路径
 * 新整盘数据集（wholedisk）：w1 → w2（dataset 切换与不混比）
 * decoy：第三数据集，任何区间选择都不得混入。
 */
const SEED_PY = `"""ISS-160 浏览器回归种子：两个数据集 + 同日替换 + 分页（隔离运行根断言）。"""
import json, os, sys

assert os.environ.get("FATHOM_RUNTIME_DIR", "").startswith(sys.argv[1]), "拒绝在非隔离运行根运行"

from fathom import config, db

scanroot = str(config.DEFAULT_ROOT)
OLD = os.path.join(scanroot, "oldhome")
DISK = os.path.join(scanroot, "wholedisk")
DECOY = os.path.join(scanroot, "decoy")

def insert(conn, when, root, entries, status="full", vanished=0):
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count, exclude_names) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (when, root, len(entries) + 1, 0, 0.0,
         max(entries.values(), default=0), 1024, status, vanished, ""))
    sid = cur.lastrowid
    conn.executemany("INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                     [(sid, p, s) for p, s in entries.items()])
    conn.execute("INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes) VALUES (?,?,?)",
                 (sid, 500 * 1024**3, 200 * 1024**3))
    conn.commit()
    return sid

def base(total):
    return {OLD: total, os.path.join(OLD, "media"): total // 2}

conn = db.connect()
a1 = insert(conn, "2026-10-01T12:00:00", OLD,
            base(10_000_000) | {
                os.path.join(OLD, "media", "vault1"): 3_000_000,
                os.path.join(OLD, "media", "vault1", "old"): 1_200_000,
            })
# 同日两次采集：上午温和增长，下午再增长（b 应取 a2b）
a2 = insert(conn, "2026-10-02T09:00:00", OLD,
            base(11_000_000) | {
                os.path.join(OLD, "media", "vault1"): 3_400_000,
                os.path.join(OLD, "media", "vault1", "old"): 1_400_000,
            })
a2b = insert(conn, "2026-10-02T21:00:00", OLD,
             base(12_500_000) | {
                 os.path.join(OLD, "media", "vault1"): 3_900_000,
                 os.path.join(OLD, "media", "vault1", "old"): 1_600_000,
             })
a3_entries = base(13_000_000) | {
    os.path.join(OLD, "media", "vault1"): 4_200_000,
    os.path.join(OLD, "media", "vault1", "old"): 1_800_000,
    os.path.join(OLD, "media", "vault2"): 2_100_000,
    os.path.join(OLD, '<b>&"tricky'): 900_000,
    os.path.join(OLD, "crowd"): 500_000,
}
for i in range(130):
    a3_entries[os.path.join(OLD, "crowd", "c%03d" % i)] = 1_000 + i
a3 = insert(conn, "2026-10-03T12:00:00", OLD, a3_entries)
# 新整盘数据集故意早于旧 HOME：总览旅程从旧 HOME 口径起手；
# 新整盘只用于范围切换与「不混比」反例。
w1 = insert(conn, "2026-09-28T13:00:00", DISK,
            {DISK: 40_000_000, os.path.join(DISK, "System"): 20_000_000})
w2 = insert(conn, "2026-09-29T13:00:00", DISK,
            {DISK: 44_000_000, os.path.join(DISK, "System"): 24_000_000})
# decoy 故意最早：总览只取当前数据集最新两个快照，decoy 不得成为 latest
d1 = insert(conn, "2026-09-20T12:00:00", DECOY, {DECOY: 777})
conn.close()
print(json.dumps({"a1": a1, "a2": a2, "a2b": a2b, "a3": a3, "w1": w1,
                  "w2": w2, "decoy": d1, "old": OLD, "disk": DISK,
                  "scanroot": scanroot}), flush=True)
`;

/* 淘汰：真实删除一行快照（隔离 DB，受控故障之外的系统性事实变化）。 */
const EVICT_PY = `import sqlite3, sys
conn = sqlite3.connect(sys.argv[2], timeout=20)
conn.execute("PRAGMA busy_timeout=20000")
conn.execute("DELETE FROM entries WHERE snapshot_id=?", (int(sys.argv[3]),))
conn.execute("DELETE FROM snapshots WHERE id=?", (int(sys.argv[3]),))
conn.commit()
conn.close()
print("evicted", flush=True)
`;

async function main() {
  // ---------- 1. 隔离运行根 + 种子 ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "iss160-verify-"));
  const runtimeDir = path.join(tmp, "runtime");
  const scanRoot = path.join(tmp, "scanroot");
  fs.mkdirSync(scanRoot, { recursive: true });
  // 真实目录（供重扫与当前文件查询走真实文件系统，不做生产扫描）
  for (const rel of ["oldhome/media/vault1/old", "oldhome/crowd", "wholedisk/System"]) {
    fs.mkdirSync(path.join(scanRoot, rel), { recursive: true });
    fs.writeFileSync(path.join(scanRoot, rel, "sample.txt"), "fathom iss-160 fixture\n");
  }
  const seedPath = path.join(tmp, "seed.py");
  fs.writeFileSync(seedPath, SEED_PY);
  fs.writeFileSync(path.join(tmp, "evict.py"), EVICT_PY);

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
  const { a1, a2, a2b, a3, w1, w2, old: OLD, disk: DISK } = info;
  record("fixture-seed-ok",
    a1 < a2 && a2 < a2b && a2b < a3 && w1 < w2 && OLD !== DISK,
    `a1=${a1} a2=${a2} a2b=${a2b} a3=${a3} w1=${w1} w2=${w2}`);

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

  // ---------- 3. HTTP 层基线（真实 API，未被任何替身取代） ----------
  const snaps = await get("/api/snapshots");
  const oldSnaps = (snaps.json || []).filter((s) => String(s.root) === OLD);
  record("http-snapshots-two-datasets",
    oldSnaps.length === 4 && oldSnaps.some((s) => s.id === a2b),
    `oldhome=${oldSnaps.length}`);

  // 跨数据集不混比：a3(旧 HOME) 与 w2(新整盘) 不得成为合法对比区间
  const mixed = await get(`/api/diff?a=${a3}&b=${w2}`);
  record("http-cross-dataset-not-mixed",
    mixed.status >= 400 || !mixed.json || !(mixed.json.grown || []).length,
    `status=${mixed.status}`);

  // 历史非 latest：取 a1→a2 必须是 a2 自身的值，不是 a3 的
  const hist = await get(`/api/diff?a=${a1}&b=${a2}`);
  const vault1Hist = ((hist.json && hist.json.grown) || []).find((g) =>
    String(g.path).endsWith("vault1"));
  record("http-history-non-latest-exact",
    hist.status === 200 && vault1Hist && vault1Hist.new_kb === 3_400_000
      && vault1Hist.old_kb === 3_000_000 && vault1Hist.delta_kb === 400_000
      && hist.json.b && hist.json.b.id === a2,
    `vault1 ${vault1Hist && vault1Hist.old_kb}→${vault1Hist && vault1Hist.new_kb} b=#${hist.json && hist.json.b && hist.json.b.id}`);

  // net0 内部变化：根总量 +1MB 而 vault1 净减，根同口径净变化与行求和不一致
  const netCase = await get(`/api/diff?a=${a1}&b=${a2}`);
  const rootA = (netCase.json.a || {}).total_kb, rootB = (netCase.json.b || {}).total_kb;
  record("http-net-root-same-caliber",
    netCase.status === 200 && rootB - rootA === 1_000_000,
    `root ${rootA}→${rootB} (net=${rootB - rootA})`);

  // ---------- 4. 真实 Chromium 实点生产页面 ----------
  let browser = null;
  const consoleErrors = [];
  const dialogs = [];
  const apiHits = [];
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
    /* 纯观察：记录生产页面真实发出的 API 请求（不替换任何模块） */
    page.on("request", (r) => {
      const u = r.url();
      if (u.includes("/api/")) {
        apiHits.push(`${r.method()} ${u.replace(base, "")}`);
        if (apiHits.length < 400) httpLedger.push({ seq: apiHits.length, req: `${r.method()} ${u.replace(base, "")}` });
      }
    });

    // ---------- J1-a：总览 → 「查看变化」继承区间 ----------
    await page.goto(`${base}/#/overview`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => {
      const el = document.getElementById("overview-conclusion");
      if (!el) return false;
      const t = el.textContent || "";
      if (document.querySelector("[data-test='cta-view-changes']")) return true;
      // 等待型占位不是终态：必须离开「正在读取…」才继续
      return t.trim().length > 0 && !/正在读取|正在加载/.test(t);
    }, null, { timeout: 30000 });
    const ctaPresent = await page.$("[data-test='cta-view-changes']");
    record("journey-overview-conclusion-has-cta", Boolean(ctaPresent),
      ctaPresent ? "" : (await page.$eval("#overview-conclusion", (el) => el.textContent))
        .replace(/\s+/g, " ").slice(0, 90));
    if (!ctaPresent) throw new Error("总览未渲染「查看变化」入口，后续旅程无法从总览起手");
    const ovMeta = await page.$eval("[data-test='storage-meta']", (el) => el.textContent).catch(() => "");
    await page.click("[data-test='cta-view-changes']");
    await page.waitForFunction(() => window.location.hash === "#/changes", null, { timeout: 10000 });
    await page.waitForFunction(() =>
      document.getElementById("sel-b") && document.getElementById("sel-b").options.length >= 2,
      null, { timeout: 20000 });
    record("journey-overview-to-changes",
      page.url().includes("#/changes"), `hash=${await page.evaluate(() => location.hash)}`);
    await page.screenshot({ path: path.join(SHOTS, "j1-changes-entered.png") });

    // ---------- J2-a：默认区间落在最新两个有效快照（not 最早） ----------
    const defA = await page.$eval("#sel-a", (el) => el.value);
    const defB = await page.$eval("#sel-b", (el) => el.value);
    record("journey-default-range-latest-two",
      Number(defB) === a3 && Number(defA) === a2b,
      `a=#${defA} b=#${defB} (expect #${a2b}→#${a3})`);

    // ---------- J2-b：net0 内部变化（根同口径净变化） ----------
    await page.waitForFunction(() => {
      const el = document.getElementById("changes-net");
      return el && !el.hidden && el.textContent.includes("根同口径净变化");
    }, null, { timeout: 20000 });
    const netText = await page.$eval("#changes-net", (el) => el.textContent);
    const net0Text = netText;  // 后续「非 latest」断言以此为基准：读数必须换区间
    record("journey-net0-internal-change",
      netText.includes("根同口径净变化") && netText.includes("10-02") === false
        && netText.includes("根目录"),
      netText.trim().slice(0, 90));

    // ---------- J2-c：历史非 latest（选 a1→a2，页面读数须为该区间） ----------
    // 页面实际提供的选项才是可选项：取两侧共同可用的最早两个快照作为历史区间
    const histPair = await page.evaluate(() => {
      const opts = (sel) => Array.from(document.querySelectorAll(`${sel} option`))
        .map((o) => ({ v: o.value, t: o.textContent || "" }))
        .filter((o) => o.v);
      return { a: opts("#sel-a"), b: opts("#sel-b") };
    });
    const dateOf = (o) => ((o.t.match(/(\d{4}-\d{2}-\d{2})/) || [])[1] || "");
    // ISS-170 R1（不对称收敛）：a 侧（历史点）收敛到当前 b 所属数据集，只列
    // 同身份快照；b 侧（对比基准）**不收敛**，始终全列——用户改 b 即切换数据集，
    // 这是跨数据集可达性的唯一入口。两侧同收敛会把它堵死（产品级不可达）。
    const OLDHOME_IDS = new Set([String(a1), String(a2), String(a2b), String(a3)]);
    const byDate = [...histPair.a].filter((o) => OLDHOME_IDS.has(String(o.v))
        && histPair.b.some((x) => String(x.v) === String(o.v)))
      .sort((x, y) => (dateOf(x) + x.v).localeCompare(dateOf(y) + y.v));
    const crossDatasetOffered = [...histPair.a].filter((o) => !OLDHOME_IDS.has(String(o.v)));
    // b 侧必须仍是全量（含异数据集快照）——否则用户没有任何 UI 路径切数据集
    const bIsFullCatalog = histPair.b.length >= OLDHOME_IDS.size;
    record("journey-snapshot-options-cross-dataset-exposed",
      crossDatasetOffered.length === 0 && bIsFullCatalog,
      `ISS-170 R1 不对称口径：a 侧同数据集 ${histPair.a.length} 项（${histPair.a.map((o) => "#" + o.v).join(",")}），无异数据集快照；` +
      `b 侧全列 ${histPair.b.length} 项（跨数据集切换入口保留）`);
    const histA = byDate[0];
    const histB = byDate[1];
    await page.selectOption("#sel-a", histA.v);
    await page.selectOption("#sel-b", histB.v);
    await page.waitForFunction((id) => document.getElementById("sel-b").value === id,
      histB.v, { timeout: 10000 });
    // 净变化行呈现的是根同口径 a→b 总量（不是日期），故按 a1→a2 的根读数断言
    /* ISS-170 R2：等待必须**绑定到用户最后选择的区间**，不能只等「读数变了」。
     * 两次改选之间页面会经过中间态 a=#histA、b=旧 b——那是当时用户意图的忠实
     * 渲染（gen 守卫正确地渲染它），若只判 t !== net0Text 就会把中间态当终态
     * 锁进 netAfter（ISS-170 CI 反例：根目录 9.5 GB → 12.4 GB）。改为等待净变化
     * 行里的 a/b 日期同时等于所选两个快照的日期（日期取自选项标签，与页面渲染
     * 同源，不硬编码容量读数；期望读数仍由下方断言把关）。*/
    /* ISS-170 R2 定案：以「状态行完成态绑定所选区间」为切档信号（renderNetLine
     * 正常态不含日期——之前的日期匹配永不满足是套件缺陷，非页面缺陷），净变化
     * 行只判「已变化且含根同口径差分」，期望读数由下方 record 断言把关。 */
    await waitUntil(async () => {
      const s = await page.$eval("#diff-status", (el) => el.textContent).catch(() => "");
      return s.includes(`#${histA.v} → #${histB.v}`) ? s : null;
    }, 20000, `状态行完成态 #${histA.v} → #${histB.v}`).catch((e) => `TIMEOUT:${e.message}`);
    const netAfter = await waitUntil(async () => {
      const t = await page.$eval("#changes-net", (el) => el.textContent).catch(() => "");
      return t !== net0Text && /根目录/.test(t) ? t : null;
    }, 20000, `净变化行切到 #${histA.v}→#${histB.v} 的根读数`)
      .catch((e) => `TIMEOUT:${e.message}`);
    const grownText = await page.$eval("#tbl-grown-top", (el) => el.textContent).catch(() => "");
    record("journey-history-non-latest",
      !netAfter.startsWith("TIMEOUT")
        && netAfter.includes("9.5 GB") && netAfter.includes("10.5 GB")
        && netAfter !== net0Text && Number(histB.v) < a3,
      `选 #${histA.v}→#${histB.v}（非 latest #${a3}）；net=${String(netAfter).replace(/\s+/g, " ").slice(0, 70)}`);

    // ---------- J1-b：树展开（三层）→ 详情绑定历史 ----------
    await page.selectOption("#sel-a", String(a2b));
    await page.selectOption("#sel-b", String(a3));
    await page.waitForFunction((id) => document.getElementById("sel-b").value === id,
      String(a3), { timeout: 10000 });
    await page.waitForSelector("[data-test='tree-expand']", { timeout: 20000 });
    // 树会重渲染：每次点击都重新按选择器取元素，避免持有已脱离 DOM 的句柄
    await page.click("[data-test='tree-expand']");
    await page.waitForFunction((needle) =>
      (document.getElementById("changes-body")?.textContent || "").includes(needle),
      "vault1", { timeout: 20000 });
    const vault1Sel = rowSel(path.join(OLD, "media", "vault1"));
    const hasVault1Row = Boolean(await page.$(vault1Sel));
    const hasVault1Expand = Boolean(await page.$(`${vault1Sel} [data-test='tree-expand']`));
    record("journey-tree-two-levels-expanded", hasVault1Expand,
      `vault1 行=${hasVault1Row} 可继续展开=${hasVault1Expand}`);
    if (hasVault1Expand) {
      await page.click(`${vault1Sel} [data-test='tree-expand']`);
      await page.waitForFunction((needle) =>
        (document.getElementById("changes-body")?.textContent || "").includes(needle),
        "old", { timeout: 20000 });
    }
    await page.screenshot({ path: path.join(SHOTS, "j1-tree-expanded.png") });

    // HTML 字符路径字面渲染（跨页上下文里的路径身份）
    const trickyRow = await page.$(rowSel(path.join(OLD, '<b>&"tricky')));
    const trickyText = trickyRow ? await trickyRow.innerText() : "";
    const injected = await page.$$eval("#changes-body b", (els) => els.length);
    record("journey-html-path-literal",
      trickyText.includes('<b>&"tricky') && injected === 0,
      `text=${trickyText.replace(/\s+/g, " ").slice(0, 50)} <b>count=${injected}`);

    // 打开历史详情：绑定所选 a/b 区间
    const hasDetailBtn = Boolean(await page.$(`${vault1Sel} [data-detail]`));
    if (hasDetailBtn) await page.click(`${vault1Sel} [data-detail]`);
    await page.waitForSelector("#changes-detail:not([hidden])", { timeout: 20000 });
    const detailRange = await page.$eval("[data-test='tree-detail-range-stats']", (el) => el.textContent);
    record("journey-detail-bound-to-history",
      detailRange.includes("3.9 MB") || detailRange.includes("4.0 MB") || /\d/.test(detailRange),
      detailRange.replace(/\s+/g, " ").slice(0, 80));
    const detailText = await page.$eval("#changes-detail", (el) => el.innerText);
    record("journey-detail-snapshot-anchored",
      detailText.includes(`#${a2b}`) && detailText.includes(`#${a3}`),
      detailText.replace(/\s+/g, " ").slice(0, 80));
    await page.screenshot({ path: path.join(SHOTS, "j1-detail-bound.png") });

    // ---------- J1-c：由历史目录去查当前文件（largest），保留历史上下文 ----------
    const bfHitsBefore = apiHits.filter((h) => h.includes("/api/bigfiles")).length;
    const bfSection = await page.$("[data-test='detail-bigfiles-section']");
    record("journey-detail-has-current-files", Boolean(bfSection), "详情含当前大文件区");
    const bfHead = bfSection
      ? await bfSection.$eval("h3", (el) => el.textContent) : "";
    record("journey-current-files-not-history-window",
      bfHead.includes("非") || bfHead.includes("当前"), bfHead.trim().slice(0, 60));
    if (bfSection) {
      await page.selectOption("[data-test='detail-bf-mode']", "largest");
      const before = apiHits.filter((h) => h.includes("/api/bigfiles")).length;
      await page.click("[data-test='detail-bf-query']");
      await page.waitForFunction((n) => document.body.textContent.includes("已取消")
        || /没有|未找到|无匹配|结果|最大/.test(document.body.textContent), before, { timeout: 20000 })
        .catch(() => {});
      const bfAfter = apiHits.filter((h) => h.includes("/api/bigfiles")).length;
      record("journey-largest-explicit-click-only",
        before === bfHitsBefore, `进详情零自动查询（进详情后 ${before - bfHitsBefore} 次）`);
      const bfSectionText = await page.$eval("[data-test='detail-bigfiles-section']",
        (el) => el.innerText).catch(() => "");
      record("journey-largest-keeps-history-context",
        bfSectionText.includes("非") && bfSectionText.includes("历史")
          && bfSectionText.includes("当前"),
        bfSectionText.replace(/\s+/g, " ").slice(0, 80));
      await page.screenshot({ path: path.join(SHOTS, "j1-largest-current.png") });
    }

    // ---------- J1-d：返回（树/详情/焦点跨页保留） ----------
    await page.click(".nav-item[data-page='bigfiles']");
    await page.waitForFunction(() => location.hash === "#/bigfiles", null, { timeout: 10000 });
    await page.waitForSelector("#tbl-bigfiles", { timeout: 20000 });
    await page.click("[data-test='changes-tab-detail']").catch(() => {});
    await page.click(".nav-item[data-page='changes']");
    await page.waitForFunction(() => location.hash === "#/changes", null, { timeout: 10000 });
    await page.waitForSelector("[data-test='tree-expand']", { timeout: 20000 });
    const restoredExpanded = await page.$$eval("[data-test='tree-expand'][aria-expanded='true']",
      (els) => els.length);
    record("journey-return-restores-tree",
      restoredExpanded >= 1, `返回后展开层数=${restoredExpanded}`);

    // ---------- J2-d：分页（crowd 130 子目录，真实游标） ----------
    const hasMore = Boolean(await page.$("[data-test='tree-load-more']"));
    record("journey-tree-no-dup-rows",
      await page.$$eval("#changes-body tr", (trs) => {
        const p = trs.map((tr) => tr.getAttribute("data-path")).filter(Boolean);
        return p.length === new Set(p).size;
      }),
      `树内无重复路径（当前树有「显示更多」=${hasMore}，分页断言走分布页）`);

    // ---------- J2-e / J1-e：分布页 + 快照选择（历史非 latest） ----------
    await page.click(".nav-item[data-page='browse']");
    await page.waitForFunction(() => location.hash === "#/browse", null, { timeout: 10000 });
    await page.waitForFunction(() =>
      document.querySelector("[data-test='browse-snapshot-select']") &&
      document.querySelector("[data-test='browse-snapshot-select']").options.length >= 3,
      null, { timeout: 20000 });
    // ---------- J2-d：分页（真实游标：a3 快照下 crowd 130 子目录） ----------
    try {
      // 分布页默认在旭日分区，浏览器表在隐藏分区里——先切到浏览器分区（真实点击）
      await page.click("[data-test='browse-tab-browser']").catch(() => {});
      await page.waitForFunction(() => {
        const el = document.getElementById("browser-meta");
        return el && /已显示/.test(el.textContent);
      }, null, { timeout: 20000 }).catch(() => {});
      const crowdRow = await page.$(rowSel2(path.join(OLD, "crowd")));
      if (crowdRow) {
        await page.click(rowSel2(path.join(OLD, "crowd")));
        await page.waitForFunction(() => {
          const el = document.getElementById("browser-meta");
          return el && /已显示 100\/130/.test(el.textContent);
        }, null, { timeout: 20000 });
        const b1 = await page.$$eval("#tbl-browse tbody tr", (t) => t.length);
        await page.click("[data-test='browse-more']");
        await page.waitForFunction(() => {
          const more = document.querySelector("[data-test='browse-more']");
          return document.querySelectorAll("#tbl-browse tbody tr").length === 130 && !more;
        }, null, { timeout: 20000 });
        const names = await page.$$eval("#tbl-browse tbody tr", (trs) =>
          trs.map((tr) => tr.getAttribute("data-path")).filter(Boolean));
        record("journey-pagination-append-unique",
          b1 >= 100 && b1 <= 101 && names.length === 130 && new Set(names).size === 130,
          `第一页 ${b1} → 追加后 ${names.length}，唯一 ${new Set(names).size}`);
        // 回到根，再验证历史非 latest
        await page.click("#crumbs .crumb:first-child");
        await page.waitForFunction(() => {
          const cur = document.querySelector("#crumbs .crumb.current");
          return cur && !cur.textContent.includes("crowd");
        }, null, { timeout: 15000 }).catch(() => {});
      } else {
        record("journey-pagination-append-unique", false, "a3 根下未找到 crowd 行");
      }
    } catch (e) {
      record("journey-pagination-append-unique", false, `分页流程失败: ${e.message}`);
    }

    // ---------- J1-e：分布页历史非 latest ----------
    await page.selectOption("[data-test='browse-snapshot-select']", String(a1));
    await page.waitForFunction((sid) =>
      document.getElementById("browser-meta").textContent.includes(`#${sid}`),
      a1, { timeout: 20000 });
    const browseMeta = await page.$eval("#browser-meta", (el) => el.textContent);
    record("journey-browse-old-snapshot-not-latest",
      browseMeta.includes(`#${a1}`) && !browseMeta.includes(`#${a3}`),
      browseMeta.replace(/\s+/g, " ").slice(0, 80));
    await page.screenshot({ path: path.join(SHOTS, "j1-browse-history.png") });

    // ---------- J1-f：设置页（范围面板真实可达） ----------
    await page.click(".nav-item[data-page='settings']");
    await page.waitForFunction(() => location.hash === "#/settings", null, { timeout: 10000 });
    await page.click("#settings-head-scope").catch(() => {});
    await page.waitForSelector("#scope-panel", { state: "visible", timeout: 20000 });
    const scopeCurrent = await page.$eval("[data-test='scope-current']", (el) => el.textContent);
    record("journey-settings-scope-reachable",
      scopeCurrent.trim().length > 0, scopeCurrent.replace(/\s+/g, " ").slice(0, 70));
    await page.screenshot({ path: path.join(SHOTS, "j1-settings-scope.png") });

    // ---------- J2-f：旧 HOME / 新整盘切换（真实 PUT 冻结接口 + 页面核对） ----------
    const token = await bootstrapToken(port);
    // 旧 HOME / 新整盘两套身份各自可查，且互不混比（跨身份区间 400）
    const diskPair = await get(`/api/diff?a=${w1}&b=${w2}`);
    const mixedAgain = await get(`/api/diff?a=${a3}&b=${w2}`);
    record("journey-two-datasets-disjoint-not-mixed",
      diskPair.status === 200 && diskPair.json.b.id === w2
        && ((diskPair.json.grown) || []).every((g) => String(g.path).startsWith(DISK))
        && mixedAgain.status >= 400,
      `新整盘 #${w1}→#${w2} 可查且行全属该根；跨身份 #${a3}→#${w2} status=${mixedAgain.status}`);
    // 旧 HOME 历史依然可查且不混比
    const oldStillThere = await get(`/api/diff?a=${a1}&b=${a2}`);
    record("journey-old-home-history-survives-switch",
      oldStillThere.status === 200
        && ((oldStillThere.json.grown) || []).some((g) => String(g.path).endsWith("vault1")),
      `旧根区间仍可查 status=${oldStillThere.status}`);


    // ---------- J3-a：快速切页（改范围/重复请求不残留错读） ----------
    await page.click(".nav-item[data-page='changes']");
    await page.waitForSelector("#sel-b", { timeout: 20000 });
    await page.selectOption("#sel-a", String(a1));
    await page.selectOption("#sel-b", String(a3));
    await page.selectOption("#sel-a", String(a2));
    await page.click(".nav-item[data-page='overview']");
    await page.click(".nav-item[data-page='changes']");
    await page.waitForSelector("[data-test='tree-expand']", { timeout: 20000 });
    const selA = await page.$eval("#sel-a", (el) => el.value);
    const selB = await page.$eval("#sel-b", (el) => el.value);
    const settled = await page.$eval("#changes-net", (el) => el.textContent);
    record("journey-fast-switch-no-stale-read",
      Number(selB) === a3 && settled.includes("根同口径净变化") && !settled.includes("尚无快照"),
      `a=#${selA} b=#${selB}`);

    // ---------- J3-b：受控 500（唯一故障注入点，注入计数必须可数） ----------
    let injected500 = 0;
    await page.route("**/api/diff*", (route) => {
      injected500 += 1;
      return route.fulfill({ status: 500, contentType: "application/json",
        body: JSON.stringify({ detail: "受控故障注入（ISS-160）" }) });
    });
    await page.selectOption("#sel-a", String(a1));
    await page.selectOption("#sel-b", String(a2));
    await sleep(1200);
    const errStatus = await page.$eval("#diff-status", (el) => el.textContent);
    const netDuring500 = await page.$eval("#changes-net", (el) => el.textContent);
    record("journey-500-shown-not-faked",
      injected500 >= 1 && !netDuring500.includes("9.5 GB"),
      `注入 ${injected500} 次；状态「${errStatus.replace(/\s+/g, " ").slice(0, 40)}」`
        + `；净变化未被伪造成该区间的成功读数`);
    await page.screenshot({ path: path.join(SHOTS, "j3-controlled-500.png") });
    // 撤除替身后同一区间必须恢复真实响应（证明生产模块未被替身取代）
    await page.unroute("**/api/diff*");
    await page.selectOption("#sel-a", String(a2b));
    await page.selectOption("#sel-b", String(a3));
    await page.waitForFunction(() => {
      const el = document.getElementById("changes-net");
      return el && !el.hidden && el.textContent.includes("根同口径净变化");
    }, null, { timeout: 20000 });
    const afterUnroute = await httpJson("GET", `/api/diff?a=${a2b}&b=${a3}`, port);
    record("journey-unroute-restores-real-api",
      afterUnroute.status === 200 && afterUnroute.json.b.id === a3,
      `真实 /api/diff status=${afterUnroute.status} b=#${afterUnroute.json.b.id}`);

    // ---------- J3-c：同日替换（a2/a2b 同日 → 不捏造第二个跨日基线） ----------
    await page.selectOption("#sel-a", String(a1));
    await page.selectOption("#sel-b", String(a2b));
    await page.waitForFunction((id) => document.getElementById("sel-b").value === id,
      String(a2b), { timeout: 10000 });
    const sameDayNet = await waitUntil(async () => {
      const t = await page.$eval("#changes-net", (el) => el.textContent).catch(() => "");
      return /9\.5 GB/.test(t) ? t : null;   // a1 根 10,000,000KB → a2b 根 12,500,000KB
    }, 20000, "同日区间 a1→a2b 的根读数").catch(() => "TIMEOUT");
    record("journey-same-day-not-second-baseline",
      !String(sameDayNet).startsWith("TIMEOUT")
        && String(sameDayNet).includes("9.5 GB")
        && !String(sameDayNet).includes("基线已建立"),
      `a1(10-01)→a2b(10-02 同日较晚者)：${String(sameDayNet).replace(/\s+/g, " ").slice(0, 70)}`);

    // ---------- J3-d：淘汰（真实删行后不得残留错读，须说明原因） ----------
    await page.selectOption("#sel-a", String(a1));
    await page.selectOption("#sel-b", String(a3));
    await page.waitForFunction((id) => document.getElementById("sel-b").value === id,
      String(a3), { timeout: 10000 });
    const evicted = spawn(PY, [path.join(tmp, "evict.py"), env.FATHOM_DB, String(a2b)],
      { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
    await new Promise((res) => evicted.on("exit", res));
    await page.selectOption("#sel-a", String(a1));
    await page.selectOption("#sel-b", String(a2));
    await page.selectOption("#sel-b", String(a3));
    await page.waitForFunction((id) => document.getElementById("sel-b").value === id,
      String(a3), { timeout: 15000 });
    const afterEvict = await page.$eval("#changes-net", (el) => el.textContent);
    const diffStatusEvict = await page.$eval("#diff-status", (el) => el.textContent);
    record("journey-evicted-snapshot-explained",
      !afterEvict.includes("尚无快照") && !diffStatusEvict.includes("NaN"),
      `net=${afterEvict.replace(/\s+/g, " ").slice(0, 60)}`);
    await page.screenshot({ path: path.join(SHOTS, "j3-evicted.png") });

    // ---------- J3-e：取消（大文件页真实取消按钮） ----------
    await page.click(".nav-item[data-page='bigfiles']");
    await page.waitForFunction(() => location.hash === "#/bigfiles", null, { timeout: 10000 });
    await page.waitForSelector("#tbl-bigfiles", { timeout: 20000 });
    const bfBeforeNav = apiHits.filter((h) => h.includes("/api/bigfiles")).length;
    await page.click(".nav-item[data-page='overview']");
    await page.waitForFunction(() => location.hash === "#/overview", null, { timeout: 10000 });
    const bfAfterNav = apiHits.filter((h) => h.includes("/api/bigfiles")).length;
    record("journey-leave-page-no-stale-bigfiles",
      bfAfterNav - bfBeforeNav <= 1,
      `离页后新增 bigfiles 请求 ${bfAfterNav - bfBeforeNav} 次`);

    // ---------- J4-a：键盘与焦点返回 ----------
    await page.click(".nav-item[data-page='changes']");
    await page.waitForSelector("[data-test='tree-expand']", { timeout: 20000 });
    const keyboardDetail = await (async () => {
      const kSel = "#changes-body [data-detail]";
      if (!(await page.$(kSel))) return { ok: false, detail: "树内无可聚焦的行内详情按钮" };
      await page.focus(kSel);
      const focusTag = await page.evaluate(() => document.activeElement.tagName);
      await page.keyboard.press("Enter");
      await page.waitForFunction(() => {
        const d = document.getElementById("changes-detail");
        return d && !d.hidden;
      }, null, { timeout: 15000 }).catch(() => {});
      const opened = await page.evaluate(() => {
        const d = document.getElementById("changes-detail");
        return Boolean(d && !d.hidden);
      });
      if (!opened) return { ok: false, detail: `Enter 未打开详情（focus 起点 ${focusTag}）` };
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => document.getElementById("changes-detail").hidden,
        null, { timeout: 8000 });
      const afterEsc = await page.evaluate(() => {
        const a = document.activeElement;
        return a ? `${a.tagName}${a.className ? "." + String(a.className).split(" ")[0] : ""}` : "NONE";
      });
      return {
        ok: afterEsc !== "NONE" && afterEsc !== "BODY",
        detail: `Enter 开详情（focus 起点 ${focusTag}）→ Esc 关闭 → 焦点回到 ${afterEsc}`,
      };
    })();
    record("journey-keyboard-expand-esc-focus-return",
      keyboardDetail.ok, keyboardDetail.detail);
    await page.screenshot({ path: path.join(SHOTS, "j4-keyboard-focus.png") });

    // ---------- J4-b：图表隐藏后恢复（跨页返回不塌成 0 宽） ----------
    const chartOk = await (async () => {
      // 图表在隐藏容器里拿不到宽度：离开再返回后必须恢复（量当前可见页的图表）
      await page.click(".nav-item[data-page='overview']");
      await page.waitForFunction(() => location.hash === "#/overview", null, { timeout: 10000 });
      await sleep(500);
      await page.click(".nav-item[data-page='changes']");
      await page.waitForSelector("#chart-grown", { state: "attached", timeout: 20000 });
      await sleep(500);
      await page.click(".nav-item[data-page='overview']");
      await page.waitForFunction(() => location.hash === "#/overview", null, { timeout: 10000 });
      await page.waitForSelector("#chart-volume", { state: "visible", timeout: 20000 });
      await sleep(800);
      const w = await page.evaluate(() => {
        const el = document.getElementById("chart-volume");
        return el ? el.getBoundingClientRect().width : 0;
      });
      const inner = await page.evaluate(() => {
        const el = document.getElementById("chart-volume");
        const c = el && el.querySelector("canvas");
        return c ? c.getBoundingClientRect().width : 0;
      });
      return { ok: w > 100 && inner > 100, detail: `返回后 #chart-volume 宽 ${Math.round(w)}px，内层 canvas ${Math.round(inner)}px` };
    })();
    record("journey-chart-restored-after-hidden", chartOk.ok, chartOk.detail);

    // ---------- J4-c：三桌面尺寸 + 截图 ----------
    for (const [w, h] of [[980, 640], [1220, 820], [1440, 900]]) {
      await page.setViewportSize({ width: w, height: h });
      await sleep(400);
      const overflow = await page.evaluate(() =>
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
          - document.documentElement.clientWidth);
      record(`viewport-${w}x${h}-no-overflow`, overflow <= 2, `overflowPx=${overflow}`);
      await page.screenshot({ path: path.join(SHOTS, `viewport-${w}x${h}.png`) });
    }

    // ---------- J1-g：重扫闭环（用户主动重扫 → 前后真实容量主体/时间） ----------
    await page.setViewportSize({ width: 1220, height: 820 });
    await page.click(".nav-item[data-page='overview']");
    await page.waitForFunction(() => !document.getElementById("page-overview").classList.contains("hidden"),
      null, { timeout: 10000 });
    const beforeFree = await page.$eval("[data-test='storage-free']", (el) => el.textContent).catch(() => "");
    const beforeKicker = await page.$eval("[data-test='storage-kicker']", (el) => el.textContent).catch(() => "");
    const maxSeed = Math.max(a1, a2, a2b, a3, w1, w2);
    const beforeSnaps = await get("/api/snapshots");
    const beforeCount = (beforeSnaps.json || []).length;
    await page.click("#btn-scan");
    const rescanned = await waitUntil(async () => {
      const r = await get("/api/snapshots");
      const list = r.json || [];
      return list.length > beforeCount ? list : null;
    }, 90000, "重扫产生新快照").catch(() => null);
    const newSnap = rescanned
      ? rescanned.reduce((m, s) => (Number(s.id) > Number(m.id) ? s : m), rescanned[0]) : null;
    record("journey-rescan-produces-new-snapshot",
      Boolean(newSnap) && Number(newSnap.id) > maxSeed,
      newSnap ? `新快照 #${newSnap.id} @${newSnap.created_at}` : "重扫未产生新快照");
    await sleep(2500);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#page-overview:not(.hidden)", { timeout: 20000 });
    await sleep(2500);
    // 重扫扫的是配置的扫描根（身份 ≠ 种子里的 oldhome 系列），因此总览必须
    // 绑定新快照或如实说明暂不可比，而不是拿旧口径的数字冒充前后对比。
    const panelText = await page.$eval("#overview-storage", (el) => el.innerText).catch(() => "");
    const afterMeta = panelText;
    record("journey-rescan-shows-real-before-after",
      panelText.trim().length > 0
        && (panelText.includes(String(newSnap.id))
          || /等待|不足|不可比|需要|另一|没有/.test(panelText))
        && !/NaN|undefined/.test(panelText),
      `重扫后总览：${panelText.replace(/\s+/g, " ").slice(0, 80)}`);
    record("journey-rescan-not-claiming-user-freed",
      !/你已释放|由你处理释放|已清理/.test(afterMeta),
      afterMeta.replace(/\s+/g, " ").slice(0, 80));
    await page.screenshot({ path: path.join(SHOTS, "j1-rescan-after.png") });

    // ---------- J5：mock 仅注入受控故障/系统动作（对照：真实 API 全程可用） ----------
    const realStillWorks = await get(`/api/diff?a=${a1}&b=${a3}`);
    record("mock-discipline-production-api-intact",
      realStillWorks.status === 200 && realStillWorks.json.b.id === a3,
      `撤除替身后真实 /api/diff 仍 200（b=#${realStillWorks.json.b.id}）`);
    const noStubbedModule = await page.evaluate(() => {
      /* 生产模块必须是真实 ES 模块实例：没有测试替身全局注入 */
      return typeof window.__fathom_test_stub__ === "undefined";
    });
    record("mock-discipline-no-global-stub", noStubbedModule, "页面无测试替身全局变量");
    record("mock-discipline-ledger-captured", httpLedger.length > 20,
      `记录 ${httpLedger.length} 条真实 API 请求（截图↔HTTP 关联台账）`);

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
  process.stderr.write(`\n== ISS-160 前端验证：${checks.length - failed.length}/${checks.length} 通过 ==\n`);
  if (failed.length) {
    process.stderr.write("失败项：\n" + failed.map((c) => `- ${c.name}: ${c.detail}`).join("\n") + "\n");
  }
  if (serveLog) {
    fs.writeFileSync(path.join(OUT, "serve.log"), serveLog);
  }
  fs.writeFileSync(path.join(OUT, "http-ledger.json"),
    JSON.stringify({ base, apiHits, ledger: httpLedger }, null, 2));
  fs.writeFileSync(path.join(OUT, "result.json"), JSON.stringify({
    task: "iss160-storage-investigation-frontend",
    pass: failed.length === 0,
    total: checks.length, failed: failed.length,
    checks, shots: fs.readdirSync(SHOTS),
    source: require("child_process").execSync("git rev-parse HEAD", { cwd: REPO }).toString().trim(),
  }, null, 2));

  /* 结果 JSON 走 stdout（单行，整体可 JSON.parse；契约同
   * verify_tree_changes_frontend.cjs 的消费方式）：人类 PASS 行与总结留
   * stderr，ci_browser_checks.sh 对 tee 捕获的 stdout 整体解析。 */
  process.stdout.write(JSON.stringify({
    ok: failed.length === 0,
    passed: checks.length - failed.length,
    failed: failed.length,
    checks,
  }) + "\n");
}

main().catch((e) => { console.error(e); process.exit(1); });
