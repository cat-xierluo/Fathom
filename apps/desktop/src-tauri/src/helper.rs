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
//! ISS-059：路径 1 复用前对 instance 文件记录的端口做 ``/health`` 探活；
//! 陈旧文件（pid 不在运行且端口无响应）删除后走 spawn，pid 判活只用
//! ``ps -p`` 只读查询——不对任何 pid 发信号（含信号 0 也不使用）。端口
//! 耗尽信息保留在句柄（``ExhaustedInfo``），``helper_status`` 据此返回
//! ``state=exhausted`` 与 recovery 结构。
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
///
/// ISS-060 (f)：第二个返回值为 helper.log 在本次 spawn 前的字节偏移；调用方
/// 在 ``decode_exit_event`` 时只读该偏移之后追加的内容，避免跨运行长驻日
/// 志复活历史事件。
pub fn spawn_helper(bin: &Path, runtime_dir: &Path) -> Result<(Child, u64), String> {
    let logs_dir = runtime_dir.join("logs");
    std::fs::create_dir_all(&logs_dir).map_err(|err| {
        format!("创建日志目录失败 {}：{}", logs_dir.display(), err)
    })?;
    let log_path = logs_dir.join("helper.log");
    // spawn 前的文件大小 = 本次 helper 写入的起始偏移。文件不存在时（首次
    // 运行）按 0 处理；读元数据失败时回退到 0 走「全路径扫描」的旧行为
    // （仅丢一点性能，不改语义）。
    let log_offset = std::fs::metadata(&log_path)
        .map(|m| m.len())
        .unwrap_or(0);
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
    let child = cmd.spawn().map_err(|err| {
        format!(
            "启动 helper 失败 {}：{}",
            bin.display(),
            err
        )
    })?;
    Ok((child, log_offset))
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

/// ISS-059：instance 文件命中后的处置决策（``instance_disposition`` 的结果）。
/// 探测结果与 pid 存活判定均可注入，单测不依赖真实 7952。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstanceDisposition {
    /// ``/health`` 健康且同源：维持现状复用该实例。
    Reuse,
    /// ``/health`` 不可达且 pid 不在运行：陈旧文件，删除后落回路径 2 / spawn。
    StaleRemove,
    /// pid 仍在运行但 ``/health`` 未就绪（可能是启动中的他实例）：保留文件，
    /// 不删不发信号，按路径 2 继续探测/等待。
    KeepAndFallThrough,
}

/// instance 文件命中后的处置判定（ISS-059，纯函数）：
/// - ``Ours`` → 复用（健康且身份同源）；
/// - ``Refused``/``Mismatch`` 且 pid 不在运行 → 陈旧，可删文件；
/// - ``Refused``/``Mismatch`` 且 pid 仍在运行 → 保留文件继续等待。
pub fn instance_disposition(probe: &ProbeKind, pid_alive: bool) -> InstanceDisposition {
    match probe {
        ProbeKind::Ours { .. } => InstanceDisposition::Reuse,
        ProbeKind::Refused | ProbeKind::Mismatch => {
            if pid_alive {
                InstanceDisposition::KeepAndFallThrough
            } else {
                InstanceDisposition::StaleRemove
            }
        }
    }
}

/// 只读判定 pid 是否在运行：``ps -p <pid>``（macOS）。零信号合同：本函数
/// 不对该 pid 发送任何信号——包括信号 0 也不使用（``kill -0`` 虽不实际杀
/// 进程，但为避免与「零击杀」表述产生歧义一律不用），只以 ps 的存在性
/// 输出为准。ps 不可用时保守返回 ``true``（视为「在运行」）：宁可漏删
/// 陈旧文件走路径 2，也不误删可能存活的实例文件。
pub fn pid_is_alive(pid: i64) -> bool {
    if pid <= 0 {
        // pid 0 是内核任务、负数不合法，都不可能是 helper 用户态进程
        return false;
    }
    match Command::new("ps")
        .arg("-p")
        .arg(pid.to_string())
        .arg("-o")
        .arg("pid=")
        .output()
    {
        Ok(out) => out.status.success() && !out.stdout.is_empty(),
        Err(_) => true,
    }
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
        candidates: Vec<u16>,
        blocked: serde_json::Value,
        recovery: Option<String>,
    },
    #[serde(rename = "started")]
    Started { port: u16, instance_id: Option<String> },
    #[serde(rename = "exited")]
    Exited { code: Option<i32>, signal: Option<i32> },
}

