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
//! - 应用内更新（ISS-040B）：updater/process 两插件 + 三个协调命令，全程不静默
//!   ——检查（手动或启动延迟 ≥10s 的状态提示）、下载安装、重启三步各自经
//!   设置页确认层；未配置/不可达是两个明确的可恢复失败态，不伪装成功。
//!
//! 更新相关新 crate 仅 tauri-plugin-updater / tauri-plugin-process 两个
//! （ISS-040B 合同边界，版本锁进 Cargo.lock）；helper 进程操作全用
//! ``std::process::Command`` 与 ``std`` 文件 I/O。

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::{
    menu::{Menu, MenuItem, MenuItemKind, PredefinedMenuItem, WINDOW_SUBMENU_ID},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Listener, Manager, RunEvent, State, Url, WebviewWindow, WindowEvent,
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

/// ISS-077：把壳认领到的裸 Esc 转发为页面合成 ``keydown``。println 行是
/// 实机日志锚点——PM 前台按 Esc 时可在壳日志 grep ``[esc-forward]`` 复核
/// 「菜单认领 + 转发」确实发生（页面侧效果仍需前台实机验证）。
fn forward_escape_to_page(app: &AppHandle) {
    let Some(win) = app.get_webview_window("main") else {
        eprintln!("[esc-forward] 主窗口不存在，本次 Esc 转发跳过");
        return;
    };
    println!("[esc-forward] 菜单认领裸 Esc，转发合成 keydown 到主窗口");
    if let Err(err) = win.eval(ESC_FORWARD_JS) {
        eprintln!("[esc-forward] eval 注入失败：{err}");
    }
}

/// ISS-077：向默认菜单的 Window 子菜单挂载「关闭详情面板（Esc）」转发项。
/// 失败不阻塞启动（应用可用性优先于键盘修复），但打 loud 日志；挂载合同由
/// ``scripts/verify_tauri_esc_delivery.sh`` 静态断言 + PM 实机复核兜底。
/// 仅 macOS 需要此转发（WebView2/GTK 裸 Esc 正常送达页面）。
#[cfg(target_os = "macos")]
fn attach_escape_forward_menu_item(app: &AppHandle) {
    let item = MenuItem::with_id(
        app,
        ESC_FORWARD_MENU_ID,
        "关闭详情面板",
        true,
        Some(ESC_FORWARD_ACCELERATOR),
    );
    let item = match item {
        Ok(item) => item,
        Err(err) => {
            eprintln!("[esc-forward] 菜单项构造失败，Esc 转发未挂载（Esc 维持不送达）：{err}");
            return;
        }
    };
    let Some(menu) = app.menu() else {
        eprintln!("[esc-forward] 应用菜单不存在（macOS 默认菜单应已初始化），Esc 转发未挂载");
        return;
    };
    let Some(MenuItemKind::Submenu(window_menu)) = menu.get(WINDOW_SUBMENU_ID) else {
        eprintln!("[esc-forward] 默认菜单缺 Window 子菜单（上游 id 漂移？），Esc 转发未挂载");
        return;
    };
    if let Err(err) = window_menu.append(&item) {
        eprintln!("[esc-forward] Esc 转发菜单项 append 失败：{err}");
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

/// ISS-068 起本壳依赖的 Tauri 插件名清单，供测试断言**名字合同**
/// （ISS-040B 加入 updater/process 两个）。
///
/// 重要边界（勿高估本常量）：它是**手写**的，与 `run()` 中真实的
/// `.plugin(...)` 调用**没有强制关联**。删掉 `.plugin()` 只改变运行时行为，
/// 本常量仍在、测试照过（2026-09-17 实测：删 opener 注册后
/// `cargo test opener_plugin_name_matches` 仍 1 passed）。因此它**不是**注册
/// 护栏，只断言「上游插件的 `Plugin::name()` == 前端命令前缀」这一跨仓库
/// 名字合同（updater/process 的前端命令前缀 plugin:updater|* /
/// plugin:process|* 分别对应 "updater" / "process"）。
///
/// 真正的 opener 注册护栏是 `scripts/ci_tauri_opener_registered.sh` 的源码
/// 正则检查（见该脚本头）；tauri-build 的 ACL 产物**无法**观察 `.plugin()`。
const REGISTERED_PLUGIN_NAMES: &[&str] = &["opener", "updater", "process"];

/// ISS-077：壳层 Esc 转发菜单项 id。AppKit 对裸 Esc（无修饰）走键等价分发
/// （视图树 ``performKeyEquivalent:`` → 主菜单）且先于 ``keyDown:``；macOS
/// WKWebView 壳在该路径上不把 Esc 送达页面（ISS-028 实机证据：Tab/Enter/
/// 方向键均送达、Esc 无 keydown；同页 Web/Playwright 下 Esc 可用）。挂一个
/// 无修饰 Esc 加速键菜单项让主菜单认领裸 Esc，再转发给页面。
const ESC_FORWARD_MENU_ID: &str = "esc-forward";

/// ISS-077：加速键字符串。muda 0.19.3 解析 "Escape" 为无修饰 Esc 加速键
/// （accelerator.rs "ESCAPE" | "ESC"，NSMenuItem keyEquivalent "\\u{1b}" +
/// 空 modifier mask，只匹配裸 Esc，带修饰键不拦截）。
const ESC_FORWARD_ACCELERATOR: &str = "Escape";

/// ISS-077：转发载荷——向页面投递合成 ``keydown``（Escape）。投递目标为
/// ``document.activeElement``（兜底 ``document``），``bubbles`` 使 document 级
/// 监听收到——复用前端既有全局 Esc 处理（changes.js 关闭目录详情侧栏），
/// 前端零改动、零双绑定。菜单认领后原生 ``keyDown:`` 不再分发，无双重投递。
const ESC_FORWARD_JS: &str = r#"(function () {
  var target = document.activeElement || document.body || document;
  target.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", code: "Escape", keyCode: 27, which: 27,
    bubbles: true, cancelable: true, composed: true
  }));
})();"#;

// ===== 应用内更新协调（ISS-040B）=====
//
// 边界（ISS-040 父卡 + 040B 合同）：
// - 全程不静默：检查由用户点击或启动后延迟（≥10s）触发；下载安装必须经设置页
//   确认层（confirmed=true）；重启是独立确认；启动延迟检查只 emit 状态事件，
//   不弹窗、不下载、不安装。
// - 更新动作只在可信 Rust 壳内执行：前端经本壳三个命令（updater_check /
//   updater_install / updater_restart）协调，不经 plugin:updater|* 直调插件
//   命令——回环远程页面（127.0.0.1:7952 仪表盘）因此不获得宽泛 updater 权限。
// - 当前 endpoint 为 RFC 保留域 updates.invalid（永不解析）＝「生产更新源关闭」
//   语义；运行态检查得到 state=unreachable 的明确可恢复失败态，由发行侧
//   （ISS-041/G10）替换真实 HTTPS 源后才可能 available。
// - 验签由 tauri-plugin-updater 强制（minisign，公钥来自 tauri.conf.json
//   plugins.updater.pubkey，不可关闭）；私钥只在 CI/PM 发行时经
//   TAURI_SIGNING_PRIVATE_KEY 注入，绝不入仓库/日志/产物。
// - 失败全部回落可恢复态（JSON 状态或 {ok:false,error}），不 panic。

/// 启动延迟检查的等待秒数。合同 ≥10s；取 15s 避开启动期 helper 握手与
/// 首屏渲染的资源竞争。期间用户仍可手动检查（互不排队，后到者覆盖状态行）。
const UPDATER_STARTUP_DELAY_S: u64 = 15;

/// 手动/延迟检查的请求超时上界（秒）：防挂死 endpoint 让确认层永远转圈。
const UPDATER_CHECK_TIMEOUT_S: u64 = 30;

