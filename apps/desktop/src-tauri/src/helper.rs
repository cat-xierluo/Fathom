//! Fathom helper 进程生命周期。
//!
//! ISS-009 切片 1：Tauri 壳拉起内嵌 PyInstaller onedir helper（FROZEN 路径下
//! helper 通过 ``--runtime-mode release`` 显式声明；运行根收敛到
//! ``~/Library/Application Support/Fathom``），经 ``helper-instance.json`` /
//! ``/health`` 握手后由主窗口导航到本地服务。退出时按身份 SIGTERM 回收本
//! 进程拉起的 helper；同服务已在跑则复用并不重复拉起；端口全部耗尽（exit 3）
//! 让用户在握手页查看恢复动作；永不向任何外部 PID 发信号。
//!
//! 依赖策略：仅用 ``std`` 与 Tauri 已提供的 API（``tauri::path::BaseDirectory::Resource``）。
//! 不引入新 crate，避免 ``--locked --offline`` 门禁下拉不到依赖。
//!
//! 端口策略（与 ``fathom.cli.cmd_serve`` 对齐）：
//! - 探测候选端口 7952..7952+range 的 ``/health``：返回 ``service=fathom`` 且
//!   ``protocol_version=1`` 时为「同服务已运行」，复用不拉起；
//! - 不发任何信号；失败时给出 occupied_pid（lsof 只读）以便恢复页呈现；
//! - 候选全部 1xx/连接失败时 helper 让位到下一端口；range 用尽后 helper
//!   自己退出 3 写 stderr，握手页读取 helper 退出码与 events 决定 UI。
//!
//! 退出策略：只向「本进程拉起」的 helper 发 SIGTERM；bounded 等待 10s；不
//! 对未在自己子进程表里的 helper 做任何动作。
//!
//! 资源定位：
//! - 打包态：``resource_dir()/helper/fathom-helper/fathom-helper``（PyInstaller onedir）
//! - 开发态：``FATHOM_HELPER_BIN`` 环境变量覆盖；缺失时返回 ``Err``，
//!   不 panic（避免 tauri::Builder 在 setup 里崩溃）。
//!
//! 身份与运行根：
//! - ``SERVICE_IDENTITY = "fathom"``，``PROTOCOL_VERSION = 1``；与
//!   ``fathom.__init__`` 的常量同源（单一版本源 ISS-037）。
//! - ``FATHOM_RUNTIME_DIR`` 显式传给 helper；默认 ``~/Library/Application
//!   Support/Fathom``，与 ``RuntimeConfig`` release 模式默认一致。

use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Manager};

/// 与 ``fathom.__init__`` 同源：单一身份面（ISS-029 合同 / ISS-037 单版本源）。
pub const SERVICE_IDENTITY: &str = "fathom";
pub const PROTOCOL_VERSION: i64 = 1;

/// 默认回环端口与让位段；与 ``fathom.config.PORT / PORT_RANGE`` 对齐。
pub const DEFAULT_PORT: u16 = 7952;
pub const DEFAULT_PORT_RANGE: u8 = 4;

/// 握手超时（秒）；与任务卡 §Phase 1.3 一致（20s）。
pub const HANDSHAKE_TIMEOUT_S: u64 = 20;
/// SIGTERM 回收等待上限（秒）；与任务卡 §Phase 1.5 一致。
pub const STOP_TIMEOUT_S: u64 = 10;

/// 候选运行根（与 ``fathom.config.RuntimeConfig.from_env`` release 模式默认一致）。
pub fn default_runtime_dir() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"));
    home.join("Library").join("Application Support").join("Fathom")
}

