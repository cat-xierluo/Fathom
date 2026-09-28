//! macOS 主 WebView 的滚动边界。WebKit 私有 selector 必须先检查能力和 ABI；
//! 不可用时保留窗口可运行并给出诊断，不拦截 wheel 或修改系统滚动偏好。

use objc2::runtime::{AnyObject, Bool};
use objc2::{msg_send, sel, Encode, MainThreadMarker};
use tauri::{webview::PlatformWebview, WebviewWindow};

#[derive(Debug)]
pub struct MaskChange {
    pub before: usize,
    pub after: usize,
}

pub fn install(window: &WebviewWindow) -> tauri::Result<()> {
    window.with_webview(|webview| match disable_rubber_banding(&webview) {
        Ok(change) => eprintln!(
            "[scroll-boundary] 原生回弹 mask {} → {}",
            change.before, change.after
        ),
        Err(err) => eprintln!("[scroll-boundary] 未关闭原生回弹：{err}"),
    })
}

fn compatible_types(
    getter_count: usize,
    getter_return: &str,
    setter_count: usize,
    setter_return: &str,
    setter_argument: &str,
) -> bool {
    getter_count == 2
        && setter_count == 3
        && setter_return == "v"
        && usize::ENCODING.equivalent_to_str(getter_return)
        && usize::ENCODING.equivalent_to_str(setter_argument)
}

fn checked_view(webview: &PlatformWebview) -> Result<&AnyObject, String> {
    if MainThreadMarker::new().is_none() {
        return Err("WKWebView 必须在主线程访问".into());
    }
    let pointer = webview.inner().cast::<AnyObject>();
    // SAFETY: Tauri with_webview 提供本次闭包存活期间的 WKWebView；仅在主线程
    // 局部借用，不保存指针。空句柄和私有方法 ABI 均在发送消息前检查。
    let view = unsafe { pointer.as_ref() }.ok_or("WKWebView 句柄为空")?;
    let getter = sel!(_rubberBandingEnabled);
    let setter = sel!(_setRubberBandingEnabled:);
    let (has_getter, has_setter): (Bool, Bool) = unsafe {
        (
            msg_send![view, respondsToSelector: getter],
            msg_send![view, respondsToSelector: setter],
        )
    };
    if !has_getter.as_bool() || !has_setter.as_bool() {
        return Err("当前 WebKit 不提供 rubber-banding selector".into());
    }
    let getter_method = view
        .class()
        .instance_method(getter)
        .ok_or("无法读取 rubber-banding getter 方法签名")?;
    let setter_method = view
        .class()
        .instance_method(setter)
        .ok_or("无法读取 rubber-banding setter 方法签名")?;
    let getter_return = getter_method.return_type();
    let setter_return = setter_method.return_type();
    let argument = setter_method
        .argument_type(2)
        .ok_or("rubber-banding setter 缺少 mask 参数")?;
    if !compatible_types(
        getter_method.arguments_count(),
        getter_return.to_str().unwrap_or(""),
        setter_method.arguments_count(),
        setter_return.to_str().unwrap_or(""),
        argument.to_str().unwrap_or(""),
    ) {
        return Err("rubber-banding 方法签名不兼容 NSUInteger mask，跳过调用".into());
    }
    Ok(view)
}

/// 只读回当前 mask，供同一初始化路径的隔离原生验证使用。
pub fn current_mask(webview: &PlatformWebview) -> Result<usize, String> {
    let view = checked_view(webview)?;
    // SAFETY: checked_view 已检查主线程、selector 存在及 NSUInteger 返回类型。
    Ok(unsafe { msg_send![view, _rubberBandingEnabled] })
}

fn disable_rubber_banding(webview: &PlatformWebview) -> Result<MaskChange, String> {
    let before = current_mask(webview)?;
    let view = checked_view(webview)?;
    // _WKRectEdge 是 NSUInteger 位掩码，0 表示各边均禁用；不能传 BOOL。
    // SAFETY: checked_view 已验证 setter 的 void 返回和 NSUInteger 参数 ABI。
    unsafe {
        let _: () = msg_send![view, _setRubberBandingEnabled: 0usize];
    }
    let after = current_mask(webview)?;
    if after != 0 {
        return Err(format!("设置后 mask 仍为 {after}（原值 {before}）"));
    }
    Ok(MaskChange { before, after })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mask_abi_rejects_boolean_and_wrong_integer_width() {
        let mask = usize::ENCODING.to_string();
        assert!(compatible_types(2, &mask, 3, "v", &mask));
        for incorrect in ["B", "c", "I", "q", "@", ""] {
            assert!(!compatible_types(2, incorrect, 3, "v", &mask));
            assert!(!compatible_types(2, &mask, 3, "v", incorrect));
        }
    }

    #[test]
    fn mask_abi_rejects_wrong_arity_or_setter_return() {
        let mask = usize::ENCODING.to_string();
        assert!(!compatible_types(3, &mask, 3, "v", &mask));
        assert!(!compatible_types(2, &mask, 2, "v", &mask));
        assert!(!compatible_types(2, &mask, 3, "B", &mask));
    }
}
