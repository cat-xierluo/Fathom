#!/usr/bin/env node
/* ISS-026 · UX 原型自动验证（Playwright 驱动，无外部依赖安装）
 *
 * 合同映射：
 * - docs/DESIGN.md「原型验收」与「原型第二轮：原生 Mac 视觉方向」：五页导航、
 *   总览→变化→目录详情（≤3 步定位证据）、首次启动向导、失败恢复（保留旧数据 + 重试）、
 *   详情关键列（现在/变化/状态）在打开详情时无需容器内横向滚动即可见。
 * - docs/TESTING.md UX 回归行：980×640 / 1220×820（另加 1440×900）无横向溢出；
 *   长路径不撑破布局；键盘导航 + Esc 焦点返回；图表非零尺寸且缺失日不补零、
 *   末位 x 轴标签不越出 viewBox。
 * - 状态合同：正常 / 单快照 / 首次启动（结论区不得残留其他场景内容）/ 部分权限 /
 *   失败保留旧数据 / 服务断开 / 未记录 ≠ 新增 / Agent 未启用不得假装已识别。
 *
 * 运行：node scripts/verify_ux_prototype.cjs [--evidence-dir <dir>]
 *   --evidence-dir  截图/日志/结果 JSON 的输出目录（默认为系统临时目录下按 uid
 *                   隔开的通用任务私有路径，不绑定任何 worker session）。
 *   每次执行写入 <evidence-dir>/runs/run-<N>/（N 递增，不覆盖历史运行），
 *   便于同场景 before/after 对照与失败迭代留痕。
 * 退出码：0 全部通过；1 存在失败；2 环境不可用（无浏览器等）。
 */
"use strict";

const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execSync } = require("child_process");

/* playwright 解析：先常规 require，失败则回退全局安装位置（不安装任何东西） */
let chromium = null;
const PW_CANDIDATES = ["playwright", "/opt/homebrew/lib/node_modules/playwright"];
for (const cand of PW_CANDIDATES) {
  try { chromium = require(cand).chromium; break; } catch (e) { /* 下一个 */ }
}

const PROTO_DIR = path.resolve(__dirname, "..", "prototypes", "ux");

/* --evidence-dir 参数：默认通用任务私有临时路径（uid 隔离，不绑定 worker session） */
function parseEvidenceDir(argv) {
  const idx = argv.indexOf("--evidence-dir");
  if (idx >= 0 && argv[idx + 1]) return path.resolve(argv[idx + 1]);
  const eq = argv.find((a) => a.indexOf("--evidence-dir=") === 0);
  if (eq) return path.resolve(eq.slice("--evidence-dir=".length));
  return path.join(os.tmpdir(), "fathom-prototype-evidence-" + process.getuid());
}
const EVIDENCE_DIR = parseEvidenceDir(process.argv.slice(2));

/* 每次运行独立子目录（run-1、run-2…），历史运行不被覆盖 */
function nextRunDir(root) {
  const runsRoot = path.join(root, "runs");
  fs.mkdirSync(runsRoot, { recursive: true });
  let max = 0;
  for (const name of fs.readdirSync(runsRoot)) {
    const m = /^run-(\d+)$/.exec(name);
    if (m) max = Math.max(max, parseInt(m[1], 10));
  }
  return path.join(runsRoot, "run-" + (max + 1));
}
const RUN_DIR = nextRunDir(EVIDENCE_DIR);
const RESULTS_JSON = path.join(RUN_DIR, "verify-results.json");
const LOG_FILE = path.join(RUN_DIR, "verify-log.txt");
const MEASURE_JSON = path.join(RUN_DIR, "layout-measurements.json");

function gitHead() {
  try { return execSync("git rev-parse HEAD", { cwd: path.resolve(__dirname, "..") }).toString().trim(); }
  catch (e) { return null; }
}

/* 与 prototypes/ux/app.js 一致的合成数据路径（验证脚本自带副本，避免跨文件依赖） */
const ROOT = "/Users/演示用户";
const BUILD_PATH = ROOT + "/开发/项目A/build";
const LONG_DIR =
  ROOT + "/媒体库/视频项目/2026年夏季产品发布会素材（多机位拍摄）/A机位未剪辑片段/20260712_上午场第二段/机位A_原始素材_高码率";
const LIB_RESTRICTED = ROOT + "/Library/受限目录";
const MINUS = "−"; /* − U+2212 */

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

const logLines = [];
let passCount = 0;
let failCount = 0;
const results = [];
const layoutMeasurements = {};

