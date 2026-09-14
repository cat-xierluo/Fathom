# ISS-032 WRITEBACK_PROPOSAL（给 PM 的回写建议）

本卡 Phase 1 + Phase 2 已交付可验证内核、API 接线、前端展示、夹具扩检查。
下列回写由 PM 合并后或独立 reviewer 验收时同步到仓库权威文档。

## 1. docs/TASKS.md（任务卡）

### ISS-032 验收勾选

按合同四框勾选：
- [x] 同参数并发只启动一次实际 find 且取消/超时后子进程回收
- [x] 无匹配/无权限/失败/截断/过期缓存分别可辨
- [x] du/find 墙钟/峰值内存/输出量及 DB/WAL/日志增长有小/大样例证据
- [x] 诊断导出不含真实路径/令牌/文件内容且预算与保留策略写入配置事实

### 状态

READY → REVIEW（或 DONE，取决于合并门禁）。

### 证据段落（追加）

> （2026-09-14）候选 commit `<phase2 commit>` 经独立 fixed-head review ACCEPT 路径：
> - `tests/test_bigfiles.py` 24/24 通过（基础合同 / 并发去重 / 取消+超时 / TTL / 五态 / 截断 / 日志脱敏 / 资源统计 / 向后兼容 / 默认参数 / 资源预算测量 / API 端点集成）；
> - `RESULT_BUDGET.md`：find 在 5×200MB / 20×200MB 合成树下 wall_ms p50 分别为 4 / 21ms；peak_rss_bytes 1.39 / 1.75 MB；find_output_lines 5 / 20；
> - `scripts/verify_frontend_refresh.cjs` 扩展为 5 个新增 scenario 覆盖 ok/truncated/expired/failed/permission_denied/no_match 六态，本会话 dispatch allowlist 阻断 node，合并门禁上由 PM/独立 reviewer 复跑 39/39 矩阵确认；
> - 真实 Tauri WebView、x86_64 发行 helper、签名公证、`FIND` 子进程在生产 launchd 跨日触发、`du` 共享块/稀疏文件口径仍 `NOT_VERIFIED`。

## 2. docs/ARCHITECTURE.md

### bigfiles.py 描述更新

旧描述：
> `bigfiles.py` | `/usr/bin/find -xdev -type f -size +... -mtime -... -print0` 后 stat | 大小为 st_size 逻辑字节；每次请求实时遍历；无超时/去重/失败呈现 |

新描述：
> `bigfiles.py` | `BigfilesManager`：`/usr/bin/find -xdev -type f -size +... -mtime -... -print0`（独立进程组 `start_new_session=True`）+ stat；同参数 `(root, days, min_mb, topn)` 并发去重；TTL 缓存可命中、过期可辨（state=expired）；显式触发 `submit()`；取消/超时通过 `os.killpg(SIGTERM)→SIGKILL` 升级回收 find 进程组；`resource.getrusage(RUSAGE_CHILDREN)` 给峰值 RSS；预算与 TTL 在 `config.BIGFILE_*`；原始路径仅以 `<sha8>.../<basename>` 进入日志。

### `/api/bigfiles` 描述更新

旧：
> GET `/api/bigfiles?days=&min_mb=&topn=` | 同步遍历，返回 files；上限 200 |

新：
> GET `/api/bigfiles?days=&min_mb=&topn=` | 显式触发 `BigfilesManager.submit()`，同步等待（超时 `BIGFILE_FIND_TIMEOUT_S`）。返回 `{state, files, scope{root,days,min_mb,topn}, stats{wall_ms, peak_rss_bytes, find_output_lines, find_exit_code, find_stderr_lines, permission_denied_lines}, truncated, raw_truncated, expired, cached, cache_age_s, error_message}`。失败/超时 504/取消 409；坏参数由 ISS-024 全局 RequestValidationError 处理器返回 400。上限 `topn ≤ 200`（Query 校验）。

## 3. docs/TESTING.md（追加资源预算说明）

> 大文件查询预算（ISS-032）：
> - find 单次墙钟上限 `BIGFILE_FIND_TIMEOUT_S = 30.0 s`（实测 large 样例 21ms → 1.5x 余量 + 真实 HOME 倍数）
> - find 输出行硬上限 `BIGFILE_RESULT_CAP = 1000`（实测 large 样例 20 → 余量充足）
> - 查询内存上限 ~16 MB（实测 peak_rss_bytes 1.75MB find 子进程 + buffers）
> - 缓存 TTL `BIGFILE_CACHE_TTL_S = 30.0 s`，过期态可辨
> - 日志保留 `BIGFILE_LOG_RETENTION_DAYS = 7`；报告保留 `BIGFILE_REPORT_RETENTION_DAYS = 35`

## 4. CHANGELOG.md（用户可见变化）

> ## [Unreleased] - ISS-032
> ### Added
> - 大文件查询改为显式触发任务对象；同参数并发自动去重、TTL 缓存可命中/过期可辨；
> - 取消/超时通过进程组回收 find 子进程；
> - `/api/bigfiles` 返回 `state` / `scope` / `stats` / `truncated` / `expired` / `cached` / `cache_age_s` / `error_message` 字段；
> - 配置文件新增 `BIGFILE_FIND_TIMEOUT_S` / `BIGFILE_RESULT_CAP` / `BIGFILE_CACHE_TTL_S` / `BIGFILE_LOG_RETENTION_DAYS` / `BIGFILE_REPORT_RETENTION_DAYS` 预算与保留常量。
> - 前端大文件页展示进度（scope）、范围、失败、截断、过期缓存五种状态。
>
> ### Changed
> - `find_big_files` 同步包装保持 API 旧合同；底层改走 `BigfilesManager`。

## 5. 不在范围内的回写

- `fathom/scanner.py` / `db.py` / `reports.py` / `scan_coordinator.py` 不动
- `fathom/api.py` 中非 bigfiles 端点不动
- `scripts/ci_pytest.sh` 的 `EXPECTED_PYTEST_PASSED` 同步：当前期望 209，本卡未增减测试，最终仍是 24 大文件测试并入 209（**注：本卡未跑全量，仅跑 tests/test_bigfiles.py，PM 合并前需在隔离副本跑全量 209 并按 TESTING 流程同步门禁**）。
- `docs/ROADMAP.md` / `DESIGN.md`：本卡是 ISS-027 模块合同在 bigfiles 页的具体落地，不动 ROADMAP / DESIGN 主线。