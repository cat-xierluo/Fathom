#!/usr/bin/env bash
# ISS-031 API/浏览器 39 项检查入口（CI 与本地同一断言口径）；
# ISS-100 追加前端功能回归（verify_frontend_refresh，默认 160 项）——
# 同一 job 内两套检查依次执行，日志各自可辨认，任一失败 job 即红。
#
# 本地等价命令（39/39 沿用 ISS-031 已验证口径；refresh 基线 160 项为
# ISS-102 后的本机已验证通过数，CI 首跑以计数门禁实测对齐）：
#   node scripts/verify_api_security.cjs
#   node scripts/verify_frontend_refresh.cjs
# CI 前置（workflow 内完成，此处只做兜底）：
#   - FATHOM_PYTHON 指向锁定 venv；脚本用一个 exec 包装器把它桥接到
#     .runtime/bin/python
#     （verify_api_security.cjs 固定从该路径启动夹具服务，仓库内
#     .runtime 是本机符号链接，不入库，CI 克隆中不存在）。不能把 venv
#     的 python 二进制再软链到该路径：CPython 会按软链位置寻找
#     pyvenv.cfg，进而退回 runner 系统环境。verify_frontend_refresh.cjs
#     夹具为纯 Node（无 Python 依赖），不需要该包装器；
#   - NODE_PATH 指向安装了 playwright@<锁定版本> 的 node_modules（两套
#     检查共用）；
#   - PW_INSTALL=1 且提供 PLAYWRIGHT_BIN 时下载 chromium（仅 CI 冷环境）。
#
# 断言口径：两套结果 JSON 均必须 ok=true、failed=0、passed 等于各自期望
# （EXPECTED_BROWSER_PASSED 默认 39；EXPECTED_REFRESH_PASSED 默认 217；
# ISS-035C 起新增 analysis frontend 检查（EXPECTED_ANALYSIS_PASSED，ISS-128 后为 59，ISS-137 后为 77），
# ISS-108 增补 5 项至 172 后，ISS-106 总览层级/五态容器再增 7 项=179：
# ready 清态与次级图 1 + 空库等待 1 + 单快照等待 1 + 错误容器 1 +
# 净变化根差分 1 + 无基线不伪造零 1 + 980 首屏坐标 1；
# 既有 3 项总览断言（扫描主按钮/装饰刻度/表面层级）原地更新不改变计数；
# ISS-111 再增 9 项=188：原 091 监控权限卡 6 项断言（浏览器数字占比/
# 降级/mock 深链/无页错误/denied-over 倍数/空库引导）原地改写为权限分区
# 版本（6 → 14：三卡与徽章 1、granted 数字占比 1、通知徽章 1、降级 1、
# 后台计划引导 1、denied 三态 1、unknown 三态 1、监控交叉说明 1、交叉
# 前往 1、mock FDA 深链 1、mock 通知深链 1、无页错误 1、denied-over 1、
# 空库 1），另增关于页版本从 status 回填 1 项；
# ISS-113 再增 5 项=193：available+自动下载开断言原地改写（自动下载去向
# 一句话 + notes 收进「了解详情」折叠区 + 无手动安装入口 + 开关在场勾选，
# 计数不变），另增 5 项——开关关闭回 040B 现状（PUT /api/config 落 false
# +「下载并安装」手动入口出现）1、downloading 事件驱动后台下载呈现（字节
# 进度百分比 + 取消 + cancelled 终态后「重试下载」而非手动安装）1、重试
# 下载 = 重新检查回自动语境 1、downloaded（ready）一句话 + ready 区块 +
# 「安装（需重启）」确认层取消不发 updater_install 且 ready 保持 1、
# 确认安装 confirmed:true → preparing/installing → installed「重启以完成」
# 1；浏览器降级断言原地补「无自动下载开关」（计数不变））；
# ISS-115 再增 3 项=196：130 条错误行不冒充目录、仅路径校验未确认时不显示
# 0 个未读、当前探测与历史快照时间/计数分开展示。
# ISS-115 复审再增 2 项=198：无法访问路径也可能被归入 vanished，
# 总览与权限页均不能据此断言目录已消失。
# ISS-115 再将现有 3 项权限卡断言改为只验原始条数（低于/高于分母与零值），
# 移除行数/目录数的比例或倍数展示；ISS-116 再增新旧分类展示 2 项，
# 检查总数为 200；ISS-114 再增 8 项=208：980/1220 两视口各验证
# 边界策略、正常 wheel 滚动、顶部边界和底部边界壳坐标；原生手势仍需
# 单独实机证据；ISS-112 深链按钮 hidden 计算样式互斥断言再增 4 项=212；
# ISS-118 深链降级 fallback 镜像互斥断言再增 2 项=214。
# ISS-143 目录详情历史首/中/末点真实鼠标悬停再增 3 项=217。
# ISS-149 树形套件 +14：trend 锚定窗口/缺测 null/排除隔离/等价表格
# 键盘读数/真实悬停缺测标注/a-b 迟到响应不恢复旧曲线（60 → 74）。
# ISS-159 新增分布页显式快照绑定回归（verify_browse_snapshot_frontend，
# EXPECTED_BROWSE_SNAPSHOT_PASSED 默认 33）：真实 serve + 真实 Chromium，
# HTTP 层旧行为形态/历史实点/次级差分/404 语义 + 页面层快照选择/结构
# 节点/HTML 转义/单时点详情/恢复/分页/三视口。
# ISS-160 新增跨页上下文/恢复/重扫核对闭环回归
# （verify_storage_investigation_frontend，EXPECTED_INVESTIGATION_PASSED）：
# 真实 serve 入口全流程 + net0/历史非 latest/分页/数据集切换 + 快速切页
# 与故障不残留错读 + 键盘焦点/三视口/图表恢复 + mock 纪律对照。
# verify 脚本自身任一检查失败都会以非零退出（pipefail 直通，不走门禁
# 兜底），计数门禁只拦空跑与静默漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

