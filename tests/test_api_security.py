"""本地 API 边界测试（ISS-022，反例来源 AUD-06）。

覆盖三份合同：
1. Host/Origin/写令牌：错误 Host（DNS rebinding）、跨站 Origin、无凭据写请求
   必须在副作用发生前被拒绝；合法浏览器（同源 + 令牌）与 CLI（无 Origin +
   先 bootstrap 取令牌）入口按合同可用。TestClient 与真实客户端走同一守卫，
   不存在测试专用旁路。
2. reveal：只规范化监控根内真实存在的路径；拒绝 ..、越界符号链接、前缀
   同名根、相对路径、不存在路径与非对象请求；mock 证明被拒路径从未调用 open。
3. 渲染与凭据卫生：日报日期格式收紧；令牌只经 /api/bootstrap 发放，不出现在
   日志；静态页响应带 CSP。

扫描内核全部 mock，不跑真实 du，不触发真实 Finder（subprocess.run 被替换）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db

EVIL_ORIGIN = "http://evil.example"
TAURI_LOADER_ORIGIN = "tauri://localhost"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """全部可写路径与扫描根指到临时目录，避免触碰生产 data/reports/logs/HOME。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    root = tmp_path / "scanroot"
    root.mkdir()
    monkeypatch.setattr(config, "DEFAULT_ROOT", root)
    # _scan_lock 是进程身份的化身：独立锁避免与其他测试的扫描线程串台
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    """与真实客户端同一合同：合法 Host（base_url）+ 写令牌（bootstrap 后注入）。

    不提供任何绕过守卫的测试入口；恶意头通过 per-request headers 显式构造。
    """
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


@pytest.fixture
def reveal_spy(monkeypatch):
    """记录 open 调用而不真正执行；断言“被拒绝的路径从未进入 open”。"""
    calls: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def root_fs():
    """根内合法目录 + 指向根外的符号链接 + 前缀同名邻居根（AUD-06 反例布景）。"""
    root = config.DEFAULT_ROOT
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "sub").mkdir()
    (root.parent / (root.name + "-evil")).mkdir()  # 前缀同名：/scanroot-evil 不是 /scanroot
    (root / "link-out").symlink_to(outside)  # 根内符号链接指向根外
    return root


def _mock_scan_kernel(monkeypatch) -> None:
    monkeypatch.setattr(api.scanner, "create_snapshot", lambda conn, *a, **k: 9001)
    monkeypatch.setattr(api.reports, "write_daily_report",
                        lambda conn, sid: "/tmp/fake.md")
    monkeypatch.setattr(api.scanner, "prune_snapshots", lambda conn: 0)


def _scan_run_count() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM scan_runs").fetchone()["c"]
    finally:
        conn.close()


def _wait_scan_done(client, timeout: float = 5.0) -> None:
    """等后台扫描线程收尾，避免测试结束后跨 monkeypatch 释放锁的告警。"""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not api._scan_lock.locked() and client.get("/api/scan/status").json()["status"] == "done":
            return
        time.sleep(0.02)
    raise AssertionError("扫描未在超时内完成")


# ---------- Host 边界（DNS rebinding 防线） ----------


class TestHostBoundary:
    def test_foreign_host_rejected_on_read(self, client):
        r = client.get("/api/status", headers={"host": "evil.example"})
        assert r.status_code == 403

    def test_foreign_host_with_port_rejected(self, client):
        r = client.get("/api/status", headers={"host": "evil.example:7952"})
        assert r.status_code == 403

    def test_foreign_host_write_rejected_before_side_effect(self, client, monkeypatch):
        _mock_scan_kernel(monkeypatch)
        before = _scan_run_count()
        r = client.post("/api/scan", headers={"host": "evil.example"})
        assert r.status_code == 403
        assert _scan_run_count() == before  # 拒绝发生在任何副作用之前

    def test_localhost_alias_accepted(self, client):
        r = client.get("/api/status", headers={"host": f"localhost:{config.PORT}"})
        assert r.status_code == 200


# ---------- Origin / 写令牌边界（CSRF 防线，不把 CORS 当鉴权） ----------


