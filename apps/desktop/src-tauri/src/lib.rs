//! Fathom 桌面壳：菜单栏常驻 + 主窗口（拉起并握手内嵌 helper，导航到本地服务）。
//!
//! 设计原则：
//! - 壳负责 helper 进程生命周期（ISS-009 切片 1）：启动时拉起 PyInstaller
//!   onedir helper，经 ``helper-instance.json`` / ``/health`` 握手后导航主
//!   窗口到本地服务；退出时按身份 SIGTERM 回收；同服务已在跑则复用。
//! - 退出回收覆盖所有路径（ISS-057）：tray 菜单「退出」与非 tray 路径
//!   （Cmd+Q / AppleScript quit / 系统注销等，经 ``RunEvent::ExitRequested``
//!   / ``RunEvent::Exit`` 全局钩子）都调用同一幂等回收；关闭窗口 = 隐藏
//!   （macOS 菜单栏应用惯例）；退出流程只向本壳拉起的 helper 发信号，
//!   不触碰外部同服务实例。
//! - tray 标题（剩余空间）由前端页面定期 invoke `update_tray_status` 推送，
//!   Rust 侧不引 HTTP 依赖。
//! - helper 路径定位：打包态 ``resource_dir()/helper/fathom-helper/fathom-helper``；
//!   开发态 ``FATHOM_HELPER_BIN`` 覆盖；缺失时握手页显示明确错误而非 panic。
//!
//! 不引入新 crate（--locked --offline 门禁下避免拉不到依赖）；helper 进程
//! 操作全用 ``std::process::Command`` 与 ``std`` 文件 I/O。

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, State, Url, WebviewWindow, WindowEvent,
};

mod autostart;
mod helper;

use helper::{
    default_runtime_dir, exhausted_status_json, locate_helper, spawn_helper, HelperEvent,
    HelperHandle, HANDSHAKE_TIMEOUT_S, STOP_TIMEOUT_S,
};

struct TrayStatusMenu(MenuItem<tauri::Wry>);

/// 全局 helper 句柄；Tauri ``State`` 通过 ``app.manage`` 注入。
struct HelperState(Mutex<Option<HelperHandle>>);

/// tray 标题推送：空值隐藏对应元素，超长截断；失败记日志不向调用方扩散。
#[tauri::command]
fn update_tray_status(app: AppHandle, title: String, tooltip: String) {
    const TITLE_MAX_CHARS: usize = 16; // 菜单栏横向空间有限，防异常超长标题撑爆

    let Some(tray) = app.tray_by_id("sentinel") else {
        eprintln!("[tray] update_tray_status: sentinel tray 不存在，忽略本次推送");
        return;
    };

    let title = title.trim();
    let title = if title.is_empty() {
        None
    } else if title.chars().count() > TITLE_MAX_CHARS {
        let truncated: String = title.chars().take(TITLE_MAX_CHARS).collect();
        eprintln!("[tray] 标题超长已截断: \"{}\" -> \"{}\"", title, truncated);
        Some(truncated)
    } else {
        Some(title.to_string())
    };
    if let Err(e) = tray.set_title(title) {
        eprintln!("[tray] set_title 失败: {e}");
    }

    let tooltip = tooltip.trim();
    if let Some(status) = app.try_state::<TrayStatusMenu>() {
        if let Err(e) = status.0.set_text(if tooltip.is_empty() { "Fathom" } else { tooltip }) {
            eprintln!("[tray] 状态菜单更新失败: {e}");
        }
    }
    if let Err(e) = tray.set_tooltip(if tooltip.is_empty() { None } else { Some(tooltip) }) {
        eprintln!("[tray] set_tooltip 失败: {e}");
    }
}

