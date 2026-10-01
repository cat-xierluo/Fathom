#!/usr/bin/env python3
"""ISS-138：在固定 main 冻结的 Mach-O helper 上验证解读 API 生命周期与 shim 接线。

本脚本是 **reusable CLI**（``--helper PATH --output DIR``），不依赖任何本次私有
证据目录：所有 runtime/scan/resource 根、替身 CLI、合成快照都在 task-local
临时目录中生成，退出时清理。

生命周期一定从生产 HTTP 入口进入（``/api/analysis/previews`` →
``/api/analysis/jobs`` → ``/api/analysis/jobs/{id}`` →
``/api/analysis/jobs/{id}/cancel`` + ``PUT /api/config`` → ``/api/analyses``），
**不使用**私有 AnalysisManager，也不冻结自行模拟的 fake manager：serve 进程是
真实的冻结 Mach-O helper，解释器/依赖/路由/DB 全部来自该产物。

合成快照通过当前 repo 的公共 schema helper（``fathom.db``）建表并插入，不改
产品代码。fake Claude/Codex CLI 严格限于 task-local shim，输出固定合成结果：
它只证明 **接线与生命周期**（进程派发、取消、授权撤销、停机回收、正文入库），
**不代表** 真实模型语义质量，也不代表新版 CLI 能力。

保持 NOT_VERIFIED：代码签名、公证、双架构、Tauri GUI、实机升级、真实模型语义。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT_HTTP = 20.0
TIMEOUT_TERMINAL_S = 45.0
TIMEOUT_SHUTDOWN_S = 30.0
POLL_INTERVAL_S = 0.2
# 合成结果正文（固定字符串；不声称是模型产出的质量证据）
SHIM_SUMMARY = "ISS-138 合成解读：合成快照区间内条目总量小幅下降。"
REPO_ROOT = Path(__file__).resolve().parent.parent


class Failure(Exception):
    """断言失败：脚本必须以非零码退出。"""


# --------------------------------------------------------------------------
# 断言收集器
# --------------------------------------------------------------------------
class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def record(self, name: str, ok: bool, detail: str, **extra: Any) -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail, **extra})
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name} :: {detail}", flush=True)
        return bool(ok)

    def expect(self, name: str, ok: bool, detail: str, **extra: Any) -> None:
        if not self.record(name, ok, detail, **extra):
            raise Failure(f"{name} 失败：{detail}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [i for i in self.items if not i["ok"]]


# --------------------------------------------------------------------------
# HTTP 客户端（只走回环，Host 守卫要求 Host 与端口一致）
# --------------------------------------------------------------------------
class Http:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = ""
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: Any = None,
                *, use_token: bool = False, timeout: float = TIMEOUT_HTTP
                ) -> tuple[int, Any]:
        url = f"{self.base}{path}"
        data = None
        headers = {"Host": f"127.0.0.1:{url.rsplit(':', 1)[1].split('/', 1)[0]}"}
        headers["Host"] = f"127.0.0.1:{self.base.rsplit(':', 1)[1]}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if use_token:
            if not self.token:
                raise Failure("写请求前未取得写令牌")
            headers["X-Fathom-Token"] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise Failure(f"{method} {path} 传输失败：{exc}") from exc
        self.calls.append({"method": method, "path": path, "status": status})
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return status, payload

    def get(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return self.request("POST", path, body, use_token=True, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return self.request("PUT", path, body, use_token=True, **kw)


def _write_report(out_dir: Path, report: dict[str, Any]) -> None:
    """report.json 与 status 单一来源（NB4）：内层失败与最终态写同一文件。"""
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# task-local fake CLI 替身
# --------------------------------------------------------------------------
SHIM_SOURCE = '''#!/usr/bin/env python3
"""Task-local fake Claude Code CLI 替身（ISS-138 验证专用）。

