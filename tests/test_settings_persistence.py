"""ISS-016A：settings.json 持久化层（fathom/config.py）。

覆盖任务卡五类 pytest 中的配置层部分：
1. 默认值：无 settings.json 时全部内置默认，旧运行根不迁移不报错；
2. 校验拒绝：计划时间/路径/有限正数（含 nan/inf/0/负数/bool/字符串）
   一律 ConfigurationError + 中文消息；
3. 原子写失败回退：os.replace 失败时旧文件不动、无临时文件残留；
4. 重启后保留：子进程干净 import 读到 settings.json（不依赖本进程状态）；
5. 环境变量优先级：FATHOM_SCAN_ROOT > settings.json（子进程钉住）。

子进程隔离沿用 TestDuTimeoutConfig 的理由：import 时点的行为在干净进程
里验证，不把半初始化状态留在测试进程内。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fathom import config

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- 默认值 ----------

def test_missing_settings_file_gives_all_defaults(tmp_path):
    settings = config.load_user_settings(tmp_path / "settings.json")
    assert settings == config.UserSettings()
    # 旧运行根语义：文件不存在不是错误，也不触发任何迁移/写入
    assert not (tmp_path / "settings.json").exists()
    assert list(tmp_path.iterdir()) == []


def test_publish_policy_values_defaults_match_documented_constants():
    config._publish_policy_values(config.UserSettings())
    assert config.MIN_DIR_KB == 10 * 1024
    assert config.FREE_ALERT_GB == 10
    assert (config.SCAN_HOUR, config.SCAN_MINUTE) == (12, 0)
    assert config.DEFAULT_SCAN_TIME == "12:00"


def test_settings_file_with_partial_keys_fills_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"min_kb": 512}), encoding="utf-8")
    settings = config.load_user_settings(path)
    assert settings.min_kb == 512.0
    assert settings.scan_root is None and settings.scan_time is None
    assert settings.free_alert_gb is None


def test_corrupt_settings_file_fails_closed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.ConfigurationError, match="settings.json"):
        config.load_user_settings(path)
    # 静默回落默认会把扫描根换掉并形成新数据集——必须是错误而不是降级
    path.write_text(json.dumps({"scan_time": "25:00"}), encoding="utf-8")
    with pytest.raises(config.ConfigurationError, match="scan_time"):
        config.load_user_settings(path)


# ---------- 校验拒绝 ----------

@pytest.mark.parametrize("bad_time", ["25:00", "12:60", "9:00", "1200", "12", "00:00:00", "aa:bb", ""])
def test_invalid_scan_time_rejected(bad_time):
    with pytest.raises(config.ConfigurationError, match="scan_time"):
        config.parse_user_settings({"scan_time": bad_time})


@pytest.mark.parametrize("good_time", ["00:00", "09:30", "23:59", "13:07"])
def test_valid_scan_time_accepted(good_time):
    assert config.parse_user_settings({"scan_time": good_time}).scan_time == good_time


@pytest.mark.parametrize("bad_number", [0, -1, -0.5, float("nan"), float("inf"),
                                        float("-inf"), "10240", True, "x"])
def test_non_positive_or_non_finite_numbers_rejected(bad_number):
    for key in ("min_kb", "free_alert_gb"):
        with pytest.raises(config.ConfigurationError, match=key):
            config.parse_user_settings({key: bad_number})


def test_valid_numbers_accepted():
    parsed = config.parse_user_settings({"min_kb": 512, "free_alert_gb": 2.5})
    assert parsed.min_kb == 512.0
    assert parsed.free_alert_gb == 2.5


def test_invalid_scan_root_shapes_rejected(tmp_path):
    file_path = tmp_path / "a-file.txt"
    file_path.write_text("x", encoding="utf-8")
    for bad in ("relative/path", str(tmp_path / "no-such-dir"), str(file_path), "", 123):
        with pytest.raises(config.ConfigurationError, match="scan_root"):
            config.parse_user_settings({"scan_root": bad})


def test_valid_scan_root_resolved(tmp_path):
    target = tmp_path / "real-dir"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    parsed = config.parse_user_settings({"scan_root": str(tmp_path / "link")})
    assert parsed.scan_root == str(target.resolve())


def test_unknown_keys_rejected():
    with pytest.raises(config.ConfigurationError, match="未知的配置项"):
        config.parse_user_settings({"min_kb": 1, "scan_excludes": []})


def test_merge_keeps_absent_fields():
    current = config.UserSettings(scan_time="09:30", min_kb=512)
    merged = config.merge_user_settings(current, {"free_alert_gb": 2.5})
    assert merged == config.UserSettings(scan_time="09:30", min_kb=512, free_alert_gb=2.5)


# ---------- 原子写失败回退 ----------

def test_atomic_write_failure_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    config.save_user_settings(path, config.UserSettings(scan_time="09:30"))
    old_bytes = path.read_bytes()

    def failing_replace(src, dst):
        raise OSError("合成磁盘故障：rename 失败")

    monkeypatch.setattr(config.os, "replace", failing_replace)
    with pytest.raises(OSError, match="合成磁盘故障"):
        config.save_user_settings(path, config.UserSettings(scan_time="23:45"))

    assert path.read_bytes() == old_bytes  # 旧文件原样保留
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "settings.json"]
    assert leftovers == [], f"临时文件残留：{leftovers}"


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    original = config.UserSettings(
        scan_time="07:15", min_kb=2048, free_alert_gb=1.5)
    config.save_user_settings(path, original)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "scan_time": "07:15", "min_kb": 2048, "free_alert_gb": 1.5}
    assert config.load_user_settings(path) == original


# ---------- 重启后保留 / 环境变量优先级（子进程干净 import） ----------

def _import_probe(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """在干净子进程里 import fathom.config 并打印生效值（重启语义）。"""
    env = dict(os.environ)
    for name in ("FATHOM_SCAN_ROOT", "FATHOM_RUNTIME_DIR", "FATHOM_DB",
                 "FATHOM_PORT", "FATHOM_RUNTIME_MODE", "FATHOM_RESOURCE_DIR"):
        env.pop(name, None)
    env.update(extra_env)
    code = (
        "import json\n"
        "from fathom import config\n"
        "print(json.dumps({\n"
        "    'scan_root': str(config.get_runtime_config().scan_root),\n"
        "    'scan_time': f'{config.SCAN_HOUR:02d}:{config.SCAN_MINUTE:02d}',\n"
        "    'min_kb': config.MIN_DIR_KB,\n"
        "    'free_alert_gb': config.FREE_ALERT_GB,\n"
        "    'db_path': str(config.get_runtime_config().db_path),\n"
        "}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )


def test_settings_survive_process_restart(tmp_path):
    """保存（落盘）后重启进程（新 import）仍生效。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    root_b = tmp_path / "root-b"
    root_b.mkdir()
    (runtime / "settings.json").write_text(json.dumps({
        "scan_root": str(root_b), "scan_time": "09:30",
        "min_kb": 512, "free_alert_gb": 2.5,
    }), encoding="utf-8")

    proc = _import_probe({"FATHOM_RUNTIME_DIR": str(runtime)})
    assert proc.returncode == 0, proc.stderr
    values = json.loads(proc.stdout.strip().splitlines()[-1])
    # macOS 的 pytest tmp_path 在 /var → /private/var 符号链接之后，统一比 resolve 后的形态
    assert values["scan_root"] == str(root_b.resolve())
    assert values["scan_time"] == "09:30"
    assert values["min_kb"] == 512
    assert values["free_alert_gb"] == 2.5