/// 查询 helper 当前握手状态；前端握手页用。返回 JSON object：
/// - ``state``: ``"ready"`` | ``"starting"`` | ``"reused"`` | ``"exhausted"`` | ``"error"``
/// - ``port``: u16（ready/reused 时）
/// - ``log_path``: 错误或诊断时
/// - ``recovery``: exhausted 时恢复信息（``ports`` 候选端口列表 + 各端口占用
///   pid（未知 null）+ ``hint`` 恢复提示文案键；ISS-059）
/// - ``error``: error 时
#[tauri::command]
fn helper_status(state: State<'_, HelperState>) -> serde_json::Value {
    let guard = match state.0.lock() {
        Ok(g) => g,
        Err(_) => return serde_json::json!({"state": "error", "error": "helper 句柄锁中毒"}),
    };
    let Some(handle) = guard.as_ref() else {
        return serde_json::json!({"state": "starting"});
    };
    let runtime = handle.runtime_dir.clone();
    // ISS-059：端口耗尽已判定时直接呈现 exhausted（含 recovery），不再重跑
    // 20s 握手；恢复路径是握手页「重试握手」（helper_retry 用新句柄重新
    // 走完整流程，耗尽信息随句柄重建而清空）。
    if let Some(info) = handle.exhausted_info() {
        return exhausted_status_json(&info, &runtime);
    }
    match handle.handshake() {
        Ok(Some(instance)) => {
            if handle.reused.load(std::sync::atomic::Ordering::SeqCst) {
                serde_json::json!({
                    "state": "reused",
                    "port": instance.port,
                    "service": instance.service,
                    "protocol_version": instance.protocol_version,
                    "version": instance.version,
                })
            } else {
                serde_json::json!({
                    "state": "ready",
                    "port": instance.port,
                    "service": instance.service,
                    "protocol_version": instance.protocol_version,
                    "version": instance.version,
                    "instance_id": instance.instance_id,
                    "runtime_mode": instance.runtime_mode,
                })
            }
        }
        Ok(None) => serde_json::json!({"state": "starting"}),
        Err(err) => serde_json::json!({
            "state": "error",
            "error": err,
            "log_path": runtime.join("logs").join("helper.log").display().to_string(),
            "runtime_dir": runtime.display().to_string(),
            "timeout_s": HANDSHAKE_TIMEOUT_S,
            "stop_timeout_s": STOP_TIMEOUT_S,
        }),
    }
}

/// ``start_helper_internal`` 失败后的状态 JSON（ISS-059）：端口耗尽（新句柄
/// 里已记录 ``ExhaustedInfo``）优先呈现 ``state=exhausted``，其余失败保持
/// ``state=error``——不得把普通 error 误报成 exhausted。
fn retry_error_json(
    err: &str,
    exhausted: Option<helper::ExhaustedInfo>,
    runtime: &Path,
) -> serde_json::Value {
    if let Some(info) = exhausted {
        return exhausted_status_json(&info, runtime);
    }
    serde_json::json!({
        "state": "error",
        "error": err,
        "log_path": runtime.join("logs").join("helper.log").display().to_string(),
        "runtime_dir": runtime.display().to_string(),
        "timeout_s": HANDSHAKE_TIMEOUT_S,
        "stop_timeout_s": STOP_TIMEOUT_S,
    })
}

/// 重启 helper：先 stop 当前句柄，再 spawn 新的子进程并握手；返回 ``helper_status`` 同样 JSON。
/// ISS-059：exhausted 后重试同一流程；仍耗尽则仍返回 exhausted（新句柄会
/// 记录新的 ``ExhaustedInfo``）。
#[tauri::command]
fn helper_retry(app: AppHandle, state: State<'_, HelperState>) -> serde_json::Value {
    let runtime = match state.0.lock() {
        Ok(g) => match g.as_ref() {
            Some(h) => h.runtime_dir.clone(),
            None => default_runtime_dir(),
        },
        Err(_) => return serde_json::json!({"state": "error", "error": "helper 句柄锁中毒"}),
    };
    // 先 stop（ISS-060 (d)：错误不再静默丢弃，落 eprintln 便于排查）。
    // 既有语义由 ``HelperHandle::stop`` 自身保证：复用模式零信号、
    // 本壳子进程 bounded 10s SIGTERM → SIGKILL 兜底属设计内。
    match state.0.lock() {
        Ok(g) => {
            if let Some(h) = g.as_ref() {
                if let Err(err) = h.stop() {
                    eprintln!("[helper] helper_retry: stop() 失败：{err}");
                }
            }
        }
        Err(_) => eprintln!("[helper] helper_retry: HelperState 锁中毒，跳过 stop()"),
    }
    // 重置 handle
    if let Ok(mut g) = state.0.lock() {
        *g = Some(HelperHandle::new(runtime.clone()));
    }
    match start_helper_internal(&app, &state) {
        Ok(_) => helper_status(state),
        Err(err) => {
            let exhausted = state
                .0
                .lock()
                .ok()
                .and_then(|g| g.as_ref().and_then(|h| h.exhausted_info()));
            retry_error_json(&err, exhausted, &runtime)
        }
    }
}