/// 更新状态事件名：启动延迟检查的结果只经此事件通知前端（设置页状态行），
/// 不弹窗。手动检查的返回值直接给 invoke 调用方，不走事件。
const UPDATER_EVENT: &str = "updater-state";

/// 可安装候选暂存：updater_check 发现 available 时写入；updater_install
/// （经确认层）取出执行；安装成功后清空，失败保留供重试。
struct UpdaterState(Mutex<Option<tauri_plugin_updater::Update>>);

/// 检查结果状态机的纯枚举。unconfigured 与 unreachable 是两个**各自明确**的
/// 失败态：前者是「本壳没有更新配置」（部署/构建缺陷），后者是「源不可达」
/// （当前占位 endpoint 的预期运行态），用户话术与恢复路径不同，不得合并。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum UpdaterCheckState {
    /// 未配置：无 endpoints（updater 插件未接线或配置被剥离）。
    Unconfigured,
    /// 不可达：网络/DNS/HTTP 传输层失败（updates.invalid 的预期态）。
    Unreachable,
    /// 已是最新（无更新公告，或公告版本不高于当前）。
    UpToDate,
    /// 有可用更新（版本高于当前）。
    Available,
    /// 其余失败（清单解析、签名配置、序列化等）。
    Failed,
}

impl UpdaterCheckState {
    /// 前端合同字符串（settings.js 消费同名 state 字段）。
    fn as_str(self) -> &'static str {
        match self {
            UpdaterCheckState::Unconfigured => "unconfigured",
            UpdaterCheckState::Unreachable => "unreachable",
            UpdaterCheckState::UpToDate => "up_to_date",
            UpdaterCheckState::Available => "available",
            UpdaterCheckState::Failed => "failed",
        }
    }
}

/// 插件错误的粗分类（纯 match 适配层；单测直接构造变体覆盖）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum UpdaterErrorKind {
    /// 配置缺失（无 endpoints）。
    Unconfigured,
    /// 网络/DNS/HTTP 传输层失败。
    Network,
    /// 其余（清单解析、签名配置、序列化等）。
    Other,
}

fn updater_error_kind(err: &tauri_plugin_updater::Error) -> UpdaterErrorKind {
    use tauri_plugin_updater::Error;
    match err {
        Error::EmptyEndpoints => UpdaterErrorKind::Unconfigured,
        Error::Reqwest(_) | Error::Network(_) => UpdaterErrorKind::Network,
        _ => UpdaterErrorKind::Other,
    }
}

/// 点分版本比较（std 实现，不引 semver crate）：逐段优先按数字比较，
/// 非数字段退化为不区分大小写的字典序；段数不等时短者补 "0"。
/// 0.10.0 > 0.9.0（数字语义，非字典序）。仅作状态映射的防御层——
/// 「是否有更新」的权威判定在上游 updater 的 semver 比较里。
fn version_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let a_segs: Vec<&str> = a.split('.').collect();
    let b_segs: Vec<&str> = b.split('.').collect();
    let n = a_segs.len().max(b_segs.len());
    for i in 0..n {
        let x = a_segs.get(i).copied().unwrap_or("0");
        let y = b_segs.get(i).copied().unwrap_or("0");
        match (x.parse::<u64>(), y.parse::<u64>()) {
            (Ok(nx), Ok(ny)) => match nx.cmp(&ny) {
                Ordering::Equal => continue,
                ord => return ord,
            },
            _ => {
                let ord = x.to_lowercase().cmp(&y.to_lowercase());
                if ord != Ordering::Equal {
                    return ord;
                }
            }
        }
    }
    Ordering::Equal
}

/// 纯映射①（版本分支）：远端版本高于当前 → Available；无远端、相等或更低
/// → UpToDate。上游 check() 已做版本比较（不高于当前时返回 None），本函数是
/// 状态 JSON 化前的第二道防御，并把「相等/更低」明确归入 up_to_date 而非
/// failed——公告版本不高于当前不是错误，是无可安装。
fn updater_state_from_versions(current: &str, remote: Option<&str>) -> UpdaterCheckState {
    match remote {
        Some(v) if version_cmp(v, current) == std::cmp::Ordering::Greater => {
            UpdaterCheckState::Available
        }
        _ => UpdaterCheckState::UpToDate,
    }
}

/// 纯映射②（错误分支）：unconfigured / unreachable / failed 三分支各自成态
/// （区分理由见 UpdaterCheckState 文档）。
fn updater_state_from_error(kind: UpdaterErrorKind) -> UpdaterCheckState {
    match kind {
        UpdaterErrorKind::Unconfigured => UpdaterCheckState::Unconfigured,
        UpdaterErrorKind::Network => UpdaterCheckState::Unreachable,
        UpdaterErrorKind::Other => UpdaterCheckState::Failed,
    }
}

/// 状态机 → 前端合同 JSON（与 helper_status 同风格：结构化、可恢复、
/// 不伪装成功）。available 携带 available_version/notes；失败态携带 error。
fn updater_status_json(
    state: UpdaterCheckState,
    current_version: &str,
    available_version: Option<&str>,
    notes: Option<&str>,
    error: Option<&str>,
) -> serde_json::Value {
    let mut value = serde_json::json!({
        "state": state.as_str(),
        "current_version": current_version,
    });
    let obj = value.as_object_mut().expect("json! 宏产物必为对象");
    if let Some(v) = available_version {
        obj.insert("available_version".to_string(), serde_json::json!(v));
    }
    if let Some(n) = notes {
        if !n.trim().is_empty() {
            obj.insert("notes".to_string(), serde_json::json!(n));
        }
    }
    if let Some(e) = error {
        obj.insert("error".to_string(), serde_json::json!(e));
    }
    value
}

/// 执行一次更新检查（手动与启动延迟共用；不弹窗、不安装）。
/// 所有失败都映射为结构化状态 JSON——unconfigured/unreachable/failed 都是
/// 可恢复态，绝不 panic、不伪装成功。
async fn perform_updater_check(
    app: &AppHandle,
    store: Option<&UpdaterState>,
) -> serde_json::Value {
    use tauri_plugin_updater::UpdaterExt;

    let current = app.package_info().version.to_string();
    let updater = match app
        .updater_builder()
        .timeout(std::time::Duration::from_secs(UPDATER_CHECK_TIMEOUT_S))
        .build()
    {
        Ok(u) => u,
        Err(err) => {
            let state = updater_state_from_error(updater_error_kind(&err));
            return updater_status_json(state, &current, None, None, Some(&err.to_string()));
        }
    };
    match updater.check().await {
        Ok(Some(update)) => {
            let state = updater_state_from_versions(&current, Some(&update.version));
            if state == UpdaterCheckState::Available {
                // 暂存候选供 updater_install（经确认层）取用；锁中毒时仍返回
                // available（安装命令会给出「请重新检查」的可恢复错误）。
                if let Some(store) = store {
                    if let Ok(mut guard) = store.0.lock() {
                        *guard = Some(update.clone());
                    }
                }
                return updater_status_json(
                    state,
                    &current,
                    Some(&update.version),
                    update.body.as_deref(),
                    None,
                );
            }
            // 上游公告了不高于当前的版本：按 up_to_date 呈现，不暂存候选。
            updater_status_json(state, &current, None, None, None)
        }
        Ok(None) => updater_status_json(UpdaterCheckState::UpToDate, &current, None, None, None),
        Err(err) => {
            let state = updater_state_from_error(updater_error_kind(&err));
            updater_status_json(state, &current, None, None, Some(&err.to_string()))
        }
    }
}