def test_env_var_scan_root_takes_precedence_over_settings(tmp_path):
    """FATHOM_SCAN_ROOT 环境变量优先于 settings.json（优先级钉住）。

    理由见 fathom/config.py 模块 docstring：环境变量是 ISS-025 以来的
    运维/隔离合同入口，落盘设置不能反超，否则一份意外落盘的文件就能
    把扫描重定向到错误根并静默形成新数据集。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    (runtime / "settings.json").write_text(json.dumps({
        "scan_root": str(root_b), "scan_time": "09:30", "min_kb": 512,
    }), encoding="utf-8")

    proc = _import_probe({
        "FATHOM_RUNTIME_DIR": str(runtime), "FATHOM_SCAN_ROOT": str(root_a)})
    assert proc.returncode == 0, proc.stderr
    values = json.loads(proc.stdout.strip().splitlines()[-1])
    assert values["scan_root"] == str(root_a.resolve())  # 环境变量赢
    assert values["min_kb"] == 512  # 其余字段仍来自 settings.json


def test_defaults_without_settings_file_in_subprocess(tmp_path):
    """无 settings.json 的旧运行根：全部内置默认，不迁移不报错。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    proc = _import_probe({"FATHOM_RUNTIME_DIR": str(runtime)})
    assert proc.returncode == 0, proc.stderr
    values = json.loads(proc.stdout.strip().splitlines()[-1])
    assert values["scan_time"] == "12:00"
    assert values["min_kb"] == 10 * 1024
    assert values["free_alert_gb"] == 10
    assert not (runtime / "settings.json").exists()  # import 不写文件