/// 导航主窗口到 ``http://127.0.0.1:<port>/``；仅在握手成功时调用。
fn navigate_main_to(window: &WebviewWindow, port: u16) -> Result<(), String> {
    let url_str = format!("http://127.0.0.1:{}/", port);
    let parsed = Url::parse(&url_str).map_err(|err| {
        format!("主窗口导航 URL 解析 {} 失败：{}", url_str, err)
    })?;
    window.navigate(parsed).map_err(|err| {
        format!("主窗口导航 {} 失败：{}", url_str, err)
    })
}

/// 启动 helper 并握手；成功后导航主窗口。返回 ``Result<(), String>``。
///
/// 错误由 ``start_helper_internal`` 上抛；握手页通过 ``helper_status`` 命令查询。
fn start_helper_internal(app: &AppHandle, state: &State<'_, HelperState>) -> Result<(), String> {
    let bin = locate_helper(app)?;
    let runtime = state
        .0
        .lock()
        .map_err(|_| "helper 句柄锁中毒".to_string())?
        .as_ref()
        .map(|h| h.runtime_dir.clone())
        .unwrap_or_else(default_runtime_dir);

    let (child, log_offset) = spawn_helper(&bin, &runtime)?;
    let port = std::env::var("FATHOM_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(helper::DEFAULT_PORT);
    if let Ok(mut g) = state.0.lock() {
        if let Some(h) = g.as_mut() {
            h.adopt(child, port, log_offset);
        }
    }

    // 握手：拿 helper-instance.json 或 /health
    let handshake_result = {
        let guard = state.0.lock().map_err(|_| "helper 句柄锁中毒".to_string())?;
        match guard.as_ref() {
            Some(h) => h.handshake(),
            None => Err("helper 句柄未初始化".to_string()),
        }
    };
    match handshake_result {
        Ok(Some(instance)) => {
            let reused = state
                .0
                .lock()
                .map(|g| {
                    g.as_ref()
                        .map(|h| h.reused.load(std::sync::atomic::Ordering::SeqCst))
                        .unwrap_or(false)
                })
                .unwrap_or(false);
            if let Some(win) = app.get_webview_window("main") {
                let _ = navigate_main_to(&win, instance.port);
            } else {
                eprintln!("[helper] 主窗口不存在，跳过 navigate");
            }
            if reused {
                println!(
                    "[helper] 已复用 127.0.0.1:{} 的同服务实例（不拉起、不 SIGTERM）",
                    instance.port
                );
            } else {
                println!(
                    "[helper] 本壳拉起的 helper 已就绪 127.0.0.1:{} instance_id={:?}",
                    instance.port, instance.instance_id
                );
            }
            Ok(())
        }
        Ok(None) => {
            // 复用路径但 instance 字段为空；不再拉起
            if let Some(win) = app.get_webview_window("main") {
                let _ = navigate_main_to(&win, helper::DEFAULT_PORT);
            }
            Ok(())
        }
        Err(err) => {
            // 检查 helper 是否让位（same-service-discovered）或端口耗尽
            let event = {
                let guard = state.0.lock().map_err(|_| "helper 句柄锁中毒".to_string())?;
                match guard.as_ref() {
                    Some(h) => h.wait_with_events().ok(),
                    None => None,
                }
            };
            match event {
                Some(HelperEvent::SameServiceDiscovered { port, .. }) => {
                    if let Some(win) = app.get_webview_window("main") {
                        let _ = navigate_main_to(&win, port);
                    }
                    Ok(())
                }
                // ISS-059：ExhaustedInfo 已由 wait_with_events 记入句柄，
                // helper_status / helper_retry 据此返回 state=exhausted。
                Some(HelperEvent::PortsExhausted { .. }) => Err("ports-exhausted".to_string()),
                _ => Err(err),
            }
        }
    }
}

fn show_main(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
    }
}