/// 手动检查更新（设置页「检查更新」按钮）。永不返回 Err：所有失败都映射为
/// 结构化状态 JSON，前端据此渲染可恢复态。
#[tauri::command]
async fn updater_check(
    app: AppHandle,
    state: State<'_, UpdaterState>,
) -> Result<serde_json::Value, String> {
    Ok(perform_updater_check(&app, Some(state.inner())).await)
}

/// 下载并安装当前候选（仅经设置页确认层调用：confirmed=true 才动手）。
/// ISS-040C 六步接线（协议状态在冻结 helper 的 upgrade-* 子命令，见上方
/// 模块段）：确认后先 upgrade-prepare（①停写→②旧 helper 退出→③一致备份
/// →④journal；在途扫描→明确拒绝、绝不终止），再⑤download_and_install——
/// 拆为 download（进度映射 UPDATER_EVENT downloading 含 downloaded/total、
/// 下载阶段可取消）与 install（不可取消且文案明确），下载与验签都由
/// tauri-plugin-updater 完成（minisign 验签不可关闭）；随后新 helper 身份
/// 核验，⑥成功经 upgrade-finalize 清 journal、清空候选——重启仍走
/// updater_restart 独立确认（不静默、不自动重启）。任一步失败经
/// upgrade-rollback 回滚（旧 helper 恢复运行、旧数据不动、候选保留、
/// journal 清除）并返回 {ok:false,...} 可恢复错误，绝不 panic。
#[tauri::command]
async fn updater_install(
    app: AppHandle,
    state: State<'_, UpdaterState>,
    ctl: State<'_, UpdaterInstallCtl>,
    confirmed: bool,
) -> Result<serde_json::Value, String> {
    if !confirmed {
        return Ok(serde_json::json!({
            "ok": false,
            "error": "updater_install 需经用户确认（confirmed=true）；本壳不做静默安装",
        }));
    }
    let candidate = state
        .0
        .lock()
        .map_err(|_| "更新候选锁中毒".to_string())?
        .clone();
    let Some(update) = candidate else {
        return Ok(serde_json::json!({
            "ok": false,
            "error": "没有已确认的可用更新；请先「检查更新」",
        }));
    };

    // 协议①-④的 Python 侧入口 = 同一冻结 helper（生产 CLI main.py）。
    let runtime_dir = {
        let helper_state = app.state::<HelperState>();
        let resolved = match helper_state.0.lock() {
            Ok(guard) => match guard.as_ref() {
                Some(handle) => handle.runtime_dir.clone(),
                None => helper::default_runtime_dir(),
            },
            Err(_) => helper::default_runtime_dir(),
        };
        resolved
    };
    let helper_bin = match locate_helper(&app) {
        Ok(bin) => bin,
        Err(err) => {
            return Ok(serde_json::json!({
                "ok": false,
                "state": "failed",
                "error": format!("无法定位冻结 helper，升级未开始：{err}"),
            }));
        }
    };

    ctl.begin(UpdaterInstallPhase::Preparing);
    let _ = app.emit(
        UPDATER_EVENT,
        serde_json::json!({
            "state": "preparing",
            "current_version": update.current_version,
            "available_version": update.version,
        }),
    );

    // ①-④ prepare（经冻结 helper 子命令；协议失败已在 Python 侧回滚）。
    let prepare = match run_upgrade_phase(
        &helper_bin,
        &runtime_dir,
        &[
            "upgrade-prepare",
            "--from",
            &update.current_version,
            "--to",
            &update.version,
        ],
    ) {
        Ok(payload) => payload,
        Err(err) => serde_json::json!({ "ok": false, "kind": "internal", "error": err }),
    };
    if !prepare.get("ok").and_then(|value| value.as_bool()).unwrap_or(false) {
        let kind = prepare
            .get("kind")
            .and_then(|value| value.as_str())
            .unwrap_or("internal")
            .to_string();
        let error = prepare
            .get("error")
            .and_then(|value| value.as_str())
            .unwrap_or("未知失败")
            .to_string();
        // prepare 已自回滚；壳侧（进程属主）补齐旧 helper 重启。
        rollback_upgrade_and_restart_helper(&app, &helper_bin, &runtime_dir, "prepare 失败");
        let hint = prepare_failure_hint(&kind);
        let _ = app.emit(
            UPDATER_EVENT,
            serde_json::json!({
                "state": "failed",
                "kind": kind,
                "error": error,
                "hint": hint,
            }),
        );
        return Ok(serde_json::json!({
            "ok": false,
            "state": "failed",
            "kind": kind,
            "error": format!("{error}（{hint}）"),
        }));
    }

    // ⑤ download_and_install 前半（download 内完成下载+minisign 验签）：
    // 进度映射 UPDATER_EVENT（downloading 含 downloaded/total，节流）；取消
    // 请求在 poll 边界受理——中止即回滚，候选保留。
    ctl.begin(UpdaterInstallPhase::Downloading);
    let mut downloaded: u64 = 0;
    let mut last_emitted: u64 = 0;
    let emit_app = app.clone();
    let current_version = update.current_version.clone();
    let target_version = update.version.clone();
    let ctl_ref = ctl.inner();
    let mut download = std::pin::pin!(update.download(
        move |chunk, total| {
            downloaded = downloaded.saturating_add(chunk as u64);
            if progress_should_emit(last_emitted, downloaded, total) {
                last_emitted = downloaded;
                let _ = emit_app.emit(
                    UPDATER_EVENT,
                    updater_download_progress_json(
                        &current_version,
                        &target_version,
                        downloaded,
                        total,
                    ),
                );
            }
        },
        || {},
    ));
    enum DownloadOutcome {
        Completed(Vec<u8>),
        Cancelled,
        Failed(String),
    }
    use std::future::Future as _;
    let outcome = std::future::poll_fn(|cx| {
        if ctl_ref.is_cancel_requested() {
            return std::task::Poll::Ready(DownloadOutcome::Cancelled);
        }
        match download.as_mut().poll(cx) {
            std::task::Poll::Ready(Ok(bytes)) => {
                std::task::Poll::Ready(DownloadOutcome::Completed(bytes))
            }
            std::task::Poll::Ready(Err(err)) => {
                std::task::Poll::Ready(DownloadOutcome::Failed(err.to_string()))
            }
            std::task::Poll::Pending => std::task::Poll::Pending,
        }
    })
    .await;

    let bytes = match outcome {
        DownloadOutcome::Cancelled => {
            rollback_upgrade_and_restart_helper(&app, &helper_bin, &runtime_dir, "下载取消");
            let _ = app.emit(
                UPDATER_EVENT,
                serde_json::json!({
                    "state": "cancelled",
                    "current_version": update.current_version,
                    "available_version": update.version,
                    "hint": "下载已取消；旧版本保持运行，可再次安装",
                }),
            );
            return Ok(serde_json::json!({
                "ok": false,
                "state": "cancelled",
                "error": "下载已取消（旧版本保持运行，候选保留）",
            }));
        }
        DownloadOutcome::Failed(err) => {
            rollback_upgrade_and_restart_helper(&app, &helper_bin, &runtime_dir, "下载失败");
            let error = format!("下载/验签失败（已回滚，候选保留，可重试）：{err}");
            let _ = app.emit(
                UPDATER_EVENT,
                serde_json::json!({ "state": "failed", "error": error }),
            );
            return Ok(serde_json::json!({
                "ok": false,
                "state": "failed",
                "error": error,
            }));
        }
        DownloadOutcome::Completed(bytes) => bytes,
    };

    // 进入安装（download_and_install 后半）：不可取消，文案明确（合同⑤）。
    ctl.begin(UpdaterInstallPhase::Installing);
    let _ = app.emit(
        UPDATER_EVENT,
        serde_json::json!({
            "state": "installing",
            "current_version": update.current_version,
            "available_version": update.version,
            "cancellable": false,
            "hint": UPDATER_INSTALL_NOT_CANCELLABLE_HINT,
        }),
    );
    if let Err(err) = update.install(&bytes) {
        rollback_upgrade_and_restart_helper(&app, &helper_bin, &runtime_dir, "安装失败");
        let error = format!("安装失败（已回滚到旧版本，候选保留，可重试）：{err}");
        let _ = app.emit(
            UPDATER_EVENT,
            serde_json::json!({ "state": "failed", "error": error }),
        );
        return Ok(serde_json::json!({
            "ok": false,
            "state": "failed",
            "error": error,
        }));
    }

    // 新 helper 身份核验（030A step6 握手语义的壳侧生产化）：安装后 bundle
    // 已是 N+1，运行其 --version 与候选版本核对；失败→journal 判定路径回滚。
    ctl.begin(UpdaterInstallPhase::Verifying);
    let verify = match locate_helper(&app) {
        Ok(new_bin) => verify_new_helper(&new_bin, &update.version),
        Err(err) => Err(format!("无法定位新 helper（身份核验失败）：{err}")),
    };
    if let Err(err) = verify {
        rollback_upgrade_and_restart_helper(
            &app,
            &helper_bin,
            &runtime_dir,
            "新 helper 握手失败",
        );
        let error = format!("{err}（已回滚到可运行旧版，旧数据不动）");
        let _ = app.emit(
            UPDATER_EVENT,
            serde_json::json!({
                "state": "failed",
                "kind": "handshake",
                "error": error,
            }),
        );
        return Ok(serde_json::json!({
            "ok": false,
            "state": "failed",
            "kind": "handshake",
            "error": error,
        }));
    }

    // ⑥成功收尾：清 journal（候选清空；重启仍走 updater_restart 独立确认，
    // 不静默、不自动重启）。
    ctl.begin(UpdaterInstallPhase::Finalizing);
    if let Err(err) = run_upgrade_phase(&helper_bin, &runtime_dir, &["upgrade-finalize"]) {
        // 安装已成功：finalize 失败不回滚安装；半升级态可检测（upgrade-detect
        // / upgrade-rollback 可恢复），如实落日志不伪装成功收尾。
        eprintln!(
            "[updater] upgrade-finalize 失败（journal 可能残留，可经 upgrade-detect 检测恢复）：{err}"
        );
    }
    if let Ok(mut guard) = state.0.lock() {
        *guard = None;
    }
    let _ = app.emit(
        UPDATER_EVENT,
        serde_json::json!({
            "state": "installed",
            "current_version": update.current_version,
            "available_version": update.version,
            "hint": "更新已安装；重启应用后生效",
        }),
    );
    Ok(serde_json::json!({
        "ok": true,
        "state": "installed",
        "current_version": update.current_version,
        "available_version": update.version,
    }))
}