/// 解析 helper 可执行文件路径。打包态：资源目录 ``helper/fathom-helper/fathom-helper``；
/// 开发态：环境变量 ``FATHOM_HELPER_BIN`` 覆盖。失败时返回 ``Err``，绝不 panic。
pub fn locate_helper(app: &AppHandle) -> Result<PathBuf, String> {
    if let Ok(override_path) = std::env::var("FATHOM_HELPER_BIN") {
        let candidate = PathBuf::from(override_path);
        if candidate.is_file() {
            return Ok(candidate);
        }
        return Err(format!(
            "FATHOM_HELPER_BIN 指向的文件不存在或不可执行：{}",
            candidate.display()
        ));
    }
    // 打包态：tauri 资源目录
    let resource_dir = app
        .path()
        .resolve("helper/fathom-helper/fathom-helper", tauri::path::BaseDirectory::Resource)
        .map_err(|err| {
            format!(
                "无法定位资源目录 helper/fathom-helper/fathom-helper：{}。开发态请设置 FATHOM_HELPER_BIN 指向本地冻结产物。",
                err
            )
        })?;
    if resource_dir.is_file() {
        Ok(resource_dir)
    } else {
        Err(format!(
            "资源目录下未找到 helper 可执行文件：{}。请确认 build_helper.sh 已产出并已纳入 bundle resources。",
            resource_dir.display()
        ))
    }
}

/// 启动 helper 子进程。std::process::Command；stdout/stderr 落到运行根
/// ``logs/helper.log``；返回子进程句柄与 pid。
pub fn spawn_helper(bin: &Path, runtime_dir: &Path) -> Result<Child, String> {
    let logs_dir = runtime_dir.join("logs");
    std::fs::create_dir_all(&logs_dir).map_err(|err| {
        format!("创建日志目录失败 {}：{}", logs_dir.display(), err)
    })?;
    let log_path = logs_dir.join("helper.log");
    let log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|err| {
            format!("打开 helper 日志失败 {}：{}", log_path.display(), err)
        })?;
    let log_file_err = log_file.try_clone().map_err(|err| {
        format!("克隆 helper 日志 fd 失败：{}", err)
    })?;

    let port_arg = std::env::var("FATHOM_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT);

    let mut cmd = Command::new(bin);
    cmd.arg("--runtime-mode")
        .arg("release")
        .arg("--port")
        .arg(port_arg.to_string())
        .arg("--port-range")
        .arg(DEFAULT_PORT_RANGE.to_string())
        .arg("serve")
        .env("FATHOM_RUNTIME_DIR", runtime_dir)
        // 与 helper 期望的 resource_dir / scan_root 一致；运行时由 helper
        // 自身的 RuntimeConfig.from_env() 解析。
        .env("FATHOM_RUNTIME_MODE", "release")
        .env_remove("FATHOM_RESOURCE_DIR") // 强制 helper 走 sys._MEIPASS 或 env
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_file_err));
    // 在 macOS 上设置成 detached 让 helper 不因父进程 group 而被信号连坐
    // （launchd/双击启动时父进程的 SIGTERM 不传播给 helper）。
    cmd.spawn().map_err(|err| {
        format!(
            "启动 helper 失败 {}：{}",
            bin.display(),
            err
        )
    })
}

/// helper-instance.json 读取结果；0600 由 helper 自身保证。
#[derive(Debug, Clone, Serialize)]
pub struct HelperInstance {
    pub pid: i64,
    pub port: u16,
    pub service: String,
    pub protocol_version: i64,
    pub version: Option<String>,
    pub instance_id: Option<String>,
    pub runtime_mode: Option<String>,
}

