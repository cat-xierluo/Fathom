import json, subprocess, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, "/Users/maoking/Library/Application Support/maoscripts/fathom-worktrees/iss-126-hermes")
from fathom.agent_runtime import get_candidate, detect_runtime, get_adapter, AgentCliRunner, dispatch_request

meta = get_candidate("hermes-agent")
info = detect_runtime(meta)
adapter = get_adapter(info)
runner = AgentCliRunner(liveness_watch=True)
cwd = Path(tempfile.mkdtemp(prefix="fathom-reap-cancel-"))
cancel_event = threading.Event()
threading.Thread(target=lambda: (time.sleep(2.0), cancel_event.set()), daemon=True).start()
r = dispatch_request(adapter, runner,
    "请从 1 逐个数到 300，每个数字单独一行，全部输出。",
    cwd=cwd, timeout_s=120.0, cancel_event=cancel_event)
print(json.dumps({"ok": r.ok, "reason_code": r.reason_code, "outcome": r.run.outcome.value,
                  "wall_ms": r.run.wall_ms, "group_reaped": r.run.group_reaped}, ensure_ascii=False))
