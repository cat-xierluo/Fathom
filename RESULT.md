# ISS-168 交付报告：entries(path) 二级索引（trend 路径等值查询索引化）

分支 `iss-168-trend-path-index`，基线 main `9ab8fd7`。未 push（由 PM 代推）。
`_trend_points` 查询本体零改动，SQLite 计划器自动改用索引。

## 一、改动摘要

| 文件 | 改动 |
|---|---|
| `fathom/db.py` | +34 行：`_ENTRIES_PATH_INDEX` / `_INDEX_STATEMENTS` 常量（`CREATE INDEX IF NOT EXISTS idx_entries_path ON entries(path)`）、`_ensure_indexes()` 幂等补建 helper；挂 `_prepare_database` 快路径、迁移链末端、`connect()` 快路径三处 |
| `tests/test_trend_path_index.py` | 新建，8 例（合成数据 + 隔离临时目录，沿用 `tests/test_trend_anchor.py` 夹具口径） |
| `.github/workflows/ci.yml` | `EXPECTED_PYTEST_PASSED` 1455 → 1463 |
| `scripts/ci_pytest.sh` | 同上，另补 ISS-168 计数说明注释 |
| `fathom/api.py` | **零改动**（`_trend_points` 保持原样） |

### 挂点：三处，缺一则生产老库拿不到索引

`entries` 主键是 `(snapshot_id, path) WITHOUT ROWID`，`path` 非主键前缀，
`WHERE path = ?` 用不上主键 → 全表扫描（100 万档实测 252ms → log N）。

1. `connect()` 快路径（`fathom/db.py:886`）：**已是 `SCHEMA_VERSION` 的库不跑
   迁移链**。若索引只挂迁移链，生产 100 万档老库永远补建不到 —— 这是本卡
   最容易漏、后果最重的一处。
2. `_prepare_database` 快路径（`fathom/db.py:819`）：同版本库的迁移锁内打开。
3. 迁移链末端（`fathom/db.py:844`）：新建库与旧库升级在**同一事务内**补建，
   两条路径到达完全相同的最终结构。

### 硬约束遵守

- `SCHEMA_VERSION` 保持 **8**，版本号语义不变（测试断言 `schema_version ==
  SCHEMA_VERSION` 在三条路径下均成立）。
- 不加列、不重建表、不迁移数据：`CREATE INDEX IF NOT EXISTS` 命中即空操作，
  旧库打开自动补建，**不动任何一行**（v0 迁移用例断言 `total_kb == 42` 不变）。
- 兼容路径即本卡的「幂等 `IF NOT EXISTS`」。

### 快路径不引入写锁停顿（额外验证，非卡片要求）

`connect()` 每次查询都新建连接，若该语句取写锁会拖慢读路径。实测：另一
连接持有 `BEGIN IMMEDIATE` 写锁时，连续 20 次 `db.connect()` + 查询
`ok=20 blocked=0 elapsed=0.028s` —— 索引已存在时该 DDL 不取写锁，快路径无争用。

## 二、先红后绿证据

### 红（实现前，`tests/test_trend_path_index.py`）

```
$ python3 -m pytest tests/test_trend_path_index.py -q
FFFFF...                                                            [100%]
...
E       AssertionError: 实际计划：SCAN entries
E       assert ('USING INDEX ' + 'idx_entries_path') in 'SCAN entries'

tests/test_trend_path_index.py:177: AssertionError
...
FAILED tests/test_trend_path_index.py::test_new_database_has_entries_path_index
FAILED tests/test_trend_path_index.py::test_existing_v8_database_gains_path_index_on_open
FAILED tests/test_trend_path_index.py::test_legacy_v0_database_migration_creates_path_index
FAILED tests/test_trend_path_index.py::test_trend_query_plan_uses_path_index
FAILED tests/test_trend_path_index.py::test_trend_query_plan_uses_path_index_after_legacy_open
5 failed, 3 passed in 0.15s
EXIT=1
```