固定合成输出：让生产适配器/合同/DB 走完整真实路径。它 **不是** 真实模型，
不承载任何语义质量证据；也 **不代表** 新版 Claude Code CLI 的能力。
"""
import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
VERSION = "2.1.237"


def _delay() -> float:
    try:
        return float((HERE / "shim_delay").read_text().strip() or 0)
    except Exception:
        return 0.0


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        sys.stdout.write(VERSION + " (Claude Code)\\n")
        return 0
    payload = ""
    if not sys.stdin.isatty():
        try:
            payload = sys.stdin.read()
        except Exception:
            payload = ""
    time.sleep(_delay())
    ids = []
    for eid in re.findall(r"f-\\d{3}", payload or ""):
        if eid not in ids:
            ids.append(eid)
    if not ids:
        ids = ["f-001"]
    body = {
        "schema_version": 1,
        "summary": %r,
        "findings": [{
            "text": "合成事实：区间内存在条目体积变化。",
            "evidence_ids": ids[:5],
            "certainty": "observed",
        }],
        "limitations": ["合成替身输出，非真实模型结果。"],
        "inspect_next": [{"evidence_id": ids[0], "reason": "合成下一步建议。"}],
    }
    sys.stdout.write(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": json.dumps(body, ensure_ascii=False),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# --------------------------------------------------------------------------
# 合成快照 seed（走当前 repo 公共 schema helper）
# --------------------------------------------------------------------------
def seed_snapshots(runtime_dir: Path, scan_root: Path) -> list[int]:
    env = dict(os.environ)
    env["FATHOM_RUNTIME_DIR"] = str(runtime_dir)
    env["FATHOM_SCAN_ROOT"] = str(scan_root)
    env["FATHOM_RUNTIME_MODE"] = "development"
    code = (
        "import json, sys;"
        "from pathlib import Path;"
        "sys.path.insert(0, %r);" % str(REPO_ROOT) +
        "import fathom.config as config, fathom.db as db;"
        "conn = db.connect(config.DB_PATH);"
        "conn.execute('SELECT 1 FROM snapshots LIMIT 1').fetchall();"
        "conn.close();"
        "print(json.dumps({'db': str(config.DB_PATH)}))"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise Failure(f"公共 schema 初始化失败：{proc.stderr[-500:]}")
    db_path = Path(json.loads(proc.stdout.strip().splitlines()[-1])["db"])
    if not db_path.exists():
        raise Failure(f"预期 DB 未生成：{db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        now = "2026-01-01T00:00:00"
        ids: list[int] = []
        # 合成数据集：区间 1->2 与 2->3 都必须有真实体积变化，
        # 否则 facts package 为空，替身输出无可引用证据（夹具缺陷，非产品缺陷）。
        spec = [
            ("2026-01-01T00:00:00", 5, 10, 0),
            ("2026-01-02T00:00:00", 4, 10, 0),
            ("2026-01-03T00:00:00", 4, 24, 0),
            ("2026-01-04T00:00:00", 4, 24, 0),
            ("2026-01-05T00:00:00", 4, 36, 0),
        ]
        for created_at, entries, size_kb, _ in spec:
            cur = conn.execute(
                "INSERT INTO snapshots(created_at, root, dir_count, denied_count,"
                " du_seconds, total_kb) VALUES (?,?,?,?,?,?)",
                (created_at, str(scan_root), 1, 0, 0.01, size_kb),
            )
            sid = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO entries(snapshot_id, path, size_kb) VALUES (?,?,?)",
                [(sid, f"synthetic/dir{i}/file{i}.bin", size_kb) for i in range(entries)],
            )
            conn.execute(
                "INSERT INTO volume_stats(snapshot_id, total_bytes, free_bytes)"
                " VALUES (?,?,?)", (sid, size_kb * 1024, size_kb * 512))
            ids.append(sid)
        conn.commit()
    finally:
        conn.close()
    return ids


def adapter_contract_version() -> str:
    code = ("import sys; sys.path.insert(0, %r);" % str(REPO_ROOT) +
            "import fathom.agent_runtime as ar; print(ar.ADAPTER_CONTRACT_VERSION)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise Failure(f"无法读取 adapter 合同版本：{proc.stderr[-300:]}")
    return proc.stdout.strip()


def analysis_settings_payload(shim: Path) -> dict[str, Any]:
    """分析授权设置体：runtime 只允许 {id, executable, version}（产品校验白名单）。"""
    return {
        "enabled": True,
        "runtime": {"id": "claude-code", "executable": str(shim),
                    "version": "2.1.237"},
        "settings_revision": 1,
        "consent_revision": 1,
    }


def set_shim_delay(shim_dir: Path, seconds: float) -> None:
    (shim_dir / "shim_delay").write_text(str(seconds), encoding="utf-8")


def _pgrep_full(pattern: str) -> list[int]:
    """列出 argv 含 pattern 的 pid（不用 pgrep 自身，避免自匹配）。"""
    out: list[int] = []
    try:
        proc = subprocess.run(["ps", "-Ao", "pid=,command="],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        if pattern in cmd and pid_s.isdigit():
            pid = int(pid_s)
            if pid != os.getpid():  # 只认自己启动的替身，不含本脚本
                out.append(pid)
    return out


def observe_shim(shim: Path, deadline_s: float, exclude: set[int] | None = None
                 ) -> dict[str, Any]:
    """有界轮询直到观测到自启动的 task-local 替身活跃进程/进程组。

    只 seeing job starting/running 不能证明 spawn；本函数是 spawn 的唯一证据源。
    exclude 是已归属到前一个场景的 pid，避免把旧替身算作本次观测。
    """
    exclude = exclude or set()
    end = time.monotonic() + deadline_s
    pattern = str(shim)
    while time.monotonic() < end:
        pids = [p for p in _pgrep_full(pattern) if p not in exclude]
        if pids:
            groups: dict[int, list[int]] = {}
            for pid in pids:
                try:
                    groups.setdefault(os.getpgid(pid), []).append(pid)
                except (ProcessLookupError, PermissionError):
                    continue
            return {"observed": True, "pids": sorted(pids),
                    "pgids": sorted(groups), "members": {str(k): sorted(v)
                                                        for k, v in groups.items()}}
        time.sleep(0.1)
    return {"observed": False, "pids": [], "pgids": [], "members": {},
            "detail": f"{deadline_s}s 内未观测到替身子进程（pattern={pattern}）"}


def assert_shim_gone(shim: Path, seen: dict[str, Any], deadline_s: float
                     ) -> dict[str, Any]:
    """校验此前记录的自有子进程 pid/进程组确实全部消失（只读观测，不发信号）。"""
    pattern = str(shim)
    recorded = list(seen.get("pids") or [])
    end = time.monotonic() + deadline_s
    alive: list[int] = []
    while time.monotonic() < end:
        current = set(_pgrep_full(pattern))
        alive = sorted(current.intersection(recorded)) if recorded else sorted(current)
        if not alive:
            return {"reaped": True, "still_alive": [], "recorded": recorded}
        time.sleep(0.2)
    return {"reaped": False, "still_alive": alive, "recorded": recorded}


# --------------------------------------------------------------------------
# serve 进程
# --------------------------------------------------------------------------
class Serve:
    def __init__(self, helper: Path, runtime_dir: Path, scan_root: Path,
                 resource_dir: Path, port: int, log_path: Path) -> None:
        self.helper = helper
        self.port = port
        self.log_path = log_path
        self.log = log_path.open("wb")
        env = dict(os.environ)
        env.update({
            "FATHOM_RUNTIME_DIR": str(runtime_dir),
            "FATHOM_SCAN_ROOT": str(scan_root),
            "FATHOM_RESOURCE_DIR": str(resource_dir),
            "FATHOM_RUNTIME_MODE": "development",
            "FATHOM_PORT": str(port),
            "PYTHONUNBUFFERED": "1",
        })
        self.proc = subprocess.Popen(
            [str(helper), "--runtime-dir", str(runtime_dir),
             "--scan-root", str(scan_root), "--resource-dir", str(resource_dir),
             "--runtime-mode", "development", "--port", str(port),
             "--port-range", "0", "serve"],
            stdout=self.log, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,  # 独立进程组：验证子进程组回收
        )

    def stop(self, sig: int = signal.SIGTERM) -> int:
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            return int(self.proc.wait(timeout=TIMEOUT_SHUTDOWN_S))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                return int(self.proc.wait(timeout=10))
            except subprocess.TimeoutExpired:
                return -1

    def close(self) -> None:
        try:
            self.log.close()
        except Exception:
            pass


def wait_health(http: Http, deadline_s: float) -> dict[str, Any]:
    end = time.monotonic() + deadline_s
    last = "未开始"
    while time.monotonic() < end:
        try:
            status, body = http.get("/health", timeout=3)
            if status == 200 and isinstance(body, dict):
                return body
            last = f"status={status} body={str(body)[:200]}"
        except Failure as exc:
            last = str(exc)
        time.sleep(0.25)
    raise Failure(f"/health 在 {deadline_s}s 内未就绪：{last}")


def wait_terminal(http: Http, job_id: str, deadline_s: float = TIMEOUT_TERMINAL_S
                  ) -> dict[str, Any]:
    end = time.monotonic() + deadline_s
    view: dict[str, Any] = {}
    while time.monotonic() < end:
        status, body = http.get(f"/api/analysis/jobs/{job_id}")
        if status != 200 or not isinstance(body, dict):
            raise Failure(f"查询 job 失败：status={status} body={body}")
        view = body.get("job") or {}
        if view.get("terminal"):
            return view
        time.sleep(POLL_INTERVAL_S)
    raise Failure(f"job {job_id} 在 {deadline_s}s 内未达终态，最后={view}")


def db_rows(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def start_job(http: Http, a: int, b: int, key: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    status, preview = http.post("/api/analysis/previews", {"a": a, "b": b})
    if status != 200 or not isinstance(preview, dict):
        raise Failure(f"预览失败：status={status} body={preview}")
    body = {"preview_id": preview["preview_id"],
            "request_digest": preview["request_digest"],
            "idempotency_key": key}
    status, resp = http.post("/api/analysis/jobs", body)
    if status != 202 or not isinstance(resp, dict):
        raise Failure(f"派发失败：status={status} body={resp}")
    return preview["preview_id"], resp["job"], preview


def main() -> int:
    ap = argparse.ArgumentParser(description="ISS-138 冻结 helper 解读生命周期验证")
    ap.add_argument("--helper", required=True, help="冻结 Mach-O helper 可执行文件路径")
    ap.add_argument("--output", required=True, help="证据输出目录")
    ap.add_argument("--build-ready", required=True, metavar="PATH",
                    help="可移植来源 manifest 路径（build receipt）。必填："
                         "本脚本不依赖任何 session 私有目录。")
    args = ap.parse_args()

    helper = Path(args.helper).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    build_ready = Path(args.build_ready).expanduser().resolve()
    checks = Checks()
    report: dict[str, Any] = {
        "task": "ISS-138",
        "helper": str(helper),
        "output_dir": str(out_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "not_verified": [
            "代码签名/公证/stapling", "x86_64 双架构", "Tauri GUI 实机",
            "实机安装升级链路", "真实模型语义质量", "新版 Claude/Codex CLI 能力",
        ],
    }

    # ---- 1. 产物身份绑定 ----
    if not helper.is_file() or not os.access(helper, os.X_OK):
        report["status"] = "NOT_VERIFIED"
        report["reason"] = f"helper 不存在或不可执行：{helper}"
        _write_report(out_dir, report)
        print(f"[NOT_VERIFIED] {report['reason']}", file=sys.stderr)
        return 2
    helper_sha = sha256_of(helper)
    report["helper_sha256"] = helper_sha
    report["helper_bytes"] = helper.stat().st_size
    checks.record("helper_exists", True, f"{helper} ({helper.stat().st_size} 字节)")

    # 来源 manifest 必填且严格：缺失/非法/ok!=true/指纹缺失/指纹不符一律非零退出，
    # 不放宽、不自动猜测 SHA。manifest 字段：ok(bool,须 true)、helper_sha256(64 hex)、
    # source_sha(来源提交，可选记录)、helper(可选路径记录)。
    report["build_ready_path"] = str(build_ready)
    if not build_ready.is_file():
        report["status"] = "NOT_VERIFIED"
        report["reason"] = f"来源 manifest 缺失：{build_ready}（--build-ready 必填）"
        _write_report(out_dir, report)
        print(f"[NOT_VERIFIED] {report['reason']}", file=sys.stderr)
        return 2
    try:
        build_info = json.loads(build_ready.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report["status"] = "NOT_VERIFIED"
        report["reason"] = f"来源 manifest 非合法 JSON：{build_ready}：{exc}"
        _write_report(out_dir, report)
        print(f"[NOT_VERIFIED] {report['reason']}", file=sys.stderr)
        return 2
    report["build_ready"] = build_info
    if not isinstance(build_info, dict) or build_info.get("ok") is not True:
        report["status"] = "NOT_VERIFIED"
        report["reason"] = (f"来源 manifest 缺 ok=true（按任务卡不得自行构建）："
                            f"{build_ready}；内容={str(build_info)[:200]}")
        _write_report(out_dir, report)
        print(f"[NOT_VERIFIED] {report['reason']}", file=sys.stderr)
        return 2
    declared_sha = build_info.get("helper_sha256")
    report["build_ready_sha256"] = declared_sha
    if not isinstance(declared_sha, str) or len(declared_sha) != 64:
        report["status"] = "FAILED"
        report["reason"] = (f"来源 manifest 缺有效 helper_sha256（64 hex）：{build_ready}；"
                            f"得到 {declared_sha!r}")
        _write_report(out_dir, report)
        print(f"[FAIL] {report['reason']}", file=sys.stderr)
        return 1
    if declared_sha != helper_sha:
        report["status"] = "FAILED"
        report["reason"] = (f"来源 manifest 指纹 {declared_sha} 与实测 {helper_sha} 不一致")
        _write_report(out_dir, report)
        print(f"[FAIL] {report['reason']}", file=sys.stderr)
        return 1
    checks.expect("helper_sha_bound", True,
                  f"SHA256={helper_sha} 与 manifest 指纹一致；source_sha="
                  f"{build_info.get('source_sha') or 'n/a'}")

    # ---- 2. 隔离环境 ----
    work = Path(tempfile.mkdtemp(prefix="iss138-"))
    runtime_dir = work / "runtime"
    scan_root = work / "scan"
    resource_dir = work / "resource"
    shim_dir = work / "shim"
    for d in (runtime_dir, scan_root, resource_dir, shim_dir):
        d.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        sub = scan_root / f"synthetic/dir{i}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"file{i}.bin").write_bytes(b"synthetic-iss138")
    shim = shim_dir / "claude"
    shim.write_text(SHIM_SOURCE % SHIM_SUMMARY, encoding="utf-8")
    shim.chmod(0o755)
    set_shim_delay(shim_dir, 0.0)
    report["isolated_roots"] = {
        "work": str(work), "runtime": str(runtime_dir),
        "scan": str(scan_root), "resource": str(resource_dir), "shim": str(shim),
    }

    contract = adapter_contract_version()
    report["adapter_contract_version"] = contract
    # 不预写 settings.json：分析授权统一经生产入口 PUT /api/config 写入，
    # 避免绕过产品校验白名单（runtime 仅允许 id/executable/version）。

    port = free_port()
    report["port"] = port
    log_path = out_dir / "serve.log"
    http = Http(port)
    serve = Serve(helper, runtime_dir, scan_root, resource_dir, port, log_path)
    report["serve"] = {"helper_argv_tail": "serve", "pid": serve.proc.pid,
                       "log": str(log_path)}

    try:
        # ---- 3. 身份断言：pid 与 runtime_mode 必须是我们自 spawn 的（ISS-035B）----
        health = wait_health(http, deadline_s=60)
        checks.expect("health_pid_is_self_spawn", health.get("pid") == serve.proc.pid,
                      f"/health pid={health.get('pid')} 自 spawn pid={serve.proc.pid}")
        checks.expect("health_runtime_mode_development",
                      health.get("runtime_mode") == "development",
                      f"/health runtime_mode={health.get('runtime_mode')}")
        report["health"] = health

        status, boot = http.get("/api/bootstrap")
        if status != 200 or not isinstance(boot, dict) or not boot.get("token"):
            raise Failure(f"取写令牌失败：status={status} body={boot}")
        http.token = str(boot["token"])
        checks.expect("write_token_issued", True, "GET /api/bootstrap 发放写令牌")

        snap_ids = seed_snapshots(runtime_dir, scan_root)
        db_path = runtime_dir / "data" / "fathom.db"
        if not db_path.exists():
            cands = sorted(runtime_dir.rglob("*.db"))
            if not cands:
                raise Failure(f"未找到合成 DB（runtime={runtime_dir}）")
            db_path = cands[0]
        report["db_path"] = str(db_path)
        report["snapshot_ids"] = snap_ids
        checks.expect("synthetic_snapshots_seeded", len(snap_ids) >= 5,
                      f"经公共 schema helper 插入合成快照 {snap_ids}")

        # 分析授权：只经产品生产入口 PUT /api/config 写入（不预写 settings.json）
        status, put_body = http.put("/api/config",
                                    {"analysis": analysis_settings_payload(shim)})
        checks.expect("consent_enabled_via_product_api", status == 200,
                      f"PUT /api/config analysis.enabled=true → status={status} "
                      f"body={str(put_body)[:160]}")

        a1, b1 = snap_ids[0], snap_ids[1]   # A 成功区间
        a2, b2 = snap_ids[1], snap_ids[2]   # C 撤权区间（与 A/B 均不重叠）

        # ---- 4. 场景 A：成功正文入库 ----
        set_shim_delay(shim_dir, 0.0)
        _, job, preview = start_job(http, a1, b1, "iss138-success")
        report["preview_contract"] = {
            "keys": sorted(preview.keys()),
            "runtime_id": (preview.get("runtime") or {}).get("id"),
            "runtime_executable": (preview.get("runtime") or {}).get("executable"),
        }
        checks.expect("preview_from_production_http", bool(preview.get("preview_id")),
                      f"POST /api/analysis/previews → preview_id={preview.get('preview_id')}")
        view = wait_terminal(http, job["job_id"])
        report["job_success_view"] = view
        checks.expect("A_job_succeeded", view.get("status") == "succeeded",
                      f"GET /api/analysis/jobs/{job['job_id']} → status={view.get('status')}"
                      f" reason={view.get('reason_code')}")
        status, listed = http.get(f"/api/analyses?a={a1}&b={b1}")
        analyses = (listed or {}).get("analyses") or []
        checks.expect("A_analysis_listed", status == 200 and len(analyses) == 1,
                      f"GET /api/analyses?a={a1}&b={b1} → {len(analyses)} 条")
        rows = db_rows(db_path,
                       "SELECT id, result_json FROM agent_analyses "
                       "WHERE a_snapshot_id=? AND b_snapshot_id=?", (a1, b1))
        checks.expect("A_body_persisted_in_db", len(rows) == 1
                      and SHIM_SUMMARY in (rows[0].get("result_json") or ""),
                      f"agent_analyses 行={len(rows)} result_json 命中合成正文="
                      f"{bool(rows and SHIM_SUMMARY in (rows[0].get('result_json') or ''))}")
        report["analysis_row"] = ({"id": rows[0]["id"]} if rows else None)
        runs = db_rows(db_path,
                       "SELECT status, reason_code FROM analysis_runs WHERE job_id=?",
                       (job["job_id"],))
        report["analysis_run_rows"] = runs
        checks.expect("A_run_row_succeeded", bool(runs) and runs[0]["status"] == "succeeded",
                      f"analysis_runs={runs}")

        # ---- 5. 场景 B：在途取消不写可信正文 ----
        # B 用独立快照对 (aB,bB)，与 A/C 归因隔离
        aB, bB = snap_ids[3], snap_ids[4]
        set_shim_delay(shim_dir, 12.0)
        _, job_b, _ = start_job(http, aB, bB, "iss138-cancel")
        # 前置视图强断言：必须确切观测到自启动的替身进程组（spawn 唯一证据源）
        deadline = time.monotonic() + 20
        running = False
        while time.monotonic() < deadline:
            _, body = http.get(f"/api/analysis/jobs/{job_b['job_id']}")
            cur = (body or {}).get("job") or {}
            if cur.get("status") in ("running", "starting"):
                running = True
                break
            if cur.get("terminal"):
                break
            time.sleep(0.1)
        checks.expect("B_job_in_flight_before_cancel", running,
                      f"cancel 前状态轮询到在途={running}")
        seen_b = observe_shim(shim, deadline_s=20.0)
        report["B_shim_observation"] = seen_b
        checks.expect("B_shim_child_observed", seen_b.get("observed") is True,
                      f"取消前观测自有替身 pids={seen_b.get('pids')} "
                      f"pgids={seen_b.get('pgids')}；{seen_b.get('detail','')}")
        status, cancel_body = http.post(f"/api/analysis/jobs/{job_b['job_id']}/cancel")
        checks.expect("B_cancel_accepted", status in (200, 409),
                      f"POST .../cancel → status={status} body={str(cancel_body)[:160]}")
        view_b = wait_terminal(http, job_b["job_id"])
        report["job_cancel_view"] = view_b
        checks.expect("B_job_cancelled", view_b.get("status") == "cancelled",
                      f"在途取消终态 status={view_b.get('status')}")
        reaped_b = assert_shim_gone(shim, seen_b, deadline_s=20.0)
        report["B_shim_reaped"] = reaped_b
        checks.expect("B_shim_child_reaped", reaped_b.get("reaped") is True,
                      f"取消后自有替身 pid 残留={reaped_b.get('still_alive')} "
                      f"（记录 pid={reaped_b.get('recorded')}）")
        rows_b = db_rows(db_path,
                         "SELECT id FROM agent_analyses "
                         "WHERE a_snapshot_id=? AND b_snapshot_id=?", (aB, bB))
        checks.expect("B_no_body_persisted", len(rows_b) == 0,
                      f"取消后 agent_analyses 行数={len(rows_b)}（必须 0）")
        _, listed_b = http.get(f"/api/analyses?a={aB}&b={bB}")
        checks.expect("B_no_analysis_listed",
                      len((listed_b or {}).get("analyses") or []) == 0,
                      "取消后 GET /api/analyses 为空")
        # 迟到正文复查：替身已回收 + 有界 settle 后复检，不无限等待
        time.sleep(2.0)
        rows_b2 = db_rows(db_path,
                          "SELECT id FROM agent_analyses "
                          "WHERE a_snapshot_id=? AND b_snapshot_id=?", (aB, bB))
        checks.expect("B_no_late_body_after_settle", len(rows_b2) == 0,
                      f"settle 后复查 agent_analyses 行数={len(rows_b2)}（必须 0，无迟到正文）")
        set_shim_delay(shim_dir, 0.0)

        # ---- 6. 场景 C：撤销授权后收敛、无迟到正文 ----
        set_shim_delay(shim_dir, 12.0)
        _, job_c, _ = start_job(http, a2, b2, "iss138-consent")
        # C 前置视图强断言：撤权前必须观测到自启动的替身进程组
        seen_c = observe_shim(shim, deadline_s=20.0)
        report["C_shim_observation"] = seen_c
        checks.expect("C_shim_child_observed", seen_c.get("observed") is True,
                      f"撤权前观测自有替身 pids={seen_c.get('pids')} "
                      f"pgids={seen_c.get('pgids')}；{seen_c.get('detail','')}")
        _, pre_c = http.get(f"/api/analysis/jobs/{job_c['job_id']}")
        pre_status = ((pre_c or {}).get("job") or {}).get("status")
        report["C_job_pre_revoke"] = pre_status
        checks.expect("C_job_in_flight_before_revoke",
                      pre_status in ("running", "starting"),
                      f"撤权前 job 状态={pre_status}")
        # GET /api/config 的 analysis 是只读视图（含 defaults/source 等非入参键），
        # 回写必须按产品白名单重建，否则 400 analysis 含未知字段。
        status, cfg = http.get("/api/config")
        cur_analysis = ((cfg or {}).get("analysis")) or {}
        cur_runtime = cur_analysis.get("runtime") or {}
        new_analysis: dict[str, Any] = {
            "enabled": False,
            "runtime": {
                "id": cur_runtime.get("id", "claude-code"),
                "executable": cur_runtime.get("executable", str(shim)),
                "version": cur_runtime.get("version", "2.1.237"),
            },
            "settings_revision": int(cur_analysis.get("settings_revision", 1) or 1),
            "consent_revision": int(cur_analysis.get("consent_revision", 1) or 1) + 1,
        }
        status, put_body = http.put("/api/config", {"analysis": new_analysis})
        applied = bool((put_body or {}).get("applied")) if isinstance(put_body, dict) else False
        checks.expect("C_consent_revoked", status == 200 and applied,
                      f"PUT /api/config enabled=false consent_revision+1 → status={status} "
                      f"applied={applied} body={str(put_body)[:160]}")
        view_c = wait_terminal(http, job_c["job_id"], deadline_s=60)
        report["job_consent_view"] = view_c
        checks.expect("C_converges_without_body",
                      view_c.get("status") in ("cancelled", "failed")
                      and view_c.get("status") != "succeeded",
                      f"撤销授权后终态 status={view_c.get('status')} "
                      f"reason={view_c.get('reason_code')}")
        rows_c = db_rows(db_path,
                         "SELECT id FROM agent_analyses "
                         "WHERE a_snapshot_id=? AND b_snapshot_id=?", (a2, b2))
        checks.expect("C_no_late_body", len(rows_c) == 0,
                      f"撤销授权后 agent_analyses 行数={len(rows_c)}（必须 0，无迟到正文）")
        time.sleep(2.0)
        rows_c2 = db_rows(db_path,
                          "SELECT id FROM agent_analyses "
                          "WHERE a_snapshot_id=? AND b_snapshot_id=?", (a2, b2))
        checks.expect("C_no_late_body_after_settle", len(rows_c2) == 0,
                      "settle 后复查仍无正文")
        reaped_c = assert_shim_gone(shim, seen_c, deadline_s=20.0)
        report["C_shim_reaped"] = reaped_c
        checks.expect("C_shim_child_reaped", reaped_c.get("reaped") is True,
                      f"撤权收敛后自有替身 pid 残留={reaped_c.get('still_alive')} "
                      f"（记录 pid={reaped_c.get('recorded')}）")
        set_shim_delay(shim_dir, 0.0)

        # ---- 7. 场景 D：serve 退出时子进程组回收 + 实例记录清理 ----
        # 场景 C 已撤销授权（这正是产品预期行为），派发前必须经同一生产入口重新授权。
        status, reenable = http.put(
            "/api/config", {"analysis": {**analysis_settings_payload(shim),
                                         "consent_revision": 2}})
        checks.expect("D_reconsent_via_product_api", status == 200,
                      f"重新授权（consent_revision=2）→ status={status} "
                      f"body={str(reenable)[:120]}")
        set_shim_delay(shim_dir, 25.0)
        aD, bD = snap_ids[2], snap_ids[3]   # D 独立区间
        _, job_d, _ = start_job(http, aD, bD, "iss138-shutdown")
        # D 前置视图强断言：停机前必须观测到自有替身进程组（否则停机回收无从谈起）
        seen_d = observe_shim(shim, deadline_s=20.0)
        report["D_shim_observation"] = seen_d
        checks.expect("D_shim_child_observed", seen_d.get("observed") is True,
                      f"停机前观测自有替身 pids={seen_d.get('pids')} "
                      f"pgids={seen_d.get('pgids')}；{seen_d.get('detail','')}")
        _, body_d = http.get(f"/api/analysis/jobs/{job_d['job_id']}")
        pre_d = ((body_d or {}).get("job") or {}).get("status")
        report["job_shutdown_view"] = (body_d or {}).get("job")
        checks.expect("D_job_in_flight_before_shutdown",
                      pre_d in ("running", "starting"),
                      f"停机前 job 状态={pre_d}")
        exit_code = serve.stop(signal.SIGTERM)
        report["serve_exit_code"] = exit_code
        checks.expect("D_serve_exits_cleanly", exit_code == 0,
                      f"SIGTERM 后 helper 退出码={exit_code}")
        reaped_d = assert_shim_gone(shim, seen_d, deadline_s=25.0)
        leftover_pids = reaped_d.get("still_alive") or []
        report["leftover_shim_pids"] = leftover_pids
        report["D_shim_reaped"] = reaped_d
        checks.expect("D_child_group_reaped", reaped_d.get("reaped") is True,
                      f"helper 停机后自有替身 pid 残留={leftover_pids or '无'}"
                      f"（记录 pid={reaped_d.get('recorded')}，pgids="
                      f"{seen_d.get('pgids')}）")
        try:
            subprocess.run(["pgrep", "-f", f"fathom.db-wal"], capture_output=True,
                           text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        instance_files = [p for p in runtime_dir.rglob("*")
                          if p.is_file() and "instance" in p.name.lower()]
        report["instance_files"] = [str(p) for p in instance_files]
        checks.expect("D_instance_record_cleared", not instance_files,
                      f"helper 退出后运行根内实例记录残留={[str(p) for p in instance_files] or '无'}")

    except Failure as exc:
        report["status"] = "FAILED"
        report["reason"] = str(exc)
        print(f"[FAIL] {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        report["status"] = "ERROR"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        print(f"[ERROR] {report['reason']}", file=sys.stderr)
    finally:
        if serve.proc.poll() is None:
            serve.stop(signal.SIGTERM)
        serve.close()
        report["checks"] = checks.items
        report["failed_checks"] = [c["name"] for c in checks.failed]
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if "status" not in report:
            report["status"] = "FAILED" if checks.failed else "PASSED"
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps({"status": report["status"], "failed": report.get("failed_checks"),
                      "report": str(out_dir / "report.json")}, ensure_ascii=False))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
