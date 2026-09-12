//! Fathom 桌面壳：菜单栏常驻 + 主窗口（加载本机 FastAPI 仪表盘）。
//!
//! 设计原则（DEC-007）：
//! - 壳只负责 UI（tray + 窗口），不管 Python 后端生命周期——后端由 launchd 常驻。
//! - 关闭窗口 = 隐藏（macOS 菜单栏应用惯例），退出走 tray 菜单。
//! - tray 标题（剩余空间）由前端页面定期 invoke `update_tray_status` 推送，
//!   Rust 侧不引 HTTP 依赖。

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, WindowEvent,
};

#[tauri::command]
fn update_tray_status(app: tauri::AppHandle, title: String, tooltip: String) {
    if let Some(tray) = app.tray_by_id("sentinel") {
        let _ = tray.set_title(Some(title));
        let _ = tray.set_tooltip(Some(tooltip));
    }
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![update_tray_status])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // macOS 菜单栏应用惯例：点关闭只隐藏窗口，tray 常驻
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .setup(|app| {
            let status = MenuItem::with_id(app, "status", "Fathom 启动中…", false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "打开主界面", true, None::<&str>)?;
            let scan = MenuItem::with_id(app, "scan", "立即扫描…", true, None::<&str>)?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "退出 Fathom", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&status, &sep1, &open, &scan, &sep2, &quit])?;

            let _tray = TrayIconBuilder::with_id("sentinel")
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
                    "quit" => app.exit(0),
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

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Fathom 桌面壳启动失败");
}
