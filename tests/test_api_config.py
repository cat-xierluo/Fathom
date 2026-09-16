"""ISS-016A：GET/PUT /api/config 的 API 合同。

覆盖：
1. GET 默认值与来源标注（sources/defaults/policies/settings_path）；
2. PUT 有效保存：原子落盘 + 进程内生效 + requires_user_action 结构；
3. PUT 校验拒绝：400 + 中文 detail，旧值仍被 GET 返回（文件与生效值都不动）；
4. 守卫：PUT 与既有写方法同一 Host/Origin/写令牌边界；
5. 原子写失败：500 + 旧文件未改动；
6. 环境变量优先级：FATHOM_SCAN_ROOT 存在时 PUT 不改变生效扫描根；
7. 换根：旧数据集（snapshots 行）保留且可按 (root, min_kb) 区分（ISS-021）；
8. 重启后保留：PUT 之后的新进程 import 读到保存值。

测试隔离沿用 test_api_security.py 的夹具风格：全部可写路径指向临时目录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fathom import api, config, db

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIL_ORIGIN = "http://evil.example"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """运行根/扫描根/DB 全部指到临时目录；模块常量注册自动恢复。

    _ACTIVE 用显式 env 构造（含 FATHOM_SCAN_ROOT），但进程 os.environ 不设
    该变量——refresh_user_settings 以进程环境判定优先级，默认分支因此可测；
    需要环境变量场景的用例自行 monkeypatch.setenv。
    """
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
    monkeypatch.setattr(config, "_CLI_SCAN_ROOT_PINNED", False)
    # 兼容常量与 _ACTIVE 对齐：refresh 会按 _ACTIVE 重发布它们，起始值
    # 也必须指向临时运行根，避免任何查询落到真实 repo data/
    monkeypatch.setattr(config, "DATA_DIR", cfg.data_dir)
    monkeypatch.setattr(config, "REPORTS_DIR", cfg.reports_dir)
    monkeypatch.setattr(config, "LOGS_DIR", cfg.logs_dir)
    monkeypatch.setattr(config, "DB_PATH", cfg.db_path)
    monkeypatch.setattr(config, "FRONTEND_DIR", cfg.frontend_dir)
    monkeypatch.setattr(config, "DEFAULT_ROOT", cfg.scan_root)
    monkeypatch.setattr(config, "PORT", cfg.port)
    for name in ("MIN_DIR_KB", "FREE_ALERT_GB", "SCAN_HOUR", "SCAN_MINUTE"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config, "EXCLUDE_NAMES", [])
    monkeypatch.setattr(api, "_scan_lock", threading.Lock())


@pytest.fixture
def client():
    """与真实客户端同一合同：合法 Host（base_url）+ 写令牌（bootstrap 后注入）。"""
    with TestClient(api.app, base_url=f"http://127.0.0.1:{config.PORT}") as c:
        c.headers["X-Fathom-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c


def _settings_file() -> Path:
    return config.get_runtime_config().runtime_dir / "settings.json"


def _seed_snapshot(root: str, min_kb: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status) "
            "VALUES (?, ?, 1, 0, 1.0, 100, ?, 'full')",
            ("2026-09-15T12:00:00", root, min_kb),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- GET：默认值与结构 ----------

def test_get_config_defaults_without_settings_file(client):
    body = client.get("/api/config").json()
    assert body["scan_root"] == str(config.DEFAULT_ROOT)
    assert body["scan_time"] == "12:00"
    assert body["min_kb"] == 10 * 1024
    assert body["free_alert_gb"] == 10
    assert body["exclude_names"] == []
    # 进程环境未设 FATHOM_SCAN_ROOT、无 settings.json：全部默认来源
    assert body["sources"] == {
        "scan_root": "default", "scan_time": "default",
        "min_kb": "default", "free_alert_gb": "default",
        "exclude_names": "default",
    }
    assert body["defaults"]["scan_time"] == "12:00"
    assert body["policies"]["du_timeout_s"] == config.DU_TIMEOUT_S
    assert body["settings_path"] == str(_settings_file())
    assert not _settings_file().exists()  # GET 不产生写入


# ---------- PUT：有效保存 ----------

def test_put_config_valid_applies_and_persists(client, tmp_path):
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    response = client.put("/api/config", json={
        "scan_root": str(other_root), "scan_time": "09:30",
        "min_kb": 512, "free_alert_gb": 2.5,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["service_reload"] == "requires_user_action"
    assert "重新安装" in body["hint"]

    # 原子落盘：只含已保存键（路径按 resolve 后形态保存）
    data = json.loads(_settings_file().read_text(encoding="utf-8"))
    assert data == {"scan_root": str(other_root.resolve()), "scan_time": "09:30",
                    "min_kb": 512, "free_alert_gb": 2.5}
    # 进程内立即生效（applied=true 是真话）
    view = client.get("/api/config").json()
    assert view["scan_root"] == str(other_root.resolve())
    assert view["scan_time"] == "09:30"
    assert view["min_kb"] == 512
    assert view["free_alert_gb"] == 2.5
    assert view["sources"]["min_kb"] == "settings"
    assert config.get_runtime_config().scan_root == other_root.resolve()
    assert config.MIN_DIR_KB == 512 and config.FREE_ALERT_GB == 2.5
    assert (config.SCAN_HOUR, config.SCAN_MINUTE) == (9, 30)


def test_put_config_partial_update_keeps_other_fields(client, tmp_path):
    assert client.put("/api/config", json={"scan_time": "08:00"}).status_code == 200
    assert client.put("/api/config", json={"min_kb": 256}).status_code == 200
    view = client.get("/api/config").json()
    assert view["scan_time"] == "08:00"  # 上一次保存的字段仍在
    assert view["min_kb"] == 256
    data = json.loads(_settings_file().read_text(encoding="utf-8"))
    assert data == {"scan_time": "08:00", "min_kb": 256}


# ---------- PUT：校验拒绝（400 + 中文 detail + 旧值仍可读） ----------

@pytest.mark.parametrize("payload, match", [
    ({"scan_time": "25:00"}, "scan_time"),
    ({"scan_time": "12:60"}, "scan_time"),
    ({"scan_time": "9:00"}, "scan_time"),
    ({"min_kb": 0}, "min_kb"),
    ({"min_kb": -5}, "min_kb"),
    ({"min_kb": "512"}, "min_kb"),
    ({"min_kb": True}, "min_kb"),
    ({"scan_root": "relative/path"}, "scan_root"),
    ({"scan_root": "/no/such/dir-at-all"}, "scan_root"),
    ({"unknown_key": 1}, "未知的配置项"),
])
def test_put_config_invalid_rejected_old_value_still_served(client, payload, match):
    assert client.put("/api/config", json={"scan_time": "10:15"}).status_code == 200
    before = client.get("/api/config").json()

    response = client.put("/api/config", json=payload)
    assert response.status_code == 400
    assert match in response.json()["detail"]

    # 拒绝后：GET 仍返回旧值，落盘文件未被改写
    assert client.get("/api/config").json() == before
    on_disk = json.loads(_settings_file().read_text(encoding="utf-8"))
    assert on_disk == {"scan_time": "10:15"}


@pytest.mark.parametrize("raw_body", [
    '{"min_kb": NaN}',
    '{"min_kb": Infinity}',
    '{"min_kb": -Infinity}',
    '{"free_alert_gb": NaN}',
    '{"free_alert_gb": Infinity}',
])
def test_put_config_json_nan_literals_rejected(client, raw_body):
    """JSON 的 NaN/Infinity 字面量（Python json 可解析、JSON 标准不允许）
    必须被有限性校验拒绝，不能绕过 ``<= 0`` 检查（ISS-061 同款防线）。"""
    assert client.put("/api/config", json={"scan_time": "10:15"}).status_code == 200
    before = client.get("/api/config").json()

    response = client.put("/api/config", content=raw_body,
                          headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "有限" in response.json()["detail"] or "正数" in response.json()["detail"]
    assert client.get("/api/config").json() == before


def test_put_config_non_object_and_broken_json_rejected(client):
    response = client.put("/api/config", content="[1,2]", headers={
        "Content-Type": "application/json"})
    assert response.status_code == 400
    assert "JSON 对象" in response.json()["detail"]
    response = client.put("/api/config", content="{broken", headers={
        "Content-Type": "application/json"})
    assert response.status_code == 400
    assert "JSON" in response.json()["detail"]
    assert not _settings_file().exists()


def test_put_config_scan_root_must_be_existing_directory(client, tmp_path):
    file_path = tmp_path / "plain.txt"
    file_path.write_text("x", encoding="utf-8")
    for bad in (str(file_path), str(tmp_path / "missing")):
        response = client.put("/api/config", json={"scan_root": bad})
        assert response.status_code == 400
        assert "scan_root" in response.json()["detail"]


# ---------- 守卫：PUT 与既有写方法同一边界 ----------

def test_put_config_without_token_rejected(client):
    response = client.put("/api/config", json={"scan_time": "09:00"},
                          headers={"X-Fathom-Token": ""})
    assert response.status_code == 403
    assert not _settings_file().exists()


def test_put_config_with_wrong_token_rejected(client):
    response = client.put("/api/config", json={"scan_time": "09:00"},
                          headers={"X-Fathom-Token": "forged-token"})
    assert response.status_code == 403
    assert not _settings_file().exists()


def test_put_config_with_foreign_host_rejected(client):
    response = client.put("/api/config", json={"scan_time": "09:00"},
                          headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert not _settings_file().exists()


def test_put_config_with_cross_site_origin_rejected(client):
    response = client.put("/api/config", json={"scan_time": "09:00"},
                          headers={"Origin": EVIL_ORIGIN})
    assert response.status_code == 403
    assert not _settings_file().exists()


def test_get_config_still_readable_without_token(client):
    response = client.get("/api/config", headers={"X-Fathom-Token": ""})
    assert response.status_code == 200


# ---------- 原子写失败回退 ----------

def test_put_config_write_failure_returns_500_and_keeps_old_file(client, monkeypatch):
    assert client.put("/api/config", json={"scan_time": "10:15"}).status_code == 200
    old_bytes = _settings_file().read_bytes()

    def failing_replace(src, dst):
        raise OSError("合成磁盘故障：rename 失败")

    monkeypatch.setattr(config.os, "replace", failing_replace)
    response = client.put("/api/config", json={"scan_time": "23:59"})
    assert response.status_code == 500
    assert "旧文件未改动" in response.json()["detail"]
    assert _settings_file().read_bytes() == old_bytes
    # 进程内生效值也保持旧值
    assert client.get("/api/config").json()["scan_time"] == "10:15"
    leftovers = [p.name for p in _settings_file().parent.iterdir()
                 if p.name != "settings.json"]
    assert leftovers == [], f"临时文件残留：{leftovers}"


# ---------- 环境变量优先级（API 路径） ----------

def test_put_scan_root_cannot_override_env_var(client, tmp_path, monkeypatch):
    env_root = (tmp_path / "scanroot").resolve()  # 夹具 _ACTIVE 的扫描根
    monkeypatch.setenv("FATHOM_SCAN_ROOT", str(env_root))
    other_root = tmp_path / "other-root"
    other_root.mkdir()

    response = client.put("/api/config", json={"scan_root": str(other_root)})
    assert response.status_code == 200  # 保存成功（写入 settings.json）…
    view = client.get("/api/config").json()
    assert view["scan_root"] == str(env_root)  # …但生效值仍是环境变量
    assert view["sources"]["scan_root"] == "env"
    # 落盘值如实保留，供环境变量移除后生效
    on_disk = json.loads(_settings_file().read_text(encoding="utf-8"))
    assert on_disk["scan_root"] == str(other_root.resolve())


# ---------- 换根：旧数据集保留且可区分（ISS-021 口径） ----------

def test_scan_root_change_preserves_old_dataset(client, tmp_path):
    old_root = str(config.DEFAULT_ROOT)
    _seed_snapshot(old_root, 10 * 1024)
    new_root = tmp_path / "new-root"
    new_root.mkdir()

    assert client.put("/api/config", json={
        "scan_root": str(new_root), "min_kb": 512}).status_code == 200

    rows = client.get("/api/snapshots").json()
    assert len(rows) == 1
    assert rows[0]["root"] == old_root          # 旧数据不删
    assert rows[0]["min_kb"] == 10 * 1024       # 旧口径 (root, min_kb) 不变
    assert client.get("/api/config").json()["scan_root"] == str(new_root.resolve())
    # 同一查询按数据集身份区分：新根下没有任何混入旧根的行
    conn = sqlite3.connect(config.get_runtime_config().db_path)
    try:
        mixed = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE root != ? AND min_kb = ?",
            (old_root, 10 * 1024)).fetchone()[0]
        assert mixed == 0
    finally:
        conn.close()


# ---------- 重启后保留（API 保存 → 新进程 import） ----------

def test_put_config_survives_process_restart(client):
    assert client.put("/api/config", json={
        "scan_time": "06:45", "min_kb": 128, "free_alert_gb": 4.5,
    }).status_code == 200
    runtime = config.get_runtime_config().runtime_dir
    env = dict(os.environ)
    for name in ("FATHOM_SCAN_ROOT", "FATHOM_RUNTIME_DIR", "FATHOM_DB",
                 "FATHOM_PORT", "FATHOM_RUNTIME_MODE", "FATHOM_RESOURCE_DIR"):
        env.pop(name, None)
    env["FATHOM_RUNTIME_DIR"] = str(runtime)
    code = (
        "import json\n"
        "from fathom import config\n"
        "print(json.dumps({'scan_time': f'{config.SCAN_HOUR:02d}:{config.SCAN_MINUTE:02d}',"
        " 'min_kb': config.MIN_DIR_KB, 'free_alert_gb': config.FREE_ALERT_GB}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "scan_time": "06:45", "min_kb": 128, "free_alert_gb": 4.5}


# ---------- ISS-066：exclude_names 端点合同 ----------

def test_get_config_includes_exclude_names_default(client):
    """GET 默认包含 exclude_names 空列表 + 来源 default。"""
    body = client.get("/api/config").json()
    assert body["exclude_names"] == []
    assert body["sources"]["exclude_names"] == "default"


def test_put_config_exclude_names_valid_canonical_and_survives(client):
    """PUT 接受 exclude_names 列表 → 排序去重后落盘；进程内立即生效。"""
    response = client.put("/api/config", json={
        "exclude_names": ["b", "a", "a", "c"],
    })
    assert response.status_code == 200
    assert response.json()["applied"] is True
    data = json.loads(_settings_file().read_text(encoding="utf-8"))
    # 落盘是规范串（排序去重后 ``;`` 拼接）。
    assert data == {"exclude_names": "a;b;c"}
    view = client.get("/api/config").json()
    assert view["exclude_names"] == ["a", "b", "c"]
    assert view["sources"]["exclude_names"] == "settings"


@pytest.mark.parametrize("payload, match", [
    ([""], "exclude_names"),
    (["dir/child"], "exclude_names"),
    ([".."], "exclude_names"),
    (["."], "exclude_names"),
    (["bad\x00name"], "exclude_names"),
    ([f"mask{i}" for i in range(51)], "exclude_names"),
    ({"k": "v"}, "exclude_names"),
    (42, "exclude_names"),
])
def test_put_config_exclude_names_invalid_rejected(client, payload, match):
    """非法 exclude_names：400 + 中文 detail；旧值仍被 GET 返回。"""
    # 先设置一个合法值作为基准。
    assert client.put("/api/config", json={
        "exclude_names": ["keep"]
    }).status_code == 200
    before = client.get("/api/config").json()

    response = client.put("/api/config", json={"exclude_names": payload})
    assert response.status_code == 400
    assert match in response.json()["detail"]
    # 拒绝后 GET 仍返回旧值，落盘未被改写。
    assert client.get("/api/config").json() == before


def test_put_config_empty_exclude_names_clears(client):
    """空列表是合法的清空操作（不是 400）。"""
    assert client.put("/api/config", json={
        "exclude_names": ["keep"]
    }).status_code == 200
    response = client.put("/api/config", json={"exclude_names": []})
    assert response.status_code == 200
    view = client.get("/api/config").json()
    assert view["exclude_names"] == []


def test_api_status_includes_vanished_count_and_exclude_names(client):
    """ISS-066：/api/status 暴露 vanished_count 与生效 exclude_names 供前端消费。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, "
            "vanished_count, exclude_names) "
            "VALUES (?, ?, 1, 0, 1.0, 100, 1024, 'partial', 3, 'skip.noindex')",
            ("2026-09-15T12:00:00", "/synthetic/root",),
        )
        conn.commit()
        sid = cur.lastrowid
    finally:
        conn.close()

    status = client.get("/api/status").json()
    assert "vanished_count" in status, "ISS-002A 前端消费 vanished 必须有显式字段"
    assert status["vanished_count"] == 3
    assert "exclude_names" in status
    assert status["exclude_names"] == []  # 当前配置无排除集
    # 最新快照里的 exclude_names 也如实暴露。
    latest = status["latest_snapshot"]
    assert latest["id"] == sid
    assert latest["exclude_names"] == "skip.noindex"
    assert latest["vanished_count"] == 3