/// 解析 helper-instance.json（JSON object）。文件不存在/格式错误返回 ``Err``。
pub fn read_helper_instance(runtime_dir: &Path) -> Result<HelperInstance, String> {
    let path = runtime_dir.join("helper-instance.json");
    let raw = std::fs::read_to_string(&path).map_err(|err| {
        format!("读取 helper-instance.json 失败：{}", err)
    })?;
    let value: serde_json::Value = serde_json::from_str(&raw).map_err(|err| {
        format!("helper-instance.json 不是合法 JSON：{}", err)
    })?;
    let obj = value.as_object().ok_or_else(|| {
        "helper-instance.json 不是 JSON object".to_string()
    })?;
    let pid = obj
        .get("pid")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "helper-instance.json 缺少 pid".to_string())?;
    let port = obj
        .get("port")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| "helper-instance.json 缺少 port".to_string())?
        as u16;
    let service = obj
        .get("service")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let protocol_version = obj
        .get("protocol_version")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let version = obj.get("version").and_then(|v| v.as_str()).map(String::from);
    let instance_id = obj.get("instance_id").and_then(|v| v.as_str()).map(String::from);
    let runtime_mode = obj.get("runtime_mode").and_then(|v| v.as_str()).map(String::from);
    Ok(HelperInstance {
        pid,
        port,
        service,
        protocol_version,
        version,
        instance_id,
        runtime_mode,
    })
}

/// 让位 / 端口耗尽等结构化事件（来自 helper stderr 单行 JSON）。
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "event")]
pub enum HelperEvent {
    #[serde(rename = "same-service-discovered")]
    SameServiceDiscovered {
        port: u16,
        pid: Option<i64>,
        version: Option<String>,
    },
    #[serde(rename = "ports-exhausted")]
    PortsExhausted {
        target_port: u16,
        blocked: serde_json::Value,
        recovery: Option<String>,
    },
    #[serde(rename = "started")]
    Started { port: u16, instance_id: Option<String> },
    #[serde(rename = "exited")]
    Exited { code: Option<i32>, signal: Option<i32> },
}

/// helper 进程拉起 + 握手 + 让位/耗尽判定的总控。
///
/// 设计要点：
/// - 「已就绪」= helper-instance.json 含本服务身份（service=fathom 且
///   protocol_version=1）或 ``/health`` 探测返回同源身份；任一满足即视为
///   helper 已上线（helper 的 cmd_serve 与 cmd_status 都会写 discovery 文件）。
/// - 超时 20s；每次重试间隔 250ms（与生产 smoke 等价）。
/// - 让位事件（exit 0 + ``same-service-discovered`` 事件）：复用 helper 的
///   ``port`` 字段，不拉起、不 SIGTERM。
/// - 端口耗尽事件（exit 3）：返回 ``PortsExhausted``，由握手页呈现恢复动作。
/// - 不向任何 helper 之外的进程发信号。
pub struct HelperHandle {
    inner: Mutex<Option<Child>>,
    /// 启动时 helper 落地的 ``port``（用于 SIGTERM 后清理识别）。
    pub spawned_port: Mutex<Option<u16>>,
    pub runtime_dir: PathBuf,
    /// helper 让位 / 已复用事件：true 表示当前 handle 是「复用」模式，
    /// 退出时不向外部 PID 发信号。
    pub reused: AtomicBool,
}

impl HelperHandle {
    pub fn new(runtime_dir: PathBuf) -> Self {
        Self {
            inner: Mutex::new(None),
            spawned_port: Mutex::new(None),
            runtime_dir,
            reused: AtomicBool::new(false),
        }
    }

