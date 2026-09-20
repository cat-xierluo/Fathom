//! ISS-010A：登录项与后台计划的只读状态桥 + dry-run；
//! ISS-010B：发行态后台注册命令模块（用户同意流 + launchd 写路径）。
//!
//! 硬边界（ISS-010B 起修订，原「零写入」升级为「写路径仅本模块」）：
//! - 真实写路径**仅存在于本模块的注册命令**（`autostart_register` /
//!   `autostart_unregister` 及其内部 core）：写 `~/Library/LaunchAgents`
//!   下的 plist、调用 `launchctl bootout/bootstrap`。壳内其它代码（lib.rs、
//!   helper.rs 等）不得出现任何 launchctl 写动词——这是
//!   `tests/test_launchd_autostart.py` grep 守护钉住的不变量。
//! - 注册/注销命令必须拿到 `confirmed == true` 才动手；解释与确认 UI 在
//!   设置页完成（展示 plist 摘要与命令清单），取消/失败回落原状态。
//! - 状态查询保持只读（`launchctl print`，≤2s 超时）；`release_plan` 与
//!   Python 侧 `dry_run_plan` 一样是纯展示，不执行任何命令、不写文件。
//! - 不新增 crate：仅用 `std` + 已引入的 `serde_json`/`tauri`。
//! - SMAppService（登录项）需 objc 桥，本切片不实现；`login_item` 字段如实
//!   返回 `"unknown"`，由父卡 ISS-010 后续补齐。

use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::AppHandle;

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

// ===========================================================================
// ISS-010B：发行态后台注册命令模块（用户同意流 + launchd 写路径）。
//
// 与 Python 侧 `fathom/launchd.py` 的 `release_plan` / `register_release` /
// `unregister_release` 同源同形（plist 结构、命令 argv、回滚语义一一对应；
// 跨语言一致性由两侧单测分别钉住同一结构）。本节是设置页开关的生产写
// 路径；所有真实执行只经 `autostart_register` / `autostart_unregister`
// 两个命令进入，且必须 `confirmed == true`。
// ===========================================================================

/// 与 ``fathom.config`` 同源（config.py `SCAN_HOUR = 12`）；改这里必须同步 Python 侧。
pub const DEFAULT_SCAN_HOUR: u32 = 12;

/// 真实 runner 的单命令超时（毫秒）。launchctl bootstrap/bootout 正常毫秒级
/// 返回；卡死时按失败处理并走回滚，不让设置页请求悬挂。
const LAUNCHCTL_TIMEOUT_MS: u64 = 10_000;

/// 命令执行器：``Ok((exit_code, combined_output))`` 表示已执行；``Err`` 表示
/// 无法执行（命令缺失/权限拒绝/超时）。测试注入 fake 实现全覆盖（fake 需要
/// 记账，天然是 FnMut），生产用 ``real_cmd_runner``（真实执行仅存在于本
/// 模块——grep 守护的不变量）。
type CmdRunner = dyn FnMut(&[&str]) -> Result<(i32, String), String>;

/// 发行态注册的路径上下文（全部可注入：测试用临时目录构造，绝不触碰
/// 本机 ~/Library/LaunchAgents）。
pub struct ReleaseCtx {
    pub helper_bin: PathBuf,
    pub launchagents_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub uid: String,
    pub scan_hour: u32,
    pub scan_minute: u32,
}

impl ReleaseCtx {
    fn scan_plist_path(&self) -> PathBuf {
        self.launchagents_dir.join(format!("{SCAN_LABEL}.plist"))
    }

    fn web_plist_path(&self) -> PathBuf {
        self.launchagents_dir.join(format!("{WEB_LABEL}.plist"))
    }

    fn domain(&self) -> String {
        format!("gui/{}", self.uid)
    }
}

/// plist 头/尾与键值对发射——与 Python `_PLIST_HEAD`/`_PLIST_TAIL`/`_pair`
/// 逐字节同形（跨语言一致性由两侧单测的黄金串钉住）。
const PLIST_HEAD: &str = concat!(
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
    "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n",
    "<plist version=\"1.0\">\n<dict>\n"
);
const PLIST_TAIL: &str = "</dict>\n</plist>\n";

fn plist_pair(key: &str, value: &str) -> String {
    format!("    <key>{key}</key>\n    <string>{value}</string>\n")
}

fn plist_argv_xml(argv: &[String]) -> String {
    let mut xml = String::from("    <key>ProgramArguments</key>\n    <array>\n");
    for arg in argv {
        xml.push_str(&format!("        <string>{arg}</string>\n"));
    }
    xml.push_str("    </array>\n");
    xml
}