def test_api_status_no_snapshot_zero_fields(client):
    """无快照时 /api/status 不假装有 vanished/exclude 含义。"""
    status = client.get("/api/status").json()
    assert status["vanished_count"] == 0
    assert status["exclude_names"] == []


# ---------- /api/snapshots 补齐快照级缺口字段（ISS-067） ----------

def _seed_snapshot_with_coverage(
    root: str, min_kb: int, *, vanished_count: int, exclude_names: str,
    created_at: str = "2026-09-15T12:00:00",
) -> int:
    """写入一条带 vanished/exclude 的快照行，返回 id。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO snapshots(created_at, root, dir_count, denied_count, "
            "du_seconds, total_kb, min_kb, collection_status, "
            "vanished_count, exclude_names) "
            "VALUES (?, ?, 1, 0, 1.0, 100, ?, 'partial', ?, ?)",
            (created_at, root, min_kb, vanished_count, exclude_names),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_api_snapshots_exposes_vanished_count_and_exclude_names(client):
    """ISS-067：/api/snapshots 的每行必须带 vanished_count 与 exclude_names。

    ISS-002A 的 overview 覆盖说明读的是 /api/snapshots（不是 /api/status）；
    旧 SELECT 漏了这两列，导致「扫描期间消失」与「排除掩码」两类缺口在生产
    中恒为 0/空，前端 `?? 0` / `?? []` 防御把缺失静默降级。
    """
    sid = _seed_snapshot_with_coverage(
        str(config.DEFAULT_ROOT), 10 * 1024,
        vanished_count=3, exclude_names="*.noindex;*.tmp",
    )

    rows = client.get("/api/snapshots").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == sid
    assert "vanished_count" in row, "overview 消费 vanished 必须由本端点提供"
    assert row["vanished_count"] == 3
    assert "exclude_names" in row, "overview 消费 excluded 必须由本端点提供"
    # 与 DB 层一致：快照行里存的是 ';' 分隔串（dataset 身份口径）。
    assert row["exclude_names"] == "*.noindex;*.tmp"


def test_api_snapshots_empty_exclude_names_is_empty_string(client):
    """默认全量采集（掩码空）→ exclude_names 为空串，前端解析为 0 项。"""
    _seed_snapshot(str(config.DEFAULT_ROOT), 10 * 1024)

    rows = client.get("/api/snapshots").json()
    assert rows[0]["exclude_names"] == ""
    assert rows[0]["vanished_count"] == 0


def test_api_snapshots_covers_every_row_not_just_latest(client):
    """列表端点须逐行补齐，历史行不得因只改最新行而缺失字段。"""
    _seed_snapshot_with_coverage(
        str(config.DEFAULT_ROOT), 10 * 1024,
        vanished_count=0, exclude_names="", created_at="2026-09-13T12:00:00",
    )
    _seed_snapshot_with_coverage(
        str(config.DEFAULT_ROOT), 10 * 1024,
        vanished_count=5, exclude_names="*.cache", created_at="2026-09-15T12:00:00",
    )

    rows = client.get("/api/snapshots").json()
    assert len(rows) == 2
    # 端点按 created_at DESC 排序：最新在前。
    assert rows[0]["vanished_count"] == 5
    assert rows[0]["exclude_names"] == "*.cache"
    assert rows[1]["vanished_count"] == 0
    assert rows[1]["exclude_names"] == ""


def test_api_snapshots_exclude_names_is_snapshot_not_live_config(client):
    """ISS-067 核心接缝：exclude_names 必须是快照采集时的值。

    改配置后（PUT /api/config）旧快照的 dataset 身份不得漂移——若用配置层
    生效值顶替，用户改一次排除集，全部历史快照都会被打上当前掩码。
    """
    _seed_snapshot_with_coverage(
        str(config.DEFAULT_ROOT), 10 * 1024,
        vanished_count=0, exclude_names="*.oldmask",
    )

    # 之后把当前生效配置改成完全不同的掩码。
    assert client.put("/api/config", json={
        "exclude_names": ["*.newmask"]}).status_code == 200
    assert client.get("/api/config").json()["exclude_names"] == ["*.newmask"]

    rows = client.get("/api/snapshots").json()
    # 快照行如实返回自己的采集掩码，不受当前配置影响。
    assert rows[0]["exclude_names"] == "*.oldmask"
