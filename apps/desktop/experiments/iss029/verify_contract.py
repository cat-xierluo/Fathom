#!/usr/bin/env python3
"""ISS-029 helper 合同的确定性、无生产副作用验证器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE = "dev.fathom.helper-experiment.iss029"
PROTOCOL = 1
HELPER_VERSION = "0.0.1-iss029"


class Harness:
    def __init__(self, helper_src: Path, evidence_dir: Path):
        self.work = Path(tempfile.mkdtemp(prefix="fathom-iss029."))
        self.resource = self.work / "app" / "Fathom 实验 & Helper 目录"
        self.resource.mkdir(parents=True)
        self.helper = self.resource / "helper_contract.py"
        shutil.copy2(helper_src, self.helper)
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.json_path = evidence_dir / ("experiment-%s.json" % run_id)
        self.log_path = evidence_dir / ("experiment-%s.log" % run_id)
        self.log = self.log_path.open("w", encoding="utf-8")
        self.cases: list[dict] = []
        self.children: dict[int, tuple[subprocess.Popen, str, str]] = {}
        self.base_port = self._free_range(5)

    @staticmethod
    def _free_range(count: int) -> int:
        for base in range(18000, 26000, count):
            sockets = []
            try:
                for port in range(base, base + count):
                    sock = socket.socket()
                    sock.bind(("127.0.0.1", port))
                    sockets.append(sock)
                return base
            except OSError:
                pass
            finally:
                for sock in sockets:
                    sock.close()
        raise RuntimeError("找不到连续实验端口")

    def record(self, name: str, status: str, detail: str, kind: str = "assertion") -> None:
        if status not in {"passed", "failed", "blocked"}:
            raise ValueError(status)
        self.cases.append({"name": name, "status": status, "kind": kind, "detail": detail})
        self.log.write("[%s] %s :: %s\n" % (status, name, detail))
        self.log.flush()

    @staticmethod
    def _identity(pid: int) -> str:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)], capture_output=True,
            text=True, check=False)
        return result.stdout.strip()

    def start(self, argv: list[str], label: str) -> subprocess.Popen:
        proc = subprocess.Popen(argv, stdout=self.log, stderr=self.log)
        # macOS 上解释器启动初期 command 会从 shim 变为真实 Python 路径；
        # 等身份连续稳定后才登记，避免把正常 exec 误判为 PID 复用。
        identity = ""
        previous = ""
        for _ in range(30):
            current = self._identity(proc.pid)
            if current and current == previous:
                identity = current
                break
            previous = current
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if not identity:
            # 并发锁失败者可能在登记前已结构化退出；它已不是活动进程，
            # 无需进入 cleanup 跟踪集，仍保留 Popen 供调用者取得退出码。
            if proc.poll() is not None:
                return proc
            proc.terminate()
            proc.wait(timeout=3)
            raise RuntimeError("活动子进程启动后无法取得稳定身份：%s" % label)
        self.children[proc.pid] = (proc, identity, label)
        return proc

    def wait(self, proc: subprocess.Popen, timeout: float) -> int:
        try:
            return proc.wait(timeout=timeout)
        finally:
            if proc.poll() is not None:
                self.children.pop(proc.pid, None)

    def signal(self, proc: subprocess.Popen, signum: int) -> bool:
        tracked = self.children.get(proc.pid)
        if not tracked or proc.poll() is not None:
            return False
        if self._identity(proc.pid) != tracked[1]:
            return False
        os.kill(proc.pid, signum)
        return True

    def cleanup(self) -> None:
        # 只向仍由本 Harness 持有、启动身份仍匹配的活动 Popen 发信号。
        active = [entry[0] for entry in list(self.children.values())]
        for proc in active:
            try:
                self.signal(proc, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + 2
        for proc in active:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                self.wait(proc, remaining)
            except subprocess.TimeoutExpired:
                try:
                    self.signal(proc, signal.SIGKILL)
                    self.wait(proc, 2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.log.close()
        shutil.rmtree(self.work, ignore_errors=True)

    def helper_argv(self, data: Path, *extra: str) -> list[str]:
        return [sys.executable, str(self.helper), "serve", "--data-dir", str(data),
                "--port", str(self.base_port), "--port-range", "5", *extra]

    def start_helper(self, data: Path, *extra: str) -> subprocess.Popen:
        return self.start(self.helper_argv(data, *extra), "helper")

    def start_dummy(self, port: int) -> subprocess.Popen:
        source = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'unknown'; self.send_response(200)
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
"""
        return self.start([sys.executable, "-c", source, str(port),
                           "iss029-dummy-%s" % self.work.name], "dummy")

    @staticmethod
    def request(port: int, path: str, method: str = "GET", token: str = "",
                host: str | None = None) -> tuple[int, str]:
        headers = {"Host": host or "127.0.0.1:%d" % port}
        if token:
            headers["X-Fathom-Helper-Token"] = token
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=1) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def wait_health(self, port: int, timeout: float = 10) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                code, body = self.request(port, "/health")
                if code == 200:
                    return json.loads(body)
            except Exception:
                pass
            time.sleep(0.1)
        return None

    @staticmethod
    def pids_on_port(port: int) -> list[int]:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True, text=True, check=False).stdout
        return sorted(int(value) for value in out.split() if value.isdigit())

    @staticmethod
    def fingerprint(root: Path) -> dict[str, str]:
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}

    @staticmethod
    def replace_json(path: Path, value: dict) -> None:
        tmp = path.with_name(".%s.replacement" % path.name)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)

    def finish(self) -> int:
        counts = {key: sum(case["status"] == key for case in self.cases)
                  for key in ("passed", "failed", "blocked")}
        result = {
            "schema": "fathom.iss029.experiment-results.v2",
            "verdict": "PASS" if counts["failed"] == 0 and counts["blocked"] == 0 else
                       ("FAIL" if counts["failed"] else "PARTIAL_BLOCKED"),
            **counts,
            "host_note": "仅证明当前宿主架构；x86_64 归 ISS-041 原生 runner",
            "cases": self.cases,
        }
        self.json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        print(json.dumps({**counts, "verdict": result["verdict"],
                          "evidence": str(self.json_path)}, ensure_ascii=False))
        return 1 if counts["failed"] else (3 if counts["blocked"] else 0)


