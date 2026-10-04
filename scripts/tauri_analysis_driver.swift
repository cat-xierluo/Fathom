#!/usr/bin/env swift
// ISS-141 · 变化解读实机驱动（只读小工具 + 启动凭据持有者）
//
// 职责分工：
//   - launch  —— 用 NSWorkspace + NSWorkspace.OpenConfiguration 注入隔离 env
//               并 **不激活**（activates=false），直接返回 NSRunningApplication
//               句柄；调用方由此拿到唯一 GUI 进程凭据，不靠 pgrep 集合差收养。
//   - 其余子命令 —— 只按已识别的自有 PID + bundle id 操作窗口/事件/截图。
//
// 身份纪律（fail-closed）：
//   - launch 之外的每个子命令都必须同时给 --pid 与 --bundle-id；运行进程的
//     bundle id 与传入值不符即拒。窗口必须属于该 PID 且 layer==0。
//   - 无法消歧（0 个或 >1 个自有窗口）直接非零退出，不猜、不回退到屏幕
//     坐标盲点、不退化整屏截图冒充原生窗口证据。
//   - 尺寸回读用窗口级 AX 元素的真实值；标题栏高度按窗口是否真的有
//     kAXTitleUIElement 实测，不写死常数假装实测。
//
// 子命令：
//   launch      NSWorkspace 隔离启动（env 注入 / 不激活 / 返回 GUI pid 凭据）
//   resolve-app --bundle-id  解析唯一 GUI 进程 pid（NSRunningApplication(bundleIdentifier:)）
//   identify    列出自有窗口（window_id / 帧 / 标题 / 标题栏实测）
//   set-size    设外框 size（仅在显式给 --x/--y 时才搬位置）
//   measure     窗口级 AX 回读外框与内容区
//   click       CGEvent postToPid 左键点击（窗口内相对坐标）
//   screenshot  screencapture -x -o -l<window id> 抓该窗口
//
// --help 输出**单行 JSON**（供入口按 JSON 契约解析），退出码：
//   0 成功（stdout 单行 JSON）/ 2 用法错误 / 3 身份或环境阻塞

import Cocoa
import CoreGraphics
import Foundation

// ---------------------------------------------------------------- JSON 输出