/// 发行态扫描计划 plist：ProgramArguments = helper 二进制 + scan 子命令。
fn release_scan_plist(ctx: &ReleaseCtx) -> String {
    let argv: Vec<String> = vec![
        ctx.helper_bin.display().to_string(),
        "scan".into(),
        "--source".into(),
        "scheduled".into(),
    ];
    let mut xml = String::from(PLIST_HEAD);
    xml.push_str(&plist_pair("Label", SCAN_LABEL));
    xml.push_str(&plist_argv_xml(&argv));
    xml.push_str("    <key>StartCalendarInterval</key>\n    <dict>\n");
    xml.push_str(&format!(
        "        <key>Hour</key>\n        <integer>{}</integer>\n",
        ctx.scan_hour
    ));
    xml.push_str(&format!(
        "        <key>Minute</key>\n        <integer>{}</integer>\n    </dict>\n",
        ctx.scan_minute
    ));
    xml.push_str("    <key>ProcessType</key>\n    <string>Background</string>\n");
    xml.push_str(&plist_pair(
        "StandardOutPath",
        &ctx.logs_dir.join("launchd-scan.out.log").display().to_string(),
    ));
    xml.push_str(&plist_pair(
        "StandardErrorPath",
        &ctx.logs_dir.join("launchd-scan.err.log").display().to_string(),
    ));
    xml.push_str(PLIST_TAIL);
    xml
}

/// 发行态常驻 Web plist：ProgramArguments = helper 二进制 + serve 子命令。
fn release_web_plist(ctx: &ReleaseCtx) -> String {
    let argv: Vec<String> = vec![ctx.helper_bin.display().to_string(), "serve".into()];
    let mut xml = String::from(PLIST_HEAD);
    xml.push_str(&plist_pair("Label", WEB_LABEL));
    xml.push_str(&plist_argv_xml(&argv));
    xml.push_str("    <key>RunAtLoad</key>\n    <true/>\n");
    xml.push_str("    <key>KeepAlive</key>\n    <true/>\n");
    xml.push_str("    <key>ThrottleInterval</key>\n    <integer>30</integer>\n");
    xml.push_str("    <key>ProcessType</key>\n    <string>Background</string>\n");
    xml.push_str(&plist_pair(
        "StandardOutPath",
        &ctx.logs_dir.join("launchd-web.out.log").display().to_string(),
    ));
    xml.push_str(&plist_pair(
        "StandardErrorPath",
        &ctx.logs_dir.join("launchd-web.err.log").display().to_string(),
    ));
    xml.push_str(PLIST_TAIL);
    xml
}

/// 解析设置页传入的计划时间 ``HH:MM``；非法返回 None（调用方拒绝注册，
/// 不静默回落——用户确认的摘要必须与实际写入一致）。
fn parse_scan_time(raw: &str) -> Option<(u32, u32)> {
    let mut parts = raw.splitn(2, ':');
    let hour = parts.next()?;
    let minute = parts.next()?;
    if hour.is_empty() || minute.is_empty() || !hour.bytes().all(|b| b.is_ascii_digit())
        || !minute.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let hour: u32 = hour.parse().ok()?;
    let minute: u32 = minute.parse().ok()?;
    if hour > 23 || minute > 59 {
        return None;
    }
    Some((hour, minute))
}

/// 纯展示计划（同 Python `release_plan`）：plist 内容 + 将执行的命令清单
/// （uid 以 `<uid>` 占位）。**不执行任何命令、不写任何文件**。
fn release_plan_value(ctx: &ReleaseCtx) -> serde_json::Value {
    let scan_path = ctx.scan_plist_path();
    let web_path = ctx.web_plist_path();
    let commands: Vec<Vec<String>> = vec![
        vec!["id".into(), "-u".into()],
        vec![
            "launchctl".into(),
            "bootout".into(),
            "gui/<uid>".into(),
            scan_path.display().to_string(),
        ],
        vec![
            "launchctl".into(),
            "bootstrap".into(),
            "gui/<uid>".into(),
            scan_path.display().to_string(),
        ],
        vec![
            "launchctl".into(),
            "bootout".into(),
            "gui/<uid>".into(),
            web_path.display().to_string(),
        ],
        vec![
            "launchctl".into(),
            "bootstrap".into(),
            "gui/<uid>".into(),
            web_path.display().to_string(),
        ],
    ];
    serde_json::json!({
        "plist_files": [
            {"label": SCAN_LABEL, "path": scan_path.display().to_string(),
             "content": release_scan_plist(ctx)},
            {"label": WEB_LABEL, "path": web_path.display().to_string(),
             "content": release_web_plist(ctx)},
        ],
        "commands": commands,
        "uid_placeholder": "<uid>",
        "helper_bin": ctx.helper_bin.display().to_string(),
        "warning": "release_plan 仅展示将写入的 plist 与 launchctl 命令清单，不执行任何命令、不创建/修改任何文件。需用户明确确认后才会经注册命令执行。",
    })
}

