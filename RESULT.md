# ISS-177 交付报告：bigfiles 查询范围语义跟随生效范围

基线 `3760ac3`（0.4.0 发版文档对账）。分支 `iss-177-bigfiles-scope-semantics`。中文提交、无署名、未 push。

## 根因（上游反例已复现确认）

`fathom/api.py` 的 `/api/bigfiles` 仍调用 `bigfiles.resolve_query_root(path)`，而该函数**只认 `config.DEFAULT_ROOT`**。#270（ISS-176）的 `scope_key` 只把范围身份并入去重/缓存键，**没有改变实际查询范围**，因此：

1. 显式 `path` 指向生效范围内、旧根之外的目录 → 400「必须位于旧根 A」；
2. 无 `path` 请求 → 200，但 `scope.resolved_root` 仍是旧根 A，静默继续查询旧根。

既有 `tests/test_plan_identity_consumers.py:173` 只断言无路径请求的状态码、未核对查的是哪个目录，故漏过。

## 修法

### 1. `fathom/bigfiles.py`：解析层支持多根生效范围

- 新增 `resolve_query_root(..., allowed_roots=None)` 关键字参数。
- `allowed_roots` 非空 → 走新增的 `_resolve_within_roots()`：逐个生效根判定，无 path 取**首根**，越界信息指认生效范围。
- `allowed_roots` 为 `None`/空 → **逐字节回落** ISS-150 单根旧口径（含原错误文案 `监控根 … 之内的绝对路径`），legacy 行为与去重键不变。
- 守卫强度不降：字符串层挡相对路径与前缀同名根（`/root-evil` 不是 `/root`），resolve 层挡 `..` 折叠与符号链接越界，目标不存在/非目录仍 404（与越界 400 不混淆）。错误信息只列生效根本身，不回显解析后的用户路径。

### 2. `fathom/api.py`：端点按生效范围校验并如实反映

- 新增 `_bigfiles_allowed_roots()`：读 `config.effective_scope_selection()`，返回按选择顺序的根列表；未启用返回 `None`（legacy）。
- 端点把 `allowed_roots` 传入解析层。
- 响应字段如实反映**实际查询范围**：`scope.root` 改为生效范围首根（未启用时仍是 `DEFAULT_ROOT`）、`scope.resolved_root` 为真实查询根、新增 `scope.scope_roots`（未启用时为 `null`）。
- 端点 docstring 补 ISS-177 段落写明缺省语义。

## 范围语义取舍（ISS-155 一致性）

**选定：范围启用时，无 `path` 的缺省查询根 = 生效范围的第一个根（`roots[0]`）。**

理由（与 ISS-155 配置保存语义最一致）：`ScopeSelection.roots` 的顺序在 ISS-155 里就是**采集顺序**（"顺序即采集顺序"，`_validated_scope_roots` 与 docstring 均如此定义），首根即主根；`save_scope_selection` 与 `_plan_preview` 都按该顺序解释范围。选首根让 bigfiles 的缺省查询与扫描计划的第一个成员同源，不引入第二套"主根"判定。

**已排除的备选**：把缺省设为「多根聚合查询」——bigfiles 是单根聚合端点（`find` 单根执行、结果集单根），聚合会改变结果集口径与去重键语义，属更大设计变更，超出本卡修法范围。`scope.scope_roots` 字段已如实下发完整根集，前端可据此显式选择要查的根。

## 测试

**新增用例 13 个**（`tests/test_bigfiles_scoped.py`，diff 核对）：
- `TestEffectiveScopeQueryRoot`（7 个，HTTP 端点层）：①范围内新根可查且返回**该根**文件（夹具在旧根放诱饵文件）；②多根各自可查且只返回自己的文件；③符号链接越界拒绝；④无 path 的 `resolved_root` == 首根且**不含旧根文件**；范围外 path 拒绝且信息指认生效范围；⑤legacy 两条（无 path 仍查旧根、`scope_roots` 为 null；旧根外 path 仍按旧文案 400）。
- `TestResolveQueryRootAllowedRoots`（6 个，单元层）：首根缺省、前缀同名根/`..`/symlink 拒绝、404、空 roots 回落 legacy。

**升级既有断言 1 处**（`tests/test_plan_identity_consumers.py:173`）：从"只断言状态码"升级为核对 `resolved_root` 落在已选根内 + 显式 path 返回的文件确属该根 + 无 path 请求的 `resolved_root` 不等于旧根。

**顺带修隔离缺陷 1 处**（同文件 `_isolated_runtime`）：本文件用 `PUT /api/storage/scope` 真实保存范围，若沿用进程级 `config._USER_SETTINGS`，保存结果会**跨文件泄漏**污染随后运行的用例（实测污染 `test_bigfiles_scoped` 的 `scope.root`）。已按 `conftest.isolated` 的既有做法在夹具里逐测试归零。

断言强度：所有新用例均核对**返回文件归属**与 `resolved_root`，不只状态码。

## 验证（真实执行）

```
python3 -m pytest tests/test_plan_identity_consumers.py tests/test_bigfiles_scoped.py -q   # ×3
python3 -m pytest tests/ -q -k "bigfiles or scope"
```

- 三文件 ×3 全绿（稳定复跑，无 flake）。
- 定向 `-k "bigfiles or scope"` 全绿。

## 差异说明（供 PM 核对）

- `tests/test_directory_bigfiles_frontend.py` **在本仓不存在**（`ls tests/ | grep -i "front\|director\|bigfile"` 只有 `test_bigfiles_scoped.py`、`test_bigfiles_task_handle.py`、`test_bigfiles.py`）。该文件为上游卡里列出的验证路径，本仓无对应物，故实际执行了等价的两文件 ×3。
- 未改 `CHANGELOG`/文档（写域之外）。
- `scope` 响应新增 `scope_roots` 字段：未启用范围时为 `null`，旧前端不读该键不受影响。
