#!/usr/bin/env node
/* ISS-182：生产前端真实 Chromium + 明示的壳 IPC 夹具。
 * 两个随机 origin 模拟 helper 换端口，状态保存在 Node 夹具中，不用 web storage。
 * 此套件验证恢复接线/迟到响应；不冒充真实 Rust 更新、验签或 OS 回滚验证。 */
"use strict";
const { chromium } = require("playwright");
const { once } = require("events");
const { createFixture } = require("./verify_frontend_refresh.cjs");
const checks = [];
function record(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail });
  console.error(`${ok ? "PASS" : "FAIL"} ${name} | ${detail}`);
}
const available = { state: "available", current_version: "0.4.0", available_version: "0.4.1" };
const failed = { ...available, state: "failed", kind: "install", error: "invalid gzip header", hint: "已恢复旧版本与数据库，可重试" };
let snapshot = { recovery: true, revision: 2, generation: 1, status: available, activity: failed };
let delayedRecovery = false, resolveRecovery = null, rejectBusy = false, delayBusy = false, resolveBusy = null;
async function main() {
  const fixtures = [createFixture(), createFixture()];
  let browser;
  try {
    for (const f of fixtures) {
      f.state.config.auto_download_updates = false;
      f.server.listen(0, "127.0.0.1"); await once(f.server, "listening");
    }
    const bases = fixtures.map(f => `http://127.0.0.1:${f.server.address().port}`);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    await page.exposeFunction("__bridgeInvoke", async (cmd, args) => {
      if (cmd === "updater_check" && args?.recover) {
        const captured = structuredClone(snapshot);
        if (delayedRecovery) return new Promise(resolve => { resolveRecovery = () => resolve(captured); });
        return captured;
      }
      if (cmd === "updater_check") {
        const retry = args?.retry === true && ["failed", "cancelled"].includes(snapshot.activity?.state);
        snapshot = { ...snapshot, generation: snapshot.generation + (retry ? 1 : 0),
          revision: snapshot.revision + 1, status: available, activity: retry ? null : snapshot.activity };
        const result = { ...available, generation: snapshot.generation, revision: snapshot.revision };
        if (retry) {
          // Explicit retry may reuse the already verified byte cache, as Rust does.
          snapshot = { ...snapshot, revision: snapshot.revision + 1,
            activity: { ...available, state: "downloaded", generation: snapshot.generation, revision: snapshot.revision + 1 } };
          queueMicrotask(() => page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }), snapshot.activity));
        }
        return result;
      }
      if (cmd === "updater_install") {
        if (rejectBusy) {
          const result = { ok: false, state: "busy", error: "已有下载或安装事务在进行中" };
          if (delayBusy) return new Promise(resolve => { resolveBusy = () => resolve(result); });
          return result;
        }
        snapshot = { recovery: true, generation: snapshot.generation + 1, revision: snapshot.revision + 1, status: available,
          activity: { ...available, state: "installed", hint: "更新已安装；重启后生效" } };
        return { ...snapshot.activity, ok: true, generation: snapshot.generation, revision: snapshot.revision };
      }
      if (cmd === "autostart_status") return { scan: "disabled", web: "disabled", login_item: "unknown" };
      return null;
    });
    await page.addInitScript(() => {
      window.__listeners = {};
      window.__listenerCount = 0;
      window.__TAURI__ = { core: { invoke: (cmd, args) => window.__bridgeInvoke(cmd, args) }, event: {
        listen: (name, cb) => { window.__listeners[name] = cb; if (name === "updater-state") window.__listenerCount++; return Promise.resolve(() => {}); },
        emit: () => Promise.resolve(),
      } };
    });
    const about = async (base) => {
      await page.goto(`${base}/#settings/about`, { waitUntil: "networkidle" });
      return page.evaluate(() => ({ settings: !document.getElementById("page-settings").classList.contains("hidden"),
        about: !document.getElementById("settings-section-about").hidden }));
    };
    const text = () => page.evaluate(() => document.querySelector("[data-test='updater-terminal']")?.textContent || "");
    const first = await about(bases[0]);
    record("reload-settings-about-route", first.settings && first.about, JSON.stringify(first));
    // Continue collecting the terminal counterexample when the old router falls back.
    if (!first.settings) { await page.click("a[data-page='settings']"); await page.click("[data-section='about']"); }
    await page.waitForSelector("[data-test='updater-check-btn']");
    const firstError = await text();
    record("reload-restores-failure-fields", firstError.includes("invalid gzip header") && firstError.includes("已恢复")
      && await page.locator("[data-test='updater-terminal']").getAttribute("data-kind").catch(() => "") === "install", firstError);
    snapshot = { recovery: true, revision: 5, generation: 2, status: available, activity: failed };
    const second = await about(bases[1]);
    const secondError = await text();
    record("origin-change-restores-failure", second.settings && second.about && secondError.includes("invalid gzip header"), secondError);
    // Delayed snapshot is captured before a newer live event. Its response may
    // arrive later, but must not resurrect the old terminal after new preparing.
    delayedRecovery = true;
    const navigation = page.goto(`${bases[0]}/#settings/about`, { waitUntil: "networkidle" });
    const deadline = Date.now() + 5000;
    while (!resolveRecovery && Date.now() < deadline) await new Promise(r => setTimeout(r, 20));
    if (resolveRecovery) {
      await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }),
        { ...available, state: "preparing", generation: 3, revision: 6 });
      resolveRecovery(); delayedRecovery = false; await navigation;
      const line = await page.locator("[data-test='updater-status-text']").textContent();
      record("late-recovery-cannot-overwrite-new-operation", line.includes("正在准备") && !(await text()), line);
    } else { delayedRecovery = false; await navigation; record("late-recovery-cannot-overwrite-new-operation", false, "未发起恢复查询"); }
    snapshot = { recovery: true, generation: 3, revision: 7, status: available, activity: failed };
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }), { ...failed, generation: 3, revision: 7 });
    await page.click("[data-test='updater-check-btn']");
    await page.waitForSelector("[data-test='updater-install-btn']");
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }), { ...failed, error: "superseded failure", generation: 2, revision: 5 });
    record("query-keeps-current-failure-rejects-previous-terminal", (await text()).includes("invalid gzip header")
      && !(await text()).includes("superseded failure"), await text());
    rejectBusy = true;
    await page.click("[data-test='updater-install-btn']");
    await page.click("[data-test='updater-confirm-yes']");
    await page.waitForSelector("[data-test='updater-note']");
    const busyNote = await page.locator("[data-test='updater-note']").textContent();
    record("synced-protocol-still-shows-busy-rejection", busyNote.includes("已有下载或安装事务")
      && await page.locator("[data-test='updater-install-btn']").isVisible(), busyNote);
    delayBusy = true;
    await page.click("[data-test='updater-install-btn']");
    await page.click("[data-test='updater-confirm-yes']");
    const busyDeadline = Date.now() + 5000;
    while (!resolveBusy && Date.now() < busyDeadline) await new Promise(r => setTimeout(r, 20));
    if (!resolveBusy) throw new Error("busy延迟请求未发出");
    snapshot = { ...snapshot, generation: snapshot.generation + 1, revision: snapshot.revision + 1,
      activity: { ...available, state: "preparing" } };
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }),
      { ...snapshot.activity, generation: snapshot.generation, revision: snapshot.revision });
    resolveBusy();
    await page.waitForSelector("[data-test='updater-note']");
    const busyProgress = await page.locator("[data-test='updater-status-text']").textContent();
    record("late-busy-cannot-restore-previous-failure", busyProgress.includes("正在准备") && !(await text()), busyProgress);
    // Finish that other accepted operation, then explicitly check to retry a new one.
    snapshot = { ...snapshot, revision: snapshot.revision + 1, activity: failed };
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }),
      { ...failed, generation: snapshot.generation, revision: snapshot.revision });
    rejectBusy = false; delayBusy = false;
    await page.click("[data-test='updater-check-btn']");
    await page.waitForSelector("[data-test='updater-install-btn']");
    await page.click("[data-test='updater-install-btn']");
    await page.click("[data-test='updater-confirm-yes']");
    await page.waitForSelector("[data-test='updater-restart-btn']");
    record("new-install-result-shows-restart", await page.locator("[data-test='updater-restart-btn']").isVisible(), "invoke成功终态");
    const installedGeneration = snapshot.generation;
    await page.click("[data-test='updater-check-btn']");
    const installedAfterQuery = await page.locator("[data-test='updater-status-text']").textContent();
    record("manual-check-after-installed-keeps-restart", snapshot.generation === installedGeneration
      && installedAfterQuery.includes("重启后生效") && await page.locator("[data-test='updater-restart-btn']").isVisible(),
      installedAfterQuery);
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }),
      { ...available, state: "downloaded", generation: snapshot.generation, revision: snapshot.revision + 1 });
    record("new-install-rejects-same-generation-ready-before-reload",
      await page.locator("[data-test='updater-restart-btn']").isVisible(), "invoke返回也保留世代号");
    // Installed 后预下载缓存已清空，且自动下载仍开；恢复不能变成重新下载。
    fixtures[1].state.config.auto_download_updates = true;
    const installedRoute = await about(bases[1]);
    const restart = await page.locator("[data-test='updater-restart-btn']").isVisible().catch(() => false);
    record("reload-restores-installed-restart", installedRoute.settings && installedRoute.about && restart, `restart=${restart}`);
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }), { ...failed, generation: 2, revision: 5 });
    record("old-failure-cannot-overwrite-installed", !(await text()) && await page.locator("[data-test='updater-restart-btn']").isVisible().catch(() => false), await text());
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }),
      { ...available, state: "up_to_date", presentation: "check", generation: snapshot.generation, revision: snapshot.revision + 1 });
    record("startup-check-cannot-hide-installed-restart",
      await page.locator("[data-test='updater-restart-btn']").isVisible(), "启动检查不替代已安装终态");
    await page.evaluate(payload => window.__listeners["updater-state"]?.({ payload }),
      { ...available, state: "downloaded", generation: snapshot.generation, revision: snapshot.revision + 2 });
    record("same-generation-ready-cannot-hide-installed-restart",
      await page.locator("[data-test='updater-restart-btn']").isVisible(), "已清预下载缓存，旧就绪通知不替代终态");
    await page.click("a[data-page='overview']");
    await page.click("a[data-page='settings']");
    await page.click("[data-section='about']");
    const subscriptions = await page.evaluate(() => window.__listenerCount);
    record("page-reentry-has-one-updater-subscription", subscriptions === 1, `listeners=${subscriptions}`);
    // A separate failed transaction can still be retried explicitly with auto download on.
    snapshot = { recovery: true, generation: snapshot.generation + 1, revision: snapshot.revision + 10,
      status: available, activity: failed };
    await page.reload({ waitUntil: "networkidle" });
    await page.click("[data-test='updater-retry-download-btn']");
    await page.waitForSelector("[data-test='updater-install-ready-btn']");
    record("explicit-download-retry-starts-next-generation", !(await text())
      && snapshot.activity?.state === "downloaded", "显式重试仍可进入已验签就绪态");
  } finally {
    if (browser) await browser.close();
    for (const f of fixtures) { f.server.close(); await once(f.server, "close"); }
  }
  const failures = checks.filter(c => !c.ok);
  console.log(JSON.stringify({ ok: !failures.length, passed: checks.length - failures.length, failed: failures.length, checks }));
  if (failures.length) process.exitCode = 1;
}
main().catch(e => { console.error(e); process.exitCode = 1; });
