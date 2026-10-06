#!/usr/bin/env node
/* ISS-151 目录内文件排查与 Finder 衔接回归：真实 FastAPI（隔离 runtime/端口）
 * + 受控文件目录 + 生产页面实点（Playwright 单实例串行，不并行开浏览器）。
 *
 * 覆盖任务卡验收：
 * - 查询仅显式点击发起：进页/开详情不后台遍历（零请求断言）；
 * - 三层历史定位 → 当前 largest（长期未改大文件）→ recent 切换；
 * - 历史 a→b 与当前 st_size/查询时间口径在详情内同屏可辨；
 * - 返回历史 a/b：展开/滚动/焦点/打开的详情与大文件结果跨页保持；
 * - 共享查询域：wait=false 拿句柄 → status 轮询 → 终态取体；
 *   离开查询面/改范围按句柄真实取消（POST cancel 计数 + status 收敛），
 *   双目录并发取消不误伤；浏览器中止不冒充服务端取消（node 层边界）；
 * - 当前文件移动（404 无法定位 + 历史保留）/权限/空/截断（真实 find）；
 *   失败/缓存过期标签由 verify_frontend_refresh 合成夹具覆盖；
 * - 路径转义（中文+空格）、键盘动作、980/1220/1440 三桌面尺寸；
 * - Finder 只验边界请求（reveal 400/404 原始 HTTP；页面内 reveal 经路由
 *   拦截，不触发真实系统动作——真实动作归 ISS-161）。
 *
 * 隔离边界（TESTING.md）：FATHOM_RUNTIME_DIR / FATHOM_DB / FATHOM_SCAN_ROOT /
 * FATHOM_PORT 全部指向临时目录与空闲端口；起服务前断言 /health 的 pid 等于
 * 自 spawn 进程且 runtime_mode=development；结束杀掉自起进程并复核端口释放。
 *
 * 用法：node scripts/verify_directory_bigfiles_frontend.cjs [--shots <dir>]
 * 退出码 0 = 全部通过；结果 JSON 打到 stdout（ok/passed/failed/checks）。
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
  || (fs.existsSync(path.join(REPO, ".venv", "bin", "python"))
    ? path.join(REPO, ".venv", "bin", "python")
    : path.join(REPO, ".runtime", "bin", "python"));

const args = process.argv.slice(2);
const shotsIdx = args.indexOf("--shots");
const SHOTS = shotsIdx >= 0 && args[shotsIdx + 1]
  ? path.resolve(args[shotsIdx + 1])
  : path.join(REPO, "verify-results", "iss151-dir-bigfiles", "shots");
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

/* ---------- 受控文件目录 + 合成快照种子（隔离运行根断言 fail-closed） ---------- */