function line(msg) {
  logLines.push(msg);
  console.log(msg);
}
function check(name, cond, detail) {
  const ok = !!cond;
  results.push({ name, pass: ok, detail: detail == null ? "" : String(detail) });
  if (ok) { passCount++; line("PASS  " + name + (detail ? "  — " + detail : "")); }
  else { failCount++; line("FAIL  " + name + (detail ? "  — " + detail : "")); }
}

function startServer() {
  const server = http.createServer((req, res) => {
    const raw = decodeURIComponent((req.url || "/").split("?")[0]);
    if (raw === "/favicon.ico") { res.writeHead(204); res.end(); return; }
    const rel = raw === "/" ? "/index.html" : raw;
    const file = path.join(PROTO_DIR, path.normalize(rel));
    if (!file.startsWith(PROTO_DIR + path.sep) && file !== PROTO_DIR) {
      res.writeHead(403); res.end("forbidden"); return;
    }
    fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404); res.end("not found"); return; }
      res.writeHead(200, { "Content-Type": MIME[path.extname(file).toLowerCase()] || "application/octet-stream" });
      res.end(data);
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

async function main() {
  fs.mkdirSync(RUN_DIR, { recursive: true });
  const head = gitHead();
  line("== ISS-026 UX 原型验证 ==");
  line("输出目录: " + RUN_DIR + (head ? "（head " + head.slice(0, 10) + "）" : ""));

  let playwrightErr = null;
  let browser = null;
  const { server, port } = await startServer();
  const BASE = "http://127.0.0.1:" + port + "/";
  line("静态服务已启动: " + BASE + "（随机端口，仅本机回环）");

  const pageErrors = [];
  const consoleErrors = [];
  const shots = [];

  try {
    if (!chromium) throw new Error("无法解析 playwright 模块（常规 require 与全局安装位置均失败）");
    try {
      browser = await chromium.launch({ headless: true });
    } catch (e) {
      playwrightErr = e;
      line("环境不可用：无法启动 chromium —— " + e.message.split("\n")[0]);
      try { line("chromium.executablePath() = " + chromium.executablePath()); } catch (_) {}
      line("按任务约束不安装/下载浏览器；请提供可用 chromium 后重跑。");
      process.exitCode = 2;
      return;
    }

    const context = await browser.newContext({ viewport: { width: 1220, height: 820 } });
    const page = await context.newPage();
    page.on("pageerror", (e) => pageErrors.push(String(e && e.message ? e.message : e)));
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

    async function shot(name) {
      const p = path.join(RUN_DIR, name + ".png");
      await page.screenshot({ path: p });
      shots.push(path.basename(p));
    }
    async function gotoHash(pg) {
      await page.evaluate((h) => { location.hash = h; }, "#/" + pg);
      await page.waitForFunction(
        (id) => { const el = document.querySelector(".page:not([hidden])"); return el && el.id === id; },
        "page-" + pg, { timeout: 5000 }
      );
    }
    async function setScenario(name) {
      await page.selectOption("#scenario-select", name);
      await page.waitForFunction((s) => document.body.dataset.scenario === s, name, { timeout: 5000 });
    }
    async function noOverflow(label) {
      const m = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        vw: window.innerWidth,
      }));
      check("无横向溢出 " + label, m.doc <= m.vw + 1 && m.body <= m.vw + 1, "doc=" + m.doc + " body=" + m.body + " vw=" + m.vw);
    }
    /* 关键列布局断言：打开详情后，变化表「现在/变化/状态」无需容器内横向滚动即可见。
     * 页面整体不溢出不足以证明这一点（r1 审查实测 1220px 下表格视窗 506px、内容 707px）。 */
    async function keyColumnsVisible(label) {
      const m = await page.evaluate((bp) => {
        const wrap = document.querySelector("#page-changes .tbl-wrap");
        const row = document.querySelector('#page-changes tr[data-path="' + bp + '"]');
        if (!wrap || !row) return null;
        const wrapRect = wrap.getBoundingClientRect();
        const cells = Array.from(row.cells).map((c) => ({
          head: c.textContent.trim().slice(0, 12),
          right: Math.round(c.getBoundingClientRect().right),
          visible: c.getBoundingClientRect().right <= wrapRect.right + 0.5,
        }));
        return {
          wrapClient: wrap.clientWidth,
          wrapScroll: wrap.scrollWidth,
          innerOverflow: wrap.scrollWidth - wrap.clientWidth,
          cells,
        };
      }, BUILD_PATH);
      layoutMeasurements[label] = m;
      const keyOk = !!m && m.innerOverflow <= 1 && m.cells.slice(1).every((c) => c.visible);
      check("布局·" + label + " 关键列（之前/现在/变化/状态）无需滚动即可见", keyOk,
        m ? "tbl-wrap " + m.wrapClient + "/" + m.wrapScroll + "px，最右列右缘 " + (m.cells[m.cells.length - 1] || {}).right : "无表");
      return m;
    }
    const bodyText = () => page.evaluate(() => document.body.innerText || "");

    /* ===== C1 加载与外壳 ===== */
    await page.goto(BASE, { waitUntil: "load" });
    await page.waitForSelector(".nav-item", { timeout: 5000 });
    check("页面加载且无 JS 报错", pageErrors.length === 0, pageErrors.join("; "));
    check("原型横幅存在（合成数据声明）", await page.isVisible(".proto-banner"));
    check("五页导航齐全", (await page.$$eval(".nav-item", (els) => els.map((e) => e.dataset.page).join(","))) === "overview,changes,browse,bigfiles,settings");
    check("侧边导航含 SVG 图标（线条图标，无 emoji）", (await page.$$eval(".sidenav svg", (els) => els.length)) >= 5);

    /* ===== C2 总览（正常，1220×820） ===== */
    check("总览·质量行显示覆盖完整", (await page.textContent('[data-region="quality"]')).includes("覆盖完整"));
    const conc = await page.textContent('[data-region="conclusion"]');
    check("总览·结论先回答增长 +2.3 GiB", conc.includes("+2.3 GiB"), "");
    check("总览·结论说明口径与来源未识别", conc.includes("未识别") || conc.includes("只描述路径"));
    const sparkBox = await page.$('[data-region="volume"] svg.spark');
    check("总览·走势图非零尺寸", sparkBox ? (await sparkBox.boundingBox()) !== null : false);
    const sparkAria = await page.$eval('[data-region="volume"] svg.spark', (el) => el.getAttribute("aria-label") || "");
    check("总览·走势图声明缺失日留空（不补零）与等价表格", sparkAria.includes("留空") && sparkAria.includes("表格"));
    const volMeta = await page.textContent("#vol-meta");
    check("总览·卷容量缺口标注 09-09", volMeta.includes("09-09") && volMeta.includes("不补零"));
    /* 图表精修约束：末位 x 轴标签 bbox 不得越出 viewBox（r1 审查发现的裁切） */
    const sparkClip = await page.$eval('[data-region="volume"] svg.spark', (svg) => {
      const vb = svg.viewBox.baseVal;
      const texts = Array.from(svg.querySelectorAll("text"));
      const last = texts[texts.length - 1];
      if (!last) return { ok: false, vbW: vb.width, right: -1 };
      const b = last.getBBox();
      return { ok: b.x + b.width <= vb.width + 0.5, vbW: vb.width, right: b.x + b.width };
    });
    check("图表·末位 x 轴标签不被裁切（bbox 不越出 viewBox）", sparkClip.ok,
      "right=" + sparkClip.right.toFixed(1) + " / viewBox=" + sparkClip.vbW);
    await shot("00-overview-1220");

    /* ===== C2.5 场景切换：首启不得残留上一场景结论（r1 审查 B1 回归） ===== */
    await setScenario("first-launch");
    await gotoHash("overview");
    await page.waitForSelector("#onboard:not([hidden])", { timeout: 5000 });
    const b1 = await page.evaluate(() => {
      const el = document.querySelector('[data-region="conclusion"]');
      return { hidden: !el || el.hidden, text: (el && el.innerText ? el.innerText : "").trim() };
    });
    check("首启·切换场景后结论区为空/隐藏（不残留旧场景结论）", b1.hidden || b1.text === "", b1.hidden ? "hidden" : JSON.stringify(b1.text.slice(0, 60)));
    await setScenario("normal");
    await gotoHash("overview");
    await page.waitForFunction(() => (document.querySelector('[data-region="conclusion"]') || {}).innerText.indexOf("+2.3 GiB") >= 0, null, { timeout: 5000 });
    check("首启·切回正常场景结论恢复", true);

    /* ===== C3 日常定位旅程（总览 → 变化 → 目录详情，≤3 步） ===== */
    await page.click('[data-journey="locate"]');
    await page.waitForFunction(() => location.hash === "#/changes", null, { timeout: 5000 });
    check("旅程·步骤1 查看变化跳转且导航高亮", (await page.getAttribute('a[data-page="changes"]', "aria-current")) === "page");
    const net = await page.textContent('[data-region="net-line"]');
    check("变化·净变化口径行 +2.3 GiB 与根目录数值", net.includes("+2.3 GiB") && net.includes("32.0 GiB") && net.includes("34.3 GiB"));
    const diffMeta = await page.textContent('[data-region="diff-meta"]');
    check("变化·区间元数据（同根/同口径/覆盖）", diffMeta.includes("同根") && diffMeta.includes("同口径") && diffMeta.includes("覆盖"));
    await page.click('#page-changes tr[data-path="' + BUILD_PATH + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    const dStats = await page.textContent('[data-region="detail-stats"]');
    check("旅程·步骤2 目录详情显示 +2.1 GiB 证据", dStats.includes("+2.1 GiB"), dStats.trim());
    await page.click("#detail details.tbl-toggle summary");
    const dTrend = await page.textContent('[data-region="detail-trend"]');
    check("旅程·步骤3 趋势等价表格含未扫描缺口", dTrend.includes("未扫描") && dTrend.includes("09-09"));
    const dAgent = await page.textContent('[data-region="detail-agent"]');
    check("详情·Agent 未启用如实展示", dAgent.includes("未启用"));
    check("全局·不出现“已识别”类虚构结论", !((await bodyText()).includes("已识别")));

    /* 详情几何（1220×820）：内容可用宽度不足时必须改为覆盖层，关键列可见 */
    const dGeom1220 = await page.evaluate(() => {
      const el = document.querySelector("#detail");
      const r = el.getBoundingClientRect();
      return { x: r.x, w: r.width, vw: window.innerWidth, pos: getComputedStyle(el).position };
    });
    layoutMeasurements["detail-1220"] = dGeom1220;
    check("详情·1220px 为全宽覆盖层（左侧贴合侧栏、右侧贴合视口）",
      dGeom1220.pos === "fixed" && Math.abs(dGeom1220.x + dGeom1220.w - dGeom1220.vw) <= 4,
      "pos=" + dGeom1220.pos + " x=" + Math.round(dGeom1220.x) + " w=" + Math.round(dGeom1220.w));
    await keyColumnsVisible("1220 变化页+详情打开");
    await noOverflow("1220×820 变化页+详情打开");
    await shot("02-changes-detail-1220");

    /* ===== C4 键盘导航与焦点返回 ===== */
    await page.evaluate((p) => { document.querySelector('#page-changes tr[data-path="' + p + '"]').focus(); }, BUILD_PATH);
    await page.keyboard.press("ArrowDown");
    const nextPath = await page.evaluate(() => document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.path : "");
    check("键盘·ArrowDown 移动到下一行", !!nextPath && nextPath !== BUILD_PATH, nextPath);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    check("键盘·Enter 打开目录详情", (await page.getAttribute("#detail", "data-path")) === nextPath);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.body.dataset.detail === "closed", null, { timeout: 5000 });
    const escFocus = await page.evaluate(() => (document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.path : ""));
    check("键盘·Esc 关闭详情且焦点返回原行", escFocus === nextPath, escFocus);

    /* ===== C5 数字语义与长路径（变化表） ===== */
    await page.click('#page-changes tr[data-path="' + LONG_DIR + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    const chgText = await page.textContent("#changes-table");
    check("数字·正数 +2.1 GiB / 负数 −(U+2212)1.0 GiB / 缺失 —", chgText.includes("+2.1 GiB") && chgText.includes(MINUS + "1.0 GiB") && chgText.includes("—"));
    check("数字·未记录行（小缓存 11 MiB，不判为新增）", chgText.includes("11 MiB") && chgText.includes("未记录"));
    const longCell = await page.$('#page-changes tr[data-path="' + LONG_DIR + '"] .cell-path');
    /* 省略号截断本身会让 scrollWidth > clientWidth（设计使然）；断言布局宽度受控即可 */
    const longInfo = await page.evaluate((el) => ({ t: el.textContent.trim(), title: el.getAttribute("title"), w: el.getBoundingClientRect().width }), longCell);
    check("长路径·中间截断且 title 保留完整路径", longInfo.t.includes("…") && longInfo.title === LONG_DIR, longInfo.t);
    check("长路径·单元格宽度受控（不撑破布局）", longInfo.w > 0 && longInfo.w <= 421, "w=" + longInfo.w.toFixed(1) + " / max 420");
    const dPathCode = await page.$eval("#detail .detail-path code", (el) => ({ t: el.textContent.trim(), title: el.getAttribute("title") }));
    check("长路径·详情路径截断 + title 完整", dPathCode.t.includes("…") && dPathCode.title === LONG_DIR, dPathCode.t);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.body.dataset.detail === "closed", null, { timeout: 5000 });

    /* ===== C6 980×640：详情为全宽层，无横向溢出 ===== */
    await page.setViewportSize({ width: 980, height: 640 });
    await page.click('#page-changes tr[data-path="' + BUILD_PATH + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    /* 打开详情会对覆盖层应用入场动画；等动画结束再量几何 */
    await page.waitForFunction(() => {
      const el = document.querySelector("#detail");
      return !el || el.getAnimations().length === 0;
    }, { timeout: 5000 });
    const oBox = await (await page.$("#detail")).boundingBox();
    check("详情·980px 为固定全宽层（右侧贴合视口）", oBox && oBox.x >= 74 && oBox.x <= 78 && Math.abs(oBox.x + oBox.width - 980) <= 4,
      oBox ? "x=" + oBox.x + " w=" + oBox.width : "无");
    await noOverflow("980×640 变化页+详情打开");
    await shot("10-980-detail-overlay");
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 1220, height: 820 });

    /* ===== C7 部分权限（读取受限） ===== */
    await setScenario("partial");
    await gotoHash("overview"); /* 质量行/结论区在总览页：先回到总览，避免读到其他场景的隐藏残留 */
    check("部分权限·质量行警告", (await page.textContent('[data-region="quality"]')).includes("部分目录未读取"));
    check("部分权限·结论限定已测量范围", (await page.textContent('[data-region="conclusion"]')).includes("仅覆盖已测量范围"));
    await gotoHash("changes");
    const restRow = await page.textContent('#page-changes tr[data-path="' + LIB_RESTRICTED + '"]');
    /* prev = G(0.6) = 629146 KiB，fmtKib 按 MiB 档输出 "614 MiB"（不足 1 GiB 不显示 GiB） */
    check("部分权限·受限行显示 读取受限/之前 614 MiB/现在 —", restRow.includes("读取受限") && restRow.includes("614 MiB") && restRow.includes("—"), restRow.trim().slice(0, 70));
    check("部分权限·缺失不判为删除", !((await bodyText()).includes("已删除")));
    await page.click('#page-changes tr[data-path="' + LIB_RESTRICTED + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    const restDetail = await page.textContent("#detail");
    check("部分权限·详情解释读取受限并给授权入口", restDetail.includes("读取受限") && restDetail.includes("授权后重扫"), "");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.body.dataset.detail === "closed", null, { timeout: 5000 });
    await gotoHash("overview"); /* goto-perm 按钮在总览质量行 */
    await page.click('[data-action="goto-perm"]');
    await page.waitForFunction(() => location.hash === "#/settings", null, { timeout: 5000 });
    check("部分权限·设置页权限区说明部分受限", (await page.textContent('[data-region="perm"]')).includes("部分受限"));
    await shot("05-partial-permissions");

    /* ===== C8 失败保留旧数据 + 重试恢复 ===== */
    await setScenario("failed");
    await gotoHash("overview");
    const failedConc = await page.textContent('[data-region="conclusion"]');
    check("失败·结论声明保留上次有效数据", failedConc.includes("保留上次有效数据") || failedConc.includes("保留有效"));
    check("失败·顶栏状态 failed", (await page.getAttribute("#status-chip", "data-scan-state")) === "failed");
    await gotoHash("changes");
    check("失败·变化页仍展示保留快照数据", (await page.$$eval("#changes-table tbody tr", (r) => r.length)) >= 5);
    await gotoHash("overview");
    await shot("06-failed-overview");
    await page.click('[data-action="retry-scan"]');
    await page.waitForFunction(() => document.body.dataset.scenario === "normal", null, { timeout: 12000 });
    check("失败·重试扫描后恢复正常状态", (await page.getAttribute("#status-chip", "data-scan-state")) === "idle" && (await page.textContent("#status-chip")).includes("成功"));

    /* ===== C9 服务断开 ===== */
    await setScenario("offline");
    await gotoHash("overview");
    check("断开·结论为无法连接本地服务", (await page.textContent('[data-region="conclusion"]')).includes("无法连接本地服务"));
    const errCount = await page.$$eval("[data-region-error]", (els) => els.length);
    check("断开·各区域单独错误态（保留高度）", errCount >= 2, "region-error x" + errCount);
    check("断开·缓存数据时间如实标注", ((await bodyText())).includes("上次成功获取数据") || ((await bodyText())).includes("保留的缓存"));
    check("断开·不出现 main.py 之类误导恢复项", !((await bodyText()).includes("main.py")));
    await page.click("#btn-scan");
    const offToast = await page.textContent("#toast-region");
    check("断开·扫描请求被拒且如实提示", offToast.includes("未发送"), offToast.trim().slice(0, 40));
    await gotoHash("settings");
    const svc = await page.textContent('[data-region="service"]');
    check("断开·设置页恢复说明（重启应用，无 main.py）", svc.includes("未连接") && svc.includes("恢复说明") && !svc.includes("main.py"));
    await shot("07-offline-settings");

    /* ===== C10 单快照 ===== */
    await setScenario("single");
    await gotoHash("overview");
    check("单快照·结论为基线已建立", (await page.textContent('[data-region="conclusion"]')).includes("基线已建立"));
    check("单快照·不绘制假趋势", (await page.$$('[data-region="volume"] svg.spark')).length === 0);
    await gotoHash("changes");
    check("单快照·变化页等待下一快照（不显示假 0）", ((await page.textContent("#page-changes"))).includes("等待下一个日期的快照"));
    await gotoHash("browse");
    const singleRow = await page.textContent('#page-browse tbody tr[data-path="' + ROOT + '/媒体库"]');
    check("单快照·分布较上快照列为 —", singleRow.includes("—"));
    await shot("03-single-browse");

    /* ===== C11 首次启动向导（三步 + 首扫） ===== */
    await setScenario("first-launch");
    await gotoHash("overview");
    await page.waitForSelector("#onboard:not([hidden])", { timeout: 5000 });
    check("首启·向导步骤 0 出现", (await page.getAttribute("#onboard", "data-step")) === "0");
    await page.click('[data-action="wizard-next"]');            /* 步骤 1：范围 */
    await page.waitForFunction(() => document.querySelector("#onboard").dataset.step === "1", null, { timeout: 3000 });
    check("首启·步骤 1 监控范围两个选项", (await page.$$(".choice")).length === 2);
    await page.click('.choice[data-choice-root="/Volumes/演示数据盘"]');
    check("首启·选择未覆盖根时给出诚实提示", (await page.textContent("#toast-region")).includes("演示提示"));
    await page.click('.choice[data-choice-root="' + ROOT + '"]');
    await page.click('[data-action="wizard-next"]');            /* 步骤 2：权限 */
    await page.waitForFunction(() => document.querySelector("#onboard").dataset.step === "2", null, { timeout: 3000 });
    check("首启·步骤 2 可暂不授权", ((await page.textContent('[data-region="onboard-body"]'))).includes("暂不授权"));
    await shot("01-first-launch-permission");
    await page.click('[data-action="wizard-scan"]');            /* 步骤 3：首扫 */
    await page.waitForFunction(() => document.querySelector("#onboard").dataset.step === "3", null, { timeout: 3000 });
    check("首启·首扫阶段可见且不估算百分比", ((await page.textContent('[data-region="wizard-elapsed"]'))).includes("不估算百分比"));
    await page.waitForFunction(() => !!document.querySelector('#onboard .scan-phase-list li.done'), null, { timeout: 5000 });
    check("首启·阶段推进（读取目录结构完成）", true);
    await page.waitForFunction(() => document.body.dataset.scenario === "single" && location.hash === "#/browse", null, { timeout: 12000 });
    check("首启·首扫完成建立基线并落到分布页", (await page.$$eval("#page-browse tbody tr", (r) => r.length)) >= 5);
    await shot("02-after-first-scan");
    /* 取消路径：不留数据 */
    await setScenario("first-launch");
    await gotoHash("overview");
    await page.click('[data-action="wizard-next"]');
    await page.click('[data-action="wizard-next"]');
    await page.click('[data-action="wizard-scan"]');
    await page.waitForFunction(() => document.querySelector("#onboard").dataset.step === "3", null, { timeout: 3000 });
    await page.click('[data-action="wizard-cancel"]');
    await page.waitForFunction(() => document.body.dataset.scenario === "first-launch" && document.querySelector("#onboard").dataset.step === "0", null, { timeout: 5000 });
    check("首启·取消首扫不写入数据（仍为空库）", true);

    /* ===== C12 大文件（显式查询） ===== */
    await setScenario("normal");
    await gotoHash("bigfiles");
    check("大文件·初始为尚未查询（不自动遍历）", ((await page.textContent('[data-region="bf-status"]'))).includes("尚未查询"));
    check("大文件·未查询时无结果表", (await page.$$("#page-bigfiles [data-region='bf-table'] table")).length === 0);
    await page.click("#btn-bf");
    await page.waitForFunction(() => (document.querySelector('[data-region="bf-status"]') || {}).innerText.indexOf("已查询") >= 0, null, { timeout: 6000 });
    check("大文件·查询后显示 4 项与更新时间", (await page.$$eval("#page-bigfiles [data-region='bf-table'] tbody tr", (r) => r.length)) === 4);
    const bfText = await page.textContent("#page-bigfiles");
    check("大文件·命中含 3.2 GiB 且长文件名截断", bfText.includes("3.2 GiB") && bfText.includes("…"));
    await page.fill("#bf-mb", "5000");
    await page.click("#btn-bf");
    await page.waitForFunction(() => (document.querySelector('[data-region="bf-status"]') || {}).innerText.indexOf("无匹配结果") >= 0, null, { timeout: 6000 });
    check("大文件·无匹配与失败是不同状态（无错误框）", (await page.$$("#page-bigfiles [data-region-error]")).length === 0);
    await shot("04-bigfiles");

    /* ===== C13 分布：占比/图例/下钻/返回 ===== */
    await gotoHash("browse");
    const segCount = await page.$$eval(".share-seg", (els) => els.length);
    check("分布·占比条分段 ≥6（含其他）", segCount >= 6, "x" + segCount);
    const legend = await page.textContent(".share-legend");
    check("分布·图例文本干净（无拼接残渣）", legend.includes("其他") && !legend.includes('"') && !legend.includes("+ \""));
    check("分布·脚注声明父子不可相加", ((await page.textContent('[data-region="browse-note"]'))).includes("不可相加"));
    await page.click('#page-browse tr[data-path="' + ROOT + '/媒体库"]');
    await page.waitForFunction(() => document.querySelectorAll("#crumbs .crumb").length === 2, null, { timeout: 5000 });
    check("分布·行点击下钻且面包屑更新", ((await page.textContent("#crumbs"))).includes("媒体库"));
    check("分布·返回上层行存在", ((await page.textContent("#page-browse"))).includes("../ 返回上层"));
    await page.click("#crumbs .crumb");                          /* 回主目录 */
    await page.waitForFunction(() => document.querySelectorAll("#crumbs .crumb").length === 1, null, { timeout: 5000 });
    /* 长路径钻到叶子并打开详情 */
    await page.click('#page-browse tr[data-path="' + ROOT + '/媒体库"]');
    await page.click('#page-browse tr[data-path="' + ROOT + '/媒体库/视频项目"]');
    await page.click('#page-browse tr[data-path="' + ROOT + '/媒体库/视频项目/' + '2026年夏季产品发布会素材（多机位拍摄）"]');
    await page.click('#page-browse tr[data-path="' + ROOT + '/媒体库/视频项目/' + '2026年夏季产品发布会素材（多机位拍摄）/A机位未剪辑片段"]');
    await page.click('#page-browse tr[data-path="' + ROOT + '/媒体库/视频项目/' + '2026年夏季产品发布会素材（多机位拍摄）/A机位未剪辑片段/20260712_上午场第二段"]');
    await page.click('#page-browse tr[data-path="' + LONG_DIR + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    check("分布·钻到长路径叶子行打开详情", (await page.getAttribute("#detail", "data-path")) === LONG_DIR);
    await shot("08-long-path-detail");
    await page.keyboard.press("Escape");

    /* ===== C14 变化页交互：排序/搜索/同日守卫/报告 ===== */
    await gotoHash("changes");
    await page.click('#changes-table .th-sort[data-sort="path"]');
    const sortAttr = await page.$eval('#changes-table th[aria-sort] .th-sort', (el) => el.dataset.sort);
    check("变化·排序 aria-sort 落在 th 上", (await page.$$eval('#changes-table th[aria-sort]', (ths) => ths.length)) === 1 && sortAttr === "path", sortAttr);
    await page.fill("#changes-search", "下载");
    await page.waitForFunction(() => document.querySelectorAll("#changes-table tbody tr").length === 1, null, { timeout: 5000 });
    check("变化·搜索过滤到 1 行", true);
    await page.fill("#changes-search", "");
    await page.waitForFunction(() => document.querySelectorAll("#changes-table tbody tr").length === 7, null, { timeout: 5000 });
    check("变化·清空搜索恢复 7 行", true);
    await page.selectOption("#range-b", "2026-09-11");
    check("变化·同日对比被拦截并解释", ((await page.textContent('[data-region="diff-meta"]'))).includes("同一天"));
    await page.selectOption("#range-b", "2026-09-12");
    await page.click("#btn-reports");
    await page.waitForSelector("#reports-panel:not([hidden])", { timeout: 5000 });
    const rep = await page.textContent("#reports-panel");
    check("变化·历史报告含净变化与口径说明", rep.includes("+2.3 GiB") && rep.includes("同根同口径"));

    /* ===== C15 操作诚实性（Finder/复制为模拟） ===== */
    /* 变化表行不内嵌行操作：操作集中在目录详情（DESIGN：详情承担证据与操作入口） */
    await page.click('#page-changes tr[data-path="' + BUILD_PATH + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    await page.click('[data-region="detail-actions"] [data-action="finder"]');
    const finderToast = await page.textContent("#toast-region");
    check("操作·Finder 为模拟声明（不操作真实系统）", finderToast.includes("原型模拟") && finderToast.includes("不操作真实文件系统"), finderToast.trim().slice(0, 50));
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.body.dataset.detail === "closed", null, { timeout: 5000 });

    /* ===== C16 溢出矩阵：3 视口 × 5 页 ===== */
    for (const vp of [{ width: 980, height: 640 }, { width: 1220, height: 820 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(vp);
      for (const pg of ["overview", "changes", "browse", "bigfiles", "settings"]) {
        await gotoHash(pg);
        await noOverflow(vp.width + "×" + vp.height + " " + pg);
      }
    }
    await page.setViewportSize({ width: 980, height: 640 });
    await gotoHash("changes");
    await page.click('#page-changes tr[data-path="' + BUILD_PATH + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    await noOverflow("980×640 变化页+详情（复验）");
    await shot("09-980-changes-detail");
    await page.keyboard.press("Escape");

    /* ===== C16.5 1440×900：详情为右侧栏时关键列仍无需滚动即可见 ===== */
    await page.setViewportSize({ width: 1440, height: 900 });
    await gotoHash("changes");
    await page.click('#page-changes tr[data-path="' + BUILD_PATH + '"]');
    await page.waitForFunction(() => document.body.dataset.detail === "open", null, { timeout: 5000 });
    await page.waitForFunction(() => {
      const el = document.querySelector("#detail");
      return !el || el.getAnimations().length === 0;
    }, { timeout: 5000 });
    const dGeom1440 = await page.evaluate(() => {
      const el = document.querySelector("#detail");
      const r = el.getBoundingClientRect();
      return { x: r.x, w: r.width, vw: window.innerWidth, pos: getComputedStyle(el).position };
    });
    layoutMeasurements["detail-1440"] = dGeom1440;
    check("详情·1440px 为右侧栏（宽约 408，不超视口）",
      dGeom1440.pos !== "fixed" && dGeom1440.x + dGeom1440.w <= dGeom1440.vw + 1 && Math.abs(dGeom1440.w - 408) <= 10,
      "pos=" + dGeom1440.pos + " x=" + Math.round(dGeom1440.x) + " w=" + Math.round(dGeom1440.w));
    await keyColumnsVisible("1440 变化页+详情右栏");
    await noOverflow("1440×900 变化页+详情右栏");
    await shot("11-1440-changes-detail-rail");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.body.dataset.detail === "closed", null, { timeout: 5000 });

    /* ===== C17 运行期无错误 ===== */
    check("全程无 pageerror", pageErrors.length === 0, pageErrors.join("; ").slice(0, 200));
    check("全程无 console error", consoleErrors.length === 0, consoleErrors.join("; ").slice(0, 200));

    await browser.close();
    browser = null;
    await context.close();
  } finally {
    if (browser) { try { await browser.close(); } catch (_) {} }
    await new Promise((resolve) => server.close(resolve));
    line("静态服务与浏览器已关闭（资源清理）");
  }

  line("");
  line("结果：PASS " + passCount + " / FAIL " + failCount + " / 截图 " + shots.length + " 张");
  fs.writeFileSync(LOG_FILE, logLines.join("\n") + "\n", "utf8");
  fs.writeFileSync(
    RESULTS_JSON,
    JSON.stringify(
      {
        generated_at: new Date().toISOString(),
        git_head: head,
        playwright_error: playwrightErr ? String(playwrightErr.message) : null,
        passed: passCount,
        failed: failCount,
        screenshots: shots,
        results,
      },
      null,
      2
    ) + "\n",
    "utf8"
  );
  fs.writeFileSync(MEASURE_JSON, JSON.stringify({ generated_at: new Date().toISOString(), git_head: head, measurements: layoutMeasurements }, null, 2) + "\n", "utf8");
  line("结果 JSON: " + RESULTS_JSON);
  line("布局度量: " + MEASURE_JSON);
  line("日志: " + LOG_FILE);
  if (failCount > 0) process.exitCode = 1;
}

main().catch((e) => {
  line("验证脚本异常退出：" + (e && e.stack ? e.stack : e));
  fs.writeFileSync(LOG_FILE, logLines.join("\n") + "\n异常: " + String(e && e.stack ? e.stack : e) + "\n", "utf8");
  process.exitCode = 1;
});