func emit(_ payload: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

func fail(_ message: String, code: Int32 = 3) -> Never {
    FileHandle.standardError.write(Data("driver-error: \(message)\n".utf8))
    exit(code)
}

let SUBCOMMANDS = ["launch", "resolve-app", "identify", "set-size",
                   "measure", "click", "screenshot"]

func helpPayload() -> [String: Any] {
    return [
        "driver": "tauri_analysis_driver",
        "card": "ISS-141",
        "subcommands": SUBCOMMANDS,
        "usage": "tauri_analysis_driver <subcommand> [--flags]",
        "requires_identity_flags": ["pid", "bundle-id"],
        "never": ["activate", "AXRaise", "global keyboard", "launchctl"],
        "flags": [
            "launch": ["--app", "--runtime-dir", "--scan-root", "--port",
                       "--timeout", "--activate(false 固定)"],
            "resolve-app": ["--bundle-id"],
            "identify": ["--pid", "--bundle-id"],
            "set-size": ["--pid", "--bundle-id", "--window-id", "--width",
                         "--height", "--x", "--y"],
            "measure": ["--pid", "--bundle-id", "--window-id"],
            "click": ["--pid", "--bundle-id", "--window-id", "--click-x",
                      "--click-y"],
            "screenshot": ["--pid", "--bundle-id", "--window-id", "--output"],
        ],
        "notes": [
            "launch 用 NSWorkspace.OpenConfiguration.environment 注入隔离 env",
            "launch 固定 activates=false，不抢前台",
            "set-size 仅在显式提供 --x/--y 时才移动窗口",
            "measure 用窗口级 AX 元素实测；标题栏按是否真有标题元素实测",
        ],
    ]
}

// ---------------------------------------------------------------- 参数解析

struct Options {
    var command = ""
    var pid: pid_t = -1
    var bundleId = ""
    var windowId: Int = 0
    var width: Double = 0
    var height: Double = 0
    var x: Double = 0
    var y: Double = 0
    var xSet = false
    var ySet = false
    var output = ""
    var clickX: Double = 0
    var clickY: Double = 0
    var app = ""
    var runtimeDir = ""
    var scanRoot = ""
    var port = ""
    var timeout: Double = 90
    var requireContentPlane = false
}

func parseArgs(_ argv: [String]) -> Options {
    var options = Options()
    if argv.isEmpty { emit(helpPayload()) }
    if argv[0] == "--help" || argv[0] == "-h" { emit(helpPayload()) }
    if !SUBCOMMANDS.contains(argv[0]) {
        fail("未知子命令 \(argv[0])（可用：\(SUBCOMMANDS.joined(separator: ", "))）",
             code: 2)
    }
    options.command = argv[0]
    var index = 1
    while index < argv.count {
        let flag = argv[index]
        guard index + 1 < argv.count else { fail("参数 \(flag) 缺值", code: 2) }
        let value = argv[index + 1]
        switch flag {
        case "--pid":
            guard let parsed = pid_t(value) else { fail("--pid 非法：\(value)", code: 2) }
            options.pid = parsed
        case "--bundle-id": options.bundleId = value
        case "--window-id":
            guard let parsed = Int(value) else { fail("--window-id 非法：\(value)", code: 2) }
            options.windowId = parsed
        case "--width":
            guard let parsed = Double(value) else { fail("--width 非法：\(value)", code: 2) }
            options.width = parsed
        case "--height":
            guard let parsed = Double(value) else { fail("--height 非法：\(value)", code: 2) }
            options.height = parsed
        case "--x":
            guard let parsed = Double(value) else { fail("--x 非法：\(value)", code: 2) }
            options.x = parsed; options.xSet = true
        case "--y":
            guard let parsed = Double(value) else { fail("--y 非法：\(value)", code: 2) }
            options.y = parsed; options.ySet = true
        case "--output": options.output = value
        case "--require-content-plane": options.requireContentPlane = true;
        case "--click-x":
            guard let parsed = Double(value) else { fail("--click-x 非法：\(value)", code: 2) }
            options.clickX = parsed
        case "--click-y":
            guard let parsed = Double(value) else { fail("--click-y 非法：\(value)", code: 2) }
            options.clickY = parsed
        case "--app": options.app = value
        case "--runtime-dir": options.runtimeDir = value
        case "--scan-root": options.scanRoot = value
        case "--port": options.port = value
        case "--timeout":
            guard let parsed = Double(value) else { fail("--timeout 非法：\(value)", code: 2) }
            options.timeout = parsed
        default:
            fail("未知参数 \(flag)", code: 2)
        }
        index += 2
    }
    return options
}

let options = parseArgs(Array(CommandLine.arguments.dropFirst()))

// ---------------------------------------------------------------- launch

if options.command == "launch" {
    let bundleURL = URL(fileURLWithPath: options.app)
    guard options.runtimeDir != "", options.scanRoot != "", options.port != "" else {
        fail("launch 需要 --app --runtime-dir --scan-root --port", code: 2)
    }
    // 启动前自证：runtime 根与扫描根都必须在，且扫描根不得等于/落在 HOME。
    let runtimeURL = URL(fileURLWithPath: options.runtimeDir)
    let scanURL = URL(fileURLWithPath: options.scanRoot)
    guard FileManager.default.fileExists(atPath: runtimeURL.path) else {
        fail("runtime 根不存在，拒绝启动：\(runtimeURL.path)")
    }
    guard FileManager.default.fileExists(atPath: scanURL.path) else {
        fail("扫描根不存在，拒绝启动：\(scanURL.path)")
    }
    let homePath = URL(fileURLWithPath: NSHomeDirectory())
        .resolvingSymlinksInPath().path
    let scanPath = scanURL.resolvingSymlinksInPath().path
    if scanPath == homePath || scanPath.hasPrefix(homePath + "/") {
        fail("扫描根落在 HOME 内，拒绝启动：\(scanPath)")
    }
    // 端口合法性与非生产端口自证。
    guard let portNumber = Int(options.port), portNumber > 0, portNumber < 65_536 else {
        fail("--port 非法：\(options.port)", code: 2)
    }
    if portNumber == 7952 { fail("拒绝使用生产端口 7952") }

    let config = NSWorkspace.OpenConfiguration()
    config.environment = [
        "FATHOM_RUNTIME_DIR": runtimeURL.path,
        "FATHOM_SCAN_ROOT": scanPath,
        "FATHOM_PORT": options.port,
    ]
    // 零打扰：固定不激活，不抢前台。
    config.activates = false
    config.addsToRecentItems = false
    let sem = DispatchSemaphore(value: 0)
    var resultApp: NSRunningApplication?
    var launchError: Error?
    var timedOut = false
    DispatchQueue.global().async {
        NSWorkspace.shared.openApplication(at: bundleURL, configuration: config) { app, error in
            resultApp = app
            launchError = error
            sem.signal()
        }
    }
    if sem.wait(timeout: .now() + options.timeout) == .timedOut {
        timedOut = true
    }
    if timedOut { fail("launch 超时 \(Int(options.timeout))s：无法取得 GUI 进程凭据") }
    if let error = launchError {
        fail("NSWorkspace 启动失败：\(error.localizedDescription)")
    }
    guard let app = resultApp else {
        fail("NSWorkspace 未返回 NSRunningApplication，拒绝继续（无法证明自有身份）")
    }
    let pid = app.processIdentifier
    guard pid > 1 else { fail("launch 返回无效 PID \(pid)") }
    // 凭据自证：激活后再次解析，必须仍是同一 bundle id 的存活进程。
    guard let resolved = NSRunningApplication(processIdentifier: pid),
          !resolved.isTerminated,
          let actualBundleId = resolved.bundleIdentifier else {
        fail("launch 后无法自证 GUI 进程身份（pid=\(pid)）")
    }
    emit([
        "launched": true,
        "pid": Int(pid),
        "bundle_id": actualBundleId,
        "executable": resolved.executableURL?.path ?? "",
        "activated": resolved.isActive,
        "env": config.environment,
        "runtime_dir": runtimeURL.path,
        "scan_root": scanPath,
        "port": portNumber,
    ])
}

if options.command == "resolve-app" {
    guard !options.bundleId.isEmpty else { fail("resolve-app 需要 --bundle-id", code: 2) }
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: options.bundleId)
    guard !apps.isEmpty else {
        fail("没有运行中的 bundle \(options.bundleId)")
    }
    let alive = apps.filter { !$0.isTerminated }
    guard alive.count == 1 else {
        // 多个候选 = 无法消歧 = 拒绝（不猜、不收养）
        fail("bundle \(options.bundleId) 有 \(alive.count) 个运行实例，无法消歧")
    }
    let app = alive[0]
    emit([
        "pid": Int(app.processIdentifier),
        "bundle_id": options.bundleId,
        "executable": app.executableURL?.path ?? "",
        "frontmost": app.isActive,
    ])
}