/// 退出路径统一回收（ISS-057）：take() 取走 ``HelperState`` 里的句柄后调用
/// ``stop()``，保证幂等——tray 菜单退出、``RunEvent::ExitRequested`` 与
/// ``RunEvent::Exit`` 可能依次触发，第一次 take 后后续调用拿到 ``None``
/// 直接返回，不会对同一 helper 重复发信号。
///
/// 既有三条语义由 ``HelperHandle::stop`` 自身保证，此处不绕过：
/// a) 复用模式（reused=true，helper 为外部/他实例持有）不发信号——「零击杀」
///    指外部/他实例进程；
/// b) 对本壳自己拉起的子进程：SIGTERM 后 bounded 等待（``STOP_TIMEOUT_S``，
///    10s 上限），超时后 ``kill -KILL`` 兜底属设计内（不让本壳因 helper
///    卡死而无法退出，会留下孤儿进程）；
/// c) 只向本壳 spawn 的子进程发信号（不向任何外部 PID 发信号）。
///
/// ISS-060 (d)：stop() 的错误（如 SIGKILL 兜底后进程仍未释放、wait 锁
/// 中毒等）不再静默丢弃——通过 ``eprintln`` 落日志，便于事后排查。
fn reap_spawned_helper(app: &AppHandle) {
    let Some(state) = app.try_state::<HelperState>() else {
        return;
    };
    let Ok(mut guard) = state.0.lock() else {
        eprintln!("[helper] reap_spawned_helper: HelperState 锁中毒，跳过本轮回收");
        return;
    };
    let Some(handle) = guard.take() else {
        return;
    };
    if let Err(err) = handle.stop() {
        eprintln!("[helper] reap_spawned_helper: helper.stop() 失败：{err}");
    }
}

/// tray 退出流程：先 SIGTERM 回收本壳拉起的 helper，再退出 app。
fn quit_with_helper(app: &AppHandle) {
    reap_spawned_helper(app);
    app.exit(0);
}