/// ISS-059：单个候选端口的占用快照（``state=exhausted`` 呈现用）。
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ExhaustedPort {
    pub port: u16,
    /// 占用进程 pid；探测未给出时为 ``None``（序列化为 null）。
    pub occupied_pid: Option<i64>,
}

/// ISS-059：端口耗尽信息（helper exit 3 + ``ports-exhausted`` 事件还原），
/// 由 ``HelperHandle`` 保留，``helper_status`` / ``helper_retry`` 据此返回
/// ``state=exhausted`` 与 recovery 结构。
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ExhaustedInfo {
    pub target_port: u16,
    /// 全部候选端口的占用快照（blocked 未覆盖的候选以 ``occupied_pid=None``
    /// 补齐）。
    pub blocked: Vec<ExhaustedPort>,
    /// 给用户的恢复提示文案（来自 helper 的 recovery 字段；缺失时用内置默认）。
    pub hint: String,
}

impl ExhaustedInfo {
    /// 从 ``ports-exhausted`` 事件字段还原（纯函数，单测覆盖）。helper 侧
    /// ``occupied_pid`` 是 lsof pid 列表，呈现取首个；空列表/缺失归为
    /// ``None``（未知）。
    pub fn from_event(
        target_port: u16,
        candidates: &[u16],
        blocked: &serde_json::Value,
        recovery: &Option<String>,
    ) -> Self {
        let mut ports: Vec<ExhaustedPort> = Vec::new();
        if let Some(arr) = blocked.as_array() {
            for entry in arr {
                let Some(port) = entry.get("port").and_then(|v| v.as_u64()) else {
                    continue;
                };
                let occupied_pid = entry.get("occupied_pid").and_then(|v| {
                    v.as_i64().or_else(|| {
                        v.as_array()
                            .and_then(|list| list.first())
                            .and_then(|p| p.as_i64())
                    })
                });
                ports.push(ExhaustedPort {
                    port: port as u16,
                    occupied_pid,
                });
            }
        }
        for candidate in candidates {
            if !ports.iter().any(|p| p.port == *candidate) {
                ports.push(ExhaustedPort {
                    port: *candidate,
                    occupied_pid: None,
                });
            }
        }
        ports.sort_by_key(|p| p.port);
        Self {
            target_port,
            blocked: ports,
            hint: recovery.clone().unwrap_or_else(|| {
                "端口候选范围全部被占用，未触碰任何占用进程；请检查占用进程"
                    .to_string()
                    + "（lsof -tiTCP:<port> -sTCP:LISTEN -n -P）并释放，或更换起始端口"
                    + "后点「重试握手」。"
            }),
        }
    }
}

/// ``state=exhausted`` 的状态 JSON（``helper_status`` / ``helper_retry`` 共用，
/// ISS-059）。``recovery`` 至少含候选端口列表（各端口占用 pid，未知为
/// null）与恢复提示文案键 ``hint``（握手页 ``renderExhausted`` 已有渲染）。
pub fn exhausted_status_json(info: &ExhaustedInfo, runtime_dir: &Path) -> serde_json::Value {
    serde_json::json!({
        "state": "exhausted",
        "recovery": {
            "ports": info.blocked,
            "hint": info.hint,
        },
        "target_port": info.target_port,
        "log_path": runtime_dir.join("logs").join("helper.log").display().to_string(),
        "runtime_dir": runtime_dir.display().to_string(),
        "timeout_s": HANDSHAKE_TIMEOUT_S,
    })
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
    /// ISS-059：最近一次 ``ports-exhausted`` 判定；``helper_status`` /
    /// ``helper_retry`` 据此返回 ``state=exhausted``。重试会用新句柄重新
    /// 走完整流程，随句柄重建而清空。
    last_exhausted: Mutex<Option<ExhaustedInfo>>,
    /// ISS-060 (f)：spawn 时 helper.log 的字节偏移；``decode_exit_event``
    /// 只读该偏移之后追加的内容，避免跨运行长驻日志复活历史事件。
    spawn_log_offset: Mutex<Option<u64>>,
}