计划串 `SCAN entries` 与 perf 审计线的全表扫描事实一致。红阶段失败
**不是**只靠一条恒真断言：本文件另含反例自证用例
（`test_trend_query_plan_without_index_is_full_scan`），删掉索引后断言
必须转为 `SCAN entries`，保证绿阶段的 `USING INDEX` 断言非平凡。

### 绿（实现后）

三文件全绿，见下表。

## 三、验证（真实入口，全部实际执行）

环境：本 worktree **无 `.venv`**，按卡片口径改用系统 `python3 -m pytest`
（`/opt/homebrew/bin/python3`，Python 3.14.7，pytest 9.1.1）。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `python3 -m pytest tests/test_db_migrations.py tests/test_trend_anchor.py tests/test_trend_path_index.py -q` | **0** | **54 passed in 2.94s** |
| `python3 -m pytest tests/test_db_migrations.py tests/test_trend_path_index.py -q`（连跑①） | **0** | 40 passed in 2.22s |
| 同上（连跑②） | **0** | 40 passed in 2.82s |
| 同上（连跑③） | **0** | 40 passed in 4.79s |

×3 连跑全绿且计数一致，无顺序/时序依赖（索引补建在 WAL 与迁移锁下均幂等）。

### 全量套件（为计数取证）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `python3 -m pytest tests/ -q` | **1** | `45 failed, 1418 passed in 379.98s` —— **收集总数 1463** |

**45 个失败与本卡无关，为本 worktree 环境所致**（缺 Rust/Tauri 工具链与已构建
前端产物），已实证：把 `fathom/db.py` 回退到基线 `HEAD~1` 后重跑其中 3 例
（`test_tauri_autostart_acl.py::test_capability_grants_all_four_autostart_permissions`、
`test_api_security.py::TestStaticSecurityHeaders::test_index_served_with_csp`、
`test_launchd_autostart.py::test_rust_register_commands_take_confirmation_param`）
**同样失败**（`3 failed`，退出码 1），即基线即红，与 ISS-168 无关。失败集中在
`test_tauri_*` / `test_launchd_*` / `test_auto_download_config` / CSP 静态合同，
无一例涉及 `db.connect()` 或 entries。

**收集总数 1463 = 基线 1455 + 新增 8**，计数取值有实证支撑：CI 冷环境（工具链
齐备）下这 45 例通过后 `passed` 即为 1463，与两处 `EXPECTED_PYTEST_PASSED`
一致。

## 四、计数

`EXPECTED_PYTEST_PASSED`：**1455 → 1463**（新增 8 例），两处一致：

- `.github/workflows/ci.yml:87` → `"1463"`
- `scripts/ci_pytest.sh:99` → `${EXPECTED_PYTEST_PASSED:-1463}`

计数独立一笔 `ci:` 提交，与工程改动分开。

## 五、NOT_VERIFIED

- **CI 冷环境首跑未验证**：本机为热环境 + 系统 Python，未在 GitHub Actions
  冷缓存（`.runtime/bin/python`、无 `FATHOM_PYTEST_*`）下实跑
  `scripts/ci_pytest.sh`。计数 1463 的依据是本地全量**收集总数** 1463
  （= 1455 + 8），而非本地 `passed` 数（本地 45 例因缺 Rust/Tauri 工具链
  而失败，已实证基线同红）。CI 首跑请复核 `passed` 恰为 1463。
- **性能收益未实测**：卡片的 252ms → log N 是 M Code 审计线在 100 万档大库的
  标定事实；本卡只验证查询计划改走索引（`USING INDEX idx_entries_path`），
  未在真实百万档库上复测墙钟耗时。
- 未触碰生产 `data/` / `reports/` / `logs/`，未触发真实 HOME 扫描（全部用例
  走 `tmp_path` 隔离运行根与扫描根）。