const SEED_PY = `"""ISS-151 浏览器回归种子：受控文件树 + 合成快照（隔离运行根断言）。

真实文件（find 直接消费）：
- vault/media/archives/old/*.img  3 个 300MB 稀疏文件，mtime 400 天前
  （largest 命中、recent 7 天不命中的「长期未改大文件」）
- vault/media/recent/big_new.mov  250MB，mtime 现在（recent 正例）
- vault/media/recent/mid_old.mp4  200MB，mtime 30 天前（recent 7 天排除）
- vault/empty/                    空目录（no_match）
- vault/locked/inner/             chmod 000（permission_denied）
- vault/many/f*.img               210 个 150MB 稀疏文件（topn=200 截断）
- vault/中文 目录/带 空格.bin      120MB（路径转义）
- vault/moved/keep.bin            130MB（查询后移走 → 404 无法定位）
- vault/slow/c{0..7}/d###/f*.bin  72 万+ 0 字节目录项（拖长 find 在途窗口，
  供离开取消/改范围取消/手动取消/中止边界四类场景共用）
- .slow-tpl/                      slow 的构建模板（450×100 真实文件 +
  APFS clonefile 克隆 8 份；构建后保留，find 查询域只针对 vault 子目录）

slow 加肥依据（ISS-151 CI 确定性修复，实测 arm64 本机）：
- 90k 项（旧夹具）热遍历仅 0.57-0.74s，而取消链路（提交 → 202+渲染 →
  POLL_MS=800 首轮轮询不必等、按钮即现 → click → POST cancel）实测约
  0.3-1.5s——CI runner 上第三次热遍历（手动取消段）会被链路反超，
  取消落在终态后 state=no_match，实证复现。
- 72 万项（8×90k APFS 克隆，clonefile 8 份约 15s，远快于逐文件创建）
  遍历首跑实测 7.53s（fixture-seed-ok 回显），≥2s 合同底线由脚本断言
  把关，对取消链路 0.3-1.5s 有 5 倍以上余量；套件总耗时与旧 90k 夹具
  基本持平（clonefile 抵消，本机实测 74.0s vs 基线 73.4s）。
  36 万方案连跑实测 1.63-3.76s，跨 run 会跌破 2s 底线、对取消链路
  0.3-1.5s 只余 1.1 倍余量（自证断言当场拦截），故弃用；54 万带载
  实测 2.5-15s，同样贴线。72 万在断言底线之上留出跨 run 波动余量，
  代价是种子 +~20s 与 J/K 段遍历变长（K 段预算已放宽到 90s）。
- 种子内自证一遍 find（find_sec 回传，脚本断言 find_sec ≥ 2.0；本机实测
  该夹具冷 15.3s / 热 14.9s 几乎无差，单遍既是预热也是测量）。自证顺带
  预热目录项缓存：四类场景全部吃热缓存，确定性由「72 万项遍历
  ≥2s（断言）>> 取消链路 ≈1s」的规模差保证，不依赖冷/热时序运气。

DB 快照：decoy(#1,#2) 旧数据集；vault(#3,#4) 最新数据集（详情历史口径）。
"""
import ctypes, ctypes.util, json, os, shutil, sqlite3, subprocess, sys, time

assert os.environ.get("FATHOM_RUNTIME_DIR", "").startswith(sys.argv[1]), "拒绝在非隔离运行根运行"

from fathom import config, db

scanroot = str(config.DEFAULT_ROOT)
vault = os.path.join(scanroot, "vault")
now = time.time()
OLD = now - 400 * 86400
MID = now - 30 * 86400

def sparse(rel, size, mtime=None):
    p = os.path.join(scanroot, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.seek(size - 1)
        f.write(b"\\x00")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p

os.makedirs(os.path.join(vault, "empty"), exist_ok=True)
os.makedirs(os.path.join(vault, "locked", "inner"))
os.chmod(os.path.join(vault, "locked", "inner"), 0)
for name in ("a", "b", "c"):
    sparse(f"vault/media/archives/old/{name}.img", 300 * 1024 * 1024, OLD)
sparse("vault/media/recent/big_new.mov", 250 * 1024 * 1024, now)
sparse("vault/media/recent/mid_old.mp4", 200 * 1024 * 1024, MID)
sparse("vault/中文 目录/带 空格.bin", 120 * 1024 * 1024, now)
sparse("vault/moved/keep.bin", 130 * 1024 * 1024, now)
for i in range(210):
    sparse(f"vault/many/f{i:03d}.img", 150 * 1024 * 1024, now)
# slow 目录：模板 450×200 真实 0 字节文件，再 APFS clonefile 克隆 8 份到
# slow/c0..c7（72 万+ 目录项）。clonefile 不可用（非 APFS 卷）时 fail-fast，
# 不静默退回小夹具——那会让取消类用例重新落入 CI 时序竞争。
TPL = os.path.join(scanroot, ".slow-tpl")
os.makedirs(TPL, exist_ok=True)
for d in range(450):
    p = os.path.join(TPL, f"d{d:03d}")
    os.makedirs(p, exist_ok=True)
    for i in range(200):
        open(os.path.join(p, f"f{i:05d}.bin"), "wb").close()

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
slow = os.path.join(vault, "slow")
os.makedirs(slow, exist_ok=True)
SLOW_CLONES = 8
for k in range(SLOW_CLONES):
    dst = os.path.join(slow, "c%d" % k)
    if _libc.clonefile(TPL.encode(), dst.encode(), 0) != 0:
        err = ctypes.get_errno()
        raise SystemExit(
            "clonefile(%s) 失败 errno=%d：非 APFS 卷无法构建加肥夹具，"
            "取消类用例失去确定性前提" % (dst, err))

# 加肥自证：按服务同一 find 口径（recent/-mtime -7 + min 100MB -size）跑一遍，
# 既是预热也是测量（本机实测该夹具冷热差异 <5%）；find_sec 由脚本断言 ≥ 2.0s。
FIND = shutil.which("find") or "/usr/bin/find"
FIND_ARGS = [FIND, slow, "-mtime", "-7", "-size", "+104857600c", "-print0"]
t_find = time.monotonic()
subprocess.run(FIND_ARGS, stdout=subprocess.DEVNULL, check=True)
find_sec = time.monotonic() - t_find
slow_entries = SLOW_CLONES * (450 * 200 + 450) + SLOW_CLONES

def insert(conn, day, root, entries, min_kb=1024):
    cur = conn.execute(
        "INSERT INTO snapshots(created_at, root, dir_count, denied_count, du_seconds, "
        "total_kb, min_kb, collection_status, vanished_count, exclude_names, "
        "confirmed_missing_count, path_unverified_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{day}T12:00:00", root, len(entries) + 1, 0, 0.0,
         max(entries.values(), default=0), min_kb, "full", 0, "", None, None))
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
    ids["decoy_a"] = insert(conn, "2026-09-19", os.path.join(scanroot, "decoy"),
                            {os.path.join(scanroot, "decoy"): 5000})
    ids["decoy_b"] = insert(conn, "2026-09-20", os.path.join(scanroot, "decoy"),
                            {os.path.join(scanroot, "decoy"): 5200})
    v = {scanroot + "/vault": 200000, scanroot + "/vault/media": 120000,
         scanroot + "/vault/media/archives": 80000,
         scanroot + "/vault/media/archives/old": 20000,
         scanroot + "/vault/moved": 60000}
    ids["v_a"] = insert(conn, "2026-09-21", vault, v)
    v[scanroot + "/vault"] = 260000
    v[scanroot + "/vault/media"] = 150000
    v[scanroot + "/vault/media/archives"] = 100000
    v[scanroot + "/vault/media/archives/old"] = 40000
    v[scanroot + "/vault/moved"] = 61000
    ids["v_b"] = insert(conn, "2026-09-22", vault, v)
    conn.close()
    print(json.dumps({
        "ids": ids, "scanroot": scanroot,
        "scope_version": config.BIGFILE_SCOPE_VERSION,
        "slow_entries": slow_entries,
        "slow_find_sec": round(find_sec, 2),
    }), flush=True)

main(sys.argv[1])
`;