/// 注册/注销结果的统一 JSON 形（与 Python 侧 outcome 同键）。
struct WriteOutcome {
    ok: bool,
    error: Option<String>,
    commands: Vec<Vec<String>>,
    rolled_back: bool,
    warnings: Vec<String>,
}

impl WriteOutcome {
    fn new() -> Self {
        WriteOutcome {
            ok: false,
            error: None,
            commands: Vec::new(),
            rolled_back: false,
            warnings: Vec::new(),
        }
    }

    fn to_json(self, action: &str, confirmed: bool, ctx: &ReleaseCtx) -> serde_json::Value {
        // 动态键（SCAN_LABEL/WEB_LABEL 是 const &str）用 Map 显式构建，避免
        // json! 宏对非字面量键的展开差异。
        let mut plist_paths = serde_json::Map::new();
        plist_paths.insert(
            SCAN_LABEL.to_string(),
            serde_json::Value::String(ctx.scan_plist_path().display().to_string()),
        );
        plist_paths.insert(
            WEB_LABEL.to_string(),
            serde_json::Value::String(ctx.web_plist_path().display().to_string()),
        );
        let mut value = serde_json::json!({
            "ok": self.ok,
            "action": action,
            "confirmed": confirmed,
            "commands": self.commands,
            "rolled_back": self.rolled_back,
            "warnings": self.warnings,
        });
        value["plist_paths"] = serde_json::Value::Object(plist_paths);
        if let Some(error) = self.error {
            value["error"] = serde_json::Value::String(error);
        }
        value
    }
}

/// 真实命令执行器：``Command::output`` 带软超时轮询（超时 kill 自己起的
/// 子进程并按失败处理）。真实执行只存在于本模块（grep 守护）。
fn real_cmd_runner(argv: &[&str]) -> Result<(i32, String), String> {
    let mut child = Command::new(argv[0])
        .args(&argv[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("无法执行 {}: {err}", argv[0]))?;
    let deadline = Instant::now() + Duration::from_millis(LAUNCHCTL_TIMEOUT_MS);
    let mut exited: Option<i32> = None;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(status)) => {
                exited = Some(status.code().unwrap_or(-1));
                break;
            }
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(err) => return Err(format!("等待 {} 退出失败: {err}", argv[0])),
        }
    }
    let code = match exited {
        Some(code) => code,
        None => {
            // 超时：只 kill 自己 spawn 的子进程（与 probe_label 同策略）。
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("执行 {} 超时（{}ms）", argv[0], LAUNCHCTL_TIMEOUT_MS));
        }
    };
    let output = child
        .wait_with_output()
        .map_err(|err| format!("读取 {} 输出失败: {err}", argv[0]))?;
    let mut combined = String::from_utf8_lossy(&output.stdout).to_string();
    combined.push_str(&String::from_utf8_lossy(&output.stderr));
    Ok((code, combined))
}

