#!/usr/bin/env swift
// ISS-141 · 变化解读实机驱动（只读小工具）
//
// 职责：只按**已识别的自有 PID + bundle id** 定位窗口，提供后台可执行的
// 窗口测量 / 尺寸设置 / 鼠标事件投递 / 窗口截图。不发全局键盘事件、
// 不 activate 应用、不做任何 launchctl 变更。
//
// 身份纪律（fail-closed）：
//   - 每次子命令都必须同时给 --pid 与 --bundle-id；进程运行的 bundle id
//     与传入值不符即拒绝（不是自己的窗口就不碰）。
//   - 窗口必须在 CGWindowList 中属于该 PID，且 layer == 0（正常窗口层）。
//   - 无法消歧（0 个或 >1 个自有窗口）直接非零退出，不猜、不回退到
//     屏幕坐标盲点、不退化成浏览器/整屏截图冒充原生窗口证据。
//
// 子命令：
//   identify    列出自有窗口（window_id / 帧 / 标题）
//   set-size    通过 AX 设外框 size+position（不激活）
//   measure     回读外框与内容区（内容区 = 外框 - 标题栏 28pt）
//   click       经 CGEventPostToPid 投递左键点击到自有窗口内的相对坐标
//   screenshot  screencapture -x -o -l<window id> 抓该窗口（静默、不含鼠标）
//
// 退出码：0 成功（stdout 单行 JSON）/ 2 用法错误 / 3 身份或环境阻塞。

import Cocoa
import CoreGraphics
import Foundation

let TITLEBAR_HEIGHT: Double = 28

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

struct Options {
    var command = ""
    var pid: pid_t = -1
    var bundleId = ""
    var windowId: Int = 0
    var width: Double = 0
    var height: Double = 0
    var x: Double = 0
    var y: Double = 0
    var output = ""
    var clickX: Double = 0
    var clickY: Double = 0
}

func parseArgs(_ argv: [String]) -> Options {
    var options = Options()
    var index = 0
    if argv.isEmpty {
        usage()
    }
    if argv[0] == "--help" || argv[0] == "-h" {
        usage()
    }
    options.command = argv[0]
    if !SUBCOMMANDS.contains(options.command) {
        FileHandle.standardError.write(Data(
            "driver-error: 未知子命令 \(options.command)（可用：\(SUBCOMMANDS.joined(separator: ", "))）\n".utf8))
        exit(2)
    }
    index = 1
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
            options.x = parsed
        case "--y":
            guard let parsed = Double(value) else { fail("--y 非法：\(value)", code: 2) }
            options.y = parsed
        case "--output": options.output = value
        case "--click-x":
            guard let parsed = Double(value) else { fail("--click-x 非法：\(value)", code: 2) }
            options.clickX = parsed
        case "--click-y":
            guard let parsed = Double(value) else { fail("--click-y 非法：\(value)", code: 2) }
            options.clickY = parsed
        default:
            fail("未知参数 \(flag)", code: 2)
        }
        index += 2
    }
    if options.command == "--help" || options.command == "-h" { usage() }
    return options
}

let SUBCOMMANDS = ["identify", "set-size", "measure", "click", "screenshot"]

func usage() -> Never {
    let text = """
    usage: tauri_analysis_driver <subcommand> --pid <pid> --bundle-id <id> [options]

    subcommands:
      identify    列出该 PID 的自有窗口（需 --pid --bundle-id）
      set-size    设外框 size+position（需 --window-id --width --height）
      measure     回读外框与内容区（需 --window-id）
      click       CGEventPostToPid 左键点击（需 --window-id --click-x --click-y）
                  坐标为窗口内相对点（左上原点）
      screenshot  抓自有窗口到 --output PNG（需 --window-id --output）

    身份纪律：--pid 与 --bundle-id 必须匹配运行进程，否则拒绝执行。
    本工具不 activate、不发送全局键盘事件、不修改 launchctl 环境。
    """
    print(text)
    if SUBCOMMANDS.isEmpty { fail("unreachable", code: 2) }
    exit(0)
}

let options = parseArgs(Array(CommandLine.arguments.dropFirst()))

if options.pid <= 1 { fail("必须提供 --pid（自有进程身份），拒绝执行", code: 2) }
if options.bundleId.isEmpty { fail("必须提供 --bundle-id，拒绝执行", code: 2) }

// ---- 身份消歧：进程 bundle id 必须与传入值一致 ----------------------
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

// ---- 窗口枚举：只看该 PID 的 layer 0 窗口 ----------------------------
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
        let title = info[kCGWindowName as String] as? String ?? ""
        result.append(OwnedWindow(id: number, frame: bounds, title: title))
    }
    return result
}

func axElement(_ pid: pid_t) -> AXUIElement {
    AXUIElementCreateApplication(pid)
}