class TestOriginAndTokenBoundary:
    def test_cross_site_origin_scan_rejected(self, client, monkeypatch):
        """AUD-06 反例：带外站 Origin 的 scan 曾返回 200。"""
        _mock_scan_kernel(monkeypatch)
        before = _scan_run_count()
        r = client.post("/api/scan", headers={"origin": EVIL_ORIGIN})
        assert r.status_code == 403
        assert _scan_run_count() == before

    def test_cross_site_origin_cannot_bootstrap(self, client):
        """跨站页面连“取令牌”都不允许，响应里不出现令牌。"""
        r = client.get("/api/bootstrap", headers={"origin": EVIL_ORIGIN})
        assert r.status_code == 403
        assert api._WRITE_TOKEN not in r.text

    def test_write_without_token_rejected(self, client, monkeypatch):
        """无凭据写请求（含 curl 直接 POST）在副作用前被拒绝。"""
        _mock_scan_kernel(monkeypatch)
        before = _scan_run_count()
        r = client.post("/api/scan", headers={"X-Fathom-Token": ""})
        assert r.status_code == 403
        assert _scan_run_count() == before

    def test_wrong_token_rejected(self, client, monkeypatch):
        _mock_scan_kernel(monkeypatch)
        r = client.post("/api/scan", headers={"X-Fathom-Token": "forged-token"})
        assert r.status_code == 403
        assert _scan_run_count() == 0

    def test_cli_contract_no_origin_with_token_ok(self, client, monkeypatch):
        """合法 CLI 用法：无 Origin（非浏览器）+ 先 bootstrap 取令牌 → 可写。"""
        _mock_scan_kernel(monkeypatch)
        r = client.post("/api/scan")  # httpx 默认不发 Origin 头，等同 curl
        assert r.status_code == 200 and r.json()["ok"] is True
        _wait_scan_done(client)

    def test_same_origin_browser_contract_ok(self, client, monkeypatch):
        """合法浏览器用法：同源 Origin + 令牌 → 可写。"""
        _mock_scan_kernel(monkeypatch)
        r = client.post(
            "/api/scan",
            headers={"origin": f"http://127.0.0.1:{config.PORT}"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        _wait_scan_done(client)

    def test_tauri_loader_origin_read_ok(self, client):
        """Tauri 壳 loader 页（tauri://localhost）只读探测 /api/status 可用。"""
        r = client.get("/api/status", headers={"origin": TAURI_LOADER_ORIGIN})
        assert r.status_code == 200

    def test_rejected_scan_not_started_in_background(self, client, monkeypatch):
        """被拒的写请求不仅返回 403，也没有留下 running 记录或后台扫描。"""
        _mock_scan_kernel(monkeypatch)
        client.post("/api/scan", headers={"origin": EVIL_ORIGIN})
        assert not api._scan_lock.locked()
        assert _scan_run_count() == 0


# ---------- reveal 边界（AUD-06 反例：root/../outside 曾 200 并进入 open） ----------


class TestRevealBoundary:
    def _post(self, client, payload, **headers):
        return client.post("/api/reveal", json=payload, headers=headers)

    def test_dotdot_escape_rejected(self, client, root_fs, reveal_spy):
        r = self._post(client, {"path": str(root_fs / ".." / "outside")})
        assert r.status_code == 400
        assert reveal_spy == []  # mock 证明确实未调用 open

    def test_dotdot_inside_prefix_rejected(self, client, root_fs, reveal_spy):
        """/root/../root-evil/x 这类“字符串前缀合法、规范化后越界”的路径。"""
        r = self._post(
            client,
            {"path": str(root_fs / ".." / (root_fs.name + "-evil"))},
        )
        assert r.status_code == 400
        assert reveal_spy == []

    def test_symlink_escape_rejected(self, client, root_fs, reveal_spy):
        r = self._post(client, {"path": str(root_fs / "link-out")})
        assert r.status_code == 400
        assert reveal_spy == []

    def test_prefix_similar_root_rejected(self, client, root_fs, reveal_spy):
        """/scanroot-evil 与 /scanroot 字符串前缀相似，必须拒绝（存在于磁盘）。"""
        r = self._post(client, {"path": str(root_fs) + "-evil"})
        assert r.status_code == 400
        assert reveal_spy == []

    def test_nonexistent_in_root_404(self, client, root_fs, reveal_spy):
        r = self._post(client, {"path": str(root_fs / "no-such-dir")})
        assert r.status_code == 404
        assert reveal_spy == []

    @pytest.mark.parametrize("payload", [
        [1, 2, 3],                       # JSON 数组：非对象
        "just-a-string",                 # JSON 字符串：非对象
        42,                              # JSON 数字：非对象
        {"no_path": 1},                  # 对象但缺 path
        {"path": 123},                   # path 非字符串
        {"path": None},                  # path 为 null
        {"path": ""},                    # 空字符串
        {"path": "not/absolute"},        # 相对路径
    ])
    def test_non_object_or_invalid_path_rejected(self, client, root_fs,
                                                 reveal_spy, payload):
        r = self._post(client, payload)
        assert r.status_code == 400
        assert reveal_spy == []

    def test_relative_path_rejected(self, client, root_fs, reveal_spy):
        r = self._post(client, {"path": "sub"})
        assert r.status_code == 400
        assert reveal_spy == []

    def test_valid_path_revealed_with_canonical_path(self, client, root_fs,
                                                     reveal_spy):
        r = self._post(client, {"path": str(root_fs / "sub") + "/"})
        assert r.status_code == 200
        assert reveal_spy == [["/usr/bin/open", "-R",
                               str((root_fs / "sub").resolve())]]

    def test_root_itself_revealable(self, client, root_fs, reveal_spy):
        r = self._post(client, {"path": str(root_fs)})
        assert r.status_code == 200
        assert len(reveal_spy) == 1

    def test_reveal_without_token_rejected(self, client, root_fs, reveal_spy):
        r = client.post("/api/reveal", json={"path": str(root_fs / "sub")},
                        headers={"X-Fathom-Token": ""})
        assert r.status_code == 403
        assert reveal_spy == []

    def test_reveal_with_foreign_host_rejected(self, client, root_fs, reveal_spy):
        r = client.post("/api/reveal", json={"path": str(root_fs / "sub")},
                        headers={"host": "evil.example"})
        assert r.status_code == 403
        assert reveal_spy == []


# ---------- 读取路径 / 日报边界与凭据卫生 ----------


class TestReportAndCredentialHygiene:
    def test_slash_date_rejected_by_routing(self, client):
        """含斜杠的多段日期到不了 handler（路由不匹配，404），不会拼出越界路径。"""
        r = client.get("/api/reports/2026-09/1x")
        assert r.status_code == 404

    def test_malformed_date_rejected(self, client):
        """旧校验只看长度和第 5 位，2026-9-12 这类形状会被收紧后的格式拒绝。"""
        r = client.get("/api/reports/2026-9-12")
        assert r.status_code == 400

    @pytest.mark.parametrize("bad", ["2026-9-12", "20260912", "abcd-ef-gh",
                                     "2026-09-12extra"])
    def test_non_date_shapes_rejected(self, client, bad):
        assert client.get(f"/api/reports/{bad}").status_code == 400

    def test_valid_date_without_report_404(self, client):
        r = client.get("/api/reports/2026-09-01")
        assert r.status_code == 404

    def test_existing_report_served(self, client):
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (config.REPORTS_DIR / "2026-09-01.md").write_text(
            "# 日报\n- `<img src=x onerror=alert(1)>` 仅作文本\n", encoding="utf-8")
        r = client.get("/api/reports/2026-09-01")
        assert r.status_code == 200
        assert "onerror" in r.json()["content"]  # 原文返回，前端负责转义展示

    def test_token_not_in_status_or_reports_responses(self, client):
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (config.REPORTS_DIR / "2026-09-01.md").write_text("x", encoding="utf-8")
        for path in ("/api/status", "/api/snapshots", "/api/reports",
                     "/api/reports/2026-09-01", "/api/scan/status"):
            assert api._WRITE_TOKEN not in client.get(path).text, path

    def test_token_never_logged(self, client, caplog):
        """令牌不进日志：合法与被拒请求全程捕获，日志文本不含令牌值。"""
        with caplog.at_level(logging.DEBUG, logger="fathom.api"):
            client.get("/api/bootstrap")
            client.post("/api/scan", headers={"X-Fathom-Token": "forged"})
            client.post("/api/scan", headers={"origin": EVIL_ORIGIN})
        assert api._WRITE_TOKEN not in caplog.text

    def test_no_shell_like_endpoint_added(self):
        """本任务不提供任意 shell API：POST 路由只允许 scan/reveal 两个白名单。"""
        routes = {r.path for r in api.app.routes if getattr(r, "methods", None)
                  and "POST" in r.methods}
        assert routes == {"/api/scan", "/api/reveal"}


# ---------- 静态页安全头（浏览器渲染边界的响应侧） ----------


class TestStaticSecurityHeaders:
    def test_index_served_with_csp(self, client):
        r = client.get("/")
        assert r.status_code == 200
        csp = r.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    def test_api_response_has_security_headers_too(self, client):
        r = client.get("/api/status")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    def test_docs_still_host_gated(self, client):
        r = client.get("/docs", headers={"host": "evil.example"})
        assert r.status_code == 403


# ---------- JSON 基础形状 ----------


def test_bootstrap_shape(client):
    r = client.get("/api/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("token"), str) and len(body["token"]) >= 32
    # 令牌不经 URL 查询串发放
    assert "?" not in str(r.request.url)


def test_scan_runs_table_untouched_by_guard_rejections(client, monkeypatch):
    """连续多种被拒写入后，scan_runs 与快照表保持为空（无部分副作用）。"""
    _mock_scan_kernel(monkeypatch)
    conn = db.connect()  # 先建好 schema（等同真实服务的既有库），再尝试被拒写入
    conn.close()
    for headers in ({"host": "evil.example"},
                    {"origin": EVIL_ORIGIN},
                    {"X-Fathom-Token": "bad"}):
        client.post("/api/scan", headers=headers)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        runs = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    assert runs == 0 and snaps == 0