// ---------------------------------------------------------------- 身份消歧

guard options.command != "resolve-app" else { fail("unreachable") }
guard options.pid > 1 else { fail("必须提供 --pid（自有进程身份），拒绝执行", code: 2) }
guard !options.bundleId.isEmpty else { fail("必须提供 --bundle-id，拒绝执行", code: 2) }
guard let running = NSRunningApplication(processIdentifier: options.pid) else {
    fail("PID \(options.pid) 不存在或已退出")
}
if running.isTerminated { fail("PID \(options.pid) 已终止") }
guard let actualBundleId = running.bundleIdentifier else {
    fail("PID \(options.pid) 无 bundle identifier（非 app 进程），拒绝执行")
}
if actualBundleId != options.bundleId {
    fail("bundle id 不符：期望 \(options.bundleId) 实测 \(actualBundleId)")
}

struct OwnedWindow {
    let id: Int
    let frame: CGRect
    let title: String
}

func ownedWindows() -> [OwnedWindow] {
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID)
        as? [[String: Any]] ?? []
    var result: [OwnedWindow] = []
    for info in list {
        guard let ownerPid = info[kCGWindowOwnerPID as String] as? pid_t,
              ownerPid == options.pid else { continue }
        guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0 else { continue }
        guard let number = info[kCGWindowNumber as String] as? Int else { continue }
        guard let boundsDict = info[kCGWindowBounds as String] as? [String: Any],
              let bounds = CGRect(dictionaryRepresentation: boundsDict as CFDictionary) else {
            continue
        }
        if bounds.width < 100 || bounds.height < 100 { continue }
        result.append(OwnedWindow(id: number, frame: bounds,
                                  title: info[kCGWindowName as String] as? String ?? ""))
    }
    return result
}

