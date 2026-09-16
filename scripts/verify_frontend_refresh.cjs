#!/usr/bin/env node
/* 前端回归（ISS-023 起源，ISS-027 扩展）：随机端口、纯合成 API、真实 Chromium。
 * ISS-027 新增口径：
 *  - 乱序/迟到响应只显示当前选择（含切目录竞态）；
 *  - 切页后扫描轮询不重复累积，扫描结束后轮询停止；
 *  - 图表隐藏期间窗口变化，重新显示后尺寸恢复；
 *  - 五页均真实走查（含大文件/设置），资源全部同源本地；
 *  - 浏览器无 Tauri（静默降级）与注入 mock 桥两种路径。
 * 驱动方式只经真实 UI（导航/点击/选择），不调用前端内部函数。 */
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
const evidenceDir = fs.mkdtempSync(path.join(os.tmpdir(), "fathom-iss027-"));
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
    "Cache-Control": "no-store",  // 合成夹具状态随调用变化，禁止任何缓存
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
  // ISS-016A：设置页真实配置夹具。取可辨别值（13:30 / 5120 / 3.5 / 21 天），
  // 前端若回退硬编码（12:00 / 10240 / 10 / 35 天）检查即失败。
  const initialConfig = () => ({
    scan_root: ROOT,
    scan_time: "13:30",
    min_kb: 5120,
    free_alert_gb: 3.5,
    sources: { scan_root: "default", scan_time: "settings", min_kb: "settings", free_alert_gb: "settings" },
    defaults: { scan_root: "/fixture/home", scan_time: "12:00", min_kb: 10240, free_alert_gb: 10 },
    policies: { keep_daily_days: 21, keep_weekly_weeks: 8, du_timeout_s: 14400, bigfile_default_days: 7, bigfile_default_mb: 100 },
    settings_path: "/fixture/runtime/settings.json",
  });
  const state = {
    mode: "dual", version: 2, scanning: false, lastDiff: null,
    staleDiffs: 0, treeSnapshotId: null, scenario: null, counts: {},
    config: initialConfig(),
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
    if (state.mode === "partial") {
      // 部分权限场景：denied_count > 0 + collection_status=partial
      return [{ ...latest, denied_count: 6, collection_status: "partial",
                dir_count: 12 }, snapshot(1, "2026-09-12T10:00:00")];
    }
    if (state.mode === "partial-vanished") {
      // ISS-002A：扫描期间消失 N 处；字段 ISS-066 落地后由 API 暴露
      return [{ ...latest, vanished_count: 4, collection_status: "partial",
                dir_count: 20 }, snapshot(1, "2026-09-12T10:00:00")];
    }
    if (state.mode === "partial-excluded") {
      // ISS-002A：排除掩码 N 项；exclude_names 数组
      return [{ ...latest, exclude_names: ["*.noindex", "*.tmp"],
                collection_status: "partial", dir_count: 30 },
              snapshot(1, "2026-09-12T10:00:00")];
    }
    if (state.mode === "partial-all") {
      // ISS-002A：三类缺口并存
      return [{ ...latest, denied_count: 6, vanished_count: 4,
                exclude_names: ["*.noindex", "*.tmp", ".git"],
                collection_status: "partial", dir_count: 40 },
              snapshot(1, "2026-09-12T10:00:00")];
    }
    if (state.mode === "partial-no-fields") {
      // ISS-002A：vanished_count 与 exclude_names 字段缺失，验证 ?? 0 / 防御
      // 注意：denied_count=6 触发 partial，但 vanished/excluded 必须被防御为 0
      // （不显示对应 chip，且 scan-note 也不出现 "消失" / "排除" 计数）。
      return [{ ...latest, denied_count: 6, collection_status: "partial",
                dir_count: 12 }, snapshot(1, "2026-09-12T10:00:00")];
    }
    return [latest, snapshot(1, "2026-09-12T10:00:00")];
  };
  const scanning = () => state.scanning || state.mode === "scanning-stuck";

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
        state.config = initialConfig();
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
      nextCall("status");
      const rows = snapshots();
      return json(res, 200, {
        root: ROOT,
        snapshot_count: rows.length,
        latest_snapshot: rows[0] || null,
        disk: { total_bytes: 1024 ** 4, free_bytes: 256 * 1024 ** 3 },
        db_bytes: 4096,
        scan: {
          running: scanning(),
          started_at: scanning() ? "2026-09-13T12:02:00" : null,
          finished_at: state.version === 3 ? "2026-09-13T12:03:00" : null,
        },
        port: server.address().port,
      });
    }
    if (url.pathname === "/api/snapshots") {
      if (state.mode === "snapshots500") return json(res, 500, { detail: "合成快照故障" });
      if (state.scenario === "snapshots-delay" && nextCall("snapshots") === 1) {
        return later(res, 200, snapshots(), 700);
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
      // 总览摘要固定带 topn=5；竞态只针对摘要请求计数，不影响变化页对比
      if (state.scenario === "overview-old-success-new-500" && url.searchParams.get("topn") === "5") {
        const call = nextCall("overviewDiff");
        if (call === 1) return later(res, 200, diff(1, 2), 1200);
        return json(res, 500, { detail: "较新的合成差分故障" });
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
      if (state.scenario === "net-overlap" || state.scenario === "net-nobaseline") {
        // 净变化口径夹具（ISS-028 修复验证）：
        //  - net-overlap：grown 含父子重叠（父 +100 与子 +33/+33 同时入选，DEC-005
        //    fold_changes 不去重祖先），逐行求和 = +166；根总量 a→b 差 = +100。
        //    页面必须显示根差分而非行和。
        //  - net-nobaseline：a/b 缺 total_kb（模拟首扫无基线），页面必须显示
        //    “无基线”，不得显示 0 或回退行求和（此处行和 = +2.0 MB）。
        const body = diff(a, b);
        if (state.scenario === "net-overlap") {
          body.a = { ...body.a, total_kb: 300000 };
          body.b = { ...body.b, total_kb: 300100 };
          body.grown = [
            { path: `${ROOT}/Parent`, old_kb: 40000, new_kb: 40100, delta_kb: 100 },
            { path: `${ROOT}/Parent/A`, old_kb: 20000, new_kb: 20033, delta_kb: 33 },
            { path: `${ROOT}/Parent/B`, old_kb: 20000, new_kb: 20033, delta_kb: 33 },
          ];
          body.shrunk = [];
        } else {
          const { total_kb: _dropA, ...aMeta } = body.a;
          const { total_kb: _dropB, ...bMeta } = body.b;
          body.a = aMeta;
          body.b = bMeta;
          body.grown = [{ path: `${ROOT}/Big`, old_kb: 100000, new_kb: 102048, delta_kb: 2048 }];
          body.shrunk = [];
        }
        return json(res, 200, body);
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
        if (call === 1) return later(res, 200, { reports: [{ date: "2026-09-11" }] }, 1200);
        return json(res, 500, { detail: "较新的合成报告故障" });
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
          truncated: false, matched_count: 2, node_count: 2, node_limit: 20000,
        };
        if (call === 1) return later(res, 200, body, 800);
        return json(res, 200, body);
      }
      const rows = snapshots();
      const sid = rows[0] ? rows[0].id : null;
      state.treeSnapshotId = sid;
      const truncated = state.scenario === "trees-truncated";
      return json(res, 200, sid == null ? { snapshot_id: null, root: null, children: [],
        truncated: false, matched_count: 0, node_count: 0, node_limit: 20000 } : {
        snapshot_id: sid,
        root: ROOT,
        children: [{ name: "root", path: ROOT, value: 300000, children: [
          { name: "Archive", path: `${ROOT}/Archive`, value: 98976 },
          { name: "Stable", path: `${ROOT}/Stable`, value: 40000 },
        ] }],
        truncated,
        matched_count: truncated ? 47 : 2,
        node_count: truncated ? 20000 : 2,
        node_limit: 20000,
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
        if (call === 1) return later(res, 200, body, 800);
        return json(res, 200, body);
      }
      if (state.scenario === "browse-switch-race") {
        // 模拟快速切目录：奇数次（旧目录）慢响应，偶数次（新目录）先回
        const call = nextCall("browse");
        const odd = call % 2 === 1;
        const body = odd
          ? {
            path: `${ROOT}/Archive`, size_kb: 98976,
            crumbs: [{ name: "root", path: ROOT }, { name: "Archive", path: `${ROOT}/Archive` }],
            children: [{ name: "SlowOldDir", path: `${ROOT}/Archive/SlowOldDir`,
              size_kb: 40000, delta_kb: 1024, is_new: false }],
            trend: [],
          }
          : {
            path: ROOT, size_kb: 300000, crumbs: [{ name: "root", path: ROOT }],
            children: [{ name: "FastNewDir", path: `${ROOT}/FastNewDir`,
              size_kb: 60000, delta_kb: -512, is_new: false }],
            trend: [],
          };
        if (odd) return later(res, 200, body, 1000);
        return json(res, 200, body);
      }
      if (state.scenario === "browse-navigation-delay") {
        nextCall("browse");
        return later(res, 200, {
          path: ROOT, size_kb: 300000, crumbs: [{ name: "root", path: ROOT }],
          children: [{ name: "OldAfterNavigation", path: `${ROOT}/OldAfterNavigation`, size_kb: 90000,
            delta_kb: 1024, is_new: false }],
          trend: [],
        }, 600);
      }
      if (!snapshots().length) return json(res, 409, { detail: "尚无快照，请先扫描" });
      return json(res, 200, {
        path: ROOT,
        size_kb: 300000,
        crumbs: [{ name: "root", path: ROOT }],
        children: [
          {
            name: "Archive", path: `${ROOT}/Archive`, size_kb: 98976,
            delta_kb: state.mode === "single" ? null : -1024,
            is_new: false,
          },
          {
            name: "Stable", path: `${ROOT}/Stable`, size_kb: 40000,
            delta_kb: state.mode === "single" ? null : 0,
            is_new: false,
          },
        ],
        trend: [{ created_at: "2026-09-12T10:00:00", size_kb: 100000 },
          { created_at: "2026-09-13T12:03:00", size_kb: 98976 }],
      });
    }
    if (url.pathname === "/api/bigfiles") {
      // 字段合同与 fathom/bigfiles.py 一致；ISS-032 扩展：
      //   state: ok | no_match | permission_denied | failed | truncated | expired
      //   scope: {root, days, min_mb, topn}
      //   stats: {wall_ms, peak_rss_bytes, find_output_lines, ...}
      //   truncated / expired / cached / cache_age_s / error_message / raw_truncated
      const days = Number(url.searchParams.get("days") || 7);
      const min_mb = Number(url.searchParams.get("min_mb") || 100);
      const topn = Number(url.searchParams.get("topn") || 50);
      const scope = { root: ROOT, days, min_mb, topn };
      const stats = (extra = {}) => ({
        wall_ms: 12,
        peak_rss_bytes: 1392640,
        find_output_lines: 1,
        find_exit_code: 0,
        find_stderr_lines: 0,
        permission_denied_lines: 0,
        ...extra,
      });
      const fileRow = { path: `${ROOT}/Build/fathom-disk.img`, size: 2 * 1024 ** 3, mtime: "2026-09-12 09:00" };
      if (state.scenario === "bigfiles-no-match") {
        return json(res, 200, {
          state: "no_match", files: [], scope,
          stats: stats({ find_output_lines: 0 }),
          truncated: false, raw_truncated: false,
          expired: false, cached: false, cache_age_s: null,
          error_message: null,
        });
      }
      if (state.scenario === "bigfiles-truncated") {
        return json(res, 200, {
          state: "truncated", files: [fileRow], scope,
          stats: stats({ find_output_lines: 17 }),
          truncated: true, raw_truncated: false,
          expired: false, cached: false, cache_age_s: null,
          error_message: null,
        });
      }
      if (state.scenario === "bigfiles-expired") {
        return json(res, 200, {
          state: "expired", files: [fileRow], scope,
          stats: stats(),
          truncated: false, raw_truncated: false,
          expired: true, cached: true, cache_age_s: 31.2,
          error_message: null,
        });
      }
      if (state.scenario === "bigfiles-failed") {
        return json(res, 200, {
          state: "failed", files: [], scope,
          stats: stats({ find_exit_code: 23, find_stderr_lines: 2 }),
          truncated: false, raw_truncated: false,
          expired: false, cached: false, cache_age_s: null,
          error_message: "find 退出 23：/scanroot/root: No such file or directory",
        });
      }
      if (state.scenario === "bigfiles-permission-denied") {
        return json(res, 200, {
          state: "permission_denied", files: [], scope,
          stats: stats({ find_exit_code: 1, find_stderr_lines: 3, permission_denied_lines: 3 }),
          truncated: false, raw_truncated: false,
          expired: false, cached: false, cache_age_s: null,
          error_message: "find 退出 1，3 行权限受限",
        });
      }
      // 默认 OK
      return json(res, 200, {
        state: "ok", files: [fileRow], scope,
        stats: stats(),
        truncated: false, raw_truncated: false,
        expired: false, cached: false, cache_age_s: null,
        error_message: null,
      });
    }
    if (url.pathname === "/api/reveal" && req.method === "POST") return json(res, 200, { ok: true });
    if (url.pathname === "/api/config" && req.method === "PUT") {
      if (req.headers["x-fathom-token"] !== TOKEN) {
        return json(res, 403, { detail: "已拒绝：写请求需要 X-Fathom-Token" });
      }
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        let body;
        try {
          body = JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}");
        } catch {
          return json(res, 400, { detail: "请求体必须是合法 JSON 对象" });
        }
        if (typeof body !== "object" || body === null || Array.isArray(body)) {
          return json(res, 400, { detail: "请求体必须是 JSON 对象" });
        }
        // 形状校验镜像 fathom/config.py 的合同；scan_root 的存在性/目录校验
        // 依赖真实文件系统，夹具根是合成路径，只校验键与数值形状（后端
        // pytest 已覆盖完整校验矩阵）。
        const allowed = ["scan_root", "scan_time", "min_kb", "free_alert_gb"];
        const unknown = Object.keys(body).filter((k) => !allowed.includes(k));
        if (unknown.length) {
          return json(res, 400, { detail: `未知的配置项：${unknown.join(", ")}` });
        }
        if (body.scan_time != null && !/^([01]\d|2[0-3]):([0-5]\d)$/.test(String(body.scan_time).trim())) {
          return json(res, 400, { detail: "scan_time 必须是 HH:MM 格式（00:00–23:59）" });
        }
        for (const key of ["min_kb", "free_alert_gb"]) {
          const value = body[key];
          if (value == null) continue;
          if (typeof value === "boolean" || typeof value !== "number"
              || !Number.isFinite(value) || value <= 0) {
            return json(res, 400, { detail: `${key} 必须是正的有限数值` });
          }
        }
        nextCall("configPut");
        for (const key of allowed) {
          if (body[key] !== undefined && body[key] !== null) state.config[key] = body[key];
        }
        return json(res, 200, {
          applied: true,
          service_reload: "requires_user_action",
          hint: "已保存到 settings.json 并在当前服务进程生效；已安装的 launchd 后台计划不受影响，需重新安装（main.py install）后才按新计划时间运行。",
          config: { ...state.config },
        });
      });
      return;
    }
    if (url.pathname === "/api/config") return json(res, 200, { ...state.config });
    if (url.pathname.startsWith("/api/scan/status")) {
      // 历史记录（ISS-028 m3 设置页）
      return json(res, 200, {
        running: state.scanning,
        started_at: state.scanning ? "2026-09-13T12:02:00" : null,
        finished_at: state.version === 3 ? "2026-09-13T12:03:00" : null,
        source: "api",
        message: state.scanning ? "进行中" : (state.version === 3 ? "成功 · du 耗时 42 秒" : "—"),
        runs: [
          { id: 1, started_at: "2026-09-12T12:00:00", finished_at: "2026-09-12T12:00:42",
            status: "done", source: "scheduled", phase: "scan", snapshot_id: 2, report_status: "ok",
            notification_status: "ok", pruned_count: 0, message: "成功 · du 耗时 42 秒" },
          { id: 2, started_at: "2026-09-11T12:00:00", finished_at: "2026-09-11T12:00:51",
            status: "done", source: "scheduled", phase: "scan", snapshot_id: 1, report_status: "ok",
            notification_status: "ok", pruned_count: 0, message: "成功 · du 耗时 51 秒" },
        ],
      });
    }
    if (url.pathname === "/api/scan" && req.method === "POST") {
      if (req.headers["x-fathom-token"] !== TOKEN) return json(res, 403, { detail: "令牌无效" });
      nextCall("scan");
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
    // ES modules（frontend/modules/）经受限字符集路径直接透出，禁止穿越
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
    const resourceUrls = [];
    page.on("request", (r) => resourceUrls.push(r.url()));
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
    const fixtureState = async () =>
      (await (await page.request.get(`${base}/__fixture`)).json());
    // 仅片段不同的 goto 可能被 Chromium 当作 same-document 导航（不重跑应用
    // init）。夹具状态切换后需要整页加载时用本入口：先脱离当前文档，保证
    // 后续 goto 必然是全新加载。
    const openPage = async (hash, waitUntil = "networkidle") => {
      if (page.url() !== "about:blank") await page.goto("about:blank");
      await page.goto(`${base}/${hash}`, { waitUntil });
    };
    const waitForCount = async (name, count) => {
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        const body = await fixtureState();
        if ((body.counts[name] || 0) >= count) return body;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      throw new Error(`等待夹具调用超时：${name} >= ${count}`);
    };

    /* ---------- 总览基线 ---------- */
    await openPage("#/overview");
    const foreign = resourceUrls.filter((u) => !u.startsWith(base));
    record("all-resources-are-local", foreign.length === 0, foreign.join(","));
    await page.waitForSelector("#overview-summary table");
    const summaryText = await page.locator("#overview-summary").textContent();
    record("delta-signs-rendered-in-overview",
      summaryText.includes("+1.0 MB") && summaryText.includes("−1.0 MB") &&
        !summaryText.includes("NaN"), summaryText.slice(0, 60));
    const overviewIcon = await page.evaluate(() => ({
      literal: document.querySelector("#overview-summary").textContent.includes("icon("),
      svg: Boolean(document.querySelector("#overview-summary [data-reveal] svg")),
    }));
    record("overview-finder-button-is-svg", !overviewIcon.literal && overviewIcon.svg);
    const overviewShot = path.join(evidenceDir, "overview-values-1220x820.png");
    await page.screenshot({ path: overviewShot });

    /* ---------- 变化页：同日补扫协调 ---------- */
    await openPage("#/changes");
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

    /* ---------- 分布页 + 零差值渲染 ---------- */
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
    record("delta-zero-rendered-in-browse", browse.text.includes("0.0 B"), browse.text.slice(0, 80));
    const browseShot = path.join(evidenceDir, "browse-after-rescan-1220x820.png");
    await page.screenshot({ path: browseShot });

    /* ---------- 图表隐藏后再显示：窗口在隐藏期间变化，重显后必须重新量尺寸 ---------- */
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.waitForSelector("#chart-volume canvas");
    const chartBefore = await page.evaluate(() => {
      const box = document.getElementById("chart-volume");
      return { box: box.clientWidth, canvas: box.querySelector("canvas").getBoundingClientRect().width };
    });
    await page.click('a[data-page="changes"]');
    await page.waitForURL("**/#/changes");
    await page.setViewportSize({ width: 1000, height: 820 });  // 总览隐藏时窗口变化
    await page.waitForTimeout(250);
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.waitForTimeout(450);
    const chartAfter = await page.evaluate(() => {
      const box = document.getElementById("chart-volume");
      return { box: box.clientWidth, canvas: box.querySelector("canvas").getBoundingClientRect().width };
    });
    record("chart-resize-after-reshow",
      chartAfter.canvas > 0 && Math.abs(chartAfter.canvas - chartAfter.box) <= 2,
      JSON.stringify({ before: chartBefore, after: chartAfter }));
    await page.setViewportSize({ width: 1220, height: 820 });

    /* ---------- 大文件 / 设置页走查（五页覆盖） ---------- */
    await openPage("#/bigfiles");
    await page.waitForSelector("#tbl-bigfiles tbody tr");
    const big = await page.evaluate(() => ({
      text: document.querySelector("#tbl-bigfiles").textContent,
      svg: Boolean(document.querySelector("#tbl-bigfiles [data-reveal] svg")),
    }));
    record("bigfiles-page-renders-row",
      big.text.includes("2.0 GB") && big.text.includes("2026-09-12 09:00") &&
        big.text.includes("fathom-disk.img") && big.svg, big.text.slice(0, 80));
    const bigfilesShot = path.join(evidenceDir, "bigfiles-1220x820.png");
    await page.screenshot({ path: bigfilesShot });

    /* ---------- 大文件状态矩阵（ISS-032） ---------- */
    // 默认已是 OK；显式遍历其余五态（截断/过期/失败/权限/无匹配）。
    // 通过 setScenario 切换夹具，然后重新打开页面让前端重发请求。
    const bigfilesReadBody = async () =>
      page.evaluate(() => document.querySelector("#tbl-bigfiles tbody").textContent);

    await setScenario("bigfiles-truncated");
    await openPage("#/bigfiles");
    await waitForText(page, "#tbl-bigfiles tbody", "结果被截断");
    const truncated = await bigfilesReadBody();
    record("bigfiles-truncated-state-shown",
      truncated.includes("结果被截断") &&
        truncated.includes("已截断到 top 200") &&
        truncated.includes("find 行数 17"), truncated.slice(0, 120));
    const bigfilesTruncatedShot = path.join(evidenceDir, "bigfiles-truncated-1220x820.png");
    await page.screenshot({ path: bigfilesTruncatedShot });

    await setScenario("bigfiles-expired");
    await openPage("#/bigfiles");
    await waitForText(page, "#tbl-bigfiles tbody", "缓存已过期");
    const expired = await bigfilesReadBody();
    record("bigfiles-expired-state-shown",
      expired.includes("缓存已过期") && expired.includes("31.2"),
      expired.slice(0, 120));
    const bigfilesExpiredShot = path.join(evidenceDir, "bigfiles-expired-1220x820.png");
    await page.screenshot({ path: bigfilesExpiredShot });

    await setScenario("bigfiles-failed");
    await openPage("#/bigfiles");
    await waitForText(page, "#tbl-bigfiles tbody", "查询失败");
    const bigFailed = await bigfilesReadBody();
    record("bigfiles-failed-state-shown",
      bigFailed.includes("查询失败") &&
        bigFailed.includes("No such file or directory"),
      bigFailed.slice(0, 120));
    const bigfilesFailedShot = path.join(evidenceDir, "bigfiles-failed-1220x820.png");
    await page.screenshot({ path: bigfilesFailedShot });

    await setScenario("bigfiles-permission-denied");
    await openPage("#/bigfiles");
    await waitForText(page, "#tbl-bigfiles tbody", "权限受限");
    const permDenied = await bigfilesReadBody();
    record("bigfiles-permission-denied-state-shown",
      permDenied.includes("权限受限") && permDenied.includes("3 行权限受限"),
      permDenied.slice(0, 120));
    const bigfilesPermShot = path.join(evidenceDir, "bigfiles-permission-denied-1220x820.png");
    await page.screenshot({ path: bigfilesPermShot });

    await setScenario("bigfiles-no-match");
    await openPage("#/bigfiles");
    await waitForText(page, "#tbl-bigfiles tbody", "无匹配文件");
    const noMatch = await bigfilesReadBody();
    record("bigfiles-no-match-state-shown",
      noMatch.includes("无匹配文件") &&
        noMatch.includes("7 天") &&
        noMatch.includes("100 MB"),
      noMatch.slice(0, 120));
    const bigfilesNoMatchShot = path.join(evidenceDir, "bigfiles-no-match-1220x820.png");
    await page.screenshot({ path: bigfilesNoMatchShot });

    // 收尾：清空 scenario，避免污染后续总览/变化用例
    await setScenario(null);
    // 回到默认 OK 状态，以便任何后续断言（若依赖 bigfiles）不踩雷
    await openPage("#/bigfiles");
    await page.waitForSelector("#tbl-bigfiles tbody tr");
    await page.click("#btn-bigfiles");

    await openPage("#/settings");
    await page.waitForSelector("#settings-table tbody tr");
    const settingsText = await page.locator("#page-settings").textContent();
    record("settings-page-renders-config",
      settingsText.includes("监控根目录") && settingsText.includes(ROOT) &&
        settingsText.includes("服务地址"), settingsText.slice(0, 60));

    /* ---------- 设置页真实配置（ISS-016A） ---------- */
    // 夹具取可辨别值（13:30 / 5120 / 3.5 / 21 天 / 8 周）：页面必须显示
    // 服务端值；若回退旧硬编码（12:00 / 10240 / 10 / 35 天 / 12 周）即失败。
    await waitForText(page, "#config-effective", "13:30");
    const settingsLiveText = await page.locator("#page-settings").textContent();
    record("settings-page-shows-server-config",
      settingsLiveText.includes("13:30") && settingsLiveText.includes("5120") &&
        settingsLiveText.includes("3.5") && settingsLiveText.includes("21 天每日一份") &&
        settingsLiveText.includes("8 周") && !settingsLiveText.includes("35 天"),
      settingsLiveText.slice(0, 120));
    const settingsShot = path.join(evidenceDir, "settings-config-1220x820.png");
    await page.screenshot({ path: settingsShot });

    // 无效输入：服务端 400 → 反馈原因；当前生效值保持旧值（13:30）可辨。
    await page.fill("#cfg-scan-time", "25:00");
    await page.click("#btn-config-save");
    await waitForText(page, "#config-feedback", "保存失败");
    const invalidSave = await page.evaluate(() => ({
      feedback: document.getElementById("config-feedback").textContent,
      effective: document.getElementById("config-effective").textContent,
    }));
    record("settings-invalid-input-feedback-keeps-old-value",
      invalidSave.feedback.includes("HH:MM") &&
        invalidSave.effective.includes("13:30") &&
        fixture.state.config.scan_time === "13:30",
      JSON.stringify(invalidSave).slice(0, 120));

    // 有效保存：生效值更新，且如实显示“需重新安装计划才生效”。
    await page.fill("#cfg-scan-time", "09:15");
    await page.fill("#cfg-min-kb", "2048");
    await page.click("#btn-config-save");
    await waitForText(page, "#config-feedback", "需重新安装");
    const appliedSave = await page.evaluate(() => ({
      feedback: document.getElementById("config-feedback").textContent,
      effective: document.getElementById("config-effective").textContent,
    }));
    record("settings-save-applies-and-hints-reinstall",
      appliedSave.effective.includes("09:15") && appliedSave.effective.includes("2048") &&
        appliedSave.feedback.includes("需重新安装") &&
        fixture.state.config.scan_time === "09:15" && fixture.state.config.min_kb === 2048,
      JSON.stringify(appliedSave).slice(0, 120));

    // 恢复默认：只填入输入框（夹具 defaults），不触发 PUT；生效值不变。
    const putCountBeforeReset = fixture.state.counts.configPut || 0;
    await page.click("#btn-config-reset");
    const resetValues = await page.evaluate(() => ({
      root: document.getElementById("cfg-scan-root").value,
      time: document.getElementById("cfg-scan-time").value,
      min: document.getElementById("cfg-min-kb").value,
      free: document.getElementById("cfg-free-alert-gb").value,
      effective: document.getElementById("config-effective").textContent,
    }));
    record("settings-restore-default-fills-without-saving",
      resetValues.time === "12:00" && resetValues.min === "10240" &&
        resetValues.free === "10" && resetValues.root === "/fixture/home" &&
        resetValues.effective.includes("09:15") &&
        (fixture.state.counts.configPut || 0) === putCountBeforeReset,
      JSON.stringify(resetValues).slice(0, 120));

    /* ---------- 状态语义矩阵 ---------- */
    await setMode("onlyadded");
    await openPage("#/overview");
    await waitForText(page, "#overview-summary", "不能判断为“无变化”");
    const onlyAdded = await page.locator("#overview-summary").textContent();
    record("only-added-is-not-no-change",
      onlyAdded.includes("新增或未记录") && !onlyAdded.includes("期间没有 ≥1MB"));

    await setMode("empty");
    await openPage("#/overview");
    await waitForText(page, "#overview-summary", "还不能比较");
    record("empty-overview-is-baseline-state",
      (await page.locator("#card-latest").textContent()).includes("尚无快照") &&
        !(await page.locator("#overview-summary").textContent()).includes("加载失败"));
    await openPage("#/changes");
    await waitForText(page, "#diff-status", "尚无快照");
    const empty = await page.evaluate(() => ({
      options: document.querySelectorAll("#sel-a option").length,
      disabled: document.querySelector("#btn-diff").disabled,
    }));
    record("empty-changes-clears-stale-results", empty.options === 0 && empty.disabled, JSON.stringify(empty));

    await setMode("single");
    await openPage("#/overview");
    await waitForText(page, "#overview-summary", "还不能比较");
    await openPage("#/changes");
    await waitForText(page, "#diff-status", "基线已建立");
    record("single-snapshot-enables-distribution-not-diff",
      await page.locator("#btn-diff").isDisabled());
    await openPage("#/browse");
    await page.waitForSelector("#tbl-browse tbody tr");
    record("single-snapshot-distribution-loads",
      (await page.locator("#tbl-browse").textContent()).includes("Archive") &&
        (await page.locator("#tbl-browse").textContent()).includes("—"));

    await setMode("error500");
    await openPage("#/overview");
    await waitForText(page, "#overview-summary", "HTTP 500");
    const error500 = await page.locator("#overview-summary").textContent();
    record("http-500-is-not-first-scan-or-no-change",
      error500.includes("加载失败") && !error500.includes("需要至少两个快照") &&
        !error500.includes("期间没有 ≥1MB"), error500);

    await setMode("snapshots500");
    await openPage("#/changes");
    await waitForText(page, "#diff-status", "HTTP 500");
    record("snapshot-500-disables-stale-comparison",
      await page.locator("#btn-diff").isDisabled());

    await setMode("dual");
    await page.route("**/api/diff*", (route) => route.abort("internetdisconnected"));
    await openPage("#/overview");
    await waitForText(page, "#overview-summary", "无法连接本地服务");
    record("network-error-is-explicit",
      !(await page.locator("#overview-summary").textContent()).includes("需要至少两个快照"));
    await page.unroute("**/api/diff*");

    /* ---------- 乱序响应：旧成功晚于新失败/新结果，只允许显示当前状态 ---------- */
    // 总览摘要：首访成功被延迟，随后一次 500 先到；迟到成功不得覆盖较新错误。
    await setScenario("overview-old-success-new-500");
    await openPage("#/changes");
    await openPage("#/overview", "domcontentloaded");  // 摘要请求1（慢成功）
    await waitForCount("overviewDiff", 1);
    await page.click('a[data-page="changes"]');
    await page.waitForURL("**/#/changes");
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");  // 摘要请求2（立即 500）
    await waitForText(page, "#overview-summary", "较新的合成差分故障");
    await page.waitForTimeout(1400);
    record("late-overview-success-cannot-overwrite-newer-error",
      (await page.locator("#overview-summary").textContent()).includes("较新的合成差分故障"));

    // 报告列表同样覆盖“旧成功晚于新 500”。
    await setScenario("report-old-success-new-500");
    await openPage("#/overview");
    await openPage("#/changes", "domcontentloaded");  // 报告请求1（慢成功）
    await waitForCount("reports", 1);
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.click('a[data-page="changes"]');
    await page.waitForURL("**/#/changes");  // 报告请求2（立即 500）
    await waitForText(page, "#report-list", "较新的合成报告故障");
    await page.waitForTimeout(1400);
    record("late-report-success-cannot-overwrite-newer-error",
      (await page.locator("#report-list").textContent()).includes("较新的合成报告故障"));

    // 树与目录浏览器各自使用独立 generation；旧响应比新响应晚到仍被丢弃。
    await setScenario("tree-browse-race");
    await openPage("#/overview");
    await openPage("#/browse", "domcontentloaded");  // 树/浏览器请求1（慢）
    await waitForCount("trees", 1);
    await waitForCount("browse", 1);
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.click('a[data-page="browse"]');
    await page.waitForURL("**/#/browse");  // 请求2（立即 NewTree/NewChild）
    await waitForText(page, "#tbl-browse", "NewChild");
    await page.waitForTimeout(1000);
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

    // 快速切目录：旧目录响应后到，表格只保留当前目录。
    await setScenario("browse-switch-race");
    await page.waitForSelector("#tbl-browse .dir-name");
    await page.click("#tbl-browse .dir-name");  // 下钻 Archive（慢响应 SlowOldDir）
    await waitForCount("browse", 1);
    await page.click("#crumbs .crumb");  // 面包屑回根（快响应 FastNewDir）
    await waitForText(page, "#tbl-browse", "FastNewDir");
    await page.waitForTimeout(1200);
    const switched = await page.evaluate(() => ({
      table: document.querySelector("#tbl-browse").textContent,
      currentCrumb: document.querySelector("#crumbs .crumb.current")?.textContent,
    }));
    record("rapid-dir-switch-shows-current-selection",
      switched.table.includes("FastNewDir") && !switched.table.includes("SlowOldDir") &&
        switched.currentCrumb === "root", JSON.stringify(switched));

    // 离开页面后，延迟的目录响应不得再改写隐藏页 DOM。
    await setScenario("browse-navigation-delay");
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.click('a[data-page="browse"]');
    await page.waitForURL("**/#/browse");  // 目录请求（慢）
    await waitForCount("browse", 1);
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");  // 响应未到先离开
    await page.waitForTimeout(800);
    record("navigation-invalidates-late-browse-write",
      !(await page.locator("#tbl-browse").textContent()).includes("OldAfterNavigation"));

    // snapshots 等待期间用户主动改选 [2,1]，响应不得用请求前 [1,2] 覆盖。
    await openPage("#/changes");
    await page.waitForFunction(() => document.querySelectorAll("#sel-b option").length === 2);
    await setScenario("snapshots-delay");  // 计数清零：下一次导航触发的快照请求才是慢响应
    await page.click('a[data-page="overview"]');
    await page.waitForURL("**/#/overview");
    await page.click('a[data-page="changes"]');
    await page.waitForURL("**/#/changes");  // 快照请求（慢）
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
    await setScenario(null);
    await openPage("#/changes");
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

    /* ---------- 轮询生命周期：切页不累积，扫描结束即停 ---------- */
    await setMode("scanning-stuck");
    await openPage("#/browse");
    await waitForText(page, "#scan-badge", "扫描进行中");
    const statusBaseline = (await fixtureState()).counts.status || 0;
    const pollStart = Date.now();
    while (Date.now() - pollStart < 11200) {  // 覆盖 ≥2 个 5s 轮询周期
      await page.click('a[data-page="overview"]');
      await page.click('a[data-page="changes"]');
      await page.click('a[data-page="browse"]');
      await page.waitForTimeout(300);
    }
    const statusDuring = (await fixtureState()).counts.status - statusBaseline;
    // 11.2s 内：初始加载 1 次 + 5s/10s 轮询 2 次；并行重复链会 ≥4
    record("page-switches-do-not-duplicate-scan-poll",
      statusDuring >= 2 && statusDuring <= 3, `status calls=${statusDuring} in 11.2s`);

    await setMode("dual");  // 扫描结束：下一次轮询观测到空闲后必须停止
    await waitForText(page, "#scan-badge", "未手动扫描过");
    const settleStart = Date.now();
    while (Date.now() - settleStart < 7000) await page.waitForTimeout(500);
    const statusAfter = (await fixtureState()).counts.status;
    record("scan-poll-stops-after-settle", statusAfter === 1, `status calls=${statusAfter} after settle`);

    /* ---------- 行操作可访问名 ---------- */
    await setMode("addedremoved");
    await openPage("#/changes");
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

    /* ---------- 净变化口径：根同口径差分，非行求和（ISS-028 修复） ---------- */
    // 父子重叠：父 +100 KiB 与子 +33/+33 同时入选（DEC-005），行求和 = +166；
    // 根总量差 = +100。净变化必须等于根差分 +100.0 KB，并与“根目录 X → Y”同源一致
    //（300000→300100 KiB 显示 293.0 MB → 293.1 MB，差值即 +100 KiB）。
    await setMode("dual");
    await setScenario("net-overlap");
    await openPage("#/changes");
    await page.waitForSelector("#changes-net:not([hidden])");
    const netOverlap = await page.evaluate(() => ({
      strong: document.querySelector("#changes-net strong")?.textContent || "",
      line: document.getElementById("changes-net").textContent,
    }));
    record("changes-net-is-root-diff-not-row-sum",
      netOverlap.strong === "+100.0 KB" &&
        netOverlap.line.includes("根同口径差分") &&
        netOverlap.line.includes("根目录 293.0 MB → 293.1 MB") &&
        !netOverlap.line.includes("166"),
      JSON.stringify(netOverlap));
    // 无基线：a/b 缺 total_kb 时净变化显示“无基线”，不得显示 0 或回退行求和
    //（夹具行和 = +2048 KiB，若回退会显示 +2.0 MB）。
    await setScenario("net-nobaseline");
    await openPage("#/changes");
    await page.waitForSelector("#changes-net:not([hidden])");
    const netNoBaseline = await page.evaluate(() => ({
      strong: document.querySelector("#changes-net strong")?.textContent || "",
      line: document.getElementById("changes-net").textContent,
    }));
    record("changes-net-no-baseline-not-zero-or-sum",
      netNoBaseline.strong === "无基线" &&
        !netNoBaseline.line.includes("2.0 MB") && !netNoBaseline.line.includes("+0.0 B"),
      JSON.stringify(netNoBaseline));
    await setScenario(null);

    /* ---------- Tauri 桥（注入 mock 桥验证有桥路径；真实壳运行见 RESULT 未验证项） ---------- */
    const tpage = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tauriErrors = [];
    tpage.on("pageerror", (e) => tauriErrors.push(e.message));
    await tpage.addInitScript(`
      window.__tauriMock = { invokes: [], handlers: {} };
      Object.defineProperty(window, "__TAURI__", { value: {
        core: { invoke: (cmd, args) => {
          window.__tauriMock.invokes.push({ cmd, args });
          return Promise.resolve();
        } },
        event: { listen: (name, handler) => {
          window.__tauriMock.handlers[name] = handler;
          return Promise.resolve(0);
        } },
      }, configurable: true });
    `);
    await tpage.goto(`${base}/#/overview`, { waitUntil: "networkidle" });
    await tpage.waitForFunction(() => (window.__tauriMock.invokes || []).length >= 1);
    const trayFirst = await tpage.evaluate(() => window.__tauriMock.invokes[0]);
    record("tauri-bridge-pushes-tray-status",
      trayFirst?.cmd === "update_tray_status" && trayFirst?.args?.title === "256 GB" &&
        /快照 2 个/.test(trayFirst?.args?.tooltip || ""), JSON.stringify(trayFirst));
    record("tauri-bridge-registers-tray-action-listener",
      (await tpage.evaluate(() => typeof window.__tauriMock.handlers["tray-action"])) === "function");
    await tpage.evaluate(() => window.__tauriMock.handlers["tray-action"]({ payload: "scan" }));
    await waitForCount("scan", 1);
    await waitForText(tpage, "#scan-badge", "上次扫描 2026-09-13 12:03");
    const trayInvokes = await tpage.evaluate(() => window.__tauriMock.invokes.length);
    record("tauri-tray-action-triggers-scan-and-refresh",
      trayInvokes >= 2 && fixture.state.version === 3, `invokes=${trayInvokes}`);
    await tpage.close();

    /* ---------- ISS-028 三条关键旅程 ---------- */

    // 旅程 1：首次启动（空库 → 基线已建立）
    await setMode("empty");
    await openPage("#/overview");
    const firstLaunch = await page.evaluate(() => ({
      latest: document.getElementById("card-latest").textContent,
      quality: document.getElementById("overview-quality").textContent,
      summary: document.getElementById("overview-summary").textContent,
    }));
    record("journey-first-launch-quality-line",
      firstLaunch.latest.includes("尚无快照") && firstLaunch.quality.includes("尚无快照") &&
        !firstLaunch.summary.includes("加载失败"),
      JSON.stringify({ latest: firstLaunch.latest.slice(0, 40) }));
    await setMode("single");
    await openPage("#/overview");
    await page.waitForFunction(() =>
      document.getElementById("overview-quality").textContent.includes("基线"));
    const baselineReady = await page.evaluate(() => ({
      quality: document.getElementById("overview-quality").textContent,
      summary: document.getElementById("overview-summary").textContent,
      scanNote: document.getElementById("overview-scan-note").textContent,
    }));
    record("journey-first-launch-baseline-ready",
      baselineReady.quality.includes("基线") && baselineReady.scanNote.includes("最近扫描"),
      baselineReady.quality.slice(0, 80));
    await openPage("#/browse");
    await page.waitForSelector("#tbl-browse tbody tr");
    const browseFirstLaunch = await page.locator("#tbl-browse").textContent();
    record("journey-first-launch-distribution-loads",
      browseFirstLaunch.includes("Archive") && browseFirstLaunch.includes("—"),
      browseFirstLaunch.slice(0, 80));

    // 旅程 2：日常定位（总览→变化→详情→证据）
    await setMode("dual");
    await openPage("#/overview");
    await page.waitForSelector("#overview-summary table");
    await page.click('a[data-page="changes"]');
    await page.waitForURL("**/#/changes");
    await page.waitForSelector("#changes-body tr.focusable");
    // 行可 Tab 聚焦
    await page.focus("#changes-body tr.focusable");
    const focusableRow = await page.evaluate(() => ({
      active: document.activeElement?.dataset?.path || "",
      tabindex: document.activeElement?.tabIndex,
      role: document.activeElement?.getAttribute("role"),
    }));
    record("journey-daily-row-focusable",
      Boolean(focusableRow.active) && focusableRow.tabindex === 0 &&
        focusableRow.role === "button", JSON.stringify(focusableRow));
    // Enter 打开详情侧栏
    await page.keyboard.press("Enter");
    await page.waitForSelector("#changes-detail:not([hidden])");
    const detail = await page.evaluate(() => ({
      title: document.querySelector("#changes-detail h2")?.textContent,
      path: document.querySelector("#changes-detail .detail-path")?.textContent,
      closeBtn: document.querySelector("#changes-detail .detail-close")?.getAttribute("aria-label"),
    }));
    record("journey-daily-detail-opens",
      detail.title === "目录详情" && detail.path && detail.closeBtn === "关闭详情",
      JSON.stringify(detail));
    // Esc 关闭，焦点返回触发行
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.getElementById("changes-detail").hidden);
    const escFocus = await page.evaluate(() => ({
      hidden: document.getElementById("changes-detail").hidden,
      focused: document.activeElement?.dataset?.path || "",
    }));
    record("journey-daily-esc-returns-focus",
      escFocus.hidden && escFocus.focused.length > 0, JSON.stringify(escFocus));

    // 旅程 3：失败恢复（离线 → 重连）
    await setMode("dual");
    await page.route("**/api/snapshots", (route) => route.abort("internetdisconnected"));
    await openPage("#/overview");
    await waitForText(page, "#overview-quality", "无法连接本地服务");
    const offlineShown = await page.locator("#overview-quality").textContent();
    record("journey-recovery-offline-explicit",
      offlineShown.includes("无法连接本地服务") && !offlineShown.includes("加载失败"),
      offlineShown.slice(0, 60));
    await page.unroute("**/api/snapshots");
    await openPage("#/overview");
    await page.waitForSelector("#overview-quality .path-mono",
      { timeout: 10000 });
    const recovered = await page.locator("#overview-quality").textContent();
    record("journey-recovery-restored-after-reconnect",
      recovered.includes("/fixture/root") && !recovered.includes("无法连接"),
      recovered.slice(0, 60));

    /* ---------- 全状态矩阵扩展（ISS-028 m1/m3） ---------- */

    // 部分覆盖：denied_count > 0、collection_status=partial
    await setMode("partial");
    await openPage("#/overview");
    await waitForText(page, "#overview-quality", "部分目录未读取");
    const partialQuality = await page.locator("#overview-quality").textContent();
    record("state-matrix-partial-quality-shown",
      partialQuality.includes("部分目录未读取") && partialQuality.includes("6 个"),
      partialQuality.slice(0, 80));
    await waitForText(page, "#overview-scan-note", "读取受限");
    const partialScanNote = await page.locator("#overview-scan-note").textContent();
    record("state-matrix-partial-scan-note-shown",
      partialScanNote.includes("读取受限") && partialScanNote.includes("6 个目录读取受限"),
      partialScanNote.slice(0, 80));

    // 首扫无日报：单快照 + 报告为空
    await setMode("single");
    await openPage("#/changes");
    await waitForText(page, "#diff-status", "基线已建立");
    await openPage("#/overview");
    await page.waitForSelector("#overview-summary");
    const singleSummary = await page.locator("#overview-summary").textContent();
    record("state-matrix-first-scan-no-report",
      singleSummary.includes("还不能比较") && !singleSummary.includes("加载失败"),
      singleSummary.slice(0, 80));

    // 截断：trees 返回 truncated=true
    await setMode("dual");
    await setScenario("trees-truncated");
    await openPage("#/browse");
    // 验证 trees 元数据被前端保留（chart 仍显示，table 仍可读）
    await page.waitForSelector("#chart-sunburst canvas");
    const treeTruncated = await page.evaluate(() => ({
      hasCanvas: Boolean(document.querySelector("#chart-sunburst canvas")),
      hasTableRows: document.querySelectorAll("#tbl-browse tbody tr").length,
    }));
    record("state-matrix-tree-truncated-renders-both",
      treeTruncated.hasCanvas && treeTruncated.hasTableRows > 0,
      JSON.stringify(treeTruncated));
    await setScenario(null);

    /* ---------- 长路径可复制（DESIGN 关键可达性） ---------- */
    await setMode("dual");
    await openPage("#/changes");
    await page.waitForSelector("#changes-body tr.focusable");
    const copyBtn = await page.evaluate(() => {
      const b = document.querySelector("#changes-body [data-copy]");
      return { exists: Boolean(b), label: b?.getAttribute("aria-label") || "" };
    });
    record("changes-copy-path-button-available",
      copyBtn.exists && copyBtn.label.startsWith("复制路径"),
      JSON.stringify(copyBtn));
    // 模拟复制并观察按钮反馈
    await page.evaluate(() => {
      const b = document.querySelector("#changes-body [data-copy]");
      b.click();
    });
    await page.waitForFunction(() => {
      const b = document.querySelector("#changes-body [data-copy]");
      return b && (b.textContent === "已复制" || b.textContent === "复制失败");
    });
    const copyResult = await page.evaluate(() =>
      document.querySelector("#changes-body [data-copy]")?.textContent);
    record("changes-copy-path-feedback-rendered",
      copyResult === "已复制" || copyResult === "复制失败",
      copyResult);
    // 分布页同样有复制按钮
    await openPage("#/browse");
    await page.waitForSelector("#tbl-browse tbody tr");
    const browseCopyBtn = await page.evaluate(() => {
      const b = document.querySelector("#tbl-browse [data-copy]");
      return { exists: Boolean(b), path: b?.dataset?.copy || "" };
    });
    record("browse-copy-path-button-available",
      browseCopyBtn.exists && browseCopyBtn.path.startsWith("/"),
      JSON.stringify(browseCopyBtn));

    /* ---------- 图表与表格等价（DESIGN：图表必须伴随表格替代） ---------- */
    await openPage("#/overview");
    await page.waitForSelector("#chart-volume canvas");
    // 展开"以表格查看"折叠块（<details>/<summary>）
    await page.evaluate(() => {
      const det = document.querySelector(".tbl-toggle");
      if (det && !det.open) det.open = true;
    });
    await page.waitForTimeout(120);
    const volumeEq = await page.evaluate(() => ({
      canvas: Boolean(document.querySelector("#chart-volume canvas")),
      tableRows: document.querySelectorAll("#overview-volume-table tbody tr").length,
    }));
    record("overview-volume-chart-and-table-coexist",
      volumeEq.canvas && volumeEq.tableRows >= 2,
      JSON.stringify(volumeEq));
    // 分布页：sunburst + 浏览器表格同时存在
    await openPage("#/browse");
    await page.waitForSelector("#chart-sunburst canvas");
    await page.waitForSelector("#tbl-browse tbody tr");
    const browseEq = await page.evaluate(() => ({
      canvas: Boolean(document.querySelector("#chart-sunburst canvas")),
      tableRows: document.querySelectorAll("#tbl-browse tbody tr").length,
      trend: Boolean(document.querySelector("#chart-browser-trend canvas")),
    }));
    record("browse-chart-and-table-coexist",
      browseEq.canvas && browseEq.tableRows > 0 && browseEq.trend,
      JSON.stringify(browseEq));

    /* ---------- 设置页扫描运行历史（ISS-028 m3） ---------- */
    await openPage("#/settings");
    await page.waitForSelector("#scan-history table, #scan-history .hint");
    const settingsHistory = await page.locator("#scan-history").textContent();
    record("settings-scan-history-rendered",
      settingsHistory.includes("2026-09-12") && settingsHistory.includes("done"),
      settingsHistory.slice(0, 80));

    /* ---------- 三种视口截图（DESIGN：980×640 / 1220×820 / 1920×1080） ---------- */
    const viewportScreens = [];
    for (const v of [
      { w: 960, h: 640, name: "overview-960x640" },
      { w: 1220, h: 820, name: "changes-1220x820" },
      { w: 1920, h: 1080, name: "browse-1920x1080" },
    ]) {
      await page.setViewportSize({ width: v.w, height: v.h });
      const targetHash = v.name.startsWith("changes") ? "#/changes"
        : v.name.startsWith("browse") ? "#/browse" : "#/overview";
      await openPage(targetHash);
      if (targetHash === "#/changes") {
        await page.waitForSelector("#changes-body tr.focusable, #changes-body tr td.hint",
          { timeout: 8000 });
      } else if (targetHash === "#/browse") {
        await page.waitForSelector("#tbl-browse tbody tr, #chart-sunburst canvas",
          { timeout: 8000 });
      } else {
        await page.waitForSelector("#overview-summary table, #overview-summary p",
          { timeout: 8000 });
      }
      const shot = path.join(evidenceDir, `${v.name}.png`);
      await page.screenshot({ path: shot });
      viewportScreens.push(shot);
      // 不允许横向滚动条
      const overflow = await page.evaluate(() => ({
        docW: document.documentElement.clientWidth,
        scrollW: document.documentElement.scrollWidth,
        bodyW: document.body.scrollWidth,
      }));
      record(`viewport-${v.w}x${v.h}-no-horizontal-overflow`,
        overflow.scrollW <= overflow.docW + 1,
        JSON.stringify(overflow));
    }

    /* ---------- ISS-002A 三类覆盖说明与系统设置深链 ----------
     * 覆盖三类缺口（denied/vanished/exclude_names）的可解释渲染；验证字段缺失
     * 时 `?? 0` 防御；mock Tauri 桥下验证深链按钮触发既有 opener 入口，浏览器
     * 环境下渲染路径文字且不伪造可点链接；重扫按钮复用既有 /api/scan 入口。 */
    await setMode("dual");  // 复位 mode，避免与既有断言交互

    // 1) 三类缺口并存：denied=6 / vanished=4 / excluded=3 各自可解释文案
    await setMode("partial-all");
    await openPage("#/overview");
    await page.waitForSelector("[data-test='coverage-classes']");
    const covAll = await page.evaluate(() => ({
      classes: document.querySelector("[data-test='coverage-classes']")?.textContent || "",
      scanNote: document.getElementById("overview-scan-note")?.textContent || "",
      // ISS-002A 追加式三类覆盖说明（独立区块 #overview-coverage-note）
      coverageNote: document.getElementById("overview-coverage-note")?.textContent || "",
    }));
    record("coverage-three-classes-rendered",
      covAll.classes.includes("权限受限") && covAll.classes.includes("6") &&
        covAll.classes.includes("扫描期间消失") && covAll.classes.includes("4") &&
        covAll.classes.includes("排除掩码") && covAll.classes.includes("3") &&
        // 文案不冒充影响/删除（合同禁止「数量=影响」「未记录=删除」表述）
        !covAll.classes.includes("数量=影响") && !covAll.classes.includes("未记录=删除") &&
        !covAll.classes.includes("数量等于影响"),
      covAll.classes.slice(0, 160));
    // 追加后的并存形态：旧串「6 个目录读取受限」仍在 #overview-scan-note（既有契约），
    // 新三类计数提示在独立区块 #overview-coverage-note 内出现。
    record("coverage-scan-note-shows-all-three-counts",
      covAll.scanNote.includes("6 个目录读取受限") &&
        covAll.coverageNote.includes("6 处权限受限") &&
        covAll.coverageNote.includes("4 处扫描期间消失") &&
        covAll.coverageNote.includes("3 项排除掩码"),
      `scan=${covAll.scanNote.slice(0, 80)} | cov=${covAll.coverageNote.slice(0, 80)}`);

    // 2) 仅 vanished（ISS-066 字段已暴露，denied=0）：只见消失 chip
    await setMode("partial-vanished");
    await openPage("#/overview");
    await page.waitForSelector("[data-test='coverage-classes']");
    const covVanished = await page.evaluate(() =>
      document.querySelector("[data-test='coverage-classes']")?.textContent || "");
    record("coverage-vanished-only-class-shown",
      covVanished.includes("扫描期间消失") && covVanished.includes("4") &&
        !covVanished.includes("权限受限") && !covVanished.includes("排除掩码"),
      covVanished.slice(0, 120));

    // 3) 仅 excluded（exclude_names 数组）：只见排除掩码 chip
    await setMode("partial-excluded");
    await openPage("#/overview");
    await page.waitForSelector("[data-test='coverage-classes']");
    const covExcluded = await page.evaluate(() =>
      document.querySelector("[data-test='coverage-classes']")?.textContent || "");
    record("coverage-excluded-only-class-shown",
      covExcluded.includes("排除掩码") && covExcluded.includes("2") &&
        !covExcluded.includes("权限受限") && !covExcluded.includes("扫描期间消失"),
      covExcluded.slice(0, 120));

    // 4) 字段缺失防御（vanished_count / exclude_names 不在快照里）：
    //    ?? 0 后 vanished/excluded chip 不出现，scan-note 与 coverage-note
    //    都不冒充这些计数。
    await setMode("partial-no-fields");
    await openPage("#/overview");
    await page.waitForSelector("[data-test='coverage-classes']");
    const covDefended = await page.evaluate(() => ({
      classes: document.querySelector("[data-test='coverage-classes']")?.textContent || "",
      scanNote: document.getElementById("overview-scan-note")?.textContent || "",
      coverageNote: document.getElementById("overview-coverage-note")?.textContent || "",
    }));
    record("coverage-vanished-and-excluded-defended-to-zero",
      covDefended.classes.includes("权限受限") && covDefended.classes.includes("6") &&
        !covDefended.classes.includes("扫描期间消失") &&
        !covDefended.classes.includes("排除掩码"),
      covDefended.classes.slice(0, 120));
    // 追加后的并存形态：旧串「6 个目录读取受限」仍在 scan-note（既有契约），
    // 新 coverage-note 只渲染权限受限 chip，不冒充消失/排除掩码计数。
    record("coverage-scan-note-defends-missing-fields",
      covDefended.scanNote.includes("6 个目录读取受限") &&
        covDefended.coverageNote.includes("权限受限") &&
        !covDefended.coverageNote.includes("消失") &&
        !covDefended.coverageNote.includes("排除掩码"),
      `scan=${covDefended.scanNote.slice(0, 80)} | cov=${covDefended.coverageNote.slice(0, 80)}`);

    // 5) full 状态：dual 模式沿用既有 quality 措辞「覆盖完整」（ISS-028 既有检查依赖），
    //    新三类明细只出现在 #overview-coverage-note；full 时该区块只显示完整覆盖 chip、
    //    无三类缺口列表。
    await setMode("dual");
    await openPage("#/overview");
    await page.waitForFunction(() =>
      document.getElementById("overview-quality")?.textContent.includes("覆盖完整"));
    const covFull = await page.evaluate(() => ({
      classes: document.querySelector("[data-test='coverage-classes']")?.textContent || "",
      quality: document.getElementById("overview-quality")?.textContent || "",
      note: document.getElementById("overview-coverage-note")?.textContent || "",
    }));
    record("coverage-full-shows-only-complete",
      covFull.quality.includes("覆盖完整") && covFull.classes === "" &&
        covFull.note.includes("完整覆盖") &&
        !covFull.quality.includes("权限受限") && !covFull.quality.includes("扫描期间消失") &&
        !covFull.note.includes("权限受限"),
      JSON.stringify({ q: covFull.quality.slice(0, 60), n: covFull.note.slice(0, 60) }));

    /* ---------- ISS-002A 设置页：浏览器降级渲染路径文字 ----------
     * 默认 page（无 Tauri 桥）下，深链按钮隐藏，回退为显示固定路径文字；
     * 验证不渲染假链接（不是 <a href="x-apple...">）且路径与 macOS 真实菜单一致。 */
    await setMode("dual");
    await openPage("#/settings");
    await page.waitForSelector("#permissions-panel");
    const permBrowser = await page.evaluate(() => ({
      openBtnVisible: !document.getElementById("btn-open-system-prefs")?.hidden,
      fallbackVisible: !document.querySelector("[data-test='perm-path-fallback']")?.hidden,
      fallbackText: document.querySelector("[data-test='perm-path-fallback']")?.textContent || "",
      authChip: document.querySelector("[data-test='perm-auth-chip']")?.textContent || "",
      rescanVisible: !document.getElementById("btn-rescan")?.hidden,
      // 不得存在伪造的 <a> 指向 x-apple.systempreferences
      fakeAnchorCount: [...document.querySelectorAll("#permissions-panel a")].filter((a) =>
        a.getAttribute("href")?.startsWith("x-apple.systempreferences")).length,
    }));
    record("permissions-browser-fallback-shows-path-text",
      !permBrowser.openBtnVisible && permBrowser.fallbackVisible &&
        permBrowser.fallbackText.includes("系统设置") &&
        permBrowser.fallbackText.includes("隐私与安全性") &&
        permBrowser.fallbackText.includes("完全磁盘访问") &&
        permBrowser.fakeAnchorCount === 0,
      JSON.stringify(permBrowser).slice(0, 160));

    /* ---------- ISS-002A 设置页：重扫按钮复用既有 /api/scan 入口 ----------
     * 点击后必须真实 POST /api/scan（fixture 计数 +1），并显示反馈；
     * 验证未新造轮询：扫描进度的刷新仍由既有 status.js 的轮询链负责。 */
    const scanCallsBefore = fixture.state.counts.scan || 0;
    await page.click("#btn-rescan");
    await waitForCount("scan", scanCallsBefore + 1);
    const rescanFeedback = await page.evaluate(() => ({
      statusText: document.getElementById("perm-rescan-status")?.textContent || "",
      statusVisible: !document.getElementById("perm-rescan-status")?.hidden,
    }));
    record("permissions-rescan-triggers-existing-entry",
      (fixture.state.counts.scan || 0) === scanCallsBefore + 1 &&
        rescanFeedback.statusVisible &&
        rescanFeedback.statusText.length > 0 &&
        // 立即反馈含进度提示，不冒充"完成"
        rescanFeedback.statusText.includes("扫描"),
      JSON.stringify(rescanFeedback).slice(0, 160));

    /* ---------- ISS-002A 设置页：mock Tauri 桥下深链按钮被驱动 ----------
     * 用独立的 mock-tpage 注入 __TAURI__.core.invoke，验证：
     *   - 按钮可见、fallback 隐藏；
     *   - 点击按钮后 invoke 收到 cmd="plugin:opener|open_url" 且 url 为
     *     x-apple.systempreferences:...Privacy_AllFiles。 */
    const tpage2 = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tpage2Errors = [];
    tpage2.on("pageerror", (e) => tpage2Errors.push(e.message));
    await tpage2.addInitScript(`
      window.__tauriMock2 = { invokes: [] };
      Object.defineProperty(window, "__TAURI__", { value: {
        core: { invoke: (cmd, args) => {
          window.__tauriMock2.invokes.push({ cmd, args });
          return Promise.resolve();
        } },
        event: { listen: () => Promise.resolve(0) },
      }, configurable: true });
    `);
    await setMode("partial-all");  // 三类缺口并存，便于查看授权状态 chip
    await tpage2.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    await tpage2.waitForSelector("#permissions-panel [data-test='perm-open-prefs-btn']");
    const permTauri = await tpage2.evaluate(() => ({
      openBtnVisible: !document.getElementById("btn-open-system-prefs")?.hidden,
      fallbackHidden: document.querySelector("[data-test='perm-path-fallback']")?.hidden,
      authChip: document.querySelector("[data-test='perm-auth-chip']")?.textContent || "",
      note: [...document.querySelectorAll("#permissions-panel p")]
        .map((p) => p.textContent).join(" | "),
    }));
    record("permissions-tauri-mock-shows-button-and-warning",
      permTauri.openBtnVisible && permTauri.fallbackHidden &&
        permTauri.authChip.includes("可能未授权") &&
        permTauri.note.includes("不代改系统权限"),
      JSON.stringify(permTauri).slice(0, 200));
    await tpage2.click("#btn-open-system-prefs");
    await tpage2.waitForFunction(() => (window.__tauriMock2.invokes || []).some(
      (c) => c && c.cmd === "plugin:opener|open_url"));
    // settings 页加载时状态层会先推送 tray 状态（update_tray_status），
    // 因此不能在 invokes[0] 上断言；在全部记录中定位 opener 调用。
    const deeplinkInvoke = await tpage2.evaluate(() => (window.__tauriMock2.invokes || [])
      .find((c) => c && c.cmd === "plugin:opener|open_url"));
    record("permissions-tauri-mock-deeplink-invokes-opener",
      deeplinkInvoke?.cmd === "plugin:opener|open_url" &&
        deeplinkInvoke?.args?.url === "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
      JSON.stringify(deeplinkInvoke));
    await tpage2.close();

    /* ---------- 汇总 ---------- */
    record("no-unhandled-page-errors",
      pageErrors.length === 0 && tauriErrors.length === 0,
      [...pageErrors, ...tauriErrors].join("; "));
    const failed = checks.filter((c) => !c.ok);
    process.stdout.write(JSON.stringify({
      ok: failed.length === 0,
      passed: checks.length - failed.length,
      failed: failed.length,
      evidence: [overviewShot, changesShot, browseShot, bigfilesShot, settingsShot,
        bigfilesTruncatedShot, bigfilesExpiredShot, bigfilesFailedShot,
        bigfilesPermShot, bigfilesNoMatchShot, ...viewportScreens],
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