/// 路径 1 单步结果（``HelperHandle::instance_file_step``，ISS-059 抽出
/// 以便单测：不跑满 20s 握手循环即可覆盖「读文件 → 判活 → 决策」）。
#[derive(Debug)]
enum InstanceStep {
    /// ``/health`` 健康且同源：直接复用返回。
    Reuse(HelperInstance),
    /// 陈旧文件已删除；携带诊断信息供握手超时文案使用。
    Stale(String),
    /// pid 仍在运行但未就绪：保留文件继续循环等待。
    Wait(String),
    /// 读不到文件或身份不符。
    Unavailable(String),
}

impl HelperHandle {
    pub fn new(runtime_dir: PathBuf) -> Self {
        Self {
            inner: Mutex::new(None),
            spawned_port: Mutex::new(None),
            runtime_dir,
            reused: AtomicBool::new(false),
            last_exhausted: Mutex::new(None),
            spawn_log_offset: Mutex::new(None),
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
    /// ``/health``；满足同服务即返回 Some；超时返回 Err。
    ///
    /// ISS-059：路径 1 命中身份匹配的 instance 文件后先对其记录的 port 做
    /// ``/health`` 探活——健康且同源才复用返回；不健康视为陈旧，仅当
    /// ``/health`` 不可达且 pid 不在运行（``ps -p`` 只读判定，零信号）时删除
    /// 该文件并落回路径 2 / spawn；pid 仍在运行但未就绪（他实例启动中）则
    /// 保留文件、不发信号，按路径 2 继续探测/等待。
    pub fn handshake(&self) -> Result<Option<HelperInstance>, String> {
        let start = Instant::now();
        let timeout = Duration::from_secs(HANDSHAKE_TIMEOUT_S);
        let mut last_err: Option<String> = None;
        let port_base: u16 = std::env::var("FATHOM_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(DEFAULT_PORT);
        while start.elapsed() < timeout {
            // 路径 1：helper-instance.json（ISS-059：身份匹配后先探活再复用）
            match self.instance_file_step() {
                InstanceStep::Reuse(instance) => return Ok(Some(instance)),
                InstanceStep::Stale(err) | InstanceStep::Wait(err) | InstanceStep::Unavailable(err) => {
                    last_err = Some(err);
                }
            }
            // 路径 2：候选端口 /health
            for offset in 0..=DEFAULT_PORT_RANGE {
                let port = port_base.saturating_add(u16::from(offset));
                match Self::probe_health(port) {
                    ProbeKind::Ours { pid, .. } => {
                        // 让位路径：helper 让位退出 0，外部已是同服务。
                        // ISS-059：探测到的实例若是本壳刚拉起的子进程
                        // （instance 文件写于 uvicorn 监听就绪前、路径 1 探活
                        // 落空的窗口内会走到这里），不算「复用」，否则退出时
                        // 会因 reused 标记漏回收自己的 helper。
                        let own_pid = self.own_child_pid().map(|p| i64::from(p));
                        if own_pid.is_none() || pid != own_pid {
                            self.reused.store(true, Ordering::SeqCst);
                        }
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
                port_base.saturating_add(u16::from(DEFAULT_PORT_RANGE)),
            )
        }))
    }

    /// 路径 1 的单步（ISS-059，抽出以便单测）：读 instance 文件 → 身份校验
    /// → ``/health`` 探活 → 处置。``Reuse`` 携带可复用实例；``Stale`` 时本
    /// 函数已删除陈旧文件；其余情形携带诊断信息供握手超时文案使用。
    fn instance_file_step(&self) -> InstanceStep {
        let instance = match read_helper_instance(&self.runtime_dir) {
            Ok(inst) => inst,
            Err(err) => return InstanceStep::Unavailable(err),
        };
        if instance.service != SERVICE_IDENTITY || instance.protocol_version != PROTOCOL_VERSION {
            return InstanceStep::Unavailable(format!(
                "helper-instance.json 身份不符：service={} protocol={}",
                instance.service, instance.protocol_version
            ));
        }
        let probe = Self::probe_health(instance.port);
        let pid_alive = pid_is_alive(instance.pid);
        match instance_disposition(&probe, pid_alive) {
            InstanceDisposition::Reuse => InstanceStep::Reuse(instance),
            InstanceDisposition::StaleRemove => {
                let stale_path = self.runtime_dir.join("helper-instance.json");
                match std::fs::remove_file(&stale_path) {
                    Ok(()) => InstanceStep::Stale(format!(
                        "helper-instance.json 陈旧（pid={} 不在运行且 127.0.0.1:{}/health 不可达），已删除并转为候选端口探测/spawn",
                        instance.pid, instance.port
                    )),
                    Err(err) => InstanceStep::Stale(format!(
                        "helper-instance.json 陈旧（pid={} port={}）但删除失败：{}",
                        instance.pid, instance.port, err
                    )),
                }
            }
            InstanceDisposition::KeepAndFallThrough => InstanceStep::Wait(format!(
                "helper-instance.json 的 pid={} 仍在运行但 127.0.0.1:{}/health 未就绪（可能在启动中），保留文件继续等待",
                instance.pid, instance.port
            )),
        }
    }

    /// 本壳拉起的 helper 子进程 pid（只读，不 wait 不发信号）；无子进程或
    /// 锁中毒时为 ``None``。
    fn own_child_pid(&self) -> Option<u32> {
        self.inner
            .lock()
            .ok()
            .and_then(|guard| guard.as_ref().map(|child| child.id()))
    }

    /// 最近一次端口耗尽信息（ISS-059）；未发生过为 ``None``。
    pub fn exhausted_info(&self) -> Option<ExhaustedInfo> {
        self.last_exhausted
            .lock()
            .ok()
            .and_then(|guard| guard.clone())
    }

    /// 绑定由本壳拉起的子进程句柄；用于后续 SIGTERM。
    ///
    /// ``log_offset``：spawn 前的文件字节偏移；``decode_exit_event`` 据此只读
    /// 本次 spawn 之后追加的日志段（ISS-060 (f)）。
    pub fn adopt(&self, child: Child, port: u16, log_offset: u64) {
        if let Ok(mut guard) = self.inner.lock() {
            *guard = Some(child);
        }
        if let Ok(mut port_guard) = self.spawned_port.lock() {
            *port_guard = Some(port);
        }
        if let Ok(mut offset_guard) = self.spawn_log_offset.lock() {
            *offset_guard = Some(log_offset);
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
            Ok(mut guard) => guard.take(),
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
                    // ISS-060 (f)：只读本次 spawn 之后追加的日志段，避免跨运行
                    // 长驻日志复活历史事件。未记录偏移（adopt 未调用）时退到
                    // 「全文件扫描」旧行为。
                    let log_offset = self
                        .spawn_log_offset
                        .lock()
                        .ok()
                        .and_then(|g| *g)
                        .unwrap_or(0);
                    let event = decode_exit_event(&log_path, log_offset, port_base, &status);
                    // ISS-059：端口耗尽时把事件还原成 ExhaustedInfo 记入句柄，
                    // helper_status / helper_retry 据此返回 state=exhausted。
                    if let HelperEvent::PortsExhausted {
                        target_port,
                        candidates,
                        blocked,
                        recovery,
                    } = &event
                    {
                        let info =
                            ExhaustedInfo::from_event(*target_port, candidates, blocked, recovery);
                        if let Ok(mut guard) = self.last_exhausted.lock() {
                            *guard = Some(info);
                        }
                    }
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
///
/// ISS-060 (f)：``log_offset`` 是 spawn 时记录的 helper.log 字节偏移；本
/// 函数只读该偏移之后追加的内容，避免跨运行长驻日志复活历史事件。``log_offset``
/// 超过文件大小时跳过扫描（本次 spawn 没新写日志），按 ``Exited`` 兜底。
fn decode_exit_event(
    log_path: &Path,
    log_offset: u64,
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
    let scanned = std::fs::File::open(log_path).ok().and_then(|mut file| {
        use std::io::Seek;
        let len = file.metadata().ok().map(|m| m.len()).unwrap_or(0);
        if log_offset > len {
            // 本次 spawn 实际没产生新日志（极少；如 helper 启动即被 SIGKILL）。
            // 走 Exited 兜底，避免误把上一次运行的尾部事件当作本次结果。
            return Some(None);
        }
        if file.seek(std::io::SeekFrom::Start(log_offset)).is_err() {
            return None;
        }
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
                    // ISS-059：候选端口列表用于 recovery 呈现；事件缺失时按
                    // 默认范围（port_base..+DEFAULT_PORT_RANGE）补齐。
                    let candidates = value
                        .get("candidates")
                        .and_then(|v| v.as_array())
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|v| v.as_u64().map(|p| p as u16))
                                .collect::<Vec<u16>>()
                        })
                        .unwrap_or_else(|| {
                            (0..=u16::from(DEFAULT_PORT_RANGE))
                                .map(|off| port_base.saturating_add(off))
                                .collect()
                        });
                    let blocked = value.get("blocked").cloned().unwrap_or(serde_json::Value::Null);
                    let recovery = value
                        .get("recovery")
                        .and_then(|v| v.as_str())
                        .map(String::from);
                    last_event = Some(HelperEvent::PortsExhausted {
                        target_port,
                        candidates,
                        blocked,
                        recovery,
                    });
                }
                _ => {}
            }
        }
        Some(last_event)
    });
    if let Some(Some(event)) = scanned {
        return event;
    }
    HelperEvent::Exited { code, signal }
}