func appElement() -> AXUIElement { AXUIElementCreateApplication(options.pid) }

/// 窗口级 AX 元素：从 application 元素的 kAXWindowsAttribute 取，
/// 优先 focused window，其次唯一窗口。**不在 application 元素上读
/// kAXSize/kAXPosition**（窗口属性在应用元素上通常 attributeUnsupported）。
/// app 刚启动时 AX 服务器可能尚未挂上窗口（实测偶发），有界重试等就绪。
func windowElement(windowId: Int) -> AXUIElement? {
    for _ in 0..<10 {
        var value: CFTypeRef?
        if AXUIElementCopyAttributeValue(appElement(),
                                        kAXWindowsAttribute as CFString,
                                        &value) == AXError.success,
           let windows = value as? [AXUIElement], !windows.isEmpty {
            var focused: CFTypeRef?
            if AXUIElementCopyAttributeValue(appElement(),
                                             kAXFocusedWindowAttribute as CFString,
                                             &focused) == AXError.success,
               let focusedElement = focused {
                return focusedElement as! AXUIElement
            }
            if windows.count == 1 { return windows[0] }
            return nil
        }
        usleep(500_000)
    }
    return nil
}

func axSize(_ element: AXUIElement) -> CGSize? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString,
                                        &value) == AXError.success,
          let raw = value else { return nil }
    var size = CGSize.zero
    guard AXValueGetValue(raw as! AXValue, .cgSize, &size) else { return nil }
    return size
}

func axPosition(_ element: AXUIElement) -> CGPoint? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString,
                                        &value) == AXError.success,
          let raw = value else { return nil }
    var point = CGPoint.zero
    guard AXValueGetValue(raw as! AXValue, .cgPoint, &point) else { return nil }
    return point
}

/// 标题【文字 label】高度。**这不是标题栏高、也不是内容区差值**：
/// 旧实现把它当标题栏高度，导致 content = frame - 16 在真实窗口上偏差 +12pt
/// （PM 实机 980x652）。此处保留仅为如实标注所测面，内容几何一律取 web area。
func titleLabelHeight(_ window: AXUIElement) -> Double {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(window,
                                        kAXTitleUIElementAttribute as CFString,
                                        &value) == AXError.success,
          let titleElement = value as! AXUIElement? else { return 0 }
    guard let titleSize = axSize(titleElement) else { return 0 }
    return titleSize.height
}

/// AXWebArea 的公开字符串值。本机 SDK 未导出 kAXWebAreaAttribute /
/// kAXWebAreaRole 常量，按 AppKit 公开的 AX 值使用。
let webAreaAttributeName = "AXWebArea"
let webAreaRoleName = "AXWebArea"

