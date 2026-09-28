"""ISS-111：GET /api/permissions 权限状态契约 + /api/status 的 app_version。

设置页「权限」分区（用户裁决：应有一个权限管理页面，能看到已授予权限、
点击去授权）直接消费本端点的三块数据：

1. ``fda``：完全磁盘访问只读探测三态。真实 os.scandir 无法在隔离环境
   稳定构造 PermissionError（探测结果取决于运行进程是否被 TCC 授权），
   因此 granted/denied/unknown 三路全部经 ``api._fda_scandir`` 注入驱动：
   - granted：可读路径正常返回条目数；
   - denied ：PermissionError（TCC 拦截的典型表现）；
   - unknown：其他 OSError——探测异常如实 unknown，不伪造已授权。
2. ``notification``：最近一次扫描运行的 notification_status（scan_runs /
   scan_run_details 既有数据，原样转出；无记录 null=未登记）。
3. ``coverage``：最近快照的 dir/denied/vanished 计数引用（ISS-091 数据
   迁入「权限」分区），空库逐字段 null。

另含两条边角契约：
- /api/status 增量追加 ``app_version``（关于页版本号不再依赖检查更新）；
- Host 守卫与 /api/status 同一中间件（外站 Host 403）。

测试隔离沿用 test_api_status_coverage.py 的夹具风格：全部可写路径指向
临时目录；全程只读，不触碰真实 TCC。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import fathom
from fathom import api, config, db


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """运行根/扫描根/DB 全部指向临时目录（与 test_api_status_coverage.py 同口径）。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    cfg = config.RuntimeConfig.from_env(
        {"FATHOM_RUNTIME_DIR": str(runtime), "FATHOM_SCAN_ROOT": str(scanroot)},
        project_root=tmp_path, home=tmp_path / "home",
    )
    monkeypatch.setattr(config, "_ACTIVE", cfg)
    monkeypatch.setattr(config, "_USER_SETTINGS", config.UserSettings())
    monkeypatch.setattr(config, "DATA_DIR", cfg.data_dir)
    monkeypatch.setattr(config, "DB_PATH", cfg.db_path)
    monkeypatch.setattr(config, "DEFAULT_ROOT", cfg.scan_root)
    monkeypatch.setattr(config, "PORT", cfg.port)
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        yield c


