#!/usr/bin/env bash
#
# ISS-009 切片 1 · 生产 helper 冻结（PyInstaller onedir）
#
# 目标：把生产 fathom CLI 冻结为 ``fathom-helper`` 目录，让 Tauri bundle
# ``resources/helper/fathom-helper/`` 路径下能找到 ``fathom-helper`` 可执行
# 文件。冻结方式与 ISS-029 验证一致：onedir，pin pyinstaller 6.22.3。
#
# 输入：
# - VENV  : PyInstaller 6.22.3 的 venv（ISS-029 预置；本脚本只 freeze，不安装）
# - REPO_ROOT : 仓库根（含 main.py / fathom/ / frontend/）
#
# 输出：
# - REPO_ROOT/apps/desktop/src-tauri/resources/helper/fathom-helper/fathom-helper（可执行）
# - 同目录 ``_internal``（PyInstaller onedir 内部资源）
#
# 校验（按合约）：
# 1. file(1) 报告 Mach-O arm64（仅在 arm64 宿主上；x86_64 留 ISS-041）
# 2. ./fathom-helper --version 退出码 0，单行 JSON 含 service=fathom / protocol_version=1 / version=0.3.0
# 3. 冻结树 resources/helper/fathom-helper/ 内不包含 data/reports/logs
#    （生产代码已用 FATHOM_RUNTIME_DIR 隔离；本脚本只断言冻结产物目录无
#    默认运行时写入，但生产代码会在运行根写 data/reports/logs——这条断言
#    只针对 helper/ 目录本身，而非运行根）
#
# 用法：
#   bash scripts/build_helper.sh [VENV_PATH]
# 退出码：
#   0 成功
#   1 冻结/校验失败
#   3 依赖 venv 未就绪（BLOCKED）
#
# bash 3.2 兼容。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${1:-$ROOT/apps/desktop/experiments/iss029/.venv-build}"
PIN_PYINSTALLER="$(sed -n 's/^pyinstaller==//p' "$ROOT/requirements-runtime-build.txt" | head -1)"
[ -n "$PIN_PYINSTALLER" ] || PIN_PYINSTALLER="6.22.3"
OUT_DIR="$ROOT/apps/desktop/src-tauri/resources/helper/fathom-helper"
WORK_DIR="$ROOT/apps/desktop/src-tauri/resources/helper/work"
MAIN_PY="$ROOT/main.py"

mkdir -p "$OUT_DIR" "$WORK_DIR"

PIN_PY="$VENV/bin/pyinstaller"
PIN_PYTHON="$VENV/bin/python"
if [ ! -x "$PIN_PY" ]; then
  echo "[build_helper] BLOCKED：未找到 PyInstaller venv，请确认 $VENV/bin/pyinstaller 存在" >&2
  exit 3
fi
GOT_VERSION="$("$PIN_PYTHON" -c 'import PyInstaller; print(PyInstaller.__version__)' 2>/dev/null || echo unknown)"
if [ "$GOT_VERSION" != "$PIN_PYINSTALLER" ]; then
  echo "[build_helper] BLOCKED：PyInstaller 版本 ${GOT_VERSION} ≠ pin ${PIN_PYINSTALLER}" >&2
  exit 3
fi

# 清理上次产物；保留 .gitkeep 不写
rm -rf "$OUT_DIR"/fathom-helper "$OUT_DIR"/_internal "$OUT_DIR"/build

# ISS-029 G1：freeze_entry.py 已通过 from fathom.cli import main 触发对象
# 导入；本脚本直接 freeze main.py 即可，不再用 freeze_entry 中转。
"$PIN_PY" --noconfirm --clean --onedir \
  --name fathom-helper \
  --paths "$ROOT" \
  --distpath "$OUT_DIR" \
  --workpath "$WORK_DIR" \
  --specpath "$WORK_DIR" \
  --log-level WARN \
  "$MAIN_PY"

BIN="$OUT_DIR/fathom-helper/fathom-helper"
if [ ! -x "$BIN" ]; then
  echo "[build_helper] FAIL：未在 $OUT_DIR/fathom-helper/ 下生成可执行文件" >&2
  exit 1
fi

# Mach-O / 架构断言（仅 arm64 宿主；x86_64 由 ISS-041 在原生 runner 复验）
FILE_OUT="$(file "$BIN")"
echo "[build_helper] file: $FILE_OUT"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64)
    if ! printf '%s' "$FILE_OUT" | grep -q "arm64" || printf '%s' "$FILE_OUT" | grep -q "x86_64"; then
      echo "[build_helper] FAIL：arm64 宿主应产出纯 arm64 Mach-O，实际：$FILE_OUT" >&2
      exit 1
    fi
    ;;
  x86_64)
    if ! printf '%s' "$FILE_OUT" | grep -q "x86_64" || printf '%s' "$FILE_OUT" | grep -q "arm64"; then
      echo "[build_helper] FAIL：x86_64 宿主应产出纯 x86_64 Mach-O，实际：$FILE_OUT" >&2
      exit 1
    fi
    ;;
  *)
    echo "[build_helper] WARN：未识别宿主架构 $HOST_ARCH，跳过架构断言" >&2
    ;;
esac

# --version 身份面。``set -e`` 下 ``OUT_VERSION=...`` 的命令替换若失败
# 会直接退出脚本，原写法再检查 ``$CODE`` 是死分支——这里改用 ``||`` 显式
# 保留「可读失败输出 + 非零退出」语义，让 CI / 上层调用方能拿到原始 stderr。
OUT_VERSION="$("$BIN" --version 2>&1)" || {
  echo "[build_helper] FAIL：--version 退出非零，输出：$OUT_VERSION" >&2
  exit 1
}
echo "[build_helper] --version: $OUT_VERSION"
if ! printf '%s' "$OUT_VERSION" | python3 -c '
import json, sys
obj = json.loads(sys.stdin.read().strip())
assert obj.get("service") == "fathom", obj
assert obj.get("protocol_version") == 1, obj
assert obj.get("version") == "0.3.0", obj
' 2>&1; then
  echo "[build_helper] FAIL：--version 身份面与合约不符" >&2
  exit 1
fi

# 冻结树不应含 data/reports/logs 默认写入路径
if [ -d "$OUT_DIR/fathom-helper/data" ] || [ -d "$OUT_DIR/fathom-helper/reports" ] || [ -d "$OUT_DIR/fathom-helper/logs" ]; then
  echo "[build_helper] FAIL：冻结树内出现 data/reports/logs（生产 RuntimeConfig 应隔离到 FATHOM_RUNTIME_DIR）" >&2
  exit 1
fi

# 写入 shasum
SHA="$(shasum -a 256 "$BIN" | awk '{print $1}')"
echo "$SHA  $BIN" > "$OUT_DIR/fathom-helper.sha256"
echo "[build_helper] SHA256: $SHA"

echo "[build_helper] OK：冻结完成 $BIN"