/// 重启应用以完成更新（独立命令，仅经「重启以完成」确认层调用）。
/// 经 tauri 核心 request_restart()（进程插件的 plugin:process|restart 即同一
/// 入口）；触发的 RunEvent::ExitRequested/Exit 会走既有幂等 helper 回收，
/// 不留孤儿进程。返回值可能因进程退出而未被前端收到——确认层以发出为准。
#[tauri::command]
fn updater_restart(app: AppHandle, confirmed: bool) -> Result<serde_json::Value, String> {
    if !confirmed {
        return Ok(serde_json::json!({
            "ok": false,
            "error": "重启需经用户确认（confirmed=true）；未重启前保持当前版本运行",
        }));
    }
    app.request_restart();
    Ok(serde_json::json!({ "ok": true }))
}

// ===== 生产升级协调接线（ISS-040C）=====
//
// 六步合同（Python 侧协议状态见 fathom/upgrade.py，与本壳分工）：
// ①非阻塞停写（ScanLease）——被在途扫描持有→明确拒绝，绝不终止在途扫描；
// ②旧 helper 优雅退出（helper-instance pid + 端口释放确认，超时→中止→回滚）；
// ③SQLite 一致备份（checkpoint + backup API + 完整性校验，禁止文件拷贝）；
// ④journal 落盘（半升级态可检测）；⑤download_and_install（本壳：进度映射
// UPDATER_EVENT downloading 含 downloaded/total；下载阶段可取消、进入安装
// 后不可取消且文案明确）；⑥成功清 journal/候选，重启仍走 updater_restart
// 独立确认；任一步失败回滚（旧 helper 恢复运行、旧数据不动、候选保留、
// journal 清除）。
//
// ①-④与⑥的协议状态经**同一冻结 helper**（生产 CLI main.py）的
// upgrade-prepare / upgrade-finalize / upgrade-rollback 子命令执行——子命令
// 随打包自然携带，不经 030A 夹具。进程重启单属主：旧 helper 的「恢复运行」
// 由本壳（进程属主）在回滚后执行，Python 侧不 spawn 进程。

/// ISS-040C：升级取消请求事件名。复用**已授权**的 ``core:event:default``
/// 通道受理前端取消（本切片不动 frontend/，按钮留后续）；不新增 updater
/// 命令/ACL 权限——恰 3 权限合同不回退。
const UPDATER_CANCEL_EVENT: &str = "updater-cancel-requested";

/// ISS-040C：进入安装后不可取消的明确文案（合同⑤）。
const UPDATER_INSTALL_NOT_CANCELLABLE_HINT: &str =
    "已进入安装阶段，不可取消；若安装失败将自动回滚到当前版本";

/// ISS-040C：取消请求到达但已过下载阶段时的拒绝文案（同样明确）。
const UPDATER_CANCEL_REFUSED_HINT: &str =
    "下载已完成、安装进行中，取消请求被拒绝；失败路径将自动回滚到当前版本";

/// 进度事件节流步长：每累计 128 KiB（或到达终点）发一次 UPDATER_EVENT，
/// 避免大包下载按 chunk 刷事件。
const UPDATER_PROGRESS_EMIT_STEP_BYTES: u64 = 128 * 1024;

/// ISS-040C：updater_install 阶段机。取消边界：准备/下载阶段可取消，
/// 进入安装（含身份核验与收尾）后不可取消。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum UpdaterInstallPhase {
    Preparing,
    Downloading,
    Installing,
    Verifying,
    Finalizing,
}

impl UpdaterInstallPhase {
    fn as_str(self) -> &'static str {
        match self {
            Self::Preparing => "preparing",
            Self::Downloading => "downloading",
            Self::Installing => "installing",
            Self::Verifying => "verifying",
            Self::Finalizing => "finalizing",
        }
    }

    /// 取消边界（合同⑤）：仅准备/下载阶段受理取消。
    fn allows_cancel(self) -> bool {
        matches!(self, Self::Preparing | Self::Downloading)
    }
}

/// ISS-040C：一次 updater_install 的控制面（当前阶段 + 取消请求）。
/// 取消经 UPDATER_CANCEL_EVENT（core:event:default）受理，不新增 ACL 权限。
struct UpdaterInstallCtl {
    phase: Mutex<UpdaterInstallPhase>,
    cancel_requested: std::sync::atomic::AtomicBool,
}

impl UpdaterInstallCtl {
    fn new() -> Self {
        Self {
            phase: Mutex::new(UpdaterInstallPhase::Preparing),
            cancel_requested: std::sync::atomic::AtomicBool::new(false),
        }
    }

    /// 进入新阶段；回到 Preparing 时复位取消标志（新一轮安装）。
    fn begin(&self, phase: UpdaterInstallPhase) {
        if let Ok(mut guard) = self.phase.lock() {
            *guard = phase;
        }
        if phase == UpdaterInstallPhase::Preparing {
            self.cancel_requested
                .store(false, std::sync::atomic::Ordering::SeqCst);
        }
    }