/// 注册写路径核心（唯一命中；测试注入 fake runner + 临时目录）。
///
/// 步骤：确认检查 → uid 检查 → 建目录 → 写两份 plist → 每标签
/// bootout（幂等清理，忽略退出码）+ bootstrap（必须 0）。任一步失败 →
/// 回滚（两标签 bootout + 删除已写 plist），不留半注册态。
fn register_release_core(ctx: &ReleaseCtx, confirmed: bool, run: &mut CmdRunner) -> serde_json::Value {
    let mut outcome = WriteOutcome::new();

    if !confirmed {
        outcome.error = Some("已拒绝：注册需要用户明确确认（confirmed=true）".into());
        return outcome.to_json("register", confirmed, ctx);
    }
    if ctx.uid.is_empty() {
        outcome.error = Some("无法解析当前 uid（id -u 失败），拒绝注册".into());
        return outcome.to_json("register", confirmed, ctx);
    }

    let specs: [(String, PathBuf); 2] = [
        (SCAN_LABEL.to_string(), ctx.scan_plist_path()),
        (WEB_LABEL.to_string(), ctx.web_plist_path()),
    ];

    // 先写两份 plist；任何一份失败 → 清掉本次已写部分，零 bootstrap。
    let mut written: Vec<PathBuf> = Vec::new();
    let write_result = (|| -> Result<(), String> {
        std::fs::create_dir_all(&ctx.launchagents_dir)
            .map_err(|err| format!("创建目录 {} 失败: {err}", ctx.launchagents_dir.display()))?;
        let contents = [
            (SCAN_LABEL, release_scan_plist(ctx)),
            (WEB_LABEL, release_web_plist(ctx)),
        ];
        for (label, content) in &contents {
            let path = ctx.launchagents_dir.join(format!("{label}.plist"));
            std::fs::write(&path, content)
                .map_err(|err| format!("写入 plist 失败（{label}）: {err}"))?;
            written.push(path);
        }
        Ok(())
    })();
    if let Err(error) = write_result {
        for path in &written {
            if let Err(err) = std::fs::remove_file(path) {
                outcome
                    .warnings
                    .push(format!("回滚清理 {} 失败: {err}", path.display()));
            }
        }
        outcome.rolled_back = !written.is_empty();
        outcome.error = Some(error);
        return outcome.to_json("register", confirmed, ctx);
    }

    let domain = ctx.domain();
    let rollback = |outcome: &mut WriteOutcome, run: &mut CmdRunner| {
        for (_, path) in &specs {
            let path_str = path.display().to_string();
            outcome.commands.push(vec![
                "launchctl".into(),
                "bootout".into(),
                domain.clone(),
                path_str.clone(),
            ]);
            if let Err(err) = run(&["launchctl", "bootout", domain.as_str(), path_str.as_str()]) {
                outcome.warnings.push(format!("回滚 bootout {path_str} 失败: {err}"));
            }
        }
        for (_, path) in &specs {
            if let Err(err) = std::fs::remove_file(path) {
                if err.kind() != std::io::ErrorKind::NotFound {
                    outcome
                        .warnings
                        .push(format!("回滚删除 {} 失败: {err}", path.display()));
                }
            }
        }
        outcome.rolled_back = true;
    };

    for (label, path) in &specs {
        let path_str = path.display().to_string();
        // 先 bootout 幂等清理旧版本（与 dev 态 _bootstrap 同形；退出码忽略）。
        outcome.commands.push(vec![
            "launchctl".into(),
            "bootout".into(),
            domain.clone(),
            path_str.clone(),
        ]);
        if let Err(err) = run(&["launchctl", "bootout", domain.as_str(), path_str.as_str()]) {
            outcome.warnings.push(format!("bootout 清理 {path_str} 未执行: {err}"));
        }
        outcome.commands.push(vec![
            "launchctl".into(),
            "bootstrap".into(),
            domain.clone(),
            path_str.clone(),
        ]);
        match run(&["launchctl", "bootstrap", domain.as_str(), path_str.as_str()]) {
            Ok((0, _)) => {}
            Ok((code, output)) => {
                let detail = if output.trim().is_empty() {
                    format!("退出码 {code}")
                } else {
                    format!("退出码 {code}：{}", output.trim())
                };
                outcome.error = Some(format!(
                    "launchctl bootstrap 失败（{label}）：{detail}，已回滚"
                ));
                rollback(&mut outcome, run);
                return outcome.to_json("register", confirmed, ctx);
            }
            Err(err) => {
                outcome.error = Some(format!(
                    "launchctl bootstrap 失败（{label}）：无法执行：{err}，已回滚"
                ));
                rollback(&mut outcome, run);
                return outcome.to_json("register", confirmed, ctx);
            }
        }
    }

    outcome.ok = true;
    outcome.to_json("register", confirmed, ctx)
}

/// 注销写路径核心（唯一命中）：每标签 bootout（幂等，退出码只记 warnings）
/// + 删除 plist（missing 容忍；删除失败 → ok=false，残留 plist 会在下次
/// 登录被 launchd 重新加载，必须让 UI 如实显示注销未完成）。
fn unregister_release_core(ctx: &ReleaseCtx, confirmed: bool, run: &mut CmdRunner) -> serde_json::Value {
    let mut outcome = WriteOutcome::new();
    if !confirmed {
        outcome.error = Some("已拒绝：注销需要用户明确确认（confirmed=true）".into());
        return outcome.to_json("unregister", confirmed, ctx);
    }
    if ctx.uid.is_empty() {
        outcome.error = Some("无法解析当前 uid（id -u 失败），拒绝注销".into());
        return outcome.to_json("unregister", confirmed, ctx);
    }

    let domain = ctx.domain();
    let mut unlink_errors: Vec<String> = Vec::new();
    for label in [SCAN_LABEL, WEB_LABEL] {
        outcome.commands.push(vec![
            "launchctl".into(),
            "bootout".into(),
            domain.clone(),
            label.into(),
        ]);
        match run(&["launchctl", "bootout", domain.as_str(), label]) {
            Ok((0, _)) => {}
            Ok((code, output)) => {
                if !output.contains("Could not find service") {
                    outcome.warnings.push(format!(
                        "bootout {label} 退出码 {code}：{}",
                        if output.trim().is_empty() { "无输出".into() } else { output.trim().to_string() }
                    ));
                }
            }
            Err(err) => outcome.warnings.push(format!("bootout {label} 无法执行: {err}")),
        }
        let path = ctx.launchagents_dir.join(format!("{label}.plist"));
        if let Err(err) = std::fs::remove_file(&path) {
            if err.kind() != std::io::ErrorKind::NotFound {
                unlink_errors.push(format!("{label}: {err}"));
            }
        }
    }

    if !unlink_errors.is_empty() {
        outcome.error = Some(format!(
            "注销未完成（plist 删除失败，残留文件会在下次登录被 launchd 重新加载）：{}",
            unlink_errors.join("；")
        ));
        return outcome.to_json("unregister", confirmed, ctx);
    }
    outcome.ok = true;
    outcome.to_json("unregister", confirmed, ctx)
}