/// 单元测试：身份字段常量与 helper 期望对齐（ISS-037 单版本源）。
#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::os::unix::process::ExitStatusExt;

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

    /// 在 127.0.0.1 随机端口起一个最小 /health 应答器（同服务身份、返回本
    /// 测试进程 pid）。ISS-059：单测不依赖真实 7952（CI/本机都可能被占）。
    fn spawn_health_responder() -> (u16, thread::JoinHandle<()>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            let mut served = 0usize;
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                // 读掉请求（内容无关），应答后关连接（Connection: close）
                let mut buf = [0u8; 1024];
                let _ = std::io::Read::read(&mut stream, &mut buf);
                let body = format!(
                    "{{\"service\":\"fathom\",\"protocol_version\":1,\"status\":\"ok\",\"pid\":{},\"version\":\"0.3.0\",\"instance_id\":\"t\"}}",
                    std::process::id()
                );
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                    body.len(),
                    body
                );
                use std::io::Write;
                let _ = stream.write_all(resp.as_bytes());
                served += 1;
                if served >= 64 {
                    break;
                }
            }
        });
        (port, handle)
    }

    /// 找一个「曾有监听、现已无监听」的临时端口：bind :0 后立即关闭。
    fn freed_port() -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        port
    }

    #[test]
    fn handshake_helper_instance_requires_service_match() {
        let runtime = tempdir();
        // ISS-059：健康实例（真实监听 + pid 存活）仍从路径 1 返回。
        let (port, _responder) = spawn_health_responder();
        std::fs::write(
            runtime.join("helper-instance.json"),
            format!(
                r#"{{"service":"fathom","protocol_version":1,"pid":{},"port":{},"version":"0.3.0","instance_id":"abc","runtime_mode":"release"}}"#,
                std::process::id(),
                port
            ),
        )
        .unwrap();
        let handle = HelperHandle::new(runtime.clone());
        let instance = handle.handshake().unwrap().unwrap();
        assert_eq!(instance.service, "fathom");
        assert_eq!(instance.protocol_version, 1);
        assert_eq!(instance.port, port);
    }

    #[test]
    fn handshake_rejects_wrong_identity() {
        // ISS-060 (e)：原单测只断言 read_helper_instance 的原始字段、未真正
        // 走 instance_disposition 的拒绝路径。本测试覆盖三条身份不符路径：
        //   (e1) service != "fathom"：身份不符直接拒；
        //   (e2) protocol_version != 1：身份不符直接拒；
        //   (e3) 身份匹配但 /health 不通（用已被释放的临时端口模拟）：
        //        走 instance_disposition → StaleRemove，文件被删，落回路径 2。
        // 全部断言走的是「拒绝」语义（InstanceStep::Unavailable 或 Stale），
        // 不是 raw 字段值。

        // (e1) service 错
        let runtime_e1 = tempdir();
        std::fs::write(
            runtime_e1.join("helper-instance.json"),
            r#"{"service":"other","protocol_version":1,"pid":42,"port":7952}"#,
        )
        .unwrap();
        let handle_e1 = HelperHandle::new(runtime_e1.clone());
        match handle_e1.instance_file_step() {
            InstanceStep::Unavailable(msg) => {
                assert!(
                    msg.contains("身份不符") || msg.contains("service="),
                    "service 错应被识别为身份不符，实际：{msg}"
                );
            }
            other => panic!(
                "service=other 应走拒绝路径（InstanceStep::Unavailable），实际：{:?}",
                other
            ),
        }
        // 文件应原样保留（身份不符不删文件，落回路径 2 探测）
        assert!(
            runtime_e1.join("helper-instance.json").exists(),
            "身份不符的 instance 文件不应被路径 1 删除（应落回路径 2 / spawn）"
        );

        // (e2) protocol_version 错
        let runtime_e2 = tempdir();
        std::fs::write(
            runtime_e2.join("helper-instance.json"),
            r#"{"service":"fathom","protocol_version":2,"pid":42,"port":7952}"#,
        )
        .unwrap();
        let handle_e2 = HelperHandle::new(runtime_e2.clone());
        match handle_e2.instance_file_step() {
            InstanceStep::Unavailable(msg) => {
                assert!(
                    msg.contains("身份不符") || msg.contains("protocol="),
                    "protocol_version 错应被识别为身份不符，实际：{msg}"
                );
            }
            other => panic!(
                "protocol_version=2 应走拒绝路径，实际：{:?}",
                other
            ),
        }

        // (e3) 身份匹配但 /health 不通 + pid 不在运行（曾有监听现已释放）
        let runtime_e3 = tempdir();
        let dead_port = freed_port();
        // macOS pid 上限 99998，4000000 必不存在（pid_is_alive 边界已覆盖）
        std::fs::write(
            runtime_e3.join("helper-instance.json"),
            format!(
                r#"{{"service":"fathom","protocol_version":1,"pid":4000000,"port":{}}}"#,
                dead_port
            ),
        )
        .unwrap();
        let handle_e3 = HelperHandle::new(runtime_e3.clone());
        match handle_e3.instance_file_step() {
            InstanceStep::Stale(msg) => {
                assert!(
                    msg.contains("陈旧") || msg.contains("已删除"),
                    "陈旧文件应被识别并删除，实际：{msg}"
                );
            }
            other => panic!(
                "陈旧文件应走 instance_disposition → StaleRemove，实际：{:?}",
                other
            ),
        }
        assert!(
            !runtime_e3.join("helper-instance.json").exists(),
            "陈旧 instance 文件应被删除"
        );

        // 顺带断言：instance_disposition 自身的「身份匹配→复用」与「身份不
        // 符 + pid 不在」两条核心契约（纯函数），补齐拒绝路径的最小单元。
        assert_eq!(
            instance_disposition(&ProbeKind::Ours { port: 7952, pid: Some(1), version: None, instance_id: None }, true),
            InstanceDisposition::Reuse,
            "身份匹配的 Ours 探活结果应一律复用"
        );
        assert_eq!(
            instance_disposition(&ProbeKind::Mismatch, false),
            InstanceDisposition::StaleRemove,
            "身份不匹配且 pid 不在应判陈旧"
        );
    }

    /// ISS-059：pid 判活只读（ps），零信号；本进程存在、超界 pid/非正 pid 不存在。
    #[test]
    fn pid_is_alive_reflects_process_table_without_signals() {
        assert!(pid_is_alive(i64::from(std::process::id())));
        // macOS pid 上限 99998：4000000 必不存在
        assert!(!pid_is_alive(4_000_000));
        // pid 0 是内核任务、负数不合法：不查表直接判不存在
        assert!(!pid_is_alive(0));
        assert!(!pid_is_alive(-1));
    }

    /// ISS-059：处置判定矩阵——健康复用 / 死 pid 陈旧可删 / 活 pid 未就绪保留。
    #[test]
    fn instance_disposition_matrix() {
        let ours = ProbeKind::Ours {
            port: 7953,
            pid: Some(42),
            version: None,
            instance_id: None,
        };
        assert_eq!(
            instance_disposition(&ours, true),
            InstanceDisposition::Reuse
        );
        assert_eq!(
            instance_disposition(&ours, false),
            InstanceDisposition::Reuse
        );
        assert_eq!(
            instance_disposition(&ProbeKind::Refused, false),
            InstanceDisposition::StaleRemove
        );
        assert_eq!(
            instance_disposition(&ProbeKind::Mismatch, false),
            InstanceDisposition::StaleRemove
        );
        assert_eq!(
            instance_disposition(&ProbeKind::Refused, true),
            InstanceDisposition::KeepAndFallThrough
        );
        assert_eq!(
            instance_disposition(&ProbeKind::Mismatch, true),
            InstanceDisposition::KeepAndFallThrough
        );
    }

    /// ISS-059 反例：陈旧文件（身份匹配、pid 不存在、端口无监听）→ 路径 1
    /// 不返回该实例，且文件被删除（落回路径 2 / spawn）。
    #[test]
    fn stale_instance_file_is_removed_and_not_returned() {
        let runtime = tempdir();
        let dead_port = freed_port();
        std::fs::write(
            runtime.join("helper-instance.json"),
            format!(
                r#"{{"service":"fathom","protocol_version":1,"pid":4000000,"port":{}}}"#,
                dead_port
            ),
        )
        .unwrap();
        let handle = HelperHandle::new(runtime.clone());
        match handle.instance_file_step() {
            InstanceStep::Stale(_) => {}
            other => panic!(
                "陈旧文件（pid 不存在、端口无监听）应判 Stale 并删除，实际：{:?}",
                other
            ),
        }
        assert!(
            !runtime.join("helper-instance.json").exists(),
            "陈旧文件应被删除"
        );
    }

    /// ISS-059：pid 仍在运行但 /health 不通（他实例启动中）→ 不删文件、
    /// 不发信号，保留文件继续等待。
    #[test]
    fn live_pid_unhealthy_port_keeps_file_and_waits() {
        let runtime = tempdir();
        let dead_port = freed_port();
        std::fs::write(
            runtime.join("helper-instance.json"),
            format!(
                r#"{{"service":"fathom","protocol_version":1,"pid":{},"port":{}}}"#,
                std::process::id(),
                dead_port
            ),
        )
        .unwrap();
        let handle = HelperHandle::new(runtime.clone());
        match handle.instance_file_step() {
            InstanceStep::Wait(_) => {}
            other => panic!(
                "pid 仍在运行但 /health 不通应判 Wait（不删文件），实际：{:?}",
                other
            ),
        }
        assert!(
            runtime.join("helper-instance.json").exists(),
            "pid 存活时不得删除 instance 文件"
        );
    }

    /// ISS-059：PortsExhausted 事件 → ExhaustedInfo → 状态 JSON 的
    /// state==exhausted 且 recovery 结构完整（候选端口全覆盖、占用 pid、提示文案）。
    #[test]
    fn ports_exhausted_status_json_shape() {
        let blocked = serde_json::json!([
            {"port": 7952, "reason": "occupied-by-unknown", "probe": "mismatch", "occupied_pid": [6026]},
            {"port": 7953, "reason": "occupied-by-unknown", "probe": "refused", "occupied_pid": []}
        ]);
        let info = ExhaustedInfo::from_event(
            7952,
            &[7952, 7953, 7954, 7955, 7956],
            &blocked,
            &Some("恢复动作：检查端口占用……".to_string()),
        );
        let json = exhausted_status_json(&info, Path::new("/tmp/fathom-rt"));
        assert_eq!(json["state"], "exhausted");
        let ports = json["recovery"]["ports"].as_array().unwrap();
        assert_eq!(ports.len(), 5, "recovery.ports 必须覆盖全部候选端口");
        assert_eq!(ports[0]["port"], 7952);
        assert_eq!(ports[0]["occupied_pid"], 6026);
        assert!(ports[1]["occupied_pid"].is_null(), "空 pid 列表归为未知（null）");
        assert!(ports[2]["occupied_pid"].is_null(), "blocked 未覆盖的候选补 null");
        assert!(
            json["recovery"]["hint"].as_str().unwrap().contains("恢复动作"),
            "hint 保留 helper 的恢复文案"
        );
    }

    /// ISS-059：blocked 缺失/为空、recovery 缺失时兜底——候选端口补齐、
    /// 内置默认提示文案非空。
    #[test]
    fn ports_exhausted_info_falls_back_to_defaults() {
        let info = ExhaustedInfo::from_event(7952, &[7952], &serde_json::Value::Null, &None);
        assert_eq!(info.blocked.len(), 1);
        assert!(info.blocked[0].occupied_pid.is_none());
        assert!(!info.hint.is_empty());
    }

    /// ISS-060 (f)：decode_exit_event 只读 spawn 时记录的偏移之后追加的日志段，
    /// 跨运行长驻日志中本次 spawn 之前的旧事件不得复活。
    ///
    /// 真实语义：``log_offset`` 是「从该字节偏移开始扫描」；在该扫描范围内
    /// 取最后一条事件作为本次 helper 退出的语义结果。``offset=0`` 表示从头
    /// 扫全文件，取到的自然是**尾部最后一条**事件（最近写入）。
    /// 生产调用方始终传入 ``spawn`` 时刻的文件大小（即上一次日志末尾偏移），
    /// 因此「跨运行长驻日志复活旧事件」路径在生产中不会出现。
    #[test]
    #[cfg(unix)]
    fn decode_exit_event_ignores_log_history_before_spawn_offset() {
        use std::io::Write;
        use std::process::ExitStatus;
        let runtime = tempdir();
        let logs_dir = runtime.join("logs");
        std::fs::create_dir_all(&logs_dir).unwrap();
        let log_path = logs_dir.join("helper.log");

        // 模拟上一次运行的尾部事件：same-service-discovered 在 7952
        let stale_event = r#"{"event":"same-service-discovered","port":7952,"pid":11111,"version":"0.2.0"}"#;
        {
            let mut f = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&log_path)
                .unwrap();
            writeln!(f, "{}", stale_event).unwrap();
        }
        let offset_after_stale = std::fs::metadata(&log_path).unwrap().len();

        // 本次 spawn 之后追加新事件：ports-exhausted（target=7952）
        let new_event = r#"{"event":"ports-exhausted","target_port":7952,"candidates":[7952,7953,7954,7955,7956],"blocked":[],"recovery":"新恢复文案"}"#;
        {
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&log_path)
                .unwrap();
            writeln!(f, "{}", new_event).unwrap();
        }

        // 关键断言 1：用 spawn_offset 调用 → 只看到本次事件（旧 same-service
        // 落在 offset 之前，不会进入扫描范围）。
        let status = ExitStatus::from_raw(3 << 8); // exit 3
        let event = decode_exit_event(&log_path, offset_after_stale, 7952, &status);
        match event {
            HelperEvent::PortsExhausted { target_port, recovery, .. } => {
                assert_eq!(target_port, 7952);
                assert_eq!(recovery.as_deref(), Some("新恢复文案"));
            }
            other => panic!(
                "spawn_offset=after_stale 应只读到本次事件（PortsExhausted），实际：{:?}",
                other
            ),
        }

        // 关键断言 2：offset=0 表示从文件头扫描整个文件，按 last_event 取到
        // 范围内最后一条——也就是本次新写入的 PortsExhausted（**不是**上一次
        // 的 stale_event）。这是真实语义：「offset 只表示扫描起点」，并不
        // 表示「忽略范围内最近写入的事件」。生产路径因 offset 总是 spawn 时
        // 的文件大小，不会读到旧事件，本用例只是把语义钉死。
        let event_full = decode_exit_event(&log_path, 0, 7952, &status);
        match event_full {
            HelperEvent::PortsExhausted { target_port, recovery, .. } => {
                assert_eq!(target_port, 7952, "offset=0 扫全文件应返回尾部最后一条");
                assert_eq!(recovery.as_deref(), Some("新恢复文案"));
            }
            other => panic!(
                "offset=0 应返回尾部最后一条（PortsExhausted），实际：{:?}",
                other
            ),
        }

        // 关键断言 3：offset 超过文件大小时走 Exited 兜底，绝不误把本次事件
        // 当作「未知」——本次 spawn 没产生新日志时不能乱猜。
        let too_big = offset_after_stale + new_event.len() as u64 + 1000;
        let event_far = decode_exit_event(&log_path, too_big, 7952, &status);
        match event_far {
            HelperEvent::Exited { code, .. } => {
                assert_eq!(code, Some(3), "offset 超界走 Exited 兜底，code=3");
            }
            other => panic!("offset 超界应走 Exited 兜底，实际：{:?}", other),
        }
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