/// WKWebView 的**实际内容面**：窗口的 AXWebArea 子元素。
/// 这是唯一能直接证明「webview 内容几何」的面；取不到就如实报 unavailable，
/// 由调用方失败闭合，绝不用 frame 或标题文字冒充内容。
func webAreaElement(_ window: AXUIElement) -> AXUIElement? {
    var value: CFTypeRef?
    if AXUIElementCopyAttributeValue(window, webAreaAttributeName as CFString,
                                      &value) == AXError.success,
       let webArea = value as! AXUIElement? {
        return webArea
    }
    // 回退：BFS 遍历窗口 AX 树找 role == AXWebArea。实测它挂在
    // window→AXGroup→AXGroup→AXScrollArea 下，不在窗口直接子层；
    // 只查一层会漏掉真实存在的 web 内容面。
    var queue: [AXUIElement] = [window]
    var depth = 0
    var visited = 0
    while !queue.isEmpty && depth < 6 && visited < 128 {
        var next: [AXUIElement] = []
        for element in queue {
            visited += 1
            var roleValue: CFTypeRef?
            if AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString,
                                             &roleValue) == AXError.success,
               (roleValue as? String) == webAreaRoleName {
                return element
            }
            var childrenValue: CFTypeRef?
            if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString,
                                             &childrenValue) == AXError.success,
               let children = childrenValue as? [AXUIElement] {
                next.append(contentsOf: children)
            }
        }
        queue = next
        depth += 1
    }
    return nil
}

/// WKWebView 默认不向外部 AX 客户端暴露 web 内容树；按辅助功能客户端对
/// WKWebView 的公开启用机制，对 AX 元素设置 AXManualAccessibility /
/// AXEnhancedUserInterface 后，AXWebArea 才会在 WebView 元素下出现。
/// 不支持的元素返回 attributeUnsupported 属正常，逐个忽略。
func enableWebAccessibility(_ root: AXUIElement) -> Int {
    var queue: [AXUIElement] = [root]
    var visited = 0
    var depth = 0
    while !queue.isEmpty && depth < 6 && visited < 128 {
        var next: [AXUIElement] = []
        for element in queue {
            visited += 1
            AXUIElementSetAttributeValue(element,
                                         "AXManualAccessibility" as CFString,
                                         kCFBooleanTrue)
            AXUIElementSetAttributeValue(element,
                                         "AXEnhancedUserInterface" as CFString,
                                         kCFBooleanTrue)
            var childrenValue: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString,
                                                &childrenValue) == AXError.success,
                  let children = childrenValue as? [AXUIElement] else { continue }
            next.append(contentsOf: children)
        }
        queue = next
        depth += 1
    }
    return visited
}

/// 先外部启用 web AX，再轮询等 AXWebArea 出现。每轮都重新 enable：
/// WebView 的 AX 元素是在启用后才长进树里的，首轮 BFS 触不到它，
/// 新节点必须补设 AXManualAccessibility。超时仍取不到返回 nil，
/// 调用方保持 fail-closed，不用 frame 伪证。
func webAreaElementActivated(_ window: AXUIElement) -> AXUIElement? {
    let app = appElement()
    for _ in 0..<12 {
        _ = enableWebAccessibility(app)
        if let webArea = webAreaElement(window) { return webArea }
        usleep(500_000)
    }
    return webAreaElement(window)
}

