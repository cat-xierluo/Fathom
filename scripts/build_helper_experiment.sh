#!/usr/bin/env bash
# ISS-029 helper 合同实验。证据默认写入会话目录，不进入 Git。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXP_DIR="$ROOT/apps/desktop/experiments/iss029"
EVIDENCE_DIR="${FATHOM_ISS029_EVIDENCE_DIR:-$ROOT/.claude/agent-sessions/fathom-release-iss-029/evidence/contract}"

exec python3 "$EXP_DIR/verify_contract.py" \
  --helper "$EXP_DIR/helper_contract.py" \
  --evidence-dir "$EVIDENCE_DIR"