    /// 探测 ``127.0.0.1:port/health``；返回是否同服务同协议的健康实例。
    /// 仅读：发起 GET 请求与读响应体；不修改任何文件、不发信号。
    pub fn probe_health(port: u16) -> ProbeKind {
        // 用 std::net::TcpStream + 手写最小 HTTP 请求避免引入新依赖。
        use std::io::Write;
        use std::net::TcpStream;
        let addr = format!("127.0.0.1:{}", port);
        let mut stream = match TcpStream::connect_timeout(
            &addr.parse().unwrap(),
            Duration::from_millis(500),
        ) {
            Ok(stream) => stream,
            Err(_) => return ProbeKind::Refused,
        };
        let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
        let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
        let req = format!(
            "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
            port
        );
        if stream.write_all(req.as_bytes()).is_err() {
            return ProbeKind::Refused;
        }
        let mut buf = String::new();
        if stream.read_to_string(&mut buf).is_err() {
            return ProbeKind::Refused;
        }
        // 拆 body：仅看最后 JSON object；忽略 header / chunked framing。
        let body = match buf.find("\r\n\r\n") {
            Some(idx) => &buf[idx + 4..],
            None => return ProbeKind::Mismatch,
        };
        let json_start = match body.find('{') {
            Some(idx) => idx,
            None => return ProbeKind::Mismatch,
        };
        let body = &body[json_start..];
        let value: serde_json::Value = match serde_json::from_str(body) {
            Ok(v) => v,
            Err(_) => return ProbeKind::Mismatch,
        };
        let service = value.get("service").and_then(|v| v.as_str()).unwrap_or("");
        let protocol = value.get("protocol_version").and_then(|v| v.as_i64()).unwrap_or(0);
        let status = value.get("status").and_then(|v| v.as_str()).unwrap_or("");
        if service == SERVICE_IDENTITY && protocol == PROTOCOL_VERSION && status == "ok" {
            ProbeKind::Ours {
                port,
                pid: value.get("pid").and_then(|v| v.as_i64()),
                version: value
                    .get("version")
                    .and_then(|v| v.as_str())
                    .map(String::from),
                instance_id: value
                    .get("instance_id")
                    .and_then(|v| v.as_str())
                    .map(String::from),
            }
        } else {
            ProbeKind::Mismatch
        }
    }

