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
    // ISS-069：初始已有一项排除掩码（来源 settings），前端应原样渲染该值；
    // 硬编码空列表或默认掩码的回归会被前端检查捕获。
    exclude_names: ["*.noindex"],
    sources: { scan_root: "default", scan_time: "settings", min_kb: "settings", free_alert_gb: "settings", exclude_names: "settings" },
    defaults: { scan_root: "/fixture/home", scan_time: "12:00", min_kb: 10240, free_alert_gb: 10 },  // ISS-073 夹具卫生：真实 effective_settings_view().defaults 无 exclude_names 键（ISS-069 曾多写），前端 resetToDefaults 也只消费这 4 键
    policies: { keep_daily_days: 21, keep_weekly_weeks: 8, du_timeout_s: 14400, bigfile_default_days: 7, bigfile_default_mb: 100 },
    settings_path: "/fixture/runtime/settings.json",
    // ISS-016B：夹具注册计划恒 12:00；初始 scan_time 13:30 → drift。
    // PUT 后按新 scan_time 重算（见 PUT 处理器）；场景可覆盖 not_registered/unknown。
    service_reload_state: { state: "drift", registered_scan_time: "12:00", current_scan_time: "13:30" },
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
    if (state.mode === "denied-over") {
      // ISS-095：denied 是 du stderr 的受限行数、可超过目录总数——
      // 25/2 = 12.5 倍，权限卡占比文案应改用倍数表述而非「（1250.0%）」。
      return [{ ...latest, denied_count: 25, collection_status: "partial",
                dir_count: 2 }, snapshot(1, "2026-09-12T10:00:00")];
    }
    return [latest, snapshot(1, "2026-09-12T10:00:00")];
  };
  // ISS-067：/api/snapshots 的合同是**显式列清单**（不含 select * 的额外列）。
  // 后端曾因漏列 vanished_count / exclude_names，使 overview 的两类覆盖说明
  // 在生产恒为 0/空；而同形夹具掩盖了这个接缝。这里如实建模该端点：仅保留
  // 显式 SELECT 的列。若后端再次漏列，这些字段即从此响应消失，覆盖检查转红。
  const SNAPSHOTS_ENDPOINT_COLUMNS = [
    "id", "created_at", "root", "total_kb", "dir_count", "denied_count",
    "min_kb", "collection_status", "vanished_count", "exclude_names",
    "total_bytes", "free_bytes",
  ];
  const snapshotsForSnapshotsEndpoint = () => snapshots().map((s) => {
    const row = {};
    for (const key of SNAPSHOTS_ENDPOINT_COLUMNS) {
      if (key in s) row[key] = s[key];
    }
    return row;
  });
  const scanning = () => state.scanning || state.mode === "scanning-stuck" ||
    state.mode === "scanning-live";
  // ISS-090：scanning-live 模式每次 /api/status 递增 live 进度（du 无总量
  // 分母，前端合同是事实计数而非百分比；旧后端/其他模式无 live 键 → 前端
  // 必须回退「扫描进行中…」，由既有 ring 检查与新增 fallback 检查钉住）。
  let liveDirs = 0;
  const liveProgress = () => {
    liveDirs += 13;
    return {
      active: true, run_id: 2, dirs_scanned: liveDirs,
      bytes_seen_kb: liveDirs * 1024 * 7,
      started_epoch_s: 1758696000.0, elapsed_s: (liveDirs / 13) * 5,
      heartbeat_epoch_s: 1758696000.0 + (liveDirs / 13) * 5,
      stale_after_s: 30,
    };
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
        // ISS-067：/api/status 的 latest_snapshot 对应后端 `SELECT *`，是完整行；
        // /api/snapshots 则是显式列清单——两条独立合同，不能共用同一形状。
        latest_snapshot: rows[0] || null,
        disk: { total_bytes: 1024 ** 4, free_bytes: 256 * 1024 ** 3 },
        db_bytes: 4096,
        scan: {
          running: scanning(),
          started_at: scanning() ? "2026-09-13T12:02:00" : null,
          finished_at: state.version === 3 ? "2026-09-13T12:03:00" : null,
          ...(state.mode === "scanning-live" ? { live: liveProgress() } : {}),
        },
        port: server.address().port,
      });
    }
    if (url.pathname === "/api/snapshots") {
      if (state.mode === "snapshots500") return json(res, 500, { detail: "合成快照故障" });
      const rows = snapshotsForSnapshotsEndpoint();
      if (state.scenario === "snapshots-delay" && nextCall("snapshots") === 1) {
        return later(res, 200, rows, 700);
      }
      return json(res, 200, rows);
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
        const allowed = ["scan_root", "scan_time", "min_kb", "free_alert_gb", "exclude_names"];
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
        // ISS-069：镜像 fathom/config.py 的 exclude_names 合同（列表形态）；
        // 前端只做即时反馈，最终以本 400 为准。
        if (body.exclude_names != null) {
          const raw = body.exclude_names;
          if (!Array.isArray(raw)) {
            return json(res, 400, { detail: "exclude_names 必须是字符串列表或规范串" });
          }
          for (const item of raw) {
            if (typeof item !== "string") {
              return json(res, 400, { detail: `exclude_names 每项必须是字符串：${JSON.stringify(item)}` });
            }
            const trimmed = item.trim();
            if (!trimmed) return json(res, 400, { detail: "exclude_names 含空项" });
            if (trimmed.includes("/")) {
              return json(res, 400, { detail: `exclude_names 不得包含路径分隔符 '/'：${trimmed}` });
            }
            if (trimmed === "." || trimmed === "..") {
              return json(res, 400, { detail: `exclude_names 不得为 '${trimmed}'` });
            }
          }
          if (new Set(raw.map((s) => s.trim())).size > 50) {
            return json(res, 400, { detail: "exclude_names 项数不得超过 50" });
          }
        }
        nextCall("configPut");
        for (const key of allowed) {
          if (body[key] === undefined || body[key] === null) continue;
          if (key === "exclude_names") {
            // 规范串形态与后端一致：排序去重后 ';' 拼接
            const canonical = [...new Set(body[key].map((s) => s.trim()))].sort().join(";");
            state.config.exclude_names = canonical;
          } else {
            state.config[key] = body[key];
          }
        }
        // ISS-016B：镜像后端——注册时间夹具恒 12:00，按保存后的 scan_time
        // 重算漂移态（差 1 分钟也是 drift），随嵌套 config 与顶层字段一起返回。
        state.config.service_reload_state = {
          state: state.config.scan_time === "12:00" ? "in_sync" : "drift",
          registered_scan_time: "12:00",
          current_scan_time: state.config.scan_time,
        };
        return json(res, 200, {
          applied: true,
          service_reload: "requires_user_action",
          service_reload_state: state.config.service_reload_state,
          hint: "已保存到 settings.json 并在当前服务进程生效；已安装的 launchd 后台计划不受影响，需重新安装（main.py install）后才按新计划时间运行。",
          config: { ...state.config },
        });
      });
      return;
    }
    if (url.pathname === "/api/config") {
      // ISS-069：exclude-names-env 场景如实建模「生效值被环境变量钉住」——
      // 后端在 FATHOM_EXCLUDE_NAMES 存在时 sources.exclude_names 恒为 env，
      // 生效值取环境变量，PUT 只落盘 settings.json 而不改变生效值。
      if (state.scenario === "exclude-names-env") {
        return json(res, 200, {
          ...state.config,
          exclude_names: ["env_pinned"],
          sources: { ...state.config.sources, exclude_names: "env" },
        });
      }
      // ISS-016B：计划一致性另两态场景（默认 drift / PUT 联动 in_sync 已覆盖）
      if (state.scenario === "reload-not-registered") {
        return json(res, 200, { ...state.config, service_reload_state: {
          state: "not_registered", registered_scan_time: null,
          current_scan_time: state.config.scan_time,
        } });
      }
      if (state.scenario === "reload-unknown") {
        return json(res, 200, { ...state.config, service_reload_state: {
          state: "unknown", registered_scan_time: null,
          current_scan_time: state.config.scan_time,
        } });
      }
      return json(res, 200, { ...state.config });
    }
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
    }));
    record("same-day-rescan-reconciles-ids",
      after.options.join(",") === "3,1" && after.selected.join(",") === "1,3" &&
        after.status.includes("已更新或不再可用"), JSON.stringify(after));
    /* ISS-094：增长/缩减图表移入独立分区 tab（默认比对明细，图表隐藏
     * 初始化记 stale）——真实点击 tab 激活分区后图表画布必须恢复非零尺寸
     * （tabs.js 的 resumeChartsIn），两个图表分区各验一次后回默认 tab。 */
    const tabCanvases = {};
    for (const tab of ["grown", "shrunk"]) {
      await page.click(`#page-changes .page-tab[data-tab="${tab}"]`);
      await page.waitForFunction((tab) => {
        const c = document.querySelector(`#page-changes .page-tab-section[data-tab="${tab}"] canvas`);
        return c && c.width > 0 && c.height > 0;
      }, tab, { timeout: 5000 });
      tabCanvases[tab] = true;
    }
    await page.click('#page-changes .page-tab[data-tab="detail"]');
    record("diff-and-report-refresh-after-scan",
      fixture.state.lastDiff?.join(",") === "1,3" && fixture.state.staleDiffs === 0 &&
        after.report.includes("2026-09-13") && tabCanvases.grown && tabCanvases.shrunk);
    const changesShot = path.join(evidenceDir, "changes-after-rescan-1220x820.png");
    await page.screenshot({ path: changesShot });

    /* ---------- 分布页 + 零差值渲染 ---------- */
    await page.click('a[data-page="browse"]');
    await page.waitForURL("**/#/browse");
    await page.waitForSelector("#chart-sunburst canvas");  // 默认分区=占用分布
    // ISS-094：目录浏览器已分区 tab 化——真实点击切到该分区再断言表格。
    await page.click('#page-browse .page-tab[data-tab="browser"]');
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

    /* ---------- ISS-087 设置页 IA：左导航 + 右 section ---------- */
    // 默认 = 监控：监控区可见，其余三区隐藏；nav 4 项与 aria-current 唯一。
    const navInitial = await page.evaluate(() => {
      const buttons = [...document.querySelectorAll(".settings-nav-item")];
      return {
        labels: buttons.map((b) => b.dataset.section),
        active: buttons.find((b) => b.getAttribute("aria-current") === "true")?.dataset.section || null,
        visible: [...document.querySelectorAll(".settings-section")]
          .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.section),
      };
    });
    record("settings-ia-nav-4-sections-default-monitoring",
      JSON.stringify(navInitial.labels) === JSON.stringify(["monitoring", "schedule", "advanced", "about"]) &&
        navInitial.active === "monitoring" &&
        JSON.stringify(navInitial.visible) === JSON.stringify(["monitoring"]),
      JSON.stringify(navInitial));

    // 键盘可达：方向键 ↑/↓ 在 nav 项之间循环切换；Enter 激活 section。
    await page.focus('[data-section="monitoring"]');
    await page.keyboard.press("ArrowDown");
    const afterArrowDown = await page.evaluate(() =>
      document.activeElement?.dataset.section || null);
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    const afterWrap = await page.evaluate(() =>
      document.activeElement?.dataset.section || null);
    await page.keyboard.press("ArrowUp");
    const afterUp = await page.evaluate(() =>
      document.activeElement?.dataset.section || null);
    record("settings-ia-nav-arrow-keys-cycle",
      afterArrowDown === "schedule" && afterWrap === "about" && afterUp === "advanced",
      JSON.stringify({ afterArrowDown, afterWrap, afterUp }));
    // Enter 激活：focus 在 advanced，Enter 应让 advanced section 可见
    await page.keyboard.press("Enter");
    const enterActivate = await page.evaluate(() => {
      const visible = [...document.querySelectorAll(".settings-section")]
        .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.section);
      const active = [...document.querySelectorAll(".settings-nav-item")]
        .find((b) => b.getAttribute("aria-current") === "true")?.dataset.section;
      return { visible, active };
    });
    record("settings-ia-nav-enter-activates-section",
      JSON.stringify(enterActivate.visible) === JSON.stringify(["advanced"]) &&
        enterActivate.active === "advanced",
      JSON.stringify(enterActivate));
    // URL hash 持久化：进入关于区后 location.hash 应含 #settings/about
    // nav 用 position:sticky 浮在 page-container 顶部，但 Enter 触发激活后
    // page-container 滚动到底（advanced 区折叠 summary 默认 closed、内容多），
    // 此时 Playwright 的可见性闸门会把 nav 判为不可见。这里改用 evaluate 直接
    // 调用 click() 绕过闸门（用户真实交互中 nav 永远在 sticky 顶部可见）。
    await page.evaluate(() => {
      document.querySelector('.settings-nav-item[data-section="about"]')?.click();
    });
    const hashAfterAbout = await page.evaluate(() => location.hash);
    record("settings-ia-hash-persist-on-about-section",
      hashAfterAbout === "#settings/about", hashAfterAbout);
    // 回到监控区作后续用例
    await page.evaluate(() => {
      document.querySelector('.settings-nav-item[data-section="monitoring"]')?.click();
    });

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
    // ISS-087：旧 settings-page-renders-config 验证 settings-table 含「监控根目录」
    // 与「服务地址」——它们都在「高级与诊断」section；切到 advanced 再断言。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="advanced"]')?.click());
    await page.waitForSelector("#settings-table tbody tr");
    const settingsText = await page.locator("#page-settings").textContent();
    record("settings-page-renders-config",
      settingsText.includes("监控根目录") && settingsText.includes(ROOT) &&
        settingsText.includes("服务地址"), settingsText.slice(0, 60));
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="monitoring"]')?.click());  // 回到监控供后续用例
    const settingsShot = path.join(evidenceDir, "settings-config-1220x820.png");
    await page.screenshot({ path: settingsShot });

    /* ---------- 关于区渲染（ISS-087）：brandBasin + 元信息 + 关于面板 + 检查更新区 ---------- */
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="about"]')?.click());
    await page.waitForSelector('[data-test="about-panel"]');
    const aboutText = await page.locator('[data-test="about-panel"]').textContent();
    const aboutHasBrandBasin = await page.evaluate(() => {
      const mark = document.querySelector(".about-mark");
      return Boolean(mark && mark.querySelector("svg") && mark.querySelector(".bv-l1"));
    });
    record("settings-about-shows-brand-basin-and-meta",
      aboutText.includes("Fathom") &&
        aboutText.includes("Apache-2.0") &&
        aboutText.includes("cat-xierluo/Fathom") &&
        aboutText.includes("Copyright 2026 maoking") &&
        aboutHasBrandBasin,
      aboutText.slice(0, 160));
    // 关于区应包含检查更新面板（来自 settings.js 的 updater 面板，挂 #settings-about-extra）
    const aboutHasUpdater = await page.evaluate(() =>
      Boolean(document.getElementById("updater-panel")));
    record("settings-about-mounts-updater-panel",
      aboutHasUpdater, `updater=${aboutHasUpdater}`);
    const aboutShot = path.join(evidenceDir, "settings-about-1220x820.png");
    await page.screenshot({ path: aboutShot });

    // 高级与诊断区：ISS-083 折叠区与服务管理面板挂入；浏览器态全量渲染。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="advanced"]')?.click());
    await page.waitForSelector("#settings-section-advanced #settings-table");
    const advancedText = await page.locator("#settings-section-advanced").textContent();
    record("settings-advanced-section-has-runinfo-and-service",
      advancedText.includes("运行信息") && advancedText.includes("服务管理") &&
        advancedText.includes("监控根目录") && advancedText.includes("服务地址"),
      advancedText.slice(0, 160));

    // 计划与通知区：扫描时间/低空间/扫描历史/权限/自启。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="schedule"]')?.click());
    await page.waitForSelector("#schedule-form");
    const scheduleText = await page.locator("#settings-section-schedule").textContent();
    record("settings-schedule-section-has-plan-and-history",
      scheduleText.includes("计划时间") && scheduleText.includes("低空间提醒") &&
        scheduleText.includes("扫描运行历史"),
      scheduleText.slice(0, 160));
    const scheduleShot = path.join(evidenceDir, "settings-schedule-1220x820.png");
    await page.screenshot({ path: scheduleShot });

    /* ---------- 计划与通知区独立表单保存（ISS-087） ---------- */
    // scan_time 字段归「计划与通知」section；通过 #btn-schedule-save 单独保存。
    await page.fill("#cfg-scan-time", "25:00");
    await page.click("#btn-schedule-save");
    await waitForText(page, "#config-feedback", "保存失败");
    const scheduleInvalid = await page.evaluate(() => ({
      feedback: document.getElementById("config-feedback").textContent,
      effective: document.getElementById("config-effective").textContent,
    }));
    record("settings-schedule-form-invalid-input-feedback",
      scheduleInvalid.feedback.includes("HH:MM") &&
        scheduleInvalid.effective.includes("13:30") &&
        fixture.state.config.scan_time === "13:30",
      JSON.stringify(scheduleInvalid).slice(0, 160));

    // 有效保存：扫描时间 09:15 → drift 文案 + 生效值刷新
    await page.fill("#cfg-scan-time", "09:15");
    await page.click("#btn-schedule-save");
    await waitForText(page, "#config-feedback", "需重新安装");
    const scheduleApplied = await page.evaluate(() => ({
      feedback: document.getElementById("config-feedback").textContent,
      effective: document.getElementById("config-effective").textContent,
    }));
    record("settings-schedule-form-save-applies-and-hints-reinstall",
      scheduleApplied.effective.includes("09:15") &&
        scheduleApplied.feedback.includes("需重新安装") &&
        fixture.state.config.scan_time === "09:15",
      JSON.stringify(scheduleApplied).slice(0, 160));

    // 监控表单（#config-form）独立保存：min_kb 2048。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="monitoring"]')?.click());
    await page.waitForSelector("#config-form");
    const putBefore = fixture.state.counts.configPut || 0;
    await page.fill("#cfg-min-kb", "2048");
    await page.click("#btn-config-save");
    // 等待 PUT 真正落盘（仅看 feedback 字符串可能被上一个保存的提示命中）：
    // 用 fixture 计数 +1 与 effective 文本包含 "2048" 双信号。
    await waitForCount("configPut", putBefore + 1);
    await waitForText(page, "#config-effective", "2048");
    const monitorApplied = await page.evaluate(() => ({
      feedback: document.getElementById("config-feedback").textContent,
      effective: document.getElementById("config-effective").textContent,
    }));
    record("settings-monitor-form-save-applies-and-hints-reinstall",
      monitorApplied.effective.includes("2048") &&
        monitorApplied.feedback.includes("需重新安装") &&
        fixture.state.config.min_kb === 2048 &&
        (fixture.state.counts.configPut || 0) === putBefore + 1,
      JSON.stringify(monitorApplied).slice(0, 200));

    // 恢复默认：仍由监控区 #btn-config-reset 触发，4 个字段都被填入默认值。
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

    /* ---------- ISS-069 设置页排除列表编辑器 ---------- */
    await openPage("#/settings");
    await page.waitForSelector("#exclude-panel [data-test='exclude-row']");
    const exclInitial = await page.evaluate(() => ({
      rows: [...document.querySelectorAll("#exclude-panel [data-test='exclude-row']")]
        .map((r) => r.getAttribute("data-mask")),
      warning: document.querySelector("#exclude-panel [data-test='exclude-dataset-warning']")?.textContent || "",
      confirmRequired: !!document.querySelector("#exclude-panel #exclude-confirm"),
      unbounded: document.body.textContent.includes("&lt;"),
    }));
    record("exclude-editor-renders-effective-masks",
      exclInitial.rows.length === 1 && exclInitial.rows[0] === "*.noindex" &&
        exclInitial.warning.includes("新数据集") && exclInitial.confirmRequired,
      JSON.stringify(exclInitial).slice(0, 200));

    // 非法输入即时反馈：'/'、'.'、'..'、空项；且不发起 PUT
    const putBeforeInvalid = fixture.state.counts.configPut || 0;
    const invalidCases = [
      { value: "bad/name", expect: "路径分隔符" },
      { value: "..", expect: "不得为" },
      { value: ".", expect: "不得为" },
    ];
    const invalidObserved = [];
    for (const item of invalidCases) {
      await page.fill("#exclude-new-input", item.value);
      await page.click("#btn-exclude-add");
      await page.waitForTimeout(80);
      const text = await page.locator("#exclude-inline-error").textContent();
      invalidObserved.push({ value: item.value, expect: item.expect, text });
    }
    // 空项：直接点加（输入为空）
    await page.fill("#exclude-new-input", "   ");
    await page.click("#btn-exclude-add");
    await page.waitForTimeout(80);
    const emptyText = await page.locator("#exclude-inline-error").textContent();
    record("exclude-editor-rejects-illegal-masks-inline",
      invalidObserved.every((o) => o.text && o.text.includes(o.expect)) &&
        emptyText.includes("空") &&
        (fixture.state.counts.configPut || 0) === putBeforeInvalid,
      JSON.stringify({ invalidObserved, emptyText }).slice(0, 240));

    // 未勾选确认时不允许保存：给出提示且不发 PUT
    await page.fill("#exclude-new-input", "node_modules");
    await page.click("#btn-exclude-add");
    await page.waitForTimeout(80);
    const afterAdd = await page.evaluate(() => ({
      rows: [...document.querySelectorAll("#exclude-panel [data-test='exclude-row']")]
        .map((r) => r.getAttribute("data-mask")),
      inlineError: document.querySelector("#exclude-inline-error")?.textContent || "",
    }));
    const putBeforeUnconfirmed = fixture.state.counts.configPut || 0;
    await page.click("#btn-exclude-save");
    await page.waitForTimeout(120);
    const unconfirmed = await page.evaluate(() => ({
      feedback: document.querySelector("#config-feedback")?.textContent || "",
      feedbackVisible: !document.querySelector("#config-feedback")?.hidden,
      effective: document.querySelector("#config-effective")?.textContent || "",
    }));
    record("exclude-editor-blocks-save-without-confirmation",
      afterAdd.rows.includes("node_modules") && afterAdd.inlineError === "" &&
        unconfirmed.feedbackVisible && unconfirmed.feedback.includes("确认") &&
        unconfirmed.feedback.includes("新数据集") &&
        (fixture.state.counts.configPut || 0) === putBeforeUnconfirmed &&
        !(fixture.state.config.exclude_names || "").includes("node_modules"),
      JSON.stringify({ afterAdd, unconfirmed }).slice(0, 240));

    // 勾选确认后保存：走 PUT /api/config，生效值刷新且提示需重装
    await page.check("#exclude-confirm");
    await page.click("#btn-exclude-save");
    await page.waitForFunction(
      () => (document.querySelector("#config-effective")?.textContent || "").includes("node_modules"),
      null, { timeout: 5000 });
    const saved = await page.evaluate(() => ({
      feedback: document.querySelector("#config-feedback")?.textContent || "",
      effective: document.querySelector("#config-effective")?.textContent || "",
      rows: [...document.querySelectorAll("#exclude-panel [data-test='exclude-row']")]
        .map((r) => r.getAttribute("data-mask")),
    }));
    record("exclude-editor-saves-via-put-and-hints-reinstall",
      saved.feedback.includes("需重新安装") &&
        String(fixture.state.config.exclude_names).includes("node_modules") &&
        String(fixture.state.config.exclude_names).includes("*.noindex"),
      JSON.stringify(saved).slice(0, 200));

    // 保存反馈必须回显服务端 hint，而不是恒定兜底串。
    // 反例（曾被潜伏放过）：saveExcludes 把 apiPut 返回的原始 Response 当已解析
    // JSON 用，data.hint 恒为 undefined，于是永远显示硬编码兜底文案；而断言
    // "需重新安装" 恰好在兜底串里也出现，故上面那条检查无法区分两者。
    // 夹具 PUT 返回的 hint 含 "settings.json"/"launchd"/"main.py install"，
    // 这三个词只可能来自服务端响应，兜底串里没有。
    const serverHintEchoed =
      saved.feedback.includes("settings.json") &&
      saved.feedback.includes("launchd") &&
      saved.feedback.includes("main.py install");
    record("exclude-editor-echoes-server-hint",
      serverHintEchoed,
      JSON.stringify({ feedback: saved.feedback.slice(0, 200) }));

    // 删除一项并确认保存：PUT 只带 exclude_names，且不含被删掩码
    await page.click("#exclude-panel [data-test='exclude-row'][data-mask='*.noindex'] [data-test='exclude-remove']");
    await page.waitForFunction(
      () => !document.querySelector("#exclude-panel [data-test='exclude-row'][data-mask='*.noindex']"),
      null, { timeout: 5000 });
    await page.check("#exclude-confirm");
    await page.click("#btn-exclude-save");
    await page.waitForTimeout(150);
    record("exclude-editor-removes-mask",
      !String(fixture.state.config.exclude_names).includes("*.noindex") &&
        String(fixture.state.config.exclude_names).includes("node_modules"),
      String(fixture.state.config.exclude_names));

    // 一次确认只对一次保存有效：保存成功后必须复位确认勾选框，否则后续任何
    // 增删都能「沿用」上一次的勾选直接保存，显式确认形同虚设。
    // 反例（曾被潜伏放过）：exclude-confirm 只在 saveExcludes 里被读、从未被写，
    // 上面所有检查都在首次保存前才 check()，故这处缺口 81 项全绿也发现不了。
    const confirmAfterSave = await page.evaluate(() => ({
      checked: !!document.querySelector("#exclude-confirm")?.checked,
    }));
    // 不复位确认的前提下再删一项、不再勾选直接保存：若真被拦，PUT 不应发生。
    const putBeforeSecond = fixture.state.counts.configPut || 0;
    await page.click("#exclude-panel [data-test='exclude-row'][data-mask='node_modules'] [data-test='exclude-remove']");
    await page.waitForFunction(
      () => !document.querySelector("#exclude-panel [data-test='exclude-row'][data-mask='node_modules']"),
      null, { timeout: 5000 });
    await page.click("#btn-exclude-save");
    await page.waitForTimeout(150);
    const secondSave = await page.evaluate(() => ({
      feedback: document.querySelector("#config-feedback")?.textContent || "",
    }));
    record("exclude-editor-resets-confirmation-after-save",
      !confirmAfterSave.checked &&
        (fixture.state.counts.configPut || 0) === putBeforeSecond &&
        String(fixture.state.config.exclude_names).includes("node_modules") &&
        secondSave.feedback.includes("确认"),
      JSON.stringify({ confirmAfterSave, putBeforeSecond,
        putAfterSecond: fixture.state.counts.configPut || 0, secondSave }).slice(0, 240));

    /* ---------- ISS-069 续作：env 覆盖时编辑器只读且如实标注 ---------- */
    // 后端在 FATHOM_EXCLUDE_NAMES 存在时 sources.exclude_names 恒为 env，
    // PUT 只落盘 settings.json、生效值不变（已用真实后端实测确认）。前端若
    // 仍允许编辑并报「已保存（N 项）」，用户会误信改动生效而实际被静默丢弃。
    await setScenario("exclude-names-env");
    await openPage("#/settings");
    await page.waitForSelector("#exclude-list [data-test='exclude-row']");
    const envLocked = await page.evaluate(() => ({
      note: document.querySelector("#exclude-override-note")?.textContent || "",
      noteHidden: document.querySelector("#exclude-override-note")?.hidden,
      inputDisabled: document.querySelector("#exclude-new-input")?.disabled,
      addDisabled: document.querySelector("#btn-exclude-add")?.disabled,
      saveDisabled: document.querySelector("#btn-exclude-save")?.disabled,
      confirmDisabled: document.querySelector("#exclude-confirm")?.disabled,
      rendered: [...document.querySelectorAll("#exclude-list [data-test='exclude-row']")]
        .map((r) => r.getAttribute("data-mask")),
    }));
    record("exclude-editor-locks-and-labels-when-env-pinned",
      envLocked.note.includes("FATHOM_EXCLUDE_NAMES") &&
        envLocked.noteHidden === false &&
        envLocked.inputDisabled === true &&
        envLocked.addDisabled === true &&
        envLocked.saveDisabled === true &&
        envLocked.confirmDisabled === true &&
        JSON.stringify(envLocked.rendered) === JSON.stringify(["env_pinned"]),
      JSON.stringify(envLocked).slice(0, 260));
    const envPutBefore = fixture.state.counts.configPut || 0;
    // 第二道防线：即便用脚本绕过 disabled 直接点保存，也不能发出 PUT。
    await page.evaluate(() => {
      const box = document.getElementById("exclude-confirm");
      if (box) box.checked = true;
      document.getElementById("btn-exclude-save")?.click();
    });
    await page.waitForTimeout(150);
    record("exclude-editor-env-pinned-never-puts",
      (fixture.state.counts.configPut || 0) === envPutBefore,
      JSON.stringify({ before: envPutBefore, after: fixture.state.counts.configPut || 0 }));

    await setScenario(null);

    /* ---------- ISS-069 续作：保存路径自身必须拦住超限列表 ---------- */
    // addExcludeMask 的守卫只覆盖「新增」一条路径；删除/重渲染/后续批量入口
    // 都可能造出超限列表。这里直接构造 51 行再点保存：必须被前端拦下、不发
    // PUT，且给出含上限数字的说明——否则请求必然被后端 400，用户只能从服务端
    // 错误里倒推原因。
    await openPage("#/settings");
    await page.waitForSelector("#exclude-list [data-test='exclude-row']");
    await page.evaluate(() => {
      document.getElementById("btn-exclude-save")?.click();  // 清空既有 inline 错误
      const list = document.getElementById("exclude-list");
      list.innerHTML = "";
      for (let i = 0; i < 51; i += 1) {
        const row = document.createElement("li");
        row.className = "exclude-row";
        row.setAttribute("data-test", "exclude-row");
        row.setAttribute("data-mask", `cap${i}`);
        row.innerHTML = `<code>cap${i}</code>`;
        list.appendChild(row);
      }
    });
    const capPutBefore = fixture.state.counts.configPut || 0;
    await page.evaluate(() => {
      const box = document.getElementById("exclude-confirm");
      if (box) box.checked = true;
      document.getElementById("btn-exclude-save")?.click();
    });
    await page.waitForTimeout(150);
    const capState = await page.evaluate(() => ({
      inline: document.querySelector("#exclude-inline-error")?.textContent || "",
      feedback: document.querySelector("#config-feedback")?.textContent || "",
    }));
    record("exclude-editor-save-path-rejects-over-cap-list",
      (fixture.state.counts.configPut || 0) === capPutBefore &&
        capState.inline.includes("50") &&
        capState.feedback.includes("50"),
      JSON.stringify({ capPutBefore, capPutAfter: fixture.state.counts.configPut || 0,
        capState }).slice(0, 260));

    /* ---------- ISS-016B 计划一致性：漂移展示 + 只读降级（浏览器页） ---------- */
    // setMode 重置 config 为初始（13:30 / 已注册 12:00 → drift）。
    await setMode("dual");
    await openPage("#/settings");
    // ISS-087：reload-state-text 在 autostart-panel（计划与通知 section）下；
    // openPage 落默认 section=monitoring 时它不可见，先切到 schedule 再断言。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="schedule"]')?.click());
    await page.waitForSelector("[data-test='reload-state-text']");
    const reloadDrift = await page.evaluate(() => ({
      text: document.querySelector("[data-test='reload-state-text']")?.textContent || "",
      note: document.querySelector("[data-test='reload-browser-note']")?.textContent || "",
      noteHidden: document.querySelector("[data-test='reload-browser-note']")?.hidden,
      hasButton: Boolean(document.getElementById("btn-reinstall-plan")),
    }));
    record("reload-drift-shown-with-times-and-readonly-degrade",
      reloadDrift.text.includes("计划时间不一致") &&
        reloadDrift.text.includes("12:00") && reloadDrift.text.includes("13:30") &&
        reloadDrift.note.includes("桌面应用") && reloadDrift.noteHidden === false &&
        reloadDrift.hasButton === false,  // 浏览器模式无重装按钮（只读降级）
      JSON.stringify(reloadDrift).slice(0, 200));

    // 保存为已注册时间 12:00 → PUT 嵌套 config 刷新为 in_sync，降级说明消失。
    // ISS-087：scan_time 字段归「计划与通知」section，通过 #btn-schedule-save 单独保存。
    await page.fill("#cfg-scan-time", "12:00");
    await page.click("#btn-schedule-save");
    // ISS-016B repair1：等待条件必须用完整短语「计划时间一致」——
    // 「一致」是「计划时间不一致」的子串，会提前命中旧 drift 文案（断言子串陷阱）。
    await page.waitForFunction(() =>
      (document.querySelector("[data-test='reload-state-text']")?.textContent || "").includes("计划时间一致"));
    const reloadSync = await page.evaluate(() => ({
      text: document.querySelector("[data-test='reload-state-text']")?.textContent || "",
      noteExists: Boolean(document.querySelector("[data-test='reload-browser-note']")),
    }));
    record("reload-in-sync-after-saving-registered-time",
      reloadSync.text.includes("计划时间一致") && reloadSync.text.includes("12:00") &&
        reloadSync.noteExists === false &&
        fixture.state.config.scan_time === "12:00" &&
        fixture.state.config.service_reload_state?.state === "in_sync",
      JSON.stringify(reloadSync).slice(0, 160));

    // 差 1 分钟也是 drift（前端文案回到不一致）。
    await page.fill("#cfg-scan-time", "12:01");
    await page.click("#btn-schedule-save");
    await page.waitForFunction(() =>
      (document.querySelector("[data-test='reload-state-text']")?.textContent || "").includes("不一致"));
    record("reload-one-minute-drift-after-save",
      fixture.state.config.service_reload_state?.state === "drift" &&
        fixture.state.config.service_reload_state?.registered_scan_time === "12:00" &&
        fixture.state.config.service_reload_state?.current_scan_time === "12:01",
      JSON.stringify(fixture.state.config.service_reload_state));

    // not_registered / unknown 两态（场景注入 GET）。
    await setScenario("reload-not-registered");
    await openPage("#/settings");
    await waitForText(page, "[data-test='reload-state-text']", "尚未注册");
    const reloadNotReg = await page.evaluate(() =>
      document.querySelector("[data-test='reload-state-text']")?.textContent || "");
    record("reload-not-registered-state-shown",
      reloadNotReg.includes("尚未注册") && !reloadNotReg.includes("不一致"),
      reloadNotReg.slice(0, 80));
    await setScenario("reload-unknown");
    await openPage("#/settings");
    await waitForText(page, "[data-test='reload-state-text']", "无法读取");
    const reloadUnknown = await page.evaluate(() =>
      document.querySelector("[data-test='reload-state-text']")?.textContent || "");
    record("reload-unknown-state-shown",
      reloadUnknown.includes("无法读取") && reloadUnknown.includes("未知"),
      reloadUnknown.slice(0, 80));
    await setScenario(null);

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
    // ISS-093：确认按钮已移除，禁用断言落在两个 select 上，并确认按钮确实不在 DOM。
    const empty = await page.evaluate(() => ({
      options: document.querySelectorAll("#sel-a option").length,
      disabled: document.querySelector("#sel-a").disabled &&
        document.querySelector("#sel-b").disabled,
      btnGone: !document.getElementById("btn-diff"),
    }));
    record("empty-changes-clears-stale-results",
      empty.options === 0 && empty.disabled && empty.btnGone, JSON.stringify(empty));

    await setMode("single");
    await openPage("#/overview");
    await waitForText(page, "#overview-summary", "还不能比较");
    await openPage("#/changes");
    await waitForText(page, "#diff-status", "基线已建立");
    record("single-snapshot-enables-distribution-not-diff",
      await page.locator("#sel-a").isDisabled() && await page.locator("#sel-b").isDisabled());
    await openPage("#/browse");
    // ISS-094：目录浏览器在独立分区 tab——真实点击切过去再断言表格。
    await page.click('#page-browse .page-tab[data-tab="browser"]');
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
      await page.locator("#sel-a").isDisabled() && await page.locator("#sel-b").isDisabled());

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
    // ISS-094：目录浏览器在独立分区 tab——真实点击切过去再驱动表格交互。
    await setScenario("browse-switch-race");
    await page.click('#page-browse .page-tab[data-tab="browser"]');
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

    // 对比发出前后发生同日替换：改选已失效基线触发自动对比（ISS-093 无确认按钮），
    // 先收到 404，再协调快照列表并自动恢复。
    await setScenario(null);
    await openPage("#/changes");
    await page.waitForFunction(() => document.querySelector("#sel-b").value === "2");
    await setVersion(3);
    await page.selectOption("#sel-a", "2");
    await page.waitForFunction(() => document.querySelector("#sel-b").value === "3");
    await waitForText(page, "#diff-status", "已更新或不再可用");
    const recovered404 = await page.evaluate(() => ({
      selected: [document.querySelector("#sel-a").value, document.querySelector("#sel-b").value],
      status: document.querySelector("#diff-status").textContent,
    }));
    record("diff-404-coordinates-snapshot-recovery",
      recovered404.selected.join(",") === "1,3" && fixture.state.staleDiffs === 1 &&
        fixture.state.lastDiff?.join(",") === "1,3", JSON.stringify(recovered404));

    /* ---------- ISS-093：选择即比对——改选自动加载，无确认按钮 ---------- */
    // dual 夹具（快照 [2,1]）初始自动对比 #1 → #2；确认按钮从 DOM 移除。
    await setMode("dual");
    await openPage("#/changes");
    await waitForText(page, "#diff-status", "正在对比快照 #1 → #2");
    record("compare-confirm-button-removed",
      await page.evaluate(() => !document.getElementById("btn-diff")));
    // 依次改选两个 select（全程无任何按钮点击）：选齐后结果自动出现。
    await page.selectOption("#sel-a", "2");
    await page.selectOption("#sel-b", "1");
    await waitForText(page, "#diff-status", "正在对比快照 #2 → #1");
    const autoCompared = await page.evaluate(() => ({
      netVisible: !document.getElementById("changes-net").hidden,
      rows: document.querySelectorAll("#changes-body tr.focusable").length,
    }));
    record("selecting-both-snapshots-compares-without-button",
      fixture.state.lastDiff?.join(",") === "2,1" &&
        autoCompared.netVisible && autoCompared.rows > 0,
      JSON.stringify(autoCompared));
    // 只切换一个 select：结果自动刷新为新组合；焦点仍留在被操作的 select（不抢焦点）。
    await page.focus("#sel-b");
    await page.selectOption("#sel-b", "2");
    await waitForText(page, "#diff-status", "正在对比快照 #2 → #2");
    const oneSwitch = await page.evaluate(() => ({
      activeElement: document.activeElement ? document.activeElement.id : "",
    }));
    record("switching-one-snapshot-refreshes-and-keeps-focus",
      fixture.state.lastDiff?.join(",") === "2,2" && oneSwitch.activeElement === "sel-b",
      JSON.stringify(oneSwitch));
    // 只选一个：空态 + 引导文案，且不发对比请求（lastDiff 保持不变）。
    await page.evaluate(() => {
      const sel = document.getElementById("sel-a");
      sel.value = "";
      sel.dispatchEvent(new Event("change"));
    });
    await waitForText(page, "#diff-status", "选齐后自动对比");
    const partial = await page.evaluate(() => ({
      emptyHint: document.querySelector("#changes-body tr td.hint")?.textContent || "",
      netHidden: document.getElementById("changes-net").hidden,
    }));
    record("partial-selection-keeps-empty-state",
      partial.emptyHint.includes("暂无可比较数据") && partial.netHidden &&
        fixture.state.lastDiff?.join(",") === "2,2",
      JSON.stringify(partial));
    // 失败态：自动对比请求中断时展示内联「重试」小按钮，点击后恢复。
    await page.route("**/api/diff*", (route) => route.abort("internetdisconnected"));
    await page.selectOption("#sel-a", "1");
    await waitForText(page, "#diff-status", "无法连接本地服务");
    record("auto-compare-failure-shows-retry",
      await page.evaluate(() => {
        const btn = document.querySelector("#diff-status .diff-retry");
        return Boolean(btn) && btn.textContent === "重试";
      }));
    await page.unroute("**/api/diff*");
    await page.click("#diff-status .diff-retry");
    await waitForText(page, "#diff-status", "正在对比快照 #1 → #2");
    record("retry-button-recovers-comparison",
      fixture.state.lastDiff?.join(",") === "1,2");
    // 键盘路径（原生行为）：处理绑定在原生 change 事件上，键盘改选产生的正是
    // 同一事件。headless macOS Chromium 的 select 方向键走系统弹窗、不直接改值
    // （探针实测），故此处用「真实 focus + change 派发」验证：聚焦的 select 收到
    // change 即自动比对，且焦点不被结果刷新抢走；真机键盘走查由 GUI 实机验收覆盖。
    await page.focus("#sel-a");
    await page.evaluate(() => {
      const sel = document.getElementById("sel-a");
      sel.value = "2";
      sel.dispatchEvent(new Event("change"));
    });
    await waitForText(page, "#diff-status", "正在对比快照 #2 → #2");
    record("keyboard-focus-select-change-compares",
      fixture.state.lastDiff?.join(",") === "2,2" &&
        await page.evaluate(() =>
          document.activeElement && document.activeElement.id) === "sel-a");

    /* ---------- 轮询生命周期：切页不累积，扫描结束即停 ---------- */
    await setMode("scanning-stuck");
    await openPage("#/browse");
    await waitForText(page, "#scan-badge", "扫描进行中");
    // ISS-073：running 徽章含旋转深度环 SVG；跨轮询 tick 同一 SVG 节点不被重建
    const ring1 = await page.$eval("#scan-badge .badge-ring svg", (el) => {
      el.dataset.probe = "ring-node";
      return el.dataset.probe;
    });
    await page.waitForTimeout(5200);  // 跨一个 5s 轮询 tick
    const ringPersist = await page.$eval("#scan-badge .badge-ring svg", (el) => el.dataset.probe === "ring-node")
      .catch(() => false);
    // ISS-073 review 修复验证：跨 tick 后徽章文本必须恰好一条且全等（v1 的
    // live NodeList 跳位会产生「扫描进行中…扫描进行中…」，includes 式检查盲区）
    const badgeTextExact = await page.$eval("#scan-badge", (el) => ({
      text: el.textContent,
      textNodes: [...el.childNodes].filter((n) => n.nodeType === 3 && n.textContent.length > 0).length,
    }));
    const textExactOk = badgeTextExact.text === "扫描进行中…" && badgeTextExact.textNodes === 1;
    record("scan-badge-ring-persists-across-poll-ticks", ringPersist === true && textExactOk,
      `probe=${ringPersist} text=${JSON.stringify(badgeTextExact.text)} textNodes=${badgeTextExact.textNodes}`);
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

    /* ---------- ISS-090：live 进度徽章——事实计数 + 无 live 键回退 ---------- */
    await setMode("scanning-live");
    await page.waitForFunction(() =>
      /^扫描中 · 已扫 \d+ 目录 · [\d.]+ (B|KB|MB|GB|TB) · (\d+:)?\d+:\d{2}$/
        .test(document.querySelector("#scan-badge")?.textContent || ""));
    const badgeDirs = () => page.$eval("#scan-badge", (el) =>
      Number((el.textContent.match(/已扫 (\d+) 目录/) || [0, 0])[1]));
    const dirs1 = await badgeDirs();
    const liveRing = await page.$("#scan-badge .badge-ring");
    await page.waitForTimeout(5300);  // 跨一个 5s 轮询 tick：事实计数必须前进
    const dirs2 = await badgeDirs();
    record("scan-badge-live-progress-updates-across-ticks",
      dirs1 > 0 && dirs2 > dirs1 && liveRing !== null,
      `dirs ${dirs1} -> ${dirs2} ring=${liveRing !== null}`);
    // live 键消失（旧后端形状 / 心跳超时）：回退无计数文案，绝不显示 0/NaN。
    await setMode("scanning-stuck");
    await waitForText(page, "#scan-badge", "扫描进行中…");
    const fallbackText = await page.$eval("#scan-badge", (el) => el.textContent);
    record("scan-badge-live-fallback-when-field-missing",
      fallbackText === "扫描进行中…", `text=${JSON.stringify(fallbackText)}`);

    await setMode("dual");  // 扫描结束：下一次轮询观测到空闲后必须停止
    await waitForText(page, "#scan-badge", "未手动扫描过");
    // ISS-073：离开 running 态后环被移除
    const ringGone = await page.$("#scan-badge .badge-ring");
    record("scan-badge-ring-removed-on-idle", ringGone === null, `ring=${Boolean(ringGone)}`);
    const settleStart = Date.now();
    while (Date.now() - settleStart < 7000) await page.waitForTimeout(500);
    const statusAfter = (await fixtureState()).counts.status;
    record("scan-poll-stops-after-settle", statusAfter === 1, `status calls=${statusAfter} after settle`);

    /* ---------- ISS-094：内容页页内二级导航（横向 tab 分区） ----------
     * 口径：变化页五分区（默认比对明细）/ 分布页两分区（默认占用分布）/
     * 大文件页天然单区块不加 tab。断言覆盖：tab 结构与默认分区、点击切换
     * 渲染对应分区并写 hash 段、hash 刷新保持（路由不回落 overview）、
     * 方向键 + Enter 键盘激活、tab 条 sticky 于唯一滚动容器（082 不回归）、
     * 旭日图点扇区联动切目录浏览器分区。全部经真实 UI 驱动。 */
    await setMode("addedremoved");
    await openPage("#/changes");
    await page.waitForFunction(() => document.querySelectorAll("#sel-b option").length === 2);
    const tabsInitial = await page.evaluate(() => ({
      tabs: [...document.querySelectorAll("#page-changes .page-tab")].map((b) => b.dataset.tab),
      labels: [...document.querySelectorAll("#page-changes .page-tab")].map((b) => b.textContent.trim()),
      active: document.querySelector('#page-changes .page-tab[aria-current="true"]')?.dataset.tab || null,
      visibleSections: [...document.querySelectorAll("#page-changes .page-tab-section")]
        .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.tab),
      hash: location.hash,
    }));
    record("changes-tabs-5-sections-default-detail",
      JSON.stringify(tabsInitial.tabs) === JSON.stringify(["detail", "grown", "shrunk", "added", "removed"]) &&
        tabsInitial.labels.join("/") === "比对明细/增长最多/缩减最多/新出现/消失" &&
        tabsInitial.active === "detail" &&
        JSON.stringify(tabsInitial.visibleSections) === JSON.stringify(["detail"]) &&
        tabsInitial.hash === "#/changes",
      JSON.stringify(tabsInitial));

    // 点击切换：added 分区渲染对应表格 + hash 写段；回默认 tab 清段。
    await page.click('#page-changes .page-tab[data-tab="added"]');
    await page.waitForSelector("#tbl-added [data-reveal]");
    await page.click('#page-changes .page-tab[data-tab="removed"]');
    await page.waitForSelector("#tbl-removed [data-reveal]");
    const removedActive = await page.evaluate(() => ({
      active: document.querySelector('#page-changes .page-tab[aria-current="true"]')?.dataset.tab || null,
      visibleSections: [...document.querySelectorAll("#page-changes .page-tab-section")]
        .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.tab),
      hash: location.hash,
    }));
    record("changes-tab-click-switches-section-and-persists-hash",
      removedActive.active === "removed" &&
        JSON.stringify(removedActive.visibleSections) === JSON.stringify(["removed"]) &&
        removedActive.hash === "#/changes/removed",
      JSON.stringify(removedActive));
    await page.click('#page-changes .page-tab[data-tab="detail"]');
    const backToDefault = await page.evaluate(() => location.hash);
    record("changes-tab-returning-default-clears-hash-segment",
      backToDefault === "#/changes", backToDefault);

    // 行操作可访问名（addedremoved 场景；分区 tab 化后 DOM 仍可查询，
    // textContent/属性读取不依赖可见性）
    const accessibleNames = await page.evaluate(() =>
      ["#tbl-added [data-reveal]", "#tbl-removed [data-reveal]"].map((selector) => {
        const button = document.querySelector(selector);
        return [button?.getAttribute("aria-label"), button?.getAttribute("title")];
      }));
    record("added-and-removed-actions-have-accessible-names",
      accessibleNames.every(([aria, title]) => aria === "在 Finder 中显示" && title === aria),
      JSON.stringify(accessibleNames));

    // 键盘可达：方向键在 tab 间移动焦点（不激活），Enter 激活。
    await page.focus('#page-changes .page-tab[data-tab="detail"]');
    await page.keyboard.press("ArrowRight");   // → grown
    await page.keyboard.press("ArrowRight");   // → shrunk
    await page.keyboard.press("End");          // → removed（Home/End 亦可达）
    const keyboardFocused = await page.evaluate(() => ({
      focusTab: document.activeElement?.dataset?.tab || null,
      activeTab: document.querySelector('#page-changes .page-tab[aria-current="true"]')?.dataset.tab || null,
    }));
    await page.keyboard.press("Enter");
    const keyboardActivated = await page.evaluate(() => ({
      activeTab: document.querySelector('#page-changes .page-tab[aria-current="true"]')?.dataset.tab || null,
      visibleSections: [...document.querySelectorAll("#page-changes .page-tab-section")]
        .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.tab),
      hash: location.hash,
    }));
    record("changes-tabs-arrow-keys-move-focus-enter-activates",
      keyboardFocused.focusTab === "removed" && keyboardFocused.activeTab === "detail" &&
        keyboardActivated.activeTab === "removed" &&
        JSON.stringify(keyboardActivated.visibleSections) === JSON.stringify(["removed"]) &&
        keyboardActivated.hash === "#/changes/removed",
      JSON.stringify({ keyboardFocused, keyboardActivated }));

    // hash 刷新保持：带段 URL 整页加载后 tab 恢复、路由不回落 overview。
    await openPage("#/changes/grown");
    await page.waitForFunction(() => document.querySelectorAll("#sel-b option").length === 2);
    const reloadedGrown = await page.evaluate(() => ({
      activeTab: document.querySelector('#page-changes .page-tab[aria-current="true"]')?.dataset.tab || null,
      visibleSections: [...document.querySelectorAll("#page-changes .page-tab-section")]
        .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.tab),
      hash: location.hash,
      pageTitle: document.getElementById("page-title").textContent,
      changesVisible: !document.getElementById("page-changes").classList.contains("hidden"),
    }));
    record("changes-tab-hash-persists-across-reload",
      reloadedGrown.activeTab === "grown" &&
        JSON.stringify(reloadedGrown.visibleSections) === JSON.stringify(["grown"]) &&
        reloadedGrown.hash === "#/changes/grown" &&
        reloadedGrown.pageTitle === "变化" && reloadedGrown.changesVisible,
      JSON.stringify(reloadedGrown));

    // tab 条 sticky：内容上滚后 tab 条贴唯一滚动容器（.page-container）顶。
    // 滚动容器高度合同（ISS-082）由既有 viewport-no-horizontal-overflow 探针
    // 钉住；这里钉「tab 条在容器内 sticky 常驻」不回归。
    // sticky 需要真实滚动量：dual 夹具的分区内容在 820 高视口下不足一屏
    // （scrollable=false），缩到 600 高制造小窗口场景（sticky 的价值场景，
    // 布局本身不变），检查后恢复标准视口。
    await page.setViewportSize({ width: 1220, height: 600 });
    const stickyCheck = await page.evaluate(() => {
      const container = document.querySelector(".page-container");
      const tabs = document.querySelector("#page-changes .page-tabs");
      const padTop = parseFloat(getComputedStyle(container).paddingTop) || 0;
      container.scrollTop = 600;
      const tabTop = tabs.getBoundingClientRect().top;
      const containerTop = container.getBoundingClientRect().top;
      return {
        tabTop, containerTop, padTop,
        scrollable: container.scrollHeight > container.clientHeight,
        scrollTop: container.scrollTop,
      };
    });
    record("changes-tabs-sticky-at-scroll-container-top",
      stickyCheck.scrollable && stickyCheck.scrollTop > 0 &&
        Math.abs(stickyCheck.tabTop - (stickyCheck.containerTop + stickyCheck.padTop)) <= 1,
      JSON.stringify(stickyCheck));
    await page.setViewportSize({ width: 1220, height: 820 });
    const changesTabsShot = path.join(evidenceDir, "changes-tabs-grown-1220x820.png");
    await page.screenshot({ path: changesTabsShot });

    // 分布页：两分区 + 默认占用分布 + 旭日图点扇区联动切目录浏览器。
    await setMode("dual");
    await openPage("#/browse");
    await page.waitForSelector("#chart-sunburst canvas");
    const browseTabsInitial = await page.evaluate(() => ({
      tabs: [...document.querySelectorAll("#page-browse .page-tab")].map((b) => b.dataset.tab),
      active: document.querySelector('#page-browse .page-tab[aria-current="true"]')?.dataset.tab || null,
      visibleSections: [...document.querySelectorAll("#page-browse .page-tab-section")]
        .filter((s) => !s.hasAttribute("hidden")).map((s) => s.dataset.tab),
    }));
    record("browse-tabs-default-sunburst",
      JSON.stringify(browseTabsInitial.tabs) === JSON.stringify(["sunburst", "browser"]) &&
        browseTabsInitial.active === "sunburst" &&
        JSON.stringify(browseTabsInitial.visibleSections) === JSON.stringify(["sunburst"]),
      JSON.stringify(browseTabsInitial));
    // 真实点击旭日图扇区（一级环 Archive：radius=[40,"92%"]，环心 40px
    // 空洞，中心是空洞；右侧 70px 落在一级环 40~94px 内）→ 自动切目录
    // 浏览器分区。先等初始径向展开动画结束再点，避免点击落在未绘制区域。
    await page.waitForTimeout(1300);
    const sunburstBox = await page.locator("#chart-sunburst canvas").boundingBox();
    await page.mouse.click(
      sunburstBox.x + sunburstBox.width / 2 + 70,
      sunburstBox.y + sunburstBox.height / 2);
    await page.waitForSelector('#page-browse .page-tab[data-tab="browser"][aria-current="true"]');
    const sunburstLink = await page.evaluate(() => ({
      activeTab: document.querySelector('#page-browse .page-tab[aria-current="true"]')?.dataset.tab || null,
      browserVisible: !document.querySelector('#page-browse .page-tab-section[data-tab="browser"]').hasAttribute("hidden"),
      hash: location.hash,
      tableRows: document.querySelectorAll("#tbl-browse tbody tr").length,
    }));
    record("browse-sunburst-click-switches-to-browser-tab",
      sunburstLink.activeTab === "browser" && sunburstLink.browserVisible &&
        sunburstLink.hash === "#/browse/browser" && sunburstLink.tableRows > 0,
      JSON.stringify(sunburstLink));
    const browseTabsShot = path.join(evidenceDir, "browse-tabs-browser-1220x820.png");
    await page.screenshot({ path: browseTabsShot });

    // 大文件页：调研结论钉住——天然单区块（一个查询表即整页），不加 tab。
    await openPage("#/bigfiles");
    await page.waitForSelector("#tbl-bigfiles tbody tr");
    const bigfilesNoTabs = await page.evaluate(() => ({
      tabBars: document.querySelectorAll("#page-bigfiles .page-tabs").length,
      sections: document.querySelectorAll("#page-bigfiles .page-tab-section").length,
    }));
    record("bigfiles-single-block-no-tabs-by-design",
      bigfilesNoTabs.tabBars === 0 && bigfilesNoTabs.sections === 0,
      JSON.stringify(bigfilesNoTabs));

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
    // ISS-094：默认分区=占用分布（旭日图）；旅程走目录浏览器分区——真实点击切换。
    await page.click('#page-browse .page-tab[data-tab="browser"]');
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
    // 分布页同样有复制按钮（ISS-094：切到目录浏览器分区后可见可点）
    await openPage("#/browse");
    await page.click('#page-browse .page-tab[data-tab="browser"]');
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
    // 分布页：sunburst + 浏览器表格同时存在（ISS-094：两个分区各真实
    // 点击一次——默认占用分布可见；目录浏览器切过去后趋势图 resume）
    await openPage("#/browse");
    await page.waitForSelector("#chart-sunburst canvas");
    await page.click('#page-browse .page-tab[data-tab="browser"]');
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
    // ISS-087：scan-history 在「计划与通知」section 下，默认 section=监控时不可见。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="schedule"]')?.click());
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
    // ISS-087：permissions-panel 在「计划与通知」section；切到 schedule 让其可见。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="schedule"]')?.click());
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
    // ISS-087：permissions-panel 在「计划与通知」section；切到 schedule 让其可见。
    await tpage2.evaluate(() => document.querySelector('.settings-nav-item[data-section="schedule"]')?.click());
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

    /* ---------- ISS-091 设置页：监控分区「权限与覆盖」事实卡 ----------
     * 用户反馈 2026-09-24（权限受限/扫描件消失，设置页要有权限按钮）的落地验证：
     * 1) 浏览器态（默认 page，无 Tauri 桥）：partial-all 夹具 denied=6 /
     *    vanished=4 / dir_count=40 → 数字与占比 6/40（15.0%）如实呈现，
     *    解释含「完全磁盘访问」与「属正常」；深链按钮隐藏、降级路径文字
     *    可见，不渲染假 <a>。监控是默认 section（ISS-087），无需切 nav；
     *    排除列表编辑器（ISS-069）与新卡同容器并存。
     * 2) mock 桥（tpage6）：按钮可见、fallback 隐藏，点击后 invoke 收到
     *    cmd=plugin:opener|open_url 且 url 指向 Privacy_AllFiles（与
     *    ISS-002A 深链同命令同目标）。 */
    await setMode("partial-all");
    await openPage("#/settings");
    await page.waitForSelector("#monitor-permissions-card [data-test='mperm-denied']");
    const mpermBrowser = await page.evaluate(() => {
      const card = document.getElementById("monitor-permissions-card");
      return {
        inMonitoring: !!card &&
          card.closest(".settings-section[data-section='monitoring']") !== null,
        denied: card?.querySelector("[data-test='mperm-denied']")?.textContent || "",
        vanished: card?.querySelector("[data-test='mperm-vanished']")?.textContent || "",
        deniedLabel: card?.querySelector("[data-test='mperm-denied']")
          ?.parentElement?.querySelector(".perm-fact-label")?.textContent || "",
        note: [...(card?.querySelectorAll(".perm-note") || [])]
          .map((p) => p.textContent).join(" | "),
        openBtnVisible: !document.getElementById("btn-mperm-open-prefs")?.hidden,
        fallbackVisible: !card?.querySelector("[data-test='mperm-path-fallback']")?.hidden,
        fallbackText: card?.querySelector("[data-test='mperm-path-fallback']")?.textContent || "",
        fakeAnchorCount: [...(card?.querySelectorAll("a") || [])].filter((a) =>
          a.getAttribute("href")?.startsWith("x-apple.systempreferences")).length,
        excludePanelStillThere: !!document.getElementById("exclude-panel"),
      };
    });
    record("mperm-monitoring-card-shows-denied-vanished-ratio",
      mpermBrowser.inMonitoring &&
        mpermBrowser.denied === "6" && mpermBrowser.vanished === "4" &&
        mpermBrowser.deniedLabel.includes("6 / 40") &&
        mpermBrowser.deniedLabel.includes("15.0%") &&
        mpermBrowser.note.includes("完全磁盘访问") &&
        mpermBrowser.note.includes("属正常") &&
        mpermBrowser.excludePanelStillThere,
      JSON.stringify(mpermBrowser).slice(0, 200));
    record("mperm-browser-fallback-hides-button-shows-path",
      !mpermBrowser.openBtnVisible && mpermBrowser.fallbackVisible &&
        mpermBrowser.fallbackText.includes("系统设置") &&
        mpermBrowser.fallbackText.includes("隐私与安全性") &&
        mpermBrowser.fallbackText.includes("完全磁盘访问") &&
        mpermBrowser.fakeAnchorCount === 0,
      JSON.stringify(mpermBrowser).slice(0, 160));
    const settingsMonitorPermShot = path.join(evidenceDir, "settings-monitor-permissions-1220x820.png");
    await page.screenshot({ path: settingsMonitorPermShot });

    const tpage6 = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tpage6Errors = [];
    tpage6.on("pageerror", (e) => tpage6Errors.push(e.message));
    await tpage6.addInitScript(`
      window.__tauriMock6 = { invokes: [] };
      Object.defineProperty(window, "__TAURI__", { value: {
        core: { invoke: (cmd, args) => {
          window.__tauriMock6.invokes.push({ cmd, args });
          return Promise.resolve();
        } },
        event: { listen: () => Promise.resolve(0) },
      }, configurable: true });
    `);
    await tpage6.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    await tpage6.waitForSelector("#monitor-permissions-card [data-test='mperm-open-prefs-btn']");
    const mpermTauri = await tpage6.evaluate(() => {
      const card = document.getElementById("monitor-permissions-card");
      return {
        openBtnVisible: !document.getElementById("btn-mperm-open-prefs")?.hidden,
        fallbackHidden: card?.querySelector("[data-test='mperm-path-fallback']")?.hidden,
        denied: card?.querySelector("[data-test='mperm-denied']")?.textContent || "",
      };
    });
    await tpage6.click("#btn-mperm-open-prefs");
    await tpage6.waitForFunction(() => (window.__tauriMock6.invokes || []).some(
      (c) => c && c.cmd === "plugin:opener|open_url"));
    // settings 页加载会先推送 tray 状态，不能断言 invokes[0]；在全部记录中定位。
    const mpermInvoke = await tpage6.evaluate(() => (window.__tauriMock6.invokes || [])
      .find((c) => c && c.cmd === "plugin:opener|open_url"));
    record("mperm-tauri-mock-deeplink-invokes-opener",
      mpermTauri.openBtnVisible && mpermTauri.fallbackHidden &&
        mpermTauri.denied === "6" &&
        mpermInvoke?.cmd === "plugin:opener|open_url" &&
        mpermInvoke?.args?.url === "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
      JSON.stringify({ t: mpermTauri, i: mpermInvoke }));
    record("mperm-tauri-mock-no-page-errors", tpage6Errors.length === 0,
      tpage6Errors.join("; "));
    await tpage6.close();

    /* ---------- ISS-095 权限卡：占比 >100% 的倍数文案 + 空库「尚未扫描」 ----------
     * 1) denied-over 夹具（denied=25 / dir_count=2；du stderr 行数可超目录数）：
     *    标签显示「25 / 2（受限行数为目录数的 12.5 倍）」，不再出现 >100%
     *    的百分比读数（旧形态「（1250.0%）」会被读成「1250% 的目录受限」）；
     *    数字事实与解释文字不受影响。
     * 2) empty 夹具（无快照）：卡片体显示「尚未扫描」引导文案，不渲染
     *    数字事实（denied/vanished 均不出现）。
     * 两组都在浏览器态（默认 page）走真实 UI 验证；setMode 重置竞态，
     * 后续用例（ISS-016B）自带 setMode("dual")，不被污染。 */
    await setMode("denied-over");
    await openPage("#/settings");
    await page.waitForSelector("#monitor-permissions-card [data-test='mperm-denied']");
    const mpermOver = await page.evaluate(() => {
      const card = document.getElementById("monitor-permissions-card");
      return {
        denied: card?.querySelector("[data-test='mperm-denied']")?.textContent || "",
        label: card?.querySelector("[data-test='mperm-denied']")
          ?.parentElement?.querySelector(".perm-fact-label")?.textContent || "",
        note: ([...(card?.querySelectorAll(".perm-note") || [])]
          .map((p) => p.textContent).join(" | ")),
      };
    });
    record("mperm-denied-over-dir-count-uses-multiple-not-percent",
      mpermOver.denied === "25" &&
        mpermOver.label.includes("25 / 2") &&
        mpermOver.label.includes("受限行数为目录数的 12.5 倍") &&
        !mpermOver.label.includes("%") &&
        mpermOver.note.includes("完全磁盘访问"),
      JSON.stringify(mpermOver).slice(0, 160));
    const mpermOverShot = path.join(evidenceDir, "settings-mperm-denied-over-1220x820.png");
    await page.screenshot({ path: mpermOverShot });

    await setMode("empty");
    await openPage("#/settings");
    /* 空库态先用「加载中…」占位（同为 p.hint），必须等 fetch 完成后的
     * 最终文案再断言，避免读到占位文本假绿。否定条件针对数字事实元素：
     * 引导文案本身合法包含「读取受限/扫描期间消失」字样，不能按字面排除。 */
    await page.waitForFunction(() => {
      const el = document.querySelector("#monitor-permissions-card [data-test='mperm-card-body']");
      return el && el.textContent.includes("尚未扫描");
    });
    const mpermEmpty = await page.evaluate(() => {
      const body = document.querySelector("#monitor-permissions-card [data-test='mperm-card-body']");
      return {
        text: body?.textContent || "",
        hasFacts: !!body?.querySelector("[data-test='mperm-facts']"),
        hasDenied: !!body?.querySelector("[data-test='mperm-denied']"),
        hasVanished: !!body?.querySelector("[data-test='mperm-vanished']"),
      };
    });
    record("mperm-empty-library-shows-not-yet-scanned-hint",
      mpermEmpty.text.includes("尚未扫描") &&
        !mpermEmpty.hasFacts && !mpermEmpty.hasDenied && !mpermEmpty.hasVanished,
      mpermEmpty.text.trim().slice(0, 80));
    const mpermEmptyShot = path.join(evidenceDir, "settings-mperm-empty-1220x820.png");
    await page.screenshot({ path: mpermEmptyShot });

    /* ---------- ISS-016B mock 桥：drift 重装入 口复用 010B 确认层 ----------
     * 形状化 mock：autostart_status 返回三态真值（两标签 enabled）、
     * autostart_register_plan 返回可审清单、autostart_register 返回 ok。
     * 验证：drift 态出现「重新安装计划」按钮 → 点击展开确认层（计划清单 +
     * 确认/取消）→ 取消后回读系统真值 → 再确认则经 confirmed=true 执行注册
     * 并回读状态。桥数据中的命令字符串是 mock 数据，不代表真实系统调用。 */
    const tpage3 = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tpage3Errors = [];
    tpage3.on("pageerror", (e) => tpage3Errors.push(e.message));
    await tpage3.addInitScript(`
      window.__tauriMock3 = { invokes: [] };
      Object.defineProperty(window, "__TAURI__", { value: {
        core: { invoke: (cmd, args) => {
          window.__tauriMock3.invokes.push({ cmd, args });
          if (cmd === "autostart_status") {
            return Promise.resolve({ scan: "enabled", web: "enabled", login_item: "unknown" });
          }
          if (cmd === "autostart_register_plan") {
            return Promise.resolve({
              plist_files: [
                { label: "com.maoscripts.fathom-scan",
                  path: "/fixture/LaunchAgents/com.maoscripts.fathom-scan.plist" },
                { label: "com.maoscripts.fathom-web",
                  path: "/fixture/LaunchAgents/com.maoscripts.fathom-web.plist" },
              ],
              commands: [
                ["id", "-u"],
                ["launchctl", "bootout", "gui/<uid>", "/fixture/LaunchAgents/com.maoscripts.fathom-scan.plist"],
                ["launchctl", "bootstrap", "gui/<uid>", "/fixture/LaunchAgents/com.maoscripts.fathom-scan.plist"],
              ],
            });
          }
          if (cmd === "autostart_register") return Promise.resolve({ ok: true });
          return Promise.resolve();
        } },
        event: { listen: () => Promise.resolve(0) },
      }, configurable: true });
    `);
    await setMode("dual");  // 重置 config：13:30 vs 已注册 12:00 → drift
    await tpage3.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    // ISS-087：autostart-toggle 在「计划与通知」section 下，默认 section=监控，
    // 必须先点 nav 才能看到；后续用例基于可见的 autostart-toggle 操作。
    await tpage3.evaluate(() => document.querySelector('.settings-nav-item[data-section="schedule"]')?.click());
    await tpage3.waitForSelector("[data-test='autostart-toggle']");
    const currentScanTime = (await fixtureState()).config.scan_time;
    const reloadTauri = await tpage3.evaluate(() => ({
      text: document.querySelector("[data-test='reload-state-text']")?.textContent || "",
      hasButton: Boolean(document.getElementById("btn-reinstall-plan")),
      toggleChecked: !!document.getElementById("autostart-toggle")?.checked,
    }));
    record("reload-reinstall-entry-visible-on-drift-with-bridge",
      reloadTauri.text.includes("计划时间不一致") && reloadTauri.hasButton &&
        reloadTauri.toggleChecked === true,  // 系统真值（两标签 enabled）回写开关
      JSON.stringify(reloadTauri).slice(0, 160));

    // 点击重装 → 确认层展开：计划清单可审，注册计划请求带当前 scanTime。
    await tpage3.click("#btn-reinstall-plan");
    await tpage3.waitForSelector("#autostart-confirm:not([hidden])");
    const confirmLayer = await tpage3.evaluate(() => ({
      text: document.getElementById("autostart-confirm").textContent,
      hasYes: Boolean(document.getElementById("autostart-confirm-yes")),
      hasNo: Boolean(document.getElementById("autostart-confirm-no")),
    }));
    const planInvoke = await tpage3.evaluate(() => (window.__tauriMock3.invokes || [])
      .find((c) => c && c.cmd === "autostart_register_plan"));
    record("reload-reinstall-opens-confirm-layer-with-plan",
      confirmLayer.text.includes("com.maoscripts.fathom-scan") &&
        confirmLayer.hasYes && confirmLayer.hasNo &&
        planInvoke?.args?.scanTime === currentScanTime,
      JSON.stringify({ planArgs: planInvoke?.args, layer: confirmLayer.text.slice(0, 60) }).slice(0, 200));

    // 取消：确认层收起，回读系统真值（开关仍按系统态勾选），drift 文案保留。
    await tpage3.click("#autostart-confirm-no");
    await tpage3.waitForFunction(() => document.getElementById("autostart-confirm").hidden);
    await tpage3.waitForFunction(() => (window.__tauriMock3.invokes || [])
      .filter((c) => c && c.cmd === "autostart_status").length >= 2);
    const afterCancel = await tpage3.evaluate(() => ({
      layerHidden: document.getElementById("autostart-confirm").hidden,
      toggleChecked: !!document.getElementById("autostart-toggle")?.checked,
      text: document.querySelector("[data-test='reload-state-text']")?.textContent || "",
    }));
    record("reload-reinstall-cancel-resyncs-system-truth",
      afterCancel.layerHidden && afterCancel.toggleChecked === true &&
        afterCancel.text.includes("计划时间不一致"),
      JSON.stringify(afterCancel).slice(0, 160));

    // 再次展开并确认执行：autostart_register(confirmed=true, scanTime=当前)，
    // 注册后回读系统状态并给出结果反馈。
    await tpage3.click("#btn-reinstall-plan");
    await tpage3.waitForSelector("#autostart-confirm:not([hidden])");
    await tpage3.click("#autostart-confirm-yes");
    await tpage3.waitForFunction(() => (window.__tauriMock3.invokes || [])
      .some((c) => c && c.cmd === "autostart_register"));
    const registerInvoke = await tpage3.evaluate(() => (window.__tauriMock3.invokes || [])
      .find((c) => c && c.cmd === "autostart_register"));
    // ISS-016B repair1：register invoke 发生 ≠ refreshAutostart 走完——其后还有
    // autostart_status 回读与 /api/config 真实刷新，note 在此之后才渲染「已开启」。
    await tpage3.waitForFunction(() =>
      (document.querySelector("[data-test='autostart-note']")?.textContent || "").includes("已开启"));
    const statusCount3 = await tpage3.evaluate(() => (window.__tauriMock3.invokes || [])
      .filter((c) => c && c.cmd === "autostart_status").length);
    const afterRegister = await tpage3.evaluate(() => ({
      note: document.querySelector("[data-test='autostart-note']")?.textContent || "",
      toggleChecked: !!document.getElementById("autostart-toggle")?.checked,
    }));
    record("reload-reinstall-confirm-executes-confirmed-register",
      registerInvoke?.args?.confirmed === true &&
        registerInvoke?.args?.scanTime === currentScanTime &&
        statusCount3 >= 3 &&  // 初始加载 + 取消回读 + 注册后回读
        afterRegister.note.includes("已开启") && afterRegister.toggleChecked === true &&
        tpage3Errors.length === 0,
      JSON.stringify({ registerArgs: registerInvoke?.args, statusCount3, afterRegister }).slice(0, 200));
    await tpage3.close();

    /* ---------- ISS-040B 应用更新：浏览器降级 + mock 桥全流程 ----------
     * 浏览器模式（无桥）：只读降级说明，不渲染检查按钮/假入口。
     * mock 桥：unconfigured 态状态行（含当前版本、无安装入口）；available 态
     * 版本+notes+确认层；取消回落（不发 updater_install）；确认后
     * updater_install 携 confirmed:true；安装成功进入「重启以完成」独立确认，
     * 取消不发 updater_restart、再确认才携带 confirmed:true 调用。 */
    await setMode("dual");
    await openPage("#/settings");
    // ISS-087：updater-panel 在「关于」section 下；切到 about 让其可见。
    await page.evaluate(() => document.querySelector('.settings-nav-item[data-section="about"]')?.click());
    await page.waitForSelector("#updater-panel");
    const updaterBrowser = await page.evaluate(() => ({
      note: document.querySelector("[data-test='updater-browser-note']")?.textContent || "",
      hasCheckBtn: Boolean(document.getElementById("btn-updater-check")),
      hasInstallBtn: Boolean(document.getElementById("btn-updater-install")),
    }));
    record("updater-browser-mode-readonly-degrade",
      updaterBrowser.note.includes("桌面应用的设置页") &&
        !updaterBrowser.hasCheckBtn && !updaterBrowser.hasInstallBtn,
      JSON.stringify(updaterBrowser).slice(0, 160));

    const tpage4 = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tpage4Errors = [];
    tpage4.on("pageerror", (e) => tpage4Errors.push(e.message));
    await tpage4.addInitScript(`
      window.__tauriMock4 = { invokes: [] };
      Object.defineProperty(window, "__TAURI__", { value: {
        core: { invoke: (cmd, args) => {
          window.__tauriMock4.invokes.push({ cmd, args });
          if (cmd === "updater_check") {
            return Promise.resolve(window.__updaterCheck ||
              { state: "unconfigured", current_version: "0.3.0" });
          }
          if (cmd === "updater_install") {
            return Promise.resolve({ ok: true, state: "installed",
              current_version: "0.3.0", available_version: "0.4.0" });
          }
          if (cmd === "updater_restart") {
            return Promise.resolve({ ok: true });
          }
          if (cmd === "autostart_status") {
            return Promise.resolve({ scan: "disabled", web: "disabled", login_item: "unknown" });
          }
          return Promise.resolve();
        } },
        event: { listen: (name, handler) => {
          (window.__tauriListeners = window.__tauriListeners || {})[name] = handler;
          return Promise.resolve(0);
        } },
      }, configurable: true });
    `);
    await tpage4.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    // ISS-087：updater-check-btn 在「关于」section 下；切到 about 让其可见。
    await tpage4.evaluate(() => document.querySelector('.settings-nav-item[data-section="about"]')?.click());
    await tpage4.waitForSelector("[data-test='updater-check-btn']");

    // 1) unconfigured：状态行含当前版本与未配置文案，且不出现安装入口。
    await tpage4.click("[data-test='updater-check-btn']");
    await tpage4.waitForFunction(() =>
      (document.querySelector("[data-test='updater-status-text']")?.textContent || "").includes("未配置"));
    const updUnconf = await tpage4.evaluate(() => ({
      status: document.querySelector("[data-test='updater-status-text']")?.textContent || "",
      hasInstall: Boolean(document.getElementById("btn-updater-install")),
      checkArgs: (window.__tauriMock4.invokes || [])
        .filter((c) => c && c.cmd === "updater_check").slice(-1)[0]?.args,
    }));
    record("updater-unconfigured-state-shown",
      updUnconf.status.includes("未配置") && updUnconf.status.includes("0.3.0") &&
        !updUnconf.hasInstall && JSON.stringify(updUnconf.checkArgs) === "{}",
      JSON.stringify(updUnconf).slice(0, 200));

    // 2) available：版本 + notes + 「下载并安装」入口。
    await tpage4.evaluate(() => {
      window.__updaterCheck = { state: "available", current_version: "0.3.0",
        available_version: "0.4.0", notes: "演示版本说明" };
    });
    await tpage4.click("[data-test='updater-check-btn']");
    await tpage4.waitForFunction(() =>
      (document.querySelector("[data-test='updater-status-text']")?.textContent || "").includes("有可用更新"));
    const updAvail = await tpage4.evaluate(() => ({
      status: document.querySelector("[data-test='updater-status-text']")?.textContent || "",
      available: document.querySelector("[data-test='updater-available']")?.textContent || "",
      notes: document.querySelector("[data-test='updater-notes']")?.textContent || "",
      hasInstallBtn: Boolean(document.getElementById("btn-updater-install")),
    }));
    record("updater-available-shows-version-and-notes",
      updAvail.status.includes("0.3.0") && updAvail.available.includes("0.4.0") &&
        updAvail.notes.includes("演示版本说明") && updAvail.hasInstallBtn,
      JSON.stringify(updAvail).slice(0, 200));

    // 3) 确认层：版本可审，确认/取消都在。
    await tpage4.click("[data-test='updater-install-btn']");
    await tpage4.waitForSelector("[data-test='updater-confirm']:not([hidden])");
    const updLayer = await tpage4.evaluate(() => ({
      text: document.querySelector("[data-test='updater-confirm']")?.textContent || "",
      hasYes: Boolean(document.getElementById("updater-confirm-yes")),
      hasNo: Boolean(document.getElementById("updater-confirm-no")),
    }));
    record("updater-install-opens-confirm-layer",
      updLayer.text.includes("0.4.0") && updLayer.text.includes("验签") &&
        updLayer.hasYes && updLayer.hasNo,
      JSON.stringify(updLayer).slice(0, 200));

    // 4) 取消回落：确认层收起，不发 updater_install，available 信息保留。
    await tpage4.click("[data-test='updater-confirm-no']");
    await tpage4.waitForFunction(() =>
      document.querySelector("[data-test='updater-confirm']")?.hidden === true);
    const updCancel = await tpage4.evaluate(() => ({
      layerHidden: document.querySelector("[data-test='updater-confirm']")?.hidden === true,
      installInvokes: (window.__tauriMock4.invokes || [])
        .filter((c) => c && c.cmd === "updater_install").length,
      stillAvailable: (document.querySelector("[data-test='updater-available']")?.textContent || "").includes("0.4.0"),
    }));
    record("updater-install-cancel-falls-back",
      updCancel.layerHidden && updCancel.installInvokes === 0 && updCancel.stillAvailable,
      JSON.stringify(updCancel).slice(0, 160));

    // 5) 确认执行：updater_install 必须携带 confirmed:true；成功后进入已安装态。
    await tpage4.click("[data-test='updater-install-btn']");
    await tpage4.waitForSelector("[data-test='updater-confirm']:not([hidden])");
    await tpage4.click("[data-test='updater-confirm-yes']");
    await tpage4.waitForFunction(() =>
      (document.querySelector("[data-test='updater-status-text']")?.textContent || "").includes("重启后生效"));
    const updInstall = await tpage4.evaluate(() => ({
      status: document.querySelector("[data-test='updater-status-text']")?.textContent || "",
      installed: document.querySelector("[data-test='updater-installed']")?.textContent || "",
      hasRestartBtn: Boolean(document.getElementById("btn-updater-restart")),
      installInvoke: (window.__tauriMock4.invokes || [])
        .find((c) => c && c.cmd === "updater_install"),
    }));
    record("updater-install-invoked-with-confirmed",
      updInstall.installInvoke?.args?.confirmed === true &&
        updInstall.status.includes("重启后生效") &&
        updInstall.installed.includes("0.4.0") && updInstall.hasRestartBtn,
      JSON.stringify(updInstall).slice(0, 200));

    // 6) 重启确认：取消不发 updater_restart、保持已安装态。
    await tpage4.click("[data-test='updater-restart-btn']");
    await tpage4.waitForSelector("[data-test='updater-restart-confirm']:not([hidden])");
    await tpage4.click("[data-test='updater-restart-no']");
    await tpage4.waitForFunction(() =>
      document.querySelector("[data-test='updater-restart-confirm']")?.hidden === true);
    const updRestartCancel = await tpage4.evaluate(() => ({
      layerHidden: document.querySelector("[data-test='updater-restart-confirm']")?.hidden === true,
      restartInvokes: (window.__tauriMock4.invokes || [])
        .filter((c) => c && c.cmd === "updater_restart").length,
      stillInstalled: (document.querySelector("[data-test='updater-installed']")?.textContent || "").length > 0,
    }));
    record("updater-restart-cancel-keeps-installed",
      updRestartCancel.layerHidden && updRestartCancel.restartInvokes === 0 &&
        updRestartCancel.stillInstalled,
      JSON.stringify(updRestartCancel).slice(0, 160));

    // 7) 再确认：updater_restart 携带 confirmed:true。
    await tpage4.click("[data-test='updater-restart-btn']");
    await tpage4.waitForSelector("[data-test='updater-restart-confirm']:not([hidden])");
    await tpage4.click("[data-test='updater-restart-yes']");
    await tpage4.waitForFunction(() => (window.__tauriMock4.invokes || [])
      .some((c) => c && c.cmd === "updater_restart"));
    const restartInvoke = await tpage4.evaluate(() => (window.__tauriMock4.invokes || [])
      .find((c) => c && c.cmd === "updater_restart"));
    record("updater-restart-confirmed-invoke",
      restartInvoke?.args?.confirmed === true && tpage4Errors.length === 0,
      JSON.stringify(restartInvoke));
    await tpage4.close();

    /* ---------- ISS-083 设置页信息架构收敛：打包态隐藏技术细节 ----------
     * 三组口径：
     *  - 无桥（浏览器/开发态）不回退：运行信息 8 行全量渲染、无高级折叠区、
     *    服务管理面板保持原位（#page-settings 直接子节点）；
     *  - mock 桥（打包态）默认视图：#settings-table 只含用户必要行（监控根/
     *    快照保留/大文件默认范围），技术行（服务地址/运行根/数据库/du 时限/
     *    桌面壳）与静态「服务管理」面板收进默认折叠的「高级与诊断」区且
     *    不可见；后台自启面板措辞收敛（不再出现 launchd/SMAppService 术语）；
     *  - 展开 summary 后技术区块全部可见（能力不删除）。
     * 真实 Tauri 壳内运行属 GUI 实机验证，不在本脚本范围（RESULT 如实标注）。 */
    const tpage5b = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tpage5bErrors = [];
    tpage5b.on("pageerror", (e) => tpage5bErrors.push(e.message));
    await tpage5b.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    // ISS-087：浏览器态「运行信息」表在「高级与诊断」section 下——先切到
    // advanced 让 settings-table 可见（默认 section=监控），再断言。
    await tpage5b.evaluate(() => document.querySelector('.settings-nav-item[data-section="advanced"]')?.click());
    await tpage5b.waitForSelector("#settings-table tbody tr");
    const browserModeRunInfo = await tpage5b.evaluate(() => ({
      tableText: document.querySelector("#settings-table")?.textContent || "",
      hasAdvancedPanel: Boolean(document.getElementById("advanced-panel")),
      // ISS-087：服务管理面板从 #page-settings 的直接子节点迁移到「高级与诊断」
      // section 容器（#settings-advanced-extra）下；浏览器态仍全量渲染，
      // 不创建 advanced-panel（折叠区是打包态独有）。
      serviceInsideAdvancedExtra: [...document.querySelectorAll("#settings-advanced-extra .panel-head h2")]
        .some((h) => h.textContent === "服务管理"),
      runInfoInsideAdvancedExtra: Boolean(document.querySelector("#settings-advanced-extra #settings-table")),
    }));
    record("settings-browser-mode-keeps-full-runinfo",
      browserModeRunInfo.tableText.includes("服务地址") &&
        browserModeRunInfo.tableText.includes("运行根") &&
        browserModeRunInfo.tableText.includes("数据库") &&
        browserModeRunInfo.tableText.includes("du 安全时限") &&
        browserModeRunInfo.tableText.includes("桌面壳") &&
        browserModeRunInfo.tableText.includes("监控根目录") &&
        !browserModeRunInfo.hasAdvancedPanel &&
        browserModeRunInfo.serviceInsideAdvancedExtra &&
        browserModeRunInfo.runInfoInsideAdvancedExtra &&
        tpage5bErrors.length === 0,
      JSON.stringify(browserModeRunInfo).slice(0, 200));
    await tpage5b.close();

    const tpage5 = await browser.newPage({ viewport: { width: 1220, height: 820 } });
    const tpage5Errors = [];
    tpage5.on("pageerror", (e) => tpage5Errors.push(e.message));
    await tpage5.addInitScript(`
      window.__tauriMock5 = { invokes: [] };
      Object.defineProperty(window, "__TAURI__", { value: {
        core: { invoke: (cmd, args) => {
          window.__tauriMock5.invokes.push({ cmd, args });
          if (cmd === "autostart_status") {
            return Promise.resolve({ scan: "enabled", web: "enabled", login_item: "disabled" });
          }
          if (cmd === "updater_check") {
            return Promise.resolve({ state: "up_to_date", current_version: "0.3.0" });
          }
          return Promise.resolve();
        } },
        event: { listen: () => Promise.resolve(0) },
      }, configurable: true });
    `);
    await tpage5.goto(`${base}/#/settings`, { waitUntil: "networkidle" });
    // ISS-087：打包态默认视图在「监控」section，settings-table 与折叠区在
    // 「高级与诊断」下——先切到 advanced 让 settings-table 可见（同时
    // 折叠区 summary 行默认 closed，断言「未展开」成立）。
    await tpage5.evaluate(() => document.querySelector('.settings-nav-item[data-section="advanced"]')?.click());
    await tpage5.waitForSelector("#settings-table tbody tr");
    const packagedDefault = await tpage5.evaluate(() => {
      const table = document.querySelector("#settings-table")?.textContent || "";
      const details = document.querySelector("#advanced-panel details[data-test='advanced-toggle']");
      // 经 h2 反查最近面板祖先：服务管理面板移入折叠区后，正向 .panel
      // 遍历会先命中外层 advanced 容器（其子树含「服务管理」h2）。
      const service = [...document.querySelectorAll("#page-settings .panel-head h2")]
        .find((node) => node.textContent === "服务管理")?.closest(".panel") || null;
      const advancedBody = document.querySelector("[data-test='advanced-body']");
      const visible = (el) => el && el.getClientRects().length > 0;
      const autostartTitle = document.querySelector("#autostart-panel .panel-head h2")?.textContent || "";
      const autostartText = document.querySelector("#autostart-panel")?.textContent || "";
      return {
        table,
        hasDetails: Boolean(details),
        detailsOpen: details ? details.open : null,
        serviceInsideAdvanced: Boolean(service && advancedBody && advancedBody.contains(service)),
        serviceVisible: visible(service),
        techTableRows: document.querySelectorAll("#settings-table-tech tbody tr").length,
        autostartTitle,
        autostartHasLaunchdTerm: autostartText.includes("launchd") || autostartText.includes("SMAppService"),
      };
    });
    record("settings-packaged-default-keeps-user-rows",
      packagedDefault.table.includes("监控根目录") &&
        packagedDefault.table.includes("快照保留") &&
        packagedDefault.table.includes("大文件默认范围") &&
        !packagedDefault.table.includes("服务地址") &&
        !packagedDefault.table.includes("运行根") &&
        !packagedDefault.table.includes("数据库") &&
        !packagedDefault.table.includes("du 安全时限") &&
        !packagedDefault.table.includes("桌面壳"),
      packagedDefault.table.slice(0, 160));
    record("settings-packaged-default-collapses-tech-blocks",
      packagedDefault.hasDetails && packagedDefault.detailsOpen === false &&
        packagedDefault.serviceInsideAdvanced && !packagedDefault.serviceVisible &&
        packagedDefault.techTableRows === 5,
      JSON.stringify(packagedDefault).slice(0, 200));
    record("settings-packaged-autostart-copy-simplified",
      packagedDefault.autostartTitle === "后台自启" &&
        !packagedDefault.autostartHasLaunchdTerm,
      JSON.stringify({
        title: packagedDefault.autostartTitle,
        hasLaunchdTerm: packagedDefault.autostartHasLaunchdTerm,
      }));
    const packagedDefaultShot = path.join(evidenceDir, "settings-packaged-default-1220x820.png");
    await tpage5.screenshot({ path: packagedDefaultShot });

    // 展开 summary：技术行与服务管理面板全部可见（能力不删除）。
    await tpage5.click("#advanced-panel details[data-test='advanced-toggle'] summary");
    await tpage5.waitForFunction(() => {
      const d = document.querySelector("#advanced-panel details[data-test='advanced-toggle']");
      const service = [...document.querySelectorAll("#page-settings .panel-head h2")]
        .find((node) => node.textContent === "服务管理")?.closest(".panel");
      return d && d.open === true && service && service.getClientRects().length > 0;
    });
    const packagedOpen = await tpage5.evaluate(() => ({
      techTableText: document.querySelector("#settings-table-tech")?.textContent || "",
      serviceVisibleText: ([...document.querySelectorAll("#page-settings .panel-head h2")]
        .find((node) => node.textContent === "服务管理")?.closest(".panel") || {})
        .textContent || "",
    }));
    record("settings-packaged-expanding-reveals-tech",
      packagedOpen.techTableText.includes("服务地址") &&
        packagedOpen.techTableText.includes("运行根") &&
        packagedOpen.techTableText.includes("数据库") &&
        packagedOpen.techTableText.includes("du 安全时限") &&
        packagedOpen.techTableText.includes("桌面壳") &&
        packagedOpen.serviceVisibleText.includes("-m fathom uninstall") || packagedOpen.serviceVisibleText.includes("main.py uninstall"),
      JSON.stringify(packagedOpen).slice(0, 200));
    const packagedOpenShot = path.join(evidenceDir, "settings-packaged-advanced-open-1220x820.png");
    await tpage5.screenshot({ path: packagedOpenShot, fullPage: true });
    await tpage5.close();

    /* ---------- 汇总 ---------- */
    record("no-unhandled-page-errors",
      pageErrors.length === 0 && tauriErrors.length === 0 && tpage4Errors.length === 0 &&
        tpage5Errors.length === 0 && tpage5bErrors.length === 0,
      [...pageErrors, ...tauriErrors, ...tpage4Errors, ...tpage5Errors, ...tpage5bErrors].join("; "));
    const failed = checks.filter((c) => !c.ok);
    process.stdout.write(JSON.stringify({
      ok: failed.length === 0,
      passed: checks.length - failed.length,
      failed: failed.length,
      evidence: [overviewShot, changesShot, browseShot, bigfilesShot, settingsShot,
        bigfilesTruncatedShot, bigfilesExpiredShot, bigfilesFailedShot,
        bigfilesPermShot, bigfilesNoMatchShot, settingsMonitorPermShot,
        mpermOverShot, mpermEmptyShot,
        packagedDefaultShot, packagedOpenShot,
        ...viewportScreens],
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