/// 从 Tauri AppHandle 解析发行态注册上下文：helper 二进制（resource 目录或
/// FATHOM_HELPER_BIN）、LaunchAgents 目录（HOME 下）、日志目录（运行根/logs）、
/// uid、计划时间。任何一步失败返回 Err（调用方转成 {ok:false} JSON）。
fn release_ctx_from_app(app: &AppHandle, scan_time: Option<&str>) -> Result<ReleaseCtx, String> {
    let helper_bin = crate::helper::locate_helper(app)?;
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "无法解析 HOME，拒绝注册路径解析".to_string())?;
    let launchagents_dir = home.join("Library").join("LaunchAgents");
    let runtime_dir = std::env::var_os("FATHOM_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(crate::helper::default_runtime_dir);
    let logs_dir = runtime_dir.join("logs");
    let (scan_hour, scan_minute) = match scan_time {
        None => (DEFAULT_SCAN_HOUR, 0),
        Some(raw) => parse_scan_time(raw).ok_or_else(|| {
            format!("scan_time 非法（{raw:?}）：需要 HH:MM 且 0-23/0-59，拒绝注册")
        })?,
    };
    let uid = read_uid().unwrap_or_default();
    Ok(ReleaseCtx {
        helper_bin,
        launchagents_dir,
        logs_dir,
        uid,
        scan_hour,
        scan_minute,
    })
}

/// Tauri 命令：发行态注册的纯展示计划（plist 摘要 + 命令清单，供设置页
/// 确认弹层展示）。不执行任何命令、不写任何文件。
#[tauri::command]
pub fn autostart_register_plan(app: AppHandle, scan_time: Option<String>) -> serde_json::Value {
    match release_ctx_from_app(&app, scan_time.as_deref()) {
        Ok(ctx) => release_plan_value(&ctx),
        Err(error) => serde_json::json!({"ok": false, "error": error}),
    }
}

/// Tauri 命令：发行态注册（需 confirmed=true）。失败自动回滚不留半注册态。
#[tauri::command]
pub fn autostart_register(
    app: AppHandle,
    confirmed: bool,
    scan_time: Option<String>,
) -> serde_json::Value {
    let ctx = match release_ctx_from_app(&app, scan_time.as_deref()) {
        Ok(ctx) => ctx,
        Err(error) => return serde_json::json!({"ok": false, "error": error}),
    };
    register_release_core(&ctx, confirmed, &mut real_cmd_runner)
}