    fn current(&self) -> UpdaterInstallPhase {
        self.phase
            .lock()
            .map(|guard| *guard)
            .unwrap_or(UpdaterInstallPhase::Preparing)
    }

    fn request_cancel(&self) {
        self.cancel_requested
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }

    fn is_cancel_requested(&self) -> bool {
        self.cancel_requested
            .load(std::sync::atomic::Ordering::SeqCst)
    }
}

/// ISS-040C：下载进度事件载荷（合同：downloading 态含 downloaded/total；
/// total 未知时为 null，downloaded 为累计字节数）。
fn updater_download_progress_json(
    current_version: &str,
    available_version: &str,
    downloaded: u64,
    total: Option<u64>,
) -> serde_json::Value {
    serde_json::json!({
        "state": "downloading",
        "current_version": current_version,
        "available_version": available_version,
        "downloaded": downloaded,
        "total": total,
        "cancellable": true,
    })
}

/// 进度节流（纯函数）：累计满 128 KiB 或已完成（total 已知且到位）时发事件。
fn progress_should_emit(last_emitted: u64, downloaded: u64, total: Option<u64>) -> bool {
    if downloaded.saturating_sub(last_emitted) >= UPDATER_PROGRESS_EMIT_STEP_BYTES {
        return true;
    }
    matches!(total, Some(total) if downloaded >= total)
}

/// ISS-040C：构建冻结 helper 的升级协调子命令。argv 合同：全局
/// ``--runtime-dir`` 位于子命令之前（argparse 布局，与 main.py 一致）；
/// env 与 spawn_helper 同源（FATHOM_RUNTIME_DIR / FATHOM_RUNTIME_MODE）。
fn upgrade_helper_command(
    bin: &Path,
    runtime_dir: &Path,
    extra_args: &[&str],
) -> std::process::Command {
    let mut cmd = std::process::Command::new(bin);
    cmd.arg("--runtime-dir").arg(runtime_dir);
    for arg in extra_args {
        cmd.arg(arg);
    }
    cmd.env("FATHOM_RUNTIME_DIR", runtime_dir)
        .env("FATHOM_RUNTIME_MODE", "release")
        .stdin(std::process::Stdio::null());
    cmd
}

/// stdout 最后一个非空行解析为 JSON（容忍诊断行；子命令合同：结果 JSON
/// 恒为末行，诊断走 stderr）。
fn parse_json_last_line(stdout: &str) -> Option<serde_json::Value> {
    stdout
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .and_then(|line| serde_json::from_str(line).ok())
}

/// 执行冻结 helper 协调子命令并解析其单行 JSON 结果。
fn run_upgrade_phase(
    bin: &Path,
    runtime_dir: &Path,
    extra_args: &[&str],
) -> Result<serde_json::Value, String> {
    let output = upgrade_helper_command(bin, runtime_dir, extra_args)
        .output()
        .map_err(|err| format!("启动升级协调子命令失败（{}）：{err}", bin.display()))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    parse_json_last_line(&stdout).ok_or_else(|| {
        format!(
            "升级协调子命令未输出可解析 JSON（exit={:?}，stdout 尾部：{:?}，stderr 尾部：{:?}）",
            output.status.code(),
            stdout.lines().last().unwrap_or(""),
            stderr.lines().last().unwrap_or(""),
        )
    })
}

/// 运行 helper 的 ``--version``（ISS-029 单行 JSON 身份面；静态、无运行时
/// 副作用）并解析。
fn probe_helper_identity(bin: &Path) -> Result<serde_json::Value, String> {
    let output = std::process::Command::new(bin)
        .arg("--version")
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|err| format!("运行 helper --version 失败（{}）：{err}", bin.display()))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    parse_json_last_line(&stdout).ok_or_else(|| {
        format!(
            "helper --version 输出不可解析（exit={:?}，stdout 尾部：{:?}）",
            output.status.code(),
            stdout.lines().last().unwrap_or(""),
        )
    })
}

/// ISS-040C：新 helper 身份核验（030A step6 握手语义的壳侧生产化）——
/// service/protocol/version 三方一致才算握手通过。
fn verify_new_helper_identity(
    version_json: &serde_json::Value,
    expected_version: &str,
) -> Result<(), String> {
    let mut problems: Vec<String> = Vec::new();
    let service = version_json
        .get("service")
        .and_then(|value| value.as_str())
        .unwrap_or("");
    if service != helper::SERVICE_IDENTITY {
        problems.push(format!("service={service:?}"));
    }
    let protocol = version_json
        .get("protocol_version")
        .and_then(|value| value.as_i64())
        .unwrap_or(0);
    if protocol != helper::PROTOCOL_VERSION {
        problems.push(format!("protocol={protocol}"));
    }
    let version = version_json
        .get("version")
        .and_then(|value| value.as_str())
        .unwrap_or("");
    if version != expected_version {
        problems.push(format!("version={version:?} != {expected_version:?}"));
    }
    if problems.is_empty() {
        Ok(())
    } else {
        Err(format!("新 helper 握手失败：{}", problems.join("; ")))
    }
}

/// 定位 + 运行 + 核验新 helper 身份（安装后 bundle 已是 N+1）。
fn verify_new_helper(bin: &Path, expected_version: &str) -> Result<(), String> {
    let identity = probe_helper_identity(bin)?;
    verify_new_helper_identity(&identity, expected_version)
}

/// prepare 失败的用户话术（kind → 明确可读的恢复提示；scan_busy 必须带
/// 「稍后重试」，对应合同①的可读失败态）。
fn prepare_failure_hint(kind: &str) -> &'static str {
    match kind {
        "scan_busy" => "有扫描正在进行；升级未开始、未终止扫描。请等扫描完成后重试",
        "helper_exit_timeout" => "旧后台服务未在时限内退出；已回滚，稍后可重试",
        "schema_refused" => "数据库版本不可信，升级被拒绝；请先处理数据文件",
        "backup" | "disk_full" => "备份失败；旧版本与旧数据未受影响，清理空间后可重试",
        "half_upgraded_state" => "存在未收口的半升级态；请先恢复（upgrade-rollback）后重试",
        _ => "升级准备失败；旧版本与旧数据未受影响，可重试",
    }
}

/// 受理取消请求（UPDATER_CANCEL_EVENT 回调）：准备/下载阶段置取消标志
/// （下载在 poll 边界中止并回滚）；已进入安装则拒绝并回明确文案。
fn handle_cancel_request(app: &AppHandle) {
    let ctl = app.state::<UpdaterInstallCtl>();
    let phase = ctl.current();
    if phase.allows_cancel() {
        ctl.request_cancel();
        println!(
            "[updater] 收到取消请求（阶段 {}）：下载将在当前位置中止并回滚",
            phase.as_str()
        );
    } else {
        eprintln!(
            "[updater] 取消请求被拒绝（阶段 {}）：{UPDATER_CANCEL_REFUSED_HINT}",
            phase.as_str()
        );
        let _ = app.emit(
            UPDATER_EVENT,
            serde_json::json!({
                "state": phase.as_str(),
                "cancellable": false,
                "hint": UPDATER_CANCEL_REFUSED_HINT,
            }),
        );
    }
}