def run(h: Harness) -> None:
    data = h.work / "data-main"
    crash_data = h.work / "data-crash"
    original_fingerprint = h.fingerprint(h.resource)

    usage = subprocess.run([sys.executable, str(h.helper), "serve"],
                           stdout=h.log, stderr=h.log, check=False)
    h.record("C0-usage-exit-2", "passed" if usage.returncode == 2 else "failed",
             "缺少 --data-dir 时 argparse 退出 2")

    version = subprocess.run([sys.executable, str(h.helper), "--version"],
                             capture_output=True, text=True, check=False)
    try:
        identity = json.loads(version.stdout)
        valid = (version.returncode == 0 and identity["service"] == SERVICE and
                 identity["protocol_version"] == PROTOCOL and
                 identity["helper_version"] == HELPER_VERSION and identity["machine"])
    except Exception:
        valid = False
    h.record("C1-version-identity", "passed" if valid else "failed",
             "--version 返回静态身份、协议和 helper 版本")

    first = h.start_helper(data)
    health = h.wait_health(h.base_port)
    valid_health = bool(health and health.get("service") == SERVICE and
                        health.get("protocol_version") == PROTOCOL and
                        health.get("helper_version") == HELPER_VERSION and
                        isinstance(health.get("instance_id"), str) and
                        health.get("pid") == first.pid and health.get("port") == h.base_port)
    h.record("C2-health-identity", "passed" if valid_health else "failed",
             "/health 含 service/protocol/helper_version/instance_id/pid/port")
    discovery = data / "helper-instance.json"
    try:
        disc = json.loads(discovery.read_text(encoding="utf-8"))
        disc_valid = (stat.S_IMODE(discovery.stat().st_mode) == 0o600 and
                      disc.get("service") == SERVICE and disc.get("protocol_version") == PROTOCOL and
                      disc.get("helper_version") == HELPER_VERSION and disc.get("pid") == first.pid and
                      disc.get("instance_id") == health.get("instance_id") and
                      isinstance(disc.get("control_token"), str) and len(disc["control_token"]) >= 32)
    except Exception:
        disc, disc_valid = {}, False
    h.record("C2-discovery-atomic-0600", "passed" if disc_valid else "failed",
             "discovery 为 0600 且身份字段与 health 一致")
    bad, _ = h.request(h.base_port, "/health", host="evil.example")
    good, _ = h.request(h.base_port, "/health", host="localhost:%d" % h.base_port)
    h.record("C2-host-guard", "passed" if (bad, good) == (403, 200) else "failed",
             "非 loopback Host=403，localhost=200")
    listen_pids = h.pids_on_port(h.base_port)
    h.record("C2-loopback-listener-owner", "passed" if listen_pids == [first.pid] else "failed",
             "实验端口只有当前 helper 监听")
    unknown, _ = h.request(h.base_port, "/nope")
    h.record("C2-unknown-path-404", "passed" if unknown == 404 else "failed",
             "未知路径返回 404")

    wrong, _ = h.request(h.base_port, "/shutdown", method="POST", token="definitely-wrong")
    right, _ = h.request(h.base_port, "/shutdown", method="POST", token=disc.get("control_token", ""))
    code = h.wait(first, 8)
    h.log.flush()
    leaked = bool(disc.get("control_token") and disc["control_token"] in h.log_path.read_text(encoding="utf-8"))
    h.record("C3-token-shutdown-no-leak", "passed" if wrong == 403 and right == 200 and
             code == 0 and not discovery.exists() and not leaked else "failed",
             "错令牌 403；正确令牌退出 0；令牌不出现在 stdout/stderr；自身 discovery 清理")

    for name, signum in (("C4-sigterm", signal.SIGTERM), ("C4-sigint", signal.SIGINT)):
        proc = h.start_helper(data)
        ready = h.wait_health(h.base_port)
        sent = h.signal(proc, signum)
        code = h.wait(proc, 8)
        h.record(name, "passed" if ready and sent and code == 0 and not discovery.exists() else "failed",
                 "身份匹配的活动子进程优雅退出 0 并清理自身 discovery")

    dummy = h.start_dummy(h.base_port)
    assert h.wait_health(h.base_port) is None
    before = h.pids_on_port(h.base_port)
    fallback = h.start_helper(data)
    fallback_health = h.wait_health(h.base_port + 1)
    after = h.pids_on_port(h.base_port)
    h.record("C5-unknown-occupier-fallback", "passed" if before == [dummy.pid] and
             after == before and fallback_health and fallback_health.get("pid") == fallback.pid else "failed",
             "未知占用者 PID 前后完全一致；helper 让位下一端口")
    h.signal(fallback, signal.SIGTERM); h.wait(fallback, 8)
    h.signal(dummy, signal.SIGTERM); h.wait(dummy, 8)

    dummies = [h.start_dummy(port) for port in range(h.base_port, h.base_port + 5)]
    time.sleep(0.4)
    before_map = {port: h.pids_on_port(port) for port in range(h.base_port, h.base_port + 5)}
    exhausted = subprocess.run(h.helper_argv(data), stdout=h.log, stderr=h.log, check=False)
    after_map = {port: h.pids_on_port(port) for port in range(h.base_port, h.base_port + 5)}
    h.record("C6-ports-exhausted-no-signal", "passed" if exhausted.returncode == 3 and
             before_map == after_map and not discovery.exists() else "failed",
             "全候选端口占用时退出 3；所有未知占用 PID 前后完全一致")
    for proc in dummies:
        h.signal(proc, signal.SIGTERM); h.wait(proc, 8)

    concurrent_data = h.work / "data-concurrent"
    concurrent_gate = h.work / "concurrent-start.gate"
    contenders = [h.start_helper(concurrent_data, "--start-gate", str(concurrent_gate))
                  for _ in range(8)]
    concurrent_gate.touch()
    owner_health = h.wait_health(h.base_port, 10)
    owner_pid = owner_health.get("pid") if owner_health else None
    owner = next((proc for proc in contenders if proc.pid == owner_pid), None)
    loser_codes = []
    for proc in contenders:
        if proc is owner:
            continue
        try:
            loser_codes.append(h.wait(proc, 8))
        except subprocess.TimeoutExpired:
            loser_codes.append(None)
    live = [proc.pid for proc in contenders if proc.poll() is None]
    h.record("C7-concurrent-single-owner", "passed" if owner and live == [owner.pid] and
             loser_codes == [4] * 7 else "failed",
             "8 个并发实例最多一个 owner；其余均结构化退出 4")
    if owner:
        h.signal(owner, signal.SIGTERM); h.wait(owner, 8)

    crash = h.start_helper(crash_data, "--crash-after", "0.5")
    old_health = h.wait_health(h.base_port)
    crash_code = h.wait(crash, 8)
    stale_exists = (crash_data / "helper-instance.json").exists()
    takeover = h.start_helper(crash_data)
    new_health = h.wait_health(h.base_port)
    h.record("C8-crash-stale-takeover", "passed" if old_health and crash_code == -signal.SIGKILL and
             stale_exists and new_health and new_health.get("instance_id") != old_health.get("instance_id")
             else "failed", "SIGKILL 留 stale；无活动锁时新实例安全接管")
    h.signal(takeover, signal.SIGTERM); h.wait(takeover, 8)

    malformed_data = h.work / "data-malformed"
    malformed_data.mkdir()
    (malformed_data / "helper-instance.json").write_text("{bad json", encoding="utf-8")
    malformed = h.start_helper(malformed_data)
    malformed_health = h.wait_health(h.base_port)
    h.record("C9-malformed-discovery-takeover", "passed" if malformed_health and
             malformed_health.get("pid") == malformed.pid else "failed",
             "损坏 discovery 不触发 UnboundLocalError；锁持有者安全接管")
    h.signal(malformed, signal.SIGTERM); h.wait(malformed, 8)

    replaced_data = h.work / "data-replaced"
    replaced = h.start_helper(replaced_data)
    replaced_health = h.wait_health(h.base_port)
    replaced_path = replaced_data / "helper-instance.json"
    replacement_token = hashlib.sha256(os.urandom(32)).hexdigest()
    foreign = {"service": SERVICE, "protocol_version": PROTOCOL,
               "helper_version": HELPER_VERSION, "instance_id": "replacement",
               "pid": 999999, "port": h.base_port, "control_token": replacement_token}
    h.replace_json(replaced_path, foreign)
    h.signal(replaced, signal.SIGTERM); h.wait(replaced, 8)
    try:
        remaining = json.loads(replaced_path.read_text(encoding="utf-8"))
    except Exception:
        remaining = None
    h.record("C10-replaced-discovery-preserved", "passed" if replaced_health and
             remaining == foreign else "failed",
             "退出仅按 pid+instance_id+token 匹配删除，替换记录保留")

    h.record("C11-resources-readonly", "passed" if original_fingerprint == h.fingerprint(h.resource)
             else "failed", "含空格/中文/& 的资源目录内容指纹未变化")
    h.record("Z1-tracked-children-drained", "passed" if not h.children else "failed",
             "已 wait 的子进程均从跟踪集移除；无活动子进程残留")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    harness = Harness(args.helper.resolve(), args.evidence_dir.resolve())
    try:
        run(harness)
        return harness.finish()
    finally:
        harness.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