expected="${EXPECTED_BROWSER_PASSED:-39}"
expected_refresh="${EXPECTED_REFRESH_PASSED:-217}"

if [ ! -x .runtime/bin/python ]; then
  requested_python="${FATHOM_PYTHON:-python3}"
  target="$(command -v "$requested_python")" || {
    echo "缺少可执行解释器: $requested_python" >&2
    exit 1
  }
  mkdir -p .runtime/bin
  {
    printf '#!/bin/bash\n'
    printf 'exec %q "$@"\n' "$target"
  } > .runtime/bin/python
  chmod 755 .runtime/bin/python
fi

# 回归门禁：隔离 PATH，确认硬编码的 .runtime 入口仍使用目标 venv，且
# fastapi 确实从该 venv 加载。旧的二次软链实现会在这里显示系统 prefix，
# 即使开发机的系统 Python 碰巧装了 fastapi 也不能通过。
target="${FATHOM_PYTHON:-.runtime/bin/python}"
target_prefix="$("$target" -c 'import os, sys; print(os.path.realpath(sys.prefix))')"
FATHOM_EXPECTED_PREFIX="$target_prefix" PATH="/usr/bin:/bin" \
  .runtime/bin/python -c '
import os
import sys
from pathlib import Path
import fastapi

expected = Path(os.environ["FATHOM_EXPECTED_PREFIX"]).resolve()
actual = Path(sys.prefix).resolve()
module = Path(fastapi.__file__).resolve()
assert actual == expected, f"fixture Python prefix 漂移: {actual} != {expected}"
assert module.is_relative_to(actual), f"fastapi 未从目标 venv 加载: {module}"
'

if [ "${PW_INSTALL:-0}" = "1" ]; then
  if [ -z "${PLAYWRIGHT_BIN:-}" ]; then
    echo "PW_INSTALL=1 需要同时设置 PLAYWRIGHT_BIN" >&2
    exit 1
  fi
  "$PLAYWRIGHT_BIN" install chromium
fi

out="$(mktemp)"
refresh_out="$(mktemp)"
analysis_out="$(mktemp)"
tree_out="$(mktemp)"
trap 'rm -f "$out" "$refresh_out" "$analysis_out" "$tree_out"' EXIT

# ISS-148：树形套件起真实 FastAPI，先预检解释器与 playwright 可用，
# 缺前置时明确报错而不是在套件内表现为模糊的等待超时。脚本消费
# FATHOM_PYTHON（同 CI pytest job 的注入口径），本机开发回退 .venv。
tree_python="${FATHOM_PYTHON:-$(pwd)/.venv/bin/python}"
if [ ! -x "$tree_python" ]; then
  tree_python="${FATHOM_PYTHON:-.runtime/bin/python}"