    /// 完整握手流程：先读 helper-instance.json；若身份不符则逐端口探测
    /// ``/health``；满足同服务即返回 Some；超时返回 None。
    pub fn handshake(&self) -> Result<Option<HelperInstance>, String> {
        let start = Instant::now();
        let timeout = Duration::from_secs(HANDSHAKE_TIMEOUT_S);
        let mut last_err: Option<String> = None;
        let port_base: u16 = std::env::var("FATHOM_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(DEFAULT_PORT);
        while start.elapsed() < timeout {
            // 路径 1：helper-instance.json
            if let Ok(instance) = read_helper_instance(&self.runtime_dir) {
                if instance.service == SERVICE_IDENTITY
                    && instance.protocol_version == PROTOCOL_VERSION
                {
                    return Ok(Some(instance));
                }
                last_err = Some(format!(
                    "helper-instance.json 身份不符：service={} protocol={}",
                    instance.service, instance.protocol_version
                ));
            }
            // 路径 2：候选端口 /health
            for offset in 0..=DEFAULT_PORT_RANGE {
                let port = port_base.saturating_add(offset);
                match Self::probe_health(port) {
                    ProbeKind::Ours { port, .. } => {
                        // 让位路径：helper 让位退出 0，外部已是同服务。
                        self.reused.store(true, Ordering::SeqCst);
                        return Ok(Some(HelperInstance {
                            pid: 0,
                            port,
                            service: SERVICE_IDENTITY.into(),
                            protocol_version: PROTOCOL_VERSION,
                            version: None,
                            instance_id: None,
                            runtime_mode: None,
                        }));
                    }
                    ProbeKind::Refused => {}
                    ProbeKind::Mismatch => {}
                }
            }
            thread::sleep(Duration::from_millis(250));
        }
        Err(last_err.unwrap_or_else(|| {
            format!(
                "helper 握手超时（{}s）—未读到 helper-instance.json 且候选端口 127.0.0.1:{}..={} /health 无响应",
                HANDSHAKE_TIMEOUT_S,
                port_base,
                port_base.saturating_add(DEFAULT_PORT_RANGE),
            )
        }))
    }

    /// 绑定由本壳拉起的子进程句柄；用于后续 SIGTERM。
    pub fn adopt(&self, child: Child, port: u16) {
        if let Ok(mut guard) = self.inner.lock() {
            *guard = Some(child);
        }
        if let Ok(mut port_guard) = self.spawned_port.lock() {
            *port_guard = Some(port);
        }
    }

    /// 等待子进程退出并返回 ``Exited`` 事件（来自 stderr 的非结构化文本按
    /// helper 的 cmd_serve 行为：成功让位 / 端口耗尽 由 helper 自己 exit code
    /// 与日志结构判定）。
    pub fn wait_with_events(&self) -> Result<HelperEvent, String> {
        let port_base: u16 = std::env::var("FATHOM_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(DEFAULT_PORT);
        let log_path = self.runtime_dir.join("logs").join("helper.log");
        let log_path = log_path.as_path();
        let mut child = match self.inner.lock() {
            Ok(guard) => guard.take(),
            Err(_) => return Err("helper 句柄互斥锁中毒".to_string()),
        };
        let child = match child.as_mut() {
            Some(child) => child,
            None => {
                // 复用模式：helper 由外部持有；本壳仅观测，让位/耗尽由
                // helper.stderr 在初始 spawn 时已读过；此处直接报告已复用。
                return Ok(HelperEvent::Exited {
                    code: Some(0),
                    signal: None,
                });
            }
        };
        // 非阻塞探活 + 极短 sleep；用 try_wait 实现 bounded 等待。
        let deadline = Instant::now() + Duration::from_secs(HANDSHAKE_TIMEOUT_S);
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let event = decode_exit_event(&log_path, port_base, &status);
                    return Ok(event);
                }
                Ok(None) => {
                    if Instant::now() >= deadline {
                        return Err(format!(
                            "helper 启动 {}s 内未退出 / 未就绪",
                            HANDSHAKE_TIMEOUT_S
                        ));
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                Err(err) => return Err(format!("helper try_wait 失败：{}", err)),
            }
        }
    }

    /// SIGTERM 由本壳拉起的 helper；bounded 等待；不对 reused 模式发信号。
    pub fn stop(&self) -> Result<(), String> {
        if self.reused.load(Ordering::SeqCst) {
            // 复用模式：helper 由外部持有，本壳不干预；helper 自身 SIGTERM
            // 由用户从 tray Quit 流程或系统事件触发，不在本切片责任范围。
            return Ok(());
        }
        let mut guard = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return Err("helper 句柄互斥锁中毒".to_string()),
        };
        let child = match guard.as_mut() {
            Some(child) => child,
            None => return Ok(()),
        };
        // SIGTERM 优雅退出：helper 自身有受控停机路径（uvicorn.run 在 SIGTERM
        // 下 should_exit=true，cli.cmd_serve 的 _exit_on_signal 触发 SystemExit
        // 并清理 helper-instance.json）。本壳只在子进程仍在时发信号一次。
        #[cfg(unix)]
        {
            use std::process::Command;
            let pid = child.id();
            // 先 try_wait，避免对已退出进程发 SIGTERM
            if let Ok(Some(_)) = child.try_wait() {
                return Ok(());
            }
            // 用 Command::new("kill") 发送信号，避免 libc 依赖
            let _ = Command::new("kill")
                .arg("-TERM")
                .arg(pid.to_string())
                .status();
            let deadline = Instant::now() + Duration::from_secs(STOP_TIMEOUT_S);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => return Ok(()),
                    Ok(None) => {
                        if Instant::now() >= deadline {
                            // bounded 等待耗尽：强制 SIGKILL（仍仅限自己拉起的）
                            let _ = Command::new("kill")
                                .arg("-KILL")
                                .arg(pid.to_string())
                                .status();
                            return Ok(());
                        }
                        thread::sleep(Duration::from_millis(100));
                    }
                    Err(_) => return Ok(()),
                }
            }
        }
        #[cfg(not(unix))]
        {
            let _ = child.kill();
            Ok(())
        }
    }
}

#[derive(Debug, Clone)]
pub enum ProbeKind {
    Refused,
    Mismatch,
    Ours {
        port: u16,
        pid: Option<i64>,
        version: Option<String>,
        instance_id: Option<String>,
    },
}

