//! ISS-010A：登录项与后台计划的只读状态桥 + dry-run。
//!
//! 硬边界（与父卡 ISS-010 / 切片 ISS-010A 一致）：
//! - 零系统写入：本模块只允许只读查询 `launchctl print gui/<uid>/<label>`
//!   与纯解析；禁止调用 `launchctl bootstrap/bootout/load/unload/enable/
//!   disable/kickstart`，禁止调用 `SMAppService.register/unregister`，禁止
//!   写入 `~/Library/LaunchAgents`。
//! - 不新增 crate：仅用 `std` + 已引入的 `serde_json`。
//! - SMAppService（登录项）需 objc 桥，本切片不实现；`login_item` 字段如实
//!   返回 `"unknown"`，由父卡 ISS-010 后续补齐。
//! - 设置页前端展示随 ISS-016A 一并落，本切片不交付。

use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;

/// 与 ``fathom.config`` 同源（config.py:322-324）；改这里必须同步 Python 侧。
pub const SCAN_LABEL: &str = "com.maoscripts.fathom-scan";
pub const WEB_LABEL: &str = "com.maoscripts.fathom-web";

/// 单标签探测超时（秒）：超时 → kill 自己起的子进程，状态降级为 ``Unknown``。
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// launchd 服务状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ServiceState {
    Enabled,
    Disabled,
    Unknown,
}

/// 解析 ``launchctl print gui/<uid>/<label>`` 的合并输出到 ``ServiceState``。
///
/// 决策矩阵：
/// - ``exit_code == 0`` 且输出含 ``state = running`` 或 ``pid = ...`` → ``Enabled``
/// - ``exit_code != 0`` 且输出含 ``Could not find service`` → ``Disabled``
/// - 其它（权限不足、命令缺失、解析失败、空输出）→ ``Unknown``
///
/// **Unknown 绝不映射为 Enabled**——保守降级，避免误启用反馈。
pub fn parse_launchctl_print(output: &str, exit_code: i32) -> ServiceState {
    if exit_code == 0 {
        // exit 0 但无 running/pid 行：可能是 launchd 域内的占位条目；保守为 Unknown。
        if output.contains("state = running") || output.contains("pid =") {
            return ServiceState::Enabled;
        }
        return ServiceState::Unknown;
    }
    if output.contains("Could not find service") {
        return ServiceState::Disabled;
    }
    ServiceState::Unknown
}

/// 读取当前 uid（替代 libc::getuid；不新增 crate）。
///
/// ``id -u`` 失败（命令缺失、非 Unix、未退出 0、输出非纯数字）→ ``None``，
/// 调用方据此把全部状态降级为 ``Unknown``。
fn read_uid() -> Option<String> {
    let output = Command::new("id").arg("-u").output().ok()?;
    if !output.status.success() {
        return None;
    }
    let uid = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if uid.is_empty() || !uid.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some(uid)
}

/// 单标签只读探测：``launchctl print gui/<uid>/<label>``，≤2s 超时。
///
/// 超时则 kill 自己 spawn 的子进程（绝不可能误杀外部），并返回 ``Unknown``。
/// stdout/stderr 通过子线程并发 drain，避免管道满导致子进程阻塞。
fn probe_label(label: &str, uid: &str) -> ServiceState {
    let target = format!("gui/{uid}/{label}");
    let mut child = match Command::new("launchctl")
        .arg("print")
        .arg(&target)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => return ServiceState::Unknown, // 命令缺失 / 权限拒绝 spawn
    };

    // 先抢走 stdout/stderr（保留读到 EOF 的能力）。
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let (tx, rx) = mpsc::channel::<(String, String)>();
    if let Some(mut out) = stdout {
        let tx = tx.clone();
        thread::spawn(move || {
            let mut buf = String::new();
            let _ = out.read_to_string(&mut buf);
            let _ = tx.send(("out".to_string(), buf));
        });
    }
    if let Some(mut err) = stderr {
        let tx = tx.clone();
        thread::spawn(move || {
            let mut buf = String::new();
            let _ = err.read_to_string(&mut buf);
            let _ = tx.send(("err".to_string(), buf));
        });
    }
    drop(tx); // 两线程 send 完毕即 drop sender，rx 收到关闭信号

    // 轮询 try_wait，≤2s。
    let deadline = Instant::now() + PROBE_TIMEOUT;
    let mut exited = false;
    let mut exit_code: i32 = -1;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(status)) => {
                exited = true;
                exit_code = status.code().unwrap_or(-1);
                break;
            }
            Ok(None) => thread::sleep(Duration::from_millis(50)),
            Err(_) => break,
        }
    }
    if !exited {
        // 超时：只 kill 自己 spawn 的这个子进程；wait 兜底回收，避免僵尸。
        let _ = child.kill();
        let _ = child.wait();
    }

    // 收尾：线程在子进程 EOF 后自然结束；recv 直到 channel 关闭。
    let mut stdout_buf = String::new();
    let mut stderr_buf = String::new();
    while let Ok((which, s)) = rx.recv() {
        match which.as_str() {
            "out" => stdout_buf = s,
            _ => stderr_buf = s,
        }
    }

    let combined = format!("{stdout_buf}{stderr_buf}");
    parse_launchctl_print(&combined, exit_code)
}