fi
[ -x "$tree_python" ] || { echo "树形套件前置缺失: 解释器不可用（.venv/.runtime 均缺，或设 FATHOM_PYTHON）" >&2; exit 1; }
export FATHOM_PYTHON="$tree_python"
tree_node_path="${NODE_PATH:-}"
if [ -n "$tree_node_path" ]; then
  tree_chromium_found=0
  IFS=':' read -r -a _pw_dirs <<< "$tree_node_path"
  for _d in "${_pw_dirs[@]}"; do
    if [ -d "$_d/playwright-core" ] || [ -d "$_d/playwright" ]; then tree_chromium_found=1; break; fi
  done
  [ "$tree_chromium_found" = "1" ] || { echo "树形套件前置缺失: NODE_PATH 中无 playwright 包（CI 先 PW_INSTALL=1）" >&2; exit 1; }
fi

node scripts/verify_api_security.cjs | tee "$out"

# 两套检查同一门禁：结果 JSON 必须 ok=true、failed=0、passed==期望，
# 日志行带各自标签（browser checks / frontend refresh）便于 CI 辨认。
assert_result_json() {
  node -e '
    const fs = require("fs");
    const j = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const expected = Number(process.argv[2]);
    if (j.ok !== true || j.failed !== 0 || j.passed !== expected) {
      console.error(`期望 ${expected} 项通过且 0 失败，实际 ok=${j.ok} passed=${j.passed} failed=${j.failed}`);
      process.exit(1);
    }
    console.log(`${process.argv[3]}: ${j.passed} passed (expected ${expected})`);
  ' "$1" "$2" "$3"
}
assert_result_json "$out" "$expected" "browser checks"

# ISS-100：前端功能回归接入。verify_frontend_refresh.cjs 自带纯 Node 合成
# 夹具与前端静态文件服务，随机端口、无 Python 依赖；playwright 走同一
# NODE_PATH，chromium 复用 PW_INSTALL 已装好的那份。任一检查失败脚本
# 自身非零退出，pipefail 直通判红。
node scripts/verify_frontend_refresh.cjs | tee "$refresh_out"
assert_result_json "$refresh_out" "$expected_refresh" "frontend refresh"

# ISS-035C：AI 解读前端回归（同 refresh 机制：纯 Node 合成 API + Playwright，
# 随机端口）。70 项——七态/检测列表/授权层/世代守卫/幂等/HTML 转义/双视口；
# ISS-126 起 codex/hermes 披露断言；ISS-128 起 +7 项幂等键生命周期与重放终态；审查后 +3 项重入终态标记（59 项）；ISS-135 起 +7 项跨会话在途发现/区间守卫/查询失败/取消 = 70 项。
# ISS-137 起 +7 项已受理 POST 响应丢失：如实报错回 idle、重试复用同键单派发、
# 重放 running 可取消、终态重跑新键、在途 reload 查询恢复不再 POST = 77 项。
# 失败自身非零退出，pipefail 直通判红；计数漂移由 EXPECTED_ANALYSIS_PASSED
# 兜底（同 refresh 门禁口径）。
node scripts/verify_analysis_frontend.cjs | tee "$analysis_out"
expected_analysis="${EXPECTED_ANALYSIS_PASSED:-77}"
assert_result_json "$analysis_out" "$expected_analysis" "analysis frontend"

# ISS-148：树形同级变化回归（真实 FastAPI 隔离入口 + 生产页面实点）。
# 53 项——三层展开/聚焦/返回、父 0 子抵消、缺父结构节点、单侧未记录、
# 分页加载更多、错误重试、a/b 与路径竞态、键盘可达、HTML 路径安全、
# 三视口无横向溢出、AI 区与排行/日报次级可达。失败自身非零退出，
# pipefail 直通判红；计数漂移由 EXPECTED_TREE_PASSED 兜底（同上口径）。
FATHOM_PYTHON="$tree_python" node scripts/verify_tree_changes_frontend.cjs | tee "$tree_out"
expected_tree="${EXPECTED_TREE_PASSED:-74}"
assert_result_json "$tree_out" "$expected_tree" "tree changes frontend"

# ISS-151：目录当前大文件回归（真实 FastAPI 隔离入口 + 生产页面实点）。
# 36 项——三层历史定位→largest/recent 切换、双目录并发取消不误伤、
# 离开按句柄真实取消、移动 404 保历史、权限/空/截断、路径转义、键盘、
# 三视口。失败自身非零退出，pipefail 直通判红；计数漂移由
# EXPECTED_DIR_BIGFILES_PASSED 兜底（同树形口径）。
FATHOM_PYTHON="$tree_python" node scripts/verify_directory_bigfiles_frontend.cjs | tee "$tree_out"
expected_dir_bigfiles="${EXPECTED_DIR_BIGFILES_PASSED:-36}"
assert_result_json "$tree_out" "$expected_dir_bigfiles" "directory bigfiles frontend"