/// Tauri 命令：发行态注销（需 confirmed=true）。bootout 幂等 + 删 plist；
/// 删除失败如实 ok=false。
#[tauri::command]
pub fn autostart_unregister(app: AppHandle, confirmed: bool) -> serde_json::Value {
    let ctx = match release_ctx_from_app(&app, None) {
        Ok(ctx) => ctx,
        Err(error) => return serde_json::json!({"ok": false, "error": error}),
    };
    unregister_release_core(&ctx, confirmed, &mut real_cmd_runner)
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

    // ----- ISS-010B：发行态注册命令模块（fake runner + 临时目录全覆盖） -----

    use std::sync::atomic::{AtomicU64, Ordering};

    /// 测试用临时目录（std 实现，不引 crate；Drop 时清理）。
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static COUNTER: AtomicU64 = AtomicU64::new(0);
            let id = COUNTER.fetch_add(1, Ordering::Relaxed);
            let dir = std::env::temp_dir()
                .join(format!("fathom-iss010b-{}-{}-{}", tag, std::process::id(), id));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).expect("测试临时目录创建失败");
            TempDir(dir)
        }

        fn path(&self) -> &std::path::Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn test_ctx(temp: &TempDir) -> ReleaseCtx {
        ReleaseCtx {
            helper_bin: temp.path().join("helper"),
            launchagents_dir: temp.path().join("LaunchAgents"),
            logs_dir: temp.path().join("logs"),
            uid: "501".to_string(),
            scan_hour: DEFAULT_SCAN_HOUR,
            scan_minute: 0,
        }
    }

    /// fake runner：记录全部 argv；按匹配器注入失败（bootstrap 失败/无法执行）。
    struct FakeRunner {
        calls: Vec<Vec<String>>,
        fail_bootstrap_for: Option<String>, // 命中该路径的 bootstrap 返回 rc=5
        fail_spawn_for: Option<String>,     // 命中该路径的 bootstrap 返回 Err
    }

    impl FakeRunner {
        fn new() -> Self {
            FakeRunner {
                calls: Vec::new(),
                fail_bootstrap_for: None,
                fail_spawn_for: None,
            }
        }

        fn run(&mut self, argv: &[&str]) -> Result<(i32, String), String> {
            let owned: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
            self.calls.push(owned.clone());
            if argv[1] == "bootstrap" {
                if let Some(target) = &self.fail_spawn_for {
                    if argv[3] == target.as_str() {
                        return Err("spawn failed (fake)".to_string());
                    }
                }
                if let Some(target) = &self.fail_bootstrap_for {
                    if argv[3] == target.as_str() {
                        return Ok((5, "Bootstrap failed: 5: Input/output error".to_string()));
                    }
                }
            }
            Ok((0, String::new()))
        }

        fn argv_strings(&self) -> Vec<Vec<String>> {
            self.calls.clone()
        }
    }

    /// 发行态扫描 plist：helper 二进制 + scan 子命令 + 可配置计划时间。
    #[test]
    fn release_scan_plist_has_helper_argv_and_schedule() {
        let temp = TempDir::new("plist-scan");
        let mut ctx = test_ctx(&temp);
        ctx.scan_hour = 13;
        ctx.scan_minute = 30;
        let xml = release_scan_plist(&ctx);
        assert!(xml.contains(&format!("<string>{}</string>", ctx.helper_bin.display())));
        assert!(xml.contains("<string>scan</string>"));
        assert!(xml.contains("<string>--source</string>"));
        assert!(xml.contains("<string>scheduled</string>"));
        assert!(xml.contains("<key>StartCalendarInterval</key>"));
        assert!(xml.contains("<integer>13</integer>"));
        assert!(xml.contains("<integer>30</integer>"));
        assert!(xml.contains(&format!(
            "<string>{}</string>",
            ctx.logs_dir.join("launchd-scan.out.log").display()
        )));
        assert!(xml.starts_with("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"));
    }

    /// 发行态 Web plist：helper + serve + RunAtLoad/KeepAlive（与 dev 态同源结构）。
    #[test]
    fn release_web_plist_has_serve_and_keepalive() {
        let temp = TempDir::new("plist-web");
        let ctx = test_ctx(&temp);
        let xml = release_web_plist(&ctx);
        assert!(xml.contains(&format!("<string>{}</string>", ctx.helper_bin.display())));
        assert!(xml.contains("<string>serve</string>"));
        assert!(xml.contains("<key>RunAtLoad</key>"));
        assert!(xml.contains("<key>KeepAlive</key>"));
        assert!(xml.contains("<key>ThrottleInterval</key>"));
        // 绝不出现 dev 态的 main.py 参数
        assert!(!xml.contains("main.py"));
    }

    /// scan_time 解析矩阵：合法/非法/边界。
    #[test]
    fn parse_scan_time_matrix() {
        assert_eq!(parse_scan_time("12:00"), Some((12, 0)));
        assert_eq!(parse_scan_time("23:59"), Some((23, 59)));
        assert_eq!(parse_scan_time("00:00"), Some((0, 0)));
        assert_eq!(parse_scan_time("24:00"), None);
        assert_eq!(parse_scan_time("12:60"), None);
        assert_eq!(parse_scan_time("12"), None);
        assert_eq!(parse_scan_time("ab:cd"), None);
        assert_eq!(parse_scan_time("12:-1"), None);
        assert_eq!(parse_scan_time(""), None);
    }

    /// release_plan 是纯展示：不接受 runner 参数（类型层面就无执行通道），
    /// 输出仅含 plist 内容与命令字符串清单。
    #[test]
    fn release_plan_never_executes() {
        let temp = TempDir::new("plan");
        let ctx = test_ctx(&temp);
        let plan = release_plan_value(&ctx);
        assert!(plan["plist_files"].as_array().unwrap().len() == 2);
        let commands = plan["commands"].as_array().unwrap();
        assert!(commands.len() == 5, "id -u + (bootout+bootstrap)×2");
        let flat: Vec<String> = commands
            .iter()
            .map(|c| {
                c.as_array()
                    .unwrap()
                    .iter()
                    .map(|x| x.as_str().unwrap())
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .collect();
        assert_eq!(flat[0], "id -u");
        assert!(flat.iter().any(|c| c.starts_with("launchctl bootout gui/<uid>")));
        assert!(flat.iter().any(|c| c.starts_with("launchctl bootstrap gui/<uid>")));
        assert_eq!(plan["uid_placeholder"], "<uid>");
    }

    /// confirmed=false：拒绝且零副作用（零命令、零文件）。
    #[test]
    fn register_refuses_without_confirmation() {
        let temp = TempDir::new("reg-noconfirm");
        let ctx = test_ctx(&temp);
        let mut runner = FakeRunner::new();
        let outcome = register_release_core(&ctx, false, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false));
        assert!(outcome["error"].as_str().unwrap().contains("确认"));
        assert!(runner.calls.is_empty(), "未确认时零命令");
        assert!(!ctx.scan_plist_path().exists());
    }

    /// uid 缺失：拒绝（fail-closed）。
    #[test]
    fn register_refuses_without_uid() {
        let temp = TempDir::new("reg-nouid");
        let mut ctx = test_ctx(&temp);
        ctx.uid = String::new();
        let mut runner = FakeRunner::new();
        let outcome = register_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false));
        assert!(runner.calls.is_empty());
    }

    /// 成功路径：写两份 plist + 每标签 bootout 清理 + bootstrap。
    #[test]
    fn register_happy_path_writes_and_bootstraps_both() {
        let temp = TempDir::new("reg-ok");
        let mut ctx = test_ctx(&temp);
        ctx.scan_hour = 13;
        let mut runner = FakeRunner::new();
        let outcome = register_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(true), "{outcome}");
        assert_ne!(outcome["rolled_back"], serde_json::json!(true));
        let scan_path = ctx.scan_plist_path();
        let web_path = ctx.web_plist_path();
        assert!(scan_path.is_file() && web_path.is_file());
        let scan_content = std::fs::read_to_string(&scan_path).unwrap();
        assert!(scan_content.contains("<integer>13</integer>"));
        // 命令序列：(bootout, bootstrap)×scan → (bootout, bootstrap)×web
        let scan_str = scan_path.display().to_string();
        let web_str = web_path.display().to_string();
        let expected: Vec<Vec<String>> = vec![
            vec!["launchctl".into(), "bootout".into(), "gui/501".into(), scan_str.clone()],
            vec!["launchctl".into(), "bootstrap".into(), "gui/501".into(), scan_str],
            vec!["launchctl".into(), "bootout".into(), "gui/501".into(), web_str.clone()],
            vec!["launchctl".into(), "bootstrap".into(), "gui/501".into(), web_str],
        ];
        let calls = runner.argv_strings();
        assert_eq!(calls, expected, "命令序列必须与计划一致");
    }

    /// bootstrap 失败（web 标签 rc=5）：两标签回滚 bootout + 两份 plist 删除。
    #[test]
    fn register_bootstrap_failure_rolls_back_completely() {
        let temp = TempDir::new("reg-rollback");
        let ctx = test_ctx(&temp);
        let mut runner = FakeRunner::new();
        runner.fail_bootstrap_for = Some(ctx.web_plist_path().display().to_string());
        let outcome = register_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false), "{outcome}");
        assert_eq!(outcome["rolled_back"], serde_json::json!(true));
        let error = outcome["error"].as_str().unwrap();
        assert!(error.contains("bootstrap"), "{error}");
        assert!(!ctx.scan_plist_path().exists(), "回滚后不留 scan plist");
        assert!(!ctx.web_plist_path().exists(), "回滚后不留 web plist");
        let bootouts: Vec<Vec<String>> = runner
            .argv_strings()
            .into_iter()
            .filter(|c| c.len() >= 2 && c[1] == "bootout")
            .collect();
        assert_eq!(bootouts.len(), 4, "清理 2 + 回滚 2：{bootouts:?}");
    }

    /// bootstrap 无法执行（spawn Err）：同样完整回滚。
    #[test]
    fn register_spawn_failure_rolls_back_completely() {
        let temp = TempDir::new("reg-spawnfail");
        let ctx = test_ctx(&temp);
        let mut runner = FakeRunner::new();
        runner.fail_spawn_for = Some(ctx.scan_plist_path().display().to_string());
        let outcome = register_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false), "{outcome}");
        assert_eq!(outcome["rolled_back"], serde_json::json!(true));
        assert!(!ctx.scan_plist_path().exists());
        assert!(!ctx.web_plist_path().exists());
    }

    /// 目录被同名文件占用（create_dir_all 失败）：零 launchctl 调用。
    #[test]
    fn register_dir_failure_bootstraps_nothing() {
        let temp = TempDir::new("reg-dirfail");
        let ctx = test_ctx(&temp);
        std::fs::write(&ctx.launchagents_dir, "不是目录").unwrap();
        let mut runner = FakeRunner::new();
        let outcome = register_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false));
        assert!(runner.calls.is_empty(), "写目录失败时零命令：{:?}", runner.calls);
    }

    /// 第二份 plist 写失败（web 路径被目录占位）：第一份被清理、零 bootstrap。
    #[test]
    fn register_second_write_failure_cleans_first() {
        let temp = TempDir::new("reg-write2fail");
        let ctx = test_ctx(&temp);
        std::fs::create_dir_all(ctx.launchagents_dir.join(format!("{WEB_LABEL}.plist"))).unwrap();
        let mut runner = FakeRunner::new();
        let outcome = register_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false), "{outcome}");
        assert!(runner.calls.is_empty(), "写失败时零 bootstrap：{:?}", runner.calls);
        assert!(!ctx.scan_plist_path().exists(), "已写的 scan plist 必须被清理");
    }

    /// 注销：confirmed=false 拒绝且零命令。
    #[test]
    fn unregister_refuses_without_confirmation() {
        let temp = TempDir::new("unreg-noconfirm");
        let ctx = test_ctx(&temp);
        let mut runner = FakeRunner::new();
        let outcome = unregister_release_core(&ctx, false, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false));
        assert!(runner.calls.is_empty());
    }

    /// 注销成功：两标签 bootout（按 label）+ 两份 plist 删除。
    #[test]
    fn unregister_happy_path_bootouts_and_unlinks() {
        let temp = TempDir::new("unreg-ok");
        let ctx = test_ctx(&temp);
        std::fs::create_dir_all(&ctx.launchagents_dir).unwrap();
        std::fs::write(ctx.scan_plist_path(), "<plist/>").unwrap();
        std::fs::write(ctx.web_plist_path(), "<plist/>").unwrap();
        let mut runner = FakeRunner::new();
        let outcome = unregister_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(true), "{outcome}");
        let calls = runner.argv_strings();
        assert_eq!(calls.len(), 2, "{calls:?}");
        assert_eq!(calls[0], vec!["launchctl", "bootout", "gui/501", SCAN_LABEL]);
        assert_eq!(calls[1], vec!["launchctl", "bootout", "gui/501", WEB_LABEL]);
        assert!(!ctx.scan_plist_path().exists());
        assert!(!ctx.web_plist_path().exists());
    }

    /// 注销幂等：plist 不存在仍 ok（bootout 照发，退出码 0）。
    #[test]
    fn unregister_missing_plists_is_idempotent_ok() {
        let temp = TempDir::new("unreg-missing");
        let ctx = test_ctx(&temp);
        let mut runner = FakeRunner::new();
        let outcome = unregister_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(true), "{outcome}");
        assert_eq!(runner.argv_strings().len(), 2);
    }

    /// 注销 bootout 报「服务不存在」：不产生 warning（正常态）。
    #[test]
    fn unregister_not_found_bootout_is_not_warned() {
        let temp = TempDir::new("unreg-notfound");
        let ctx = test_ctx(&temp);
        let outcome = unregister_release_core(&ctx, true, &mut |argv| {
            if argv[1] == "bootout" {
                Ok((
                    3,
                    "Could not find service: \"gui/501/x\" in domain gui".to_string(),
                ))
            } else {
                Ok((0, String::new()))
            }
        });
        assert_eq!(outcome["ok"], serde_json::json!(true), "{outcome}");
        assert_eq!(outcome["warnings"].as_array().unwrap().len(), 0);
    }

    /// 注销 unlink 失败（web 路径被目录占位）：ok=false，scan 仍被清。
    #[test]
    fn unregister_unlink_failure_reports_not_ok() {
        let temp = TempDir::new("unreg-unlinkfail");
        let ctx = test_ctx(&temp);
        std::fs::create_dir_all(&ctx.launchagents_dir).unwrap();
        std::fs::write(ctx.scan_plist_path(), "<plist/>").unwrap();
        std::fs::create_dir_all(ctx.web_plist_path()).unwrap();
        let mut runner = FakeRunner::new();
        let outcome = unregister_release_core(&ctx, true, &mut |argv| runner.run(argv));
        assert_eq!(outcome["ok"], serde_json::json!(false), "{outcome}");
        assert!(outcome["error"].as_str().unwrap().contains(WEB_LABEL));
        assert!(!ctx.scan_plist_path().exists());
        assert!(ctx.web_plist_path().is_dir(), "占位目录不是我们的，保留");
    }
}