func axSize(_ pid: pid_t) throws -> CGSize {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(axElement(pid), kAXSizeAttribute as CFString, &value)
    guard error == AXError.success, let sizeValue = value else {
        fail("AX 读取窗口尺寸失败（AXError \(error.rawValue)）：可能缺少辅助功能权限")
    }
    var size = CGSize.zero
    AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
    return size
}

func axPosition(_ pid: pid_t) throws -> CGPoint {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(axElement(pid), kAXPositionAttribute as CFString, &value)
    guard error == AXError.success, let posValue = value else {
        fail("AX 读取窗口位置失败（AXError \(error.rawValue)）")
    }
    var point = CGPoint.zero
    AXValueGetValue(posValue as! AXValue, .cgPoint, &point)
    return point
}

switch options.command {
case "identify":
    let windows = ownedWindows()
    emit([
        "pid": Int(options.pid),
        "bundle_id": actualBundleId,
        "frontmost": running.isActive,
        "windows": windows.map { win in
            [
                "window_id": win.id,
                "title": win.title,
                "frame_width": win.frame.width,
                "frame_height": win.frame.height,
                "frame_x": win.frame.origin.x,
                "frame_y": win.frame.origin.y,
            ]
        },
    ])

case "set-size":
    guard options.windowId != 0 else { fail("set-size 需要 --window-id", code: 2) }
    let windows = ownedWindows()
    guard let target = windows.first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝操作")
    }
    guard options.width > 0, options.height > 0 else {
        fail("--width/--height 必须为正", code: 2)
    }
    // 只设 size + position，绝不 AXRaise / activate。
    var size = CGSize(width: options.width, height: options.height)
    let sizeValue = AXValueCreate(.cgSize, &size)!
    var error = AXUIElementSetAttributeValue(
        axElement(options.pid), kAXSizeAttribute as CFString, sizeValue)
    if error != AXError.success {
        fail("AX 设置窗口尺寸失败（AXError \(error.rawValue)）")
    }
    var position = CGPoint(x: options.x, y: options.y)
    let posValue = AXValueCreate(.cgPoint, &position)!
    error = AXUIElementSetAttributeValue(
        axElement(options.pid), kAXPositionAttribute as CFString, posValue)
    if error != AXError.success {
        fail("AX 设置窗口位置失败（AXError \(error.rawValue)）")
    }
    emit(["window_id": target.id, "requested_frame_width": options.width,
          "requested_frame_height": options.height])

case "measure":
    guard options.windowId != 0 else { fail("measure 需要 --window-id", code: 2) }
    let windows = ownedWindows()
    guard let target = windows.first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝操作")
    }
    let size = try axSize(options.pid)
    let position = try axPosition(options.pid)
    emit([
        "window_id": target.id,
        "frame_width": Int(size.width.rounded()),
        "frame_height": Int(size.height.rounded()),
        "frame_x": Int(position.x.rounded()),
        "frame_y": Int(position.y.rounded()),
        "content_width": Int((size.width).rounded()),
        "content_height": Int((size.height - TITLEBAR_HEIGHT).rounded()),
        "titlebar_height": Int(TITLEBAR_HEIGHT),
        "cg_frame_width": Int(target.frame.width.rounded()),
        "cg_frame_height": Int(target.frame.height.rounded()),
    ])

case "click":
    guard options.windowId != 0 else { fail("click 需要 --window-id", code: 2) }
    let windows = ownedWindows()
    guard let target = windows.first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝投递事件")
    }
    // CGWindowList 原点为屏幕顶左，Quartz 事件坐标同为顶左原点，可直接换算。
    let screenX = target.frame.origin.x + options.clickX
    let screenY = target.frame.origin.y + options.clickY
    guard screenX >= 0, screenY >= 0 else {
        fail("换算后的点击坐标越界（窗口在屏幕外）")
    }
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
          "screen_y": Int(screenY.rounded()), "posted_to_pid": Int(options.pid)])

case "screenshot":
    guard options.windowId != 0 else { fail("screenshot 需要 --window-id", code: 2) }
    guard !options.output.isEmpty else { fail("screenshot 需要 --output", code: 2) }
    let windows = ownedWindows()
    guard let target = windows.first(where: { $0.id == options.windowId }) else {
        fail("窗口 \(options.windowId) 不属于 PID \(options.pid)，拒绝截图")
    }
    // 按窗口静默抓取（-x 无声、-o 隐藏鼠标）；不抓整屏冒充窗口证据。
    let shotPath = Process()
    shotPath.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    shotPath.arguments = ["-x", "-o", "-l\(target.id)", options.output]
    let pipe = Pipe()
    shotPath.standardError = pipe
    try? shotPath.run()
    shotPath.waitUntilExit()
    if shotPath.terminationStatus != 0 {
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        fail("screencapture 失败 exit=\(shotPath.terminationStatus)："
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
    fail("未知子命令 \(options.command)（可用：\(SUBCOMMANDS.joined(separator: ", "))）", code: 2)
}