def _seed_run(notification_status: str | None) -> None:
    """登记一条 done 运行 + detail（notification_status 可空=未登记）。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO scan_runs(started_at, finished_at, status, message) "
            "VALUES ('2026-09-28T12:00:00', '2026-09-28T12:00:42', 'done', NULL)"
        )
        run_id = cur.lastrowid
        conn.execute(
            "INSERT INTO scan_run_details(run_id, source, phase, owner_id, owner_pid, "
            "owner_started, heartbeat_at, snapshot_id, report_status, notification_status) "
            "VALUES (?, 'api', 'retention', 'owner-x', 4242, '2026-09-28T12:00:00', "
            "'2026-09-28T12:00:40', NULL, 'written', ?)",
            (run_id, notification_status),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_snapshot(root: str, *, dir_count: int, denied_count: int,
                   vanished_count: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, vanished_count) "
            "VALUES ('2026-09-28T12:00:00', ?, ?, ?, 1.0, 100, 10240, 'partial', ?)",
            (root, dir_count, denied_count, vanished_count),
        )
        conn.commit()
    finally:
        conn.close()


def test_permissions_fda_granted_on_readable_dir(client, tmp_path, monkeypatch):
    """可读路径：探测返回 granted，条目数与探测路径如实转出。"""
    probe_dir = tmp_path / "containers"
    (probe_dir / "app.one").mkdir(parents=True)
    (probe_dir / "app.two").mkdir()
    monkeypatch.setattr(api, "_fda_scandir", lambda path: 2)
    body = client.get("/api/permissions").json()
    assert body["fda"]["status"] == "granted"
    assert body["fda"]["entries"] == 2
    assert body["fda"]["probe_path"].endswith("Library/Containers")


def test_permissions_fda_denied_on_permission_error(client, monkeypatch):
    """PermissionError（TCC 拦截只读列举）：三态=denied，不伪造已授权。"""
    def _deny(path):
        raise PermissionError(f"Operation not permitted: {path}")
    monkeypatch.setattr(api, "_fda_scandir", _deny)
    body = client.get("/api/permissions").json()
    assert body["fda"]["status"] == "denied"
    assert body["fda"]["entries"] is None


def test_permissions_fda_unknown_on_probe_failure(client, monkeypatch):
    """探测异常（路径不存在/IO 错误）：三态=unknown 并带异常类别，不猜。"""
    def _boom(path):
        raise NotADirectoryError(20, "Not a directory")
    monkeypatch.setattr(api, "_fda_scandir", _boom)
    body = client.get("/api/permissions").json()
    assert body["fda"]["status"] == "unknown"
    assert body["fda"]["detail"] == "NotADirectoryError"
    assert body["fda"]["entries"] is None


def test_permissions_notification_status_passthrough(client):
    """最近一次运行的 notification_status 原样转出（submitted）。"""
    _seed_run("submitted")
    body = client.get("/api/permissions").json()
    notif = body["notification"]
    assert notif["notification_status"] == "submitted"
    assert notif["run_status"] == "done"
    assert notif["finished_at"] == "2026-09-28T12:00:42"


def test_permissions_notification_unrecorded_when_detail_missing(client):
    """运行存在但 detail 未登记通知（如旧记录）：null=未登记，不推断。"""
    _seed_run(None)
    body = client.get("/api/permissions").json()
    assert body["notification"]["notification_status"] is None


def test_permissions_notification_null_when_no_runs(client):
    """空运行历史：notification 逐字段 null，不 500。"""
    body = client.get("/api/permissions").json()
    notif = body["notification"]
    assert notif["run_id"] is None
    assert notif["notification_status"] is None


def test_permissions_coverage_references_latest_snapshot(client):
    """最近快照的 dir/denied/vanished 计数引用（ISS-091 数据迁入的契约）。"""
    _seed_snapshot(str(config.DEFAULT_ROOT), dir_count=1200,
                   denied_count=6, vanished_count=4)
    body = client.get("/api/permissions").json()
    cov = body["coverage"]
    assert cov["snapshot_id"] is not None
    assert cov["dir_count"] == 1200
    assert cov["denied_count"] == 6
    assert cov["vanished_count"] == 4
    assert cov["created_at"] == "2026-09-28T12:00:00"


def test_permissions_coverage_null_when_empty_library(client):
    """空库：coverage 逐字段 null（前端据此显示「尚未扫描」，不渲染 0）。"""
    body = client.get("/api/permissions").json()
    cov = body["coverage"]
    assert cov["snapshot_id"] is None
    assert cov["dir_count"] is None
    assert cov["denied_count"] is None
    assert cov["vanished_count"] is None


def test_status_carries_app_version(client):
    """/api/status 增量追加 app_version（关于页版本号回填源，向后兼容）。"""
    body = client.get("/api/status").json()
    assert body["app_version"] == fathom.__version__


def test_permissions_rejects_foreign_host(client):
    """权限端点与 /api/status 同受 local_boundary_guard 约束：外站 Host 403。"""
    foreign = f"http://evil.example:{config.PORT}"
    resp = client.get("/api/permissions", headers={"Host": "evil.example"})
    assert resp.status_code == 403
    # 同一请求换成可信 Host 即 200（守卫是唯一闸门，端点自身无额外放行逻辑）
    ok = client.get("/api/permissions", headers={"Host": f"127.0.0.1:{config.PORT}"})
    assert ok.status_code == 200
    assert foreign not in ok.text