/// Tauri 命令：返回两标签的只读状态 + 登录项占位。
///
/// 返回 JSON object：
/// - ``scan`` / ``web``：``"enabled"`` | ``"disabled"`` | ``"unknown"``
/// - ``login_item``：始终 ``"unknown"``（SMAppService 留父卡 ISS-010）
/// - ``source``：``"launchctl print (read-only)"``
/// - ``error``：uid 读取失败时附加，其余情况不出现
#[tauri::command]
pub fn autostart_status() -> serde_json::Value {
    match read_uid() {
        Some(uid) => serde_json::json!({
            "scan": probe_label(SCAN_LABEL, &uid),
            "web": probe_label(WEB_LABEL, &uid),
            "login_item": "unknown",
            "source": "launchctl print (read-only)",
        }),
        None => serde_json::json!({
            "scan": "unknown",
            "web": "unknown",
            "login_item": "unknown",
            "source": "launchctl print (read-only)",
            "error": "无法解析当前 uid（id -u 失败）",
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// exit 0 + ``state = running`` → Enabled。
    #[test]
    fn running_state_is_enabled() {
        let s = "section system\n\tstate = running\n\tpid = 12345\n";
        assert_eq!(parse_launchctl_print(s, 0), ServiceState::Enabled);
    }

    /// exit 0 + 仅 ``pid = ...``（无 state 行）→ Enabled。
    #[test]
    fn pid_only_is_enabled() {
        let s = "details:\n\tpid = 999\n";
        assert_eq!(parse_launchctl_print(s, 0), ServiceState::Enabled);
    }

    /// exit 非 0 + "Could not find service" → Disabled。
    #[test]
    fn could_not_find_service_is_disabled() {
        let s = "Could not find service: \"gui/501/com.maoscripts.fathom-scan\" in domain gui";
        assert_eq!(parse_launchctl_print(s, 3), ServiceState::Disabled);
    }

    /// exit 非 0 + 权限错误 → Unknown（绝不映射为 Enabled）。
    #[test]
    fn permission_error_is_unknown() {
        let s = "Permission denied: cannot access gui/501/com.maoscripts.fathom-x";
        assert_eq!(parse_launchctl_print(s, 1), ServiceState::Unknown);
    }

    /// exit 0 但无 running/pid 行 → Unknown（占位条目）。
    #[test]
    fn empty_output_exit_zero_is_unknown() {
        assert_eq!(parse_launchctl_print("", 0), ServiceState::Unknown);
    }

    /// 空输出 + 退出非 0 → Unknown。
    #[test]
    fn empty_output_nonzero_is_unknown() {
        assert_eq!(parse_launchctl_print("", 1), ServiceState::Unknown);
    }

    /// 命令不存在（退出 127）→ Unknown。
    #[test]
    fn command_not_found_is_unknown() {
        let s = "/bin/sh: launchctl: command not found";
        assert_eq!(parse_launchctl_print(s, 127), ServiceState::Unknown);
    }

    /// 矩阵兜底：所有 fail-like 输入在合理 exit 下都不能坍缩为 Enabled。
    #[test]
    fn unknown_never_collapses_to_enabled() {
        let danger: &[(&str, i32)] = &[
            ("", 0),                                            // 空输出
            ("Permission denied", 1),                           // 权限错误
            ("/bin/sh: launchctl: command not found", 127),     // 命令缺失
            ("Bootstrap failed: already bootstrapped", 1),      // 任意错误
            ("Could not find service", 0),                      // exit 0 不算 Disabled
            ("state = waiting", 0),                             // 非 running
        ];
        for (inp, code) in danger {
            assert_ne!(
                parse_launchctl_print(inp, *code),
                ServiceState::Enabled,
                "input {inp:?} (exit={code}) 不应解析为 Enabled"
            );
        }
    }

    /// 矩阵兜底：所有 fail-like 输入在 exit 0 下都不能坍缩为 Disabled（必须 Unknown）。
    #[test]
    fn disabled_only_when_exit_nonzero() {
        let s = "Could not find service: gui/501/x";
        // exit 0 + 该关键字不应被判 Disabled（launchd 偶发回执存在此关键字的占位）。
        assert_ne!(parse_launchctl_print(s, 0), ServiceState::Disabled);
    }
}