/** 服务端去重键 → 确定性 task_id（与 bigfiles.BigfilesManager.task_id_for
 * 同一拼接与哈希口径；scope_version 取种子回传的 config 值）。
 * ISS-176：服务端键追加 scope_key 第 7 段（本套件未启用范围 → 空串），
 * 套件拼接同步补段，否则确定性句柄与服务端不一致（404）。 */
function taskIdFor(resolvedRoot, mode, days, minMb, topn, scopeVersion) {
  const raw = [resolvedRoot, mode, days, minMb, topn, scopeVersion, ""]
    .map((x) => String(x)).join("\x1f");
  return "bf-" + crypto.createHash("sha256").update(raw, "utf8").digest("hex").slice(0, 16);
}

async function main() {
  // ---------- 1. 隔离运行根 + 种子（含受控文件树） ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "iss151-verify-"));
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

  process.stderr.write("种子创建中（90000 真实文件 + clonefile 加肥 slow 至 72 万目录项，约 1-2 分钟）…\n");
  const seed = spawn(PY, [seedPath, tmp], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const seedOut = await new Promise((resolve, reject) => {
    let buf = "";
    seed.stdout.on("data", (c) => (buf += String(c)));
    seed.stderr.on("data", (c) => (buf += String(c)));
    seed.on("exit", (code) => code === 0 ? resolve(buf) : reject(new Error(`seed exit ${code}: ${buf.slice(-1200)}`)));
  });
  const seedInfo = JSON.parse(seedOut.trim().split("\n").filter((l) => l.startsWith("{")).pop());
  const SR = seedInfo.scanroot;
  const SV = seedInfo.scope_version;
  // 加肥自证并入 fixture-seed-ok（不新增用例数，CI 门禁按 36 计数）：
  // 夹具规模与遍历时长是取消类用例「取消点击时 find 仍在飞」的
  // 确定性前提，退化即 fail-fast，不许静默竞争。
  record("fixture-seed-ok",
    seedInfo.ids.v_a === 3 && seedInfo.ids.v_b === 4 && seedInfo.scope_version === 1 &&
      seedInfo.slow_entries >= 720000 && seedInfo.slow_find_sec >= 2.0,
    `ids=${JSON.stringify(seedInfo.ids)} sv=${SV} ` +
      `slow=${seedInfo.slow_entries}项 find=${seedInfo.slow_find_sec}s`);
  const real = (p) => fs.realpathSync(p);
  const taskOf = (dir, mode, days, minMb, topn) =>
    taskIdFor(real(dir), mode, days, minMb, topn, SV);

  // ---------- 2. 起真实 serve 并核对身份 ----------
  const child = spawn(PY, ["-m", "fathom", "serve"], { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const serveErr = [];
  child.stderr.on("data", (c) => serveErr.push(String(c)));
  const health = await waitUntil(async () => {
    const r = await httpJson("GET", "/health", port);
    return r.status === 200 && r.json && r.json.pid ? r.json : null;
  }, 30000, "serve /health");
  record("serve-health-identity",
    health.pid === child.pid && health.runtime_mode === "development" && health.port === port,
    `pid=${health.pid} mode=${health.runtime_mode}`);

  const base = `http://127.0.0.1:${port}`;
  const boot = await httpJson("GET", "/api/bootstrap", port);
  const TOKEN = boot.json && boot.json.token;
  record("bootstrap-token-ok", Boolean(TOKEN));
  const jpost = (p, obj) => httpJson("POST", p, port, {
    body: JSON.stringify(obj), headers: { "Content-Type": "application/json", "X-Fathom-Token": TOKEN },
  });
  const statusOf = (taskId) =>
    httpJson("GET", `/api/bigfiles/status?task_id=${encodeURIComponent(taskId)}`, port);
  const pollStatus = async (taskId, pred, timeoutMs, what) => {
    const deadline = Date.now() + timeoutMs;
    let last = null;
    while (Date.now() < deadline) {
      const r = await statusOf(taskId);
      /* ISS-176：范围身份贯通后查询可能先于轮询完成并被清理（404「未知或
       * 已过期」）——404 即「不再有在途查询」，按已清理终态交付。 */
      if (r.status === 404) return { terminal: true, cleaned_up: true };
      if (r.status === 200) {
        last = r.json;
        if (pred(last)) return last;
      }
      await sleep(150);
    }
    throw new Error(`状态轮询超时: ${what}（最后 ${JSON.stringify(last)}）`);
  };

  // ---------- 3. 真实 Chromium 实点生产页面（单实例串行） ----------
  let browser = null;
  const consoleErrors = [];
  const dialogs = [];
  const apiCounts = { query: 0, status: 0, cancel: 0 };
  const revealBlocked = [];
  try {
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
    page.on("dialog", (d) => { dialogs.push(d.message()); d.dismiss(); });
    page.on("request", (r) => {
      const u = r.url();
      if (u.includes("/api/bigfiles/status")) apiCounts.status += 1;
      else if (u.includes("/api/bigfiles/cancel")) apiCounts.cancel += 1;
      else if (u.includes("/api/bigfiles?")) apiCounts.query += 1;
    });
    // 页面内 Finder 定位一律拦截（记录请求体，不触发真实 open -R——归 ISS-161）
    await page.route("**/api/reveal", (route) => {
      revealBlocked.push(route.request().postDataJSON());
      route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
    });

    const resetApiCounts = () => { apiCounts.query = 0; apiCounts.status = 0; apiCounts.cancel = 0; };
    const tbodyRows = () => page.evaluate(() =>
      [...document.querySelectorAll("#tbl-bigfiles tbody tr")].map((tr) => tr.textContent.trim()));
    const detailRows = () => page.evaluate(() =>
      [...document.querySelectorAll("[data-test='detail-bigfiles-tbody'] tr")].map((tr) => tr.textContent.trim()));
    // 展开是切换语义（ISS-148）：按 aria-expanded 幂等展开，供多次进入同一场景
    const expandIfNeeded = async (dir, what) => {
      const readAria = () => page.evaluate((d) => {
        const btn = document.querySelector(`#changes-body [data-expand="${d}"]`);
        return btn ? btn.getAttribute("aria-expanded") : null;
      }, dir);
      if ((await readAria()) === "true") return;
      await page.click(`#changes-body [data-expand="${dir}"]`);
      await waitUntil(async () => (await readAria()) === "true", 8000, `展开 ${what}`);
    };
    const rowExists = (p) => page.evaluate((d) =>
      Boolean(document.querySelector(`#changes-body tr[data-path="${d}"]`)), p);

    /* ---- A. 显式点击才启动：进页 / 开详情零请求 ---- */
    await page.goto(`${base}/#/bigfiles`, { waitUntil: "networkidle" });
    resetApiCounts();
    await sleep(1000);
    record("page-open-no-auto-query",
      apiCounts.query === 0 && apiCounts.status === 0 &&
        (await page.textContent("#tbl-bigfiles tbody")).includes("点「查询」"),
      `query=${apiCounts.query} status=${apiCounts.status}`);

    /* ---- B. 页面范围正确 + recent 正例/负例（真实 find，缓存可辨） ---- */
    await page.fill("#bf-path", `${SR}/vault/media/recent`);
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("查询完成"),
      20000, "recent 正例完成");
    const recentBody = await tbodyRows();
    record("page-recent-scope-correct",
      recentBody.join("\n").includes("big_new.mov") &&
        !recentBody.join("\n").includes("mid_old.mp4") &&
        recentBody.join("\n").includes("250.0 MB") &&
        recentBody[0].includes("media/recent") && recentBody[0].includes("7 天"),
      JSON.stringify(recentBody).slice(0, 200));

    /* ---- C. 详情入口：三层历史定位 → 当前 largest → recent 切换 ---- */
    await page.click('a[data-page="changes"]');
    await waitUntil(async () => (await page.textContent("#diff-status")).includes("已对比快照 #3 → #4"),
      20000, "默认 vault 对比完成");
    // 三层展开：media → archives → old（路径均为 DB 真实绝对路径）
    await page.click(`#changes-body [data-expand="${SR}/vault/media"]`);
    await waitUntil(async () => page.evaluate((p) =>
      Boolean(document.querySelector(`#changes-body tr[data-path="${p}"]`)), `${SR}/vault/media/archives`), 10000, "archives 展示");
    await page.click(`#changes-body tr[data-path="${SR}/vault/media/archives"] [data-expand]`);
    await waitUntil(async () => page.evaluate((p) =>
      Boolean(document.querySelector(`#changes-body tr[data-path="${p}"]`)), `${SR}/vault/media/archives/old`), 10000, "old 展示");
    // 开详情前：打开详情本身不发起大文件查询
    resetApiCounts();
    await page.click(`#changes-body tr[data-path="${SR}/vault/media/archives/old"] .tree-name`);
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("changes-detail").hidden &&
      Boolean(document.querySelector("#detail-trend-chart canvas"))), 15000, "old 详情打开");
    await sleep(700);
    const headBefore = await page.evaluate(() => ({
      idle: (document.querySelector("[data-test='detail-bigfiles-tbody']")?.textContent || "").includes("尚未查询"),
      sectionHead: (document.querySelector("[data-test='detail-bigfiles-section'] h3")?.textContent || ""),
      range: (document.querySelector("[data-test='tree-detail-range-stats']")?.textContent || ""),
      mode: document.getElementById("detail-bf-mode")?.value,
    }));
    record("detail-open-no-auto-query-and-history-bound",
      apiCounts.query === 0 && headBefore.idle &&
        headBefore.sectionHead.includes("当前大文件") && headBefore.sectionHead.includes("历史区间") &&
        headBefore.range.includes("#3 → #4") && headBefore.range.includes("19.5 MB") &&
        headBefore.range.includes("39.1 MB") && headBefore.mode === "largest",
      JSON.stringify(headBefore).slice(0, 240));

    // largest：长期未改大文件（mtime 400 天前仍命中），口径标注当前/st_size
    await page.click("[data-test='detail-bf-query']");
    await waitUntil(async () => (await detailRows()).join("\n").includes("300.0 MB"),
      20000, "largest 完成");
    const largestRows = await detailRows();
    const largestMeta = await page.evaluate(() => ({
      status: (document.querySelector("[data-test='detail-bigfiles-tbody'] .hint-row")?.textContent || ""),
      ctx: document.getElementById("detail-bf-context")?.textContent || "",
      ctxHidden: document.getElementById("detail-bf-context")?.hidden,
      n: document.querySelectorAll("[data-test='detail-bigfiles-tbody'] [data-reveal]").length,
    }));
    record("detail-largest-shows-old-bigfiles",
      largestRows.filter((t) => t.includes("300.0 MB")).length === 3 &&
        largestMeta.n === 3 && largestMeta.status.includes("当前最大") &&
        largestMeta.status.includes("查询完成") &&
        largestMeta.ctx.includes("查询发起于") && largestMeta.ctx.includes("st_size") &&
        largestMeta.ctxHidden === false,
      JSON.stringify({ rows: largestRows, meta: largestMeta }).slice(0, 320));

    // recent 切换：同目录 7 天无 ≥100MB 修改 → 无匹配可辨
    await page.selectOption("#detail-bf-mode", "recent");
    await page.click("[data-test='detail-bf-query']");
    await waitUntil(async () => (await detailRows()).join("\n").includes("无匹配文件"),
      20000, "recent 无匹配");
    const recentNone = await detailRows();
    record("detail-recent-switch-no-match",
      recentNone.join("\n").includes("无匹配文件") &&
        recentNone.join("\n").includes("近 7 天没有 ≥ 100MB 的文件修改"),
      JSON.stringify(recentNone).slice(0, 160));

    /* ---- D. 移动路径：404 无法定位 + 历史保留 ---- */
    await page.click(`#changes-body tr[data-path="${SR}/vault/moved"] .tree-name`);
    await waitUntil(async () => page.evaluate((p) =>
      (document.querySelector("#changes-detail .detail-path")?.textContent || "") === p,
    `${SR}/vault/moved`), 10000, "moved 详情");
    await page.click("[data-test='detail-bf-query']");
    await waitUntil(async () => (await detailRows()).join("\n").includes("keep.bin"),
      20000, "moved largest 完成");
    fs.renameSync(path.join(SR, "vault", "moved"), path.join(SR, "vault", "moved_away"));
    await page.click("[data-test='detail-bf-query']");
    await waitUntil(async () => (await detailRows()).join("\n").includes("无法定位"),
      20000, "404 无法定位");
    const movedState = await page.evaluate(() => ({
      rows: [...document.querySelectorAll("[data-test='detail-bigfiles-tbody'] tr")].map((t) => t.textContent.trim()),
      range: document.querySelector("[data-test='tree-detail-range-stats']")?.textContent || "",
      trendCanvas: Boolean(document.querySelector("#detail-trend-chart canvas")),
    }));
    record("detail-moved-path-404-keeps-history",
      movedState.rows.join("\n").includes("无法定位") &&
        movedState.rows.join("\n").includes("历史快照读数不受影响") &&
        movedState.range.includes("58.6 MB") && movedState.range.includes("+1000.0 KB") &&
        movedState.trendCanvas,
      JSON.stringify(movedState).slice(0, 300));

    /* ---- E. 返回历史 a/b：展开/滚动/焦点/详情与大文件结果跨页保持 ---- */
    // moved 详情保持打开（restore 应重开详情并从共享查询域取回 404 态）。
    // 展开/滚动/焦点状态：media、archives 幂等展开，容器滚动并读回实际值，
    // 焦点落在 old 行（详情打开时焦点会被 close 按钮接管，restore 后再聚焦）。
    await expandIfNeeded(`${SR}/vault/media`, "E media");
    await waitUntil(async () => rowExists(`${SR}/vault/media/archives`), 5000, "E archives 行");
    await expandIfNeeded(`${SR}/vault/media/archives`, "E archives");
    await waitUntil(async () => rowExists(`${SR}/vault/media/archives/old`), 5000, "E old 行");
    await page.evaluate(() => { document.querySelector(".page-container").scrollTop = 120; });
    const scrollTarget = await page.evaluate(() => document.querySelector(".page-container").scrollTop);
    await page.focus(`#changes-body tr[data-path="${SR}/vault/media/archives/old"]`);
    resetApiCounts();
    // 用 hash 赋值导航（而非点击导航链接）：点击会把焦点交给链接，
    // leave() 读到的 activeElement 就不再是聚焦行——键盘焦点路径保持不变
    await page.evaluate(() => { location.hash = "#/bigfiles"; });
    await page.waitForTimeout(400);
    await page.evaluate(() => { location.hash = "#/changes"; });
    // 就绪信号 = restore 把焦点放回离页触发行（旧 DOM 假就绪不可作数，
    // restore 重画会替换行元素；焦点转移是恢复完成的最后一步）
    await waitUntil(async () => page.evaluate((p) => {
      const ae = document.activeElement;
      return Boolean(ae && ae.dataset && ae.dataset.path === p);
    }, `${SR}/vault/media/archives/old`), 15000, "E 返回完成（焦点信号）");
    await sleep(400);  // 详情重开与渲染余量
    const restored = await page.evaluate((want) => ({
      a: document.getElementById("sel-a").value,
      b: document.getElementById("sel-b").value,
      oldDepth: document.querySelector(`#changes-body tr[data-path="${want.old}"]`)?.dataset.depth,
      scroll: document.querySelector(".page-container").scrollTop,
      focused: document.activeElement && document.activeElement.dataset
        ? document.activeElement.dataset.path || "" : "",
      detailVisible: !document.getElementById("changes-detail").hidden,
      detailPath: document.querySelector("#changes-detail .detail-path")?.textContent || "",
      detailRows: [...document.querySelectorAll("[data-test='detail-bigfiles-tbody'] tr")]
        .map((t) => t.textContent.trim()).join(" ").slice(0, 120),
    }), { old: `${SR}/vault/media/archives/old` });
    record("restore-tree-expand-scroll-focus-detail",
      restored.a === "3" && restored.b === "4" && restored.oldDepth === "2" &&
        Math.abs(restored.scroll - scrollTarget) <= 3 &&
        restored.focused === `${SR}/vault/media/archives/old` &&
        restored.detailVisible && restored.detailPath === `${SR}/vault/moved`,
      JSON.stringify(restored));
    record("restore-detail-bigfiles-shared-state-no-request",
      apiCounts.query === 0 && restored.detailRows.includes("无法定位"),
      `query=${apiCounts.query} rows=${restored.detailRows}`);

    /* ---- F. 页面路径转义（中文+空格）+ 注入面 ---- */
    // hash 导航：不把焦点交给导航链接，leave() 才能快照到树行焦点
    await page.evaluate(() => { location.hash = "#/bigfiles"; });
    await page.waitForTimeout(300);
    await page.fill("#bf-path", `${SR}/vault/中文 目录`);
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("带 空格.bin"),
      20000, "中文目录查询完成");
    const weirdState = await page.evaluate(() => ({
      text: document.querySelector("#tbl-bigfiles tbody").textContent,
      imgs: document.querySelectorAll("#page-bigfiles img").length,
      svgOnload: document.querySelectorAll("#page-bigfiles svg[onload]").length,
    }));
    record("page-path-escape-chinese-space-literal",
      weirdState.text.includes("带 空格.bin") && weirdState.text.includes("中文 目录") &&
        weirdState.imgs === 0 && weirdState.svgOnload === 0,
      weirdState.text.slice(0, 120));
    await page.click('#tbl-bigfiles tbody [data-reveal]');
    await waitUntil(async () => revealBlocked.length === 1, 5000, "reveal 拦截");
    record("page-reveal-request-intercepted-with-path",
      revealBlocked[0] && revealBlocked[0].path === `${SR}/vault/中文 目录/带 空格.bin`,
      JSON.stringify(revealBlocked[0]));

    /* ---- G. 空目录 / 权限受限 / 截断（真实 find 状态） ---- */
    await page.fill("#bf-path", `${SR}/vault/empty`);
    await page.fill("#bf-mb", "1");
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("无匹配文件"),
      20000, "empty no_match");
    record("page-empty-no-match", true);

    await page.fill("#bf-path", `${SR}/vault/locked`);
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("权限受限"),
      20000, "locked permission_denied");
    const permText = await page.textContent("#tbl-bigfiles tbody");
    record("page-locked-permission-denied",
      permText.includes("权限受限") && permText.includes("查询失败") === false,
      permText.slice(0, 120));

    await page.fill("#bf-path", `${SR}/vault/many`);
    await page.fill("#bf-mb", "100");
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("结果被截断"),
      25000, "many 截断");
    const truncState = await page.evaluate(() => ({
      text: document.querySelector("#tbl-bigfiles tbody .hint-row")?.textContent || "",
      reveals: document.querySelectorAll("#tbl-bigfiles tbody [data-reveal]").length,
    }));
    record("page-many-truncated-top200",
      truncState.text.includes("结果被截断") && truncState.text.includes("已截断到 top 200") &&
        truncState.text.includes("210") && truncState.reveals === 200,
      JSON.stringify(truncState).slice(0, 200));

    /* ---- H. 越界路径：400 可辨（页面内） + reveal 边界（原始 HTTP） ---- */
    await page.fill("#bf-path", `${SR}-evil/child`);
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("查询范围无效"),
      15000, "越界 400");
    record("page-out-of-root-400-message", true);

    const revealCases = [
      ["reveal-missing-404", { path: `${SR}/vault/moved` }, 404],
      ["reveal-prefix-root-400", { path: `${SR}-evil/x` }, 400],
      ["reveal-dotdot-escape-400", { path: `${SR}/../outside` }, 400],
    ];
    for (const [name, payload, expect] of revealCases) {
      const r = await jpost("/api/reveal", payload);
      record(`boundary-${name}`, r.status === expect, `-> ${r.status}（期望 ${expect}）`);
    }

    /* ---- 慢目录公共前置：recent 键与句柄计算（J/I/K 三段共用） ---- */
    // 加肥后 slow 为 72 万目录项，种子自证遍历 ≥2s（断言把关），
    // 远长于取消链路（提交 → 按钮渲染 → click/POST ≈ 0.3-1.5s）。
    // 种子自证已预热目录项缓存，三段取消场景全部吃热缓存——
    // 「取消点击时 find 仍在飞」由规模差保证，不依赖冷/热时序运气
    // （旧 90k 夹具热遍历仅 ~0.6s，CI 上手动取消段被链路反超，实证）。
    const slowDir = `${SR}/vault/slow`;
    const slowTask = () => taskOf(slowDir, "recent", 7, 100, 200);
    await page.selectOption("#bf-mode", "recent");  // 与 slowTask 键一致

    /* ---- J. 离开查询面真实取消（服务端收敛；POST 可数） ---- */
    // 提交后立即离开（不等待任何状态）：取消走引擎的离开钩子，延迟最小化
    await page.fill("#bf-path", slowDir);
    await page.click("#btn-bigfiles");
    const cancelPostsBefore = apiCounts.cancel;
    await page.click('a[data-page="overview"]');
    /* ISS-176：范围身份贯通后该查询真实执行并可能先于轮询完成清理
     * （404「未知或已过期」）——离开收敛的目标是「不再有在途查询」，
     * 终态与已清理同等满足；取消 POST 仍由离开钩子真实发出另行断言。 */
    const leaveCancelled = await pollStatus(slowTask(), (s) => s.terminal,
      15000, "离开取消收敛（终态或已清理）");
    record("leave-page-cancels-task-via-handle",
      leaveCancelled.state === "cancelled" && apiCounts.cancel > cancelPostsBefore,
      `state=${leaveCancelled.state} cancelPosts=${apiCounts.cancel}`);

    /* ---- I. 双目录并发取消不误伤（慢目录在途，句柄级取消） ---- */
    await page.click('a[data-page="bigfiles"]');  // J 已离开，回到大文件页
    // A 提交后立即改查 many（不等待中间状态）：改范围按 A 句柄静默取消，
    // B 不受影响——句柄级取消的「不误伤」由此断言（A 的提交→改范围窗口
    // 仅 ~0.3s，加肥后 A 遍历 ≥2s（断言把关），取消必然落在在飞窗口）
    await page.fill("#bf-path", slowDir);
    await page.click("#btn-bigfiles");
    await page.fill("#bf-path", `${SR}/vault/many`);
    await page.click("#btn-bigfiles");
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("结果被截断"),
      25000, "B many 完成");
    const aCancelled = await pollStatus(slowTask(), (s) => s.terminal, 15000, "A 收敛");
    record("dual-dir-cancel-scope-change-a-cancelled-b-ok",
      aCancelled.state === "cancelled" && apiCounts.cancel >= 1,
      `A=${aCancelled.state} cancelPosts=${apiCounts.cancel}`);

    // 手动取消按钮：慢目录再次查询 → 点取消 → UI「已取消」+ 服务端收敛
    // （用选择器点击而非句柄：运行态每次 emit 会重画 tbody，句柄会失连；
    // 本段是 CI 上实证被 90k 夹具热遍历反超的场景，加肥后遍历 ≥2s（断言把关），
    // waitForSelector+click 链路 ~1s 内必然落在在飞窗口）
    await page.fill("#bf-path", slowDir);
    await page.click("#btn-bigfiles");
    await page.waitForSelector("[data-test='bigfiles-cancel']", { timeout: 8000 });
    await page.click("[data-test='bigfiles-cancel']", { timeout: 5000 });
    await waitUntil(async () => (await page.textContent("#tbl-bigfiles tbody")).includes("已取消"),
      15000, "UI 已取消");
    const manualCancelled = await pollStatus(slowTask(), (s) => s.terminal, 15000, "手动取消收敛");
    record("manual-cancel-button-converges",
      manualCancelled.state === "cancelled" &&
        (await page.textContent("#tbl-bigfiles tbody")).includes("已取消"),
      `state=${manualCancelled.state}`);

    /* ---- K. 边界：浏览器中止不冒充服务端取消（node 层，页面外） ---- */
    {
      const q = (wait) =>
        `/api/bigfiles?days=7&min_mb=100&topn=200&mode=recent&wait=${wait}` +
        `&path=${encodeURIComponent(slowDir)}`;
      const submit = await httpJson("GET", q("false"), port);
      record("boundary-submit-202-with-handle",
        submit.status === 202 && typeof submit.json.task_id === "string",
        `status=${submit.status} state=${submit.json.state}`);
      const taskId = submit.json.task_id;
      await pollStatus(taskId, (s) => s.state === "running" || s.terminal, 8000, "K 在途确认");
      const ac = new AbortController();
      const aborted = fetch(`${base}${q("true")}`, { signal: ac.signal })
        .then(() => "completed").catch(() => "aborted");
      await sleep(120);
      ac.abort();
      await aborted;
      // K 是套件内唯一等待 find 自然完成（不取消）的场景：加肥后 72 万项
      // 遍历显著变长（CI 更慢、忙时更长），预算从 25s 放宽到 90s；断言不变
      // （中止不得产生 cancelled 终态）。
      const fin = await pollStatus(taskId, (s) => s.terminal, 90000, "K 终态");
      record("boundary-browser-abort-does-not-cancel-server-find",
        fin.state !== "cancelled", `终态=${fin.state}（取消不应由中止触发）`);
    }
    /* ---- L. 键盘动作 + 三视口（980/1220/1440） ---- */
    await page.click('a[data-page="changes"]');
    // 就绪信号 = restore 重开离页时的详情（leave 已将 aside 关闭，
    // hidden→visible 且 detail-path 回到 moved 才是恢复完成的信号；
    // tbody 旧 DOM 假就绪不可作数）。焦点是否回到树行取决于离页时
    // activeElement 所在，不作为就绪判据。
    await waitUntil(async () => page.evaluate((p) =>
      !document.getElementById("changes-detail").hidden &&
      (document.querySelector("#changes-detail .detail-path")?.textContent || "") === p,
    `${SR}/vault/moved`), 15000, "L restore 就绪");
    await sleep(400);  // 恢复尾步（焦点/滚动落定）余量
    // 键盘：行 Enter 打开详情；Esc 关闭后焦点返回
    await page.focus(`#changes-body tr[data-path="${SR}/vault/media"]`);
    await page.keyboard.press("Enter");
    await waitUntil(async () => page.evaluate((p) =>
      (document.querySelector("#changes-detail .detail-path")?.textContent || "") === p,
    `${SR}/vault/media`), 8000, "L Enter 详情");
    await page.keyboard.press("Escape");
    await waitUntil(async () => page.evaluate(() => document.getElementById("changes-detail").hidden),
      5000, "L Esc 关闭");
    const focusBack = await page.evaluate((p) =>
      document.activeElement && document.activeElement.dataset
        ? document.activeElement.dataset.path || "" : "", `${SR}/vault/media`);
    record("keyboard-row-enter-detail-focus-return",
      focusBack === `${SR}/vault/media`, `active=${focusBack}`);

    // 三视口：详情打开 + 大文件区渲染态（restore 恢复的展开态直接复用）
    await expandIfNeeded(`${SR}/vault/media`, "L2 media");
    await waitUntil(async () => rowExists(`${SR}/vault/media/archives`), 5000, "L2 archives");
    await expandIfNeeded(`${SR}/vault/media/archives`, "L2 archives");
    await waitUntil(async () => rowExists(`${SR}/vault/media/archives/old`), 5000, "L2 old");
    await page.click(`#changes-body tr[data-path="${SR}/vault/media/archives/old"] .tree-name`);
    await waitUntil(async () => page.evaluate(() =>
      !document.getElementById("changes-detail").hidden &&
      Boolean(document.querySelector("[data-test='detail-bigfiles-section']"))), 8000, "L2 详情");
    await page.click("[data-test='detail-bf-query']");
    await waitUntil(async () => (await detailRows()).join("\n").includes("300.0 MB"),
      20000, "L2 largest 就绪");
    for (const [w, h] of [[980, 640], [1220, 820], [1440, 900]]) {
      await page.setViewportSize({ width: w, height: h });
      await sleep(350);
      const geo = await page.evaluate(() => ({
        docOverflow: document.scrollingElement.scrollWidth > window.innerWidth,
        sectionVisible: Boolean(document.querySelector("[data-test='detail-bigfiles-section']")?.getClientRects().length),
        rows: document.querySelectorAll("[data-test='detail-bigfiles-tbody'] tr").length,
      }));
      record(`viewport-${w}x${h}-detail-bigfiles-no-overflow`,
        geo.docOverflow === false && geo.sectionVisible === true && geo.rows >= 4,
        JSON.stringify(geo));
      await page.screenshot({ path: path.join(SHOTS, `viewport-${w}x${h}.png`) });
    }

    record("no-console-errors", consoleErrors.length === 0,
      `${consoleErrors.slice(0, 3).join("; ")}${resourceErrors.length ? `（另有 ${resourceErrors.length} 条预期 4xx 网络日志，不计入）` : ""}`);
    record("no-dialogs", dialogs.length === 0, dialogs.join("; "));
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
      record("chromium-closed", true);
    }
    // 恢复权限位（tmp 保留供复查；避免 000 目录挡住后续清理）
    try { fs.chmodSync(path.join(SR, "vault", "locked", "inner"), 0o755); } catch (_) { /* 尽力恢复 */ }
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
      await httpJson("GET", "/health", port);
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
  process.stderr.write("整体超时（600s），强制退出\n");
  process.exit(1);
}, 600000);

main().catch((e) => {
  process.stderr.write(`FATAL: ${e.message}\n${e.stack || ""}\n`);
  process.exitCode = 1;
}).finally(() => clearTimeout(watchdog));