/// ISS-068：本壳依赖的 Tauri 插件名清单，供测试断言**名字合同**。
///
/// 重要边界（勿高估本常量）：它是**手写**的，与 `run()` 中真实的
/// `.plugin(tauri_plugin_opener::init())` 调用**没有强制关联**。删掉
/// `.plugin()` 只改变运行时行为，本常量仍在、测试照过（2026-09-17 实测：
/// 删注册后 `cargo test opener_plugin_name_matches` 仍 1 passed）。
/// 因此它**不是**注册护栏，只断言「上游 tauri-plugin-opener 的
/// `Plugin::name()` == 前端命令前缀 "opener"」这一跨仓库名字合同。
///
/// 真正的注册护栏是 `scripts/ci_tauri_opener_registered.sh` 的源码正则检查
/// （见该脚本头）；tauri-build 的 ACL 产物**无法**观察 `.plugin()`。
const REGISTERED_PLUGIN_NAMES: &[&str] = &["opener"];

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // ISS-068：设置页「打开系统设置」走 plugin:opener|open_url 打开
        // x-apple.systempreferences: 隐私深链。此前 Cargo.toml 已依赖
        // tauri-plugin-opener，但 Builder 未注册插件、capability 未授权，
        // 前端调用必被 ACL 拒绝。插件前缀 opener 由前端命令名
        // plugin:opener|open_url 决定，不能改成 fathom。
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            update_tray_status,
            helper_status,
            helper_retry,
            autostart::autostart_status,
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // macOS 菜单栏应用惯例：点关闭只隐藏窗口，tray 常驻
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .setup(|app| {
            // helper 句柄先初始化（runtime_dir 用环境变量或默认）
            let runtime_dir: PathBuf = std::env::var("FATHOM_RUNTIME_DIR")
                .ok()
                .map(PathBuf::from)
                .unwrap_or_else(default_runtime_dir);
            app.manage(HelperState(Mutex::new(Some(HelperHandle::new(runtime_dir)))));

            let status = MenuItem::with_id(app, "status", "Fathom 启动中…", false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "打开主界面", true, None::<&str>)?;
            let scan = MenuItem::with_id(app, "scan", "立即扫描…", true, None::<&str>)?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "退出 Fathom", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&status, &sep1, &open, &scan, &sep2, &quit])?;

            app.manage(TrayStatusMenu(status));

            // 图标、菜单、事件、状态推送都绑定此唯一 tray。
            let _tray = TrayIconBuilder::with_id("sentinel")
                .icon(tauri::include_image!("icons/tray.png"))
                .icon_as_template(true)
                .menu(&menu)
                .show_menu_on_left_click(false)
                .title("…")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "scan" => {
                        show_main(app);
                        // 前端监听该事件并触发 POST /api/scan（浏览器独立打开时无此通道，静默忽略）
                        let _ = app.emit("tray-action", "scan");
                    }
                    "quit" => quit_with_helper(app),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;

            // 启动 helper 并握手；失败也不阻塞 setup，由前端握手页显示
            let handle = app.handle();
            let state = handle.state::<HelperState>();
            if let Err(err) = start_helper_internal(handle, &state) {
                eprintln!("[helper] 启动/握手失败：{} — 握手页会显示诊断", err);
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Fathom 桌面壳启动失败")
        // ISS-057：全局退出钩子兜底非 tray 退出路径（Cmd+Q / AppleScript
        // quit / 系统注销等）。tray「退出」先走 quit_with_helper，随后
        // app.exit(0) 也会触发 ExitRequested——take() 幂等保证只回收一次。
        // 复用模式 stop() 不发信号；bounded 等待 10s 不阻塞退出。
        .run(|app, event| match event {
            RunEvent::ExitRequested { .. } => reap_spawned_helper(app),
            RunEvent::Exit => reap_spawned_helper(app),
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    use helper::ExhaustedInfo;
    use tauri::plugin::Plugin;

    /// ISS-068：断言**名字合同**（非注册护栏）——上游 tauri-plugin-opener 的
    /// `Plugin::name()` 必须等于前端命令前缀 `plugin:opener|open_url` 中的
    /// `opener`。名字若漂移，capability 的 `opener:allow-open-url` 会与实际
    /// 注册键错位、深链静默失败。绑定真实插件实例，不是字面量自证。
    ///
    /// **本测试抓不到「`.plugin()` 被删除」**：REGISTERED_PLUGIN_NAMES 是手写
    /// 常量，删注册调用后本测试仍通过（2026-09-17 实测 1 passed）。注册护栏
    /// 见 scripts/ci_tauri_opener_registered.sh。
    #[test]
    fn opener_plugin_name_matches_frontend_command_prefix() {
        let plugin = tauri_plugin_opener::init::<tauri::Wry>();
        assert_eq!(plugin.name(), "opener");
        assert!(
            REGISTERED_PLUGIN_NAMES.contains(&plugin.name()),
            "名字合同漂移：上游插件名 {:?} 不在本壳清单 {:?}，capability 的 \
             opener:allow-open-url 会与注册键错位，前端 plugin:{:?}|open_url 静默失败",
            plugin.name(),
            REGISTERED_PLUGIN_NAMES,
            plugin.name()
        );
    }

    /// ISS-059：给定 PortsExhausted 事件还原的信息 → 状态 JSON 的
    /// state==exhausted 且 recovery 结构完整（候选端口 + 占用 pid + 提示文案）。
    #[test]
    fn retry_error_json_reports_exhausted_with_recovery() {
        let blocked = serde_json::json!([
            {"port": 7953, "reason": "occupied-by-unknown", "probe": "refused", "occupied_pid": [111]},
            {"port": 7954, "reason": "occupied-by-unknown", "probe": "refused", "occupied_pid": []}
        ]);
        let info = ExhaustedInfo::from_event(
            7952,
            &[7952, 7953, 7954, 7955, 7956],
            &blocked,
            &None,
        );
        let json = retry_error_json(
            "ports-exhausted",
            Some(info),
            Path::new("/tmp/fathom-rt"),
        );
        assert_eq!(json["state"], "exhausted");
        let ports = json["recovery"]["ports"].as_array().unwrap();
        assert_eq!(ports.len(), 5, "recovery.ports 覆盖全部候选端口");
        assert_eq!(ports[1]["occupied_pid"], 111);
        assert!(ports[0]["occupied_pid"].is_null(), "blocked 未覆盖的候选为 null");
        assert!(
            !json["recovery"]["hint"].as_str().unwrap().is_empty(),
            "hint 兜底文案非空"
        );
    }

    /// ISS-059：普通 Err（无耗尽信息）必须仍是 state=error，不得误报 exhausted。
    #[test]
    fn retry_error_json_without_exhausted_info_stays_error() {
        let json = retry_error_json(
            "helper 握手超时（20s）",
            None,
            Path::new("/tmp/fathom-rt"),
        );
        assert_eq!(json["state"], "error");
        assert_eq!(json["error"], "helper 握手超时（20s）");
    }
}