# ISS-159：分布页显式快照绑定回归（真实 FastAPI 隔离入口 + 生产页面实点）。
# 33 项——HTTP 层（旧行为形态钉住/历史实点/次级差分显式/404 语义/跨根
# 400/分页稳定）+ 页面层（快照选择器驱动两分区/结构节点不填 0/HTML 路径
# 转义/distribution 单时点详情与下钻/404 一键恢复/分页追加/三视口无横向
# 溢出）。失败自身非零退出，pipefail 直通判红；计数漂移由
# EXPECTED_BROWSE_SNAPSHOT_PASSED 兜底（同树形口径）。
FATHOM_PYTHON="$tree_python" node scripts/verify_browse_snapshot_frontend.cjs | tee "$tree_out"
expected_browse_snapshot="${EXPECTED_BROWSE_SNAPSHOT_PASSED:-33}"
assert_result_json "$tree_out" "$expected_browse_snapshot" "browse snapshot frontend"

# ISS-156：设置「范围与覆盖」分区 + 首次启用引导回归（真实 FastAPI 隔离入口
# + 生产设置页实点）。37 项——HTTP 合同（旧 HOME 未启用/预览无副作用/409 冲突
# 旧值不动/400 不落盘/保存只影响下一轮）+ 页面合同（生效范围与修订可读、
# 发现≠已监控、预览身份/读取限制/耗时不确定、保存成功才变更 UI、首扫为独立
# 动作、单快照等第二个可比日期、旧 HOME legacy 保留、保存不触发扫描、
# 409/500 失败旧值可辨、预览失败/发现失败不谎报、锁定与未挂载卷不可选、
# 其它卷独立入口、排除编辑器与 AI 授权/更新恢复无退化、键盘焦点、三视口、
# 无页错误、端口释放）。失败自身非零退出，pipefail 直通判红；计数漂移由
# EXPECTED_SCOPE_SETTINGS_PASSED 兜底（同树形口径）。
FATHOM_PYTHON="$tree_python" node scripts/verify_scope_settings_frontend.cjs | tee "$tree_out"
expected_scope_settings="${EXPECTED_SCOPE_SETTINGS_PASSED:-37}"
assert_result_json "$tree_out" "$expected_scope_settings" "scope settings frontend"

# ISS-158：整盘总览与变化入口回归（真实 FastAPI 隔离入口 + 生产总览页实点）。
# 57 项——两个隔离夹具：clean（两侧可比：容器 +26MB / 目录 +20MB ⇒ 未知差额
# 6MB，两卷共享 free 不翻倍，重叠测量根去重）与 stale（本轮失败成员 ⇒ 旧值
# stale、差额 null 不可比、不出现数值）。失败自身非零退出，pipefail 直通判
# 红；计数漂移由 EXPECTED_STORAGE_OVERVIEW_PASSED 兜底（同树形口径）。
FATHOM_PYTHON="$tree_python" node scripts/verify_storage_overview_frontend.cjs | tee "$tree_out"
expected_storage_overview="${EXPECTED_STORAGE_OVERVIEW_PASSED:-65}"
assert_result_json "$tree_out" "$expected_storage_overview" "storage overview frontend"

# ISS-160：跨页上下文/恢复/重扫核对闭环回归（真实 FastAPI 隔离入口 + 生产页面实点）。
# 真实 serve 入口全流程（总览→树展开→绑定历史详情→largest→返回→分布→设置→重扫）
# + net0 内部变化/历史非 latest/分页/旧 HOME·新整盘切换 + 快速切页/改范围/重复
# 请求/500/取消/同日替换/淘汰不残留错读 + 键盘与焦点返回/三桌面尺寸/图表隐藏恢复
# + mock 仅注入受控故障。mock 纪律：page.route 只注入受控故障/系统动作，且每处
# 都有撤除后真实 /api 端点恢复的对照断言；生产模块与 API 调用不被替身取代。
# 失败自身非零退出，pipefail 直通判红；计数漂移由
# EXPECTED_INVESTIGATION_PASSED 兜底（同树形口径）。
FATHOM_PYTHON="$tree_python" node scripts/verify_storage_investigation_frontend.cjs | tee "$tree_out"
expected_investigation="${EXPECTED_INVESTIGATION_PASSED:-55}"
assert_result_json "$tree_out" "$expected_investigation" "storage investigation frontend"