/// 失败回滚（合同⑥）：先经冻结 helper ``upgrade-rollback`` 清理协议状态
/// （journal 清除、旧数据不动、候选/备份保留），再由本壳（进程属主）重启
/// 旧 helper。重启在独立线程执行，不阻塞命令 future。
fn rollback_upgrade_and_restart_helper(
    app: &AppHandle,
    bin: &Path,
    runtime_dir: &Path,
    reason: &str,
) {
    match run_upgrade_phase(bin, runtime_dir, &["upgrade-rollback"]) {
        Ok(payload) => println!("[updater] 回滚完成（{reason}）：{payload}"),
        Err(err) => eprintln!(
            "[updater] 回滚子命令失败（{reason}）：{err}（journal 可能残留；\
             启动时经 upgrade-detect 可检测并恢复）"
        ),
    }
    let handle = app.clone();
    let spawned = std::thread::Builder::new()
        .name("fathom-updater-helper-restart".to_string())
        .spawn(move || {
            // 复用既有启动+握手+导航流程（start_helper_internal）恢复服务；
            // 失败不 panic：握手页「重试握手」仍是既有恢复入口。
            let state = handle.state::<HelperState>();
            if let Err(err) = start_helper_internal(&handle, &state) {
                eprintln!("[updater] 回滚后重启 helper 失败：{err}（握手页「重试握手」可手动恢复）");
            }
        });
    if let Err(err) = spawned {
        eprintln!("[updater] 回滚后重启线程创建失败：{err}（握手页「重试握手」可手动恢复）");
    }
}

