#!/usr/bin/env bash
# ISS-031 可复现 pytest 入口（CI 与本地同一断言口径）。
#
# 本地等价命令（ISS-126 hermes 轮后 1051 passed = 1033 + 18(hermes 适配器)；ISS-126 后 1033 passed = 1014 + 19(codex 适配器)；ISS-123 后 1014 passed = 1013 + 1(tests/test_bigfiles.py spawn 窗口取消)；ISS-120 后 1013 passed = 999 + 14(tests/test_analysis_jobs_lookup.py)；ISS-035B 后 999 passed = 921 + 78(analysis_manager 45+api 18+upgrade_gate 3+seam 若干+db_migrations v7 5+同名修复+1，见 #204/#206 审查记录)；ISS-119 后 921 passed = 920 + 1(tests/test_agent_runtime.py EPERM 注入)；ISS-035D 后 920 passed = 871 + 49(tests/test_analysis_contract.py)；ISS-035A 后 871 passed = 822 + 49(tests/test_agent_runtime.py)；ISS-110 后 822 passed = 814 + 8(tests/test_upgrade_external_instance.py)；ISS-116 后 814 passed = 800 + 14；ISS-113 后 800 = 781 + 19；781 = 771 + 10(tests/test_api_permissions.py)；771 = 766 + 5(tests/test_scan_runs.py)；766 = 753 + 13(tests/test_release_gate.py)；753 = 739 + 14(tests/test_upgrade_restore.py)；739 = 727 + 12(ISS-097)，727 = 720 + 7(ISS-096)）：
#   .runtime/bin/python -m pytest tests -q
# CI：FATHOM_PYTHON 指向 setup-python 锁定版本创建的 venv 解释器。
#
# 断言口径：
#   - fathom 源码必须来自当前工作区（TESTING 的防误测要求）；
#   - pytest 非零退出（failed/error/收集失败）经 pipefail 直接判失败；
#   - 通过数必须等于 EXPECTED_PYTEST_PASSED（默认 1426）；计数变化必须
#   - 通过数必须等于 EXPECTED_PYTEST_PASSED（默认 1340）；计数变化必须
#     显式同步本默认值与任务证据，不允许静默漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