switch options.command {
case "identify":
    let windows = ownedWindows()
    var detailed: [[String: Any]] = []
    for win in windows {
        var entry: [String: Any] = [
            "window_id": win.id,
            "title": win.title,
            "cg_frame_width": Int(win.frame.width.rounded()),
            "cg_frame_height": Int(win.frame.height.rounded()),
            "cg_frame_x": Int(win.frame.origin.x.rounded()),
            "cg_frame_y": Int(win.frame.origin.y.rounded()),
        ]
        if let element = windowElement(windowId: win.id),
           let size = axSize(element) {
            entry["ax_frame_width"] = Int(size.width.rounded())
            entry["ax_frame_height"] = Int(size.height.rounded())
            entry["title_label_height"] = Int(titleLabelHeight(element).rounded())
            if let webArea = webAreaElementActivated(element), let webSize = axSize(webArea) {
                entry["web_area_width"] = Int(webSize.width.rounded())
                entry["web_area_height"] = Int(webSize.height.rounded())
                entry["content_source"] = "ax_web_area"
            } else {
                entry["content_source"] = "unavailable"
            }
        }
        detailed.append(entry)
    }
    emit(["pid": Int(options.pid), "bundle_id": actualBundleId,
          "frontmost": running.isActive, "windows": detailed])

case "set-size":
    guard options.windowId != 0 else { fail("set-size 需要 --window-id", code: 2) }
    guard options.width > 0, options.height > 0 else {
        fail("--width/--height 必须为正", code: 2)
    }
    guard ownedWindows().contains(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝操作")
    }
    guard let window = windowElement(windowId: options.windowId) else {
        fail("无法取得窗口级 AX 元素（属性可能在 application 元素上不支持）")
    }
    var size = CGSize(width: options.width, height: options.height)
    let sizeValue = AXValueCreate(.cgSize, &size)!
    var error = AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString,
                                             sizeValue)
    if error != AXError.success {
        fail("AX 设置窗口尺寸失败（AXError \(error.rawValue)）")
    }
    // 位置：**仅在显式给 --x/--y 时**才搬移，避免未声明的窗口漂移。
    var moved = false
    if options.xSet && options.ySet {
        var position = CGPoint(x: options.x, y: options.y)
        let posValue = AXValueCreate(.cgPoint, &position)!
        error = AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString,
                                             posValue)
        if error != AXError.success {
            fail("AX 设置窗口位置失败（AXError \(error.rawValue)）")
        }
        moved = true
    }
    emit(["window_id": options.windowId,
          "requested_frame_width": options.width,
          "requested_frame_height": options.height,
          "position_moved": moved])

case "measure":
    guard options.windowId != 0 else { fail("measure 需要 --window-id", code: 2) }
    guard let cgWindow = ownedWindows().first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝操作")
    }
    guard let window = windowElement(windowId: options.windowId) else {
        fail("measure 需要窗口级 AX 元素，拒绝用 application 元素假装实测")
    }
    guard let size = axSize(window) else {
        fail("窗口级 AX 读取 kAXSize 失败，拒绝用外框-常数假装实测")
    }
    let position = axPosition(window) ?? .zero
    // 内容几何**只**取自 WKWebView 自身的 AXWebArea；取不到即如实 unavailable。
    // 绝不再用 frame 或标题文字高度冒充内容。
    var payload: [String: Any] = [
        "window_id": options.windowId,
        "frame_width": Int(size.width.rounded()),
        "frame_height": Int(size.height.rounded()),
        "frame_x": Int(position.x.rounded()),
        "frame_y": Int(position.y.rounded()),
        "title_label_height": Int(titleLabelHeight(window).rounded()),
        "title_label_note": "标题文字 label 高度，非标题栏高、非内容区差值",
        "cg_frame_width": Int(cgWindow.frame.width.rounded()),
        "cg_frame_height": Int(cgWindow.frame.height.rounded()),
    ]
    if let webArea = webAreaElementActivated(window),
       let webSize = axSize(webArea) {
        let webPos = axPosition(webArea) ?? .zero
        payload["content_source"] = "ax_web_area"
        payload["content_width"] = Int(webSize.width.rounded())
        payload["content_height"] = Int(webSize.height.rounded())
        // web area 在窗口内的偏移：点击坐标面与内容测量面同源的依据
        payload["web_area_x_in_window"] = Int((webPos.x - position.x).rounded())
        payload["web_area_y_in_window"] = Int((webPos.y - position.y).rounded())
        payload["chrome_height"] = Int((size.height - webSize.height).rounded())
        payload["chrome_note"] = "frame - web area 实测差值，非假设常数"
    } else {
        // 失败闭合：不产出内容几何，调用方必须报 blocker，不得伪证
        payload["content_source"] = "unavailable"
        payload["content_width"] = NSNull()
        payload["content_height"] = NSNull()
        payload["blocker"] = "未找到 AXWebArea，无法证明 WKWebView 实际内容区"
    }
    emit(payload)