/// 启动延迟检查（ISS-040B）：std 线程 + async_runtime::block_on（不引 tokio
/// 直依赖）。只 emit updater-state 状态事件，绝不弹窗/下载/安装；失败也只
/// 是状态行里的 unreachable/failed 文案，不阻塞启动（setup 即返回）。
fn spawn_updater_startup_check(app: &AppHandle) {
    let handle = app.clone();
    let spawned = std::thread::Builder::new()
        .name("fathom-updater-startup".to_string())
        .spawn(move || {
            std::thread::sleep(std::time::Duration::from_secs(UPDATER_STARTUP_DELAY_S));
            let store = handle.state::<UpdaterState>();
            let status =
                tauri::async_runtime::block_on(perform_updater_check(&handle, Some(store.inner())));
            if let Err(err) = handle.emit(UPDATER_EVENT, status) {
                eprintln!("[updater] 启动延迟检查状态事件发送失败：{err}");
            }
        });
    if let Err(err) = spawned {
        // 线程起不来不阻塞启动：手动检查入口仍在。
        eprintln!("[updater] 启动延迟检查线程创建失败（手动检查仍可用）：{err}");
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // ISS-068：设置页「打开系统设置」走 plugin:opener|open_url 打开
        // x-apple.systempreferences: 隐私深链。此前 Cargo.toml 已依赖
        // tauri-plugin-opener，但 Builder 未注册插件、capability 未授权，
        // 前端调用必被 ACL 拒绝。插件前缀 opener 由前端命令名
        // plugin:opener|open_url 决定，不能改成 fathom。
        .plugin(tauri_plugin_opener::init())
        // ISS-040B：应用内更新两插件。updater 提供 Rust 侧检查/下载/验签/安装
        // （经 UpdaterExt，不经前端直调插件命令；2.x 无自由 init()，注册入口是
        // Builder::new().build()）；process 为发行态前端 relaunch 预留（本壳
        // 重启走 updater_restart → request_restart 同一入口）。
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![
            update_tray_status,
            helper_status,
            helper_retry,
            autostart::autostart_status,
            autostart::autostart_register_plan,
            autostart::autostart_register,
            autostart::autostart_unregister,
            updater_check,
            updater_install,
            updater_restart,
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // macOS 菜单栏应用惯例：点关闭只隐藏窗口，tray 常驻
                let _ = window.hide();
                api.prevent_close();
            }
        })
        // ISS-077：应用菜单栏事件路由（tray 菜单事件走 TrayIconBuilder 自己的
        // on_menu_event，互不影响）。esc-forward = 裸 Esc 转发项被主菜单认领。
        .on_menu_event(|app, event| {
            if event.id.as_ref() == ESC_FORWARD_MENU_ID {
                forward_escape_to_page(app);
            }
        })
        .setup(|app| {
            // helper 句柄先初始化（runtime_dir 用环境变量或默认）
            let runtime_dir: PathBuf = std::env::var("FATHOM_RUNTIME_DIR")
                .ok()
                .map(PathBuf::from)
                .unwrap_or_else(default_runtime_dir);
            app.manage(HelperState(Mutex::new(Some(HelperHandle::new(runtime_dir)))));
            // ISS-040B：更新候选暂存句柄（启动延迟检查/手动检查写入）。
            app.manage(UpdaterState(Mutex::new(None)));
            // ISS-040C：升级安装控制面 + 取消请求事件通道（core:event:default
            // 已授权，不新增 updater 命令/ACL 权限——恰 3 权限合同不回退）。
            app.manage(UpdaterInstallCtl::new());
            let cancel_app = app.handle().clone();
            app.listen(UPDATER_CANCEL_EVENT, move |_| {
                handle_cancel_request(&cancel_app);
            });

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

            // ISS-077：默认菜单就绪后挂载裸 Esc 转发项（macOS WKWebView 壳
            // 不送达 Esc keydown 的壳层修复；机制见 ESC_FORWARD_MENU_ID 注释）
            #[cfg(target_os = "macos")]
            attach_escape_forward_menu_item(handle);

            // ISS-040B：启动延迟检查（≥10s，仅状态提示；见 spawn 函数文档）。
            spawn_updater_startup_check(handle);

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

    /// ISS-068 二轮：证明 capabilities 里声明的 URL scope 模式**真的能匹配**
    /// 前端实际打开的深链（settings.js 的 PREFS_DEEP_LINK）。
    ///
    /// 背景：`opener:allow-open-url` 只是**命令级**授权；上游
    /// `Scope::is_url_allowed`（tauri-plugin-opener scope.rs）在 URL 维度用
    /// `glob::Pattern::matches` 判定，空 allow 列表恒 false -> `ForbiddenUrl`。
    /// 故仅声明权限标识符不够，必须带 scope 且模式要覆盖具体 URL。
    ///
    /// 本测试用**与上游同一 crate**（glob 0.3，见 Cargo.toml dev-dependencies）
    /// 复算匹配，避免自造语义。注意 `*` 在默认 MatchOptions 下可跨 `/` 与 `?`。
    #[test]
    fn opener_url_scope_pattern_matches_frontend_deep_link() {
        // 与 capabilities/default.json 的 scope 及 settings.js 的 URL 保持一致。
        const SCOPE_PATTERN: &str = "x-apple.systempreferences:*";
        const FRONTEND_URL: &str =
            "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";

        let pat = glob::Pattern::new(SCOPE_PATTERN).expect("scope 模式本身必须合法");
        assert!(
            pat.matches(FRONTEND_URL),
            "scope 模式 {SCOPE_PATTERN:?} 不匹配前端深链 {FRONTEND_URL:?}；\
             运行期 is_url_allowed 会返回 false，open_url 被 ForbiddenUrl 拒绝"
        );
        // 负向：不能因为放宽而覆盖任意 scheme。
        assert!(
            !pat.matches("https://example.com"),
            "scope 模式过宽：不应匹配 https URL"
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

    /// ISS-077：Esc 转发载荷的名字合同——合成事件必须是 keydown + Escape
    /// （key/code/keyCode），且 bubbles（前端 document 级监听才能收到）。
    /// 与 REGISTERED_PLUGIN_NAMES 同类限制：本测试钉住载荷常量内容，抓不到
    /// 菜单项运行时是否真的挂上（那由 scripts/verify_tauri_esc_delivery.sh
    /// 静态合同 + PM 实机复核兜底）。
    #[test]
    fn esc_forward_js_payload_matches_keydown_escape_contract() {
        for marker in [
            "new KeyboardEvent(\"keydown\"",
            "key: \"Escape\"",
            "code: \"Escape\"",
            "keyCode: 27",
            "bubbles: true",
            "dispatchEvent",
        ] {
            assert!(
                ESC_FORWARD_JS.contains(marker),
                "Esc 转发载荷缺少合同片段 {marker:?}（当前载荷：{ESC_FORWARD_JS:?}）"
            );
        }
        // 加速键必须是裸 Escape（muda 0.19.3 接受 "Escape"，映射 NSMenuItem
        // keyEquivalent \\u{1b} + 空 modifier mask，只认领无修饰 Esc）。
        assert_eq!(ESC_FORWARD_ACCELERATOR, "Escape");
        assert_eq!(ESC_FORWARD_MENU_ID, "esc-forward");
    }

    /// ISS-040B：updater/process 的名字合同（同 opener 测试的限制：非注册
    /// 护栏，只钉上游 `Plugin::name()` 与前端命令前缀一致）。绑定真实插件
    /// 实例，不是字面量自证。
    #[test]
    fn updater_and_process_plugin_names_match_registration_prefixes() {
        let updater = tauri_plugin_updater::Builder::new().build::<tauri::Wry>();
        assert_eq!(updater.name(), "updater");
        let process = tauri_plugin_process::init::<tauri::Wry>();
        assert_eq!(process.name(), "process");
        for name in ["updater", "process"] {
            assert!(
                REGISTERED_PLUGIN_NAMES.contains(&name),
                "名字合同漂移：上游插件名 {name:?} 不在本壳清单 {REGISTERED_PLUGIN_NAMES:?}"
            );
        }
    }

    /// ISS-040B：版本比较分支——相等/更高/更低/数字语义（0.10 > 0.9，
    /// 非字典序）/段数补零。
    #[test]
    fn updater_version_cmp_branches() {
        use std::cmp::Ordering;
        assert_eq!(version_cmp("0.3.0", "0.3.0"), Ordering::Equal);
        assert_eq!(version_cmp("0.4.0", "0.3.0"), Ordering::Greater);
        assert_eq!(version_cmp("0.3.0", "0.4.0"), Ordering::Less);
        assert_eq!(version_cmp("0.10.0", "0.9.9"), Ordering::Greater);
        assert_eq!(version_cmp("1.0.0", "1.0"), Ordering::Equal); // 段数补零
        assert_eq!(version_cmp("0.3.1", "0.3"), Ordering::Greater);
    }

    /// ISS-040B：状态映射①（版本分支）——无远端/相等/更低都归 up_to_date
    /// （公告不高于当前不是错误），更高才是 available。
    #[test]
    fn updater_state_from_versions_maps_equal_and_lower_to_up_to_date() {
        assert_eq!(
            updater_state_from_versions("0.3.0", None),
            UpdaterCheckState::UpToDate
        );
        assert_eq!(
            updater_state_from_versions("0.3.0", Some("0.3.0")),
            UpdaterCheckState::UpToDate
        );
        assert_eq!(
            updater_state_from_versions("0.3.0", Some("0.2.9")),
            UpdaterCheckState::UpToDate
        );
        assert_eq!(
            updater_state_from_versions("0.3.0", Some("0.4.0")),
            UpdaterCheckState::Available
        );
    }

    /// ISS-040B：状态映射②（错误分支）——unconfigured 与 unreachable 必须
    /// 各自成态（验收项），failed 是其余错误的兜底，三者互不相同。
    #[test]
    fn updater_state_from_error_distinguishes_unconfigured_and_unreachable() {
        let unconfigured = updater_state_from_error(UpdaterErrorKind::Unconfigured);
        let unreachable = updater_state_from_error(UpdaterErrorKind::Network);
        let failed = updater_state_from_error(UpdaterErrorKind::Other);
        assert_eq!(unconfigured, UpdaterCheckState::Unconfigured);
        assert_eq!(unreachable, UpdaterCheckState::Unreachable);
        assert_eq!(failed, UpdaterCheckState::Failed);
        assert_ne!(unconfigured, unreachable);
        assert_ne!(unconfigured.as_str(), unreachable.as_str());
    }

    /// ISS-040B：错误适配层——EmptyEndpoints（无 endpoints）归 Unconfigured，
    /// 网络/传输层错误归 Network，其余（如日期格式化失败）归 Other。
    /// 构造真实插件错误变体，不是自证枚举。
    #[test]
    fn updater_error_kind_adapter_maps_known_variants() {
        use tauri_plugin_updater::Error;
        assert_eq!(
            updater_error_kind(&Error::EmptyEndpoints),
            UpdaterErrorKind::Unconfigured
        );
        assert_eq!(
            updater_error_kind(&Error::Network("dns lookup failed".to_string())),
            UpdaterErrorKind::Network
        );
        assert_eq!(
            updater_error_kind(&Error::FormatDate),
            UpdaterErrorKind::Other
        );
    }

    /// ISS-040B：状态 JSON 的字段合同——available 携带 available_version 与
    /// 非空 notes、无 error；unreachable 携带 error、无 available_version；
    /// 未配置与不可达的 state 字符串不同（前端按此分流文案）。
    #[test]
    fn updater_status_json_carries_contract_fields() {
        let available = updater_status_json(
            UpdaterCheckState::Available,
            "0.3.0",
            Some("0.4.0"),
            Some("修复若干问题"),
            None,
        );
        assert_eq!(available["state"], "available");
        assert_eq!(available["current_version"], "0.3.0");
        assert_eq!(available["available_version"], "0.4.0");
        assert_eq!(available["notes"], "修复若干问题");
        assert!(available.get("error").is_none(), "available 态不应携带 error");

        let unreachable = updater_status_json(
            UpdaterCheckState::Unreachable,
            "0.3.0",
            None,
            None,
            Some("error sending request"),
        );
        assert_eq!(unreachable["state"], "unreachable");
        assert_eq!(unreachable["error"], "error sending request");
        assert!(unreachable.get("available_version").is_none());

        let unconfigured = updater_status_json(
            UpdaterCheckState::Unconfigured,
            "0.3.0",
            None,
            None,
            Some("Updater does not have any endpoints set."),
        );
        assert_eq!(unconfigured["state"], "unconfigured");
        assert_ne!(
            unconfigured["state"].as_str().unwrap(),
            unreachable["state"].as_str().unwrap()
        );

        // 空 notes 不落键：前端不需要分辨 null 与缺失。
        let bare = updater_status_json(
            UpdaterCheckState::Available,
            "0.3.0",
            Some("0.4.0"),
            Some("   "),
            None,
        );
        assert!(bare.get("notes").is_none());
    }

    /// ISS-040B：启动延迟合同——常量必须 ≥10s（不静默原则的「延迟」半边），
    /// 事件名与前端 settings.js 监听名一致。
    #[test]
    fn updater_startup_delay_meets_contract_and_event_name_stable() {
        assert!(
            UPDATER_STARTUP_DELAY_S >= 10,
            "启动延迟检查必须 ≥10s（合同），当前 {UPDATER_STARTUP_DELAY_S}s"
        );
        assert_eq!(UPDATER_EVENT, "updater-state");
    }

    /// ISS-040C：下载进度事件载荷合同——downloading 态含 downloaded/total
    ///（total 未知时为 null）、cancellable=true（下载阶段可取消的声明面）。
    #[test]
    fn updater_download_progress_json_carries_downloaded_and_total() {
        let known = updater_download_progress_json("0.3.0", "0.3.1", 2048, Some(4096));
        assert_eq!(known["state"], "downloading");
        assert_eq!(known["current_version"], "0.3.0");
        assert_eq!(known["available_version"], "0.3.1");
        assert_eq!(known["downloaded"], 2048);
        assert_eq!(known["total"], 4096);
        assert_eq!(known["cancellable"], true);

        let unknown = updater_download_progress_json("0.3.0", "0.3.1", 8, None);
        assert!(unknown["total"].is_null(), "未知总长必须是显式 null");
        assert_eq!(unknown["downloaded"], 8);
    }

    /// ISS-040C：进度节流——累计满 128 KiB 或已完成时发事件；未满且未完成不发。
    #[test]
    fn updater_progress_emit_throttle_boundaries() {
        assert!(
            !progress_should_emit(0, 1024, Some(65536)),
            "未满步长且未完成：不发"
        );
        assert!(
            progress_should_emit(0, UPDATER_PROGRESS_EMIT_STEP_BYTES, Some(1 << 20)),
            "满 128 KiB：发"
        );
        assert!(progress_should_emit(0, 65536, Some(65536)), "已完成：发");
        assert!(
            !progress_should_emit(0, 65535, Some(65536)),
            "差一字节未完成：不发"
        );
        assert!(
            !progress_should_emit(0, 4096, None),
            "总长未知且未满步长：不发"
        );
    }

    /// ISS-040C：取消边界——准备/下载阶段可取消；安装/核验/收尾不可取消；
    /// 两条不可取消文案与取消事件名钉死（合同⑤：文案明确）。
    #[test]
    fn updater_phase_cancel_boundary_contract() {
        for cancellable in [
            UpdaterInstallPhase::Preparing,
            UpdaterInstallPhase::Downloading,
        ] {
            assert!(cancellable.allows_cancel(), "{cancellable:?} 应可取消");
        }
        for fixed in [
            UpdaterInstallPhase::Installing,
            UpdaterInstallPhase::Verifying,
            UpdaterInstallPhase::Finalizing,
        ] {
            assert!(!fixed.allows_cancel(), "{fixed:?} 不可取消");
        }
        assert_eq!(UpdaterInstallPhase::Downloading.as_str(), "downloading");
        assert_eq!(UpdaterInstallPhase::Installing.as_str(), "installing");
        assert!(
            UPDATER_INSTALL_NOT_CANCELLABLE_HINT.contains("不可取消")
                && UPDATER_INSTALL_NOT_CANCELLABLE_HINT.contains("回滚"),
            "安装阶段文案必须明确「不可取消」与回滚去向"
        );
        assert!(
            !UPDATER_CANCEL_REFUSED_HINT.is_empty()
                && UPDATER_CANCEL_REFUSED_HINT.contains("拒绝"),
            "取消拒绝文案必须明确"
        );
        assert_eq!(UPDATER_CANCEL_EVENT, "updater-cancel-requested");
    }

    /// ISS-040C：升级子命令 argv 合同——全局 ``--runtime-dir`` 在子命令之前
    ///（argparse 布局），upgrade-prepare 携带 --from/--to；env 与
    /// spawn_helper 同源（FATHOM_RUNTIME_DIR / FATHOM_RUNTIME_MODE）。
    /// 这是「冻结 helper 侧子命令被生产入口真实调用」的壳侧钉子（与
    /// pytest 侧 grep 断言互为证据链）。
    #[test]
    fn upgrade_helper_command_argv_contract() {
        let bin = Path::new("/tmp/fathom-helper");
        let runtime = Path::new("/tmp/fathom-rt");
        let prepare = upgrade_helper_command(
            bin,
            runtime,
            &["upgrade-prepare", "--from", "0.3.0", "--to", "0.3.1"],
        );
        assert_eq!(prepare.get_program().to_string_lossy(), "/tmp/fathom-helper");
        let argv: Vec<String> = prepare
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            argv,
            [
                "--runtime-dir",
                "/tmp/fathom-rt",
                "upgrade-prepare",
                "--from",
                "0.3.0",
                "--to",
                "0.3.1",
            ]
        );
        let mut runtime_env: Option<String> = None;
        let mut mode_env: Option<String> = None;
        for (key, value) in prepare.get_envs() {
            if key == "FATHOM_RUNTIME_DIR" {
                runtime_env = value.map(|v| v.to_string_lossy().into_owned());
            }
            if key == "FATHOM_RUNTIME_MODE" {
                mode_env = value.map(|v| v.to_string_lossy().into_owned());
            }
        }
        assert_eq!(runtime_env.as_deref(), Some("/tmp/fathom-rt"));
        assert_eq!(mode_env.as_deref(), Some("release"));

        let rollback = upgrade_helper_command(bin, runtime, &["upgrade-rollback"]);
        assert!(
            rollback
                .get_args()
                .any(|arg| arg == "upgrade-rollback")
        );
        let finalize = upgrade_helper_command(bin, runtime, &["upgrade-finalize"]);
        assert!(
            finalize.get_args().any(|arg| arg == "upgrade-finalize")
        );
    }

    /// ISS-040C：子命令输出解析——取最后一个非空行且必须合法 JSON；
    /// 诊断行/空行/非 JSON 均不误读。
    #[test]
    fn parse_json_last_line_tolerates_diagnostic_lines() {
        let parsed = parse_json_last_line("诊断行\n{\"ok\": true, \"kind\": \"prepared\"}\n");
        assert_eq!(parsed.expect("末行 JSON 必须可解析")["kind"], "prepared");
        assert!(parse_json_last_line("").is_none());
        assert!(parse_json_last_line("not json at all").is_none());
        assert!(parse_json_last_line("前置日志\n仍然不是 JSON").is_none());
    }

    /// ISS-040C：新 helper 身份核验——service/protocol/version 三方一致才
    /// 通过；任一不符给出可读问题清单（030A step6 握手语义）。
    #[test]
    fn verify_new_helper_identity_requires_all_three_fields() {
        let good = serde_json::json!({
            "service": "fathom",
            "protocol_version": 1,
            "version": "0.3.1",
        });
        assert!(verify_new_helper_identity(&good, "0.3.1").is_ok());
        for bad in [
            serde_json::json!({"service": "other", "protocol_version": 1, "version": "0.3.1"}),
            serde_json::json!({"service": "fathom", "protocol_version": 2, "version": "0.3.1"}),
            serde_json::json!({"service": "fathom", "protocol_version": 1, "version": "0.3.0"}),
            serde_json::json!({}),
        ] {
            let err = verify_new_helper_identity(&bad, "0.3.1")
                .expect_err("身份不符必须拒绝");
            assert!(
                err.contains("新 helper 握手失败"),
                "错误必须点名握手失败：{err}"
            );
        }
    }

    /// ISS-040C：prepare 失败话术映射——scan_busy 带扫描与重试提示（合同①
    /// 的可读失败态），各 kind 都有非空恢复提示。
    #[test]
    fn prepare_failure_hint_maps_kinds_to_recovery_copy() {
        let busy = prepare_failure_hint("scan_busy");
        assert!(busy.contains("扫描") && busy.contains("重试"));
        for kind in [
            "helper_exit_timeout",
            "schema_refused",
            "backup",
            "disk_full",
            "half_upgraded_state",
            "unknown-kind",
        ] {
            assert!(
                !prepare_failure_hint(kind).is_empty(),
                "{kind} 必须有恢复提示"
            );
        }
    }

    /// ISS-040C：控制面阶段机——begin 复位/推进取消语义（下载中受理、安装
    /// 中请求被拒并保持阶段）。
    #[test]
    fn updater_install_ctl_phase_and_cancel_flags() {
        let ctl = UpdaterInstallCtl::new();
        assert_eq!(ctl.current(), UpdaterInstallPhase::Preparing);
        ctl.begin(UpdaterInstallPhase::Downloading);
        assert!(ctl.current().allows_cancel());
        ctl.request_cancel();
        assert!(ctl.is_cancel_requested());
        // 新一轮安装回到 Preparing：取消标志复位。
        ctl.begin(UpdaterInstallPhase::Preparing);
        assert!(!ctl.is_cancel_requested());
        ctl.begin(UpdaterInstallPhase::Installing);
        assert!(!ctl.current().allows_cancel());
    }
}