# ISS-091 +2：tests/test_api_status_coverage.py 锁定 /api/status
# latest_snapshot 的 dir/denied/vanished 字段契约（697 → 699）；
# ISS-090 +21：tests/test_scan_progress.py 扫描进度流式计数/状态文件/
# live 判活（699 → 720）；
# ISS-096 +7：tests/test_upgrade_helper_exit.py helper 正常退出后实例文件
# 清理的兼容（起始无实例/退出自清两条成功路径 + 损坏/未退出/端口未释放/
# 身份不符四条保守拒绝路径）（720 → 727）；
# ISS-097 +12：tests/test_upgrade_txn_journal.py 升级事务持续停写
# （prepared/installing/installed 各阶段 start_scan 拒绝 + API 409 +
# CLI exit 3 + 恢复后可写）、journal 所有权（重入拒绝字节不变/txn_id
# 唯一/损坏与旧格式 fail-closed + 显式恢复入口）、中断恢复链与 lib.rs
# 独占门/有界等待 Python 钉子（727 → 739）；
# ISS-098 +14：tests/test_upgrade_restore.py 安装后失败的真实旧 bundle
# 与数据恢复——恢复材料合同（③b 落盘/磁盘预算替换前拒绝/--helper-dir
# 缺省跳过）、修前反例转正（N+1 替换后 rollback 恢复旧版文件）、恢复
# 失败明确报错保留材料（形态/身份两层拦截 + 修复后接续）、恢复途中再次
# 中断接续、正常升级材料保留至 finalize 才清、迁移失败闭环（db_verify_
# failed → 停 N+1 → 从备份恢复库，CLI 链与 run_full 建模双钉）、三类
# 故障注入（启动失败/身份不符/迁移失败真实进程链）与 lib.rs 恢复合同
# 钉子（739 → 753）；
# ISS-101 +13：tests/test_release_gate.py 发行门聚焦单测——必需 job 清单
# 与真实 ci.yml 一致（6 实例逐字）+ 矩阵展开与缺 name 回退、解析
# fail-closed 三反例、候选登记/制品指纹四反例（更换/缺制品/commit 不符/
# manifest 版本不符）+ manifest 非法 JSON、非 git 目录阻塞、--selftest
# 全绿（753 → 771）。
# ISS-111 +10：tests/test_api_permissions.py 权限状态端点契约——FDA 探测
# 三态（可读 granted / PermissionError denied / 探测异常 unknown）+
# notification 透传/未登记/空历史 + coverage 引用与空库 + /api/status
# 的 app_version 增量字段 + 外站 Host 403 守卫（771 → 781）。
# ISS-113 +19：应用更新无感化配置项——tests/test_auto_download_config.py
# 14 项（严格布尔校验 6 参数 + 双布尔接受/默认视图 true+来源/落盘往返
# false/排除集环境覆盖透传/部分合并不动其余键 + lib.rs 三钉子：读
# settings.json 的 auto_download_updates 键与宽读 fail-safe、downloaded
# ready 事件与预下载入口/复用下载路径与独占门、安装与重启 confirmed
# 确认门不回退）+ tests/test_api_config.py 5 项（PUT true/false 往返 +
# 近亲形态 4 参数 400 且旧值不动）（781 → 800）。
# ISS-116 +14：errno 路径分类、v5→v6 迁移/API/日报/通知，
# 长路径通知覆盖质量优先保留反例。
# ISS-147 +38：tests/test_diff_children.py 绑定历史区间的同级差分
# /api/diff/children——任务卡三反例红测（Top25 折叠丢父级 / 父净 0 被
# 净变化筛选漏 / browse 时点错位）+ 分页游标绑定 + 四态行/结构节点 +
# root=/ 与相似前缀段边界 + HTML/换行目录名 + 参数与数据集错误语义 +
# 同级三序（1109 → 1147）。
# ISS-152 +29（28+返修 B-1 降级用例 1）：tests/test_storage_discovery.py
# 启动盘容器与卷发现适配器（共享 free 唯一权威/UUID 稳定身份/归属
# 去重/降级三态）（1147 → 1176）。
# ISS-150 +32：tests/test_bigfiles_scoped.py——限定目录的 largest/recent
# 模式与范围约束（两反例 + 越界/竞态/去重/预算披露）（1176 → 1208）。
# ISS-149 +14：tests/test_trend_anchor.py——trend 锚定/缺测窗口/排除
# 隔离（反例①②③红测先行 + 兼容钉住）（1219 → 1233）。
# ISS-164 +20：tests/test_bigfiles_task_handle.py——大文件查询任务句柄与
# 取消产品入口（句柄确定性 2 + manager 同键去重/取消终态/未知句柄 2 +
# API 提交即得句柄与同参同句柄、旧式 409 附句柄 3 + 取消至 cancelled 与
# 进程组回收 1 + 双任务互不误伤/幂等/终态如实/保留期 404 4 + 状态对齐与
# 404/400 2 + 写令牌 403、坏 body 400、读端点免令牌 3 + 旧式阻塞调用逐
# 字段兼容钉住 1；tests/test_api_security.py 仅白名单登记，用例数不变）
# （1233 → 1253）。
# ISS-153 +20：tests/test_db_migrations.py v8 迁移七路（全新库/v7 升级
# 逐项保留/注入失败回滚/锁竞争/幂等/零版本检测/legacy NULL 身份）+
# tests/test_storage_dataset_identity.py 身份隔离十三路（换卷/换 plan
# 不可比、同容器不同卷分组、同日替换/保留按计划隔离、淘汰 expired 与
# AI 证据不级联、轮次/容量样本辅助）（1253 → 1273）。
# ISS-159 +21：tests/test_browse_snapshot.py——browse 显式快照绑定（旧
# snapshot 实点不被 latest 覆盖/单快照与前驱跨口径差分未知/次级差分基线
# 显式/多卷同路径身份约束/缺父结构导航与 404/稳定分页与游标绑定/root=/
# HTML 字符路径/质量字段/趋势点身份/旧行为响应形态钉住）（1297 → 1318）。
expected="${EXPECTED_PYTEST_PASSED:-1407}"
# ISS-154 +59：tests/test_scan_round_coordination.py 一轮多范围五份合同
# （多范围协调/去重/顺序/钉住口径、成员阶段与轮次状态同源推导、时间跨度
# 非原子、容量采样接线与无 statvfs 替补、报告命名隔离与轮次通知措辞、
# 取消首中末与 spawn 竞态、CLI 显式多范围与旧 --root 兼容）（1281 → 1340）。
# ISS-157 +18：tests/test_storage_summary.py——容器容量与目录归因摘要（共享
# free 只计一次/父子根不可加/差额带限制且有符号/不可比为 null/失败成员标
# stale/整轮跨时间/不算伪覆盖率/不隐式扫描）（1426 → 1444）。
# ISS-157 B3 +4：tests/test_storage_summary.py 成员级容器绑定——混容器成员与
# container_id=NULL 成员均不得判可比（差额 null + 点名原因）、全成员同属
# 所选容器保持可比防过紧、归因内 comparable_to_previous 与顶层权威结论
# 一致（1451 → 1455）。
# ISS-168 +8：tests/test_trend_path_index.py——entries(path) 二级索引
# （EXPLAIN QUERY PLAN 必走 idx_entries_path、缺索引必为全表扫描的反例
# 自证、新建库/既有 v8 库打开/v0 迁移三条路径均补建、纯索引不改版本号
# 语义与既有行、查询语义不变、重复打开幂等）（1455 → 1463）。
# ISS-187 +17（含episode1根/父fd替换3项）：冻结完整前端图/版本命名空间边界/实际loader及manifest安全。
expected="${EXPECTED_PYTEST_PASSED:-1592}"
py="${FATHOM_PYTHON:-.runtime/bin/python}"

if [ ! -x "$py" ]; then
  echo "缺少可执行解释器: ${py}（本地用 .runtime/bin/python，CI 设 FATHOM_PYTHON）" >&2
  exit 1
fi

"$py" -c 'import os, fathom; src = os.path.realpath(fathom.__file__); here = os.path.realpath(os.getcwd()); assert src.startswith(here + os.sep), f"误测别处源码: {src}"'

out="$(mktemp)"
trap 'rm -f "$out"' EXIT
# pipefail：pytest 自身非零退出（失败/错误/收集失败）在此处直接判失败。
"$py" -m pytest tests -q | tee "$out"

# 只认最终摘要行的通过数；空库误跑（"no tests ran"）会因拿不到计数而失败。
passed="$(grep -oE '[0-9]+ passed' "$out" | tail -1 | grep -oE '[0-9]+')"
if [ "$passed" != "$expected" ]; then
  printf '%s\n' "pytest 通过数 ${passed} != 期望 ${expected}；计数变化需同步 EXPECTED_PYTEST_PASSED 并在任务卡留证据" >&2
  exit 1
fi
printf 'pytest: %s passed (expected %s)\n' "$passed" "$expected"