case "click":
    guard options.windowId != 0 else { fail("click 需要 --window-id", code: 2) }
    guard let target = ownedWindows().first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝投递事件")
    }
    // 点击面与内容测量面同源：坐标是【web 内容区】内的相对点，先经 web area
    // 在窗口内的偏移换算到窗口面，再换算到屏幕面。web area 取不到即拒绝投递，
    // 避免把标题栏/frame 区域误当 webview 内容点击。
    var offsetX = options.clickX
    var offsetY = options.clickY
    var contentSource = "window_relative"
    if let window = windowElement(windowId: options.windowId),
       let webArea = webAreaElementActivated(window),
       let webPos = axPosition(webArea),
       let winPos = axPosition(window) {
        offsetX = options.clickX + (webPos.x - winPos.x)
        offsetY = options.clickY + (webPos.y - winPos.y)
        contentSource = "ax_web_area"
    } else if options.requireContentPlane {
        fail("无法取得 AXWebArea：点击面无法与内容测量面同源，拒绝投递事件")
    }
    let screenX = target.frame.origin.x + offsetX
    let screenY = target.frame.origin.y + offsetY
    guard screenX >= 0, screenY >= 0 else { fail("换算后的点击坐标越界") }
    let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                       mouseCursorPosition: CGPoint(x: screenX, y: screenY),
                       mouseButton: .left)
    move!.postToPid(options.pid)
    usleep(60_000)
    let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                       mouseCursorPosition: CGPoint(x: screenX, y: screenY),
                       mouseButton: .left)
    down!.postToPid(options.pid)
    usleep(40_000)
    let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                     mouseCursorPosition: CGPoint(x: screenX, y: screenY),
                     mouseButton: .left)
    up!.postToPid(options.pid)
    emit(["window_id": target.id, "screen_x": Int(screenX.rounded()),
          "screen_y": Int(screenY.rounded()), "posted_to_pid": Int(options.pid),
          "content_source": contentSource,
          "offset_in_window_x": Int(offsetX.rounded()),
          "offset_in_window_y": Int(offsetY.rounded())])

case "screenshot":
    guard options.windowId != 0 else { fail("screenshot 需要 --window-id", code: 2) }
    guard !options.output.isEmpty else { fail("screenshot 需要 --output", code: 2) }
    guard let target = ownedWindows().first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝截图")
    }
    let shot = Process()
    shot.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    shot.arguments = ["-x", "-o", "-l\(target.id)", options.output]
    let pipe = Pipe()
    shot.standardError = pipe
    try? shot.run()
    shot.waitUntilExit()
    if shot.terminationStatus != 0 {
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        fail("screencapture 失败 exit=\(shot.terminationStatus)："
             + String(data: data, encoding: .utf8)!)
    }
    guard FileManager.default.fileExists(atPath: options.output),
          let image = NSImage(contentsOfFile: options.output) else {
        fail("截图未生成：\(options.output)")
    }
    let size = image.size
    guard size.width > 0, size.height > 0 else { fail("截图像素尺寸为 0") }
    emit(["window_id": target.id, "path": options.output,
          "width": Int(size.width.rounded()), "height": Int(size.height.rounded())])

default:
    fail("未知子命令 \(options.command)", code: 2)
}
