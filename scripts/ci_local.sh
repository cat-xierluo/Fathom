#!/usr/bin/env bash
# 本地一键 CI：复刻 .github/workflows/ci.yml 六 job 的本地等价（arm64 单机）。
# 用法：bash scripts/ci_local.sh          # 全量六段
#       FATHOM_LOCAL_JOBS="pytest browser" 只跑指定段
# 空间注意：dmg 段不在本地跑（发版 dmg 由 CI runner 产出）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="${FATHOM_LOCAL_LOG:-$ROOT/apps/desktop/src-tauri/verify-results/ci-local}"
mkdir -p "$LOG_DIR"
JOBS="${FATHOM_LOCAL_JOBS:-pytest browser brand cargo}"
PY="${FATHOM_PYTHON:-$ROOT/.venv/bin/python}"
RC=0

has() { [[ " $JOBS " == *" $1 "* ]]; }
run() { # name, logfile, cmd...
  local name="$1" log="$2"; shift 2
  echo "=== [$name] $*"
  if "$@" > "$log" 2>&1; then
    echo "=== [$name] PASS（日志 ${log}）"
  else
    echo "=== [$name] FAIL（日志 ${log}，尾部：）"
    tail -5 "$log"
    RC=1
  fi
}

if has pytest; then
  EXPECTED_PYTEST_PASSED=1517 FATHOM_PYTHON="$PY" \
    run pytest "$LOG_DIR/pytest.log" bash scripts/ci_pytest.sh
fi

if has browser; then
  EXPECTED_BROWSER_PASSED=39 EXPECTED_REFRESH_PASSED=217 \
  EXPECTED_ANALYSIS_PASSED=77 EXPECTED_TREE_PASSED=74 \
  EXPECTED_DIR_BIGFILES_PASSED=36 EXPECTED_BROWSE_SNAPSHOT_PASSED=33 \
  EXPECTED_SCOPE_SETTINGS_PASSED=37 EXPECTED_STORAGE_OVERVIEW_PASSED=52 \
  EXPECTED_INVESTIGATION_PASSED=55 EXPECTED_UPDATER_RECOVERY_PASSED=16 FATHOM_PYTHON="$PY" \
    EXPECTED_CARGO_PASSED=77 run browser "$LOG_DIR/browser.log" bash scripts/ci_browser_checks.sh
fi

if has brand; then
  run brand "$LOG_DIR/brand.log" bash scripts/ci_brand_geometry.sh
fi

if has cargo; then
  EXPECTED_CARGO_PASSED=77 run cargo "$LOG_DIR/cargo.log" bash scripts/ci_cargo_locked.sh
  run opener "$LOG_DIR/opener.log" bash scripts/ci_tauri_opener_registered.sh
fi

echo ""
if [ "$RC" -eq 0 ]; then
  echo "ci-local: 全部通过（${JOBS}）"
else
  echo "ci-local: 存在失败段（${JOBS}），RC=${RC}"
fi
exit "$RC"