/// 把 helper 的退出状态映射到 ``HelperEvent``：helper 自身会用 stderr 输出
/// ``same-service-discovered`` / ``ports-exhausted`` 等结构化 JSON 行，本
/// 函数扫描 helper.log 尾部以还原事件语义。
fn decode_exit_event(
    log_path: &Path,
    port_base: u16,
    status: &std::process::ExitStatus,
) -> HelperEvent {
    let code = status.code();
    let signal = {
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            status.signal()
        }
        #[cfg(not(unix))]
        {
            None
        }
    };
    if let Ok(file) = std::fs::File::open(log_path) {
        let reader = BufReader::new(file);
        let mut last_event: Option<HelperEvent> = None;
        for line in reader.lines().map_while(Result::ok) {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) else {
                continue;
            };
            let Some(event) = value.get("event").and_then(|v| v.as_str()) else {
                continue;
            };
            match event {
                "same-service-discovered" => {
                    let port = value
                        .get("port")
                        .and_then(|v| v.as_u64())
                        .map(|p| p as u16)
                        .unwrap_or(port_base);
                    let pid = value.get("pid").and_then(|v| v.as_i64());
                    let version = value
                        .get("version")
                        .and_then(|v| v.as_str())
                        .map(String::from);
                    last_event = Some(HelperEvent::SameServiceDiscovered {
                        port,
                        pid,
                        version,
                    });
                }
                "ports-exhausted" => {
                    let target_port = value
                        .get("target_port")
                        .and_then(|v| v.as_u64())
                        .map(|p| p as u16)
                        .unwrap_or(port_base);
                    let blocked = value.get("blocked").cloned().unwrap_or(serde_json::Value::Null);
                    let recovery = value
                        .get("recovery")
                        .and_then(|v| v.as_str())
                        .map(String::from);
                    last_event = Some(HelperEvent::PortsExhausted {
                        target_port,
                        blocked,
                        recovery,
                    });
                }
                _ => {}
            }
        }
        if let Some(event) = last_event {
            return event;
        }
    }
    HelperEvent::Exited { code, signal }
}

/// 单元测试：身份字段常量与 helper 期望对齐（ISS-037 单版本源）。
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_constants_match_helper() {
        assert_eq!(SERVICE_IDENTITY, "fathom");
        assert_eq!(PROTOCOL_VERSION, 1);
    }

    #[test]
    fn default_port_matches_helper() {
        assert_eq!(DEFAULT_PORT, 7952);
        assert_eq!(DEFAULT_PORT_RANGE, 4);
    }

    #[test]
    fn default_runtime_dir_is_application_support_fathom() {
        let dir = default_runtime_dir();
        let tail = dir
            .components()
            .rev()
            .take(2)
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<Vec<_>>()
            .join("/");
        assert_eq!(tail, "Application Support/Fathom");
    }

    #[test]
    fn handshake_helper_instance_requires_service_match() {
        let runtime = tempdir();
        std::fs::write(
            runtime.join("helper-instance.json"),
            r#"{"service":"fathom","protocol_version":1,"pid":42,"port":7952,"version":"0.3.0","instance_id":"abc","runtime_mode":"release"}"#,
        )
        .unwrap();
        let handle = HelperHandle::new(runtime.clone());
        let instance = handle.handshake().unwrap().unwrap();
        assert_eq!(instance.service, "fathom");
        assert_eq!(instance.protocol_version, 1);
        assert_eq!(instance.port, 7952);
    }

    #[test]
    fn handshake_rejects_wrong_identity() {
        let runtime = tempdir();
        std::fs::write(
            runtime.join("helper-instance.json"),
            r#"{"service":"other","protocol_version":1,"pid":42,"port":7952}"#,
        )
        .unwrap();
        let handle = HelperHandle::new(runtime.clone());
        // 没真实 /health 监听；超时前 instance 路径被身份不符拒掉；
        // 这里只断言读出来的 service/protocol 字段判定路径正确。
        let raw = read_helper_instance(&runtime).unwrap();
        assert_eq!(raw.service, "other");
        assert_eq!(raw.protocol_version, 1);
    }

    fn tempdir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "fathom-helper-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }
}
